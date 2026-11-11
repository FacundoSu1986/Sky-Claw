"""H-4: anclas que ENUMERAN (no muestrean) lo que rodea a la superficie de operador de la cuarentena del broker VFS.

* Quién muta la cuarentena persistida: un camino nuevo que escriba o borre el marcador rompe el test hasta revisarlo.
* Quién arma la ruta del estado del broker (``vfs_bridge``): tres sitios hoy, cualquier otro se revisa.
* Todo ``--mode`` del parser está documentado en ``docs/api/cli_ref.md``.
* La consulta de cuarentena llega al gestor de LODs en producción (no solo en los tests que la inyectan).
"""

from __future__ import annotations

import ast
import pathlib
import types

from sky_claw.app_context import AppContext
from sky_claw.local.mo2 import vfs_quarantine

RAIZ = pathlib.Path(__file__).resolve().parent.parent
# Sin el paréntesis a propósito: ``release_quarantine`` borra con ``asyncio.to_thread(self._quarantine_path.unlink, ...)``,
# o sea que pasa ``unlink`` como callable. Un patrón ``.unlink(`` lo dejaría fuera de la enumeración.
_MUTADORES = (".unlink", ".write_text", ".write_bytes", "os.replace")


def _funciones_que_mutan(ruta: pathlib.Path, *, referencia: str) -> set[str]:
    """Funciones del archivo cuyo cuerpo menciona ``referencia`` y además escribe o borra algo."""
    fuente = ruta.read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    return {
        nodo.name
        for nodo in ast.walk(arbol)
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef))
        and referencia in (segmento := ast.get_source_segment(fuente, nodo) or "")
        and any(m in segmento for m in _MUTADORES)
    }


def test_los_caminos_que_mutan_el_marcador_de_cuarentena_estan_congelados() -> None:
    en_el_broker = _funciones_que_mutan(RAIZ / "sky_claw/local/mo2/vfs_broker.py", referencia="_quarantine_path")
    en_la_superficie_de_operador = _funciones_que_mutan(
        RAIZ / "sky_claw/local/mo2/vfs_quarantine.py", referencia="rutas.marcador"
    )

    assert en_el_broker == {
        "_load_quarantine",  # reclamación de lock huérfano: escribe el marcador
        "_quarantine_instance",  # pone la cuarentena (os.replace atómico)
        "_liberar_cuarentena_por_worker_exit",  # el worker_exit tardío la libera solo
        "release_quarantine",  # liberación con un broker vivo
    }
    assert en_la_superficie_de_operador == {"liberar_cuarentena"}  # liberación SIN broker vivo


def _sitios_con_ruta_del_estado() -> dict[str, int]:
    sitios: dict[str, int] = {}
    for ruta in sorted((RAIZ / "sky_claw").rglob("*.py")):
        cantidad = sum(
            1
            for n in ast.walk(ast.parse(ruta.read_text(encoding="utf-8")))
            if isinstance(n, ast.Constant) and n.value == "vfs_bridge"
        )
        if cantidad:
            sitios[ruta.relative_to(RAIZ).as_posix()] = cantidad
    return sitios


def test_los_sitios_que_arman_la_ruta_del_estado_del_broker_estan_congelados() -> None:
    """Quien arme ``<config>/vfs_bridge/<instancia>`` por su cuenta puede apuntar a otro directorio que el broker."""
    assert _sitios_con_ruta_del_estado() == {
        "sky_claw/__main__.py": 3,  # install-vfs-bridge, vfs-health y vfs-quarantine
        "sky_claw/app_context.py": 1,  # el broker de producción
    }


def test_todo_modo_de_la_cli_esta_documentado_en_cli_ref() -> None:
    fuente = (RAIZ / "sky_claw/__main__.py").read_text(encoding="utf-8")
    modos: set[str] = set()
    for nodo in ast.walk(ast.parse(fuente)):
        es_add_mode = (
            isinstance(nodo, ast.Call)
            and bool(nodo.args)
            and isinstance(nodo.args[0], ast.Constant)
            and nodo.args[0].value == "--mode"
        )
        if not es_add_mode:
            continue
        for kw in nodo.keywords:
            if kw.arg == "choices" and isinstance(kw.value, ast.List):
                modos = {e.value for e in kw.value.elts if isinstance(e, ast.Constant)}
    assert modos, "no se pudieron extraer los choices de --mode"

    documentacion = (RAIZ / "docs/api/cli_ref.md").read_text(encoding="utf-8")

    sin_documentar = sorted(m for m in modos if f"`{m}`" not in documentacion)
    assert not sin_documentar, f"modos sin documentar en docs/api/cli_ref.md: {sin_documentar}"


def test_el_gestor_de_lods_de_produccion_recibe_la_consulta_de_cuarentena() -> None:
    fuente = (RAIZ / "sky_claw/app_context.py").read_text(encoding="utf-8")
    llamadas = [
        n
        for n in ast.walk(ast.parse(fuente))
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "GestorDeTrabajosDeLods"
    ]
    assert llamadas, "no se encontró la construcción de GestorDeTrabajosDeLods en app_context.py"
    for llamada in llamadas:
        assert "consultar_cuarentena" in {kw.arg for kw in llamada.keywords}


def _contexto_con(broker: object) -> types.SimpleNamespace:
    return types.SimpleNamespace(vfs_broker=broker)


def test_el_contexto_traduce_la_cuarentena_del_broker_para_el_agente() -> None:
    broker = types.SimpleNamespace(quarantine_reason="terminalidad desconocida")

    estado = AppContext._estado_de_cuarentena_vfs(_contexto_con(broker))  # type: ignore[arg-type]

    assert estado == {
        "activa": True,
        "razon": "terminalidad desconocida",
        "como_liberar": vfs_quarantine.COMO_LIBERAR,
    }


def test_el_contexto_sin_cuarentena_o_sin_broker_no_informa_nada() -> None:
    assert AppContext._estado_de_cuarentena_vfs(_contexto_con(types.SimpleNamespace(quarantine_reason=None))) is None  # type: ignore[arg-type]
    assert AppContext._estado_de_cuarentena_vfs(_contexto_con(None)) is None  # type: ignore[arg-type]
