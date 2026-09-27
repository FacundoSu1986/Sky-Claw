"""Spawn strategy de TexGen/DynDOLOD dentro de una sesión MO2/USVFS.

Este módulo es el único lugar que conoce cómo traducir la identidad de una
herramienta DynDOLOD a un ``VfsJob``. El runner recibe solamente una strategy y
conserva su lifecycle habitual: PID, readiness UIA/HITL, deadline y resultado.

El worker no recibe un command string ni reconstruye switches. La lista argv que
llega acá es exactamente la que ``DynDOLODRunner._build_xedit_args`` ya produjo.

La MISMA strategy responde la pregunta de handoff antes del spawn de DynDOLOD:
en el backend brokered la verdad vive en el perfil MO2 + USVFS, no en el
``Data`` físico. El gate (:meth:`BrokeredDynDOLODSpawnStrategy.verify_texgen_handoff`)
demuestra, fail-closed, que DynDOLOD verá exactamente el TexGen Output
autorizado: perfil idéntico al del ``VfsJob``, mod real y habilitado, identidad
del artifact, efectividad byte-exact sobre el overlay completo Y evidencia
runtime de que el mapping USVFS se aplica. Un canary NO reemplaza el recorrido
estructural ni al revés: cada pieza prueba una propiedad distinta (ver
``sky_claw/local/mo2/mod_effectivity.py``).

**Binding gate→spawn (anti-TOCTOU):** cuando el gate aprueba, el veredicto
porta un ``TexGenHandoffApproval`` con el ESTADO exacto aprobado (perfil +
fingerprint, identidad del artifact, mod fuente, identidad del canary), y
:meth:`BrokeredDynDOLODSpawnStrategy.spawn` lo REVALIDA completo en su boundary
antes de abrir la sesión — el estado puede cambiar entre la prueba y el spawn,
y un challenge reconstruido a ciegas certificaría el estado nuevo, no el
aprobado. La revalidación vuelve a atestiguar el mapping runtime porque la
sesión se abre con el challenge revalidado y el worker corre
``verify_vfs_attestation`` + probe del nieto antes de despachar la herramienta.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import pathlib
import stat
from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol

from sky_claw.app.security.links import link_kind_and_identity_or_raise
from sky_claw.local.mo2.mod_effectivity import ModEffectivityError, verificar_artifact_efectivo
from sky_claw.local.mo2.vfs_attestation import (
    VfsAttestationChallenge,
    VfsAttestationError,
    build_attestation_challenge,
    build_attestation_challenge_for_source,
    read_enabled_mods,
)
from sky_claw.local.mo2.vfs_contracts import (
    VFS_TOOL_EXECUTABLE_NAMES,
    VfsJob,
    VfsJobResult,
)
from sky_claw.local.mo2.vfs_session import VfsProcessSession
from sky_claw.local.tools.artifact_digest import digest_arbol
from sky_claw.local.tools.texgen_handoff import (
    HandoffDriftError,
    TexGenHandoffApproval,
    TexGenHandoffRequest,
    TexGenHandoffResult,
)

if TYPE_CHECKING:
    from sky_claw.local.tools.dyndolod_runner import DynDOLODProcess

logger = logging.getLogger("SkyClaw.BrokeredDynDOLOD")

#: Timeout del probe ``health`` de handoff. Es una atestación + un hash de un
#: archivo: segundos, no la ventana de UIA de una herramienta. El job completo
#: muere con este techo aunque el bridge se cuelgue.
_PROBE_HANDOFF_TIMEOUT_SECONDS = 120.0


class BrokeredDynDOLODProtocol(Protocol):
    async def open_session(
        self,
        job: VfsJob,
        *,
        challenge: VfsAttestationChallenge,
        data_root: pathlib.Path | None = None,
        mods_dir: pathlib.Path | None = None,
        install_root: pathlib.Path | None = None,
        virtual_data_dir: pathlib.Path,
        overwrite_mod: str | None = None,
    ) -> VfsProcessSession: ...

    async def submit(
        self,
        job: VfsJob,
        *,
        challenge: VfsAttestationChallenge,
        data_root: pathlib.Path | None = None,
        mods_dir: pathlib.Path | None = None,
        install_root: pathlib.Path | None = None,
        virtual_data_dir: pathlib.Path,
        overwrite_mod: str | None = None,
    ) -> VfsJobResult: ...


class BrokeredDynDOLODProcess:
    """Adaptador mínimo de ``VfsProcessSession`` al proceso que consume el runner."""

    def __init__(self, session: VfsProcessSession) -> None:
        self._session = session
        self._cancel_task: asyncio.Task[None] | None = None
        self._output: tuple[str, str] | None = None

    @property
    def backend_managed(self) -> bool:
        return True

    @property
    def pid(self) -> int:
        return self._session.pid

    @property
    def returncode(self) -> int | None:
        return self._session.returncode

    @property
    def stdout(self) -> None:
        # La captura bounded vive en el worker. No se crea un stream IPC nuevo.
        return None

    @property
    def stderr(self) -> None:
        return None

    def assign_job(self) -> None:
        """El Job Object pertenece al worker/bridge, nunca al daemon."""
        return None

    def kill(self) -> None:
        """Compatibilidad con ``kill_and_reap`` sin matar el PID desde el daemon.

        El helper histórico llama a ``kill`` seguido de ``wait``. Para una
        sesión brokered ambos métodos se traducen a ``session.cancel()``; no hay
        ``os.kill``, ``taskkill`` ni ``proc.kill`` sobre el tool remoto.
        """
        if self._cancel_task is None:
            self._cancel_task = asyncio.create_task(self._session.cancel())

    async def terminate(self) -> None:
        """Teardown del backend brokered: la sesión es la autoridad."""
        await self._session.cancel()

    async def wait(self) -> int:
        if self._cancel_task is not None:
            await self._cancel_task
            return self._session.returncode if self._session.returncode is not None else -1
        return await self._session.wait()

    async def captured_output(self) -> tuple[str, str] | None:
        if self._output is None:
            result = await self._session.result()
            self._output = (result.stdout, result.stderr)
        return self._output


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _veredicto_de_evidencia_runtime(
    resultado: VfsJobResult,
    *,
    challenge: VfsAttestationChallenge,
    request: TexGenHandoffRequest,
    approval: TexGenHandoffApproval,
) -> TexGenHandoffResult:
    """Traduce el probe ``health`` a veredicto de visibilidad USVFS (fail-closed).

    El worker corre el MISMO contrato de attestation que una herramienta real:
    ``verify_vfs_attestation`` (fingerprint del perfil + hash del canary visible
    en el ``virtual_data_dir``) y el probe del proceso nieto. Un campo faltante,
    extraño o distinto de lo esperado es UNKNOWN ⇒ bloqueo; jamás "probablemente
    anduvo".
    """
    if not resultado.success:
        return TexGenHandoffResult.bloqueado(
            "la evidencia runtime USVFS no se pudo obtener (el probe health falló "
            f"bajo el perfil '{challenge.profile}'): {resultado.message or 'sin detalle'}. "
            "Sin evidencia runtime no se afirma la visibilidad."
        )
    atestacion = resultado.attestation
    if not isinstance(atestacion, Mapping):
        return TexGenHandoffResult.bloqueado(
            "el probe health volvió sin atestación estructurada: la visibilidad USVFS es indeterminada"
        )
    if atestacion.get("profile") != challenge.profile or atestacion.get("source_mod") != request.mod_name:
        return TexGenHandoffResult.bloqueado(
            "la atestación runtime corresponde a otro perfil/mod que el pedido "
            f"(perfil={atestacion.get('profile')!r}, source_mod={atestacion.get('source_mod')!r}): "
            "evidencia fuera de contrato"
        )
    if atestacion.get("relative_path") != challenge.relative_path.as_posix():
        return TexGenHandoffResult.bloqueado(
            f"la atestación runtime observó otro canary que el pedido "
            f"({atestacion.get('relative_path')!r}): evidencia fuera de contrato"
        )
    if atestacion.get("profile_fingerprint") != challenge.profile_fingerprint:
        return TexGenHandoffResult.bloqueado(
            "el fingerprint del perfil cambió entre la construcción del challenge y el probe runtime "
            "(drift de perfil): la evidencia no corresponde al estado bajo el que se lanzaría DynDOLOD"
        )
    if atestacion.get("visible_sha256") != challenge.sha256:
        return TexGenHandoffResult.bloqueado(
            f"el canario '{challenge.relative_path.as_posix()}' no tiene los bytes autorizados bajo "
            "USVFS: la vista virtual no entrega el contenido del artifact"
        )
    if atestacion.get("grandchild_sha256") != challenge.sha256:
        return TexGenHandoffResult.bloqueado(
            "el proceso nieto bajo USVFS no leyó los bytes autorizados del canario: el mapping no se "
            "aplica como se esperaba"
        )
    return TexGenHandoffResult.aprobado(
        f"visibilidad MO2/USVFS demostrada: '{request.mod_name}' habilitado y efectivo en el perfil "
        f"'{challenge.profile}' ({request.staging.name}/ completo contra el overlay) y canary "
        f"{challenge.relative_path.as_posix()} atestiguado por worker + nieto",
        approval=approval,
    )


class BrokeredDynDOLODSpawnStrategy:
    """Construye un challenge/job nuevo y abre una sesión por herramienta."""

    def __init__(
        self,
        *,
        broker: BrokeredDynDOLODProtocol,
        instance_id: str,
        profile: str,
        data_root: pathlib.Path,
        mods_dir: pathlib.Path,
        install_root: pathlib.Path,
        physical_data_dir: pathlib.Path,
        virtual_data_dir: pathlib.Path,
        output_roots: Mapping[str, pathlib.Path],
    ) -> None:
        self._broker = broker
        self._instance_id = instance_id
        self._profile = profile
        self._data_root = data_root.resolve()
        self._mods_dir = mods_dir.resolve()
        self._install_root = install_root.resolve()
        self._physical_data_dir = physical_data_dir.resolve()
        self._virtual_data_dir = virtual_data_dir.resolve()
        self._output_roots = {key: value.resolve() for key, value in output_roots.items()}

    async def spawn(
        self,
        *,
        executable: pathlib.Path,
        args: list[str],
        tool_name: str,
        cwd: pathlib.Path,
        timeout: float,
        handoff: TexGenHandoffApproval | None = None,
    ) -> DynDOLODProcess:
        tool_id = {"TexGen": "texgen", "DynDOLOD": "dyndolod"}.get(tool_name)
        if tool_id is None:
            raise ValueError(f"herramienta no permitida para VFS: {tool_name!r}")
        if handoff is not None and tool_id != "dyndolod":
            raise ValueError(
                f"el approval del handoff TexGen solo es válido para el spawn de DynDOLOD: llegó para {tool_name!r}"
            )
        validated_executable = self._validate_executable(tool_id, executable)
        resolved_cwd = self._validate_cwd(cwd)
        output_root = self._output_roots.get(tool_id)
        if output_root is None:
            raise ValueError(f"falta output root brokered para {tool_id}")

        if handoff is not None:
            # Binding gate→spawn (anti-TOCTOU): el estado aprobado se revalida
            # COMPLETO, fail-closed, en el boundary del spawn. El challenge que
            # abre la sesión es el REVALIDADO — idéntico al aprobado en cada
            # campo ligado — y el worker lo vuelve a atestiguar dentro del VFS
            # (verify_vfs_attestation + probe del nieto) antes de despachar.
            challenge = await self._revalidar_aprobacion(handoff)
        else:
            # Camino legacy sin artifact gateado ("DynDOLOD solo"): no hay
            # estado aprobado que revalidar. Se construye SIEMPRE por spawn:
            # TexGen y DynDOLOD nunca comparten un challenge.
            challenge = await asyncio.to_thread(
                build_attestation_challenge,
                data_root=self._data_root,
                mods_dir=self._mods_dir,
                profile=self._profile,
                physical_data_dir=self._physical_data_dir,
            )
        job = VfsJob.create(
            instance_id=self._instance_id,
            profile=self._profile,
            tool_id=tool_id,
            payload={
                "executable": str(validated_executable),
                "argv": list(args),
                "cwd": str(resolved_cwd),
            },
            timeout_seconds=float(timeout),
            expected_fingerprint=challenge.profile_fingerprint,
            mutation_targets=(output_root,),
        )
        session = await self._broker.open_session(
            job,
            challenge=challenge,
            data_root=self._data_root,
            mods_dir=self._mods_dir,
            install_root=self._install_root,
            virtual_data_dir=self._virtual_data_dir,
        )
        return BrokeredDynDOLODProcess(session)

    async def _revalidar_aprobacion(self, handoff: TexGenHandoffApproval) -> VfsAttestationChallenge:
        """Revalida el approval en el boundary del spawn y devuelve el challenge.

        El gate aprobó un ESTADO; el spawn ocurre después, y en el medio el
        perfil o el artifact pueden haber cambiado. Reconstruir un challenge a
        ciegas certificaría el estado NUEVO (hasta con un canary de otro mod).
        Por eso se re-verifica, en orden y fail-closed, todo lo que el token
        ligó:

        1. **perfil esperado** idéntico al del ``VfsJob`` de DynDOLOD;
        2. **namespace** (el ``Data`` del approval es el ``-d:`` de este backend);
        3. **artifact real** bajo ``mods_dir`` (sin symlinks/junctions);
        4. **identidad del árbol completo** (digest/files/bytes) — cubre también
           los archivos NO-canary que el recorrido del gate ya validó;
        5. **enablement** (``+TexGen Output`` habilitado una sola vez);
        6. **efectividad byte-exact** sobre el overlay completo (un override
           incompatible nuevo no cambia el fingerprint del perfil);
        7. **fingerprint del perfil + identidad del canary** contra un challenge
           reconstruido desde el estado actual.

        Cualquier divergencia o estado indeterminado lanza
        :class:`HandoffDriftError` — el proceso no nace.
        """
        # --- 1. perfil esperado (B4) ---
        if handoff.profile is None or handoff.profile != self._profile:
            raise HandoffDriftError(
                f"el perfil aprobado ({handoff.profile!r}) no es el perfil del job ({self._profile!r}): "
                "el handoff no cruza perfiles"
            )
        # --- 2. namespace coherente ---
        if handoff.data_dir.resolve() != self._virtual_data_dir:
            raise HandoffDriftError(
                f"el Data bajo el que se aprobó el handoff ({handoff.data_dir}) no es el namespace "
                f"virtual de este backend ({self._virtual_data_dir})"
            )
        # --- 3. artifact real bajo mods_dir, sin symlinks/junctions ---
        mod_dir = self._mods_dir / handoff.mod_name
        artifact_root = handoff.artifact_root
        try:
            raiz_esperada = (mod_dir / artifact_root.name).resolve()
        except OSError as e:
            raise HandoffDriftError(f"no se pudo resolver el artifact aprobado '{artifact_root}': {e}") from e
        if artifact_root.resolve() != raiz_esperada:
            raise HandoffDriftError(
                f"el artifact aprobado '{artifact_root}' no vive bajo '{mod_dir}': token incoherente"
            )
        for etiqueta, ruta in (("mod", mod_dir), ("artifact", artifact_root)):
            tipo, identidad = link_kind_and_identity_or_raise(ruta)
            if identidad is None or tipo is not None or not stat.S_ISDIR(identidad.st_mode):
                raise HandoffDriftError(
                    f"el {etiqueta} '{ruta}' ya no es un directorio propio bajo el overlay "
                    "(ausente o es un enlace): el estado aprobado no se sostiene"
                )
        # --- 4. identidad del árbol COMPLETO (archivos canary y no-canary) ---
        try:
            observado = await asyncio.to_thread(digest_arbol, artifact_root)
        except OSError as e:
            raise HandoffDriftError(
                f"no se pudo re-identificar el artifact '{artifact_root}' en el boundary del spawn: {e}"
            ) from e
        if (observado.digest, observado.files, observado.bytes) != (
            handoff.artifact.digest,
            handoff.artifact.files,
            handoff.artifact.bytes,
        ):
            raise HandoffDriftError(
                f"el artifact '{artifact_root}' cambió después de la aprobación del handoff "
                f"(aprobado: {handoff.artifact.files} archivo(s)/{handoff.artifact.bytes} byte(s)/"
                f"{handoff.artifact.digest[:12]}; actual: {observado.files} archivo(s)/"
                f"{observado.bytes} byte(s)/{observado.digest[:12]}): no se lanza DynDOLOD sobre "
                "bytes que no se aprobaron"
            )
        # --- 5. habilitado en el perfil (B3) ---
        try:
            habilitados = await asyncio.to_thread(
                read_enabled_mods,
                self._data_root / "profiles" / self._profile / "modlist.txt",
            )
        except VfsAttestationError as e:
            raise HandoffDriftError(
                f"no se pudo releer el estado del perfil '{self._profile}' en el boundary del spawn (UNKNOWN): {e}"
            ) from e
        if habilitados.count(handoff.mod_name) != 1:
            raise HandoffDriftError(
                f"'{handoff.mod_name}' ya no está habilitado exactamente una vez en el perfil "
                f"'{self._profile}' (aparece {habilitados.count(handoff.mod_name)}): el overlay ya no "
                "entrega el artifact aprobado"
            )
        # --- 6. efectividad byte-exact sobre el overlay COMPLETO (B9/B10) ---
        try:
            await asyncio.to_thread(
                verificar_artifact_efectivo,
                artifact_root=artifact_root,
                mod_name=handoff.mod_name,
                mods_dir=self._mods_dir,
                data_root=self._data_root,
                enabled=habilitados,
            )
        except ModEffectivityError as e:
            raise HandoffDriftError(f"la efectividad aprobada ya no se sostiene en el boundary del spawn: {e}") from e
        # --- 7. fingerprint del perfil + identidad del canary (B6/B7/B8) ---
        if handoff.profile_fingerprint is None or handoff.canary_relative_path is None or handoff.canary_sha256 is None:
            raise HandoffDriftError(
                "el approval brokered llegó sin fingerprint/canary ligados: contrato roto, se bloquea"
            )
        try:
            fresh = await asyncio.to_thread(
                build_attestation_challenge_for_source,
                source_mod=handoff.mod_name,
                data_root=self._data_root,
                mods_dir=self._mods_dir,
                profile=self._profile,
                physical_data_dir=self._physical_data_dir,
            )
        except VfsAttestationError as e:
            raise HandoffDriftError(
                f"el canary aprobado ya no es elegible en el boundary del spawn (UNKNOWN): {e}"
            ) from e
        if (
            fresh.relative_path != handoff.canary_relative_path
            or fresh.sha256 != handoff.canary_sha256
            or fresh.profile_fingerprint != handoff.profile_fingerprint
        ):
            raise HandoffDriftError(
                "el estado del perfil o el canary aprobado cambió entre el gate y el spawn "
                f"(canary aprobado {handoff.canary_relative_path.as_posix()}/{handoff.canary_sha256[:12]} "
                f"fingerprint {handoff.profile_fingerprint[:12]}; actual "
                f"{fresh.relative_path.as_posix()}/{fresh.sha256[:12]} fingerprint "
                f"{fresh.profile_fingerprint[:12]}): la evidencia ya no corresponde al estado bajo "
                "el que se lanzaría DynDOLOD"
            )
        # El `fresh` devuelto es IGUAL al approval en cada campo ligado (la
        # comparación estricta de arriba lo garantiza): la sesión se abre con
        # los valores APROBADOS, no con "lo que había ahora". El worker entonces
        # vuelve a correr `verify_vfs_attestation` + probe del nieto y compara
        # el estado ACTUAL contra esos valores aprobados (fingerprint y hashes
        # del canary recomputados): un drift en la ventana
        # revalidación→open_session NO certifica el estado nuevo, FALLA la
        # apertura de la sesión (fail-closed) y el proceso no nace.
        return fresh

    async def verify_texgen_handoff(self, request: TexGenHandoffRequest) -> TexGenHandoffResult:
        """Gate MO2/USVFS del handoff TexGen → DynDOLOD. Fail-closed, completo.

        La pregunta es la misma del gate físico —*¿DynDOLOD verá exactamente el
        TexGen Output autorizado?*— pero la respuesta vive en otro dominio:
        DynDOLOD brokered lee a través del overlay del perfil bajo USVFS, no del
        ``Data`` físico del host. El gate demuestra, en orden:

        1. **Identidad de perfil**: el perfil del request —el dueño del
           artifact— es exactamente el que usará el ``VfsJob`` de DynDOLOD.
        2. **Namespace coherente**: el ``Data`` del request es el
           ``virtual_data_dir`` de este backend (el ``-d:`` que el worker
           revalida).
        3. **Artifact real**: ``mods/<mod>/<textures>`` existe como directorio
           propio (no symlink/junction) y sus bytes son los del árbol autorizado.
        4. **Habilitado** en ese perfil (``+TexGen Output``). Deshabilitado es el
           corte de PRIMERA ejecución: acción humana ``profile_enablement`` —
           Sky-Claw NO edita ``modlist.txt``.
        5. **Efectivo**: cada archivo gana el overlay byte-exact (sin mod de
           mayor prioridad ni ``overwrite`` incompatible) — la parte que ningún
           canary puede probar.
        6. **Evidencia runtime USVFS**: un probe ``health`` (tool allowlisted,
           read-only) atestigua worker + nieto sobre un canary DEL PROPIO MOD,
           con fingerprint del perfil fresco. Es la parte que ningún análisis
           host-side puede probar.

        Cualquier paso indeterminado bloquea. Un canary no es un tree digest:
        (3)+(5) prueban el artifact completo; (6) prueba que el mapping se
        aplica realmente.
        """
        try:
            return await self._verificar_handoff_mo2(request)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 -- boundary del gate: bug interno ⇒ bloqueo, nunca spawn
            # Deliberado y acotado: cada paso conocido ya devuelve `bloqueado`
            # con su razón; esta frontera convierte lo DESCONOCIDO en bloqueo
            # (fail-closed del gate: "cualquier paso indeterminado bloquea").
            # Queda registrado con su repr para que la regresión sea auditable
            # (y las suites del gate ejercitan cada modo de fallo conocido).
            logger.error(
                "Gate brokered de TexGen: fallo inesperado (%r) — se bloquea cerrado",
                e,
                exc_info=True,
                extra={"operation_type": "dyndolod_texgen_handoff_brokered_inesperado"},
            )
            return TexGenHandoffResult.bloqueado(
                f"no se pudo demostrar la visibilidad MO2/USVFS del '{request.mod_name}' "
                f"(fallo inesperado: {e!r}): se bloquea cerrado"
            )

    async def _verificar_handoff_mo2(self, request: TexGenHandoffRequest) -> TexGenHandoffResult:
        # --- 1. perfil dueño == perfil del VfsJob que lanzará DynDOLOD (B4) ---
        if request.expected_profile is None:
            return TexGenHandoffResult.bloqueado(
                "el perfil dueño del TexGen Output es desconocido (UNKNOWN): sin identidad de dueño "
                "no se puede demostrar bajo qué perfil debe verse el artifact"
            )
        if request.expected_profile != self._profile:
            return TexGenHandoffResult.bloqueado(
                f"el artifact fue autorizado bajo el perfil '{request.expected_profile}' pero "
                f"DynDOLOD se lanzaría bajo '{self._profile}': el handoff no cruza perfiles"
            )

        # --- 2. namespace coherente: el Data del request es el -d: del job ---
        data_del_request = request.data_dir.resolve()
        if data_del_request != self._virtual_data_dir:
            return TexGenHandoffResult.bloqueado(
                f"el Data contra el que se pide verificar ({data_del_request}) no es el namespace "
                f"virtual de este backend ({self._virtual_data_dir}): la prueba se haría en el "
                "dominio equivocado"
            )

        # --- 3. artifact real bajo mods_dir, sin symlinks/junctions ---
        mod_dir = self._mods_dir / request.mod_name
        artifact_root = mod_dir / request.staging.name
        for etiqueta, ruta in (("mod", mod_dir), ("artifact", artifact_root)):
            tipo, identidad = link_kind_and_identity_or_raise(ruta)
            if identidad is None or tipo is not None or not stat.S_ISDIR(identidad.st_mode):
                return TexGenHandoffResult.bloqueado(
                    f"el {etiqueta} '{ruta}' no existe como directorio propio bajo el overlay "
                    "(ausente o es un enlace): no hay árbol cuya visibilidad se pueda demostrar"
                )

        # --- 3b. identidad: el árbol del mod porta EXACTAMENTE los bytes autorizados (B5) ---
        # El digest del mod se computa SIEMPRE: además de la comparación contra
        # el staging, es la identidad que el approval liga al spawn (anti-TOCTOU).
        try:
            staging_resuelto = request.staging.resolve()
            dig_mod = await asyncio.to_thread(digest_arbol, artifact_root)
        except OSError as e:
            return TexGenHandoffResult.bloqueado(
                f"no se pudo identificar el artifact '{artifact_root}' contra el staging autorizado: {e}"
            )
        if staging_resuelto != artifact_root.resolve():
            try:
                dig_authorized = await asyncio.to_thread(digest_arbol, request.staging)
            except OSError as e:
                return TexGenHandoffResult.bloqueado(
                    f"no se pudo identificar el staging autorizado '{request.staging}': {e}"
                )
            if (dig_mod.digest, dig_mod.files, dig_mod.bytes) != (
                dig_authorized.digest,
                dig_authorized.files,
                dig_authorized.bytes,
            ):
                return TexGenHandoffResult.bloqueado(
                    f"el artifact empaquetado '{artifact_root}' no coincide con el staging autorizado "
                    f"({dig_authorized.files} archivo(s)/{dig_authorized.bytes} byte(s) autorizados vs "
                    f"{dig_mod.files} archivo(s)/{dig_mod.bytes} byte(s) empaquetados): no se lanza "
                    "DynDOLOD sobre bytes que no se pueden atribuir"
                )

        # --- 4. habilitado en el perfil (B3). Deshabilitado = primera ejecución. ---
        try:
            habilitados = await asyncio.to_thread(
                read_enabled_mods,
                self._data_root / "profiles" / self._profile / "modlist.txt",
            )
        except VfsAttestationError as e:
            return TexGenHandoffResult.bloqueado(
                f"no se pudo leer el estado del perfil '{self._profile}' (UNKNOWN): {e}"
            )
        if habilitados.count(request.mod_name) > 1:
            return TexGenHandoffResult.bloqueado(
                f"el modlist del perfil '{self._profile}' repite '{request.mod_name}': la prioridad efectiva es ambigua"
            )
        if request.mod_name not in habilitados:
            return TexGenHandoffResult.bloqueado(
                f"'{request.mod_name}' existe pero NO está habilitado en el perfil '{self._profile}' "
                f"(falta la línea '+{request.mod_name}' en modlist.txt). Habilitalo en Mod Organizer 2 "
                "y reanudá: Sky-Claw no edita modlist.txt. Sin esa línea el mod no entra al overlay "
                "USVFS y DynDOLOD no vería sus texturas.",
                pending_action="profile_enablement",
            )

        # --- 5. efectividad byte-exact sobre el overlay COMPLETO del artifact (B9/B10) ---
        try:
            await asyncio.to_thread(
                verificar_artifact_efectivo,
                artifact_root=artifact_root,
                mod_name=request.mod_name,
                mods_dir=self._mods_dir,
                data_root=self._data_root,
                enabled=habilitados,
            )
        except ModEffectivityError as e:
            return TexGenHandoffResult.bloqueado(str(e))

        # --- 6. canary DEL PROPIO artifact + probe runtime USVFS (B6/B7/B8/B11) ---
        try:
            challenge = await asyncio.to_thread(
                build_attestation_challenge_for_source,
                source_mod=request.mod_name,
                data_root=self._data_root,
                mods_dir=self._mods_dir,
                profile=self._profile,
                physical_data_dir=self._physical_data_dir,
            )
        except VfsAttestationError as e:
            return TexGenHandoffResult.bloqueado(
                f"no hay canary USVFS elegible dentro de '{request.mod_name}' para probar el mapping "
                f"(UNKNOWN): {e}. Sin evidencia runtime no se afirma la visibilidad."
            )
        # El canary se elige del mod empaquetado; esta comprobación lo ANCLA al
        # staging autorizado: un canario conveniente que no pertenezca al
        # artifact no prueba nada sobre él.
        try:
            partes = challenge.relative_path.parts
            if not partes or partes[0] != request.staging.name:
                raise ValueError(f"relative_path {challenge.relative_path} fuera del staging {request.staging.name}")
            rel_sobre_staging = pathlib.Path(*partes[1:])
            sha_staging = await asyncio.to_thread(_sha256_file, request.staging / rel_sobre_staging)
        except (OSError, ValueError) as e:
            return TexGenHandoffResult.bloqueado(
                f"el canario elegido '{challenge.relative_path.as_posix()}' no pertenece al árbol autorizado: {e}"
            )
        if sha_staging != challenge.sha256:
            return TexGenHandoffResult.bloqueado(
                f"el canario elegido '{challenge.relative_path.as_posix()}' no tiene los bytes del "
                "staging autorizado: la evidencia runtime no atestiguaría el artifact correcto"
            )

        job = VfsJob.create(
            instance_id=self._instance_id,
            profile=self._profile,
            tool_id="health",
            payload={},
            timeout_seconds=_PROBE_HANDOFF_TIMEOUT_SECONDS,
            expected_fingerprint=challenge.profile_fingerprint,
            mutation_targets=(),
        )
        try:
            resultado = await self._broker.submit(
                job,
                challenge=challenge,
                data_root=self._data_root,
                mods_dir=self._mods_dir,
                install_root=self._install_root,
                virtual_data_dir=self._virtual_data_dir,
            )
        except Exception as e:  # noqa: BLE001 -- boundary de transporte B11: sin respuesta no hay evidencia
            # Puente caído, job timeout, worker desconectado, error de
            # transporte no enumerable: sin respuesta no hay evidencia, y sin
            # evidencia no hay spawn (B11). Es el boundary del IPC, no un
            # swallow genérico: CancelledError se propaga (no es Exception).
            return TexGenHandoffResult.bloqueado(
                f"el bridge MO2/USVFS no pudo atestiguar la visibilidad del '{request.mod_name}' "
                f"({e!r}): se bloquea cerrado"
            )
        # El approval liga el ESTADO aprobado al spawn (anti-TOCTOU): perfil +
        # fingerprint, identidad del artifact, mod fuente y canary. `spawn` lo
        # revalida completo en su boundary antes de abrir la sesión.
        approval = TexGenHandoffApproval(
            mod_name=request.mod_name,
            artifact_root=artifact_root,
            artifact=dig_mod,
            data_dir=self._virtual_data_dir,
            profile=self._profile,
            profile_fingerprint=challenge.profile_fingerprint,
            canary_relative_path=challenge.relative_path,
            canary_sha256=challenge.sha256,
        )
        return _veredicto_de_evidencia_runtime(resultado, challenge=challenge, request=request, approval=approval)

    @staticmethod
    def _validate_executable(tool_id: str, executable: pathlib.Path) -> pathlib.Path:
        if not executable.is_absolute():
            raise ValueError("el executable brokered debe ser absoluto")
        if executable.is_symlink() or not executable.is_file():
            raise ValueError("el executable brokered debe ser un archivo real y no un symlink")
        resolved = executable.resolve()
        if resolved.name.casefold() != VFS_TOOL_EXECUTABLE_NAMES[tool_id]:
            raise ValueError(f"executable {resolved.name!r} no corresponde a tool_id {tool_id!r}")
        return resolved

    @staticmethod
    def _validate_cwd(cwd: pathlib.Path) -> pathlib.Path:
        if not cwd.is_absolute():
            raise ValueError("cwd brokered debe ser absoluto")
        if cwd.is_symlink() or not cwd.is_dir():
            raise ValueError("cwd brokered debe ser una carpeta real")
        return cwd.resolve()


def build_brokered_dyndolod_spawn_strategy(
    *,
    broker: BrokeredDynDOLODProtocol | None,
    instance_id: str | None,
    profile: str | None,
    data_root: pathlib.Path | None,
    mods_dir: pathlib.Path | None,
    install_root: pathlib.Path | None,
    physical_data_dir: pathlib.Path | None,
    virtual_data_dir: pathlib.Path | None,
    output_roots: Mapping[str, pathlib.Path] | None,
) -> BrokeredDynDOLODSpawnStrategy | None:
    """Seam explícito: faltan dependencias => no se activa brokered en silencio."""
    values = (
        broker,
        instance_id,
        profile,
        data_root,
        mods_dir,
        install_root,
        physical_data_dir,
        virtual_data_dir,
        output_roots,
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError("la strategy brokered requiere broker, instancia, perfil y todas las raíces")
    assert broker is not None
    assert instance_id is not None
    assert profile is not None
    assert data_root is not None
    assert mods_dir is not None
    assert install_root is not None
    assert physical_data_dir is not None
    assert virtual_data_dir is not None
    assert output_roots is not None
    return BrokeredDynDOLODSpawnStrategy(
        broker=broker,
        instance_id=instance_id,
        profile=profile,
        data_root=data_root,
        mods_dir=mods_dir,
        install_root=install_root,
        physical_data_dir=physical_data_dir,
        virtual_data_dir=virtual_data_dir,
        output_roots=output_roots,
    )
