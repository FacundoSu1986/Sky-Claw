"""Sky-Claw CLI entry point.

Usage::

    python -m sky_claw --mode cli          # interactive REPL
    python -m sky_claw --mode telegram     # Telegram webhook server
    python -m sky_claw --mode oneshot "install Requiem"
    python -m sky_claw --mode gui         # local desktop UI (NiceGUI)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import pathlib
import re
import signal
import sys
from typing import TYPE_CHECKING, NoReturn

# Apply Python 3.14+ polyfills before importing sky_claw submodules that could
# transitively pull NiceGUI / vbuild. Must run before the package imports below.
from sky_claw.compat import setup_python_compat

setup_python_compat()

if TYPE_CHECKING:
    from collections.abc import Callable

#: Código con el que termina el proceso por una excepción no manejada: el mismo que da Python sin congelar.
EXIT_EXCEPCION_NO_MANEJADA = 1

#: Modos con un operador mirando la pantalla: ahí el diálogo de PyInstaller con el traceback ES la señal del crash.
_MODOS_INTERACTIVOS = frozenset({"gui"})


def _volcar_traceback_a_stderr() -> None:
    """Deja en stderr el traceback de la excepción en curso, como Python al morir por ella, si hay adónde escribir."""
    flujo = sys.stderr
    if flujo is None:  # console=False sin handles heredados
        return
    with contextlib.suppress(Exception):
        import traceback  # noqa: PLC0415 - solo al fallar: no suma imports antes de verificar el Job (H-02)

        traceback.print_exc(file=flujo)
        flujo.flush()


def _despachar_sin_dialogo(punto_de_entrada: Callable[[], int | None]) -> NoReturn:
    """Corre un punto de entrada del ejecutable y sale con su código; nunca deja escapar una excepción.

    El .exe es ``console=False``: una excepción que escapa del script hace que el bootloader de PyInstaller abra un
    diálogo MODAL («Unhandled exception in script») y el proceso espere a que alguien lo cierre. Sin nadie mirando
    (MO2 lanzando el worker dentro del Job, el gate UIA, un arnés) queda colgado para siempre: R0 ronda 6 lo midió en
    ``vfs-health``. ``SystemExit`` no abre el diálogo, así que toda excepción sale como ``SystemExit(1)``, con el
    traceback en stderr. Un ``SystemExit`` propio del punto de entrada (argparse, por ejemplo) pasa tal cual.
    """
    try:
        codigo = punto_de_entrada()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 - frontera del proceso: lo que escape acá lo atrapa el diálogo modal
        _volcar_traceback_a_stderr()
        raise SystemExit(EXIT_EXCEPCION_NO_MANEJADA) from None
    raise SystemExit(codigo)


_FLAG_EXPORTAR_LICENCIAS = "--export-licenses"
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_WIN_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_DOCUMENTOS_LEGALES_OBLIGATORIOS = frozenset(
    {
        "THIRD_PARTY_NOTICES.md",
        "sky_claw/app/gui/assets/fonts/OFL.txt",
    }
)
_PREFIJO_TEXTOS_LEGALES = "licenses/third_party/texts/"
_NOMBRES_RESERVADOS_WINDOWS = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


def _escribir_flujo_seguro(flujo: object, mensaje: str) -> None:
    if flujo is None:
        return
    escribir = getattr(flujo, "write", None)
    if callable(escribir):
        with contextlib.suppress(Exception):
            escribir(mensaje + "\n")
            vaciar = getattr(flujo, "flush", None)
            if callable(vaciar):
                vaciar()


def _es_reparse_o_enlace(ruta: pathlib.Path) -> bool:
    if ruta.is_symlink():
        return True
    try:
        st = os.lstat(ruta)
    except OSError:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & _WIN_FILE_ATTRIBUTE_REPARSE_POINT)


def _contiene_reparse_en_cadena(ruta: pathlib.Path) -> bool:
    actual = ruta
    while True:
        if _es_reparse_o_enlace(actual):
            return True
        padre = actual.parent
        if padre == actual:
            return False
        actual = padre


def _validar_ruta_relativa_manifiesto(rel_str: str) -> pathlib.PurePosixPath | None:
    """Valida que una ruta del manifiesto sea estrictamente relativa, segura y dentro del perímetro legal."""
    if not rel_str or "\x00" in rel_str or "\\" in rel_str or ":" in rel_str:
        return None
    if rel_str.startswith(("/", "~")):
        return None
    partes = rel_str.split("/")
    if any(p in ("", ".", "..") for p in partes):
        return None
    for parte in partes:
        if parte.endswith((" ", ".")):
            return None
        base_mayus = parte.split(".", 1)[0].upper()
        if base_mayus in _NOMBRES_RESERVADOS_WINDOWS:
            return None
    # Limitar estrictamente a rutas legales autorizadas
    if rel_str in _DOCUMENTOS_LEGALES_OBLIGATORIOS:
        return pathlib.PurePosixPath(*partes)
    if rel_str.startswith(_PREFIJO_TEXTOS_LEGALES) and len(partes) == 4:
        return pathlib.PurePosixPath(*partes)
    return None


def _rollback_creados(archivos_creados: list[pathlib.Path], dirs_creados: list[pathlib.Path]) -> None:
    """Elimina únicamente archivos y directorios cuya creación por esta ejecución está acreditada."""
    for archivo in reversed(archivos_creados):
        with contextlib.suppress(OSError):
            archivo.unlink()
    for directorio in reversed(dirs_creados):
        with contextlib.suppress(OSError):
            directorio.rmdir()


def _crear_dir_registrado(ruta: pathlib.Path, dirs_creados: list[pathlib.Path]) -> None:
    """Crea directorios faltantes uno a uno registrando solo los efectivamente creados por esta ejecución."""
    pendientes: list[pathlib.Path] = []
    actual = ruta
    while not actual.exists():
        pendientes.append(actual)
        padre = actual.parent
        if padre == actual:
            break
        actual = padre
    for d in reversed(pendientes):
        os.mkdir(d)
        dirs_creados.append(d)


def _escribir_exclusivo(ruta: pathlib.Path, contenido: bytes, archivos_creados: list[pathlib.Path]) -> None:
    """Escribe un archivo usando creación exclusiva (O_CREAT|O_EXCL) para impedir sobrescrituras."""
    banderas = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(os.fspath(ruta), banderas, 0o644)
    try:
        archivos_creados.append(ruta)
        with os.fdopen(fd, "wb") as fh:
            fh.write(contenido)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise


def _resolver_origen_licencias(origen_base: pathlib.Path | None) -> pathlib.Path:
    if origen_base is not None:
        return origen_base.resolve()
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return pathlib.Path(str(meipass)).resolve()
    return pathlib.Path(__file__).resolve().parent.parent


def _resolver_archivo_origen_licencia(raiz_origen: pathlib.Path, rel_str: str) -> pathlib.Path:
    """Resuelve un documento legal en árbol fuente o bajo ``sky_claw/legal`` en el bundle congelado."""
    directo = raiz_origen / pathlib.Path(rel_str)
    if directo.is_file():
        return directo
    en_bundle = raiz_origen / "sky_claw" / "legal" / pathlib.Path(rel_str)
    if en_bundle.is_file():
        return en_bundle
    return directo


def exportar_licencias_terceros(
    destino: pathlib.Path | str,
    *,
    origen_base: pathlib.Path | None = None,
) -> int:
    """Exporta avisos y textos íntegros de licencias de terceros a un directorio nuevo y persistente.

    Valida el manifiesto, rutas seguras, documentos obligatorios y SHA-256 antes de cualquier escritura;
    usa creación exclusiva y rollback acotado solo a los archivos/directorios creados por esta ejecución.
    """
    raiz_origen = _resolver_origen_licencias(origen_base)
    if _contiene_reparse_en_cadena(raiz_origen):
        _escribir_flujo_seguro(sys.stderr, "Error: el directorio origen contiene enlaces simbólicos/reparse.")
        return 1
    ruta_manifiesto = _resolver_archivo_origen_licencia(raiz_origen, "licenses/third_party/manifest.json")
    if not ruta_manifiesto.is_file() or _contiene_reparse_en_cadena(ruta_manifiesto):
        _escribir_flujo_seguro(sys.stderr, f"Error: manifiesto de licencias ausente o inválido: {ruta_manifiesto}")
        return 1

    try:
        datos_manifiesto = ruta_manifiesto.read_bytes()
        manifiesto = json.loads(datos_manifiesto.decode("utf-8"))
        documentos = manifiesto["documents"]
        componentes = manifiesto["components"]
        if (
            manifiesto.get("schema_version") != 1
            or not isinstance(documentos, list)
            or not documentos
            or not isinstance(componentes, list)
            or not componentes
        ):
            raise ValueError("Esquema de manifest.json inválido")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _escribir_flujo_seguro(sys.stderr, f"Error: manifest.json corrupto o ilegible: {exc}")
        return 1

    # Validar destino exclusivo (rechazar traversal, UNC/device, symlinks/reparse, colisión y destino en origen)
    destino_str = str(destino)
    if not destino_str.strip() or "\x00" in destino_str or destino_str.startswith(("\\\\?\\", "\\\\.\\")):
        _escribir_flujo_seguro(sys.stderr, "Error: ruta de destino inválida.")
        return 1
    ruta_cruda = pathlib.Path(destino_str)
    if ".." in ruta_cruda.parts:
        _escribir_flujo_seguro(sys.stderr, "Error: ruta de destino con traversal ('..').")
        return 1
    ruta_abs = ruta_cruda if ruta_cruda.is_absolute() else (pathlib.Path.cwd() / ruta_cruda)
    if _contiene_reparse_en_cadena(ruta_abs) or _contiene_reparse_en_cadena(ruta_abs.parent):
        _escribir_flujo_seguro(sys.stderr, "Error: el destino o su cadena padre contiene enlaces simbólicos/reparse.")
        return 1
    if ruta_abs.exists():
        _escribir_flujo_seguro(sys.stderr, f"Error: el directorio de destino ya existe: {ruta_abs}")
        return 1
    if not ruta_abs.parent.is_dir():
        _escribir_flujo_seguro(sys.stderr, f"Error: el directorio padre no existe: {ruta_abs.parent}")
        return 1

    destino_resuelto = ruta_abs.parent.resolve() / ruta_abs.name
    try:
        destino_resuelto.relative_to(raiz_origen)
        _escribir_flujo_seguro(sys.stderr, "Error: no se permite exportar dentro del directorio origen/bundle.")
        return 1
    except ValueError:
        pass
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        try:
            destino_resuelto.relative_to(pathlib.Path(str(meipass)).resolve())
            _escribir_flujo_seguro(sys.stderr, "Error: no se permite exportar dentro de sys._MEIPASS.")
            return 1
        except ValueError:
            pass

    # Pre-validar todas las rutas, colisiones Windows case-insensitive, conflictos archivo/dir y SHA-256
    entradas_verificadas: list[tuple[pathlib.PurePosixPath, bytes]] = []
    vistas_norm: set[str] = set()
    dirs_norm: set[str] = {"licenses", "licenses/third_party"}
    vistas_exactas: set[str] = set()
    tiene_texto_legal = False

    for doc in documentos:
        if not isinstance(doc, dict):
            _escribir_flujo_seguro(sys.stderr, "Error: entrada de documento inválida en manifest.json.")
            return 1
        rel_str = str(doc.get("path", ""))
        rel_posix = _validar_ruta_relativa_manifiesto(rel_str)
        sha_esperado = str(doc.get("sha256", ""))
        tam_esperado = doc.get("bytes")
        if (
            rel_posix is None
            or not _SHA256_HEX_RE.fullmatch(sha_esperado)
            or not isinstance(tam_esperado, int)
            or tam_esperado <= 0
        ):
            _escribir_flujo_seguro(sys.stderr, f"Error: ruta o metadatos inválidos en manifest.json para {rel_str!r}.")
            return 1

        clave_norm = rel_posix.as_posix().casefold()
        if clave_norm in vistas_norm or clave_norm in dirs_norm:
            _escribir_flujo_seguro(sys.stderr, f"Error: colisión de ruta o conflicto archivo/directorio: {rel_str!r}.")
            return 1
        # Registrar prefijos de directorio y detectar si alguno colisiona con un archivo ya visto
        for idx in range(1, len(rel_posix.parts)):
            pref_norm = "/".join(rel_posix.parts[:idx]).casefold()
            if pref_norm in vistas_norm:
                _escribir_flujo_seguro(sys.stderr, f"Error: conflicto archivo/directorio en {rel_str!r}.")
                return 1
            dirs_norm.add(pref_norm)
        vistas_norm.add(clave_norm)
        vistas_exactas.add(rel_str)
        if rel_str.startswith(_PREFIJO_TEXTOS_LEGALES):
            tiene_texto_legal = True

        archivo_src = _resolver_archivo_origen_licencia(raiz_origen, rel_str)
        if not archivo_src.is_file() or _contiene_reparse_en_cadena(archivo_src):
            _escribir_flujo_seguro(sys.stderr, f"Error: documento faltante o reparse en origen: {rel_str}")
            return 1
        try:
            contenido = archivo_src.read_bytes().replace(b"\r\n", b"\n")
        except OSError as exc:
            _escribir_flujo_seguro(sys.stderr, f"Error leyendo {rel_str}: {exc}")
            return 1
        if len(contenido) != tam_esperado or hashlib.sha256(contenido).hexdigest() != sha_esperado:
            _escribir_flujo_seguro(sys.stderr, f"Error: hash/tamaño inconsistente para {rel_str}")
            return 1
        entradas_verificadas.append((rel_posix, contenido))

    # Verificar presencia de documentos obligatorios
    if not _DOCUMENTOS_LEGALES_OBLIGATORIOS.issubset(vistas_exactas) or not tiene_texto_legal:
        _escribir_flujo_seguro(sys.stderr, "Error: el manifiesto omite documentos legales obligatorios.")
        return 1

    # L01-12: validar integridad referencial completa de components -> documents antes de cualquier escritura
    ids_componentes_vistos: set[str] = set()
    for comp in componentes:
        if not isinstance(comp, dict):
            _escribir_flujo_seguro(sys.stderr, "Error: entrada de componente inválida en manifest.json.")
            return 1
        cid = str(comp.get("component_id", "")).strip()
        if not cid or cid in ids_componentes_vistos:
            _escribir_flujo_seguro(sys.stderr, f"Error: component_id ausente o duplicado en manifest.json: {cid!r}")
            return 1
        ids_componentes_vistos.add(cid)

        cobertura = str(comp.get("document_coverage", "FULL_TEXT_VERIFIED"))
        if cobertura not in ("FULL_TEXT_VERIFIED", "PENDING_DOCUMENTARY_EVIDENCE"):
            _escribir_flujo_seguro(sys.stderr, f"Error: document_coverage inválido para {cid!r}: {cobertura!r}")
            return 1
        refs_docs = comp.get("license_documents")
        if not isinstance(refs_docs, list):
            _escribir_flujo_seguro(sys.stderr, f"Error: license_documents inválido para {cid!r}.")
            return 1
        if cobertura == "FULL_TEXT_VERIFIED" and not refs_docs:
            _escribir_flujo_seguro(sys.stderr, f"Error: componente {cid!r} sin documentos legales referenciados.")
            return 1
        for ref in refs_docs:
            if not isinstance(ref, str) or ref not in vistas_exactas:
                _escribir_flujo_seguro(
                    sys.stderr,
                    f"Error: componente {cid!r} referencia documento ausente en manifest.json: {ref!r}",
                )
                return 1

    # Creación exclusiva y rollback acotado estrictamente a lo creado por esta ejecución
    dirs_creados: list[pathlib.Path] = []
    archivos_creados: list[pathlib.Path] = []
    try:
        os.mkdir(destino_resuelto)
        dirs_creados.append(destino_resuelto)
        dest_manifest = destino_resuelto / "licenses" / "third_party" / "manifest.json"
        _crear_dir_registrado(dest_manifest.parent, dirs_creados)
        _escribir_exclusivo(dest_manifest, datos_manifiesto, archivos_creados)
        for rel_posix, contenido in entradas_verificadas:
            dest_file = destino_resuelto / pathlib.Path(*rel_posix.parts)
            _crear_dir_registrado(dest_file.parent, dirs_creados)
            _escribir_exclusivo(dest_file, contenido, archivos_creados)
    except OSError as exc:
        _rollback_creados(archivos_creados, dirs_creados)
        _escribir_flujo_seguro(sys.stderr, f"Error escribiendo licencias exportadas: {exc}")
        return 1

    _escribir_flujo_seguro(
        sys.stdout,
        f"Licencias de terceros exportadas ({len(entradas_verificadas)} documentos verificados) en: {destino_resuelto}",
    )
    return 0


def _despachar_export_licencias(argv_restante: list[str]) -> int:
    if len(argv_restante) != 1:
        _escribir_flujo_seguro(sys.stderr, f"Uso: SkyClawApp {_FLAG_EXPORTAR_LICENCIAS} <DIRECTORIO_NUEVO>")
        return 2
    return exportar_licencias_terceros(argv_restante[0])


# #856: el worker VFS y su nieto de sonda se despachan ANTES de importar
# ``AppContext``/``Config``/``keyring``. En Windows, importar ``keyring``
# dispara ``platform.win32_ver()`` → ``cmd.exe /c ver`` en tiempo de import,
# lo cual crearía subprocesos hijos antes del fence de supervisión del Job Object.
if len(sys.argv) >= 2 and sys.argv[1] in ("--vfs-worker", "--vfs-probe-child"):
    # Auditoría independiente H-02: ``arrancar_worker`` PRUEBA el Job antes de importar ``vfs_worker`` (cuyo árbol
    # de imports llega a ``platform`` → ``cmd.exe``). Este despacho y el de ``main()`` pasan por la misma función.
    from sky_claw.local.mo2.vfs_arranque import arrancar_worker  # noqa: PLC0415

    _despachar_sin_dialogo(lambda: arrancar_worker(sys.argv[1], sys.argv[2:]))

# T5-v2.1: modo worker del ejecutable congelado. El gate UIA productivo corre en
# un proceso descartable (una llamada COM que nunca retorna no se puede matar
# desde un hilo), y en modo congelado el helper ES este ejecutable re-invocado
# con un flag. El despacho va ACÁ, antes de importar la app: el worker no debe
# arrastrar NiceGUI/Web ni abrir la GUI. La constante vive en el ejecutor y se
# importa cita por cita: mover el flag sin este despacho rompería el helper en
# silencio, así que un test lo ancla.
from sky_claw.local.tools.dyndolod_uia_ejecutor import FLAG_HELPER_UIA  # noqa: E402

if len(sys.argv) >= 3 and sys.argv[1] == FLAG_HELPER_UIA:
    from sky_claw.local.tools.dyndolod_uia_helper import main as _main_helper_uia  # noqa: PLC0415

    _despachar_sin_dialogo(lambda: _main_helper_uia(sys.argv[2:]))

# #661 P3: el driver MUTANTE de la etapa 9 desatendida corre en SU propio worker
# descartable, con flag, módulo y puerta distintos del gate read-only. Mismo
# motivo para despachar acá, antes de importar la app.
from sky_claw.local.tools.dyndolod_uia_driver_ejecutor import FLAG_HELPER_DRIVER_UIA  # noqa: E402

if len(sys.argv) >= 3 and sys.argv[1] == FLAG_HELPER_DRIVER_UIA:
    from sky_claw.local.tools.dyndolod_uia_driver_helper import main as _main_helper_driver  # noqa: PLC0415

    _despachar_sin_dialogo(lambda: _main_helper_driver(sys.argv[2:]))

from sky_claw.app.comms.telegram_polling import normalizar_operador_telegram_estricto  # noqa: E402
from sky_claw.app.modes.cli_mode import _run_cli, _run_oneshot  # noqa: E402
from sky_claw.app.modes.security_mode import _run_security  # noqa: E402
from sky_claw.app.modes.telegram_mode import _run_telegram  # noqa: E402
from sky_claw.app_context import AppContext  # noqa: E402
from sky_claw.config import Config, SystemPaths  # noqa: E402
from sky_claw.local.tools.bodyslide_operador import ACCIONES_DEL_OPERADOR  # noqa: E402
from sky_claw.logging_config import (  # noqa: E402
    install_loop_exception_handler,
    install_missing_std_stream_adapters,
    setup_logging,
    shutdown_logging,
)

if TYPE_CHECKING:
    # Solo para la anotación: importarlo acá cargaría el broker VFS antes del despacho de los workers.
    from sky_claw.local.mo2.vfs_quarantine import EstadoDeCuarentena
    from sky_claw.local.tools.bodyslide_operador import InformeDelOperador

logger = logging.getLogger("sky_claw")


def _modo_por_defecto() -> str:
    """El ``--mode`` sin argumentos: la GUI en el .exe congelado (doble clic), la CLI en desarrollo."""
    return "gui" if getattr(sys, "frozen", False) else "cli"


def _modo_declarado(argv: list[str]) -> str | None:
    """El ``--mode`` que pide el argv, leído SIN el parser completo y sin poder fallar.

    ``main()`` necesita saber si hay un operador mirando aunque falle antes de tener ``args``: en ``setup_logging``,
    en ``_ensure_std_streams`` o en el propio ``_parse_args``, cuyo ``_build_parser`` arma un ``Config`` (PR-Agent en
    #984). Un argv que no declara el modo, o que está roto, devuelve ``None`` y decide el default.
    """
    try:
        lector = argparse.ArgumentParser(add_help=False, exit_on_error=False)
        lector.add_argument("--mode")
        conocidos, _resto = lector.parse_known_args(argv)
    except (Exception, SystemExit):  # noqa: BLE001 - argv roto: no declara nada, decide el default
        return None
    modo = conocidos.mode
    return modo if isinstance(modo, str) else None


def _es_interactivo(modo: str | None) -> bool:
    """¿Hay un operador mirando? Sin modo declarado, decide el default (el doble clic del .exe abre la GUI)."""
    return (modo if modo is not None else _modo_por_defecto()) in _MODOS_INTERACTIVOS


def _build_parser() -> argparse.ArgumentParser:
    config = Config()
    _chat_id_str = config.telegram_chat_id or ""
    _default_chat_id = normalizar_operador_telegram_estricto(_chat_id_str)

    # #665: el default de --mode se decide UNA vez y el help lo deriva con
    # ``%(default)s``. Antes el help fijaba "default: cli" y un
    # ``set_defaults(mode="gui")`` posterior lo desmentía en el .exe congelado.
    modo_por_defecto = _modo_por_defecto()

    parser = argparse.ArgumentParser(
        prog="sky_claw",
        description="Sky-Claw — Autonomous Skyrim mod management agent",
    )
    parser.add_argument(
        "--mode",
        choices=[
            "cli",
            "telegram",
            "oneshot",
            "gui",
            "security",
            "install-vfs-bridge",
            "vfs-health",
            "vfs-quarantine",
            "bodyslide-recovery",
        ],
        default=modo_por_defecto,
        help="Operation mode (default: %(default)s)",
    )
    parser.add_argument(
        "--provider",
        choices=["anthropic", "deepseek", "openai", "ollama"],
        default=config.llm_provider or "deepseek",
        help="LLM provider (default: %(default)s)",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default=None,
        help="Command to execute in oneshot mode, or the action for security mode (e.g. 'scan <path>')",
    )
    parser.add_argument(
        "--mo2-root",
        type=pathlib.Path,
        default=pathlib.Path(config.mo2_root or str(SystemPaths.get_base_drive() / "MO2Portable")),
        help="Path to the MO2 portable instance",
    )
    parser.add_argument(
        "--profile",
        default="",
        help="MO2 profile for this Sky-Claw session (if omitted: MO2_PROFILE or Default; restart to change)",
    )
    parser.add_argument(
        "--skyrim-path",
        type=pathlib.Path,
        default=pathlib.Path(config.skyrim_path) if config.skyrim_path else None,
        help="Path to the Skyrim installation (required by vfs-health)",
    )
    parser.add_argument(
        "--vfs-profile",
        default="Default",
        help="MO2 profile used by the vfs-health probe (default: %(default)s)",
    )
    parser.add_argument(
        "--vfs-timeout",
        type=float,
        # Condición 4 del HOLD (2026-11-22): el job cubre lanzar por MO2 + arranque del onefile + probe; en el rig hubo
        # arranques de 32-44 s (HDD y SSD por igual). 120 s los cubre con más de 2x de margen.
        default=120.0,
        help="Timeout in seconds for the vfs-health worker (default: %(default)s)",
    )
    parser.add_argument(
        "--vfs-quarantine-action",
        choices=["status", "release"],
        default="status",
        help="vfs-quarantine: report the quarantine state or release it; release needs --vfs-evidence (default: %(default)s)",
    )
    parser.add_argument(
        "--vfs-evidence",
        default=None,
        help="vfs-quarantine release: what you verified (e.g. no job process is left alive); it is logged as CRITICAL",
    )
    parser.add_argument(
        "--bodyslide-recovery-action",
        choices=list(ACCIONES_DEL_OPERADOR),
        default="status",
        help=(
            "bodyslide-recovery: show the BodySlide journals and quarantine, release the quarantine, or confirm "
            "that a launched run is over; the last two need --bodyslide-evidence (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--bodyslide-evidence",
        default=None,
        help="bodyslide-recovery: what you verified (e.g. no BodySlide process is left alive); logged as CRITICAL",
    )
    parser.add_argument(
        "--bodyslide-exe",
        type=pathlib.Path,
        default=pathlib.Path(config.bodyslide_exe) if config.bodyslide_exe else None,
        help="bodyslide-recovery: BodySlide executable of the installation to recover; when omitted, bodyslide_exe from the config",
    )
    parser.add_argument(
        "--db-path",
        type=pathlib.Path,
        default=pathlib.Path("mod_registry.db"),
        help="Path to the mod registry database",
    )
    parser.add_argument(
        "--loot-exe",
        type=pathlib.Path,
        default=pathlib.Path(config.loot_exe or "loot.exe"),
        help="Path to the LOOT CLI executable",
    )
    parser.add_argument(
        "--webhook-host",
        default="127.0.0.1",  # nosec
        help="Host for the Telegram webhook server (default: %(default)s)",
    )
    parser.add_argument(
        "--webhook-port",
        type=int,
        default=8080,
        help="Port for the Telegram webhook server (default: %(default)s)",
    )
    parser.add_argument(
        "--operator-chat-id",
        type=int,
        default=_default_chat_id,
        help="Telegram chat ID for HITL operator notifications",
    )
    parser.add_argument(
        "--staging-dir",
        type=pathlib.Path,
        default=pathlib.Path(str(SystemPaths.get_base_drive() / "MO2Portable/downloads")),
        help="MO2 staging directory for mod downloads",
    )
    parser.add_argument(
        "--xedit-exe",
        type=pathlib.Path,
        default=pathlib.Path(config.xedit_exe) if config.xedit_exe else None,
        help="Path to the SSEEdit executable",
    )
    parser.add_argument(
        "--install-dir",
        type=pathlib.Path,
        default=pathlib.Path(config.install_dir or str(SystemPaths.modding_root())),
        help="Directory for auto-installing tools like LOOT/SSEEdit",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    return parser


def _nombres_de_modo(parser: argparse.ArgumentParser) -> frozenset[str]:
    """Los valores válidos de ``--mode``, leídos del parser real.

    La única fuente es el ``choices=[...]`` literal de ``add_argument("--mode", ...)``: ``tests/test_cli_modes.py`` lo
    extrae del AST de este archivo (contrato estático del launcher), así que no puede pasar a ser una constante aparte.
    """
    return frozenset(parser._option_string_actions["--mode"].choices or ())


#: Modos cuyo despacho en ``_main`` consume el posicional ``command``: ``oneshot`` lo ejecuta y ``security`` se lo
#: pasa a ``_run_security`` (``--mode security "scan <ruta>"``). En cualquier otro modo el posicional se ignoraba en
#: silencio y la invocación caía al REPL. ``tests/test_main.py`` congela la clasificación contra los ``choices``.
_MODOS_CON_COMANDO = frozenset({"oneshot", "security"})


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.mode not in _MODOS_CON_COMANDO and args.command is not None:
        # Solo se sugiere ``--mode <valor>`` si el valor ES un modo; un comando corriente va con ``--mode oneshot``.
        sugerencia = (
            f"¿quisiste pasar `--mode {args.command}`?"
            if args.command in _nombres_de_modo(parser)
            else "¿quisiste ejecutarlo con `--mode oneshot`?"
        )
        parser.error(
            f"el argumento posicional {args.command!r} solo se admite con `--mode oneshot` o `--mode security` "
            f"({sugerencia})"
        )
    return args


# Compat: el helper vive ahora en logging_config (compartido con el bootstrap de
# la GUI, que instala su propio handler en el loop de NiceGUI). Se mantiene el
# nombre para call-sites y tests existentes.
_install_loop_exception_handler = install_loop_exception_handler


async def _install_vfs_bridge(args: argparse.Namespace) -> pathlib.Path:
    """Instala/actualiza el plugin MO2 sin iniciar el daemon completo."""
    from sky_claw.local.mo2.bridge_installer import MO2BridgeInstaller
    from sky_claw.local.mo2.vfs_broker import VfsBrokerError, vfs_instance_id

    if sys.platform != "win32":
        raise VfsBrokerError(
            "La instalación del bridge MO2/USVFS debe ejecutarse desde Windows; "
            "un intérprete Linux/WSL no puede ser lanzado por MO2."
        )

    root = pathlib.Path(args.mo2_root).resolve()
    instance_id = vfs_instance_id(root)
    descriptor = Config.DEFAULT_CONFIG_DIR / "vfs_bridge" / instance_id / f"{instance_id}.json"
    worker_prefix: tuple[str, ...]
    if getattr(sys, "frozen", False):
        worker_executable = pathlib.Path(sys.executable)
        worker_prefix = ("--vfs-worker",)
    elif os.environ.get("SKYCLAW_ALLOW_DEV_WORKER") == "1":
        # Escape hatch SOLO para pruebas locales: un intérprete de venv (el
        # python.exe de uv/venv es un trampoline/reparse point) es rechazado por
        # la inyección de USVFS con Error 5 (ERROR_ACCESS_DENIED). No usar en
        # producción — validado en el smoke real del PR #350.
        logger.warning(
            "install-vfs-bridge: worker NO congelado por SKYCLAW_ALLOW_DEV_WORKER=1. "
            "USVFS puede rechazar la inyección de un intérprete de venv (Error 5)."
        )
        worker_executable = pathlib.Path(sys.executable)
        worker_prefix = ("-m", "sky_claw.local.mo2.vfs_worker")
    else:
        # Fail-closed: el worker en producción DEBE ser el binario congelado
        # (PyInstaller). USVFS no puede inyectar el trampoline python.exe de un
        # venv y falla con Error 5 — un fallo opaco que costó una tarde de debug
        # en el smoke del PR #350. Rechazamos temprano con un mensaje accionable.
        raise VfsBrokerError(
            "install-vfs-bridge debe ejecutarse desde el ejecutable congelado "
            "(PyInstaller): USVFS no puede inyectar un intérprete de venv/trampoline "
            "y falla con Error 5 (ERROR_ACCESS_DENIED). Empaquetá con "
            "`pyinstaller sky_claw.spec` y corré este comando desde el .exe "
            "resultante. Para pruebas locales, exportá SKYCLAW_ALLOW_DEV_WORKER=1."
        )
    installer = MO2BridgeInstaller()
    installed = await asyncio.to_thread(
        installer.install,
        mo2_root=root,
        worker_executable=worker_executable,
        worker_prefix=worker_prefix,
        descriptor_path=descriptor,
        instance_id=instance_id,
    )
    logger.info("Bridge MO2/USVFS instalado en %s", installed)
    return installed


async def _run_vfs_health(args: argparse.Namespace) -> None:
    """Ejecuta worker+nieto bajo USVFS sin arrancar el resto del daemon."""
    from sky_claw.app.core.path_resolver import PathResolutionService
    from sky_claw.app.security.path_validator import PathValidator
    from sky_claw.local.mo2.vfs_attestation import build_attestation_challenge
    from sky_claw.local.mo2.vfs_broker import VfsBrokerError, VfsExecutionBroker, vfs_instance_id
    from sky_claw.local.mo2.vfs_contracts import VfsJob

    root = pathlib.Path(args.mo2_root).resolve()
    game = None if args.skyrim_path is None else pathlib.Path(args.skyrim_path).resolve()
    if not (root / "ModOrganizer.exe").is_file():
        raise VfsBrokerError(f"ModOrganizer.exe no existe bajo {root}")
    if game is None or not (game / "Data").is_dir():
        raise VfsBrokerError("--skyrim-path debe apuntar a una instalacion con Data")

    # Resuelve explícitamente install_root, data_root y mods_dir
    from sky_claw.app_context import _construir_raices_sandbox

    sandbox_roots = _construir_raices_sandbox(
        mo2_root=root,
        install_dir=None,
        skyrim_path=game,
    )
    validator = PathValidator(roots=sandbox_roots)
    path_service = PathResolutionService(
        path_validator=validator,
        profile_name=args.vfs_profile,
        mo2_install_dir=root,
    )
    resolved_install = path_service.get_mo2_path() or root
    resolved_data = path_service.get_mo2_instance_data_root() or resolved_install
    destino_mods = path_service.get_mo2_mods_path_para_destino()
    resolved_mods = destino_mods if destino_mods is not None else (resolved_data / "mods")

    instance_id = vfs_instance_id(resolved_install)
    broker = VfsExecutionBroker(
        instance_id=instance_id,
        state_dir=Config.DEFAULT_CONFIG_DIR / "vfs_bridge" / instance_id,
    )
    await broker.start()
    try:
        challenge = await asyncio.to_thread(
            build_attestation_challenge,
            data_root=resolved_data,
            mods_dir=resolved_mods,
            profile=args.vfs_profile,
            physical_data_dir=game / "Data",
        )
        job = VfsJob.create(
            instance_id=instance_id,
            profile=args.vfs_profile,
            tool_id="health",
            payload={},
            timeout_seconds=args.vfs_timeout,
            expected_fingerprint=challenge.profile_fingerprint,
            mutation_targets=(),
        )
        result = await broker.submit(
            job,
            challenge=challenge,
            install_root=resolved_install,
            data_root=resolved_data,
            mods_dir=resolved_mods,
            virtual_data_dir=game / "Data",
        )
        if not result.success:
            raise VfsBrokerError(result.message or "el probe VFS fallo")
        logger.info(
            "Probe VFS correcto para perfil %s; worker y nieto validaron el canary",
            args.vfs_profile,
        )
    finally:
        await broker.close()


def _avisar_de_otras_instancias_en_cuarentena(con_cuarentena: list[EstadoDeCuarentena], instance_id: str) -> None:
    """Avisa de las instancias en cuarentena que NO son la de ``--mo2-root``: con otra raíz, ``release`` no encuentra nada."""
    for otra in (e for e in con_cuarentena if e.instance_id != instance_id):
        logger.warning(
            "Otra instancia está EN CUARENTENA en %s: %s (job=%s). Si es la de tu MO2, pasá su --mo2-root",
            otra.state_dir,
            otra.instance_id,
            otra.job_id,
        )


async def _run_vfs_quarantine(args: argparse.Namespace) -> EstadoDeCuarentena:
    """H-4: informa o libera la cuarentena persistida del broker VFS, sin arrancar el daemon.

    Despachado ANTES de ``AppContext``: arrancarlo levantaría el broker y tomaría el lock que este modo necesita ver
    libre. La instancia sale de ``--mo2-root`` (igual que ``install-vfs-bridge``); además se listan todas las
    instancias con cuarentena bajo ``vfs_bridge``, por si esa ruta no coincide con la que usó la sesión.
    """
    from sky_claw.local.mo2.vfs_broker import vfs_instance_id
    from sky_claw.local.mo2.vfs_quarantine import (
        como_liberar,
        leer_estado_de_cuarentena,
        liberar_cuarentena,
        listar_instancias_con_cuarentena,
    )

    base = Config.DEFAULT_CONFIG_DIR / "vfs_bridge"
    instance_id = vfs_instance_id(pathlib.Path(args.mo2_root).resolve())
    state_dir = base / instance_id

    if args.vfs_quarantine_action == "release":
        previo = await asyncio.to_thread(liberar_cuarentena, state_dir, instance_id, evidencia=args.vfs_evidence or "")
        if previo.en_cuarentena:
            logger.info("Cuarentena de la instancia %s liberada: ya se puede arrancar Sky-Claw", instance_id)
        else:
            _avisar_de_otras_instancias_en_cuarentena(
                await asyncio.to_thread(listar_instancias_con_cuarentena, base), instance_id
            )
        return previo

    estado = await asyncio.to_thread(leer_estado_de_cuarentena, state_dir, instance_id)
    if estado.en_cuarentena:
        origen = estado.origen.value if estado.origen is not None else "?"
        logger.warning(
            "Instancia %s EN CUARENTENA (origen=%s, job=%s, razón=%s, dueño vivo=%s)",
            instance_id,
            origen,
            estado.job_id,
            estado.razon,
            "sí" if estado.dueno_vivo else "no",
        )
        logger.warning("Para liberarla: %s", como_liberar(pathlib.Path(args.mo2_root).resolve()))
    else:
        logger.info("Instancia %s sin cuarentena persistida", instance_id)
    _avisar_de_otras_instancias_en_cuarentena(
        await asyncio.to_thread(listar_instancias_con_cuarentena, base), instance_id
    )
    return estado


def _run_bodyslide_recovery(args: argparse.Namespace) -> InformeDelOperador:
    """PR 2c-2b: salidas manuales del journal de BodySlide, sin arrancar el daemon.

    Despachado ANTES de ``AppContext``, como ``vfs-quarantine``: arrancarlo tomaría el recurso
    ``bodyslide-meshes`` que el operador está liberando. Ninguna acción restaura la config: lo
    hace el reconciliador en el próximo arranque, con sus propias pruebas.
    """
    from sky_claw.local.tools.bodyslide_journal import AlmacenDeJournal
    from sky_claw.local.tools.bodyslide_operador import AccionDelOperador, ejecutar_accion_del_operador

    exe = args.bodyslide_exe
    informe = ejecutar_accion_del_operador(
        AccionDelOperador(args.bodyslide_recovery_action),
        almacen=AlmacenDeJournal(),
        exe_dir=None if exe is None else pathlib.Path(exe).parent,
        evidencia=args.bodyslide_evidence,
    )
    if informe.detalle:
        logger.info("%s", informe.detalle)
    logger.info("Recurso bodyslide-meshes %s", "EN CUARENTENA" if informe.cuarentena_activa else "sin cuarentena")
    if not informe.journals:
        logger.info("Sin journals de BodySlide pendientes")
    for visto in informe.journals:
        logger.warning(
            "Journal %s: estado=%s, corrida=%s%s",
            visto.archivo,
            visto.estado,
            visto.run_id or "?",
            " (esta instalación)" if visto.de_esta_instalacion else "",
        )
    return informe


async def _main(argv_or_args: list[str] | argparse.Namespace | None = None) -> None:
    """Asynchronous runner for CLI, Telegram, oneshot and security modes.

    Accepts either raw argv strings (for testing) or a pre-parsed Namespace.
    """
    args = argv_or_args if isinstance(argv_or_args, argparse.Namespace) else _parse_args(argv_or_args)
    _install_loop_exception_handler()

    logger.info("Sky-Claw starting in %s mode", args.mode)
    if args.mode == "install-vfs-bridge":
        await _install_vfs_bridge(args)
        return
    if args.mode == "vfs-health":
        await _run_vfs_health(args)
        return
    if args.mode == "vfs-quarantine":
        from sky_claw.local.mo2.vfs_quarantine import CuarentenaNoLiberableError

        try:
            await _run_vfs_quarantine(args)
        except CuarentenaNoLiberableError as exc:
            # Rechazo ESPERADO (sin evidencia, dueño vivo, disco que falla): la instancia sigue aislada y el operador
            # necesita el motivo, no un traceback bajo «Fallo no manejado en el proceso principal».
            logger.error("No se liberó la cuarentena: %s", exc)
            sys.exit(1)
        return
    if args.mode == "bodyslide-recovery":
        from sky_claw.local.tools.bodyslide_operador import AccionDelOperadorRechazadaError

        try:
            await asyncio.to_thread(_run_bodyslide_recovery, args)
        except AccionDelOperadorRechazadaError as exc:
            # Rechazo ESPERADO (sin evidencia, dueño vivo, otra máquina): el estado no cambió y el operador
            # necesita el motivo, no un traceback.
            logger.error("BodySlide: no se cambió nada: %s", exc)
            sys.exit(1)
        return
    if args.mode == "oneshot" and not args.command:
        logger.error("Oneshot mode requires a command argument.")
        sys.exit(1)

    ctx = AppContext(args)
    await ctx.start()
    try:
        if args.mode == "cli":
            await _run_cli(ctx)
        elif args.mode == "oneshot":
            await _run_oneshot(ctx, args.command)
        elif args.mode == "telegram":
            await _run_telegram(ctx, args.webhook_host, args.webhook_port)
        elif args.mode == "security":
            await _run_security(ctx, args.command)
    finally:
        await ctx.stop()


def _install_sigterm_handler() -> None:
    """Translate SIGTERM into ``KeyboardInterrupt`` so ``asyncio.run`` unwinds
    gracefully — running ``AppContext.stop()`` and the runners' ``CancelledError``
    cleanup — instead of the process being killed outright and orphaning heavy
    external tools (DynDOLOD/xEdit/BodySlide) that hold VFS/Data handles.

    Best-effort: no-ops where signals are unavailable (e.g. non-main thread).
    On Windows this covers ``os.kill(pid, SIGTERM)``; the console-close event
    (CTRL_CLOSE_EVENT) is not delivered as a Python signal and is out of scope.
    """

    def _raise_keyboard_interrupt(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    with contextlib.suppress(ValueError, OSError, AttributeError):
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)


def _ensure_std_streams() -> None:
    """A PyInstaller ``--windowed`` build starts with ``sys.stdout`` and
    ``sys.stderr`` set to ``None`` (there is no console). Code that writes to
    them with a bare ``print`` — notably NiceGUI/uvicorn's startup banner —
    then raises ``AttributeError: 'NoneType' object has no attribute 'write'``
    and tears the GUI process down before the server can bind its port.

    Route missing streams through the queue-backed logger. This keeps all I/O
    on the listener thread and avoids a separate unstructured startup file.
    """
    install_missing_std_stream_adapters()


def main(argv: list[str] | None = None) -> None:
    """Unified entry point controller."""
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    if effective_argv and effective_argv[0] in ("--vfs-worker", "--vfs-probe-child"):
        from sky_claw.local.mo2.vfs_arranque import arrancar_worker

        _despachar_sin_dialogo(lambda: arrancar_worker(effective_argv[0], effective_argv[1:]))
    # Todo lo que puede fallar va DENTRO del try: una excepción que escapa de acá abre el diálogo modal de PyInstaller
    # (ver ``_despachar_sin_dialogo``). Hasta tener ``args``, lo que pide el argv decide si hay un operador mirando.
    modo: str | None = _modo_declarado(effective_argv)
    try:
        if effective_argv and effective_argv[0] == _FLAG_EXPORTAR_LICENCIAS:
            codigo_export = _despachar_export_licencias(effective_argv[1:])
            raise SystemExit(codigo_export)
        log_level = logging.DEBUG if "--verbose" in effective_argv or "-v" in effective_argv else logging.INFO
        setup_logging(level=log_level)
        _ensure_std_streams()
        args = _parse_args(effective_argv)
        modo = args.mode

        # P0: SIGTERM (Unix/WSL2) must trigger graceful shutdown in EVERY mode
        # so in-flight external processes are killed, not orphaned. In GUI mode
        # NiceGUI/uvicorn install their own handlers once running; this covers
        # the startup window plus the non-GUI asyncio.run loop.
        _install_sigterm_handler()

        if args.mode == "gui":
            from sky_claw.app.modes.gui_mode import run_gui_mode  # lazy: pulls NiceGUI

            run_gui_mode(args)
        else:
            asyncio.run(_main(args))
    except KeyboardInterrupt:
        logger.info("Apagado solicitado por interrupción de teclado")
    except asyncio.CancelledError:
        logger.critical("Cancelación no manejada en el proceso principal", exc_info=True)
        if _es_interactivo(modo):
            raise
        # Fuera de la GUI no hay nadie que cierre el diálogo modal de PyInstaller: ver ``_despachar_sin_dialogo``.
        _volcar_traceback_a_stderr()
        raise SystemExit(EXIT_EXCEPCION_NO_MANEJADA) from None
    except Exception:
        logger.critical("Fallo no manejado en el proceso principal", exc_info=True)
        if _es_interactivo(modo):
            raise
        _volcar_traceback_a_stderr()
        raise SystemExit(EXIT_EXCEPCION_NO_MANEJADA) from None
    finally:
        shutdown_logging()


if __name__ == "__main__":
    main()
