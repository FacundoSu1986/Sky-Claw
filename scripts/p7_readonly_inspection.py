"""Inspección READ-ONLY de preparación para P7 (Frozen Runtime, #672).

Tracker: #672
ADR 0012 §29.7, §13

Qué hace
--------
Verifica, SIN mutar nada y SIN lanzar Skyrim, todo lo que la sesión P7 va a
necesitar tener probado antes de tocar el runtime real:

1. Managed Source de Steam (identidad y buildid, sólo lectura).
2. Runtime Clone objetivo y su integridad física (``st_nlink``, reparse points,
   exclusión de la Managed Source).
3. Versiones detectadas de ``SkyrimSE.exe`` y del loader de SKSE en el Clone.
4. Ruta de la instancia MO2 y el ``gamePath`` REAL que MO2 persiste.
5. Ejecutable de SKSE configurado en la instancia.
6. Configuración persistida de Sky-Claw (``skyrim_path``).
7. Estado del Desired Runtime.
8. Estado del journal y del registro de recuperación.
9. Procesos vivos que invalidarían el preflight.
10. Dependencias que siguen apuntando a Steam o al runtime saliente.

Qué NO hace
-----------
- No escribe archivos. No abre nada en modo escritura. No crea directorios.
- No lanza Skyrim, SKSE ni MO2.
- No toca ``appmanifest_489830.acf`` ni ningún estado de Steam.
- No emite la ruta completa de forma predeterminada: ``--mostrar-rutas`` la
  incluye. El valor por defecto es una huella corta + el último componente,
  suficiente para comparar dos corridas sin publicar el árbol privado.

Uso
---
    python scripts/p7_readonly_inspection.py \\
        --storage-root <FrozenRuntimeRoot> \\
        --mo2-instance <instancia MO2> \\
        --config ~/.sky_claw/config.toml \\
        [--managed-source <steamapps/common/Skyrim Special Edition>] \\
        [--clone-id <id>] [--mostrar-rutas] [--json-salida <archivo>]

Sale con 0 si el veredicto es ``READY_FOR_P7`` y con 1 en cualquier otro caso.
Un ``INDETERMINATE`` NUNCA cuenta como listo.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import sys
from typing import Any

RAIZ_REPO = pathlib.Path(__file__).resolve().parents[1]
if str(RAIZ_REPO) not in sys.path:
    sys.path.insert(0, str(RAIZ_REPO))

from sky_claw.local.discovery.scanner import read_skyrim_version  # noqa: E402
from sky_claw.local.frozen_runtime.binder_preflight import (  # noqa: E402
    BinderPreflightAssessment,
    evaluar_binder_preflight,
)
from sky_claw.local.frozen_runtime.clones import cargar_runtime_clone_record  # noqa: E402
from sky_claw.local.frozen_runtime.effective_runtime.mo2_ini import leer_game_path_de_mo2  # noqa: E402
from sky_claw.local.frozen_runtime.effective_runtime.models import ObservationState  # noqa: E402
from sky_claw.local.frozen_runtime.effective_runtime.oracle import (  # noqa: E402
    default_runtime_probes,
    desired_runtime_from_storage,
)
from sky_claw.local.frozen_runtime.journal import cargar_journal_transicion  # noqa: E402

# Veredictos LOCALES de un hallazgo. No se reusa el veredicto global: un
# hallazgo suelto no puede leerse como "listo para P7".
HALLAZGO_OK = "OK"
HALLAZGO_BLOQUEADO = "BLOCKED"
HALLAZGO_INDETERMINADO = "INDETERMINATE"

# Veredicto CONSOLIDADO del informe.
VEREDICTO_LISTO = "READY_FOR_P7"
VEREDICTO_BLOQUEADO = "BLOCKED"
VEREDICTO_INDETERMINADO = "INDETERMINATE"

#: Nombres de loader de SKSE por edición. Se listan los tres porque son
#: binarios distintos y el oráculo los audita por separado.
LOADERS_SKSE = ("skse64_loader.exe", "skse_loader.exe", "sksevr_loader.exe")


@dataclasses.dataclass(frozen=True, slots=True)
class Hallazgo:
    """Un punto verificado del informe, con su veredicto local."""

    clave: str
    veredicto: str
    detalle: str

    def como_dict(self) -> dict[str, str]:
        return {"clave": self.clave, "veredicto": self.veredicto, "detalle": self.detalle}


def _redactor(mostrar: bool) -> Any:
    """Devuelve una función que acorta rutas salvo que se pidan completas."""

    def _ruta(valor: str | os.PathLike[str] | None) -> str | None:
        if valor is None:
            return None
        texto = os.fspath(valor)
        if mostrar:
            return texto
        pista = hashlib.sha256(texto.encode("utf-8", "surrogateescape")).hexdigest()[:10]
        return f"<…{pathlib.Path(texto).name} · sha256:{pista}>"

    return _ruta


#: Rutas absolutas de Windows dentro de un texto libre. Los detalles del
#: preflight las incorporan y no pasan por ``_redactor``.
_RE_RUTA_ABSOLUTA = re.compile(r"[A-Za-z]:[\\/][^\s\"'\),;]*")


def _redactar_texto(texto: str, ruta: Any, conocidas: list[Any]) -> str:
    """Redacta rutas dentro de un detalle de texto libre.

    Los campos de primer nivel del informe se redactan con ``_redactor``, pero
    los ``detalle`` vienen de ``binder_preflight`` y traen rutas completas
    embebidas. Publicarlos sin redactar incumple la protección por defecto del
    inspector (Codex sobre #851).
    """
    resultado = texto
    # Primero las raíces conocidas, de la más larga a la más corta: si no, una
    # raíz prefijo de otra se come el texto y deja la parte final sin redactar.
    for raiz in sorted((c for c in conocidas if c), key=lambda c: len(os.fspath(c)), reverse=True):
        literal = os.fspath(raiz)
        if literal and literal in resultado:
            resultado = resultado.replace(literal, str(ruta(literal)))
    return _RE_RUTA_ABSOLUTA.sub(lambda m: str(ruta(m.group(0))), resultado)


def _leer_skyrim_path_persistido(config_path: pathlib.Path | None) -> str | None:
    """Lee ``skyrim_path`` del TOML de Sky-Claw sin escribir nada.

    Se hace con ``tomllib`` y no con el cargador de la app para que la
    inspección no dependa de que la app pueda arrancar (claves, keyring, etc.).
    """
    if config_path is None or not config_path.is_file():
        return None
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - 3.11+ siempre lo tiene
        return None
    try:
        with config_path.open("rb") as manejador:
            datos = tomllib.load(manejador)
    except (OSError, ValueError):
        return None
    valor = datos.get("skyrim_path")
    return valor if isinstance(valor, str) else None


def _version_de_ejecutable(exe: pathlib.Path) -> tuple[str, str]:
    """``(veredicto, detalle)`` sobre la versión detectada de un ejecutable."""
    if not exe.is_file():
        return HALLAZGO_BLOQUEADO, f"{exe.name} no existe en el runtime inspeccionado"
    try:
        version = read_skyrim_version(exe)
    except Exception as exc:  # noqa: BLE001 - el informe no debe caerse por un PE raro
        return HALLAZGO_INDETERMINADO, f"{exe.name}: no se pudo leer la versión ({type(exc).__name__})"
    if not version:
        return HALLAZGO_INDETERMINADO, f"{exe.name}: el parser de PE no devolvió versión"
    return HALLAZGO_OK, f"{exe.name}: {version}"


def _loader_skse_en_instancia(instance: pathlib.Path) -> tuple[str, str]:
    """Busca el loader de SKSE en el árbol del runtime inspeccionado."""
    encontrados = [nombre for nombre in LOADERS_SKSE if (instance / nombre).is_file()]
    if not encontrados:
        return HALLAZGO_BLOQUEADO, "no hay loader de SKSE en el runtime inspeccionado"
    if len(encontrados) > 1:
        return HALLAZGO_INDETERMINADO, f"hay más de un loader de SKSE: {encontrados}"
    return HALLAZGO_OK, f"loader de SKSE presente: {encontrados[0]}"


def _dependencias_en_arbol(instance: pathlib.Path, agujas: tuple[str, ...]) -> list[str]:
    """Archivos de configuración del runtime que todavía nombran ``agujas``.

    Sólo mira archivos de texto pequeños y conocidos: no recorre el árbol de
    mods (cientos de miles de archivos) porque esta inspección tiene que poder
    correr antes de una sesión, no durante una.
    """
    candidatos = (
        "SKSE/Plugins/CrashLogger.ini",
        "Data/SKSE/Plugins/EngineFixes.toml",
        "ModOrganizer.ini",
    )
    sospechosos: list[str] = []
    for relativo in candidatos:
        ruta = instance / relativo
        if not ruta.is_file():
            continue
        try:
            if ruta.stat().st_size > 512 * 1024:
                continue
            texto = ruta.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for aguja in agujas:
            if aguja and aguja.lower() in texto.lower():
                sospechosos.append(f"{relativo} menciona {aguja}")
                break
    return sospechosos


def _estado_del_journal(storage_root: pathlib.Path) -> Hallazgo:
    try:
        cargado = cargar_journal_transicion(storage_root)
    except Exception as exc:  # noqa: BLE001 - un journal corrupto es un hallazgo, no un crash
        return Hallazgo("journal", HALLAZGO_INDETERMINADO, f"el journal no se pudo leer: {exc}")
    disposicion = getattr(cargado, "disposition", None)
    valor = getattr(disposicion, "value", str(disposicion))
    # ``JournalDisposition`` usa MAYÚSCULAS: ``PENDING_PROMOTION`` y
    # ``PENDING_ROLLBACK``. Comparar con ``startswith("pending")`` no matchea
    # nunca y el chequeo reportaba OK sobre una transición interrumpida (Codex
    # sobre #851). Se compara contra los miembros del enum, no contra su texto.
    pendientes = {"PENDING_PROMOTION", "PENDING_ROLLBACK"}
    if valor in pendientes:
        return Hallazgo(
            "journal",
            HALLAZGO_BLOQUEADO,
            f"hay una transición PENDING ({valor}): la recuperación tiene que correr antes de P7",
        )
    return Hallazgo("journal", HALLAZGO_OK, f"disposición del journal: {valor}")


def _estado_de_recuperacion(storage_root: pathlib.Path) -> Hallazgo:
    """¿Quedó un registro de recuperación sin resolver de una transición anterior?"""
    # Los registros productivos se guardan en ``state/recovery/<id>.json``
    # (``binder_recovery._ruta_registro``). Un glob por ``binder_recovery*`` no
    # encuentra NINGUNO y el chequeo afirmaba «no hay registros» siempre (Codex
    # sobre #851).
    try:
        from sky_claw.local.frozen_runtime.storage import recovery_state_dir

        registros = sorted(recovery_state_dir(storage_root).glob("*.json"))
    except Exception as exc:  # noqa: BLE001 - el informe no debe caerse por el layout
        return Hallazgo("recovery", HALLAZGO_INDETERMINADO, f"no se pudo listar el storage: {exc}")
    if not registros:
        return Hallazgo("recovery", HALLAZGO_OK, "no hay registros de recuperación pendientes")
    return Hallazgo(
        "recovery",
        HALLAZGO_BLOQUEADO,
        f"hay {len(registros)} registro(s) de recuperación en el storage: revisarlos antes de P7",
    )


def _procesos_vivos() -> Hallazgo:
    foto = default_runtime_probes().processes()
    if foto.indeterminate:
        return Hallazgo("procesos", HALLAZGO_INDETERMINADO, f"tabla de procesos ilegible: {foto.detail}")
    if foto.processes:
        descripcion = ", ".join(f"{p.kind.value}:{p.pid}" for p in foto.processes)
        return Hallazgo("procesos", HALLAZGO_BLOQUEADO, f"procesos que invalidan el preflight: {descripcion}")
    return Hallazgo("procesos", HALLAZGO_OK, "sin MO2, Skyrim ni SKSE vivos")


def _hallazgos_del_preflight(evaluacion: BinderPreflightAssessment) -> list[Hallazgo]:
    hallazgos: list[Hallazgo] = []
    for chequeo in evaluacion.checks:
        if chequeo.status.value == "pass":
            continue
        veredicto = VEREDICTO_BLOQUEADO if chequeo.status.value == "fail" else VEREDICTO_INDETERMINADO
        hallazgos.append(
            Hallazgo(
                f"preflight.{chequeo.check.value}",
                veredicto,
                f"{chequeo.reason.value}: {chequeo.detail}".strip(),
            )
        )
    return hallazgos


def construir_informe(
    *,
    storage_root: pathlib.Path,
    mo2_instance_dir: pathlib.Path,
    config_path: pathlib.Path | None,
    managed_source_root: pathlib.Path | None,
    clone_id: str | None,
    mostrar_rutas: bool,
) -> tuple[dict[str, Any], pathlib.Path | None]:
    """Arma el informe completo. Función pura respecto del disco: sólo lee."""
    ruta = _redactor(mostrar_rutas)
    hallazgos: list[Hallazgo] = []

    # --- 1. Desired Runtime -------------------------------------------------
    desired = desired_runtime_from_storage(storage_root)
    # ``desired_runtime_from_storage`` devuelve ``clone_root=None`` cuando el
    # Desired NO se pudo resolver (p. ej. una Generation que no es del linaje del
    # clone). Tener id no alcanza: sin raíz resuelta, P7 no puede afirmar sobre
    # qué runtime se va a ejecutar (Codex sobre #851).
    resuelto = bool(desired.clone_id) and desired.clone_root is not None
    hallazgos.append(
        Hallazgo(
            "desired_runtime",
            HALLAZGO_OK if resuelto else HALLAZGO_BLOQUEADO,
            f"clone={desired.clone_id!r} generation={desired.generation_id!r} root={ruta(desired.clone_root)}"
            + ("" if resuelto else " · el Desired NO se resuelve a una raíz: no se puede afirmar el runtime efectivo"),
        )
    )

    # --- 2. Clone objetivo --------------------------------------------------
    objetivo = clone_id or desired.clone_id
    registro = cargar_runtime_clone_record(storage_root, objetivo) if objetivo else None
    if registro is None:
        hallazgos.append(Hallazgo("runtime_clone", HALLAZGO_BLOQUEADO, f"no hay registro para el clone {objetivo!r}"))
        clone_root: pathlib.Path | None = None
    else:
        clone_root = pathlib.Path(registro.root_path)
        hallazgos.append(
            Hallazgo(
                "runtime_clone",
                HALLAZGO_OK,
                f"clone={registro.clone_id} lifecycle={registro.lifecycle.value} "
                f"generation={registro.source_generation_id} root={ruta(clone_root)}",
            )
        )
        # El preflight posterior se invoca con ``clone_id=objetivo`` y por eso no
        # compara el Desired contra la Generation persistida. Esa coherencia se
        # exige acá o nadie la exige (Codex sobre #851).
        if desired.clone_id and (
            desired.clone_id != registro.clone_id or desired.generation_id != registro.source_generation_id
        ):
            hallazgos.append(
                Hallazgo(
                    "desired_coherencia",
                    HALLAZGO_BLOQUEADO,
                    "el Desired declara "
                    f"({desired.clone_id!r}, {desired.generation_id!r}) y el Clone inspeccionado es "
                    f"({registro.clone_id!r}, {registro.source_generation_id!r})",
                )
            )

    # --- 3. Versiones del Clone --------------------------------------------
    if clone_root is not None and clone_root.is_dir():
        veredicto, detalle = _version_de_ejecutable(clone_root / "SkyrimSE.exe")
        hallazgos.append(Hallazgo("version.skyrim", veredicto, detalle))
        veredicto, detalle = _loader_skse_en_instancia(clone_root)
        hallazgos.append(Hallazgo("skse.loader", veredicto, detalle))
    else:
        hallazgos.append(Hallazgo("version.skyrim", HALLAZGO_BLOQUEADO, "el Clone no está en disco"))

    # --- 4. MO2: gamePath real ---------------------------------------------
    ini_path = mo2_instance_dir / "modorganizer.ini"
    evidencia = leer_game_path_de_mo2(ini_path)
    if evidencia.state is not ObservationState.OBSERVED:
        hallazgos.append(Hallazgo("mo2.game_path", HALLAZGO_BLOQUEADO, f"{evidencia.state.value}: {evidencia.detail}"))
    else:
        coincide = (
            clone_root is not None
            and evidencia.path is not None
            and os.path.normcase(os.path.abspath(evidencia.path)) == os.path.normcase(os.path.abspath(clone_root))
        )
        hallazgos.append(
            Hallazgo(
                "mo2.game_path",
                HALLAZGO_OK if coincide else HALLAZGO_BLOQUEADO,
                f"gamePath={ruta(evidencia.path)} · ¿es el Clone? {'sí' if coincide else 'no'}",
            )
        )

    # --- 5. Config persistida de Sky-Claw ----------------------------------
    configurado = _leer_skyrim_path_persistido(config_path)
    if configurado is None:
        hallazgos.append(
            Hallazgo("skyclaw.config", HALLAZGO_BLOQUEADO, "no hay skyrim_path persistido en la config de Sky-Claw")
        )
    else:
        coincide = clone_root is not None and os.path.normcase(os.path.abspath(configurado)) == os.path.normcase(
            os.path.abspath(clone_root)
        )
        hallazgos.append(
            Hallazgo(
                "skyclaw.config",
                HALLAZGO_OK if coincide else HALLAZGO_BLOQUEADO,
                f"skyrim_path={ruta(configurado)} · ¿es el Clone? {'sí' if coincide else 'no'}",
            )
        )

    # --- 6. Journal y recuperación -----------------------------------------
    hallazgos.append(_estado_del_journal(storage_root))
    hallazgos.append(_estado_de_recuperacion(storage_root))

    # --- 7. Procesos --------------------------------------------------------
    hallazgos.append(_procesos_vivos())

    # --- 8. Preflight completo ---------------------------------------------
    evaluacion = evaluar_binder_preflight(
        storage_root=storage_root,
        mo2_instance_dir=mo2_instance_dir,
        clone_id=objetivo,
        managed_source_root=managed_source_root,
        config_skyrim_path=configurado,
        exigir_config_skyrim_path=True,
        probes=default_runtime_probes(),
    )
    hallazgos.extend(_hallazgos_del_preflight(evaluacion))

    # --- 9. Dependencias que siguen apuntando afuera -----------------------
    if clone_root is not None and clone_root.is_dir():
        # Si no se pasó ``--managed-source``, el preflight pudo DESCUBRIRLA y
        # devolverla. Usar sólo el argumento dejaba ``agujas`` vacío y el chequeo
        # de dependencias era vacuo por omisión (Codex sobre #851).
        origen = managed_source_root or evaluacion.managed_source_root
        agujas = tuple(a for a in (str(origen) if origen else None,) if a)
        sospechosas = _dependencias_en_arbol(clone_root, agujas)
        if sospechosas:
            hallazgos.append(Hallazgo("dependencias", HALLAZGO_BLOQUEADO, "; ".join(sospechosas)))
        else:
            hallazgos.append(
                Hallazgo("dependencias", HALLAZGO_OK, "ningún archivo de configuración nombra la Managed Source")
            )

    # --- Redacción de los detalles -----------------------------------------
    if not mostrar_rutas:
        conocidas = [storage_root, mo2_instance_dir, managed_source_root, clone_root]
        hallazgos = [dataclasses.replace(h, detalle=_redactar_texto(h.detalle, ruta, conocidas)) for h in hallazgos]

    # --- Veredicto consolidado ---------------------------------------------
    if any(h.veredicto == VEREDICTO_BLOQUEADO for h in hallazgos):
        veredicto = VEREDICTO_BLOQUEADO
    elif any(h.veredicto == VEREDICTO_INDETERMINADO for h in hallazgos):
        veredicto = VEREDICTO_INDETERMINADO
    else:
        veredicto = VEREDICTO_LISTO

    # La raíz EFECTIVA se devuelve aparte, sin redactar: ``main`` la necesita para
    # PROTEGER el destino del informe. Si sólo se usara ``args.managed_source``,
    # omitir el argumento dejaría la instalación descubierta escribible (Codex
    # sobre #851).
    managed_efectiva = managed_source_root or evaluacion.managed_source_root

    return {
        "veredicto": veredicto,
        "listo_para_p7": veredicto == VEREDICTO_LISTO,
        "rutas_completas": mostrar_rutas,
        "storage_root": ruta(storage_root),
        "mo2_instance": ruta(mo2_instance_dir),
        "managed_source": ruta(managed_source_root),
        "clone_objetivo": objetivo,
        "preflight_verdict": evaluacion.verdict.value,
        "preflight_ready": evaluacion.is_ready,
        "hallazgos": [h.como_dict() for h in hallazgos],
        "matriz_p7": _matriz_p7(),
    }, managed_efectiva


def _matriz_p7() -> list[dict[str, str]]:
    """Las pruebas REALES de P7, con lo que cada una tiene que demostrar.

    Se emite siempre, aunque el veredicto sea BLOCKED: es el contrato de la
    sesión siguiente, no un resultado de ésta. Ninguna se ejecuta acá.
    """
    return [
        {"id": "P7-A", "prueba": "abrir MO2 desde el Clone", "demuestra": "el Clone es un runtime operable por MO2"},
        {"id": "P7-B", "prueba": "ejecutar SKSE desde MO2", "demuestra": "la cadena MO2→SKSE arranca desde el Clone"},
        {
            "id": "P7-C",
            "prueba": "verificar el proceso realmente ejecutado y su ruta",
            "demuestra": "la ruta observada en la tabla de procesos es la del Clone, no la de Steam",
        },
        {"id": "P7-D", "prueba": "repetir arranques sin pasos manuales", "demuestra": "SFR-21"},
        {
            "id": "P7-E",
            "prueba": "una actualización de Steam no modifica el Clone",
            "demuestra": "independencia física real (SFR-18/20)",
        },
        {
            "id": "P7-F",
            "prueba": "recuperación y rollback reales",
            "demuestra": "el journal restaura el runtime operativo",
        },
        {
            "id": "P7-G",
            "prueba": "garantías de SFR-21 sin manipular el estado de actualización de Steam",
            "demuestra": "SFR-21 no depende de tocar el manifest",
        },
    ]


class SalidaNoPermitidaError(Exception):
    """El destino del informe no es un lugar donde este script pueda escribir."""


def _canonico(ruta: str | os.PathLike[str]) -> str:
    """Ruta absoluta, normalizada en caja y con los enlaces RESUELTOS.

    Resolver acá y no sólo en el destino es lo que cierra el bypass: si
    ``--storage-root`` llega por un junction, la raíz protegida queda con el
    alias mientras el destino se resuelve a la ruta física, y la comparación de
    prefijos no las ve como el mismo árbol (Codex sobre #851).
    """
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(ruta))))


def _es_igual_o_hijo(candidato: str, contenedor: str) -> bool:
    """¿``candidato`` está dentro de ``contenedor`` (o es él mismo)?"""
    if candidato == contenedor:
        return True
    return candidato.startswith(contenedor + os.sep)


def _arboles_protegidos(
    *,
    storage_root: pathlib.Path,
    mo2_instance_dir: pathlib.Path,
    clone_roots: list[pathlib.Path],
    managed_source_root: pathlib.Path | None,
) -> list[tuple[pathlib.Path, str]]:
    """Árboles sobre los que el informe NO puede escribirse, con su etiqueta."""
    arboles: list[tuple[pathlib.Path, str]] = [
        (mo2_instance_dir, "la instancia MO2 (contiene modorganizer.ini)"),
        (storage_root, "el Frozen Runtime storage (active.json, journal, recovery)"),
    ]
    for indice, raiz in enumerate(clone_roots):
        arboles.append((raiz, f"el Runtime Clone #{indice}"))
    if managed_source_root is not None:
        arboles.append((managed_source_root, "la Managed Source de Steam"))
    return arboles


def validar_destino_informe(
    destino: str | os.PathLike[str],
    *,
    arboles_protegidos: list[tuple[pathlib.Path, str]],
) -> pathlib.Path:
    """Valida el destino del informe y devuelve la ruta canónica donde escribir.

    El script es READ-ONLY sobre el runtime: su única escritura es el informe. Que
    esa escritura no pueda caer dentro de una superficie que el propio script
    audita es parte del contrato, no una cortesía. ``--json-salida
    <instancia>/modorganizer.ini`` destruiría exactamente lo que P7 viene a
    inspeccionar.

    Reglas, todas fail-closed:

    1. El archivo no puede existir. Un informe no sobrescribe nada, ni siquiera
       un informe anterior.
    2. El directorio padre tiene que existir y ser un directorio.
    3. Ni el padre ni ningún ancestro puede ser un reparse point. Un junction
       dentro de un directorio permitido que apunte al storage sortearía la
       comparación de prefijos si se comparara la ruta sin resolver.
    4. La ruta real no puede estar dentro de ningún árbol protegido, ni ser uno
       de sus archivos.

    Returns:
        La ruta real (con enlaces resueltos) donde hay que crear el archivo.

    Raises:
        SalidaNoPermitidaError: con el motivo exacto. Nunca devuelve una ruta
            "probablemente segura".
    """
    crudo = os.path.abspath(os.fspath(destino))

    # Primero la pertenencia a un árbol protegido, sobre la ruta DECLARADA. El
    # orden importa para el diagnóstico: apuntar `--json-salida` al
    # `modorganizer.ini` tiene que decir «eso es la instancia MO2», no «el
    # archivo ya existe» —que además sería cierto y escondería la razón real—.
    for arbol, etiqueta in arboles_protegidos:
        if _es_igual_o_hijo(_canonico(crudo), _canonico(arbol)):
            raise SalidaNoPermitidaError(
                f"{crudo}: el informe caería dentro de {etiqueta}. Elegí una ruta fuera de los árboles "
                "que el inspector audita"
            )

    if os.path.lexists(crudo):
        raise SalidaNoPermitidaError(
            f"{crudo}: el archivo ya existe. El informe no sobrescribe nada; elegí una ruta nueva"
        )

    padre = os.path.dirname(crudo)
    if not os.path.isdir(padre):
        raise SalidaNoPermitidaError(f"{padre}: el directorio del informe no existe")

    # Un enlace en cualquier punto del padre cambia el destino real. Se compara
    # la ruta resuelta contra la declarada: si difieren, hay un reparse point en
    # el camino y no se puede afirmar dónde va a caer el archivo.
    padre_real = os.path.realpath(padre)
    if _canonico(padre) != _canonico(padre_real):
        raise SalidaNoPermitidaError(
            f"{padre}: el directorio del informe pasa por un enlace simbólico o junction "
            f"(se resuelve a {padre_real}). No se escribe a través de un reparse point"
        )

    real = os.path.join(padre_real, os.path.basename(crudo))
    for arbol, etiqueta in arboles_protegidos:
        if _es_igual_o_hijo(_canonico(real), _canonico(arbol)):
            raise SalidaNoPermitidaError(
                f"{real}: el informe caería dentro de {etiqueta}. Elegí una ruta fuera de los árboles "
                "que el inspector audita"
            )
    return pathlib.Path(real)


def escribir_informe_exclusivo(destino: pathlib.Path, texto: str) -> None:
    """Crea el informe con ``O_EXCL``: no puede pisar un archivo existente.

    ``Path.write_text`` trunca lo que encuentre. La comprobación de existencia de
    :func:`validar_destino_informe` y esta escritura están separadas por una
    ventana; ``O_CREAT | O_EXCL`` la cierra en el único lugar donde importa.
    """
    banderas = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        descriptor = os.open(destino, banderas, 0o600)
    except FileExistsError as exc:
        raise SalidaNoPermitidaError(
            f"{destino}: el archivo apareció entre la validación y la escritura; no se sobrescribe"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as manejador:
            manejador.write(texto)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(destino)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspección READ-ONLY de preparación para P7 (Frozen Runtime).")
    parser.add_argument("--storage-root", required=True, type=pathlib.Path)
    parser.add_argument("--mo2-instance", required=True, type=pathlib.Path)
    parser.add_argument("--config", type=pathlib.Path, default=None)
    parser.add_argument("--managed-source", type=pathlib.Path, default=None)
    parser.add_argument("--clone-id", default=None)
    parser.add_argument("--mostrar-rutas", action="store_true", help="incluye rutas completas en el informe")
    parser.add_argument("--json-salida", type=pathlib.Path, default=None, help="escribe el informe JSON en esta ruta")
    args = parser.parse_args(argv)

    informe, managed_efectiva = construir_informe(
        storage_root=args.storage_root,
        mo2_instance_dir=args.mo2_instance,
        config_path=args.config,
        managed_source_root=args.managed_source,
        clone_id=args.clone_id,
        mostrar_rutas=args.mostrar_rutas,
    )
    # A stdout se emite ASCII puro: la consola de Windows puede estar en cp1252 y
    # el informe contiene «→» y acentos. `print` con un carácter fuera de la
    # página de códigos lanza UnicodeEncodeError y el informe NUNCA se escribe.
    # Lo detectó CI en windows-latest; localmente la consola era UTF-8.
    print(json.dumps(informe, indent=2, ensure_ascii=True))
    # Al archivo va UTF-8 legible: ahí la página de códigos no aplica.
    texto = json.dumps(informe, indent=2, ensure_ascii=False)

    if args.json_salida is not None:
        # La validación corre DESPUÉS de leer: si el destino es inválido, el
        # informe ya se emitió por stdout y el fallo es explícito, no silencioso.
        clone_roots = _clones_del_informe(args.storage_root, informe)
        try:
            destino = validar_destino_informe(
                args.json_salida,
                arboles_protegidos=_arboles_protegidos(
                    storage_root=args.storage_root,
                    mo2_instance_dir=args.mo2_instance,
                    clone_roots=clone_roots,
                    # La DESCUBIERTA, no la del argumento: omitir
                    # ``--managed-source`` no puede dejar la instalación de Steam
                    # escribible por el propio inspector.
                    managed_source_root=managed_efectiva,
                ),
            )
            escribir_informe_exclusivo(destino, texto + "\n")
        except SalidaNoPermitidaError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    return 0 if informe["listo_para_p7"] else 1


def _clones_del_informe(storage_root: pathlib.Path, informe: dict[str, Any]) -> list[pathlib.Path]:
    """Raíces de Clone que el informe conoce, para protegerlas.

    Se toman del almacenamiento durable y NO del texto del informe: el informe
    redacta las rutas para stdout, y una ruta redactada no sirve para decidir si
    el destino cae dentro. La protección tiene que usar la ruta real.

    Se juntan las dos fuentes porque cualquiera de las dos puede existir sola: el
    registro del Clone puede faltar mientras ``active.json`` todavía declara un
    ``clone_root``. Proteger sólo la que está de moda dejaría la otra escribible.
    """
    raices: list[pathlib.Path] = []
    registro = _raiz_de_clone(storage_root, informe.get("clone_objetivo"))
    if registro is not None:
        raices.append(registro)
    try:
        deseado = desired_runtime_from_storage(storage_root)
    except Exception:  # noqa: BLE001 - el informe no debe caerse por un storage raro
        deseado = None
    if deseado is not None and deseado.clone_root:
        raiz_deseada = pathlib.Path(deseado.clone_root)
        if all(_canonico(raiz_deseada) != _canonico(r) for r in raices):
            raices.append(raiz_deseada)
    return raices


def _raiz_de_clone(storage_root: pathlib.Path, clone_id: str | None) -> pathlib.Path | None:
    if not clone_id:
        return None
    try:
        registro = cargar_runtime_clone_record(storage_root, clone_id)
    except Exception:  # noqa: BLE001 - el informe no debe caerse por un registro raro
        return None
    return pathlib.Path(registro.root_path) if registro is not None else None


if __name__ == "__main__":
    raise SystemExit(main())
