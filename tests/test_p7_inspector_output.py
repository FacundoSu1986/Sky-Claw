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


def _correr_cli(arboles: Arboles, destino: pathlib.Path, *, ioencoding: str) -> subprocess.CompletedProcess[str]:
    entorno = dict(os.environ)
    entorno["PYTHONIOENCODING"] = ioencoding
    return subprocess.run(
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
        encoding="utf-8",
        errors="replace",
        timeout=300,
        cwd=str(RAIZ_REPO),
        env=entorno,
    )


def test_la_cli_sobrevive_a_una_consola_cp1252(tmp_path: pathlib.Path) -> None:
    """El informe contiene «→» y acentos; una consola cp1252 no puede imprimirlos.

    Regresión real: en ``windows-latest`` la consola es cp1252 y ``print`` lanzaba
    ``UnicodeEncodeError`` ANTES de escribir el archivo, así que el informe nunca
    se producía. Localmente la consola era UTF-8 y el test pasaba.

    Se fuerza la página de códigos con ``PYTHONIOENCODING`` para que el caso sea
    reproducible fuera de Windows.
    """
    arboles = Arboles(tmp_path)
    destino = arboles.permitido / "informe-cp1252.json"
    proc = _correr_cli(arboles, destino, ioencoding="cp1252")
    assert "UnicodeEncodeError" not in proc.stderr, f"la CLI reventó por codificación: {proc.stderr[-400:]}"
    assert destino.is_file(), f"el informe no se escribió con consola cp1252: {proc.stderr[-400:]}"
    contenido = json.loads(destino.read_text(encoding="utf-8"))
    assert "veredicto" in contenido


def test_la_cli_rechaza_el_destino_invalido_tambien_con_cp1252(tmp_path: pathlib.Path) -> None:
    """El rechazo no puede depender de que la consola sepa imprimir el informe."""
    arboles = Arboles(tmp_path)
    ini = arboles.mo2 / "modorganizer.ini"
    antes = ini.read_bytes()
    proc = _correr_cli(arboles, ini, ioencoding="cp1252")
    assert proc.returncode == 2, f"se esperaba 2, se obtuvo {proc.returncode}: {proc.stderr[-400:]}"
    assert ini.read_bytes() == antes


def test_el_journal_pendiente_bloquea_con_su_valor_real(tmp_path: pathlib.Path) -> None:
    """``JournalDisposition`` usa MAYÚSCULAS: ``PENDING_PROMOTION``.

    El chequeo comparaba con ``startswith("pending")``, que no matchea nunca, así
    que una transición interrumpida se reportaba OK y el informe podía declarar
    ``READY_FOR_P7`` sobre un PENDING (Codex sobre #851).
    """
    from sky_claw.local.frozen_runtime import journal as journal_module

    for disposicion in (
        journal_module.JournalDisposition.PENDING_PROMOTION,
        journal_module.JournalDisposition.PENDING_ROLLBACK,
    ):
        cargado = type("Cargado", (), {"disposition": disposicion})()
        original = inspector.cargar_journal_transicion
        inspector.cargar_journal_transicion = lambda _root, _c=cargado: _c
        try:
            hallazgo = inspector._estado_del_journal(tmp_path)
        finally:
            inspector.cargar_journal_transicion = original
        assert hallazgo.veredicto == inspector.HALLAZGO_BLOQUEADO, (
            f"{disposicion.value} se reportó como {hallazgo.veredicto}"
        )


def test_los_registros_de_recuperacion_se_buscan_donde_viven(tmp_path: pathlib.Path) -> None:
    """Viven en ``state/recovery/<id>.json``, no con prefijo ``binder_recovery``."""
    from sky_claw.local.frozen_runtime.storage import recovery_state_dir

    carpeta = recovery_state_dir(tmp_path)
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / "t-123.json").write_text("{}", encoding="utf-8")
    hallazgo = inspector._estado_de_recuperacion(tmp_path)
    assert hallazgo.veredicto == inspector.HALLAZGO_BLOQUEADO, (
        f"un registro de recuperación pendiente se reportó como {hallazgo.veredicto}: {hallazgo.detalle}"
    )


def test_listo_para_p7_concuerda_con_el_veredicto(tmp_path: pathlib.Path) -> None:
    """El booleano y el código de salida no pueden contradecir al veredicto."""
    arboles = Arboles(tmp_path)
    destino = arboles.permitido / "informe-veredicto.json"
    _correr_cli(arboles, destino, ioencoding="utf-8")
    contenido = json.loads(destino.read_text(encoding="utf-8"))
    assert contenido["listo_para_p7"] is (contenido["veredicto"] == "READY_FOR_P7"), (
        f"listo_para_p7={contenido['listo_para_p7']} con veredicto={contenido['veredicto']}"
    )


def test_los_detalles_de_los_hallazgos_no_publican_rutas(tmp_path: pathlib.Path) -> None:
    """Los ``detalle`` vienen del preflight con rutas completas embebidas.

    Redactar sólo los campos de primer nivel dejaba el árbol privado en
    ``hallazgos[].detalle`` (Codex sobre #851).
    """
    arboles = Arboles(tmp_path)
    # Sin INI: el preflight emite un detalle con la ruta de la instancia MO2.
    (arboles.mo2 / "modorganizer.ini").unlink()
    destino = arboles.permitido / "informe-detalles.json"
    _correr_cli(arboles, destino, ioencoding="utf-8")
    texto = destino.read_text(encoding="utf-8")
    assert str(tmp_path) not in texto, "una ruta privada completa apareció en el informe"
    contenido = json.loads(texto)
    assert contenido["rutas_completas"] is False


@pytest.mark.skipif(os.name != "nt", reason="mklink /J es de Windows")
def test_una_raiz_protegida_por_junction_no_habilita_la_escritura(tmp_path: pathlib.Path) -> None:
    """Si ``--storage-root`` llega por un junction, el destino físico es el mismo árbol.

    Comparar la raíz sin resolver la dejaba con el alias mientras el destino se
    resolvía a la ruta física: el prefijo no coincidía y la escritura pasaba
    (Codex sobre #851).
    """
    real = tmp_path / "storage-real"
    real.mkdir()
    alias = tmp_path / "storage-alias"
    creacion = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(alias), str(real)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if creacion.returncode != 0 or not alias.exists():
        pytest.skip(f"no se pudo crear el junction: {creacion.stderr[-200:]}")
    try:
        permitido = tmp_path / "informes"
        permitido.mkdir()
        protegidos = inspector._arboles_protegidos(
            storage_root=alias,
            mo2_instance_dir=tmp_path / "MO2",
            clone_roots=[],
            managed_source_root=None,
        )
        with pytest.raises(inspector.SalidaNoPermitidaError):
            inspector.validar_destino_informe(real / "informe.json", arboles_protegidos=protegidos)
    finally:
        os.rmdir(alias)


def test_la_managed_source_descubierta_tambien_protege_el_destino(tmp_path: pathlib.Path) -> None:
    """Omitir ``--managed-source`` no puede dejar la Steam descubierta escribible.

    El preflight descubre la instalación y la devuelve en
    ``evaluacion.managed_source_root``. Proteger sólo ``args.managed_source``
    dejaba `--json-salida <steam>/informe.json` aceptado (Codex sobre #851).
    """
    arboles = Arboles(tmp_path)
    descubierta = arboles.managed
    destino = descubierta / "informe.json"

    original = inspector.construir_informe

    def _falso(**_: object):  # type: ignore[no-untyped-def]
        return {"veredicto": "BLOCKED", "listo_para_p7": False, "clone_objetivo": None, "hallazgos": []}, descubierta

    inspector.construir_informe = _falso
    try:
        codigo = inspector.main(
            [
                "--storage-root",
                str(arboles.storage),
                "--mo2-instance",
                str(arboles.mo2),
                "--json-salida",
                str(destino),
            ]
        )
    finally:
        inspector.construir_informe = original

    assert codigo == 2, f"se aceptó escribir dentro de la Managed Source descubierta (código {codigo})"
    assert not destino.exists()


def test_el_desired_sin_raiz_resuelta_no_habilita_p7(tmp_path: pathlib.Path) -> None:
    """Tener ``clone_id`` no alcanza: sin ``clone_root`` el runtime no se resuelve.

    ``desired_runtime_from_storage`` devuelve ``clone_root=None`` cuando la
    Generation no es del linaje del clone. Clasificarlo OK por tener id permitía
    ``READY_FOR_P7`` sobre un Desired inconsistente (Codex sobre #851).
    """
    from sky_claw.local.frozen_runtime.effective_runtime.models import DesiredRuntime

    arboles = Arboles(tmp_path)
    original = inspector.desired_runtime_from_storage
    inspector.desired_runtime_from_storage = lambda _root: DesiredRuntime(
        clone_id="clone-1", clone_root=None, generation_id="gen-otra"
    )
    try:
        informe, _ = inspector.construir_informe(
            storage_root=arboles.storage,
            mo2_instance_dir=arboles.mo2,
            config_path=None,
            managed_source_root=None,
            clone_id=None,
            mostrar_rutas=False,
        )
    finally:
        inspector.desired_runtime_from_storage = original

    hallazgo = next(h for h in informe["hallazgos"] if h["clave"] == "desired_runtime")
    assert hallazgo["veredicto"] == inspector.HALLAZGO_BLOQUEADO, (
        f"un Desired sin raíz resuelta se reportó como {hallazgo['veredicto']}"
    )
    assert informe["listo_para_p7"] is False


def test_las_dependencias_se_detectan_con_cualquier_separador(tmp_path: pathlib.Path) -> None:
    """Una ruta escrita con ``/`` o ``\\`` tiene que ser detectada igual.

    Codex tercera ronda (#851): ``str(Path)`` en Windows devuelve barras
    invertidas, pero los TOML pueden usar barras normales o duplicarlas. Sin
    normalizar, la dependencia pasa desapercibida y P7 declara OK.
    """
    managed = tmp_path / "Steam" / "Skyrim Special Edition"
    managed.mkdir(parents=True, exist_ok=True)

    plugins = tmp_path / "Data" / "SKSE" / "Plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    toml = plugins / "EngineFixes.toml"

    # Forma POSIX
    toml.write_text(
        f'skyrim_path = "{str(managed).replace(chr(92), "/")}"\n',
        encoding="utf-8",
    )
    sospechosos = inspector._dependencias_en_arbol(tmp_path, inspector._variantes_de_ruta_para_busqueda(managed))
    assert any("EngineFixes.toml" in s for s in sospechosos), (
        f"la dependencia con separadores '/' no se detectó: {sospechosos}"
    )

    # Forma con barras invertidas escapadas (estilo TOML/JSON raw)
    toml.write_text(
        f'skyrim_path = "{str(managed).replace(chr(92), chr(92) + chr(92))}"\n',
        encoding="utf-8",
    )
    sospechosos2 = inspector._dependencias_en_arbol(tmp_path, inspector._variantes_de_ruta_para_busqueda(managed))
    assert any("EngineFixes.toml" in s for s in sospechosos2), (
        f"la dependencia con separadores '\\\\' no se detectó: {sospechosos2}"
    )
