"""Censo enumerativo de superficies y coherencia de las tres clases (FIX 7).

Tracker: #672
ADR 0012 §29.4

Lo que este módulo NO acepta: que un ``frozenset`` de nombres comparado contra
su propio espejo cuente como censo. Cada aserción de acá tiene una de estas tres
formas:

1. **Derivación**: el conjunto congelado se compara contra lo que el AST
   ENCUENTRA en el código. Agregar un escritor nuevo rompe el test.
2. **Falsificación**: se construye un caso que el instrumento DEBE detectar
   (un módulo nuevo que escribe la superficie, una superficie divergente) y se
   comprueba que lo detecta. Un detector que nunca dice «sí» no detecta nada.
3. **Comportamiento**: tras una transición real se leen las tres clases de
   superficie y se exige que digan lo mismo.
"""

from __future__ import annotations

import pathlib
import sys
import textwrap
import tomllib

import pytest

from sky_claw.local.frozen_runtime import binder_recovery as recovery
from sky_claw.local.frozen_runtime import surface_census as censo
from sky_claw.local.frozen_runtime.effective_runtime.mo2_ini import leer_game_path_de_mo2
from sky_claw.local.frozen_runtime.effective_runtime.oracle import desired_runtime_from_storage
from sky_claw.local.frozen_runtime.journal import JournalDisposition, cargar_journal_transicion
from sky_claw.local.frozen_runtime.storage import active_state_path

# El rig sintético vive en un solo lugar (el módulo de integridad). Se importa
# por ruta explícita para no depender del importmode del runner.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_frozen_runtime_p5c1_binder_integrity import (  # noqa: E402
    _ejecutar,
    _montar_rig,
)


@pytest.fixture(autouse=True)
def _mock_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mismo mock de versión PE que el módulo de integridad.

    Sin él, ``_crear_generation_valida`` sella una identidad de runtime que el
    preflight no puede confirmar contra el ``SkyrimSE.exe`` sintético, y todo
    falla por ``generation_invalid`` antes de llegar a lo que el test mide.
    """
    monkeypatch.setattr(
        "sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version",
        lambda exe: "1.6.1170.0",
    )


# ---------------------------------------------------------------------------
# 1. Derivación: el censo se compara contra el AST, no contra un literal
# ---------------------------------------------------------------------------


def test_el_censo_de_escritores_de_superficies_se_deriva_por_ast() -> None:
    """Cada superficie del binder: escritores descubiertos == conjunto congelado.

    Si mañana otro módulo escribe ``active.json``, ``skyrim_path`` o el
    ``gamePath``, este test lo muestra como diferencia. Es el ancla contra el
    defecto dominante del repo (arreglar un hermano y no al otro).
    """
    descubiertos = censo.descubrir_escritores_de_superficies()
    assert set(descubiertos) == set(censo.ESCRITORES_DECLARADOS)
    for superficie, esperado in censo.ESCRITORES_DECLARADOS.items():
        assert descubiertos[superficie] == esperado, (
            f"el censo de escritores de {superficie!r} cambió: descubiertos {sorted(descubiertos[superficie])} "
            f"vs congelados {sorted(esperado)}. Decidí si el nuevo escritor es un adaptador del catálogo "
            "o una excepción justificada, y actualizá ESCRITORES_DECLARADOS"
        )


def test_el_censo_de_adaptadores_del_game_dir_se_deriva_por_ast() -> None:
    """Los adaptadores que derivan un destino del game dir se descubren solos."""
    assert censo.descubrir_adaptadores_destino_en_game_dir() == censo.ADAPTADORES_DESTINO_EN_GAME_DIR


def test_el_censo_del_game_dir_no_contiene_nombres_inexistentes() -> None:
    """Todo nombre del censo resuelve a una función REAL del árbol.

    El censo anterior listaba ``grass_cache`` y ``wrye_bash``, que no existen, y
    ``pandora``/``bodyslide``/``dyndolod_legacy``, que no se llaman así. Un censo
    con nombres inventados pasa cualquier comparación consigo mismo.
    """
    import ast

    existentes: set[str] = set()
    for modulo in pathlib.Path(censo.__file__).resolve().parent.parent.parent.rglob("*.py"):
        try:
            arbol = ast.parse(modulo.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        existentes |= {
            nodo.name for nodo in ast.walk(arbol) if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
    faltantes = sorted(nombre for nombre in censo.CENSO_ESCRITORES_GAME_DIR if nombre.split("::")[-1] not in existentes)
    assert not faltantes, f"el censo nombra funciones que no existen en el árbol: {faltantes}"


def test_el_descubridor_detecta_un_escritor_nuevo(tmp_path: pathlib.Path) -> None:
    """FALSIFICACIÓN: un módulo nuevo que escribe la superficie aparece en el censo.

    Sin esto, ``descubrir_escritores_de_superficies`` podría devolver siempre el
    conjunto congelado y el test de derivación sería tautológico.
    """
    paquete = tmp_path / "paquete"
    paquete.mkdir()
    (paquete / "intruso.py").write_text(
        textwrap.dedent(
            """
            from sky_claw.local.frozen_runtime.state import write_json_atomic
            from sky_claw.local.frozen_runtime.storage import active_state_path


            def escritor_intruso(root):
                write_json_atomic(active_state_path(root), {"schema_version": 2})
            """
        ),
        encoding="utf-8",
    )

    descubiertos = censo.descubrir_escritores_de_superficies(paquete)

    assert descubiertos["active_json"] == frozenset({"intruso.py::escritor_intruso"})


# ---------------------------------------------------------------------------
# 2. El catálogo de adaptadores es ejecutable, no una lista de nombres
# ---------------------------------------------------------------------------


def _resolver(referencia: str) -> object:
    import importlib

    modulo, nombre = censo.modulo_de(referencia)
    return getattr(importlib.import_module(modulo), nombre)


def test_el_catalogo_resuelve_a_callables_reales() -> None:
    """Cada escritor y verificador del catálogo existe y es invocable."""
    for adaptador in censo.CATALOGO_ADAPTADORES:
        assert callable(_resolver(adaptador.escritor)), f"escritor inexistente: {adaptador.escritor}"
        assert callable(_resolver(adaptador.verificador)), f"verificador inexistente: {adaptador.verificador}"


def test_las_tres_clases_estan_cubiertas_por_el_catalogo() -> None:
    """Las tres clases de §29.4 tienen al menos un adaptador implementado."""
    clases = {adaptador.clase for adaptador in censo.CATALOGO_ADAPTADORES}
    assert clases == set(censo.ClaseSuperficie)


def test_las_superficies_del_binder_son_las_del_catalogo() -> None:
    """Las claves que el binder usa para mutar son exactamente las del catálogo."""
    claves_binder = {
        recovery.SUPERFICIE_MO2_INI,
        recovery.SUPERFICIE_CONFIG,
        recovery.SUPERFICIE_ACTIVE,
        recovery.SUPERFICIE_ENVIRON,
    }
    assert claves_binder <= set(censo.CLASES_DE_SUPERFICIE)
    assert censo.CLASES_DE_SUPERFICIE[recovery.SUPERFICIE_ENVIRON] is censo.ClaseSuperficie.DERIVADA


#: Un verificador por adaptador, EJERCIDO. La igualdad de claves con el catálogo
#: es lo que impide que un adaptador nuevo entre sin verificación de verdad.
_VERIFICACIONES = {
    "mo2_ini": lambda rig: _verificar_ini(rig),
    "config": lambda rig: _verificar_config(rig),
    "active_json": lambda rig: _verificar_active(rig),
    "environ": lambda rig: _verificar_environ_con_negativo(),
    "journal": lambda rig: _verificar_journal(rig),
    "recuperacion": lambda rig: _verificar_recuperacion(rig),
}


def _verificar_environ_con_negativo() -> bool:
    """El verificador de la superficie DERIVADA distingue el valor correcto del roto.

    La versión anterior del test sólo llamaba al caso positivo: un verificador
    que siempre devolviera ``True`` pasaba (CodeRabbit sobre #843).
    """
    correcto = censo._verificar_environ({"SKYRIM_PATH": "x"}, "x")
    distinto = censo._verificar_environ({"SKYRIM_PATH": "otro"}, "x")
    ausente = censo._verificar_environ({}, "x")
    ausencia_ok = censo._verificar_environ({}, None)
    return correcto and not distinto and not ausente and ausencia_ok


def _verificar_ini(rig) -> bool:  # type: ignore[no-untyped-def]
    """El verificador del INI distingue el gamePath correcto del incorrecto."""
    correcto = leer_game_path_de_mo2(rig.ini).path == str(rig.clone_root)
    # Se corrompe el INI: el verificador tiene que notarlo.
    rig.ini.write_bytes(rig.ini.read_bytes().replace(b"@ByteArray", b"@ByteArrayX"))
    roto = leer_game_path_de_mo2(rig.ini).path != str(rig.clone_root)
    return correcto and roto


def _verificar_config(rig) -> bool:  # type: ignore[no-untyped-def]
    from sky_claw.local.frozen_runtime.runtime_clone_binder import _leer_skyrim_path_persistido

    correcto = _leer_skyrim_path_persistido(rig.config) == str(rig.clone_root)
    rig.config.write_text('skyrim_path = "C:\\\\Otro"\n', encoding="utf-8")
    roto = _leer_skyrim_path_persistido(rig.config) != str(rig.clone_root)
    return correcto and roto


def _verificar_active(rig) -> bool:  # type: ignore[no-untyped-def]
    correcto = desired_runtime_from_storage(rig.storage).clone_id == rig.clone_id
    active_state_path(rig.storage).write_text(
        '{"schema_version": 2, "desired_generation_id": null, "desired_clone_id": null, "updated_at_ns": 0}',
        encoding="utf-8",
    )
    roto = desired_runtime_from_storage(rig.storage).clone_id != rig.clone_id
    return correcto and roto


def _verificar_journal(rig) -> bool:  # type: ignore[no-untyped-def]
    from sky_claw.local.frozen_runtime.errors import JournalCorruptError

    correcto = cargar_journal_transicion(rig.storage).disposition is JournalDisposition.NONE
    # Un journal corrupto NO puede leerse como NONE: el lector levanta.
    (rig.storage / "state" / "transition.json").write_text("{ esto no es json", encoding="utf-8")
    try:
        cargar_journal_transicion(rig.storage)
        roto = False
    except JournalCorruptError:
        roto = True
    return correcto and roto


def _verificar_recuperacion(rig) -> bool:  # type: ignore[no-untyped-def]
    correcto = recovery.listar_registros(rig.storage) == ()
    intruso = recovery.recovery_state_dir(rig.storage) / "intruso.json"
    intruso.write_text('{"schema_version": 99}', encoding="utf-8")
    try:
        recovery.cargar_registro(rig.storage, "intruso")
        roto = False
    except recovery.RegistroCorruptoError:
        roto = True
    return correcto and roto


def test_cada_adaptador_tiene_su_verificacion_ejercida() -> None:
    """Todo adaptador del catálogo tiene una verificación de comportamiento.

    Un adaptador nuevo sin entrada acá rompe el test: no alcanza con declarar el
    nombre del verificador, hay que demostrar que distingue.
    """
    assert set(_VERIFICACIONES) == {a.clave for a in censo.CATALOGO_ADAPTADORES}


@pytest.mark.parametrize("clave", sorted(_VERIFICACIONES))
def test_cada_verificacion_distingue_el_estado_roto(tmp_path: pathlib.Path, clave: str) -> None:
    """Cada verificador dice «sí» con el bind correcto y «no» cuando se corrompe.

    Se monta un rig NUEVO por verificador porque cada uno corrompe su superficie
    a propósito: compartir el rig haría que el segundo midiera la basura del
    primero.
    """
    rig = _montar_rig(tmp_path)
    assert _ejecutar(rig).success is True

    assert _VERIFICACIONES[clave](rig) is True, f"la verificación de {clave!r} no distingue el estado roto"


# ---------------------------------------------------------------------------
# 3. Comportamiento: las tres clases quedan coherentes tras la transición
# ---------------------------------------------------------------------------


def test_las_tres_clases_quedan_coherentes_tras_la_transicion(tmp_path: pathlib.Path) -> None:
    """Persistidas, derivadas y cacheadas dicen lo MISMO después del bind.

    Es la demostración que un set de nombres no puede dar: se ejecuta la
    transición real y se leen las seis superficies del catálogo.
    """
    rig = _montar_rig(tmp_path)
    entorno = {"SKYRIM_PATH": str(rig.managed_src)}

    resultado = _ejecutar(rig, environ=entorno)
    assert resultado.success is True

    # ── PERSISTIDAS ──
    assert leer_game_path_de_mo2(rig.ini).path == str(rig.clone_root)
    assert tomllib.loads(rig.config.read_text(encoding="utf-8"))["skyrim_path"] == str(rig.clone_root)
    desired = desired_runtime_from_storage(rig.storage)
    assert desired.clone_id == rig.clone_id
    assert desired.generation_id == rig.gen_id
    assert desired.clone_root is not None

    # ── DERIVADA ──
    assert entorno["SKYRIM_PATH"] == str(rig.clone_root)

    # ── CACHEADAS ──
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.NONE
    from sky_claw.local.frozen_runtime.journal import derivar_historial_finalizados

    historial = derivar_historial_finalizados(rig.storage)
    assert historial.latest is not None
    assert historial.latest.target_clone_id == rig.clone_id
    assert recovery.listar_registros(rig.storage) == (), "el registro de recuperación debe retirarse al finalizar"

    # Y las tres clases coinciden entre sí: el valor de la derivada es el de la
    # persistida de la que se deriva.
    assert entorno["SKYRIM_PATH"] == str(desired.clone_root)


# ---------------------------------------------------------------------------
# Ronda 2 — el censo tiene que ver los dos patrones que se le escapaban
# ---------------------------------------------------------------------------


def test_el_descubridor_ve_al_escritor_que_enlaza_la_ruta(tmp_path: pathlib.Path) -> None:
    """FALSIFICACIÓN: ``p = active_state_path(root); write_json_atomic(p, ...)``.

    Es el patrón ordinario cuando la ruta se reutiliza, y la regla anterior sólo
    miraba los ARGUMENTOS de la llamada: con un ``Name`` no veía nada. El
    escritor de recuperación usa exactamente esa forma (Codex sobre #843).
    """
    paquete = tmp_path / "paquete"
    paquete.mkdir()
    (paquete / "intruso.py").write_text(
        textwrap.dedent(
            """
            from sky_claw.local.frozen_runtime.state import write_json_atomic
            from sky_claw.local.frozen_runtime.storage import active_state_path


            def escritor_con_variable(root):
                destino = active_state_path(root)
                write_json_atomic(destino, {"schema_version": 2})
            """
        ),
        encoding="utf-8",
    )

    descubiertos = censo.descubrir_escritores_de_superficies(paquete)

    assert descubiertos["active_json"] == frozenset({"intruso.py::escritor_con_variable"})


def test_el_descubridor_ve_al_restaurador_generico(tmp_path: pathlib.Path) -> None:
    """FALSIFICACIÓN: un restaurador que escribe la superficie que le pasan.

    No repite ``active_state_path`` ni el literal del archivo: recibe la ruta
    dentro de una ``SuperficieCapturada``. Es escritor de las TRES superficies
    persistidas, con el ``kind`` decidido en runtime.
    """
    paquete = tmp_path / "paquete"
    paquete.mkdir()
    (paquete / "intruso.py").write_text(
        textwrap.dedent(
            """
            from sky_claw.local.frozen_runtime.binder_recovery import SuperficieCapturada, _escribir_bytes_atomico


            def restaurador(superficie: SuperficieCapturada) -> None:
                _escribir_bytes_atomico(superficie.path, b"x")
            """
        ),
        encoding="utf-8",
    )

    descubiertos = censo.descubrir_escritores_de_superficies(paquete)

    for superficie in ("mo2_ini", "config", "active_json"):
        assert descubiertos[superficie] == frozenset({"intruso.py::restaurador"})


def test_el_descubridor_ve_write_text_directo(tmp_path: pathlib.Path) -> None:
    """FALSIFICACIÓN: ``(raiz / "active.json").write_text(...)`` sin primitiva.

    Es la forma más directa de escribir la superficie y no estaba entre los
    mutadores: el descubridor devolvía todos los conjuntos vacíos para un módulo
    así (Codex sobre #843).
    """
    paquete = tmp_path / "paquete"
    paquete.mkdir()
    (paquete / "intruso.py").write_text(
        textwrap.dedent(
            """
            def escribir(raiz):
                (raiz / "active.json").write_text("{}", encoding="utf-8")
            """
        ),
        encoding="utf-8",
    )

    descubiertos = censo.descubrir_escritores_de_superficies(paquete)

    assert descubiertos["active_json"] == frozenset({"intruso.py::escribir"})
