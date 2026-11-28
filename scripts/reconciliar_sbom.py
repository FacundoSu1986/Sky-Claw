"""Generador de SBOM SPDX 2.3 y reconciliador fail-closed contra inventario real del EXE (PR-LIC-03A).

Cumple SBOM-03 a SBOM-08 y correcciones adversariales R1 (F01–F06):
- F01: Validación estricta del esquema del inventario (``entries`` no vacío, ``entry_count``
  coherente, sin duplicados, presencia de componentes estructurales obligatorios y SHA-256 real).
- F02: Atribución con evidencia del entorno exacto de build (``RECORD`` + hashes SHA-256 para
  binarios/datos sin exigir igualdad sobre bytecode ``.pyc``), verificación de compatibilidad
  de versión CPython entre EXE y entorno de análisis, y reconciliación real contra ``analysis_toc``.
- F03: Emisión separada en SPDX 2.3 de CPython, OpenSSL, SQLite, libffi, Tcl/Tk, Microsoft
  runtime, fuentes por familia, recursos vendorizados de frontend y recursos pendientes de
  revisión jurídica (sin ocultar componentes detrás del contenedor).
- F04: Separación estricta entre ``licenseDeclared`` (normalizado a SPDX válido, nunca prosa
  ambigua como ``BSD License``) y ``licenseConcluded`` (``NOASSERTION`` hasta revisión documental).
- F05: Validación estructural y semántica interna SPDX 2.3 (identificadores, unicidad,
  relaciones sin huérfanos, expresiones de licencia, checksums y campos obligatorios),
  explicitando su alcance frente a validación contra el JSON Schema oficial.
- F06: Escritura atómica y limpieza de salidas PASS obsoletas ante error de entrada CLI.
"""

from __future__ import annotations

import argparse
import base64
import importlib.metadata as importlib_metadata
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_RAIZ_REPO = Path(__file__).resolve().parents[1]
if str(_RAIZ_REPO) not in sys.path:
    sys.path.insert(0, str(_RAIZ_REPO))

from scripts.inventario_ejecutable import (  # noqa: E402
    PREFIJOS_DATAS_ESPERADOS_SPEC,
    CategoriaComponente,
    EstadoDerechosRedistribucion,
    OrigenTecnico,
)

EXIT_OK = 0
EXIT_FAIL_CLOSED = 1
EXIT_ERROR = 2

_SPDXID_RE = re.compile(r"^SPDXRef-[A-Za-z0-9.-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_REQ_PIN_RE = re.compile(r"^([A-Za-z0-9_.\-]+)==([^\s;\\]+)")
_SPDX_TOKEN_RE = re.compile(r"^(?:[A-Za-z0-9.\-+]+\+?|LicenseRef-[A-Za-z0-9.-]+)$")
_SPDX_OPERADORES = frozenset({"AND", "OR", "WITH"})
_SPDX_CREATOR_PREFIJOS = ("Tool:", "Person:", "Organization:")
_SPDX_RELACIONES_VALIDAS = frozenset(
    {
        "DESCRIBES",
        "DESCRIBED_BY",
        "CONTAINS",
        "CONTAINED_BY",
        "DEPENDS_ON",
        "DEPENDENCY_OF",
        "BUILD_TOOL_OF",
        "GENERATED_FROM",
    }
)

#: Mapeo unívoco de textos/clasificadores legacy a identificadores SPDX 2.3 válidos (F04 / Codex #2).
#: Las variantes ambiguas (p. ej. "BSD License" sin cláusula) se excluyen deliberadamente.
_MAPA_SPDX_UNIVOCO: dict[str, str] = {
    "mit": "MIT",
    "mit license": "MIT",
    "apache-2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "apache software license 2.0": "Apache-2.0",
    "bsd-3-clause": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd 3-clause license": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd 2-clause": "BSD-2-Clause",
    "bsd 2-clause license": "BSD-2-Clause",
    "2-clause bsd license": "BSD-2-Clause",
    "isc": "ISC",
    "isc license": "ISC",
    "isc license (iscl)": "ISC",
    "psf-2.0": "PSF-2.0",
    "python software foundation license": "PSF-2.0",
    "mpl-2.0": "MPL-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "lgpl-3.0-only": "LGPL-3.0-only",
    "gnu lesser general public license v3 (lgplv3)": "LGPL-3.0-only",
    "lgpl-3.0-or-later": "LGPL-3.0-or-later",
    "gnu lesser general public license v3 or later (lgplv3+)": "LGPL-3.0-or-later",
    "gpl-3.0-only": "GPL-3.0-only",
    "gnu general public license v3 (gplv3)": "GPL-3.0-only",
    "gpl-3.0-or-later": "GPL-3.0-or-later",
    "gnu general public license v3 or later (gplv3+)": "GPL-3.0-or-later",
    "gpl-2.0-only": "GPL-2.0-only",
    "gnu general public license v2 (gplv2)": "GPL-2.0-only",
    "gpl-2.0-or-later": "GPL-2.0-or-later",
    "gnu general public license v2 or later (gplv2+)": "GPL-2.0-or-later",
    "unlicense": "Unlicense",
    "the unlicense (unlicense)": "Unlicense",
    "cc0-1.0": "CC0-1.0",
    "cc0 1.0 universal (cc0 1.0) public domain dedication": "CC0-1.0",
    "zlib": "Zlib",
    "zlib/libpng license": "Zlib",
    "hpnd": "HPND",
    "historical permission notice and disclaimer (hpnd)": "HPND",
    "0bsd": "0BSD",
    "bsd zero clause license": "0BSD",
    "ofl-1.1": "OFL-1.1",
    "sil open font license 1.1": "OFL-1.1",
}

#: Paquetes que participan únicamente en desarrollo/build/test (SBOM-03).
PAQUETES_SOLO_BUILD_POR_DEFECTO: frozenset[str] = frozenset(
    {
        "pyinstaller",
        "pyinstaller-hooks-contrib",
        "altgraph",
        "pefile-build-only",
        "hatchling",
        "hatch-vcs",
        "setuptools",
        "wheel",
        "pip",
        "pytest",
        "pytest-asyncio",
        "pytest-aiohttp",
        "pytest-cov",
        "pytest-timeout",
        "pytest-bdd",
        "ruff",
        "mypy",
        "pre-commit",
        "bandit",
        "pip-audit",
        "coverage",
        "iniconfig",
        "pluggy",
    }
)


def _normalizar_pep503(nombre: str) -> str:
    return re.sub(r"[-_.]+", "-", nombre).lower()


def _sanear_spdx_id(texto: str) -> str:
    limpio = re.sub(r"[^A-Za-z0-9.-]", "-", texto)
    return f"SPDXRef-{limpio}"


def _es_expresion_spdx_valida(expr: str) -> bool:
    """Valida sintácticamente una expresión de licencia SPDX 2.3 (o NOASSERTION/NONE) (F04/F05)."""
    s = expr.strip()
    if not s:
        return False
    if s in {"NOASSERTION", "NONE"}:
        return True
    # Rechazar prosa con signos o palabras separadas sin operador booleano (p. ej. "BSD License")
    tokens = re.findall(r"\(|\)|[^\s()]+", s)
    if not tokens:
        return False
    prof = 0
    espera_operando = True
    for tok in tokens:
        if tok == "(":
            if not espera_operando:
                return False
            prof += 1
        elif tok == ")":
            if espera_operando or prof <= 0:
                return False
            prof -= 1
        elif tok in _SPDX_OPERADORES:
            if espera_operando:
                return False
            espera_operando = True
        else:
            if not espera_operando:
                return False
            if _SPDX_TOKEN_RE.fullmatch(tok) is None:
                return False
            espera_operando = False
    return prof == 0 and not espera_operando


def _normalizar_licencia_metadata(expr: str, lic: str, classifiers: Sequence[str]) -> tuple[str, str, str]:
    """Devuelve ``(licenseDeclared_SPDX, evidencia, confianza)`` sin inventar ni emitir prosa (F04)."""
    if expr.strip():
        cand = expr.strip()
        if _es_expresion_spdx_valida(cand):
            return cand, "METADATA:License-Expression", "ALTA"
        return "NOASSERTION", f"METADATA:License-Expression no-SPDX ({cand[:80]})", "BAJA"

    primera = lic.splitlines()[0].strip() if lic.strip() else ""
    if primera and primera.upper() not in {"UNKNOWN", "NONE", "N/A"}:
        mapeada = _MAPA_SPDX_UNIVOCO.get(primera.casefold())
        if mapeada is not None:
            return mapeada, f"METADATA:License ({primera})", "MEDIA"
        if len(primera) <= 100 and _es_expresion_spdx_valida(primera):
            return primera, "METADATA:License", "MEDIA"

    lic_cls = [c.split("::")[-1].strip() for c in classifiers if c.startswith("License ::")]
    if lic_cls:
        mapeadas: list[str] = []
        no_mapeadas: list[str] = []
        for item in lic_cls:
            m = _MAPA_SPDX_UNIVOCO.get(item.casefold())
            if m is not None:
                mapeadas.append(m)
            else:
                no_mapeadas.append(item)
        if mapeadas and not no_mapeadas:
            return " AND ".join(sorted(set(mapeadas))), f"METADATA:Classifier ({'; '.join(lic_cls)})", "MEDIA"
        return (
            "NOASSERTION",
            f"METADATA:Classifier ambiguo o no mapeable ({'; '.join(lic_cls)})",
            "BAJA",
        )

    if primera:
        return "NOASSERTION", f"METADATA:License texto libre ({primera[:80]})", "BAJA"
    return "NOASSERTION", "METADATA sin declaración de licencia", "BAJA"


def _decodificar_record_sha256(hash_obj: Any) -> str | None:
    """Convierte ``dist.files[i].hash`` (PEP 376 ``sha256=<b64url>``) a 64 hex minúsculas."""
    if hash_obj is None:
        return None
    mode = getattr(hash_obj, "mode", None)
    value = getattr(hash_obj, "value", None)
    if mode != "sha256" or not isinstance(value, str) or not value:
        return None
    pad = "=" * (-len(value) % 4)
    try:
        crudo = base64.urlsafe_b64decode(value + pad)
    except ValueError:
        return None
    if len(crudo) != 32:
        return None
    return crudo.hex()


@dataclass(frozen=True, slots=True)
class PaqueteEntorno:
    """Representación estructurada de un paquete del entorno de build (SBOM-03 + SBOM-05 + F02/F04)."""

    nombre: str
    version: str
    licencia_declarada: str
    licencia_concluida: str
    evidencia: str
    confianza: str
    procedencia: str
    estado_revision_juridica: str
    archivos_record: tuple[str, ...]
    modulos_top_level: tuple[str, ...]
    solo_build: bool = False
    record_hashes: Mapping[str, str] = field(default_factory=dict)
    python_version: str = ""
    evidencia_documental: str = ""

    def como_dict(self) -> dict[str, Any]:
        return {
            "nombre": self.nombre,
            "version": self.version,
            "licencia_declarada": self.licencia_declarada,
            "licencia_concluida": self.licencia_concluida,
            "evidencia": self.evidencia,
            "confianza": self.confianza,
            "procedencia": self.procedencia,
            "estado_revision_juridica": self.estado_revision_juridica,
            "archivos_record": list(self.archivos_record),
            "modulos_top_level": list(self.modulos_top_level),
            "solo_build": self.solo_build,
            "record_hashes": dict(self.record_hashes),
            "python_version": self.python_version,
            "evidencia_documental": self.evidencia_documental,
        }

    @classmethod
    def desde_dict(cls, d: Mapping[str, Any]) -> PaqueteEntorno:
        crudo_hashes = d.get("record_hashes", {})
        hashes_norm = (
            {str(k).replace("\\", "/"): str(v).lower() for k, v in crudo_hashes.items()}
            if isinstance(crudo_hashes, Mapping)
            else {}
        )
        return cls(
            nombre=str(d["nombre"]),
            version=str(d["version"]),
            licencia_declarada=str(d.get("licencia_declarada", "NOASSERTION")),
            licencia_concluida=str(d.get("licencia_concluida", "NOASSERTION")),
            evidencia=str(d.get("evidencia", "")),
            confianza=str(d.get("confianza", "BAJA")),
            procedencia=str(d.get("procedencia", "DESCONOCIDA")),
            estado_revision_juridica=str(d.get("estado_revision_juridica", "PENDIENTE_REVISION_HUMANA")),
            archivos_record=tuple(str(x) for x in d.get("archivos_record", ())),
            modulos_top_level=tuple(str(x) for x in d.get("modulos_top_level", ())),
            solo_build=bool(d.get("solo_build", False)),
            record_hashes=hashes_norm,
            python_version=str(d.get("python_version", "")),
            evidencia_documental=str(d.get("evidencia_documental", "")),
        )


def _meta_valor(meta: importlib_metadata.PackageMetadata, clave: str) -> str:
    valores = meta.get_all(clave)
    return str(valores[0]) if valores else ""


def recolectar_entorno_actual(
    solo_build_nombres: frozenset[str] = PAQUETES_SOLO_BUILD_POR_DEFECTO,
) -> list[PaqueteEntorno]:
    """Recolecta distribuciones del entorno Python activo sin copiar ``licenseDeclared`` a ``licenseConcluded`` (F02/F04)."""
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    paquetes: list[PaqueteEntorno] = []
    vistos: set[str] = set()
    for dist in importlib_metadata.distributions():
        raw_name = _meta_valor(dist.metadata, "Name")
        if not raw_name:
            continue
        norm = _normalizar_pep503(raw_name)
        if norm in vistos or norm == "sky-claw":
            continue
        vistos.add(norm)
        expr = _meta_valor(dist.metadata, "License-Expression")
        lic = _meta_valor(dist.metadata, "License")
        cls = dist.metadata.get_all("Classifier") or []
        decl, ev, conf = _normalizar_licencia_metadata(expr, lic, cls)

        archivos_record: list[str] = []
        record_hashes: dict[str, str] = {}
        modulos_top: set[str] = set()
        tiene_archivo_licencia = False
        top_txt = dist.read_text("top_level.txt")
        if top_txt:
            for linea in top_txt.splitlines():
                t = linea.strip()
                if t:
                    modulos_top.add(t)
        if dist.files:
            for f in dist.files:
                rel = str(f).replace("\\", "/")
                archivos_record.append(rel)
                sha_hex = _decodificar_record_sha256(f.hash)
                if sha_hex is not None:
                    record_hashes[rel] = sha_hex
                base_up = Path(rel).name.upper()
                if base_up.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")):
                    tiene_archivo_licencia = True
                partes = rel.split("/")
                if (
                    len(partes) >= 2
                    and not partes[0].endswith((".dist-info", ".data"))
                    and not partes[0].startswith("..")
                ):
                    modulos_top.add(partes[0])
                elif len(partes) == 1 and partes[0].endswith(".py"):
                    modulos_top.add(partes[0][:-3])

        paquetes.append(
            PaqueteEntorno(
                nombre=norm,
                version=dist.version,
                licencia_declarada=decl,
                # F04: nunca copiar automáticamente licenseDeclared a licenseConcluded solo por metadata
                licencia_concluida="NOASSERTION",
                evidencia=ev,
                confianza=conf,
                procedencia="entorno-build-python",
                estado_revision_juridica="PENDIENTE_REVISION_HUMANA",
                archivos_record=tuple(sorted(set(archivos_record))),
                modulos_top_level=tuple(sorted(modulos_top)),
                solo_build=norm in solo_build_nombres,
                record_hashes=record_hashes,
                python_version=py_ver,
                evidencia_documental="DIST_INFO_LICENSE_FILE" if tiene_archivo_licencia else "METADATA_ONLY",
            )
        )
    paquetes.sort(key=lambda p: p.nombre)
    return paquetes


def leer_lockfile_versiones(lock_path: Path) -> dict[str, str]:
    """Lee pares ``paquete -> versión`` de ``requirements.lock`` para diagnóstico de drift (SBOM-08)."""
    versiones: dict[str, str] = {}
    for linea in lock_path.read_text(encoding="utf-8").splitlines():
        m = _REQ_PIN_RE.match(linea.strip())
        if m:
            versiones[_normalizar_pep503(m.group(1))] = m.group(2)
    return versiones


def _validar_esquema_inventario(inventario: Mapping[str, Any]) -> list[str]:
    """Valida estrictamente el esquema y completitud estructural de ``inventory.json`` (F01 / Codex #3)."""
    errores: list[str] = []
    if inventario.get("schema_version") != 1:
        errores.append(f"schema_version inválido o ausente: {inventario.get('schema_version')!r}")

    exe = inventario.get("exe")
    if not isinstance(exe, Mapping):
        errores.append("Sección obligatoria 'exe' ausente o inválida")
    else:
        if not isinstance(exe.get("name"), str) or not exe["name"].strip():
            errores.append("exe.name ausente o vacío")
        if not isinstance(exe.get("bytes"), int) or exe["bytes"] <= 0:
            errores.append("exe.bytes debe ser un entero positivo")
        sha = exe.get("sha256")
        if not isinstance(sha, str) or _SHA256_RE.fullmatch(sha) is None or set(sha) == {"0"}:
            errores.append("exe.sha256 ausente, inválido o todo ceros")

    boot = inventario.get("bootloader")
    if not isinstance(boot, Mapping):
        errores.append("Sección obligatoria 'bootloader' ausente o inválida")
    else:
        if not isinstance(boot.get("bytes"), int) or boot["bytes"] <= 0:
            errores.append("bootloader.bytes debe ser un entero positivo")
        bsha = boot.get("sha256")
        if not isinstance(bsha, str) or _SHA256_RE.fullmatch(bsha) is None or set(bsha) == {"0"}:
            errores.append("bootloader.sha256 ausente, inválido o todo ceros")

    entries = inventario.get("entries")
    if not isinstance(entries, list) or len(entries) == 0:
        errores.append("entries ausente o vacío: no se puede certificar un inventario sin contenido")
        return errores

    entry_count = inventario.get("entry_count")
    if not isinstance(entry_count, int) or entry_count != len(entries):
        errores.append(f"Inconsistencia de conteo: entry_count={entry_count!r} != len(entries)={len(entries)}")

    vistos: set[tuple[str, str, str]] = set()
    tiene_codigo_propio = False
    tiene_pyz = False
    tiene_base_lib = False
    for idx, e in enumerate(entries):
        if not isinstance(e, Mapping):
            errores.append(f"Entrada #{idx} no es un objeto JSON válido")
            continue
        for campo in ("container", "name", "typecode", "extracted_sha256", "component_category", "technical_origin"):
            if campo not in e or e[campo] in (None, ""):
                errores.append(f"Entrada #{idx}: campo obligatorio '{campo}' ausente")
        cont = str(e.get("container", ""))
        nom = str(e.get("name", ""))
        tc = str(e.get("typecode", ""))
        clave = (cont, nom.casefold(), tc)
        if clave in vistos:
            errores.append(f"Entrada duplicada en inventory.json: {cont}::{nom} ({tc})")
        vistos.add(clave)
        if str(e.get("component_category", "")) == CategoriaComponente.FIRST_PARTY_CODE.value:
            tiene_codigo_propio = True
        if cont.startswith("PYZ:"):
            tiene_pyz = True
        if cont == "ZIP:base_library.zip":
            tiene_base_lib = True

    if not tiene_codigo_propio:
        errores.append("Componente estructural obligatorio ausente: código propio sky_claw (FIRST_PARTY_CODE)")
    if not tiene_pyz:
        errores.append("Componente estructural obligatorio ausente: módulos dentro de contenedor PYZ")
    if not tiene_base_lib:
        errores.append("Componente estructural obligatorio ausente: miembros de ZIP:base_library.zip")

    return errores


def validar_spdx_23(documento: Mapping[str, Any]) -> list[str]:
    """Validador estructural y semántico interno SPDX 2.3 (F05; complementa, no sustituye, el JSON Schema oficial)."""
    errores: list[str] = []
    if documento.get("spdxVersion") != "SPDX-2.3":
        errores.append("spdxVersion debe ser 'SPDX-2.3'")
    if documento.get("dataLicense") != "CC0-1.0":
        errores.append("dataLicense debe ser 'CC0-1.0'")
    if documento.get("SPDXID") != "SPDXRef-DOCUMENT":
        errores.append("SPDXID raíz debe ser 'SPDXRef-DOCUMENT'")
    if not isinstance(documento.get("name"), str) or not documento["name"].strip():
        errores.append("name del documento obligatorio ausente")
    ns = documento.get("documentNamespace")
    if not isinstance(ns, str) or not ns.startswith(("https://", "http://", "urn:")):
        errores.append("documentNamespace obligatorio ausente o sin esquema URI válido")

    creation = documento.get("creationInfo")
    if not isinstance(creation, Mapping):
        errores.append("creationInfo obligatorio ausente")
    else:
        created = creation.get("created")
        if not isinstance(created, str) or _ISO_UTC_RE.fullmatch(created) is None:
            errores.append("creationInfo.created debe ser timestamp ISO-8601 UTC (YYYY-MM-DDTHH:MM:SSZ)")
        creators = creation.get("creators")
        if not isinstance(creators, list) or not creators:
            errores.append("creationInfo.creators obligatorio ausente")
        else:
            for cr in creators:
                if not isinstance(cr, str) or not cr.startswith(_SPDX_CREATOR_PREFIJOS):
                    errores.append(f"Creator SPDX inválido: {cr!r}")

    ids_definidos: set[str] = {"SPDXRef-DOCUMENT"}
    packages = documento.get("packages")
    if not isinstance(packages, list) or not packages:
        errores.append("packages debe ser una lista no vacía")
        packages = []

    for pkg in packages:
        if not isinstance(pkg, Mapping):
            errores.append("Elemento inválido en packages")
            continue
        spdx_id = pkg.get("SPDXID", "")
        if not isinstance(spdx_id, str) or _SPDXID_RE.fullmatch(spdx_id) is None:
            errores.append(f"SPDXID inválido en paquete: {spdx_id!r}")
        elif spdx_id in ids_definidos:
            errores.append(f"SPDXID duplicado: {spdx_id}")
        else:
            ids_definidos.add(spdx_id)

        for campo in (
            "name",
            "versionInfo",
            "downloadLocation",
            "licenseConcluded",
            "licenseDeclared",
            "copyrightText",
        ):
            if campo not in pkg or pkg[campo] in (None, ""):
                errores.append(f"Paquete {spdx_id}: campo obligatorio '{campo}' ausente")
        for lic_campo in ("licenseDeclared", "licenseConcluded"):
            val_lic = pkg.get(lic_campo)
            if isinstance(val_lic, str) and val_lic and not _es_expresion_spdx_valida(val_lic):
                errores.append(f"Paquete {spdx_id}: expresión de licencia SPDX inválida en {lic_campo}: {val_lic!r}")
        if not isinstance(pkg.get("filesAnalyzed"), bool):
            errores.append(f"Paquete {spdx_id}: filesAnalyzed debe ser booleano")
        if pkg.get("filesAnalyzed") is True and "packageVerificationCode" not in pkg:
            errores.append(f"Paquete {spdx_id}: filesAnalyzed=true exige packageVerificationCode")
        for cs in pkg.get("checksums", []):
            alg = str(cs.get("algorithm", "")).upper()
            val = str(cs.get("checksumValue", "")).lower()
            if not val or set(val) == {"0"}:
                errores.append(f"Checksum inválido o todo ceros en paquete {spdx_id}")
            elif alg == "SHA256" and _SHA256_RE.fullmatch(val) is None:
                errores.append(f"Checksum SHA256 malformado en paquete {spdx_id}")

    files = documento.get("files")
    if not isinstance(files, list) or not files:
        errores.append("files debe incluir al menos el archivo ejecutable con SHA256 real")
    else:
        tiene_sha256_real = False
        for f in files:
            if not isinstance(f, Mapping):
                errores.append("Elemento inválido en files")
                continue
            fid = f.get("SPDXID", "")
            if not isinstance(fid, str) or _SPDXID_RE.fullmatch(fid) is None:
                errores.append(f"SPDXID inválido en archivo: {fid!r}")
            elif fid in ids_definidos:
                errores.append(f"SPDXID duplicado en archivo: {fid}")
            else:
                ids_definidos.add(fid)
            for cs in f.get("checksums", []):
                alg = str(cs.get("algorithm", "")).upper()
                val = str(cs.get("checksumValue", "")).lower()
                if not val or set(val) == {"0"}:
                    errores.append(f"Checksum todo ceros prohibido en {f.get('fileName')}")
                elif alg == "SHA256" and _SHA256_RE.fullmatch(val):
                    tiene_sha256_real = True
                elif alg == "SHA1" and _SHA1_RE.fullmatch(val) is None:
                    errores.append(f"Checksum SHA1 malformado en {f.get('fileName')}")
        if not tiene_sha256_real:
            errores.append("Falta checksum SHA256 real del ejecutable en files")

    rels = documento.get("relationships")
    if not isinstance(rels, list) or not rels:
        errores.append("Falta relación DESCRIBES desde SPDXRef-DOCUMENT")
    else:
        tiene_describes = False
        for r in rels:
            if not isinstance(r, Mapping):
                errores.append("Relación inválida en relationships")
                continue
            src = str(r.get("spdxElementId", ""))
            rel_t = str(r.get("relationshipType", ""))
            dst = str(r.get("relatedSpdxElement", ""))
            if rel_t not in _SPDX_RELACIONES_VALIDAS:
                errores.append(f"relationshipType no reconocido: {rel_t!r}")
            if src not in ids_definidos:
                errores.append(f"Relación con origen huérfano: {src}")
            if dst not in ids_definidos and dst not in {"NONE", "NOASSERTION"}:
                errores.append(f"Relación con destino huérfano: {dst}")
            if src == "SPDXRef-DOCUMENT" and rel_t == "DESCRIBES":
                tiene_describes = True
        if not tiene_describes:
            errores.append("Falta relación DESCRIBES desde SPDXRef-DOCUMENT")

    return errores


def _atribuir_entrada(
    entrada: Mapping[str, Any],
    indice_modulos: Mapping[str, list[PaqueteEntorno]],
    indice_archivos: Mapping[str, list[PaqueteEntorno]],
) -> tuple[PaqueteEntorno | None, list[str], str | None]:
    """Atribuye una entrada del EXE sin heurísticas silenciosas (SBOM-03/F02).

    Devuelve ``(paquete_atribuido, candidatos_ambiguos, ruta_record_coincidente)``.
    """
    nombre = str(entrada["name"])
    contenedor = str(entrada["container"])

    # 1. Coincidencia exacta por ruta en RECORD
    por_archivo = indice_archivos.get(nombre.casefold(), [])
    if len(por_archivo) == 1:
        return por_archivo[0], [], nombre
    if len(por_archivo) > 1:
        return None, sorted(p.nombre for p in por_archivo), None

    # 2. Para módulos PYZ, resolver ruta exacta de módulo/paquete en RECORD (soporta namespaces PEP 420)
    if contenedor.startswith("PYZ:"):
        ruta_mod = nombre.replace(".", "/").casefold()
        for candidato_ruta in (f"{ruta_mod}.py", f"{ruta_mod}/__init__.py"):
            por_mod_exacto = indice_archivos.get(candidato_ruta, [])
            if len(por_mod_exacto) == 1:
                return por_mod_exacto[0], [], candidato_ruta
            if len(por_mod_exacto) > 1:
                return None, sorted(p.nombre for p in por_mod_exacto), None

    # 3. Coincidencia por módulo top-level para entradas PYZ o rutas con prefijo de paquete
    raiz = nombre.split(".")[0] if contenedor.startswith("PYZ:") else nombre.split("/")[0]
    por_modulo = indice_modulos.get(raiz.casefold(), [])
    if len(por_modulo) == 1:
        return por_modulo[0], [], None
    if len(por_modulo) > 1:
        return None, sorted(p.nombre for p in por_modulo), None

    return None, [], None


def _familia_fuente_gui(ruta_fuente: str) -> str:
    """Extrae la familia tipográfica canónica a partir del archivo de fuente GUI (F03)."""
    stem = Path(ruta_fuente).stem.casefold()
    for prefijo, familia in (
        ("cinzel", "cinzel"),
        ("eb-garamond", "eb-garamond"),
        ("ebgaramond", "eb-garamond"),
        ("noto-sans-runic", "noto-sans-runic"),
        ("notosansrunic", "noto-sans-runic"),
        ("spline-sans-mono", "spline-sans-mono"),
        ("splinesansmono", "spline-sans-mono"),
    ):
        if stem.startswith(prefijo):
            return familia
    return re.sub(r"[^a-z0-9-]+", "-", stem.split("-")[0]).strip("-") or "desconocida"


def _reconciliar_analysis_toc(
    analysis_toc: Mapping[str, Any] | None,
    entradas: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Contrasta ``Analysis`` TOC de PyInstaller contra las entradas efectivamente empaquetadas (F02/F11/F14).

    No descarta silenciosamente secciones ni entradas malformadas: cualquier estructura inválida
    devuelve ``status='MALFORMED'`` para fallo cerrado.
    """
    if analysis_toc is None:
        return {
            "provided": False,
            "status": "NOT_PROVIDED",
            "declared_count": 0,
            "matched_count": 0,
            "missing_from_exe": [],
            "malformed_entries": [],
        }

    if not isinstance(analysis_toc, Mapping):
        return {
            "provided": True,
            "status": "MALFORMED",
            "declared_count": 0,
            "matched_count": 0,
            "missing_from_exe": [],
            "malformed_entries": ["analysis_toc debe ser un objeto JSON (Mapping)"],
        }

    nombres_en_exe = {str(e.get("name", "")).replace("\\", "/").casefold() for e in entradas}
    declarados: list[str] = []
    malformed: list[str] = []
    for seccion in ("pure", "binaries", "datas"):
        if seccion not in analysis_toc or analysis_toc[seccion] is None:
            continue
        items = analysis_toc[seccion]
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            malformed.append(f"Sección '{seccion}' debe ser una lista/tupla, recibido {type(items).__name__}")
            continue
        for idx, item in enumerate(items):
            if isinstance(item, str):
                dest = item
            elif (
                isinstance(item, Sequence)
                and not isinstance(item, (str, bytes))
                and len(item) >= 1
                and isinstance(item[0], str)
            ):
                dest = item[0]
            else:
                malformed.append(f"Entrada malformada en '{seccion}[{idx}]': {item!r}")
                continue
            dest_limpio = dest.strip()
            if not dest_limpio or ".." in re.split(r"[/\\]", dest_limpio) or dest_limpio.startswith(("/", "\\")):
                malformed.append(f"Destino inválido en '{seccion}[{idx}]': {dest!r}")
                continue
            declarados.append(dest_limpio.replace("\\", "/").lstrip("./"))

    if malformed:
        return {
            "provided": True,
            "status": "MALFORMED",
            "declared_count": len(set(declarados)),
            "matched_count": 0,
            "missing_from_exe": [],
            "malformed_entries": malformed,
        }

    unicos = sorted(set(declarados))
    if not unicos:
        return {
            "provided": True,
            "status": "EMPTY_TOC",
            "declared_count": 0,
            "matched_count": 0,
            "missing_from_exe": [],
            "malformed_entries": [],
        }

    faltantes: list[str] = []
    coincidentes = 0
    for dest in unicos:
        if dest.casefold() in nombres_en_exe:
            coincidentes += 1
        else:
            faltantes.append(dest)

    return {
        "provided": True,
        "status": "DISCREPANCY" if faltantes else "VERIFIED",
        "declared_count": len(unicos),
        "matched_count": coincidentes,
        "missing_from_exe": faltantes,
        "malformed_entries": [],
    }


def _detectar_version_python_exe(inventario: Mapping[str, Any], entradas: Sequence[Mapping[str, Any]]) -> str:
    """Extrae la versión ``major.minor`` de CPython desde la cookie o ``python3XX.dll`` del EXE (F02/Codex #4)."""
    exe_info = inventario.get("exe", {})
    if isinstance(exe_info, Mapping):
        cookie_ver = str(exe_info.get("python_version_cookie", "")).strip()
        if re.fullmatch(r"\d+\.\d+", cookie_ver):
            return cookie_ver
        py_dll = str(exe_info.get("python_dll", "")).casefold()
        m_dll = re.fullmatch(r"python(\d)(\d+)\.dll", py_dll)
        if m_dll:
            return f"{m_dll.group(1)}.{m_dll.group(2)}"
    for e in entradas:
        base = Path(str(e.get("name", ""))).name.casefold()
        m = re.fullmatch(r"python(\d)(\d+)\.dll", base)
        if m:
            return f"{m.group(1)}.{m.group(2)}"
    return ""


def _version_entorno_python(
    paquetes_entorno: Sequence[PaqueteEntorno],
    build_python_version: str | None,
) -> str:
    if build_python_version and build_python_version.strip():
        return build_python_version.strip()
    versiones = sorted({p.python_version.strip() for p in paquetes_entorno if p.python_version.strip()})
    return versiones[0] if len(versiones) == 1 else ""


def reconciliar(
    inventario: Mapping[str, Any],
    paquetes_entorno: Sequence[PaqueteEntorno],
    *,
    lock_versiones: Mapping[str, str] | None = None,
    analysis_toc: Mapping[str, Any] | None = None,
    spec_datas_esperados: Sequence[str] | None = None,
    build_python_version: str | None = None,
    creado_en: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Reconcilia el inventario del EXE contra el entorno de build y emite SPDX 2.3 + reporte."""
    inventory_schema_errors = _validar_esquema_inventario(inventario)

    indice_modulos: dict[str, list[PaqueteEntorno]] = {}
    indice_archivos: dict[str, list[PaqueteEntorno]] = {}
    mapa_entorno: dict[str, PaqueteEntorno] = {}

    for pkg in paquetes_entorno:
        mapa_entorno[pkg.nombre] = pkg
        for mod in pkg.modulos_top_level:
            indice_modulos.setdefault(mod.casefold(), []).append(pkg)
        for rel in pkg.archivos_record:
            indice_archivos.setdefault(rel.replace("\\", "/").casefold(), []).append(pkg)

    redistribuidos: dict[str, PaqueteEntorno] = {}
    unidentified: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    pep420_namespaces: list[dict[str, Any]] = []
    pending_legal: list[dict[str, Any]] = []
    hash_mismatches: list[dict[str, Any]] = []
    hash_verified_binary_count = 0
    bytecode_entries_count = 0
    nominal_without_hash: list[str] = []
    opaque_containers: list[dict[str, str]] = list(inventario.get("opaque_containers", []))

    stdlib_presente = False
    openssl_files: list[str] = []
    sqlite_files: list[str] = []
    libffi_files: list[str] = []
    tcl_tk_files: list[str] = []
    fuentes_ofl_por_familia: dict[str, list[str]] = {}
    vendored_frontend: list[dict[str, Any]] = []
    mo2_bridge_files: list[str] = []
    windows_dlls: list[str] = []

    raw_entries = inventario.get("entries", [])
    entradas: list[Mapping[str, Any]] = (
        [e for e in raw_entries if isinstance(e, Mapping)] if isinstance(raw_entries, list) else []
    )

    for e in entradas:
        cat = str(e.get("component_category", ""))
        orig = str(e.get("technical_origin", ""))
        der = str(e.get("redistribution_rights_status", ""))
        nombre = str(e.get("name", ""))
        cont = str(e.get("container", ""))

        # F09: registrar frontera in-process con MO2 sin ocultar el estado jurídico pendiente
        if orig == OrigenTecnico.MO2_PLUGIN_IN_PROCESS.value:
            mo2_bridge_files.append(nombre)
            continue

        if der == EstadoDerechosRedistribucion.PENDIENTE_REVISION_JURIDICA.value:
            pending_legal.append(
                {
                    "name": nombre,
                    "container": cont,
                    "component_category": cat,
                    "technical_origin": orig,
                    "redistribution_rights_status": der,
                    "extracted_sha256": e.get("extracted_sha256"),
                }
            )
            continue

        if cat == CategoriaComponente.OPAQUE_CONTAINER.value:
            continue
        if cat in {
            CategoriaComponente.FIRST_PARTY_CODE.value,
            CategoriaComponente.FIRST_PARTY_ASSET.value,
            CategoriaComponente.PYINSTALLER_BOOTLOADER.value,
        }:
            continue
        if cat == CategoriaComponente.WINDOWS_SYSTEM_DLL.value:
            windows_dlls.append(nombre)
            continue
        if orig == OrigenTecnico.OPENSSL_EMBEDDED.value:
            openssl_files.append(nombre)
            continue
        if orig == OrigenTecnico.SQLITE_EMBEDDED.value:
            sqlite_files.append(nombre)
            continue
        if orig == OrigenTecnico.LIBFFI_EMBEDDED.value:
            libffi_files.append(nombre)
            continue
        if orig == OrigenTecnico.TCL_TK_EMBEDDED.value:
            tcl_tk_files.append(nombre)
            continue
        if cat == CategoriaComponente.THIRD_PARTY_FONT.value and orig == OrigenTecnico.GOOGLE_FONTS_OFL.value:
            fam = _familia_fuente_gui(nombre)
            fuentes_ofl_por_familia.setdefault(fam, []).append(nombre)
            continue
        if orig == OrigenTecnico.CPYTHON_STDLIB.value:
            stdlib_presente = True
            continue
        if e.get("typecode") == "z" or nombre == "base_library.zip":
            continue

        # Recursos vendorizados de frontend (p. ej. nicegui/static/*.js|*.css|*.woff2): preservar aparte en SBOM (F03)
        if cat == CategoriaComponente.THIRD_PARTY_VENDORED.value or (
            cat == CategoriaComponente.THIRD_PARTY_FONT.value and nombre.casefold().startswith("nicegui/")
        ):
            vendored_frontend.append(
                {
                    "name": nombre,
                    "container": cont,
                    "component_category": cat,
                    "extracted_sha256": e.get("extracted_sha256"),
                }
            )

        atribuido, candidatos, ruta_record = _atribuir_entrada(e, indice_modulos, indice_archivos)
        if candidatos:
            # PEP 420: paquete namespace implícito sin __init__.py propio (sus módulos hoja se atribuyen por RECORD)
            ruta_init = f"{nombre.replace('.', '/').casefold()}/__init__.py"
            if (
                e.get("typecode") == "pyz_pkg"
                and int(e.get("extracted_bytes", 0)) <= 256
                and ruta_init not in indice_archivos
                and any(
                    any(
                        r.casefold().startswith(f"{nombre.replace('.', '/').casefold()}/")
                        for r in mapa_entorno[c].archivos_record
                    )
                    for c in candidatos
                )
            ):
                pep420_namespaces.append({"name": nombre, "container": cont, "owners": candidatos})
            else:
                ambiguous.append({"name": nombre, "container": cont, "candidates": candidatos})
        elif atribuido is not None:
            redistribuidos[atribuido.nombre] = atribuido
            # F02/F13: distinguir representación bytecode (.pyc/PYZ), verificación criptográfica RECORD y atribución nominal sin hash
            es_bytecode = cont.startswith("PYZ:") or nombre.casefold().endswith(".pyc")
            if es_bytecode:
                bytecode_entries_count += 1
            elif ruta_record is not None and atribuido.record_hashes:
                mapa_hashes_ci = {k.replace("\\", "/").casefold(): v for k, v in atribuido.record_hashes.items()}
                sha_record = mapa_hashes_ci.get(ruta_record.replace("\\", "/").casefold())
                sha_exe_entry = str(e.get("extracted_sha256", "")).lower()
                if sha_record is not None:
                    if sha_record == sha_exe_entry:
                        hash_verified_binary_count += 1
                    else:
                        hash_mismatches.append(
                            {
                                "name": nombre,
                                "package": atribuido.nombre,
                                "record_sha256": sha_record,
                                "exe_extracted_sha256": sha_exe_entry,
                            }
                        )
                else:
                    nominal_without_hash.append(nombre)
            else:
                nominal_without_hash.append(nombre)
        elif cat != CategoriaComponente.THIRD_PARTY_VENDORED.value and not (
            cat == CategoriaComponente.THIRD_PARTY_FONT.value and nombre.casefold().startswith("nicegui/")
        ):
            unidentified.append(
                {
                    "name": nombre,
                    "container": cont,
                    "component_category": cat,
                    "extracted_sha256": e.get("extracted_sha256"),
                }
            )

    # F02/F12: verificar compatibilidad de entorno Python de build vs EXE analizado (cookie + PE binary)
    exe_py_minor = _detectar_version_python_exe(inventario, entradas)
    exe_info_raw = inventario.get("exe", {})
    exe_py_binary = (
        str(exe_info_raw.get("python_version_binary", "")).strip() if isinstance(exe_info_raw, Mapping) else ""
    )
    if not re.fullmatch(r"\d+\.\d+\.\d+", exe_py_binary):
        exe_py_binary = ""
    env_py_ver = _version_entorno_python(paquetes_entorno, build_python_version)
    environment_mismatch: list[str] = []
    if exe_py_minor and env_py_ver:
        env_minor = ".".join(env_py_ver.split(".")[:2])
        if env_minor != exe_py_minor:
            environment_mismatch.append(
                f"CPython del EXE ({exe_py_minor}) no coincide con el entorno de análisis ({env_py_ver})"
            )
    if exe_py_binary and env_py_ver and re.fullmatch(r"\d+\.\d+\.\d+", env_py_ver) and env_py_ver != exe_py_binary:
        environment_mismatch.append(
            f"Versión patch de CPython en binario EXE ({exe_py_binary}) no coincide con entorno declarado ({env_py_ver})"
        )

    # F02: reconciliación real contra analysis_toc si se provee
    analysis_toc_rec = _reconciliar_analysis_toc(analysis_toc, entradas)

    # Verificar presencia de datas esperados de sky_claw.spec (si se solicita)
    esperados = tuple(spec_datas_esperados) if spec_datas_esperados is not None else ()
    rutas_carchive = [str(e["name"]) for e in entradas if e.get("container") == "CArchive"]
    missing_spec_datas = [
        pref for pref in esperados if not any(r == pref or r.startswith(pref + "/") for r in rutas_carchive)
    ]

    # F04: Evaluar incertidumbre de licencias y revisión humana pendiente en paquetes redistribuidos
    license_uncertainties: list[dict[str, Any]] = []
    pending_human_legal_review: list[dict[str, Any]] = []
    for pkg in sorted(redistribuidos.values(), key=lambda p: p.nombre):
        decl = pkg.licencia_declarada.strip() or "NOASSERTION"
        concl = pkg.licencia_concluida.strip() or "NOASSERTION"
        decl_valida = _es_expresion_spdx_valida(decl) and decl not in {"NOASSERTION", "NONE", "UNKNOWN"}
        concl_valida = _es_expresion_spdx_valida(concl) and concl not in {"NOASSERTION", "NONE", "UNKNOWN"}
        if not decl_valida or not concl_valida or decl != concl:
            license_uncertainties.append(
                {
                    "package": pkg.nombre,
                    "version": pkg.version,
                    "licenseDeclared": decl,
                    "licenseConcluded": concl,
                    "evidence": pkg.evidencia,
                    "documentary_evidence": pkg.evidencia_documental or "NO_ESPECIFICADA",
                    "legal_review_status": pkg.estado_revision_juridica,
                    "reason": (
                        "licencia declarada válida pendiente de conclusión/revisión humana"
                        if decl_valida and concl == "NOASSERTION"
                        else "licencia desconocida, inválida SPDX o contradictoria entre declarada y concluida"
                    ),
                }
            )
        if pkg.estado_revision_juridica != "VERIFICADO_DOCUMENTAL" and concl == "NOASSERTION":
            pending_human_legal_review.append(
                {
                    "package": pkg.nombre,
                    "version": pkg.version,
                    "licenseDeclared": decl,
                    "documentary_evidence": pkg.evidencia_documental or "NO_ESPECIFICADA",
                    "legal_review_status": pkg.estado_revision_juridica,
                }
            )

    if vendored_frontend:
        license_uncertainties.append(
            {
                "package": "nicegui-vendored-frontend-assets",
                "version": "NOASSERTION",
                "licenseDeclared": "NOASSERTION",
                "licenseConcluded": "NOASSERTION",
                "evidence": f"{len(vendored_frontend)} activos JS/CSS/fuentes embebidos en nicegui",
                "documentary_evidence": "PENDIENTE_INVENTARIO_LICENCIAS_FRONTEND",
                "legal_review_status": "PENDIENTE_REVISION_JURIDICA",
                "reason": "activos frontend vendorizados requieren inventario explícito de licencias upstream",
            }
        )

    # Clasificación de entorno (SBOM-03) y drift diagnóstico respecto a requirements.lock (SBOM-08)
    redist_names = sorted(redistribuidos)
    build_only_names = sorted(p.nombre for p in paquetes_entorno if p.solo_build and p.nombre not in redistribuidos)
    installed_not_redist = sorted(
        p.nombre for p in paquetes_entorno if not p.solo_build and p.nombre not in redistribuidos
    )

    lock_drift: list[dict[str, str]] = []
    if lock_versiones is not None:
        for nombre in redist_names:
            inst_ver = redistribuidos[nombre].version
            locked_ver = lock_versiones.get(nombre)
            if locked_ver is None:
                lock_drift.append({"package": nombre, "installed": inst_ver, "locked": "ABSENT_IN_LOCK"})
            elif locked_ver != inst_ver:
                lock_drift.append({"package": nombre, "installed": inst_ver, "locked": locked_ver})

    # Construcción de documento SPDX 2.3 (SBOM-04 + F03)
    timestamp = creado_en or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    exe_info = inventario.get("exe", {}) if isinstance(inventario.get("exe"), Mapping) else {}
    boot_info = inventario.get("bootloader", {}) if isinstance(inventario.get("bootloader"), Mapping) else {}
    exe_sha256 = str(exe_info.get("sha256", ""))
    exe_name = str(exe_info.get("name", "SkyClawApp.exe"))
    boot_sha256 = str(boot_info.get("sha256", ""))
    exe_pkg_id = "SPDXRef-Package-SkyClawApp-Exe"
    exe_file_id = "SPDXRef-File-SkyClawApp-Exe"

    spdx_packages: list[dict[str, Any]] = [
        {
            "SPDXID": exe_pkg_id,
            "name": exe_name,
            "versionInfo": "frozen-candidate",
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "checksums": [{"algorithm": "SHA256", "checksumValue": exe_sha256}] if exe_sha256 else [],
            "licenseDeclared": "MIT",
            "licenseConcluded": (
                "NOASSERTION"
                if (pending_legal or license_uncertainties or unidentified or inventory_schema_errors)
                else "MIT"
            ),
            "copyrightText": "Copyright (c) 2025-2026 FacundoSu1986",
            "primaryPackagePurpose": "APPLICATION",
        }
    ]
    spdx_relationships: list[dict[str, str]] = [
        {"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES", "relatedSpdxElement": exe_pkg_id},
        {"spdxElementId": exe_pkg_id, "relationshipType": "CONTAINS", "relatedSpdxElement": exe_file_id},
    ]

    def _agregar_paquete_spdx(pkg_dict: dict[str, Any]) -> None:
        spdx_packages.append(pkg_dict)
        spdx_relationships.append(
            {"spdxElementId": exe_pkg_id, "relationshipType": "CONTAINS", "relatedSpdxElement": pkg_dict["SPDXID"]}
        )

    # Bootloader PyInstaller
    _agregar_paquete_spdx(
        {
            "SPDXID": "SPDXRef-Package-PyInstaller-Bootloader",
            "name": "pyinstaller-bootloader",
            "versionInfo": "embedded",
            "downloadLocation": "https://github.com/pyinstaller/pyinstaller",
            "filesAnalyzed": False,
            "checksums": [{"algorithm": "SHA256", "checksumValue": boot_sha256}] if boot_sha256 else [],
            "licenseDeclared": "GPL-2.0-or-later WITH Bootloader-exception",
            "licenseConcluded": "NOASSERTION",
            "copyrightText": "Copyright (c) PyInstaller Development Team",
        }
    )

    # F02/F03/F10/F12: CPython exacto exige evidencia binaria del artefacto (python_version_binary)
    # coincidente con la versión exacta del entorno de build (major.minor solo NO certifica patch).
    cpython_version_uncertainty = False
    if stdlib_presente:
        tiene_patch_entorno = bool(re.fullmatch(r"\d+\.\d+\.\d+", env_py_ver))
        if exe_py_binary and tiene_patch_entorno and env_py_ver == exe_py_binary and not environment_mismatch:
            cpython_ver = exe_py_binary
        else:
            cpython_ver = "NOASSERTION"
            cpython_version_uncertainty = True
        _agregar_paquete_spdx(
            {
                "SPDXID": "SPDXRef-Package-CPython-Runtime",
                "name": "cpython-runtime",
                "versionInfo": cpython_ver,
                "downloadLocation": "https://www.python.org/",
                "filesAnalyzed": False,
                "licenseDeclared": "PSF-2.0",
                "licenseConcluded": "NOASSERTION",
                "copyrightText": "Copyright (c) Python Software Foundation",
                "comment": (
                    f"cookie_minor={exe_py_minor or 'NOASSERTION'}; "
                    f"binary_pe_version={exe_py_binary or 'NOASSERTION'}; "
                    f"version_uncertainty={cpython_version_uncertainty}; "
                    "CPython runtime y stdlib; OpenSSL/SQLite/libffi/Tcl-Tk se reportan como paquetes separados"
                ),
            }
        )

    # F03: Bibliotecas nativas separadas (OpenSSL, SQLite, libffi, Tcl/Tk, Microsoft runtime)
    nativos_especificos: tuple[tuple[list[str], str, str, str, str, str], ...] = (
        (
            openssl_files,
            "SPDXRef-Package-OpenSSL-Embedded",
            "openssl-embedded",
            "https://www.openssl.org/",
            "Apache-2.0",
            "OpenSSL Project Authors",
        ),
        (
            sqlite_files,
            "SPDXRef-Package-SQLite-Embedded",
            "sqlite-embedded",
            "https://www.sqlite.org/",
            "blessing",
            "SQLite Authors (Public Domain)",
        ),
        (
            libffi_files,
            "SPDXRef-Package-libffi-Embedded",
            "libffi-embedded",
            "https://sourceware.org/libffi/",
            "MIT",
            "libffi Authors",
        ),
        (
            tcl_tk_files,
            "SPDXRef-Package-Tcl-Tk-Embedded",
            "tcl-tk-embedded",
            "https://www.tcl-lang.org/",
            "TCL",
            "Tcl/Tk Authors",
        ),
        (
            windows_dlls,
            "SPDXRef-Package-Microsoft-Windows-Runtime",
            "microsoft-windows-runtime",
            "NOASSERTION",
            "NOASSERTION",
            "Copyright (c) Microsoft Corporation",
        ),
    )
    for archivos_nat, sid, nombre_nat, url_nat, lic_decl_nat, copy_nat in nativos_especificos:
        if archivos_nat:
            _agregar_paquete_spdx(
                {
                    "SPDXID": sid,
                    "name": nombre_nat,
                    "versionInfo": "NOASSERTION",
                    "downloadLocation": url_nat,
                    "filesAnalyzed": False,
                    "licenseDeclared": lic_decl_nat,
                    "licenseConcluded": "NOASSERTION",
                    "copyrightText": copy_nat,
                    "comment": (
                        f"version_uncertainty=TRUE (DLLs binarias sin extracción PE de versión); "
                        f"files={', '.join(sorted(set(archivos_nat))[:12])}"
                    ),
                }
            )

    # F03: Fuentes GUI separadas por familia tipográfica
    for familia in sorted(fuentes_ofl_por_familia):
        archivos_fam = sorted(set(fuentes_ofl_por_familia[familia]))
        _agregar_paquete_spdx(
            {
                "SPDXID": _sanear_spdx_id(f"Package-Font-{familia}"),
                "name": f"sky-claw-font-{familia}",
                "versionInfo": "google-fonts-subset",
                "downloadLocation": "https://github.com/google/fonts",
                "filesAnalyzed": False,
                "licenseDeclared": "OFL-1.1",
                "licenseConcluded": "NOASSERTION",
                "copyrightText": f"{familia} Project Authors (verificar OFL.txt)",
                "comment": f"font_family={familia}; files={', '.join(archivos_fam)}",
            }
        )

    # F03: Activos JS/CSS/fuentes vendorizados (p. ej. nicegui)
    if vendored_frontend:
        _agregar_paquete_spdx(
            {
                "SPDXID": "SPDXRef-Package-NiceGUI-Vendored-Frontend",
                "name": "nicegui-vendored-frontend-assets",
                "versionInfo": "NOASSERTION",
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseDeclared": "NOASSERTION",
                "licenseConcluded": "NOASSERTION",
                "copyrightText": "NOASSERTION",
                "comment": (
                    f"vendored_asset_count={len(vendored_frontend)}; "
                    "incluye JS/CSS/fuentes de terceros (Quasar/Vue/Tailwind/Material/etc.) pendientes de inventario detallado"
                ),
            }
        )

    # F09: Componente identificable skyclaw_bridge con frontera in-process MO2 y revisión jurídica pendiente
    if mo2_bridge_files:
        _agregar_paquete_spdx(
            {
                "SPDXID": "SPDXRef-Package-SkyClaw-MO2-Bridge",
                "name": "skyclaw-mo2-bridge-plugin",
                "versionInfo": "embedded-bundle",
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseDeclared": "MIT",
                "licenseConcluded": "NOASSERTION",
                "copyrightText": "Copyright (c) 2025-2026 FacundoSu1986",
                "comment": (
                    f"integration_boundary=IN_PROCESS_MO2_PLUGIN; legal_review=PENDIENTE_REVISION_JURIDICA; "
                    f"compatibility_assessment=NOASSERTION (sin afirmar compatibilidad ni incompatibilidad definitiva); "
                    f"files={', '.join(sorted(set(mo2_bridge_files))[:12])}"
                ),
            }
        )

    # F03 + F04: Recursos pendientes de revisión jurídica explícitos en el SBOM (nunca desaparecen)
    for idx_pl, item_pl in enumerate(sorted(pending_legal, key=lambda x: str(x["name"]))):
        pl_name = str(item_pl["name"])
        pl_sha = str(item_pl.get("extracted_sha256") or "")
        pkg_pl: dict[str, Any] = {
            "SPDXID": _sanear_spdx_id(f"Package-PendingLegal-{idx_pl}-{Path(pl_name).name}"),
            "name": f"pending-legal-review:{pl_name}",
            "versionInfo": "NOASSERTION",
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseDeclared": "NOASSERTION",
            "licenseConcluded": "NOASSERTION",
            "copyrightText": "NOASSERTION",
            "comment": (
                f"technical_origin={item_pl['technical_origin']}; "
                f"redistribution_rights_status={item_pl['redistribution_rights_status']}"
            ),
        }
        if pl_sha and _SHA256_RE.fullmatch(pl_sha):
            pkg_pl["checksums"] = [{"algorithm": "SHA256", "checksumValue": pl_sha}]
        _agregar_paquete_spdx(pkg_pl)

    # Paquetes Python redistribuidos atribuidos
    for pkg in sorted(redistribuidos.values(), key=lambda p: p.nombre):
        pid = _sanear_spdx_id(f"Package-Py-{pkg.nombre}-{pkg.version}")
        _agregar_paquete_spdx(
            {
                "SPDXID": pid,
                "name": pkg.nombre,
                "versionInfo": pkg.version,
                "downloadLocation": f"https://pypi.org/project/{pkg.nombre}/{pkg.version}/",
                "filesAnalyzed": False,
                "licenseDeclared": pkg.licencia_declarada,
                "licenseConcluded": pkg.licencia_concluida,
                "copyrightText": "NOASSERTION",
                "comment": (
                    f"evidence={pkg.evidencia}; documentary={pkg.evidencia_documental or 'N/A'}; "
                    f"confidence={pkg.confianza}; provenance={pkg.procedencia}; "
                    f"legal_review={pkg.estado_revision_juridica}"
                ),
            }
        )

    sbom_spdx: dict[str, Any] = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"sky-claw-sbom-{exe_name}",
        "documentNamespace": f"https://github.com/FacundoSu1986/Sky-Claw/spdx/{exe_sha256 or 'invalid'}",
        "creationInfo": {
            "created": timestamp,
            "creators": ["Tool: sky-claw-reconciliar-sbom-1.1"],
            "licenseListVersion": "3.28",
        },
        "packages": spdx_packages,
        "files": [
            {
                "SPDXID": exe_file_id,
                "fileName": exe_name,
                "checksums": [{"algorithm": "SHA256", "checksumValue": exe_sha256}] if exe_sha256 else [],
                "licenseConcluded": "NOASSERTION",
                "licenseInfoInFiles": ["NOASSERTION"],
                "copyrightText": "Copyright (c) 2025-2026 FacundoSu1986",
            }
        ],
        "relationships": spdx_relationships,
    }

    spdx_errors = validar_spdx_23(sbom_spdx)
    blockers: list[str] = []
    if inventory_schema_errors:
        blockers.append(f"inventory_schema_errors={len(inventory_schema_errors)}")
    if environment_mismatch:
        blockers.append(f"environment_mismatch={len(environment_mismatch)}")
    if cpython_version_uncertainty:
        blockers.append("cpython_version_unverified=1")
    if mo2_bridge_files:
        blockers.append(f"mo2_integration_boundary_pending_legal={len(set(mo2_bridge_files))}")
    if hash_mismatches:
        blockers.append(f"hash_mismatches={len(hash_mismatches)}")
    if analysis_toc_rec["status"] == "MALFORMED":
        blockers.append(f"analysis_toc_malformed={len(analysis_toc_rec['malformed_entries'])}")
    if analysis_toc_rec["status"] == "EMPTY_TOC":
        blockers.append("analysis_toc_empty=1")
    if analysis_toc_rec["missing_from_exe"]:
        blockers.append(f"analysis_toc_missing_from_exe={len(analysis_toc_rec['missing_from_exe'])}")
    if unidentified:
        blockers.append(f"unidentified_entries={len(unidentified)}")
    if opaque_containers:
        blockers.append(f"opaque_containers={len(opaque_containers)}")
    if ambiguous:
        blockers.append(f"ambiguous_attributions={len(ambiguous)}")
    if missing_spec_datas:
        blockers.append(f"missing_spec_datas={len(missing_spec_datas)}")
    if license_uncertainties:
        blockers.append(f"license_uncertainties={len(license_uncertainties)}")
    if pending_legal:
        blockers.append(f"pending_legal_review={len(pending_legal)}")
    if spdx_errors:
        blockers.append(f"spdx_validation_errors={len(spdx_errors)}")

    gate_passed = len(blockers) == 0
    exit_code = EXIT_OK if gate_passed else EXIT_FAIL_CLOSED
    structural_complete = (
        len(inventory_schema_errors) == 0
        and len(environment_mismatch) == 0
        and len(hash_mismatches) == 0
        and analysis_toc_rec["status"] not in {"MALFORMED", "EMPTY_TOC", "DISCREPANCY"}
        and len(unidentified) == 0
        and len(opaque_containers) == 0
        and len(ambiguous) == 0
    )
    # F11: distinguir inventario estructural completo de reconciliación de procedencia verificada con Analysis TOC
    provenance_verified = structural_complete and analysis_toc_rec["status"] == "VERIFIED"
    total_attributed = (
        hash_verified_binary_count + bytecode_entries_count + len(nominal_without_hash) + len(hash_mismatches)
    )

    reporte: dict[str, Any] = {
        "schema_version": 3,
        "exe": exe_info,
        "trust_model": inventario.get("trust_model", {}),
        "gate_passed": gate_passed,
        "structural_inventory_complete": structural_complete,
        "provenance_reconciliation_verified": provenance_verified,
        "technical_inventory_complete": structural_complete,
        "legal_certification": False,
        "spdx_validation_scope": "internal_structural_and_semantic_validator_not_full_jsonschema",
        "blockers": blockers,
        "inventory_schema_errors": inventory_schema_errors,
        "environment_mismatch": environment_mismatch,
        "cpython_version_uncertainty": cpython_version_uncertainty,
        "mo2_integration_boundary": {
            "present": bool(mo2_bridge_files),
            "component_name": "skyclaw-mo2-bridge-plugin",
            "boundary_type": "IN_PROCESS_MO2_PLUGIN",
            "legal_review_status": "PENDIENTE_REVISION_JURIDICA" if mo2_bridge_files else "NO_APLICA",
            "compatibility_assessment": "NOASSERTION",
            "files": sorted(set(mo2_bridge_files)),
        },
        "hash_verification": {
            "total_attributed_entries": total_attributed,
            "verified_cryptographic_record_matches": hash_verified_binary_count,
            "verified_binary_record_matches": hash_verified_binary_count,
            "nominal_attributions_without_hash": len(nominal_without_hash),
            "nominal_entries_sample": sorted(set(nominal_without_hash))[:50],
            "bytecode_transformed_not_hash_comparable": bytecode_entries_count,
            "bytecode_entries_not_compared_to_source_hash": bytecode_entries_count,
            "mismatches": hash_mismatches,
        },
        "hash_mismatches": hash_mismatches,
        "analysis_toc_provided": analysis_toc is not None,
        "analysis_toc_reconciliation": analysis_toc_rec,
        "spdx_validation_errors": spdx_errors,
        "packages_summary": {
            "redistributed": redist_names,
            "installed_not_redistributed": installed_not_redist,
            "build_only": build_only_names,
        },
        "native_components": {
            "cpython_runtime_present": stdlib_presente,
            "openssl_files": sorted(set(openssl_files)),
            "sqlite_files": sorted(set(sqlite_files)),
            "libffi_files": sorted(set(libffi_files)),
            "tcl_tk_files": sorted(set(tcl_tk_files)),
            "windows_system_dlls": sorted(set(windows_dlls)),
        },
        "windows_system_dlls": sorted(set(windows_dlls)),
        "gui_fonts_ofl_by_family": {k: sorted(set(v)) for k, v in sorted(fuentes_ofl_por_familia.items())},
        "gui_fonts_ofl": sorted({f for flist in fuentes_ofl_por_familia.values() for f in flist}),
        "vendored_frontend_assets_count": len(vendored_frontend),
        "unidentified": unidentified,
        "opaque_containers": opaque_containers,
        "ambiguous_attributions": ambiguous,
        "pep420_namespace_parents": pep420_namespaces,
        "missing_spec_datas": missing_spec_datas,
        "license_uncertainties": license_uncertainties,
        "pending_human_legal_review": pending_human_legal_review,
        "pending_legal_review": pending_legal,
        "lock_drift": lock_drift,
    }
    return sbom_spdx, reporte, exit_code


def _escribir_json_seguro(datos: Mapping[str, Any], ruta: Path) -> None:
    """Persiste JSON de forma atómica (temporal en el mismo directorio + ``os.replace``) (F06)."""
    if ruta.is_dir():
        raise OSError(f"El destino es un directorio: {ruta}")
    ruta.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(datos, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=ruta.parent, prefix=f".{ruta.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_path, ruta)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _limpiar_salidas_obsoletas(*rutas: Path) -> None:
    """Elimina salidas previas para impedir la reutilización de un PASS antiguo tras error CLI (F06)."""
    for ruta in rutas:
        if ruta.is_file():
            ruta.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Genera SBOM SPDX 2.3 y reconcilia contra inventory.json (fail-closed)."
    )
    parser.add_argument("--inventory", type=Path, required=True, help="Ruta a inventory.json.")
    parser.add_argument("--sbom-out", type=Path, required=True, help="Ruta de salida para sbom.spdx.json.")
    parser.add_argument(
        "--report-out", type=Path, required=True, help="Ruta de salida para reconciliation_report.json."
    )
    parser.add_argument("--env-json", type=Path, default=None, help="JSON opcional de paquetes del entorno de build.")
    parser.add_argument(
        "--analysis-toc-json", type=Path, default=None, help="JSON opcional del Analysis TOC de PyInstaller (F02)."
    )
    parser.add_argument(
        "--build-python-version", type=str, default=None, help="Versión exacta de Python del entorno de build (F02)."
    )
    parser.add_argument(
        "--lockfile", type=Path, default=None, help="requirements.lock para diagnóstico de drift (SBOM-08)."
    )
    parser.add_argument(
        "--check-spec-datas", action="store_true", help="Exige presencia de todos los datas de sky_claw.spec."
    )
    args = parser.parse_args(argv)

    try:
        inventario = json.loads(args.inventory.read_text(encoding="utf-8"))
        if not isinstance(inventario, Mapping):
            raise ValueError("inventory.json debe contener un objeto JSON en la raíz")
        if args.env_json is not None:
            crudo_env = json.loads(args.env_json.read_text(encoding="utf-8"))
            if not isinstance(crudo_env, list):
                raise ValueError("env-json debe contener una lista de paquetes")
            paquetes = [PaqueteEntorno.desde_dict(item) for item in crudo_env]
        else:
            paquetes = recolectar_entorno_actual()
        toc_data = (
            json.loads(args.analysis_toc_json.read_text(encoding="utf-8"))
            if args.analysis_toc_json is not None
            else None
        )
        lock_ver = leer_lockfile_versiones(args.lockfile) if args.lockfile is not None else None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _limpiar_salidas_obsoletas(args.sbom_out, args.report_out)
        print(f"reconciliar_sbom: error de entrada: {exc}", file=sys.stderr)
        return EXIT_ERROR

    spec_datas = PREFIJOS_DATAS_ESPERADOS_SPEC if args.check_spec_datas else None
    sbom, reporte, codigo = reconciliar(
        inventario,
        paquetes,
        lock_versiones=lock_ver,
        analysis_toc=toc_data,
        spec_datas_esperados=spec_datas,
        build_python_version=args.build_python_version,
    )

    # SBOM-05: Conservar siempre el SBOM y el reporte diagnóstico aunque la reconciliación falle
    try:
        _escribir_json_seguro(sbom, args.sbom_out)
        _escribir_json_seguro(reporte, args.report_out)
    except OSError as exc:
        print(f"reconciliar_sbom: error al escribir salidas: {exc}", file=sys.stderr)
        return EXIT_ERROR

    estado = "PASS" if codigo == EXIT_OK else f"FAIL ({', '.join(reporte['blockers'])})"
    print(f"reconciliar_sbom: {estado} redistributed={len(reporte['packages_summary']['redistributed'])}")
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
