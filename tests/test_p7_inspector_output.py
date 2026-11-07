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


# ---------------------------------------------------------------------------
# FIX A — el inspector NO escribe: el informe sale por stdout
# ---------------------------------------------------------------------------


def test_el_inspector_no_expone_ninguna_funcion_de_escritura_de_informes() -> None:
    """Las funciones del escritor anterior ya no existen, ni por accidente.

    Un ``validar_destino_informe`` que sobreviva sin llamadores es una invitación
    a re-cablearlo. Si alguien lo necesita, tiene que volver a discutir el
    TOCTOU, no encontrarlo ya escrito.
    """
    for nombre in (
        "validar_destino_informe",
        "escribir_informe_exclusivo",
        "SalidaNoPermitidaError",
        "_arboles_protegidos",
    ):
        assert not hasattr(inspector, nombre), f"sobrevive {nombre}: el camino de escritura no se retiró entero"


def test_la_cli_rechaza_json_salida(tmp_path: pathlib.Path) -> None:
    """``--json-salida`` ya no es una opción: argparse la rechaza con código 2."""
    arboles = Arboles(tmp_path)
    destino = arboles.permitido / "informe.json"
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
    assert proc.returncode == 2, f"se esperaba 2 (opción inexistente), se obtuvo {proc.returncode}"
    assert "unrecognized arguments" in proc.stderr
    assert not destino.exists(), "el inspector escribió un archivo"


def test_la_cli_emite_el_informe_por_stdout(tmp_path: pathlib.Path) -> None:
    """Control positivo: sin archivo de salida, el JSON sale por stdout."""
    arboles = Arboles(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            str(RUTA_INSPECTOR),
            "--storage-root",
            str(arboles.storage),
            "--mo2-instance",
            str(arboles.mo2),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(RAIZ_REPO),
    )
    assert proc.returncode in (0, 1), f"la CLI falló por un motivo inesperado: {proc.stderr[-400:]}"
    contenido = json.loads(proc.stdout)
    assert "veredicto" in contenido
    assert contenido["rutas_completas"] is False, "por defecto el informe NO debe incluir rutas completas"


def test_la_cli_sobrevive_a_una_consola_cp1252(tmp_path: pathlib.Path) -> None:
    """El informe contiene «→» y acentos; una consola cp1252 no puede imprimirlos.

    ``sys.stdout`` usa ``strict`` por defecto en Windows y lanza
    ``UnicodeEncodeError``. Se emite ASCII puro (``ensure_ascii=True``) y el JSON
    sigue siendo válido.
    """
    arboles = Arboles(tmp_path)
    entorno = dict(os.environ)
    entorno["PYTHONIOENCODING"] = "cp1252"
    proc = subprocess.run(
        [
            sys.executable,
            str(RUTA_INSPECTOR),
            "--storage-root",
            str(arboles.storage),
            "--mo2-instance",
            str(arboles.mo2),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        cwd=str(RAIZ_REPO),
        env=entorno,
    )
    assert proc.returncode in (0, 1), f"la CLI murió con una consola cp1252: {proc.stderr[-400:]}"
    assert "UnicodeEncodeError" not in proc.stderr
    contenido = json.loads(proc.stdout)
    assert "veredicto" in contenido


def test_la_cli_rechaza_una_redireccion_a_un_arbol_protegido_no_aplica(tmp_path: pathlib.Path) -> None:
    """La redirección la hace el shell: el inspector no puede —ni debe— impedirla.

    Este test documenta el límite del contrato en vez de fingir una garantía. El
    inspector no abre archivos; si el operador redirige stdout al INI, el daño lo
    hace su shell, no la herramienta. Lo que la herramienta garantiza es que ella
    no elige el destino.
    """
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
        ],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(RAIZ_REPO),
    )
    assert proc.returncode in (0, 1)
    assert ini.read_bytes() == antes, "el inspector escribió sobre el INI sin que se le pidiera"


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


def _correr_cli(arboles: Arboles) -> subprocess.CompletedProcess[str]:
    """Corre el inspector y devuelve el proceso con el JSON en stdout."""
    return subprocess.run(
        [
            sys.executable,
            str(RUTA_INSPECTOR),
            "--storage-root",
            str(arboles.storage),
            "--mo2-instance",
            str(arboles.mo2),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        cwd=str(RAIZ_REPO),
    )


def test_listo_para_p7_concuerda_con_el_veredicto(tmp_path: pathlib.Path) -> None:
    """El booleano y el código de salida no pueden contradecir al veredicto."""
    arboles = Arboles(tmp_path)
    proc = _correr_cli(arboles)
    contenido = json.loads(proc.stdout)
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
    proc = _correr_cli(arboles)
    contenido = json.loads(proc.stdout)

    # La comparación va sobre el JSON DECODIFICADO, no sobre el texto crudo.
    # ``json.dumps(ensure_ascii=True)`` escapa cada barra invertida de una ruta
    # de Windows como ``\\``, así que ``str(tmp_path)`` no aparece nunca en
    # ``proc.stdout`` y la aserción pasaba aunque el detalle publicara el árbol
    # privado entero. Un assert que no puede fallar no es un ancla (CodeRabbit
    # sobre #851).
    detalles = "\n".join(str(h.get("detalle", "")) for h in contenido["hallazgos"])
    assert str(tmp_path) not in detalles, f"una ruta privada completa apareció en un detalle: {detalles}"
    assert str(arboles.mo2) not in detalles, f"la ruta de la instancia MO2 apareció sin redactar: {detalles}"
    assert contenido["rutas_completas"] is False


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


def test_el_detalle_de_dependencias_se_redacta_aunque_la_source_se_descubra() -> None:
    """El detalle nombra la ruta: tiene que redactarse con CUALQUIER variante.

    ``_dependencias_en_arbol`` devuelve ``"<archivo> menciona <aguja>"``, y la
    aguja es una ruta completa. Si la Managed Source la DESCUBRIÓ el preflight
    —no vino por ``--managed-source``—, no está en ``conocidas``; la redacción
    depende entonces del patrón de ruta absoluta. Se prueba la unidad, no el
    informe entero: con un storage sin Clone el bloque de dependencias ni
    siquiera corre y el test sería vacuo.
    """
    source = pathlib.Path("G:/Steam/steamapps/common/Skyrim Special Edition")
    detalle = inspector._dependencias_en_arbol.__name__  # sanity: la función existe
    assert detalle == "_dependencias_en_arbol"

    agujas = inspector._variantes_de_ruta_para_busqueda(source)
    assert agujas, "sin variantes el chequeo de dependencias sería vacuo"

    # Cada variante, embebida en un detalle, tiene que salir redactada.
    for aguja in agujas:
        texto = f"Data/SKSE/Plugins/EngineFixes.toml menciona {aguja}"
        redactado = inspector._redactar_texto(texto, lambda _: "<ruta>", [])
        assert aguja not in redactado, f"la variante {aguja!r} sobrevivió a la redacción: {redactado}"

    # Y con la raíz declarada en ``conocidas`` tampoco.
    texto = f"EngineFixes.toml menciona {source}"
    redactado = inspector._redactar_texto(texto, lambda _: "<ruta>", [source])
    assert str(source) not in redactado


# ---------------------------------------------------------------------------
# FIX 1 de la auditoría de cierre — un candidato sin inspeccionar NO es «OK»
# ---------------------------------------------------------------------------
#
# El defecto: ``_dependencias_en_arbol`` devolvía una lista de sospechosos y
# colapsaba en el mismo ``continue`` tres situaciones distintas —no existe,
# no es un archivo regular, excede el límite de tamaño, no se pudo leer—. El
# llamador convertía la lista vacía en ``HALLAZGO_OK``. Un ``ModOrganizer.ini``
# de 600 KiB, o uno con una ACL que impide leerlo, producía «ningún archivo de
# configuración nombra la Managed Source» sin haber leído el archivo. El
# inspector afirmaba sobre lo que no había mirado.

#: Los tres candidatos que el inspector mira. Se replican acá a propósito: si
#: la lista cambia, el test tiene que romperse para que alguien decida qué
#: hacer con el candidato nuevo, en vez de que quede sin cobertura en silencio.
CANDIDATOS_ESPERADOS = ("SKSE/Plugins/CrashLogger.ini", "Data/SKSE/Plugins/EngineFixes.toml", "ModOrganizer.ini")


def _clone_vacio(base: pathlib.Path) -> pathlib.Path:
    """Un Clone sin ninguno de los candidatos: ausencia verificada de los tres."""
    clone = base / "Clone"
    clone.mkdir(parents=True, exist_ok=True)
    return clone


def test_los_candidatos_son_los_que_el_test_cubre() -> None:
    """El conjunto de candidatos está congelado: agregar uno rompe acá."""
    clone = pathlib.Path("no-existe")
    inspeccion = inspector._dependencias_en_arbol(clone, ("x",))
    assert inspeccion.candidatos == CANDIDATOS_ESPERADOS


def test_un_candidato_ausente_es_ausencia_verificada_no_un_fallo(tmp_path: pathlib.Path) -> None:
    """Sin archivos, no hay nada que inspeccionar: no es lo mismo que no poder."""
    clone = _clone_vacio(tmp_path)
    inspeccion = inspector._dependencias_en_arbol(clone, ("C:/Steam/Skyrim",))
    assert inspeccion.inspeccionados == ()
    assert inspeccion.no_inspeccionados == (), f"la ausencia se reportó como fallo: {inspeccion.no_inspeccionados}"
    assert inspeccion.sospechosas == ()


def test_un_candidato_leido_sin_dependencias_se_declara_inspeccionado(tmp_path: pathlib.Path) -> None:
    """Leer y no encontrar nada es una afirmación POSITIVA, no un vacío."""
    clone = _clone_vacio(tmp_path)
    (clone / "ModOrganizer.ini").write_text("[General]\r\ngamePath=C:/otro\r\n", encoding="utf-8", newline="")
    inspeccion = inspector._dependencias_en_arbol(clone, ("C:/Steam/Skyrim",))
    assert inspeccion.inspeccionados == ("ModOrganizer.ini",)
    assert inspeccion.no_inspeccionados == ()
    assert inspeccion.sospechosas == ()


def test_un_candidato_que_menciona_la_aguja_es_sospechoso(tmp_path: pathlib.Path) -> None:
    """Control positivo del detector: si no marca esto, los demás tests son vacuos."""
    clone = _clone_vacio(tmp_path)
    (clone / "ModOrganizer.ini").write_text("[General]\r\ngamePath=C:/Steam/Skyrim\r\n", encoding="utf-8", newline="")
    inspeccion = inspector._dependencias_en_arbol(clone, ("C:/Steam/Skyrim",))
    assert len(inspeccion.sospechosas) == 1
    assert "ModOrganizer.ini" in inspeccion.sospechosas[0]
    assert inspeccion.inspeccionados == ("ModOrganizer.ini",)


def test_un_candidato_ilegible_no_se_da_por_inspeccionado(tmp_path: pathlib.Path, monkeypatch) -> None:
    """Un archivo que existe y no se puede leer deja el chequeo INDETERMINADO.

    Es el caso que el defecto silenciaba: ``except OSError: continue`` producía
    una lista vacía y el llamador la leía como «no hay dependencias».
    """
    clone = _clone_vacio(tmp_path)
    (clone / "ModOrganizer.ini").write_text("[General]\r\n", encoding="utf-8", newline="")

    original = pathlib.Path.open

    def _open_que_niega(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.name == "ModOrganizer.ini":
            raise PermissionError(13, "ACCESS_DENIED")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", _open_que_niega)
    inspeccion = inspector._dependencias_en_arbol(clone, ("C:/Steam/Skyrim",))

    assert inspeccion.inspeccionados == ()
    assert [rel for rel, _ in inspeccion.no_inspeccionados] == ["ModOrganizer.ini"]
    assert inspeccion.no_inspeccionados[0][1], "un fallo sin motivo no es un diagnóstico"


def test_un_candidato_que_excede_el_limite_no_se_da_por_inspeccionado(tmp_path: pathlib.Path) -> None:
    """El límite de rendimiento se conserva, pero deja de ser silencioso.

    No recorrer el árbol de mods es un requisito; convertir el recorte en un
    «OK» no lo es. El archivo se declara NO inspeccionado con su motivo.
    """
    clone = _clone_vacio(tmp_path)
    limite = inspector.LIMITE_DE_LECTURA_CANDIDATO
    grande = clone / "ModOrganizer.ini"
    grande.write_bytes(b"x" * (limite + 1))

    inspeccion = inspector._dependencias_en_arbol(clone, ("C:/Steam/Skyrim",))
    assert inspeccion.inspeccionados == ()
    assert [rel for rel, _ in inspeccion.no_inspeccionados] == ["ModOrganizer.ini"]
    motivo = inspeccion.no_inspeccionados[0][1]
    assert str(limite) in motivo, f"el motivo no dice el límite: {motivo}"


def test_un_candidato_justo_en_el_limite_si_se_inspecciona(tmp_path: pathlib.Path) -> None:
    """Control negativo del límite: el borde exacto no es «excedido»."""
    clone = _clone_vacio(tmp_path)
    limite = inspector.LIMITE_DE_LECTURA_CANDIDATO
    (clone / "ModOrganizer.ini").write_bytes(b"x" * limite)
    inspeccion = inspector._dependencias_en_arbol(clone, ("C:/Steam/Skyrim",))
    assert inspeccion.inspeccionados == ("ModOrganizer.ini",)
    assert inspeccion.no_inspeccionados == ()


def test_un_candidato_que_no_es_archivo_regular_no_se_inspecciona(tmp_path: pathlib.Path) -> None:
    """Un directorio con el nombre del candidato es anómalo, no «ausente».

    ``Path.is_file()`` devuelve ``False`` para un directorio, y el ``continue``
    anterior lo trataba igual que un archivo que no existe.
    """
    clone = _clone_vacio(tmp_path)
    (clone / "ModOrganizer.ini").mkdir()

    inspeccion = inspector._dependencias_en_arbol(clone, ("C:/Steam/Skyrim",))
    assert inspeccion.inspeccionados == ()
    assert [rel for rel, _ in inspeccion.no_inspeccionados] == ["ModOrganizer.ini"]


def test_el_hallazgo_de_dependencias_es_indeterminado_si_queda_algo_sin_mirar() -> None:
    """Un candidato sin inspeccionar NO puede producir ``OK``."""
    inspeccion = inspector.InspeccionDeDependencias(
        candidatos=CANDIDATOS_ESPERADOS,
        sospechosas=(),
        inspeccionados=("SKSE/Plugins/CrashLogger.ini",),
        no_inspeccionados=(("ModOrganizer.ini", "excede el límite"),),
    )
    hallazgo = inspector._hallazgo_de_dependencias(inspeccion)
    assert hallazgo.veredicto == inspector.HALLAZGO_INDETERMINADO, (
        f"un candidato sin inspeccionar produjo {hallazgo.veredicto}: {hallazgo.detalle}"
    )
    assert "ModOrganizer.ini" in hallazgo.detalle


def test_el_hallazgo_de_dependencias_es_ok_solo_si_todo_se_leyo() -> None:
    """El ``OK`` exige haber leído: dice cuántos archivos miró."""
    inspeccion = inspector.InspeccionDeDependencias(
        candidatos=CANDIDATOS_ESPERADOS,
        sospechosas=(),
        inspeccionados=("ModOrganizer.ini",),
        no_inspeccionados=(),
    )
    hallazgo = inspector._hallazgo_de_dependencias(inspeccion)
    assert hallazgo.veredicto == inspector.HALLAZGO_OK
    assert "ModOrganizer.ini" in hallazgo.detalle


def test_el_hallazgo_de_dependencias_bloquea_sobre_indeterminado() -> None:
    """Si además hay sospechosas, el veredicto más fuerte gana."""
    inspeccion = inspector.InspeccionDeDependencias(
        candidatos=CANDIDATOS_ESPERADOS,
        sospechosas=("ModOrganizer.ini menciona C:/Steam/Skyrim",),
        inspeccionados=("ModOrganizer.ini",),
        no_inspeccionados=(("SKSE/Plugins/CrashLogger.ini", "no se pudo leer"),),
    )
    hallazgo = inspector._hallazgo_de_dependencias(inspeccion)
    assert hallazgo.veredicto == inspector.HALLAZGO_BLOQUEADO
    assert "menciona" in hallazgo.detalle


def test_un_indeterminado_impide_ready_for_p7() -> None:
    """La consolidación no puede devolver LISTO con un solo INDETERMINADO.

    Se prueba sobre la función pura, con control positivo: los mismos hallazgos
    sin el INDETERMINADO SÍ dan LISTO. Sin ese control el test sería vacuo —
    cualquier implementación que devolviera INDETERMINADO siempre lo pasaría.
    """
    todos_ok = [inspector.Hallazgo(f"ok-{i}", inspector.HALLAZGO_OK, "verificado") for i in range(5)]
    assert inspector._veredicto_consolidado(todos_ok) == inspector.VEREDICTO_LISTO

    con_sin_mirar = [
        *todos_ok,
        inspector.Hallazgo("dependencias", inspector.HALLAZGO_INDETERMINADO, "ModOrganizer.ini sin leer"),
    ]
    assert inspector._veredicto_consolidado(con_sin_mirar) == inspector.VEREDICTO_INDETERMINADO


def test_el_informe_no_queda_listo_si_un_candidato_no_se_pudo_leer(tmp_path: pathlib.Path, monkeypatch) -> None:
    """De punta a punta: el informe real no puede salir ``READY_FOR_P7``.

    El hallazgo de ``dependencias`` es el discriminador: con el defecto era
    ``OK``. Se afirma sobre ÉL y no sobre ``listo_para_p7`` a secas, porque en un
    árbol sintético otros hallazgos también fallan y esa aserción no
    discriminaría nada.
    """
    from types import SimpleNamespace

    arboles = Arboles(tmp_path)
    (arboles.clone / "ModOrganizer.ini").write_text("[General]\r\n", encoding="utf-8", newline="")

    monkeypatch.setattr(
        inspector,
        "cargar_runtime_clone_record",
        lambda _root, _clone: SimpleNamespace(
            root_path=str(arboles.clone),
            clone_id="clone-1",
            lifecycle=SimpleNamespace(value="PROVISIONED"),
            source_generation_id="gen-1",
        ),
    )
    original = pathlib.Path.open

    def _open_que_niega(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.name == "ModOrganizer.ini" and self.parent == arboles.clone:
            raise PermissionError(13, "ACCESS_DENIED")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", _open_que_niega)

    informe, _ = inspector.construir_informe(
        storage_root=arboles.storage,
        mo2_instance_dir=arboles.mo2,
        config_path=None,
        managed_source_root=None,
        clone_id="clone-1",
        mostrar_rutas=False,
    )

    hallazgo = next(h for h in informe["hallazgos"] if h["clave"] == "dependencias")
    assert hallazgo["veredicto"] == inspector.HALLAZGO_INDETERMINADO, (
        f"un candidato ilegible se reportó como {hallazgo['veredicto']}: {hallazgo['detalle']}"
    )
    assert informe["listo_para_p7"] is False


def test_la_matriz_p7_g_nombra_steamapps_temp() -> None:
    """SFR-21 prohíbe escribir en ``steamapps/temp/**`` además de staging/downloading.

    Si la matriz que consume el operador omite esa superficie, el chequeo de
    no-escritura de P7-G puede declararse completo sin haberla mirado."""
    filas = {fila["id"]: fila for fila in inspector._matriz_p7()}
    criterio = filas["P7-G"]["prueba"]
    assert "steamapps/temp/**" in criterio
    assert "staging/downloading" in criterio
    assert "appmanifest" in criterio
