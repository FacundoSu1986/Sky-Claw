"""Generador de SBOM SPDX 2.3 y reconciliador fail-closed contra inventario real del EXE (PR-LIC-03A).

Cumple SBOM-03 a SBOM-08:
- Atribución inequívoca combinando entorno de build (``importlib.metadata``, ``RECORD``,
  ``top_level.txt``), ``Analysis`` TOC opcional e inventario efectivo del EXE.
- Nunca resuelve ambigüedades por heurística silenciosa (``ambiguous_attributions`` → FAIL).
- Genera SPDX 2.3 conforme a especificación (``SPDX-2.3``, ``CC0-1.0``, SHA-256 real del EXE,
  ``DESCRIBES``/``CONTAINS``, ``filesAnalyzed`` + ``packageVerificationCode`` cuando aplica).
- Separa ``licenseDeclared``, ``licenseConcluded``, evidencia, confianza, procedencia y estado
  de revisión jurídica (SBOM-05).
- Conserva siempre ``sbom.spdx.json`` y ``reconciliation_report.json`` incluso en FAIL.
- Reporta drift diagnóstico contra ``requirements.lock`` sin alterar resolución (SBOM-08).
"""

from __future__ import annotations

import argparse
import importlib.metadata as importlib_metadata
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
)

EXIT_OK = 0
EXIT_FAIL_CLOSED = 1
EXIT_ERROR = 2

_SPDXID_RE = re.compile(r"^SPDXRef-[A-Za-z0-9.-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REQ_PIN_RE = re.compile(r"^([A-Za-z0-9_.\-]+)==([^\s;\\]+)")

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


def _normalizar_licencia_metadata(expr: str, lic: str, classifiers: Sequence[str]) -> tuple[str, str, str]:
    """Devuelve ``(licenseDeclared, evidencia, confianza)`` sin inventar licencias (SBOM-05)."""
    if expr.strip():
        return expr.strip(), "METADATA:License-Expression", "ALTA"
    primera = lic.splitlines()[0].strip() if lic.strip() else ""
    if primera and primera.upper() not in {"UNKNOWN", "NONE", "N/A"} and len(primera) <= 120:
        return primera, "METADATA:License", "MEDIA"
    lic_cls = [c.split("::")[-1].strip() for c in classifiers if c.startswith("License ::")]
    if lic_cls:
        return " AND ".join(sorted(set(lic_cls))), "METADATA:Classifier", "MEDIA"
    return "NOASSERTION", "METADATA sin declaración de licencia", "BAJA"


@dataclass(frozen=True, slots=True)
class PaqueteEntorno:
    """Representación estructurada de un paquete del entorno de build (SBOM-03 + SBOM-05)."""

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
        }

    @classmethod
    def desde_dict(cls, d: Mapping[str, Any]) -> PaqueteEntorno:
        return cls(
            nombre=str(d["nombre"]),
            version=str(d["version"]),
            licencia_declarada=str(d.get("licencia_declarada", "NOASSERTION")),
            licencia_concluida=str(d.get("licencia_concluida", "NOASSERTION")),
            evidencia=str(d.get("evidencia", "")),
            confianza=str(d.get("confianza", "BAJA")),
            procedencia=str(d.get("procedencia", "DESCONOCIDA")),
            estado_revision_juridica=str(d.get("estado_revision_juridica", "PENDIENTE")),
            archivos_record=tuple(str(x) for x in d.get("archivos_record", ())),
            modulos_top_level=tuple(str(x) for x in d.get("modulos_top_level", ())),
            solo_build=bool(d.get("solo_build", False)),
        )


def _meta_valor(meta: importlib_metadata.PackageMetadata, clave: str) -> str:
    valores = meta.get_all(clave)
    return str(valores[0]) if valores else ""


def recolectar_entorno_actual(
    solo_build_nombres: frozenset[str] = PAQUETES_SOLO_BUILD_POR_DEFECTO,
) -> list[PaqueteEntorno]:
    """Recolecta distribuciones instaladas en el entorno Python activo (``importlib.metadata``)."""
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
        modulos_top: set[str] = set()
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
                licencia_concluida=decl if decl != "NOASSERTION" else "NOASSERTION",
                evidencia=ev,
                confianza=conf,
                procedencia="entorno-build-python",
                estado_revision_juridica="VERIFICADO" if decl != "NOASSERTION" else "PENDIENTE",
                archivos_record=tuple(sorted(set(archivos_record))),
                modulos_top_level=tuple(sorted(modulos_top)),
                solo_build=norm in solo_build_nombres,
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


def validar_spdx_23(documento: Mapping[str, Any]) -> list[str]:
    """Valida requisitos estructurales y de integridad SPDX 2.3 (SBOM-04)."""
    errores: list[str] = []
    if documento.get("spdxVersion") != "SPDX-2.3":
        errores.append("spdxVersion debe ser 'SPDX-2.3'")
    if documento.get("dataLicense") != "CC0-1.0":
        errores.append("dataLicense debe ser 'CC0-1.0'")
    if documento.get("SPDXID") != "SPDXRef-DOCUMENT":
        errores.append("SPDXID raíz debe ser 'SPDXRef-DOCUMENT'")
    if not documento.get("documentNamespace"):
        errores.append("documentNamespace obligatorio ausente")

    creation = documento.get("creationInfo")
    if not isinstance(creation, Mapping) or not creation.get("created") or not creation.get("creators"):
        errores.append("creationInfo.created y creationInfo.creators son obligatorios")

    packages = documento.get("packages")
    if not isinstance(packages, list) or len(packages) < 2:
        errores.append("packages debe contener el ejecutable y sus componentes efectivos (>= 2)")
        packages = []

    ids_vistos: set[str] = set()
    for pkg in packages:
        spdx_id = pkg.get("SPDXID", "")
        if not isinstance(spdx_id, str) or _SPDXID_RE.fullmatch(spdx_id) is None:
            errores.append(f"SPDXID inválido en paquete: {spdx_id!r}")
        elif spdx_id in ids_vistos:
            errores.append(f"SPDXID duplicado: {spdx_id}")
        ids_vistos.add(spdx_id)
        for campo in ("name", "versionInfo", "downloadLocation", "licenseConcluded", "licenseDeclared"):
            if campo not in pkg or pkg[campo] in (None, ""):
                errores.append(f"Paquete {spdx_id}: campo obligatorio '{campo}' ausente")
        if pkg.get("filesAnalyzed") is True and "packageVerificationCode" not in pkg:
            errores.append(f"Paquete {spdx_id}: filesAnalyzed=true exige packageVerificationCode")

    files = documento.get("files")
    if not isinstance(files, list) or not files:
        errores.append("files debe incluir al menos el archivo ejecutable con SHA256 real")
    else:
        tiene_sha256_real = False
        for f in files:
            for cs in f.get("checksums", []):
                alg = str(cs.get("algorithm", "")).upper()
                val = str(cs.get("checksumValue", "")).lower()
                if set(val) == {"0"}:
                    errores.append(f"Checksum todo ceros prohibido en {f.get('fileName')}")
                if alg == "SHA256" and _SHA256_RE.fullmatch(val):
                    tiene_sha256_real = True
        if not tiene_sha256_real:
            errores.append("Falta checksum SHA256 real del ejecutable en files")

    rels = documento.get("relationships")
    if not isinstance(rels, list) or not any(r.get("relationshipType") == "DESCRIBES" for r in rels):
        errores.append("Falta relación DESCRIBES desde SPDXRef-DOCUMENT")

    return errores


def _atribuir_entrada(
    entrada: Mapping[str, Any],
    indice_modulos: Mapping[str, list[PaqueteEntorno]],
    indice_archivos: Mapping[str, list[PaqueteEntorno]],
) -> tuple[PaqueteEntorno | None, list[str]]:
    """Atribuye una entrada del EXE sin heurísticas silenciosas (SBOM-03)."""
    nombre = str(entrada["name"])
    contenedor = str(entrada["container"])

    # 1. Coincidencia exacta por ruta en RECORD
    por_archivo = indice_archivos.get(nombre.casefold(), [])
    if len(por_archivo) == 1:
        return por_archivo[0], []
    if len(por_archivo) > 1:
        return None, sorted(p.nombre for p in por_archivo)

    # 2. Para módulos PYZ, resolver ruta exacta de módulo/paquete en RECORD (soporta namespaces PEP 420)
    if contenedor.startswith("PYZ:"):
        ruta_mod = nombre.replace(".", "/").casefold()
        for candidato_ruta in (f"{ruta_mod}.py", f"{ruta_mod}/__init__.py"):
            por_mod_exacto = indice_archivos.get(candidato_ruta, [])
            if len(por_mod_exacto) == 1:
                return por_mod_exacto[0], []
            if len(por_mod_exacto) > 1:
                return None, sorted(p.nombre for p in por_mod_exacto)

    # 3. Coincidencia por módulo top-level para entradas PYZ o rutas con prefijo de paquete
    raiz = nombre.split(".")[0] if contenedor.startswith("PYZ:") else nombre.split("/")[0]
    por_modulo = indice_modulos.get(raiz.casefold(), [])
    if len(por_modulo) == 1:
        return por_modulo[0], []
    if len(por_modulo) > 1:
        return None, sorted(p.nombre for p in por_modulo)

    return None, []


def reconciliar(
    inventario: Mapping[str, Any],
    paquetes_entorno: Sequence[PaqueteEntorno],
    *,
    lock_versiones: Mapping[str, str] | None = None,
    analysis_toc: Mapping[str, Any] | None = None,
    spec_datas_esperados: Sequence[str] | None = None,
    creado_en: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Reconcilia el inventario del EXE contra el entorno de build y emite SPDX 2.3 + reporte."""
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
    opaque_containers: list[dict[str, str]] = list(inventario.get("opaque_containers", []))
    stdlib_presente = False
    fuentes_ofl_presentes: list[str] = []
    windows_dlls: list[str] = []

    entradas = list(inventario.get("entries", []))
    for e in entradas:
        cat = str(e.get("component_category", ""))
        orig = str(e.get("technical_origin", ""))
        der = str(e.get("redistribution_rights_status", ""))
        nombre = str(e.get("name", ""))

        if der == EstadoDerechosRedistribucion.PENDIENTE_REVISION_JURIDICA.value:
            pending_legal.append(
                {
                    "name": nombre,
                    "container": e.get("container"),
                    "component_category": cat,
                    "technical_origin": orig,
                    "redistribution_rights_status": der,
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
        if cat == CategoriaComponente.THIRD_PARTY_FONT.value and orig == "GOOGLE_FONTS_OFL":
            fuentes_ofl_presentes.append(nombre)
            continue
        if orig == "CPYTHON_STDLIB":
            stdlib_presente = True
            continue
        if e.get("typecode") == "z" or nombre == "base_library.zip":
            continue

        atribuido, candidatos = _atribuir_entrada(e, indice_modulos, indice_archivos)
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
                pep420_namespaces.append({"name": nombre, "container": e.get("container"), "owners": candidatos})
            else:
                ambiguous.append({"name": nombre, "container": e.get("container"), "candidates": candidatos})
        elif atribuido is not None:
            redistribuidos[atribuido.nombre] = atribuido
        else:
            unidentified.append(
                {
                    "name": nombre,
                    "container": e.get("container"),
                    "component_category": cat,
                    "extracted_sha256": e.get("extracted_sha256"),
                }
            )

    # Verificar presencia de datas esperados de sky_claw.spec (si se solicita)
    esperados = tuple(spec_datas_esperados) if spec_datas_esperados is not None else ()
    rutas_carchive = [str(e["name"]) for e in entradas if e.get("container") == "CArchive"]
    missing_spec_datas = [
        pref for pref in esperados if not any(r == pref or r.startswith(pref + "/") for r in rutas_carchive)
    ]

    # Evaluar incertidumbre de licencias en paquetes redistribuidos (SBOM-05)
    license_uncertainties: list[dict[str, Any]] = []
    for pkg in sorted(redistribuidos.values(), key=lambda p: p.nombre):
        decl = pkg.licencia_declarada.strip()
        concl = pkg.licencia_concluida.strip()
        if decl in {"", "NOASSERTION", "UNKNOWN"} or concl in {"", "NOASSERTION", "UNKNOWN"} or decl != concl:
            license_uncertainties.append(
                {
                    "package": pkg.nombre,
                    "version": pkg.version,
                    "licenseDeclared": decl or "NOASSERTION",
                    "licenseConcluded": concl or "NOASSERTION",
                    "reason": "licencia desconocida o contradictoria entre declarada y concluida",
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

    # Construcción de documento SPDX 2.3 (SBOM-04)
    timestamp = creado_en or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    exe_info = inventario["exe"]
    exe_sha256 = str(exe_info["sha256"])
    exe_name = str(exe_info["name"])
    exe_pkg_id = "SPDXRef-Package-SkyClawApp-Exe"
    exe_file_id = "SPDXRef-File-SkyClawApp-Exe"

    spdx_packages: list[dict[str, Any]] = [
        {
            "SPDXID": exe_pkg_id,
            "name": exe_name,
            "versionInfo": "frozen-candidate",
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "checksums": [{"algorithm": "SHA256", "checksumValue": exe_sha256}],
            "licenseDeclared": "MIT",
            "licenseConcluded": "NOASSERTION" if (pending_legal or license_uncertainties or unidentified) else "MIT",
            "copyrightText": "Copyright (c) 2025-2026 FacundoSu1986",
            "primaryPackagePurpose": "APPLICATION",
        }
    ]
    spdx_relationships: list[dict[str, str]] = [
        {"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES", "relatedSpdxElement": exe_pkg_id},
        {"spdxElementId": exe_pkg_id, "relationshipType": "CONTAINS", "relatedSpdxElement": exe_file_id},
    ]

    # Bootloader PyInstaller
    boot_id = "SPDXRef-Package-PyInstaller-Bootloader"
    spdx_packages.append(
        {
            "SPDXID": boot_id,
            "name": "pyinstaller-bootloader",
            "versionInfo": "embedded",
            "downloadLocation": "https://github.com/pyinstaller/pyinstaller",
            "filesAnalyzed": False,
            "checksums": [{"algorithm": "SHA256", "checksumValue": str(inventario["bootloader"]["sha256"])}],
            "licenseDeclared": "GPL-2.0-or-later WITH Bootloader-exception",
            "licenseConcluded": "GPL-2.0-or-later WITH Bootloader-exception",
            "copyrightText": "Copyright (c) PyInstaller Development Team",
        }
    )
    spdx_relationships.append(
        {"spdxElementId": exe_pkg_id, "relationshipType": "CONTAINS", "relatedSpdxElement": boot_id}
    )

    if stdlib_presente:
        py_id = "SPDXRef-Package-CPython-Runtime"
        spdx_packages.append(
            {
                "SPDXID": py_id,
                "name": "cpython-runtime",
                "versionInfo": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                "downloadLocation": "https://www.python.org/",
                "filesAnalyzed": False,
                "licenseDeclared": "PSF-2.0",
                "licenseConcluded": "PSF-2.0",
                "copyrightText": "Copyright (c) Python Software Foundation",
            }
        )
        spdx_relationships.append(
            {"spdxElementId": exe_pkg_id, "relationshipType": "CONTAINS", "relatedSpdxElement": py_id}
        )

    if fuentes_ofl_presentes:
        ofl_id = "SPDXRef-Package-GUI-Fonts-OFL"
        spdx_packages.append(
            {
                "SPDXID": ofl_id,
                "name": "sky-claw-gui-fonts",
                "versionInfo": "google-fonts-subset",
                "downloadLocation": "https://github.com/google/fonts",
                "filesAnalyzed": False,
                "licenseDeclared": "OFL-1.1",
                "licenseConcluded": "OFL-1.1",
                "copyrightText": "Cinzel / EB Garamond / Noto / Spline Sans Mono Project Authors",
            }
        )
        spdx_relationships.append(
            {"spdxElementId": exe_pkg_id, "relationshipType": "CONTAINS", "relatedSpdxElement": ofl_id}
        )

    for pkg in sorted(redistribuidos.values(), key=lambda p: p.nombre):
        pid = _sanear_spdx_id(f"Package-Py-{pkg.nombre}-{pkg.version}")
        spdx_packages.append(
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
                    f"evidence={pkg.evidencia}; confidence={pkg.confianza}; "
                    f"provenance={pkg.procedencia}; legal_review={pkg.estado_revision_juridica}"
                ),
            }
        )
        spdx_relationships.append(
            {"spdxElementId": exe_pkg_id, "relationshipType": "CONTAINS", "relatedSpdxElement": pid}
        )

    sbom_spdx: dict[str, Any] = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"sky-claw-sbom-{exe_name}",
        "documentNamespace": f"https://github.com/FacundoSu1986/Sky-Claw/spdx/{exe_sha256}",
        "creationInfo": {
            "created": timestamp,
            "creators": ["Tool: sky-claw-reconciliar-sbom-1.0"],
            "licenseListVersion": "3.28",
        },
        "packages": spdx_packages,
        "files": [
            {
                "SPDXID": exe_file_id,
                "fileName": exe_name,
                "checksums": [{"algorithm": "SHA256", "checksumValue": exe_sha256}],
                "licenseConcluded": "NOASSERTION",
                "licenseInfoInFiles": ["NOASSERTION"],
                "copyrightText": "Copyright (c) 2025-2026 FacundoSu1986",
            }
        ],
        "relationships": spdx_relationships,
    }

    spdx_errors = validar_spdx_23(sbom_spdx)
    blockers: list[str] = []
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

    reporte: dict[str, Any] = {
        "schema_version": 1,
        "exe": exe_info,
        "gate_passed": gate_passed,
        "technical_inventory_complete": len(unidentified) == 0 and len(opaque_containers) == 0 and len(ambiguous) == 0,
        "legal_certification": False,
        "blockers": blockers,
        "spdx_validation_errors": spdx_errors,
        "packages_summary": {
            "redistributed": redist_names,
            "installed_not_redistributed": installed_not_redist,
            "build_only": build_only_names,
        },
        "windows_system_dlls": sorted(set(windows_dlls)),
        "gui_fonts_ofl": sorted(set(fuentes_ofl_presentes)),
        "unidentified": unidentified,
        "opaque_containers": opaque_containers,
        "ambiguous_attributions": ambiguous,
        "pep420_namespace_parents": pep420_namespaces,
        "missing_spec_datas": missing_spec_datas,
        "license_uncertainties": license_uncertainties,
        "pending_legal_review": pending_legal,
        "lock_drift": lock_drift,
        "analysis_toc_provided": analysis_toc is not None,
    }
    return sbom_spdx, reporte, exit_code


def _escribir_json_seguro(datos: Mapping[str, Any], ruta: Path) -> None:
    if ruta.is_dir():
        raise OSError(f"El destino es un directorio: {ruta}")
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(datos, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


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
        "--lockfile", type=Path, default=None, help="requirements.lock para diagnóstico de drift (SBOM-08)."
    )
    parser.add_argument(
        "--check-spec-datas", action="store_true", help="Exige presencia de todos los datas de sky_claw.spec."
    )
    args = parser.parse_args(argv)

    try:
        inventario = json.loads(args.inventory.read_text(encoding="utf-8"))
        if args.env_json is not None:
            crudo_env = json.loads(args.env_json.read_text(encoding="utf-8"))
            paquetes = [PaqueteEntorno.desde_dict(item) for item in crudo_env]
        else:
            paquetes = recolectar_entorno_actual()
        lock_ver = leer_lockfile_versiones(args.lockfile) if args.lockfile is not None else None
    except (OSError, ValueError, KeyError) as exc:
        print(f"reconciliar_sbom: error de entrada: {exc}", file=sys.stderr)
        return EXIT_ERROR

    spec_datas = PREFIJOS_DATAS_ESPERADOS_SPEC if args.check_spec_datas else None
    sbom, reporte, codigo = reconciliar(inventario, paquetes, lock_versiones=lock_ver, spec_datas_esperados=spec_datas)

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
