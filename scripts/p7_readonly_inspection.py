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
        [--clone-id <id>] [--mostrar-rutas] > informe.json

Sale con 0 si el veredicto es ``READY_FOR_P7`` y con 1 en cualquier otro caso.
Un ``INDETERMINATE`` NUNCA cuenta como listo.

La persistencia del informe es del OPERADOR (redirección de stdout). El script
no tiene ninguna opción de escritura: un proceso que no puede escribir no puede
destruir la superficie que audita. La versión anterior ofrecía
``--json-salida``; se retiró porque entre la validación del destino y el
``os.open`` el directorio padre podía sustituirse por una junction (TOCTOU
demostrado con test RED). ``O_EXCL`` protege la identidad del ARCHIVO, no la
del DIRECTORIO.
"""

from __future__ import annotations

import argparse
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


def _variantes_de_ruta_para_busqueda(ruta: pathlib.Path) -> tuple[str, ...]:
    """Genera las representaciones textuales más probables de ``ruta``.

    En Windows ``str(Path)`` usa barras invertidas, pero los archivos de
    configuración pueden usar barras normales o duplicar las invertidas. Sin
    normalizar, una dependencia externa escrita con ``/`` pasa desapercibida
    (Codex tercera ronda sobre #851).
    """
    raw = str(ruta)
    posix = ruta.as_posix()
    escapada = raw.replace("\\", "\\\\")
    variantes = {raw, posix}
    if escapada != raw:
        variantes.add(escapada)
    # También la forma con barras invertidas escapadas en estilo TOML/JSON
    doble_backslash = posix.replace("/", "\\\\")
    if doble_backslash not in variantes:
        variantes.add(doble_backslash)
    return tuple(v for v in variantes if v)


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
    # Si no se pasó ``--managed-source``, el preflight pudo DESCUBRIRLA y
    # devolverla. Usar sólo el argumento dejaba ``agujas`` vacío y el chequeo de
    # dependencias era vacuo por omisión (Codex sobre #851).
    #
    # ``origen`` y ``agujas`` se calculan SIEMPRE, no dentro del ``if``: los
    # detalles que produce este bloque nombran la ruta, y la redacción de más
    # abajo tiene que conocerla. Dejarla fuera de ``conocidas`` hacía que la
    # protección dependiera sólo del patrón de ruta absoluta —que hoy la cubre,
    # pero es una sola regex entre el árbol privado y el informe— (CodeRabbit
    # sobre #851).
    origen = managed_source_root or evaluacion.managed_source_root
    agujas = _variantes_de_ruta_para_busqueda(origen) if origen else ()
    if clone_root is not None and clone_root.is_dir():
        sospechosas = _dependencias_en_arbol(clone_root, agujas)
        if sospechosas:
            hallazgos.append(Hallazgo("dependencias", HALLAZGO_BLOQUEADO, "; ".join(sospechosas)))
        else:
            hallazgos.append(
                Hallazgo("dependencias", HALLAZGO_OK, "ningún archivo de configuración nombra la Managed Source")
            )

    # --- Redacción de los detalles -----------------------------------------
    if not mostrar_rutas:
        conocidas = [storage_root, mo2_instance_dir, managed_source_root, clone_root, origen, *agujas]
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


def main(argv: list[str] | None = None) -> int:
    """Emite el informe JSON por stdout. NO escribe archivos.

    El inspector es READ-ONLY y eso incluye su propia salida: no hay
    ``--json-salida``. La versión anterior escribía el informe y validaba el
    destino por ruta; entre la validación y el ``os.open`` el directorio padre
    podía sustituirse por una junction y el archivo caía dentro del árbol que el
    inspector venía a auditar (TOCTOU demostrado con test RED). ``O_EXCL``
    protege la identidad del ARCHIVO, no la del DIRECTORIO.

    La persistencia es del operador: ``... > informe.json`` o el mecanismo que
    él elija. Un proceso que no puede escribir no puede destruir lo que audita.
    """
    parser = argparse.ArgumentParser(
        description="Inspección READ-ONLY de preparación para P7 (Frozen Runtime). Emite JSON por stdout."
    )
    parser.add_argument("--storage-root", required=True, type=pathlib.Path)
    parser.add_argument("--mo2-instance", required=True, type=pathlib.Path)
    parser.add_argument("--config", type=pathlib.Path, default=None)
    parser.add_argument("--managed-source", type=pathlib.Path, default=None)
    parser.add_argument("--clone-id", default=None)
    parser.add_argument("--mostrar-rutas", action="store_true", help="incluye rutas completas en el informe")
    args = parser.parse_args(argv)

    informe, _ = construir_informe(
        storage_root=args.storage_root,
        mo2_instance_dir=args.mo2_instance,
        config_path=args.config,
        managed_source_root=args.managed_source,
        clone_id=args.clone_id,
        mostrar_rutas=args.mostrar_rutas,
    )
    # A stdout se emite ASCII puro: la consola de Windows puede estar en cp1252 y
    # el informe contiene «→» y acentos. `print` con un carácter fuera de la
    # página de códigos lanza UnicodeEncodeError y el informe NUNCA se emite.
    # Lo detectó CI en windows-latest; localmente la consola era UTF-8.
    print(json.dumps(informe, indent=2, ensure_ascii=True))
    return 0 if informe["listo_para_p7"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
