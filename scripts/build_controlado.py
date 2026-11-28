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
import contextlib
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
import uuid
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
_HASH_SHA256_RE = re.compile(r"--hash=sha256:(\S+)")

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

    conflictos_hash = [
        f"{pkg}: hashes runtime != hashes build"
        for pkg in comunes
        if set(lock_runtime[pkg].hashes) != set(lock_build[pkg].hashes)
    ]
    if conflictos_hash:
        raise BuildControladoError(
            "Conflicto de hashes SHA-256 en paquetes compartidos entre locks: " + "; ".join(conflictos_hash)
        )


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


def _verificar_sin_reparse_en_ancestros(ruta: Path, *, etiqueta: str) -> None:
    """Rechaza symlinks, junctions, reparse points y enlaces rotos en ``ruta`` o ancestros (B11/B17/B22)."""
    abs_sin_resolver = Path(os.path.abspath(str(ruta)))
    for candidato in (abs_sin_resolver, *abs_sin_resolver.parents):
        if _es_reparse_point_o_symlink(candidato):
            raise BuildControladoError(f"Ruta de {etiqueta} peligrosa (symlink/junction/reparse point): {candidato}")
        if candidato == candidato.parent:
            break


def resolver_out_dir_exclusivo(dist_dir: Path, *, perfil: str) -> Path:
    """Genera un subdirectorio exclusivo por ejecución bajo ``dist_dir/build-evidence`` (B20)."""
    base = dist_dir / "build-evidence"
    for _ in range(16):
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        candidato = base / f"{perfil}-{stamp}-{uuid.uuid4().hex[:8]}"
        if not candidato.exists():
            return candidato
    raise BuildControladoError(f"No se pudo generar un directorio de evidencia exclusivo bajo {base}")


def _validar_ruta_destino_comun(ruta: Path, repo_root: Path, *, etiqueta: str, prohibir_dentro_repo: bool) -> Path:
    if not str(ruta).strip():
        raise BuildControladoError(f"Ruta de {etiqueta} vacía")

    _verificar_sin_reparse_en_ancestros(ruta, etiqueta=etiqueta)

    resuelta = ruta.resolve()
    repo_resuelto = repo_root.resolve()

    if resuelta == resuelta.parent or resuelta == Path.home().resolve():
        raise BuildControladoError(f"Ruta de {etiqueta} prohibida (raíz del sistema o HOME): {resuelta}")

    if resuelta == repo_resuelto:
        raise BuildControladoError(f"Ruta de {etiqueta} peligrosa: coincide con la raíz del repositorio ({resuelta})")

    if prohibir_dentro_repo and repo_resuelto in resuelta.parents:
        raise BuildControladoError(
            f"Ruta de {etiqueta} peligrosa: vive dentro del checkout del repositorio ({resuelta})"
        )

    partes_bajas = {p.casefold() for p in resuelta.parts}
    prohibidas = partes_bajas & _SEGMENTOS_PROHIBIDOS
    if prohibidas:
        raise BuildControladoError(
            f"Ruta de {etiqueta} prohibida (coincide con desarrollo/Skyrim/MO2/SO: {sorted(prohibidas)}): {resuelta}"
        )

    return resuelta


def validar_directorio_build_seguro(ruta: Path, repo_root: Path) -> Path:
    """Rechaza destinos peligrosos, preexistentes, symlinks/junctions o rutas de desarrollo/Skyrim/MO2 (B11/B17)."""
    resuelta = _validar_ruta_destino_comun(ruta, repo_root, etiqueta="build", prohibir_dentro_repo=True)
    if resuelta.exists():
        raise BuildControladoError(
            f"El directorio de build ya existe ({resuelta}); se exige un directorio nuevo y exclusivo por ejecución"
        )
    return resuelta


def validar_destinos_build(
    *,
    dist_dir: Path,
    out_dir: Path,
    build_dir_opcional: Path | None,
    repo_root: Path,
) -> tuple[Path, Path, Path | None]:
    """Valida ``dist_dir``, ``out_dir`` y ``build_dir`` antes de cualquier escritura o limpieza (B17).

    Garantiza que no existan symlinks/junctions, rutas peligrosas, colisiones entre destinos ni
    sobrescritura/eliminación de evidencias preexistentes ajenas a la ejecución actual.
    """
    dist_res = _validar_ruta_destino_comun(dist_dir, repo_root, etiqueta="dist_dir", prohibir_dentro_repo=False)
    out_res = _validar_ruta_destino_comun(out_dir, repo_root, etiqueta="out_dir", prohibir_dentro_repo=False)

    if dist_res.exists() and not dist_res.is_dir():
        raise BuildControladoError(f"dist_dir existe pero no es un directorio: {dist_res}")
    exe_existente = dist_res / "SkyClawApp.exe"
    if exe_existente.exists() and _es_reparse_point_o_symlink(exe_existente):
        raise BuildControladoError(f"Destino de ejecutable peligroso (symlink/reparse point): {exe_existente}")

    if out_res.exists():
        if not out_res.is_dir():
            raise BuildControladoError(f"out_dir existe pero no es un directorio: {out_res}")
        if any(out_res.iterdir()):
            raise BuildControladoError(
                f"out_dir preexistente no está vacío ({out_res}); se rechaza para no eliminar evidencias "
                "ajenas a la ejecución actual. Use un directorio nuevo o vacío."
            )

    if out_res == dist_res or out_res in dist_res.parents:
        raise BuildControladoError(
            f"Colisión de destinos: out_dir ({out_res}) no puede coincidir ni contener a dist_dir ({dist_res})"
        )

    build_res: Path | None = None
    if build_dir_opcional is not None:
        build_res = validar_directorio_build_seguro(build_dir_opcional, repo_root)
        for otro_nombre, otro in (("dist_dir", dist_res), ("out_dir", out_res)):
            if build_res == otro or build_res in otro.parents or otro in build_res.parents:
                raise BuildControladoError(
                    f"Colisión de destinos: build_dir ({build_res}) colisiona con {otro_nombre} ({otro})"
                )

    return dist_res, out_res, build_res


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


def validar_spdx_contra_schema_oficial(sbom_path: Path, schema_path: Path | None) -> dict[str, Any]:
    """Ejecuta validación contra el JSON Schema oficial SPDX 2.3 solo si se provee el schema (B19)."""
    if schema_path is None:
        return {
            "status": "not_executed",
            "executed": False,
            "schema_source": None,
            "errors": [],
            "reason": "official_spdx_json_schema_not_provided",
        }
    if not schema_path.is_file():
        return {
            "status": "failed",
            "executed": False,
            "schema_source": str(schema_path.name),
            "errors": [f"Archivo de JSON Schema oficial SPDX inexistente: {schema_path}"],
            "reason": "official_spdx_json_schema_missing",
        }
    if not sbom_path.is_file():
        return {
            "status": "failed",
            "executed": False,
            "schema_source": str(schema_path.name),
            "errors": [f"SBOM SPDX inexistente: {sbom_path}"],
            "reason": "sbom_missing",
        }
    try:
        import jsonschema
    except ImportError:
        return {
            "status": "failed",
            "executed": False,
            "schema_source": str(schema_path.name),
            "errors": ["Paquete 'jsonschema' no disponible para validar el JSON Schema oficial SPDX"],
            "reason": "jsonschema_not_installed",
        }

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    sbom_data = json.loads(sbom_path.read_text(encoding="utf-8"))
    validator_cls = jsonschema.validators.validator_for(schema)
    validator = validator_cls(schema)
    errores = [
        f"{list(e.path)}: {e.message}" for e in sorted(validator.iter_errors(sbom_data), key=lambda x: str(x.path))
    ]
    return {
        "status": "passed" if not errores else "failed",
        "executed": True,
        "schema_source": schema_path.name,
        "errors": errores[:50],
        "reason": None if not errores else "schema_validation_errors",
    }


def _normalizar_validacion_spdx_oficial(spdx_official_validation: Mapping[str, Any] | None) -> dict[str, Any]:
    if spdx_official_validation is None:
        return {
            "status": "not_executed",
            "executed": False,
            "schema_source": None,
            "errors": [],
            "reason": "official_spdx_json_schema_not_provided",
        }
    errores = [str(e) for e in spdx_official_validation.get("errors", [])]
    status = str(spdx_official_validation.get("status", "not_executed"))
    return {
        "status": status,
        "executed": bool(spdx_official_validation.get("executed", False)),
        "schema_source": spdx_official_validation.get("schema_source"),
        "errors": errores,
        "reason": spdx_official_validation.get("reason"),
    }


def evaluar_veredicto_tecnico(
    reporte: Mapping[str, Any],
    lock_runtime: Mapping[str, EntradaLock],
    lock_build: Mapping[str, EntradaLock],
    *,
    smoke_passed: bool,
    entorno_errores: Sequence[str],
    spdx_internal_errors: Sequence[str] | None = None,
    spdx_official_validation: Mapping[str, Any] | None = None,
    source_pre: Mapping[str, Any] | None = None,
    source_post: Mapping[str, Any] | None = None,
    allow_dirty: bool = False,
    extra_technical_blockers: Sequence[str] = (),
) -> dict[str, Any]:
    """Evalúa validez técnica independiente preservando bloqueos jurídicos conocidos (B07/B08/B14/B18/B19)."""
    legal_blockers: list[str] = []
    technical_blockers: list[str] = [str(b) for b in extra_technical_blockers]

    spdx_declared_counts: list[int | None] = []
    for b in reporte.get("blockers", []):
        b_str = str(b)
        if b_str.startswith(PREFIJOS_BLOQUEOS_JURIDICOS_CONOCIDOS):
            legal_blockers.append(b_str)
        elif b_str.startswith("spdx_validation_errors="):
            raw_val = b_str.split("=", 1)[1].strip()
            try:
                spdx_declared_counts.append(int(raw_val))
            except ValueError:
                spdx_declared_counts.append(None)
        else:
            technical_blockers.append(b_str)

    # B08: detectar si alguna dependencia exclusiva de build terminó redistribuida dentro del EXE
    solo_build_set = set(lock_build) - set(lock_runtime)
    redist = {_normalizar_pep503(p) for p in reporte.get("packages_summary", {}).get("redistributed", [])}
    build_en_exe = sorted(redist & solo_build_set)
    if build_en_exe:
        technical_blockers.append(f"build_deps_redistributed_in_exe={','.join(build_en_exe)}")

    # B19/B23: separar validación SPDX interna vs JSON Schema oficial y rechazar contadores inconsistentes
    internal_errs = (
        [str(e) for e in reporte.get("spdx_validation_errors", [])]
        if spdx_internal_errors is None
        else [str(e) for e in spdx_internal_errors]
    )
    spdx_internal_inconsistent = False
    for cnt in spdx_declared_counts:
        if cnt is None or cnt < 0 or (cnt > 0 and len(internal_errs) != cnt):
            spdx_internal_inconsistent = True
    if spdx_internal_inconsistent:
        technical_blockers.append("spdx_internal_validation_inconsistent=1")
    official_val = _normalizar_validacion_spdx_oficial(spdx_official_validation)
    if internal_errs:
        technical_blockers.append(f"spdx_internal_validation_errors={len(internal_errs)}")
    if official_val["status"] == "failed" or official_val["errors"]:
        technical_blockers.append(f"spdx_official_schema_errors={max(1, len(official_val['errors']))}")

    # B18: vincular estado del árbol fuente pre/post build y distinguir local_dev vs certificable
    source_mutated = False
    source_dirty = False
    if source_pre is not None or source_post is not None:
        if source_pre is None or source_post is None:
            technical_blockers.append("source_identity_incomplete=1")
        else:
            if source_pre != source_post:
                source_mutated = True
                technical_blockers.append("source_mutated_during_build=1")
            if (
                not source_pre.get("source_tree_clean", False)
                or not source_post.get("source_tree_clean", False)
                or source_pre.get("commit") in (None, "", "UNKNOWN")
            ):
                source_dirty = True
                technical_blockers.append("source_tree_dirty=1")

    if entorno_errores:
        technical_blockers.append(f"build_env_validation_errors={len(entorno_errores)}")
    if not smoke_passed:
        technical_blockers.append("smoke_frozen_executable_failed=1")
    if not reporte.get("structural_inventory_complete", False):
        technical_blockers.append("structural_inventory_incomplete=1")
    if not reporte.get("provenance_reconciliation_verified", False):
        technical_blockers.append("provenance_reconciliation_unverified=1")
    if reporte.get("cpython_version_uncertainty", True):
        technical_blockers.append("cpython_version_unverified=1")
    if reporte.get("lock_drift"):
        technical_blockers.append(f"lock_drift={len(reporte['lock_drift'])}")

    blockers_unicos = sorted(set(technical_blockers))
    technical_valid = len(blockers_unicos) == 0
    build_mode = "local_dev" if allow_dirty else "controlled_certifiable"
    certifiable = technical_valid and not allow_dirty and not source_dirty and not source_mutated
    local_dev_passed = bool(allow_dirty and not source_mutated and blockers_unicos in ([], ["source_tree_dirty=1"]))

    return {
        "technical_build_valid": technical_valid,
        "certifiable_controlled_build": certifiable,
        "build_mode": build_mode,
        "local_dev_build_passed": local_dev_passed,
        "source_mutated_during_build": source_mutated,
        "legal_gate_passed": bool(reporte.get("gate_passed", False)),
        "legal_certification": False,
        "technical_blockers": blockers_unicos,
        "legal_blockers": sorted(set(legal_blockers)),
        "build_deps_redistributed_in_exe": build_en_exe,
        "spdx_internal_validation": {
            "executed": True,
            "passed": len(internal_errs) == 0 and not spdx_internal_inconsistent,
            "errors": internal_errs,
            "inconsistent_blocker_count": spdx_internal_inconsistent,
            "scope": str(
                reporte.get(
                    "spdx_validation_scope",
                    "internal_structural_and_semantic_validator_not_full_jsonschema",
                )
            ),
        },
        "spdx_official_schema_validation": official_val,
    }


def _entorno_limpio_base() -> dict[str, str]:
    """Construye un entorno saneado sin ``PIP_*`` ni ``VIRTUAL_ENV`` para crear el venv (B09)."""
    vars_prohibidas = frozenset({"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV"})
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PIP_") and k.upper() not in vars_prohibidas}
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _entorno_pip_controlado(build_venv: Path) -> dict[str, str]:
    """Construye un entorno aislado para ``pip`` y PyInstaller eliminando cualquier ``PIP_*`` heredada (B09)."""
    env = _entorno_limpio_base()
    scripts_dir = build_venv / ("Scripts" if os.name == "nt" else "bin")
    env["PATH"] = str(scripts_dir) + os.pathsep + env.get("PATH", "")
    env["VIRTUAL_ENV"] = str(build_venv)
    env["PIP_CONFIG_FILE"] = os.devnull
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
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


def _digest_arbol_codigo(raiz: Path) -> str:
    """Calcula un SHA-256 determinístico de los archivos fuente de ``sky_claw/`` excluyendo cachés (B18)."""
    resumen = hashlib.sha256()
    pkg_dir = raiz / "sky_claw"
    if not pkg_dir.is_dir():
        return "ABSENT"
    for ruta in sorted(pkg_dir.rglob("*")):
        if not ruta.is_file():
            continue
        if "__pycache__" in ruta.parts or ruta.suffix.casefold() in {".pyc", ".pyo"}:
            continue
        rel = ruta.relative_to(raiz).as_posix()
        resumen.update(rel.encode("utf-8"))
        resumen.update(b"\0")
        resumen.update(_sha256_archivo(ruta)[0].encode("ascii"))
        resumen.update(b"\n")
    return resumen.hexdigest()


def capturar_identidad_fuente(
    repo_root: Path,
    *,
    lock_runtime_path: Path,
    lock_build_path: Path,
) -> dict[str, Any]:
    """Captura estado git y digests SHA-256 de los inputs efectivos que afectan la compilación (B18)."""
    try:
        sha = subprocess.check_output(["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True).strip()
        tree = subprocess.check_output(["git", "-C", str(repo_root), "rev-parse", "HEAD^{tree}"], text=True).strip()
        status = subprocess.check_output(
            ["git", "-C", str(repo_root), "status", "--porcelain=v1"], text=True
        ).splitlines()
        clean = len(status) == 0
        dirty_count = len(status)
    except (OSError, subprocess.SubprocessError):
        sha = "UNKNOWN"
        tree = "UNKNOWN"
        clean = False
        dirty_count = -1

    inputs_sha256: dict[str, str] = {}
    for etiqueta, ruta in (
        ("sky_claw.spec", repo_root / "sky_claw.spec"),
        ("pyproject.toml", repo_root / "pyproject.toml"),
        (lock_build_path.name, lock_build_path),
        (lock_runtime_path.name, lock_runtime_path),
    ):
        inputs_sha256[etiqueta] = _sha256_archivo(ruta)[0] if ruta.is_file() else "ABSENT"
    inputs_sha256["sky_claw_tree"] = _digest_arbol_codigo(repo_root)

    return {
        "commit": sha,
        "tree": tree,
        "source_tree_clean": clean,
        "dirty_entries_count": dirty_count,
        "inputs_sha256": inputs_sha256,
    }


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


def _preparar_venv_controlado(
    *,
    build_venv: Path,
    repo_root: Path,
    perfil: str,
    lock_runtime_path: Path,
    lock_build_path: Path,
    lock_runtime: Mapping[str, EntradaLock],
    lock_build: Mapping[str, EntradaLock],
    py_host: Path,
) -> tuple[Path, str, dict[str, str], list[dict[str, Any]], dict[str, str]]:
    """Crea, instala y valida el venv aislado compartido por build local y CI (B01/B02/B09/B16)."""
    _verificar_sin_reparse_en_ancestros(build_venv, etiqueta="build_venv")
    _ejecutar_cmd(
        [str(py_host), "-m", "venv", str(build_venv)],
        cwd=repo_root,
        env=_entorno_limpio_base(),
        etiqueta="venv",
    )
    env_ctrl = _entorno_pip_controlado(build_venv)
    py_build = build_venv / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")

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
    # Nota: se usa `-e .` con `--no-deps --no-build-isolation` porque hatchling 1.33 rechaza
    # duplicados en `force-include` de pyproject.toml al armar wheel estándar (fuera del write-set).
    _ejecutar_cmd(
        [str(py_build), "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e", "."],
        cwd=repo_root,
        env=env_ctrl,
        etiqueta="pip_install_project_no_isolation",
    )
    _ejecutar_cmd([str(py_build), "-m", "pip", "check"], cwd=repo_root, env=env_ctrl, etiqueta="pip_check")

    py_ver_build, mapa_inst = _obtener_mapa_instalados(py_build, repo_root, env_ctrl)
    errores_entorno = validar_entorno_build(mapa_inst, lock_runtime, lock_build, perfil=perfil)
    if errores_entorno:
        raise BuildControladoError("Validación de entorno de build falló: " + "; ".join(errores_entorno))

    _, paquetes_env = _recolectar_entorno_desde_interprete(py_build, repo_root, env_ctrl)
    return py_build, py_ver_build, mapa_inst, paquetes_env, env_ctrl


def _promover_evidencia_staging(staging_dir: Path, out_dir: Path) -> list[Path]:
    """Copia atómicamente archivo por archivo desde staging hacia ``out_dir`` y retorna los creados (B17/B21)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    creados: list[Path] = []
    for src in sorted(staging_dir.iterdir()):
        if not src.is_file():
            continue
        destino = out_dir / src.name
        tmp_dest = out_dir / f".{src.name}.{os.getpid()}.tmp"
        try:
            shutil.copy2(src, tmp_dest)
            os.replace(tmp_dest, destino)
            creados.append(destino)
        except Exception:
            tmp_dest.unlink(missing_ok=True)
            for previo in creados:
                previo.unlink(missing_ok=True)
            raise
    return creados


def promover_evidencia_y_ejecutable(
    *,
    staging_ev: Path,
    out_dir: Path,
    exe_staging: Path,
    dist_dir: Path,
    promover_exe: bool,
) -> bool:
    """Promueve TODA la evidencia antes de sustituir ``SkyClawApp.exe``, con rollback ante fallo (B21).

    Nota: la sustitución conjunta de múltiples archivos (evidencias + ejecutable) en NTFS no es
    una transacción atómica del sistema de archivos; se ejecuta de forma secuencial (evidencia primero,
    ejecutable después) con rollback best-effort si falla la sustitución final del ejecutable.
    """
    out_existia = out_dir.exists()
    creados = _promover_evidencia_staging(staging_ev, out_dir)
    if not promover_exe:
        return False

    dist_dir.mkdir(parents=True, exist_ok=True)
    exe_final = dist_dir / "SkyClawApp.exe"
    tmp_dest = dist_dir / f".SkyClawApp.exe.{os.getpid()}.tmp"
    try:
        shutil.copy2(exe_staging, tmp_dest)
        os.replace(tmp_dest, exe_final)
        return True
    except Exception:
        tmp_dest.unlink(missing_ok=True)
        for p in creados:
            p.unlink(missing_ok=True)
        if not out_existia and out_dir.is_dir():
            with contextlib.suppress(OSError):
                out_dir.rmdir()
        raise


def ejecutar_build_controlado(
    *,
    repo_root: Path,
    perfil: str,
    dist_dir: Path,
    out_dir: Path | None = None,
    build_dir_opcional: Path | None = None,
    python_base: Path | None = None,
    allow_dirty: bool = False,
    spdx_schema_path: Path | None = None,
) -> tuple[dict[str, Any], int]:
    """Ejecuta el pipeline completo de build controlado en Windows aislado (B01–B23)."""
    if perfil not in _PERFIL_A_LOCK:
        raise BuildControladoError(f"Perfil inválido: {perfil!r}. Opciones: {sorted(_PERFIL_A_LOCK)}")

    out_dir_efectivo = out_dir if out_dir is not None else resolver_out_dir_exclusivo(dist_dir, perfil=perfil)

    # B17: validar TODOS los destinos ANTES de crear directorios, escribir o tocar archivos.
    dist_res, out_res, build_res = validar_destinos_build(
        dist_dir=dist_dir,
        out_dir=out_dir_efectivo,
        build_dir_opcional=build_dir_opcional,
        repo_root=repo_root,
    )

    lock_runtime_path = repo_root / _PERFIL_A_LOCK[perfil]
    lock_build_path = repo_root / "requirements-build.lock"
    lock_runtime = parsear_lockfile_con_hashes(lock_runtime_path)
    lock_build = parsear_lockfile_con_hashes(lock_build_path)
    verificar_compatibilidad_locks(lock_runtime, lock_build)

    # B18: capturar identidad del árbol fuente e inputs efectivos ANTES de compilar
    source_pre = capturar_identidad_fuente(
        repo_root, lock_runtime_path=lock_runtime_path, lock_build_path=lock_build_path
    )

    creado_por_esta_ejecucion = False
    if build_res is not None:
        build_root = build_res
        build_root.mkdir(parents=True, exist_ok=False)
        creado_por_esta_ejecucion = True
    else:
        build_root = Path(tempfile.mkdtemp(prefix=f"skyclaw-build-{perfil}-"))
        creado_por_esta_ejecucion = True

    build_venv = build_root / "venv"
    staging_dist = build_root / "dist"
    staging_work = build_root / "work"
    staging_ev = build_root / "evidence_staging"
    staging_ev.mkdir(parents=True, exist_ok=True)
    py_host = python_base or Path(sys.executable)

    try:
        # 1–5. Preparar, instalar y validar venv exclusivo mediante función compartida con CI (B01/B02/B05/B09/B16)
        py_build, py_ver_build, mapa_inst, paquetes_env, env_ctrl = _preparar_venv_controlado(
            build_venv=build_venv,
            repo_root=repo_root,
            perfil=perfil,
            lock_runtime_path=lock_runtime_path,
            lock_build_path=lock_build_path,
            lock_runtime=lock_runtime,
            lock_build=lock_build,
            py_host=py_host,
        )
        errores_entorno: list[str] = []

        env_json_path = staging_ev / "build_env.json"
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
        toc_out_path = staging_ev / "analysis_toc.json"
        _escribir_json_atomico(toc_saneado, toc_out_path)

        # 7. Registrar SHA-256 del EXE en staging
        reg_sha_path = staging_ev / "exe_sha256.json"
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
        inv_path = staging_ev / "inventory.json"
        sbom_path = staging_ev / "sbom.spdx.json"
        rep_path = staging_ev / "reconciliation_report.json"
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
        if rec_proc.returncode not in (0, 1) or not rep_path.is_file() or not sbom_path.is_file():
            raise BuildControladoError(f"Error en reconciliar_sbom.py (rc={rec_proc.returncode}): {rec_proc.stderr}")

        # B18: capturar identidad del árbol fuente DESPUÉS de compilar y reconciliar
        source_post = capturar_identidad_fuente(
            repo_root, lock_runtime_path=lock_runtime_path, lock_build_path=lock_build_path
        )

        # B19: validar SPDX oficial solo si se proveyó --spdx-schema (sin confundir con validador interno)
        spdx_official = validar_spdx_contra_schema_oficial(sbom_path, spdx_schema_path)

        reporte = json.loads(rep_path.read_text(encoding="utf-8"))
        veredicto = evaluar_veredicto_tecnico(
            reporte,
            lock_runtime,
            lock_build,
            smoke_passed=smoke_passed,
            entorno_errores=errores_entorno,
            spdx_internal_errors=reporte.get("spdx_validation_errors", []),
            spdx_official_validation=spdx_official,
            source_pre=source_pre,
            source_post=source_post,
            allow_dirty=allow_dirty,
        )

        # 10. Preparar manifiesto completo en staging y promover evidencia ANTES de sustituir el EXE (B21)
        exe_sha, exe_bytes = _sha256_archivo(exe_staging)
        debe_promover = bool(veredicto["technical_build_valid"] or veredicto["local_dev_build_passed"])
        mapa_inst_norm = {_normalizar_pep503(k): v for k, v in mapa_inst.items()}
        pre_inputs = source_pre["inputs_sha256"]

        manifiesto: dict[str, Any] = {
            "schema_version": 2,
            "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "profile": perfil,
            "build_mode": veredicto["build_mode"],
            "git": {
                "commit": source_pre["commit"],
                "tree": source_pre["tree"],
                "source_tree_clean": bool(
                    source_pre["source_tree_clean"]
                    and source_post["source_tree_clean"]
                    and not veredicto["source_mutated_during_build"]
                ),
                "dirty_entries_count": max(source_pre["dirty_entries_count"], source_post["dirty_entries_count"]),
            },
            "source_identity": {
                "pre_build": source_pre,
                "post_build": source_post,
                "mutated_during_build": veredicto["source_mutated_during_build"],
                "certifiable_controlled_build": veredicto["certifiable_controlled_build"],
            },
            "bootstrap": {
                "python_version": py_ver_build,
                "platform": platform.platform(),
                "machine": platform.machine(),
                "pip_version": mapa_inst_norm.get("pip", "UNKNOWN"),
                "build_tools": {k: mapa_inst_norm.get(_normalizar_pep503(k), "ABSENT") for k in sorted(lock_build)},
                "non_pinned_os_components": ["windows_os_version", "ucrt_system_dlls", "pyinstaller_zip_timestamps"],
            },
            "lockfiles": {
                "runtime": {"path": lock_runtime_path.name, "sha256": pre_inputs[lock_runtime_path.name]},
                "build": {"path": lock_build_path.name, "sha256": pre_inputs[lock_build_path.name]},
            },
            "artifacts": {
                "exe": {
                    "name": "SkyClawApp.exe",
                    "sha256": exe_sha,
                    "bytes": exe_bytes,
                    "promoted_to_dist": debe_promover,
                },
                "exe_sha256_json": {"path": reg_sha_path.name, "sha256": _sha256_archivo(reg_sha_path)[0]},
                "analysis_toc_json": {"path": toc_out_path.name, "sha256": _sha256_archivo(toc_out_path)[0]},
                "build_env_json": {"path": env_json_path.name, "sha256": _sha256_archivo(env_json_path)[0]},
                "inventory_json": {"path": inv_path.name, "sha256": _sha256_archivo(inv_path)[0]},
                "sbom_spdx_json": {"path": sbom_path.name, "sha256": _sha256_archivo(sbom_path)[0]},
                "reconciliation_report_json": {"path": rep_path.name, "sha256": _sha256_archivo(rep_path)[0]},
            },
            "smoke_passed": smoke_passed,
            "spdx_internal_validation": veredicto["spdx_internal_validation"],
            "spdx_official_schema_validation": veredicto["spdx_official_schema_validation"],
            "verdict": veredicto,
        }
        _escribir_json_atomico(manifiesto, staging_ev / "build_manifest.json")
        promover_evidencia_y_ejecutable(
            staging_ev=staging_ev,
            out_dir=out_res,
            exe_staging=exe_staging,
            dist_dir=dist_res,
            promover_exe=debe_promover,
        )
        ok_exit = veredicto["technical_build_valid"] or veredicto["local_dev_build_passed"]
        return manifiesto, (EXIT_OK if ok_exit else EXIT_FAIL_CLOSED)
    finally:
        if creado_por_esta_ejecucion and build_root.exists():
            shutil.rmtree(build_root, ignore_errors=True)


def _cargar_identidad_fuente_previa(
    source_pre_path: Path | None,
    *,
    perfil: str,
    run_id: str | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Valida la captura previa de identidad fuente y su vinculación a la misma ejecución (B18)."""
    if source_pre_path is None or not source_pre_path.is_file():
        return None, ["source_pre_identity_missing=1"]
    try:
        payload = json.loads(source_pre_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, ["source_pre_identity_invalid_json=1"]

    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        return None, ["source_pre_identity_invalid_schema=1"]

    blockers: list[str] = []
    if payload.get("profile") != perfil:
        blockers.append("source_pre_profile_mismatch=1")

    expected_run_id = (run_id or "").strip()
    actual_run_id = str(payload.get("run_id") or "").strip()
    if expected_run_id:
        if actual_run_id != expected_run_id:
            blockers.append("source_pre_run_id_mismatch=1")
    elif actual_run_id:
        blockers.append("source_pre_run_id_unexpected=1")

    source_pre = payload.get("source_identity")
    if not isinstance(source_pre, Mapping) or "inputs_sha256" not in source_pre:
        blockers.append("source_pre_identity_malformed=1")
        return None, blockers

    return dict(source_pre), blockers


def _verificar_reporte_ci(
    *,
    repo_root: Path,
    perfil: str,
    report_path: Path,
    exe_path: Path | None,
    exe_sha256_json: Path | None,
    venv_path: Path | None,
    analysis_toc_path: Path | None,
    sbom_path: Path | None,
    spdx_schema_path: Path | None,
    smoke_outcome: str | None,
    source_pre_path: Path | None,
    run_id: str | None,
    allow_dirty: bool,
) -> tuple[dict[str, Any], int]:
    """Evalúa el veredicto técnico en modo CI verificando EXE real, venv, smoke e identidad pre/post (B16/B18/B19)."""
    lock_runtime_path = repo_root / _PERFIL_A_LOCK[perfil]
    lock_build_path = repo_root / "requirements-build.lock"
    lock_rt = parsear_lockfile_con_hashes(lock_runtime_path)
    lock_b = parsear_lockfile_con_hashes(lock_build_path)
    verificar_compatibilidad_locks(lock_rt, lock_b)

    if not report_path.is_file():
        raise BuildControladoError(f"Reporte de reconciliación inexistente: {report_path}")
    reporte = json.loads(report_path.read_text(encoding="utf-8"))

    extra_blockers: list[str] = []

    # B16: exigir EXE real y comprobar que el reporte pertenezca exactamente a ese ejecutable
    if exe_path is None or not exe_path.is_file():
        extra_blockers.append("exe_path_missing_for_report_verification=1")
    else:
        exe_sha_real, _ = _sha256_archivo(exe_path)
        rep_exe = reporte.get("exe", {})
        rep_sha = str(rep_exe.get("sha256", "")).strip().lower() if isinstance(rep_exe, Mapping) else ""
        if not rep_sha or rep_sha != exe_sha_real.lower():
            extra_blockers.append("exe_report_sha256_mismatch=1")
        if exe_sha256_json is not None:
            if not exe_sha256_json.is_file():
                extra_blockers.append("exe_sha256_registry_missing=1")
            else:
                reg = json.loads(exe_sha256_json.read_text(encoding="utf-8"))
                reg_sha = str(reg.get("sha256", "")).strip().lower()
                if reg_sha != exe_sha_real.lower():
                    extra_blockers.append("exe_sha256_registry_mismatch=1")

    # B16: exigir validación real del venv de build (nunca asumir entorno_errores=[])
    entorno_errores: list[str] = []
    if venv_path is None or not venv_path.is_dir():
        extra_blockers.append("build_venv_not_verified=1")
    else:
        env_ctrl = _entorno_pip_controlado(venv_path)
        py_v = venv_path / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
        if not py_v.is_file():
            extra_blockers.append("build_venv_python_missing=1")
        else:
            _, mapa = _obtener_mapa_instalados(py_v, repo_root, env_ctrl)
            entorno_errores = validar_entorno_build(mapa, lock_rt, lock_b, perfil=perfil)

    # B16: nunca asumir smoke_passed=True por defecto
    smoke_passed = (smoke_outcome or "").strip().lower() == "success"

    # B13/B16: comprobar que analysis_toc existe y está saneado si se provee
    if analysis_toc_path is not None:
        if not analysis_toc_path.is_file():
            extra_blockers.append("analysis_toc_missing=1")
        else:
            toc_txt = analysis_toc_path.read_text(encoding="utf-8")
            if re.search(r"[A-Za-z]:\\\\Users\\\\|/Users/|/home/", toc_txt):
                extra_blockers.append("analysis_toc_unsanitized_personal_path=1")

    # B18: cargar captura previa independiente y contrastar contra captura posterior independiente
    source_pre, pre_blockers = _cargar_identidad_fuente_previa(source_pre_path, perfil=perfil, run_id=run_id)
    extra_blockers.extend(pre_blockers)
    source_post = capturar_identidad_fuente(
        repo_root, lock_runtime_path=lock_runtime_path, lock_build_path=lock_build_path
    )

    # B19/B23: si se solicitó explícitamente --spdx-schema sin --sbom, fallar cerrado
    if spdx_schema_path is not None and sbom_path is None:
        spdx_official = {
            "status": "failed",
            "executed": False,
            "schema_source": spdx_schema_path.name,
            "errors": ["Se solicitó --spdx-schema sin proporcionar --sbom"],
            "reason": "sbom_path_missing_for_requested_spdx_schema",
        }
    elif sbom_path is not None:
        spdx_official = validar_spdx_contra_schema_oficial(sbom_path, spdx_schema_path)
    else:
        spdx_official = _normalizar_validacion_spdx_oficial(None)

    ver = evaluar_veredicto_tecnico(
        reporte,
        lock_rt,
        lock_b,
        smoke_passed=smoke_passed,
        entorno_errores=entorno_errores,
        spdx_internal_errors=reporte.get("spdx_validation_errors", []),
        spdx_official_validation=spdx_official,
        source_pre=source_pre,
        source_post=source_post,
        allow_dirty=allow_dirty,
        extra_technical_blockers=extra_blockers,
    )
    return ver, (EXIT_OK if ver["technical_build_valid"] else EXIT_FAIL_CLOSED)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build controlado, aislado y trazable de SkyClawApp.exe (PR-LIC-03B).")
    parser.add_argument(
        "--profile", choices=sorted(_PERFIL_A_LOCK), default="base", help="Perfil de build (default: base)."
    )
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"), help="Directorio destino para SkyClawApp.exe.")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Directorio de evidencias del build (default: subdirectorio exclusivo bajo dist/build-evidence/).",
    )
    parser.add_argument(
        "--build-dir", type=Path, default=None, help="Directorio temporal opcional (debe ser nuevo y seguro)."
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Modo local de desarrollo: permite compilar con árbol sucio marcando certifiable_controlled_build=False (B18).",
    )
    parser.add_argument(
        "--spdx-schema",
        type=Path,
        default=None,
        help="Ruta opcional al JSON Schema oficial de SPDX 2.3 para ejecutar validación oficial (B19).",
    )
    parser.add_argument(
        "--prepare-venv",
        type=Path,
        default=None,
        help="Modo CI: crea, instala con hashes y valida el venv aislado usando el mismo contrato que build local (B16).",
    )
    parser.add_argument(
        "--validate-venv",
        type=Path,
        default=None,
        help="Modo CI: valida un venv ya instalado contra los locks y exporta --env-out sin reconstruir.",
    )
    parser.add_argument(
        "--env-out",
        type=Path,
        default=None,
        help="Ruta de salida para lista PaqueteEntorno en modo --prepare-venv/--validate-venv.",
    )
    parser.add_argument(
        "--verify-report",
        type=Path,
        default=None,
        help="Modo CI: evalúa el veredicto técnico independiente verificando EXE real, venv y smoke (B16/B18/B19).",
    )
    parser.add_argument(
        "--exe",
        type=Path,
        default=None,
        help="Ruta al ejecutable SkyClawApp.exe real a verificar en modo --verify-report (B16).",
    )
    parser.add_argument(
        "--exe-sha256-json",
        type=Path,
        default=None,
        help="Ruta al registro exe_sha256.json para cotejar en modo --verify-report (B16).",
    )
    parser.add_argument(
        "--analysis-toc",
        type=Path,
        default=None,
        help="Ruta al analysis_toc.json saneado para verificar en modo --verify-report (B16).",
    )
    parser.add_argument(
        "--sbom",
        type=Path,
        default=None,
        help="Ruta al sbom.spdx.json para validación oficial opcional con --spdx-schema en --verify-report (B19).",
    )
    parser.add_argument(
        "--smoke-outcome",
        type=str,
        default=None,
        help="Resultado explícito del smoke test ('success' para aprobado) en modo --verify-report (B16).",
    )
    parser.add_argument(
        "--source-pre-out",
        type=Path,
        default=None,
        help="Modo CI (--prepare-venv): guarda captura previa de identidad fuente vinculada a --run-id (B18).",
    )
    parser.add_argument(
        "--source-pre",
        type=Path,
        default=None,
        help="Modo CI (--verify-report): ruta al JSON de identidad fuente capturado antes de compilar (B18).",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Identificador de ejecución para vincular --source-pre-out con --source-pre (B18).",
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

        if args.prepare_venv is not None:
            lock_rt_path = repo_root / _PERFIL_A_LOCK[args.profile]
            lock_b_path = repo_root / "requirements-build.lock"
            lock_rt = parsear_lockfile_con_hashes(lock_rt_path)
            lock_b = parsear_lockfile_con_hashes(lock_b_path)
            verificar_compatibilidad_locks(lock_rt, lock_b)
            # B22: normalizar ruta absoluta antes de cambiar cwd y reservar directorio nuevo y exclusivo
            venv_abs = Path(os.path.abspath(str(args.prepare_venv)))
            venv_seguro = validar_directorio_build_seguro(venv_abs, repo_root)
            venv_seguro.mkdir(parents=True, exist_ok=False)
            # B18: capturar identidad fuente ANTES de preparar el entorno y compilar
            source_pre = capturar_identidad_fuente(
                repo_root, lock_runtime_path=lock_rt_path, lock_build_path=lock_b_path
            )
            try:
                _, py_ver, mapa, pkgs, _ = _preparar_venv_controlado(
                    build_venv=venv_seguro,
                    repo_root=repo_root,
                    perfil=args.profile,
                    lock_runtime_path=lock_rt_path,
                    lock_build_path=lock_b_path,
                    lock_runtime=lock_rt,
                    lock_build=lock_b,
                    py_host=Path(sys.executable),
                )
            except Exception:
                shutil.rmtree(venv_seguro, ignore_errors=True)
                raise
            if args.env_out is not None:
                _verificar_sin_reparse_en_ancestros(args.env_out, etiqueta="env_out")
                args.env_out.parent.mkdir(parents=True, exist_ok=True)
                args.env_out.write_text(json.dumps(pkgs, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            if args.source_pre_out is not None:
                _verificar_sin_reparse_en_ancestros(args.source_pre_out, etiqueta="source_pre_out")
                _escribir_json_atomico(
                    {
                        "schema_version": 1,
                        "profile": args.profile,
                        "run_id": (args.run_id or "").strip() or None,
                        "captured_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "source_identity": source_pre,
                    },
                    args.source_pre_out,
                )
            print(
                f"build_controlado: venv preparado y validado profile={args.profile} python={py_ver} pkgs={len(mapa)}"
            )
            return EXIT_OK

        if args.verify_report is not None:
            ver, rc = _verificar_reporte_ci(
                repo_root=repo_root,
                perfil=args.profile,
                report_path=args.verify_report,
                exe_path=args.exe,
                exe_sha256_json=args.exe_sha256_json,
                venv_path=args.validate_venv,
                analysis_toc_path=args.analysis_toc,
                sbom_path=args.sbom,
                spdx_schema_path=args.spdx_schema,
                smoke_outcome=args.smoke_outcome,
                source_pre_path=args.source_pre,
                run_id=args.run_id,
                allow_dirty=args.allow_dirty,
            )
            print(
                f"build_controlado: technical_build_valid={ver['technical_build_valid']} "
                f"certifiable={ver['certifiable_controlled_build']} "
                f"spdx_official={ver['spdx_official_schema_validation']['status']} "
                f"technical_blockers={ver['technical_blockers']} legal_blockers={ver['legal_blockers']}"
            )
            return rc

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

        manifiesto, codigo = ejecutar_build_controlado(
            repo_root=repo_root,
            perfil=args.profile,
            dist_dir=args.dist_dir,
            out_dir=args.out_dir,
            build_dir_opcional=args.build_dir,
            allow_dirty=args.allow_dirty,
            spdx_schema_path=args.spdx_schema,
        )
        ver = manifiesto["verdict"]
        print(
            f"build_controlado: profile={args.profile} build_mode={ver['build_mode']} "
            f"technical_build_valid={ver['technical_build_valid']} "
            f"certifiable={ver['certifiable_controlled_build']} "
            f"spdx_official={ver['spdx_official_schema_validation']['status']} "
            f"exe_sha256={manifiesto['artifacts']['exe']['sha256']} legal_blockers={len(ver['legal_blockers'])}"
        )
        return codigo
    except (BuildControladoError, OSError, ValueError) as exc:
        print(f"build_controlado: ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
