"""Orquestador de build controlado, aislado y trazable para SkyClawApp.exe (PR-LIC-03B).

Cumple B01–B15:
- B01: Entorno virtual exclusivo y temporal de compilación, sin reutilizar ``.venv`` de desarrollo.
- B02/B09/B12: Instalación con ``--require-hashes --no-deps --only-binary=:all:`` sobre
  ``requirements-build.lock`` y el lock del perfil (``requirements.lock`` o
  ``requirements-native-parallax.lock``), instalación del proyecto con
  ``--no-deps --no-build-isolation .`` y verificación ``pip check`` con el intérprete real.
- B03: Perfiles explícitos ``base`` (default, excluye ``numpy``/``pillow`` según ADR 0014) y
  ``native-parallax``.
- B04/B13: Captura y saneamiento del ``Analysis TOC`` real de PyInstaller (sin rutas personales),
  vinculación SHA-256 de commit, estado limpio/sucio del árbol fuente, locks, entorno, TOC y EXE.
- B05/B08: Validación de entorno instalado (runtime + build) separada de la redistribución efectiva;
  detecta si una dependencia exclusiva de build terminó dentro del EXE sin fusionar locks.
- B11: Protección estricta contra rutas peligrosas/preexistentes/symlinks y promoción atómica del EXE.
- B14: Veredicto técnico independiente que falla cerrado ante cualquier blocker desconocido y
  preserva intactos los bloqueos jurídicos conocidos sin otorgar certificación legal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_RAIZ_REPO = Path(__file__).resolve().parents[1]
if str(_RAIZ_REPO) not in sys.path:
    sys.path.insert(0, str(_RAIZ_REPO))

EXIT_OK = 0
EXIT_FAIL_CLOSED = 1
EXIT_ERROR = 2

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PIN_EXACTO_RE = re.compile(r"^([A-Za-z0-9_.\-]+)==([^\s;\\]+)")
_OPERADORES_FLOTANTES: tuple[str, ...] = (">=", "<=", "~=", "!=", ">", "<")
_HASH_SHA256_RE = re.compile(r"--hash=sha256:([0-9a-fA-F]+)")

#: Herramientas obligatorias del toolchain de build (B09/B12).
HERRAMIENTAS_BUILD_OBLIGATORIAS: frozenset[str] = frozenset(
    {
        "pyinstaller",
        "pyinstaller-hooks-contrib",
        "hatchling",
        "hatch-vcs",
    }
)

#: Dependencias exclusivas del perfil opcional ``native-parallax`` (ADR 0014 / B03).
PAQUETES_EXTRA_NATIVE_PARALLAX: frozenset[str] = frozenset({"numpy", "pillow"})

#: Únicos prefijos de blockers puramente jurídicos conocidos (B14: cualquier otro blocker falla cerrado).
PREFIJOS_BLOQUEOS_JURIDICOS_CONOCIDOS: tuple[str, ...] = (
    "pending_legal_review=",
    "license_uncertainties=",
    "mo2_integration_boundary_pending_legal=",
)

#: Segmentos de ruta prohibidos como destino de build/limpieza (B11).
_SEGMENTOS_PROHIBIDOS = frozenset(
    {
        ".venv",
        "venv",
        "skyrim",
        "skyrim special edition",
        "mod organizer",
        "mod organizer 2",
        "mo2",
        "golden master",
        "windows",
        "program files",
        "program files (x86)",
        "system32",
    }
)

_PERFIL_A_LOCK: dict[str, str] = {
    "base": "requirements.lock",
    "native-parallax": "requirements-native-parallax.lock",
}


class BuildControladoError(ValueError):
    """Error fail-closed durante validación o ejecución del build controlado."""


@dataclass(frozen=True, slots=True)
class EntradaLock:
    """Entrada fijada por versión exacta y hashes SHA-256 en un archivo ``*.lock``."""

    nombre: str
    version: str
    hashes: tuple[str, ...]


def _normalizar_pep503(nombre: str) -> str:
    return re.sub(r"[-_.]+", "-", nombre).lower()


def _sha256_archivo(ruta: Path) -> tuple[str, int]:
    resumen = hashlib.sha256()
    total = 0
    with ruta.open("rb") as fh:
        while bloque := fh.read(1024 * 1024):
            resumen.update(bloque)
            total += len(bloque)
    return resumen.hexdigest(), total


def _escribir_json_atomico(datos: Mapping[str, Any], destino: Path) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(datos, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=destino.parent, prefix=f".{destino.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_path, destino)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def parsear_lockfile_con_hashes(ruta: Path) -> dict[str, EntradaLock]:
    """Parsea un lockfile exigiendo pin ``==``, hashes ``sha256`` válidos y unicidad PEP 503 (B09)."""
    if not ruta.is_file():
        raise BuildControladoError(f"Lockfile inexistente: {ruta}")

    lineas_crudas = ruta.read_text(encoding="utf-8").splitlines()
    # Unir continuaciones con '\'
    bloques: list[str] = []
    acumulado = ""
    for linea in lineas_crudas:
        limpia = linea.strip()
        if not limpia or limpia.startswith("#"):
            if acumulado:
                bloques.append(acumulado)
                acumulado = ""
            continue
        if limpia.endswith("\\"):
            acumulado += " " + limpia[:-1].strip()
        else:
            acumulado += " " + limpia
            bloques.append(acumulado.strip())
            acumulado = ""
    if acumulado:
        bloques.append(acumulado.strip())

    entradas: dict[str, EntradaLock] = {}
    for bloque in bloques:
        if bloque.startswith(("-r ", "-c ", "--")):
            continue
        m_pin = _PIN_EXACTO_RE.match(bloque)
        if m_pin is None:
            if any(op in bloque for op in _OPERADORES_FLOTANTES):
                raise BuildControladoError(f"Dependencia con versión flotante prohibida en {ruta.name}: {bloque[:80]}")
            raise BuildControladoError(f"Entrada no fijada con '==' en {ruta.name}: {bloque[:80]}")

        nombre_norm = _normalizar_pep503(m_pin.group(1))
        version = m_pin.group(2)
        if nombre_norm in entradas:
            raise BuildControladoError(f"Paquete duplicado tras normalización PEP 503 en {ruta.name}: '{nombre_norm}'")

        hashes_raw = _HASH_SHA256_RE.findall(bloque)
        if not hashes_raw:
            raise BuildControladoError(f"Falta --hash=sha256 para '{nombre_norm}=={version}' en {ruta.name}")
        hashes_validos: list[str] = []
        for h in hashes_raw:
            h_low = h.lower()
            if _SHA256_RE.fullmatch(h_low) is None or set(h_low) == {"0"}:
                raise BuildControladoError(f"Hash SHA-256 inválido para '{nombre_norm}' en {ruta.name}: {h!r}")
            hashes_validos.append(f"sha256:{h_low}")

        entradas[nombre_norm] = EntradaLock(
            nombre=nombre_norm,
            version=version,
            hashes=tuple(sorted(set(hashes_validos))),
        )

    if not entradas:
        raise BuildControladoError(f"Lockfile vacío sin paquetes fijados: {ruta}")
    return entradas


def verificar_compatibilidad_locks(
    lock_runtime: Mapping[str, EntradaLock],
    lock_build: Mapping[str, EntradaLock],
) -> None:
    """Verifica ausencia de conflictos de versión entre locks y presencia de herramientas obligatorias (B09/B12)."""
    faltantes_build = sorted(HERRAMIENTAS_BUILD_OBLIGATORIAS - set(lock_build))
    if faltantes_build:
        raise BuildControladoError(f"Herramientas obligatorias de build ausentes en lock: {', '.join(faltantes_build)}")

    comunes = sorted(set(lock_runtime) & set(lock_build))
    conflictos = [
        f"{pkg}: runtime={lock_runtime[pkg].version} != build={lock_build[pkg].version}"
        for pkg in comunes
        if lock_runtime[pkg].version != lock_build[pkg].version
    ]
    if conflictos:
        raise BuildControladoError("Conflicto de versiones entre lock runtime y build: " + "; ".join(conflictos))


def validar_entorno_build(
    instalados: Mapping[str, str],
    lock_runtime: Mapping[str, EntradaLock],
    lock_build: Mapping[str, EntradaLock],
    *,
    perfil: str,
) -> list[str]:
    """Valida que el entorno instalado coincida exactamente con los locks autorizados (B03/B05/B09)."""
    if perfil not in _PERFIL_A_LOCK:
        return [f"Perfil no soportado: {perfil!r}"]

    errores: list[str] = []
    inst_norm = {_normalizar_pep503(k): v for k, v in instalados.items()}
    autorizados_base = set(lock_runtime) | set(lock_build) | {"sky-claw", "pip"}

    # 1. Paquetes no autorizados (bloquea fugas de [dev] o inyecciones)
    extras = sorted(set(inst_norm) - autorizados_base)
    for pkg in extras:
        errores.append(f"Paquete no autorizado instalado en entorno de build: {pkg}=={inst_norm[pkg]}")

    # 2. Faltantes obligatorios de runtime
    for pkg, entry in sorted(lock_runtime.items()):
        if pkg not in inst_norm:
            errores.append(f"Paquete runtime requerido ausente en entorno de build: {pkg}=={entry.version}")
        elif inst_norm[pkg] != entry.version:
            errores.append(
                f"Versión incorrecta de paquete runtime '{pkg}': instalado={inst_norm[pkg]} lock={entry.version}"
            )

    # 3. Faltantes obligatorios de build y versiones exactas
    for pkg, entry in sorted(lock_build.items()):
        if pkg not in inst_norm:
            errores.append(f"Herramienta/dependencia de build requerida ausente: {pkg}=={entry.version}")
        elif inst_norm[pkg] != entry.version:
            errores.append(
                f"Versión incorrecta de paquete build '{pkg}': instalado={inst_norm[pkg]} lock={entry.version}"
            )

    # 4. Perfil base no puede contener extras opcionales de native-parallax (ADR 0014 / B03)
    if perfil == "base":
        for pkg_np in sorted(PAQUETES_EXTRA_NATIVE_PARALLAX):
            if pkg_np in inst_norm:
                errores.append(f"Contaminación de perfil base (ADR 0014): '{pkg_np}' prohibido en perfil base")

    return errores


def _es_reparse_point_o_symlink(ruta: Path) -> bool:
    if ruta.is_symlink():
        return True
    try:
        st = os.lstat(ruta)
    except OSError:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    return bool(attrs & reparse_flag)


def validar_directorio_build_seguro(ruta: Path, repo_root: Path) -> Path:
    """Rechaza destinos peligrosos, preexistentes, symlinks/junctions o rutas de desarrollo/Skyrim/MO2 (B11)."""
    if not str(ruta).strip():
        raise BuildControladoError("Ruta de directorio de build vacía")

    # Rechazar symlinks/junctions en la ruta o en cualquier ancestro inmediato
    for candidato in (ruta, *ruta.parents):
        if candidato.exists() and _es_reparse_point_o_symlink(candidato):
            raise BuildControladoError(f"Ruta de build peligrosa (symlink/junction/reparse point): {candidato}")
        if candidato == candidato.parent:
            break

    resuelta = ruta.resolve()
    repo_resuelto = repo_root.resolve()

    if resuelta == resuelta.parent or resuelta == Path.home().resolve():
        raise BuildControladoError(f"Ruta de build prohibida (raíz del sistema o HOME): {resuelta}")

    if resuelta == repo_resuelto or repo_resuelto in resuelta.parents:
        raise BuildControladoError(
            f"Ruta de build peligrosa: coincide o vive dentro del checkout del repositorio ({resuelta})"
        )

    partes_bajas = {p.casefold() for p in resuelta.parts}
    prohibidas = partes_bajas & _SEGMENTOS_PROHIBIDOS
    if prohibidas:
        raise BuildControladoError(
            f"Ruta de build prohibida (coincide con desarrollo/Skyrim/MO2/SO: {sorted(prohibidas)}): {resuelta}"
        )

    if resuelta.exists():
        raise BuildControladoError(
            f"El directorio de build ya existe ({resuelta}); se exige un directorio nuevo y exclusivo por ejecución"
        )

    return resuelta


def _sanear_ruta_origen(ruta_str: str, repo_root: Path, build_venv: Path) -> str:
    """Reemplaza prefijos absolutos locales por marcadores canónicos sin exponer rutas personales (B13)."""
    if not ruta_str:
        return ""
    norm = ruta_str.replace("\\", "/")
    repo_norm = str(repo_root.resolve()).replace("\\", "/").rstrip("/")
    venv_norm = str(build_venv.resolve()).replace("\\", "/").rstrip("/")
    home_norm = str(Path.home().resolve()).replace("\\", "/").rstrip("/")

    norm_low = norm.casefold()
    if norm_low.startswith(repo_norm.casefold() + "/"):
        return "<REPO>/" + norm[len(repo_norm) + 1 :]
    if norm_low.startswith(venv_norm.casefold() + "/"):
        return "<BUILD_VENV>/" + norm[len(venv_norm) + 1 :]
    if norm_low.startswith(home_norm.casefold() + "/"):
        return "<EXTERNAL>/" + Path(norm).name
    if re.match(r"^[A-Za-z]:/", norm) or norm.startswith("/"):
        return "<EXTERNAL>/" + Path(norm).name
    return norm


def sanear_analysis_toc(
    toc: Mapping[str, Any],
    *,
    repo_root: Path,
    build_venv: Path,
) -> dict[str, Any]:
    """Sanea las rutas origen del ``Analysis TOC`` real para no publicar rutas personales (B13)."""
    resultado: dict[str, Any] = {"schema_version": int(toc.get("schema_version", 1))}
    for seccion in ("pure", "binaries", "datas", "scripts"):
        items = toc.get(seccion, [])
        if not isinstance(items, list):
            resultado[seccion] = items
            continue
        limpios: list[Any] = []
        for item in items:
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                dest = str(item[0])
                src_saneado = _sanear_ruta_origen(str(item[1]), repo_root, build_venv)
                tipo = str(item[2])
                extra = list(item[3:])
                limpios.append([dest, src_saneado, tipo, *extra])
            else:
                limpios.append(item)
        resultado[seccion] = limpios
    return resultado


def evaluar_veredicto_tecnico(
    reporte: Mapping[str, Any],
    lock_runtime: Mapping[str, EntradaLock],
    lock_build: Mapping[str, EntradaLock],
    *,
    smoke_passed: bool,
    entorno_errores: Sequence[str],
    spdx_schema_errors: Sequence[str],
) -> dict[str, Any]:
    """Evalúa validez técnica independiente preservando bloqueos jurídicos conocidos (B07/B08/B14)."""
    legal_blockers: list[str] = []
    technical_blockers: list[str] = []

    for b in reporte.get("blockers", []):
        b_str = str(b)
        if b_str.startswith(PREFIJOS_BLOQUEOS_JURIDICOS_CONOCIDOS):
            legal_blockers.append(b_str)
        else:
            technical_blockers.append(b_str)

    # B08: detectar si alguna dependencia exclusiva de build terminó redistribuida dentro del EXE
    solo_build_set = set(lock_build) - set(lock_runtime)
    redist = {_normalizar_pep503(p) for p in reporte.get("packages_summary", {}).get("redistributed", [])}
    build_en_exe = sorted(redist & solo_build_set)
    if build_en_exe:
        technical_blockers.append(f"build_deps_redistributed_in_exe={','.join(build_en_exe)}")

    if entorno_errores:
        technical_blockers.append(f"build_env_validation_errors={len(entorno_errores)}")
    if not smoke_passed:
        technical_blockers.append("smoke_frozen_executable_failed=1")
    if spdx_schema_errors:
        technical_blockers.append(f"spdx_official_schema_errors={len(spdx_schema_errors)}")
    if not reporte.get("structural_inventory_complete", False):
        technical_blockers.append("structural_inventory_incomplete=1")
    if not reporte.get("provenance_reconciliation_verified", False):
        technical_blockers.append("provenance_reconciliation_unverified=1")
    if reporte.get("cpython_version_uncertainty", True):
        technical_blockers.append("cpython_version_unverified=1")
    if reporte.get("lock_drift"):
        technical_blockers.append(f"lock_drift={len(reporte['lock_drift'])}")

    technical_valid = len(technical_blockers) == 0
    return {
        "technical_build_valid": technical_valid,
        "legal_gate_passed": bool(reporte.get("gate_passed", False)),
        "legal_certification": False,
        "technical_blockers": sorted(set(technical_blockers)),
        "legal_blockers": sorted(set(legal_blockers)),
        "build_deps_redistributed_in_exe": build_en_exe,
    }


def _entorno_pip_controlado(build_venv: Path) -> dict[str, str]:
    """Construye un entorno aislado para ``pip`` y PyInstaller eliminando cualquier ``PIP_*`` heredada (B09)."""
    vars_prohibidas = frozenset({"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV"})
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PIP_") and k.upper() not in vars_prohibidas}
    scripts_dir = build_venv / ("Scripts" if os.name == "nt" else "bin")
    env["PATH"] = str(scripts_dir) + os.pathsep + env.get("PATH", "")
    env["VIRTUAL_ENV"] = str(build_venv)
    env["PIP_CONFIG_FILE"] = os.devnull
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _ejecutar_cmd(
    cmd: Sequence[str], *, cwd: Path, env: dict[str, str], etiqueta: str
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        list(cmd),
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detalle = (proc.stderr or proc.stdout or "").strip()[-2000:]
        raise BuildControladoError(f"Fallo en paso '{etiqueta}' (rc={proc.returncode}): {detalle}")
    return proc


def _estado_git(repo_root: Path) -> dict[str, Any]:
    try:
        sha = subprocess.check_output(["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True).strip()
        status = subprocess.check_output(["git", "-C", str(repo_root), "status", "--porcelain"], text=True).splitlines()
        return {
            "commit": sha,
            "source_tree_clean": len(status) == 0,
            "dirty_entries_count": len(status),
        }
    except (OSError, subprocess.SubprocessError):
        return {"commit": "UNKNOWN", "source_tree_clean": False, "dirty_entries_count": -1}


def _recolectar_entorno_desde_interprete(
    python_exe: Path, repo_root: Path, env: dict[str, str]
) -> tuple[str, list[dict[str, Any]]]:
    """Recolecta versión exacta de Python y lista ``PaqueteEntorno`` ejecutando ``recolectar_entorno_actual`` en el venv de build."""
    codigo = (
        "import json, sys; "
        f"sys.path.insert(0, {str(repo_root)!r}); "
        "from scripts.reconciliar_sbom import recolectar_entorno_actual; "
        "import importlib.metadata as m; "
        "py = f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}'; "
        "pkgs = [p.como_dict() for p in recolectar_entorno_actual()]; "
        "inst = {d.metadata['Name']: d.version for d in m.distributions() if d.metadata.get('Name')}; "
        "print(json.dumps({'python_version': py, 'packages': pkgs, 'installed_map': inst}))"
    )
    proc = _ejecutar_cmd([str(python_exe), "-c", codigo], cwd=repo_root, env=env, etiqueta="recolectar_entorno_build")
    datos = json.loads(proc.stdout)
    return str(datos["python_version"]), list(datos["packages"])


def _obtener_mapa_instalados(python_exe: Path, repo_root: Path, env: dict[str, str]) -> tuple[str, dict[str, str]]:
    codigo = (
        "import json, sys, importlib.metadata as m; "
        "py = f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}'; "
        "inst = {}; "
        "[inst.__setitem__(d.metadata['Name'], d.version) for d in m.distributions() if d.metadata['Name']]; "
        "print(json.dumps({'python_version': py, 'installed': inst}))"
    )
    proc = _ejecutar_cmd([str(python_exe), "-c", codigo], cwd=repo_root, env=env, etiqueta="inspeccionar_entorno_build")
    datos = json.loads(proc.stdout)
    return str(datos["python_version"]), dict(datos["installed"])


def _limpiar_evidencia_obsoleta(out_dir: Path) -> None:
    """Elimina artefactos de corridas anteriores para impedir reutilización tras fallo (B11)."""
    for nombre in (
        "build_manifest.json",
        "analysis_toc.json",
        "build_env.json",
        "exe_sha256.json",
        "inventory.json",
        "sbom.spdx.json",
        "reconciliation_report.json",
    ):
        p = out_dir / nombre
        if p.is_file():
            p.unlink(missing_ok=True)


def ejecutar_build_controlado(
    *,
    repo_root: Path,
    perfil: str,
    dist_dir: Path,
    out_dir: Path,
    build_dir_opcional: Path | None = None,
    python_base: Path | None = None,
) -> tuple[dict[str, Any], int]:
    """Ejecuta el pipeline completo de build controlado en Windows aislado (B01–B14)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    _limpiar_evidencia_obsoleta(out_dir)

    if perfil not in _PERFIL_A_LOCK:
        raise BuildControladoError(f"Perfil inválido: {perfil!r}. Opciones: {sorted(_PERFIL_A_LOCK)}")

    lock_runtime_path = repo_root / _PERFIL_A_LOCK[perfil]
    lock_build_path = repo_root / "requirements-build.lock"
    lock_runtime = parsear_lockfile_con_hashes(lock_runtime_path)
    lock_build = parsear_lockfile_con_hashes(lock_build_path)
    verificar_compatibilidad_locks(lock_runtime, lock_build)

    creado_por_esta_ejecucion = False
    if build_dir_opcional is not None:
        build_root = validar_directorio_build_seguro(build_dir_opcional, repo_root)
        build_root.mkdir(parents=True, exist_ok=False)
        creado_por_esta_ejecucion = True
    else:
        build_root = Path(tempfile.mkdtemp(prefix=f"skyclaw-build-{perfil}-"))
        creado_por_esta_ejecucion = True

    build_venv = build_root / "venv"
    staging_dist = build_root / "dist"
    staging_work = build_root / "work"
    py_host = python_base or Path(sys.executable)

    try:
        # 1. Crear venv exclusivo (B01)
        _ejecutar_cmd(
            [str(py_host), "-m", "venv", str(build_venv)], cwd=repo_root, env=os.environ.copy(), etiqueta="venv"
        )
        env_ctrl = _entorno_pip_controlado(build_venv)
        py_build = (
            build_venv / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
        )

        # 2. Instalar toolchain de build y runtime con --require-hashes --no-deps --only-binary=:all: (B02/B09)
        _ejecutar_cmd(
            [
                str(py_build),
                "-m",
                "pip",
                "install",
                "--require-hashes",
                "--no-deps",
                "--only-binary=:all:",
                "-r",
                str(lock_build_path),
            ],
            cwd=repo_root,
            env=env_ctrl,
            etiqueta="pip_install_build_lock",
        )
        _ejecutar_cmd(
            [
                str(py_build),
                "-m",
                "pip",
                "install",
                "--require-hashes",
                "--no-deps",
                "--only-binary=:all:",
                "-r",
                str(lock_runtime_path),
            ],
            cwd=repo_root,
            env=env_ctrl,
            etiqueta="pip_install_runtime_lock",
        )
        # 3. Instalar metadatos de sky-claw sin resolución ni build isolation flotante (B02/B09).
        # Nota: se usa `-e .` con `--no-deps --no-build-isolation` porque hatchling 1.33 rechaza
        # duplicados en `force-include` de pyproject.toml al armar wheel estándar (fuera del write-set).
        _ejecutar_cmd(
            [str(py_build), "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e", "."],
            cwd=repo_root,
            env=env_ctrl,
            etiqueta="pip_install_project_no_isolation",
        )
        # 4. Ejecutar pip check con el intérprete real del venv de build (B09)
        _ejecutar_cmd([str(py_build), "-m", "pip", "check"], cwd=repo_root, env=env_ctrl, etiqueta="pip_check")

        # 5. Validar entorno instalado (B05/B08/B09)
        py_ver_build, mapa_inst = _obtener_mapa_instalados(py_build, repo_root, env_ctrl)
        errores_entorno = validar_entorno_build(mapa_inst, lock_runtime, lock_build, perfil=perfil)
        if errores_entorno:
            raise BuildControladoError("Validación de entorno de build falló: " + "; ".join(errores_entorno))

        _, paquetes_env = _recolectar_entorno_desde_interprete(py_build, repo_root, env_ctrl)
        env_json_path = out_dir / "build_env.json"
        _escribir_json_atomico({"python_version": py_ver_build, "packages": paquetes_env}, env_json_path)
        env_list_path = build_root / "build_env_list.json"
        env_list_path.write_text(json.dumps(paquetes_env, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        # 6. Ejecutar PyInstaller en staging aislado exportando Analysis TOC real (B04/B11/B13)
        toc_crudo_path = build_root / "analysis_toc_raw.json"
        env_pyi = dict(env_ctrl)
        env_pyi["SKYCLAW_EXPORT_ANALYSIS_TOC"] = str(toc_crudo_path)
        _ejecutar_cmd(
            [
                str(py_build),
                "-m",
                "PyInstaller",
                "sky_claw.spec",
                "--clean",
                "--noconfirm",
                "--distpath",
                str(staging_dist),
                "--workpath",
                str(staging_work),
            ],
            cwd=repo_root,
            env=env_pyi,
            etiqueta="pyinstaller_build",
        )
        exe_staging = staging_dist / "SkyClawApp.exe"
        if not exe_staging.is_file():
            raise BuildControladoError(f"PyInstaller no generó el ejecutable esperado en {exe_staging}")
        if not toc_crudo_path.is_file():
            raise BuildControladoError("sky_claw.spec no exportó el Analysis TOC real (SKYCLAW_EXPORT_ANALYSIS_TOC)")

        # Sanear rutas personales del Analysis TOC antes de persistirlo en evidencia (B13)
        toc_saneado = sanear_analysis_toc(
            json.loads(toc_crudo_path.read_text(encoding="utf-8")),
            repo_root=repo_root,
            build_venv=build_venv,
        )
        toc_out_path = out_dir / "analysis_toc.json"
        _escribir_json_atomico(toc_saneado, toc_out_path)

        # 7. Registrar SHA-256 del EXE en staging
        reg_sha_path = out_dir / "exe_sha256.json"
        _ejecutar_cmd(
            [
                str(py_build),
                "scripts/registrar_sha256_ejecutable.py",
                "registrar",
                str(exe_staging),
                "--json",
                str(reg_sha_path),
            ],
            cwd=repo_root,
            env=env_ctrl,
            etiqueta="registrar_sha256",
        )

        # 8. Smoke test aislado del ejecutable
        smoke_proc = subprocess.run(
            [str(py_build), "scripts/smoke_frozen_executable.py", str(exe_staging)],
            cwd=str(repo_root),
            env=env_ctrl,
            capture_output=True,
            text=True,
            check=False,
        )
        smoke_passed = smoke_proc.returncode == 0

        # 9. PR-LIC-03A: inventario efectivo + reconciliación contra Analysis TOC y build_env reales
        inv_path = out_dir / "inventory.json"
        sbom_path = out_dir / "sbom.spdx.json"
        rep_path = out_dir / "reconciliation_report.json"
        _ejecutar_cmd(
            [
                str(py_build),
                "scripts/inventario_ejecutable.py",
                str(exe_staging),
                "--out",
                str(inv_path),
                "--registro-sha256",
                str(reg_sha_path),
            ],
            cwd=repo_root,
            env=env_ctrl,
            etiqueta="inventario_ejecutable",
        )
        rec_proc = subprocess.run(
            [
                str(py_build),
                "scripts/reconciliar_sbom.py",
                "--inventory",
                str(inv_path),
                "--env-json",
                str(env_list_path),
                "--analysis-toc-json",
                str(toc_out_path),
                "--build-python-version",
                py_ver_build,
                "--lockfile",
                str(lock_runtime_path),
                "--check-spec-datas",
                "--sbom-out",
                str(sbom_path),
                "--report-out",
                str(rep_path),
            ],
            cwd=str(repo_root),
            env=env_ctrl,
            capture_output=True,
            text=True,
            check=False,
        )
        if rec_proc.returncode == 2 or not rep_path.is_file() or not sbom_path.is_file():
            raise BuildControladoError(f"Error en reconciliar_sbom.py (rc={rec_proc.returncode}): {rec_proc.stderr}")

        reporte = json.loads(rep_path.read_text(encoding="utf-8"))
        veredicto = evaluar_veredicto_tecnico(
            reporte,
            lock_runtime,
            lock_build,
            smoke_passed=smoke_passed,
            entorno_errores=errores_entorno,
            spdx_schema_errors=reporte.get("spdx_validation_errors", []),
        )

        # 10. Promover EXE al destino final SOLO si technical_build_valid es True (B11)
        exe_sha, exe_bytes = _sha256_archivo(exe_staging)
        exe_final = dist_dir / "SkyClawApp.exe"
        promoted = False
        if veredicto["technical_build_valid"]:
            dist_dir.mkdir(parents=True, exist_ok=True)
            tmp_dest = dist_dir / f".SkyClawApp.exe.{os.getpid()}.tmp"
            shutil.copy2(exe_staging, tmp_dest)
            os.replace(tmp_dest, exe_final)
            promoted = True

        git_info = _estado_git(repo_root)
        manifiesto: dict[str, Any] = {
            "schema_version": 1,
            "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "profile": perfil,
            "git": git_info,
            "bootstrap": {
                "python_version": py_ver_build,
                "platform": platform.platform(),
                "machine": platform.machine(),
                "pip_version": mapa_inst.get("pip", "UNKNOWN"),
                "build_tools": {
                    k: mapa_inst.get(_normalizar_pep503(k), mapa_inst.get(k, "ABSENT")) for k in sorted(lock_build)
                },
                "non_pinned_os_components": ["windows_os_version", "ucrt_system_dlls", "pyinstaller_zip_timestamps"],
            },
            "lockfiles": {
                "runtime": {"path": lock_runtime_path.name, "sha256": _sha256_archivo(lock_runtime_path)[0]},
                "build": {"path": lock_build_path.name, "sha256": _sha256_archivo(lock_build_path)[0]},
            },
            "artifacts": {
                "exe": {"name": "SkyClawApp.exe", "sha256": exe_sha, "bytes": exe_bytes, "promoted_to_dist": promoted},
                "exe_sha256_json": {"path": reg_sha_path.name, "sha256": _sha256_archivo(reg_sha_path)[0]},
                "analysis_toc_json": {"path": toc_out_path.name, "sha256": _sha256_archivo(toc_out_path)[0]},
                "build_env_json": {"path": env_json_path.name, "sha256": _sha256_archivo(env_json_path)[0]},
                "inventory_json": {"path": inv_path.name, "sha256": _sha256_archivo(inv_path)[0]},
                "sbom_spdx_json": {"path": sbom_path.name, "sha256": _sha256_archivo(sbom_path)[0]},
                "reconciliation_report_json": {"path": rep_path.name, "sha256": _sha256_archivo(rep_path)[0]},
            },
            "smoke_passed": smoke_passed,
            "verdict": veredicto,
        }
        _escribir_json_atomico(manifiesto, out_dir / "build_manifest.json")
        return manifiesto, (EXIT_OK if veredicto["technical_build_valid"] else EXIT_FAIL_CLOSED)
    finally:
        if creado_por_esta_ejecucion and build_root.exists():
            shutil.rmtree(build_root, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build controlado, aislado y trazable de SkyClawApp.exe (PR-LIC-03B).")
    parser.add_argument(
        "--profile", choices=sorted(_PERFIL_A_LOCK), default="base", help="Perfil de build (default: base)."
    )
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"), help="Directorio destino para SkyClawApp.exe.")
    parser.add_argument(
        "--out-dir", type=Path, default=Path("dist/build-evidence"), help="Directorio de evidencias del build."
    )
    parser.add_argument(
        "--build-dir", type=Path, default=None, help="Directorio temporal opcional (debe ser nuevo y seguro)."
    )
    parser.add_argument(
        "--validate-venv",
        type=Path,
        default=None,
        help="Modo CI: valida un venv ya instalado contra los locks y exporta --env-out sin reconstruir.",
    )
    parser.add_argument(
        "--env-out", type=Path, default=None, help="Ruta de salida para lista PaqueteEntorno en modo --validate-venv."
    )
    parser.add_argument(
        "--verify-report",
        type=Path,
        default=None,
        help="Modo CI: evalúa el veredicto técnico independiente (B08/B14) sobre un reconciliation_report.json existente.",
    )
    parser.add_argument(
        "--sanitize-toc",
        type=Path,
        default=None,
        help="Modo CI: sanea rutas absolutas/personales in-place en un analysis_toc.json exportado (B13).",
    )
    args = parser.parse_args(argv)

    repo_root = _RAIZ_REPO
    try:
        if args.sanitize_toc is not None:
            toc_raw = json.loads(args.sanitize_toc.read_text(encoding="utf-8"))
            venv_ref = args.validate_venv or (repo_root / ".build-venv")
            toc_clean = sanear_analysis_toc(toc_raw, repo_root=repo_root, build_venv=venv_ref)
            _escribir_json_atomico(toc_clean, args.sanitize_toc)
            print(f"build_controlado: analysis_toc saneado ({args.sanitize_toc})")
            return EXIT_OK

        if args.validate_venv is not None:
            lock_rt = parsear_lockfile_con_hashes(repo_root / _PERFIL_A_LOCK[args.profile])
            lock_b = parsear_lockfile_con_hashes(repo_root / "requirements-build.lock")
            verificar_compatibilidad_locks(lock_rt, lock_b)
            env_ctrl = _entorno_pip_controlado(args.validate_venv)
            py_v = (
                args.validate_venv
                / ("Scripts" if os.name == "nt" else "bin")
                / ("python.exe" if os.name == "nt" else "python")
            )
            py_ver, mapa = _obtener_mapa_instalados(py_v, repo_root, env_ctrl)
            errs = validar_entorno_build(mapa, lock_rt, lock_b, perfil=args.profile)
            if errs:
                for e in errs:
                    print(f"build_controlado: {e}", file=sys.stderr)
                return EXIT_FAIL_CLOSED
            if args.env_out is not None:
                _, pkgs = _recolectar_entorno_desde_interprete(py_v, repo_root, env_ctrl)
                args.env_out.parent.mkdir(parents=True, exist_ok=True)
                args.env_out.write_text(json.dumps(pkgs, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"build_controlado: entorno OK profile={args.profile} python={py_ver} pkgs={len(mapa)}")
            return EXIT_OK

        if args.verify_report is not None:
            lock_rt = parsear_lockfile_con_hashes(repo_root / _PERFIL_A_LOCK[args.profile])
            lock_b = parsear_lockfile_con_hashes(repo_root / "requirements-build.lock")
            reporte = json.loads(args.verify_report.read_text(encoding="utf-8"))
            ver = evaluar_veredicto_tecnico(
                reporte,
                lock_rt,
                lock_b,
                smoke_passed=True,
                entorno_errores=[],
                spdx_schema_errors=reporte.get("spdx_validation_errors", []),
            )
            print(
                f"build_controlado: technical_build_valid={ver['technical_build_valid']} "
                f"technical_blockers={ver['technical_blockers']} legal_blockers={ver['legal_blockers']}"
            )
            return EXIT_OK if ver["technical_build_valid"] else EXIT_FAIL_CLOSED

        manifiesto, codigo = ejecutar_build_controlado(
            repo_root=repo_root,
            perfil=args.profile,
            dist_dir=args.dist_dir,
            out_dir=args.out_dir,
            build_dir_opcional=args.build_dir,
        )
        ver = manifiesto["verdict"]
        print(
            f"build_controlado: profile={args.profile} technical_build_valid={ver['technical_build_valid']} "
            f"exe_sha256={manifiesto['artifacts']['exe']['sha256']} legal_blockers={len(ver['legal_blockers'])}"
        )
        return codigo
    except (BuildControladoError, OSError, ValueError) as exc:
        print(f"build_controlado: ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
