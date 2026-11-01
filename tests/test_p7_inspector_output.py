"""Pruebas de la salida del inspector P7 (FIX 2 del PR #851).

El inspector es READ-ONLY sobre el runtime: su única escritura es el informe.
Que esa escritura no pueda caer sobre una superficie que el propio inspector
audita es parte del contrato.

El defecto que estas pruebas cierran es concreto: la versión anterior hacía
``args.json_salida.write_text(...)``, así que
``--json-salida <instancia>/modorganizer.ini`` **truncaba el INI** y
``--json-salida <storage>/active.json`` **pisaba el Desired Runtime**. El
inspector destruía exactamente lo que venía a medir.

Todos los casos son fail-closed: ante duda, no se escribe.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

RAIZ_REPO = pathlib.Path(__file__).resolve().parents[1]
RUTA_INSPECTOR = RAIZ_REPO / "scripts" / "p7_readonly_inspection.py"


def _cargar_inspector():  # type: ignore[no-untyped-def]
    """Carga el script como módulo.

    Se registra en ``sys.modules`` ANTES de ``exec_module``: ``dataclasses`` busca
    el módulo de la clase en ``sys.modules`` para resolver anotaciones, y sin el
    registro falla con ``AttributeError: 'NoneType' object has no attribute
    '__dict__'`` durante la colección.
    """
    nombre = "p7_readonly_inspection_bajo_prueba"
    spec = importlib.util.spec_from_file_location(nombre, RUTA_INSPECTOR)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = modulo
    spec.loader.exec_module(modulo)
    return modulo


inspector = _cargar_inspector()


class Arboles:
    """Árboles protegidos sintéticos, con la misma forma que usa ``main``."""

    def __init__(self, base: pathlib.Path) -> None:
        self.mo2 = base / "MO2"
        self.storage = base / "Storage"
        self.clone = base / "Clones" / "Clone1"
        self.managed = base / "Steam" / "steamapps" / "common" / "Skyrim Special Edition"
        self.permitido = base / "informes"
        for ruta in (self.mo2, self.storage, self.clone, self.managed, self.permitido):
            ruta.mkdir(parents=True, exist_ok=True)
        (self.mo2 / "modorganizer.ini").write_text("[General]\r\ngamePath=x\r\n", encoding="utf-8", newline="")
        (self.storage / "active.json").write_text("{}", encoding="utf-8")

    def protegidos(self) -> list[tuple[pathlib.Path, str]]:
        return inspector._arboles_protegidos(
            storage_root=self.storage,
            mo2_instance_dir=self.mo2,
            clone_roots=[self.clone],
            managed_source_root=self.managed,
        )


def _validar(arboles: Arboles, destino: pathlib.Path) -> pathlib.Path:
    return inspector.validar_destino_informe(destino, arboles_protegidos=arboles.protegidos())


# ---------------------------------------------------------------------------
# Los siete casos exigidos
# ---------------------------------------------------------------------------


def test_rechaza_el_destino_que_es_el_modorganizer_ini(tmp_path: pathlib.Path) -> None:
    """El caso más grave: el informe truncaría el INI que el inspector audita."""
    arboles = Arboles(tmp_path)
    ini = arboles.mo2 / "modorganizer.ini"
    antes = ini.read_bytes()
    with pytest.raises(inspector.SalidaNoPermitidaError, match="modorganizer.ini|instancia MO2"):
        _validar(arboles, ini)
    # Y el INI sigue intacto: la validación no toca nada.
    assert ini.read_bytes() == antes


def test_rechaza_el_destino_que_es_active_json(tmp_path: pathlib.Path) -> None:
    """``active.json`` es el Desired Runtime: pisarlo revierte el estado durable."""
    arboles = Arboles(tmp_path)
    with pytest.raises(inspector.SalidaNoPermitidaError, match="storage|Frozen Runtime"):
        _validar(arboles, arboles.storage / "active.json")


def test_rechaza_un_destino_dentro_del_runtime_clone(tmp_path: pathlib.Path) -> None:
    arboles = Arboles(tmp_path)
    with pytest.raises(inspector.SalidaNoPermitidaError, match="Runtime Clone"):
        _validar(arboles, arboles.clone / "SkyrimSE.exe")


def test_rechaza_un_destino_dentro_de_la_managed_source(tmp_path: pathlib.Path) -> None:
    """Escribir en la instalación de Steam es justo lo que SFR-11 prohíbe."""
    arboles = Arboles(tmp_path)
    with pytest.raises(inspector.SalidaNoPermitidaError, match="Managed Source"):
        _validar(arboles, arboles.managed / "informe.json")


def test_rechaza_sobrescribir_un_informe_existente(tmp_path: pathlib.Path) -> None:
    """Un informe no pisa ni siquiera a otro informe."""
    arboles = Arboles(tmp_path)
    existente = arboles.permitido / "informe.json"
    existente.write_text("previo", encoding="utf-8")
    with pytest.raises(inspector.SalidaNoPermitidaError, match="ya existe"):
        _validar(arboles, existente)
    assert existente.read_text(encoding="utf-8") == "previo"


def test_rechaza_un_junction_que_apunta_a_un_arbol_protegido(tmp_path: pathlib.Path) -> None:
    """Un junction dentro de un directorio permitido no puede sortear el prefijo.

    Sin resolver el enlace, la comparación de prefijos diría «está dentro de
    ``informes/``» y escribiría en el storage. Se resuelve primero.
    """
    arboles = Arboles(tmp_path)
    enlace = arboles.permitido / "atajo"
    creacion = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(enlace), str(arboles.storage)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if creacion.returncode != 0 or not enlace.exists():
        pytest.skip(f"no se pudo crear el junction en esta máquina: {creacion.stderr[-200:]}")
    try:
        with pytest.raises(inspector.SalidaNoPermitidaError, match="enlace|reparse|storage"):
            _validar(arboles, enlace / "informe.json")
    finally:
        os.rmdir(enlace)


def test_acepta_un_destino_nuevo_fuera_de_los_arboles_protegidos(tmp_path: pathlib.Path) -> None:
    """Control positivo: sin él, un guard que rechaza todo pasaría los seis negativos."""
    arboles = Arboles(tmp_path)
    destino = arboles.permitido / "informe-nuevo.json"
    canonico = _validar(arboles, destino)
    assert canonico.name == "informe-nuevo.json"
    inspector.escribir_informe_exclusivo(canonico, '{"ok": true}\n')
    assert canonico.read_text(encoding="utf-8") == '{"ok": true}\n'


# ---------------------------------------------------------------------------
# La escritura en sí
# ---------------------------------------------------------------------------


def test_la_escritura_no_sobrescribe_aunque_el_archivo_aparezca_despues(tmp_path: pathlib.Path) -> None:
    """``O_EXCL`` cierra la ventana entre validar y escribir.

    La validación y la escritura son dos llamadas: entre ellas el archivo puede
    aparecer. ``write_text`` lo truncaría; ``O_CREAT|O_EXCL`` no.
    """
    arboles = Arboles(tmp_path)
    destino = _validar(arboles, arboles.permitido / "carrera.json")
    # Alguien lo crea justo después de la validación.
    destino.write_text("ajeno", encoding="utf-8")
    with pytest.raises(inspector.SalidaNoPermitidaError, match="apareció"):
        inspector.escribir_informe_exclusivo(destino, "{}\n")
    assert destino.read_text(encoding="utf-8") == "ajeno"


def test_un_destino_invalido_devuelve_codigo_2_y_no_escribe(tmp_path: pathlib.Path) -> None:
    """Por la CLI: el informe sale por stdout y el fallo es explícito."""
    arboles = Arboles(tmp_path)
    ini = arboles.mo2 / "modorganizer.ini"
    antes = ini.read_bytes()
    proc = subprocess.run(
        [
            sys.executable,
            str(RUTA_INSPECTOR),
            "--storage-root",
            str(arboles.storage),
            "--mo2-instance",
            str(arboles.mo2),
            "--json-salida",
            str(ini),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(RAIZ_REPO),
    )
    assert proc.returncode == 2, f"se esperaba 2 (salida rechazada), se obtuvo {proc.returncode}: {proc.stderr[-400:]}"
    assert "ERROR" in proc.stderr
    assert ini.read_bytes() == antes, "el INI fue modificado por el inspector"


def test_un_destino_valido_por_la_cli_escribe_el_informe(tmp_path: pathlib.Path) -> None:
    """Control positivo por la CLI: un destino permitido se escribe de verdad.

    Sin este caso, un validador que rechazara TODO pasaría los tests negativos y
    rompería la herramienta en el único uso que tiene.
    """
    arboles = Arboles(tmp_path)
    destino = arboles.permitido / "informe-cli.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(RUTA_INSPECTOR),
            "--storage-root",
            str(arboles.storage),
            "--mo2-instance",
            str(arboles.mo2),
            "--json-salida",
            str(destino),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(RAIZ_REPO),
    )
    assert proc.returncode in (0, 1), f"la CLI falló por un motivo inesperado: {proc.stderr[-400:]}"
    assert destino.is_file(), "el informe no se escribió"
    contenido = json.loads(destino.read_text(encoding="utf-8"))
    assert "veredicto" in contenido
    assert contenido["rutas_completas"] is False, "por defecto el informe NO debe incluir rutas completas"
