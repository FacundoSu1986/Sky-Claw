"""Caracterización del seam Asset Conflict Scan (PR4).

PR4 del Strangler Fig extrae de ``SupervisorAgent`` la creación perezosa del
``AssetConflictDetector`` y los contratos ``scan_asset_conflicts`` /
``scan_asset_conflicts_json`` a :class:`AssetConflictScanner`
(``sky_claw/app/orchestrator/asset_conflict_scan.py``). Estas anclas fijan:

- Contrato lazy: construir el scanner NO construye el detector (en la GUI
  ``MO2_PATH`` puede hidratarse después de construir el supervisor).
- Memoización: dos accesos a ``detector`` devuelven LA MISMA instancia y los
  getters del resolver se llaman una sola vez.
- Contrato plain: ``scan()`` devuelve exactamente la lista del detector,
  incluido el caso vacío ``[]``; ``OSError``/``RuntimeError`` se re-lanzan.
- Contrato JSON: ``scan_json()`` devuelve el string del detector tal cual y
  re-lanza los mismos tipos de error.
- Frontera: el módulo nuevo no conoce a ``SupervisorAgent`` (AST).
- Frontera inversa: ``SupervisorAgent`` ya no construye el detector ni
  conserva el estado ``_asset_detector`` (AST, con resolución de aliases y
  llamadas dotted).
- Wiring: ``__init__`` construye el scanner con el path resolver y pasa
  ``scanner.scan`` / ``scanner.scan_json`` a ``build_orchestration_composition``
  (AST — detecta inversión plain/JSON y callbacks del supervisor).
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from unittest.mock import MagicMock

import pytest

from sky_claw.app.orchestrator.asset_conflict_scan import AssetConflictScanner
from sky_claw.local.assets import AssetConflictDetector, AssetConflictReport, AssetType


def _scanner_con_resolver(
    mods_path: pathlib.Path | None,
    *,
    profile: str = "Sesion",
) -> tuple[AssetConflictScanner, MagicMock]:
    """Scanner con resolver doblado: getters preconfigurados."""
    resolver = MagicMock()
    resolver.get_mo2_mods_path.return_value = mods_path
    resolver.get_active_profile.return_value = profile
    return AssetConflictScanner(path_resolver=resolver), resolver


def _reporte(path: str = "meshes/a.nif") -> AssetConflictReport:
    return AssetConflictReport(
        file_path=path,
        winner_mod="ModB",
        overwritten_mods=("ModA",),
        asset_type=AssetType.MESH,
    )


# ---------------------------------------------------------------------------
# T1 — Laziness: construir el scanner NO construye el detector
# ---------------------------------------------------------------------------


def test_construir_el_scanner_no_resuelve_paths_ni_construye_detector(tmp_path: pathlib.Path) -> None:
    """Construcción barata: ningún getter del resolver se llama y el memo
    queda vacío (un scanner "pre-resuelto" dejaría al detector construido
    ANTES del primer acceso)."""
    scanner, resolver = _scanner_con_resolver(tmp_path)

    assert scanner._detector is None
    resolver.get_mo2_mods_path.assert_not_called()
    resolver.get_active_profile.assert_not_called()


# ---------------------------------------------------------------------------
# T2 — Memoización: el detector se construye una sola vez
# ---------------------------------------------------------------------------


def test_detector_se_construye_lazy_y_se_memoiza(tmp_path: pathlib.Path) -> None:
    """Primer acceso resuelve paths y construye; el segundo devuelve la MISMA
    instancia sin volver a llamar a los getters."""
    scanner, resolver = _scanner_con_resolver(tmp_path, profile="MiPerfil")

    primero = scanner.detector
    segundo = scanner.detector

    assert primero is segundo
    assert isinstance(primero, AssetConflictDetector)
    assert primero.mo2_mods_path == tmp_path
    assert primero.profile_name == "MiPerfil"
    resolver.get_mo2_mods_path.assert_called_once_with()
    resolver.get_active_profile.assert_called_once_with()


# ---------------------------------------------------------------------------
# T3/T5 — Contrato plain de scan()
# ---------------------------------------------------------------------------


def test_scan_devuelve_exactamente_la_lista_del_detector() -> None:
    scanner, _ = _scanner_con_resolver(None)
    detector = MagicMock()
    reportes = [_reporte("meshes/a.nif"), _reporte("textures/b.dds")]
    detector.detect_conflicts.return_value = reportes
    scanner._detector = detector

    resultado = scanner.scan()

    assert resultado is reportes
    detector.detect_conflicts.assert_called_once_with()


def test_scan_sin_conflictos_devuelve_lista_vacia() -> None:
    scanner, _ = _scanner_con_resolver(None)
    detector = MagicMock()
    detector.detect_conflicts.return_value = []
    scanner._detector = detector

    assert scanner.scan() == []


@pytest.mark.parametrize("excepcion", [OSError("io roto"), RuntimeError("sin MO2")])
def test_scan_relanza_errores_conocidos(excepcion: Exception) -> None:
    """El contrato pre-PR4 re-lanza (OSError, RuntimeError) tras loggear."""
    scanner, _ = _scanner_con_resolver(None)
    detector = MagicMock()
    detector.detect_conflicts.side_effect = excepcion
    scanner._detector = detector

    with pytest.raises(type(excepcion)) as ctx:
        scanner.scan()
    assert ctx.value is excepcion


# ---------------------------------------------------------------------------
# T4 — Contrato JSON de scan_json()
# ---------------------------------------------------------------------------


def test_scan_json_devuelve_el_string_del_detector_tal_cual() -> None:
    scanner, _ = _scanner_con_resolver(None)
    detector = MagicMock()
    detector.scan_to_json.return_value = '{"conflicts": [], "total": 0}'
    scanner._detector = detector

    resultado = scanner.scan_json()

    assert resultado == '{"conflicts": [], "total": 0}'
    detector.scan_to_json.assert_called_once_with()


@pytest.mark.parametrize("excepcion", [OSError("io roto"), RuntimeError("sin MO2")])
def test_scan_json_relanza_errores_conocidos(excepcion: Exception) -> None:
    scanner, _ = _scanner_con_resolver(None)
    detector = MagicMock()
    detector.scan_to_json.side_effect = excepcion
    scanner._detector = detector

    with pytest.raises(type(excepcion)) as ctx:
        scanner.scan_json()
    assert ctx.value is excepcion


# ---------------------------------------------------------------------------
# Facades del supervisor (compatibilidad pública)
# ---------------------------------------------------------------------------


def test_facades_del_supervisor_delegan_en_el_scanner() -> None:
    """``SupervisorAgent.asset_detector / scan_asset_conflicts( _json)`` siguen
    existiendo como facades (GUI en sky_claw_gui.py + harness BDD los usan) y
    delegan en el scanner inyectado."""
    from sky_claw.app.orchestrator.supervisor import SupervisorAgent

    supervisor = SupervisorAgent.__new__(SupervisorAgent)
    scanner = MagicMock()
    supervisor._asset_scanner = scanner

    assert supervisor.asset_detector is scanner.detector
    assert supervisor.scan_asset_conflicts() is scanner.scan.return_value
    assert supervisor.scan_asset_conflicts_json() is scanner.scan_json.return_value
    scanner.scan.assert_called_once_with()
    scanner.scan_json.assert_called_once_with()


# ---------------------------------------------------------------------------
# T6..T9 — Anclas estructurales (AST) — fronteras del seam extraído
# ---------------------------------------------------------------------------


def _arbol_de(clase_o_modulo: object) -> ast.Module:
    return ast.parse(pathlib.Path(inspect.getfile(clase_o_modulo)).read_text(encoding="utf-8"))


def _inventario_nombres_e_imports(arbol: ast.Module) -> set[str]:
    """Inventario amplio de identificadores: imports (módulo y nombres),
    nombres y atributos (encadena los dotted a sus partes)."""
    inventario: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom):
            if nodo.module:
                inventario.add(nodo.module)
            inventario.update(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.Import):
            inventario.update(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.Name):
            inventario.add(nodo.id)
        elif isinstance(nodo, ast.Attribute):
            cursor = nodo
            while isinstance(cursor, ast.Attribute):
                inventario.add(cursor.attr)
                cursor = cursor.value
            if isinstance(cursor, ast.Name):
                inventario.add(cursor.id)
    return inventario


def test_scanner_no_conoce_al_supervisor() -> None:
    """T6 (AST): el módulo nuevo no importa ni referencia a SupervisorAgent.

    Detecta referencias en nombres, atributos e imports, incluidos imports
    perezosos o locales al cuerpo de funciones.
    """
    from sky_claw.app.orchestrator import asset_conflict_scan

    inventario = _inventario_nombres_e_imports(_arbol_de(asset_conflict_scan))

    assert "SupervisorAgent" not in inventario, (
        "asset_conflict_scan referencia 'SupervisorAgent' — el flujo es scanner ← composition, nunca al revés"
    )
    assert "sky_claw.app.orchestrator.supervisor" not in inventario


def _arbol_supervisor() -> ast.Module:
    from sky_claw.app.orchestrator import supervisor

    return _arbol_de(supervisor)


def _cuerpo_supervisor(arbol: ast.Module) -> ast.ClassDef:
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == "SupervisorAgent":
            return nodo
    raise AssertionError("No se encontró la clase SupervisorAgent en supervisor.py")


def test_supervisor_no_construye_el_detector_de_assets() -> None:
    """T7 (AST): ningún call site del módulo construye AssetConflictDetector.

    Resuelve aliases ``from X import Y as Z`` (módulo y locales) y compara el
    nombre FINAL de llamadas dotted (``assets.AssetConflictDetector(...)``).
    """
    arbol = _arbol_supervisor()

    alias_map: dict[str, str] = {}
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom):
            for alias in nodo.names:
                alias_map[alias.asname or alias.name] = alias.name

    llamadas: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call):
            if isinstance(nodo.func, ast.Name):
                llamador = nodo.func.id
            elif isinstance(nodo.func, ast.Attribute):
                llamador = nodo.func.attr
            else:
                continue
            llamadas.add(alias_map.get(llamador, llamador))

    assert "AssetConflictDetector" not in llamadas, (
        "supervisor.py construye AssetConflictDetector — vive en AssetConflictScanner "
        "(sky_claw/app/orchestrator/asset_conflict_scan.py)"
    )


def test_supervisor_ya_no_tiene_estado_asset_detector() -> None:
    """T8 (AST): ``_asset_detector`` no puede reaparecer como atributo,
    asignación ni referencia del supervisor."""
    inventario = _inventario_nombres_e_imports(_arbol_supervisor())

    assert "_asset_detector" not in inventario, (
        "supervisor.py reintrodujo el estado _asset_detector — el memo vive en AssetConflictScanner"
    )


def test_supervisor_cablea_el_scanner_a_la_composition() -> None:
    """T9 (AST): __init__ construye AssetConflictScanner con el path resolver
    y pasa ``.scan`` / ``.scan_json`` al builder.

    Ancla las dos mitades: quitar la construcción del scanner O los kwargs al
    builder rompe el test; también detecta la inversión plain/JSON y el
    callback viejo del supervisor (``self.scan_asset_conflicts``).
    """
    arbol = _arbol_supervisor()
    clase = _cuerpo_supervisor(arbol)
    for miembro in clase.body:
        if isinstance(miembro, ast.FunctionDef) and miembro.name == "__init__":
            construcciones = []
            kwargs_al_builder: dict[str, ast.expr] = {}
            for sub in ast.walk(miembro):
                if (
                    isinstance(sub, ast.Assign)
                    and isinstance(sub.value, ast.Call)
                    and isinstance(sub.value.func, ast.Name)
                    and sub.value.func.id == "AssetConflictScanner"
                    and any(isinstance(t, ast.Name) and t.id == "asset_conflict_scanner" for t in sub.targets)
                ):
                    construcciones.append(sub)
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "build_orchestration_composition"
                ):
                    for kw in sub.keywords:
                        kwargs_al_builder[kw.arg] = kw.value
            assert construcciones, "__init__ no construye AssetConflictScanner"
            kwargs_reales = {}
            for kw in construcciones[0].value.keywords:
                v = kw.value
                if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) and v.value.id == "self":
                    kwargs_reales[kw.arg] = f"self.{v.attr}"
                elif isinstance(v, ast.Name):
                    kwargs_reales[kw.arg] = v.id
            assert kwargs_reales == {"path_resolver": "self._path_resolver"}, (
                f"AssetConflictScanner se construye con {kwargs_reales} — "
                "se espera exactamente path_resolver=self._path_resolver"
            )

            def _es_attr_del_scanner(nodo: ast.expr | None, attr: str) -> bool:
                return (
                    isinstance(nodo, ast.Attribute)
                    and nodo.attr == attr
                    and isinstance(nodo.value, ast.Name)
                    and nodo.value.id == "asset_conflict_scanner"
                )

            assert _es_attr_del_scanner(kwargs_al_builder.get("scan_asset_conflicts"), "scan"), (
                "scan_asset_conflicts debe ser asset_conflict_scanner.scan (no otro callable)"
            )
            assert _es_attr_del_scanner(kwargs_al_builder.get("scan_asset_conflicts_json"), "scan_json"), (
                "scan_asset_conflicts_json debe ser asset_conflict_scanner.scan_json — "
                "detecta inversión plain/JSON y callbacks del supervisor"
            )
            return
    raise AssertionError("No se encontró SupervisorAgent.__init__")
