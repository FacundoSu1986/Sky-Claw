"""BodySlide bajo MO2/USVFS (PR 2c-2b): lanzador brokered DORMIDO.

Forma de ``brokered_synthesis``: un selector explícito de backend (``standalone`` |
``brokered``), sin fallback, y un guard que falla cerrado. A diferencia de Synthesis, acá no
hay un runner paralelo: el backend brokered es solo un **lanzador** que se inyecta en el
``BodySlideRunner``. Journal, siembra, restauración y veredicto siguen siendo una sola
implementación (``bodyslide_lanzador``).

Propiedad: *solo el fence del broker convierte ``LANZADO`` en ``TERMINAL``*. ``submit``
devuelve solo después de ``worker_exit`` con el Job vacío; cualquier excepción que vete el
rollback (``exception_forbids_rollback``: teardown fallido, bridge perdido, timeout sin
terminalidad) es :class:`TerminalidadDesconocidaError` y el runner no restaura. La salida
de ese HOLD es del operador (``--mode bodyslide-recovery``).

**Dormido:** la barrera ``bodyslide_service._CONTENCION_BODYSLIDE_VERIFICADA`` sigue en
``False`` y ningún sitio productivo llama a :func:`resolve_bodyslide_lanzador` (ancla AST en
``tests/test_brokered_bodyslide.py``). Fuera de 2c-2b: registro de binarios atestiguados
(D7), censo de procesos (D8), staging con handles (D2/D9) y scratch como overwrite forzado
(D3). Hasta entonces el destino es el legacy ``<game>/BodySlide_Output/<grupo>``, que está
fuera del ``Data`` virtual.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import pathlib
from typing import NoReturn, Protocol

from sky_claw.app.db.rollback_veto import exception_forbids_rollback
from sky_claw.local.mo2.vfs_attestation import (
    VfsAttestationChallenge,
    VfsAttestationError,
    build_attestation_challenge,
    read_enabled_plugins,
)
from sky_claw.local.mo2.vfs_broker import VfsJobTimeoutError
from sky_claw.local.mo2.vfs_contracts import BODYSLIDE_EXECUTABLE_NAMES, JsonValue, VfsJob, VfsJobResult
from sky_claw.local.tools.bodyslide_lanzador import (
    PedidoDeLanzamiento,
    SalidaDelLanzamiento,
    TerminalidadDesconocidaError,
)

logger = logging.getLogger(__name__)

BODYSLIDE_BACKEND_STANDALONE = "standalone"
BODYSLIDE_BACKEND_BROKERED = "brokered"
_BACKENDS_VALIDOS: frozenset[str] = frozenset({BODYSLIDE_BACKEND_STANDALONE, BODYSLIDE_BACKEND_BROKERED})

_MOTIVO_DEL_HOLD = (
    "el backend 'brokered' de BodySlide está dormido: la barrera _CONTENCION_BODYSLIDE_VERIFICADA sigue en False "
    "(activación solo con PR 3, el rig real y la firma del propietario, brief §13.2 C5). No se lanza nada y no hay "
    "fallback a standalone."
)

_BLOQUE = 1024 * 1024


class VfsBrokerProtocol(Protocol):
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


def _sha256_de(ruta: pathlib.Path) -> str:
    hasher = hashlib.sha256()
    with ruta.open("rb") as archivo:
        while bloque := archivo.read(_BLOQUE):
            hasher.update(bloque)
    return hasher.hexdigest()


def _fallo_del_worker(resultado: VfsJobResult) -> NoReturn:
    """Traduce un fallo TIPADO del worker. Todos prueban terminalidad: el worker lo reportó."""
    tipo = resultado.tool_result.get("bodyslide_failure_kind")
    mensaje = resultado.message or f"el worker de BodySlide falló ({tipo})"
    if tipo == "timeout":
        raise TimeoutError(mensaje)
    raise RuntimeError(f"[{tipo}] {mensaje}")


class BrokeredBodySlideLanzador:
    """Lanzador que pide el spawn al broker USVFS con ``tool_id="bodyslide"``."""

    def __init__(
        self,
        *,
        broker: VfsBrokerProtocol,
        instance_id: str,
        profile: str,
        data_root: pathlib.Path,
        mods_dir: pathlib.Path,
        install_root: pathlib.Path,
        virtual_data_dir: pathlib.Path,
        overwrite_mod: str | None = None,
    ) -> None:
        if not instance_id.strip():
            raise ValueError("BodySlide brokered requiere instance_id no vacío")
        if not profile.strip():
            raise ValueError("BodySlide brokered requiere un perfil MO2 explícito")
        self._broker = broker
        self._instance_id = instance_id.strip()
        self._profile = profile
        self._data_root = data_root
        self._mods_dir = mods_dir
        self._install_root = install_root
        self._virtual_data_dir = virtual_data_dir
        self._overwrite_mod = overwrite_mod

    async def _expected_plugins(self) -> list[JsonValue]:
        """Plugins habilitados del perfil que el worker exige visibles (C2), con la lectura de LOOT y DynDOLOD.

        Sin la lista completa no hay prueba de visibilidad y el job no se arma. El error es común: no se
        lanzó nada, así que el runner restaura la config que sembró.
        """
        plugins_txt = self._data_root / "profiles" / self._profile / "plugins.txt"
        try:
            plugins = await asyncio.to_thread(read_enabled_plugins, plugins_txt, game_data_dir=self._virtual_data_dir)
        except VfsAttestationError as exc:
            raise RuntimeError(
                f"no se pudieron leer los plugins habilitados del perfil ({exc}): BodySlide no se lanza"
            ) from exc
        return list(plugins)

    def _payload(
        self, pedido: PedidoDeLanzamiento, exe_sha256: str, expected_plugins: list[JsonValue]
    ) -> dict[str, JsonValue]:
        return {
            "expected_plugins": expected_plugins,
            "bodyslide_exe": str(pedido.exe),
            "exe_sha256": exe_sha256,
            "group": pedido.group,
            "target": pedido.target,
            "preset": pedido.preset,
            "preset_verified": pedido.preset_verified,
            "build_morphs": pedido.build_morphs,
            "quiet_verified": pedido.quiet_verified,
        }

    async def lanzar(self, pedido: PedidoDeLanzamiento) -> SalidaDelLanzamiento:
        """Despacha el job. Ver la tabla de terminalidad en ``bodyslide_lanzador``."""
        if pedido.exe.name.casefold() not in BODYSLIDE_EXECUTABLE_NAMES:
            raise RuntimeError(f"ejecutable incompatible con BodySlide brokered: {pedido.exe.name}")
        exe_sha256 = await asyncio.to_thread(_sha256_de, pedido.exe)
        expected_plugins = await self._expected_plugins()
        challenge = await asyncio.to_thread(
            build_attestation_challenge,
            data_root=self._data_root,
            mods_dir=self._mods_dir,
            profile=self._profile,
            physical_data_dir=self._virtual_data_dir,
        )
        payload = self._payload(pedido, exe_sha256, expected_plugins)
        job = VfsJob.create(
            instance_id=self._instance_id,
            profile=self._profile,
            tool_id="bodyslide",
            payload=payload,
            timeout_seconds=float(pedido.timeout),
            expected_fingerprint=challenge.profile_fingerprint,
            mutation_targets=(pathlib.Path(pedido.target),),
        )
        try:
            resultado = await self._broker.submit(
                job,
                challenge=challenge,
                data_root=self._data_root,
                mods_dir=self._mods_dir,
                install_root=self._install_root,
                virtual_data_dir=self._virtual_data_dir,
                overwrite_mod=self._overwrite_mod,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # El broker marca con el veto toda excepción sin terminalidad probada (teardown fallido,
            # bridge perdido, timeout sin worker_exit). Esa es la única fuente de la decisión.
            if exception_forbids_rollback(exc):
                raise TerminalidadDesconocidaError(str(exc)) from exc
            if isinstance(exc, VfsJobTimeoutError):
                raise TimeoutError(str(exc)) from exc
            raise
        if "bodyslide_failure_kind" in resultado.tool_result:
            _fallo_del_worker(resultado)
        if resultado.exit_code is None:
            raise RuntimeError("el worker de BodySlide terminó sin código de salida")
        return SalidaDelLanzamiento(
            stdout=resultado.stdout.encode(),
            stderr=resultado.stderr.encode(),
            return_code=resultado.exit_code,
        )


class VfsRequiredBodySlideLanzador:
    """Guard fail-closed: se pidió el backend brokered y no está disponible. Jamás cae a standalone.

    Lanza un error común (no :class:`TerminalidadDesconocidaError`): no se lanzó nada, así que la
    terminalidad está probada y el runner restaura la config que sembró.
    """

    def __init__(self, reason: str) -> None:
        self._reason = reason

    @property
    def reason(self) -> str:
        return self._reason

    async def lanzar(self, pedido: PedidoDeLanzamiento) -> SalidaDelLanzamiento:
        del pedido
        raise RuntimeError(self._reason)


def _normalizar_backend(valor: object) -> str | None:
    if isinstance(valor, str):
        normalizado = valor.strip().casefold()
        if normalizado in _BACKENDS_VALIDOS:
            return normalizado
    return None


def _guard(reason: str) -> VfsRequiredBodySlideLanzador:
    mensaje = f"BodySlide brokered no disponible: {reason}"
    logger.error("%s", mensaje)
    return VfsRequiredBodySlideLanzador(mensaje)


def resolve_bodyslide_lanzador(
    *,
    backend: object,
    broker: VfsBrokerProtocol | None,
    instance_id: str | None,
    profile: str | None,
    data_root: pathlib.Path | None,
    mods_dir: pathlib.Path | None,
    install_root: pathlib.Path | None,
    virtual_data_dir: pathlib.Path | None,
    overwrite_mod: str | None = None,
) -> BrokeredBodySlideLanzador | VfsRequiredBodySlideLanzador | None:
    """Resuelve el lanzador según el selector explícito.

    * ``standalone`` → ``None``: el runner usa ``LanzadorStandalone``.
    * ``brokered`` → :class:`BrokeredBodySlideLanzador` si la barrera está levantada y todo es
      válido; si no, :class:`VfsRequiredBodySlideLanzador`.
    * cualquier otro valor → :class:`VfsRequiredBodySlideLanzador` (jamás fallback).
    """
    # La barrera se lee en cada resolución: es la del servicio, una sola para toda la tool.
    from sky_claw.local.tools import bodyslide_service

    solicitado = _normalizar_backend(backend)
    if solicitado == BODYSLIDE_BACKEND_STANDALONE:
        return None
    if solicitado != BODYSLIDE_BACKEND_BROKERED:
        return _guard(f"el backend {backend!r} no es válido ('standalone' | 'brokered'); no hay fallback.")
    if not bodyslide_service._CONTENCION_BODYSLIDE_VERIFICADA:
        return _guard(_MOTIVO_DEL_HOLD)
    if broker is None:
        return _guard("falta el VfsExecutionBroker; no se degrada a standalone.")
    if not isinstance(instance_id, str) or not instance_id.strip():
        return _guard("falta un instance_id válido.")
    if not isinstance(profile, str) or not profile.strip():
        return _guard("falta el perfil MO2 explícito.")
    if data_root is None or mods_dir is None or install_root is None or virtual_data_dir is None:
        return _guard("faltan las raíces de MO2 (data_root, mods_dir, install_root, Data virtual).")
    return BrokeredBodySlideLanzador(
        broker=broker,
        instance_id=instance_id,
        profile=profile,
        data_root=data_root,
        mods_dir=mods_dir,
        install_root=install_root,
        virtual_data_dir=virtual_data_dir,
        overwrite_mod=overwrite_mod,
    )


__all__ = [
    "BODYSLIDE_BACKEND_BROKERED",
    "BODYSLIDE_BACKEND_STANDALONE",
    "BrokeredBodySlideLanzador",
    "VfsRequiredBodySlideLanzador",
    "resolve_bodyslide_lanzador",
]
