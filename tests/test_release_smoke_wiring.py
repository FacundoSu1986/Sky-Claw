"""Ancla los smokes a cada workflow que produce o publica el ejecutable."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

import yaml

RAIZ = Path(__file__).resolve().parents[1]
DIRECTORIO_WORKFLOWS = RAIZ / ".github" / "workflows"
COMANDO_SMOKE = "python scripts/smoke_frozen_executable.py dist/SkyClawApp.exe"
PRODUCTORES_ESPERADOS = {
    ("ci.yml", "build"),
    ("release.yml", "release"),
}
#: Construyen el ejecutable para MEDIRLO (laboratorio de Job Objects) y no lo publican. No son una
#: exención escrita: ``test_los_productores_sin_publicacion_no_publican_el_ejecutable`` verifica que
#: no pueden publicarlo (token de solo lectura, ningún artefacto con ``dist``, ningún paso de
#: firma/SBOM/release).
PRODUCTORES_SIN_PUBLICACION = {
    ("vfs-supervision-sandbox.yml", "sandbox"),
}
#: Rutas que esos productores pueden subir como artefacto: únicamente los resultados de las mediciones.
RUTAS_DE_ARTEFACTO_PERMITIDAS = frozenset({"sandbox_min/resultados*.json", "sandbox_min/pytest.txt"})
#: Registro del SHA-256 del ejecutable recién construido (``scripts/registrar_sha256_ejecutable.py``).
COMANDO_REGISTRO = re.compile(
    r"python scripts/registrar_sha256_ejecutable\.py registrar dist/SkyClawApp\.exe(?: --json (?P<json>\S+))?"
)
COMANDO_VERIFICACION = re.compile(
    r"python scripts/registrar_sha256_ejecutable\.py verificar (?P<json>\S+) dist/SkyClawApp\.exe"
)
#: Productores donde el ejecutable lo usan muchos pasos a lo largo de la corrida: ahí el hash se vuelve a
#: verificar al final, para que «el ejecutable que se probó» y «el que se hasheó» sean el mismo archivo.
PRODUCTORES_CON_VERIFICACION = {
    ("vfs-supervision-sandbox.yml", "sandbox"),
}


def _cargar_workflows() -> dict[str, dict[str, Any]]:
    rutas = sorted(ruta for patron in ("*.yml", "*.yaml") for ruta in DIRECTORIO_WORKFLOWS.glob(patron))
    return {ruta.name: yaml.safe_load(ruta.read_text(encoding="utf-8")) for ruta in rutas}


def _productores_de_ejecutable(workflows: dict[str, dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    productores: dict[tuple[str, str], list[dict[str, Any]]] = {}
    patron_build = re.compile(r"(?:^|\s)pyinstaller\s+sky_claw\.spec(?:\s|$)", re.IGNORECASE)
    for nombre_workflow, workflow in workflows.items():
        for nombre_job, job in workflow.get("jobs", {}).items():
            steps = job.get("steps", [])
            if any(patron_build.search(step.get("run", "")) for step in steps):
                productores[(nombre_workflow, nombre_job)] = steps
    return productores


def _indice_unico(steps: list[dict[str, Any]], predicado: Any, descripcion: str) -> int:
    indices = [indice for indice, step in enumerate(steps) if predicado(step)]
    assert len(indices) == 1, f"se esperaba exactamente un step {descripcion}; encontrados: {indices}"
    return indices[0]


def test_todos_y_solo_los_workflows_productores_smokean_antes_de_publicar() -> None:
    workflows = _cargar_workflows()
    productores = _productores_de_ejecutable(workflows)

    assert set(productores) == PRODUCTORES_ESPERADOS | PRODUCTORES_SIN_PUBLICACION
    for nombre_workflow, nombre_job in sorted(PRODUCTORES_ESPERADOS):
        steps = productores[(nombre_workflow, nombre_job)]
        build_index = _indice_unico(
            steps,
            lambda step: "pyinstaller sky_claw.spec" in step.get("run", "").lower(),
            "de construcción PyInstaller",
        )
        smoke_index = _indice_unico(
            steps,
            lambda step: step.get("run") == COMANDO_SMOKE,
            "de smoke del ejecutable congelado",
        )
        assert build_index < smoke_index, f"{nombre_workflow} debe smoke-testear después de construir"

        if nombre_workflow == "ci.yml":
            assert workflows[nombre_workflow]["jobs"][nombre_job]["runs-on"] == "windows-latest"
            publish_index = _indice_unico(
                steps,
                lambda step: step.get("uses", "").startswith("actions/upload-artifact@"),
                "de publicación del artefacto de CI",
            )
            assert steps[build_index].get("id") == "build_executable"
            assert steps[smoke_index].get("id") == "smoke_frozen_executable"
        else:
            assert workflows[nombre_workflow]["jobs"][nombre_job]["runs-on"] == "windows-latest"
            sbom_index = _indice_unico(steps, lambda step: step.get("name") == "Generate SBOM", "de SBOM")
            signing_index = _indice_unico(
                steps,
                lambda step: step.get("name") == "Sign executable",
                "de firma",
            )
            publish_index = _indice_unico(
                steps,
                lambda step: step.get("name") == "Create GitHub Release",
                "de publicación de release",
            )
            assert smoke_index < sbom_index < signing_index < publish_index
            assert steps[smoke_index].get("id") == "smoke_frozen_executable"

        assert smoke_index < publish_index, f"{nombre_workflow} publica antes de validar el ejecutable"


def test_release_conserva_el_job_pausado_y_no_cambia_la_receta_de_firma() -> None:
    release = _cargar_workflows()["release.yml"]
    release_job = release["jobs"]["release"]
    steps = release_job["steps"]

    assert release_job["if"] == "${{ false }}"
    signing_step = _indice_unico(steps, lambda step: step.get("name") == "Sign executable", "de firma")
    assert steps[signing_step]["run"] == (
        "cosign sign-blob dist/SkyClawApp.exe `\n  --yes `\n  --bundle dist/SkyClawApp.exe.bundle.json\n"
    )


def test_los_productores_sin_publicacion_no_publican_el_ejecutable() -> None:
    """Un productor fuera de ``PRODUCTORES_ESPERADOS`` solo es admisible si NO puede publicar."""
    workflows = _cargar_workflows()
    productores = _productores_de_ejecutable(workflows)
    patron_publicacion = re.compile(r"\b(?:sign|sbom|release|cosign|attest|publish)\b", re.IGNORECASE)

    for nombre_workflow, nombre_job in sorted(PRODUCTORES_SIN_PUBLICACION):
        workflow = workflows[nombre_workflow]
        steps = productores[(nombre_workflow, nombre_job)]

        # El token no puede escribir en el repo ni en releases.
        assert workflow.get("permissions") == {"contents": "read"}, nombre_workflow
        assert "permissions" not in workflow["jobs"][nombre_job], nombre_workflow
        for step in steps:
            # Ningún paso de firma, SBOM ni release (ni por nombre ni por acción ni por comando).
            for campo in (step.get("name", ""), step.get("uses", ""), step.get("run", "")):
                assert patron_publicacion.search(campo) is None, (nombre_workflow, campo)
            # Los artefactos subidos son SOLO los resultados del laboratorio (allowlist, no una búsqueda de
            # subcadena: ``path: .`` subiría ``dist/SkyClawApp.exe`` sin pasar por el smoke).
            if step.get("uses", "").startswith("actions/upload-artifact@"):
                rutas = {linea.strip() for linea in str(step.get("with", {}).get("path", "")).splitlines()}
                rutas.discard("")
                assert rutas and rutas <= RUTAS_DE_ARTEFACTO_PERMITIDAS, (nombre_workflow, sorted(rutas))


#: Las ÚNICAS condiciones completas que hacen correr un paso también cuando uno anterior falló (o, con
#: ``always()``, hasta si cancelaron el job). Una subcadena no alcanza: ``cancelled()`` solo es verdadero si
#: cancelaron, y ``success() && !cancelled()`` exige que todo lo anterior haya salido bien.
_CONDICIONES_QUE_CORREN_TRAS_UN_FALLO = frozenset({"!cancelled()", "always()"})


def _corre_tras_un_fallo(condicion: str) -> bool:
    """``True`` si ``condicion`` es exactamente ``!cancelled()`` o ``always()``, con o sin ``${{ }}`` ni espacios."""
    sin_espacios = re.sub(r"\s+", "", condicion)
    if sin_espacios.startswith("${{") and sin_espacios.endswith("}}"):
        sin_espacios = sin_espacios[3:-2]
    return sin_espacios in _CONDICIONES_QUE_CORREN_TRAS_UN_FALLO


def _usos_del_ejecutable(steps: list[dict[str, Any]], desde: int, excluidos: set[int]) -> list[int]:
    """Índices (posteriores a ``desde``) de los pasos que mencionan al ejecutable en cualquier campo."""
    return [
        indice
        for indice, step in enumerate(steps)
        if indice > desde and indice not in excluidos and "SkyClawApp.exe" in repr(step)
    ]


def test_todo_productor_registra_el_sha256_del_ejecutable_antes_de_usarlo() -> None:
    """Enumera a TODOS los productores (no una lista a mano): un productor nuevo rompe el ancla hasta registrar el hash."""
    productores = _productores_de_ejecutable(_cargar_workflows())

    assert set(productores) == PRODUCTORES_ESPERADOS | PRODUCTORES_SIN_PUBLICACION
    for (nombre_workflow, _nombre_job), steps in sorted(productores.items()):
        build_index = _indice_unico(
            steps,
            lambda step: "pyinstaller sky_claw.spec" in step.get("run", "").lower(),
            "de construcción PyInstaller",
        )
        registro_index = _indice_unico(
            steps,
            lambda step: COMANDO_REGISTRO.fullmatch(step.get("run", "").strip()) is not None,
            f"de registro del SHA-256 del ejecutable ({nombre_workflow})",
        )
        assert build_index < registro_index, f"{nombre_workflow}: el hash se registra después de construir"
        # Ningún paso usa el ejecutable entre que se construye y que se hashea (smoke, firma, subida, pruebas).
        previos = [
            indice for indice in _usos_del_ejecutable(steps, build_index, {registro_index}) if indice < registro_index
        ]
        assert previos == [], f"{nombre_workflow}: pasos que usan el ejecutable antes de hashearlo: {previos}"


def test_el_registro_del_laboratorio_se_sube_como_artefacto_de_resultados() -> None:
    """El hash que no queda guardado no es evidencia: su ruta debe caer en la lista de artefactos permitidos."""
    productores = _productores_de_ejecutable(_cargar_workflows())

    for clave in sorted(PRODUCTORES_CON_VERIFICACION):
        steps = productores[clave]
        registro = next(
            coincidencia
            for step in steps
            if (coincidencia := COMANDO_REGISTRO.fullmatch(step.get("run", "").strip())) is not None
        )
        ruta = registro.group("json")
        assert ruta is not None, f"{clave}: el registro del laboratorio debe guardar su JSON (--json)"
        assert any(fnmatch.fnmatch(ruta, patron) for patron in RUTAS_DE_ARTEFACTO_PERMITIDAS), (clave, ruta)


def test_el_laboratorio_vuelve_a_verificar_el_hash_despues_del_ultimo_uso_del_ejecutable() -> None:
    productores = _productores_de_ejecutable(_cargar_workflows())

    for clave in sorted(PRODUCTORES_CON_VERIFICACION):
        steps = productores[clave]
        registro_index = _indice_unico(
            steps,
            lambda step: COMANDO_REGISTRO.fullmatch(step.get("run", "").strip()) is not None,
            "de registro",
        )
        verificacion_index = _indice_unico(
            steps,
            lambda step: COMANDO_VERIFICACION.fullmatch(step.get("run", "").strip()) is not None,
            "de verificación del SHA-256",
        )
        usos = _usos_del_ejecutable(steps, registro_index, {verificacion_index})
        assert usos, f"{clave}: el laboratorio debería usar el ejecutable entre el registro y la verificación"
        assert verificacion_index > max(usos), f"{clave}: se verifica antes del último uso del ejecutable"
        # Debe correr también cuando un paso previo falló: es justo cuando más importa saber qué se probó.
        condicion = str(steps[verificacion_index].get("if", ""))
        assert _corre_tras_un_fallo(condicion), (clave, condicion)


# ---------------------------------------------------------------------------
# La condición «corre también tras un fallo» se compara EXACTA, no por subcadena (review CodeRabbit, #888)
# ---------------------------------------------------------------------------

CONDICIONES_QUE_CORREN_TRAS_UN_FALLO = [
    "!cancelled()",
    "${{ !cancelled() }}",
    "${{!cancelled()}}",
    "  ${{   !  cancelled( )  }}  ",
    "always()",
    "${{ always() }}",
]
#: Las que una comparación por subcadena (``"cancelled()" in condicion``) dejaría pasar y que SALTEAN el paso
#: cuando un paso previo falla: ``cancelled()`` solo es verdadero si cancelaron el job, y ``success() && ...``
#: exige que todo lo anterior haya salido bien.
CONDICIONES_QUE_SALTEAN_EL_PASO_TRAS_UN_FALLO = [
    "",
    "cancelled()",
    "${{ cancelled() }}",
    "${{ success() && !cancelled() }}",
    "${{ success() }}",
    "${{ !cancelled() && success() }}",
    "${{ always() && success() }}",
    "${{ failure() }}",
]


def test_la_condicion_tras_un_fallo_se_compara_exacta_y_no_por_subcadena() -> None:
    for condicion in CONDICIONES_QUE_CORREN_TRAS_UN_FALLO:
        assert _corre_tras_un_fallo(condicion), condicion
    for condicion in CONDICIONES_QUE_SALTEAN_EL_PASO_TRAS_UN_FALLO:
        assert not _corre_tras_un_fallo(condicion), condicion
