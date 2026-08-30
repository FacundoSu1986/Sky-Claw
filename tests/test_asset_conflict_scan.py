"""Caracterización del seam Asset Conflict Scan (PR4 + hardening PR4.1).

PR4 del Strangler Fig extrae de ``SupervisorAgent`` la creación perezosa del
``AssetConflictDetector`` y los contratos ``scan_asset_conflicts`` /
``scan_asset_conflicts_json`` a :class:`AssetConflictScanner`
(``sky_claw/app/orchestrator/asset_conflict_scan.py``). PR4.1 añade el
hardening de seguridad: el **validator de modding** se inyecta al scanner y llega
al detector para activar los guards de child-path que antes quedaban inertes.

Estas anclas fijan:

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
- Wiring: ``__init__`` construye el scanner con ``path_resolver`` +
  ``path_validator`` (el de modding) y pasa ``scanner.scan`` / ``scanner.scan_json``
  a ``build_orchestration_composition`` (AST — detecta inversión plain/JSON,
  callbacks del supervisor, kwargs inesperados y ``**kwargs``).
- Seguridad: el MISMO validator inyectado llega al detector y participa de
  verdad en ``parse_modlist`` / ``calculate_checksum`` (fail-closed).
- Errores de resolución lazy: fallos en ``get_mo2_mods_path`` /
  ``get_active_profile`` se re-lanzan sin dejar el memo a medio construir.
- Inventario de seams bound al supervisor (enumera, no muestrea).
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from unittest.mock import MagicMock

import pytest

from sky_claw.app.orchestrator.asset_conflict_scan import AssetConflictScanner
from sky_claw.app.security.path_validator import PathValidator, PathViolationError
from sky_claw.local.assets import AssetConflictDetector, AssetConflictReport, AssetType


def _scanner_con_resolver(
    mods_path: pathlib.Path | None,
    *,
    profile: str = "Sesion",
    path_validator: object | None = None,
) -> tuple[AssetConflictScanner, MagicMock]:
    """Scanner con resolver doblado: getters preconfigurados."""
    resolver = MagicMock()
    resolver.get_mo2_mods_path.return_value = mods_path
    resolver.get_active_profile.return_value = profile
    return (
        AssetConflictScanner(
            path_resolver=resolver,
            path_validator=path_validator,
        ),
        resolver,
    )


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
# Hardening de seguridad — wiring del validator + guards child-path activos
# ---------------------------------------------------------------------------


def test_scanner_construye_el_detector_con_el_mismo_validator(tmp_path: pathlib.Path) -> None:
    """T-S1: el detector construido lazy recibe EXACTAMENTE el validator que
    se inyectó al scanner (identidad, no clon)."""
    validator = PathValidator(roots=[tmp_path])
    scanner, _ = _scanner_con_resolver(tmp_path, path_validator=validator)

    detector = scanner.detector

    assert detector._path_validator is validator


def test_detector_recibe_el_mismo_validator_tras_memoizacion(tmp_path: pathlib.Path) -> None:
    """T-S1 (memo): el acceso repetido conserva el MISMO detector con el MISMO
    validator — no se reconstruye ni se re-clona el guard."""
    validator = PathValidator(roots=[tmp_path])
    scanner, _ = _scanner_con_resolver(tmp_path, path_validator=validator)

    assert scanner.detector is scanner.detector
    assert scanner.detector._path_validator is validator


def test_parse_modlist_no_abre_archivo_si_el_validator_rechaza(tmp_path: pathlib.Path) -> None:
    """T-S2: un validator que rechaza modlist_path impide que ``parse_modlist``
    llegue a ``open()`` — el guard de child-path participa de verdad (fail-closed).

    Usamos el ``PathValidator`` real con la root apuntando a un directorio
    DISTINTO del que contiene modlist.txt: ``validate()`` lanza
    ``PathViolationError`` antes de cualquier lectura.
    """
    mo2 = tmp_path / "mo2"
    mo2.mkdir()
    mods = mo2 / "mods"
    mods.mkdir()
    profiles = mo2 / "profiles" / "Sesion"
    profiles.mkdir(parents=True)
    (profiles / "modlist.txt").write_text("+ModLegitimo\n", encoding="utf-8")

    # Root del validator apunta a otro lado: cualquier path de MO2 es "fuera".
    validator = PathValidator(roots=[tmp_path / "otro_sandbox"])
    scanner, _ = _scanner_con_resolver(mods, path_validator=validator)
    detector = scanner.detector

    with pytest.raises(PathViolationError):
        detector.parse_modlist()


def test_calculate_checksum_no_abre_asset_si_el_validator_rechaza(tmp_path: pathlib.Path) -> None:
    """T-S3: un path rechazado por el validator no se abre para checksum.

    El asset existe en disco pero queda FUERA de la root del validator, así que
    ``calculate_checksum`` lanza ``PathViolationError`` en lugar de leer."""
    otro = tmp_path / "fuera"
    otro.mkdir()
    asset = otro / "textures" / "a.dds"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"\x00\x01\x02\x03")

    validator = PathValidator(roots=[tmp_path / "sandbox_interno"])
    scanner, _ = _scanner_con_resolver(tmp_path, path_validator=validator)
    detector = scanner.detector

    with pytest.raises(PathViolationError):
        detector.calculate_checksum(asset)


# ---------------------------------------------------------------------------
# Errores durante la resolución lazy del detector
# ---------------------------------------------------------------------------


def test_scan_relanza_error_del_resolver_antes_de_construir_detector(tmp_path: pathlib.Path) -> None:
    """El error en ``get_mo2_mods_path`` durante la resolución lazy se re-lanza
    tal cual, sin dejar ``_detector`` parcialmente inicializado."""
    scanner, resolver = _scanner_con_resolver(tmp_path)
    boom = RuntimeError("MO2_PATH sin configurar")
    resolver.get_mo2_mods_path.side_effect = boom

    with pytest.raises(RuntimeError) as ctx:
        scanner.scan()
    assert ctx.value is boom
    assert scanner._detector is None


def test_scan_json_relanza_error_del_resolver_antes_de_construir_detector(tmp_path: pathlib.Path) -> None:
    """Mismo contrato para ``scan_json``: el fallo del resolver se propaga y el
    memo no queda a medio construir."""
    scanner, resolver = _scanner_con_resolver(tmp_path)
    boom = RuntimeError("profile sin hidratar")
    resolver.get_active_profile.side_effect = boom

    with pytest.raises(RuntimeError) as ctx:
        scanner.scan_json()
    assert ctx.value is boom
    assert scanner._detector is None


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


def _formato_kwarg(value: ast.expr) -> str:
    """Normaliza una expresión de kwarg a una forma comparable.

    Cubre ``self.attr`` (→ ``self.attr``), ``Name`` (→ ``name``) y cualquier
    otra expresión por su representación de tipo (ast.Call, ast.Constant, …)
    para poder asertar el kwarg INESPERADO sin depender del nombre local.
    """
    if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id == "self":
        return f"self.{value.attr}"
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Constant):
        return repr(value.value)
    return type(value).__name__


def test_unique_seam_del_supervisor_bound_al_builder_es_plugin_guard() -> None:
    """Guardrail de familia (enumera, no muestrea).

    De todos los kwargs entregados a ``build_orchestration_composition``, los
    que son métodos bound del supervisor (``self.<método>``) deben ser
    exactamente:
        plugin_limit_guard ← self._run_plugin_limit_guard

    Es el único seam de dominio residual que sigue bound al supervisor tras PR4.
    PR5 (plugin-limit) debe romper esta ancla a propósito y actualizarla; un
    callable bound nuevo que reaparezca sin pasar por la extracción la rompe
    (mantiene el inventario exhaustivo, no un caso por cada seam).
    """
    arbol = _arbol_supervisor()
    clase = _cuerpo_supervisor(arbol)

    # Métodos reales de SupervisorAgent (FunctionDef / AsyncFunctionDef): solo
    # estos cuentan como "callables bound". Los datos/servicios (self.scraper,
    # self._lock_manager, self._path_resolver, …) son atributos, no métodos.
    nombres_metodo = {m.name for m in clase.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}

    bound_del_supervisor: dict[str, str] = {}
    for miembro in clase.body:
        if not (isinstance(miembro, ast.FunctionDef) and miembro.name == "__init__"):
            continue
        for sub in ast.walk(miembro):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id == "build_orchestration_composition"
            ):
                for kw in sub.keywords:
                    if kw.arg is None:
                        continue
                    valor = kw.value
                    if (
                        isinstance(valor, ast.Attribute)
                        and isinstance(valor.value, ast.Name)
                        and valor.value.id == "self"
                        and valor.attr in nombres_metodo
                    ):
                        bound_del_supervisor[kw.arg] = valor.attr
        break

    assert bound_del_supervisor == {"plugin_limit_guard": "_run_plugin_limit_guard"}, (
        f"Seams bound al supervisor entregados al builder: {bound_del_supervisor}. "
        "Se espera exactamente {plugin_limit_guard: _run_plugin_limit_guard} "
        "(los demás seams ya están extraídos: grass → GrassRuntimeDepsProvider, "
        "asset scan → AssetConflictScanner)"
    )


def test_supervisor_cablea_el_scanner_a_la_composition() -> None:
    """T9 (AST): ``__init__`` construye AssetConflictScanner SOLO con
    ``path_resolver=self._path_resolver`` y ``path_validator=self._modding_validator``
    y pasa ``scanner.scan`` / ``scanner.scan_json`` al builder — sin congelar el
    nombre de la variable local.

    Invariantes:
      - TODOS los kwargs a ``AssetConflictScanner`` se inventarían (named + ``**``);
        un ``**extras`` o un kwarg inesperado rompe el test.
      - ``path_validator`` apunta al validator de modding (no ``_path_validator``
        del rollback ni un validator fabricado).
      - el MISMO scanner construido se usa para ``scan`` y ``scan_json``
        (detección de inversión plain/JSON y de callbacks viejos del supervisor).
      - la correspondencia scanner↔callback es por IDENTIDAD EXACTA de nombre
        (pertenencia a un ``set``), nunca por substring: un scanner distinto
        cuyo nombre sea subcadena del construido (p.ej. ``scanner`` dentro de
        ``asset_conflict_scanner``) rompe el test.
    """
    arbol = _arbol_supervisor()
    clase = _cuerpo_supervisor(arbol)
    for miembro in clase.body:
        if not (isinstance(miembro, ast.FunctionDef) and miembro.name == "__init__"):
            continue

        construcciones: list[tuple[set[str], dict[str, ast.expr], list[ast.expr]]] = []
        kwargs_al_builder: dict[str, ast.expr] = {}
        for sub in ast.walk(miembro):
            # Construcción del scanner: captura la variable destino para rastrear
            # el MISMO objeto hasta los callbacks del builder.
            if (
                isinstance(sub, ast.Assign)
                and isinstance(sub.value, ast.Call)
                and isinstance(sub.value.func, ast.Name)
                and sub.value.func.id == "AssetConflictScanner"
            ):
                kw_named: dict[str, ast.expr] = {}
                kw_args: list[ast.expr] = []
                for kw in sub.value.keywords:
                    if kw.arg is None:
                        kw_args.append(kw.value)
                    else:
                        kw_named[kw.arg] = kw.value
                nombres_destino = {t.id for t in sub.targets if isinstance(t, ast.Name)}
                construcciones.append((nombres_destino, kw_named, kw_args))

            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id == "build_orchestration_composition"
            ):
                for kw in sub.keywords:
                    if kw.arg is not None:
                        kwargs_al_builder[kw.arg] = kw.value

        assert construcciones, "__init__ no construye AssetConflictScanner"

        for nombres_destino, kw_named, kw_args in construcciones:
            # 1. Sin `**extras` ni kwargs inesperados: el contrato es exacto.
            assert kw_args == [], (
                "AssetConflictScanner recibe `**kwargs` no nominales — "
                "el contrato del scanner es path_resolver + path_validator explícitos"
            )
            assert set(kw_named) == {"path_resolver", "path_validator"}, (
                f"AssetConflictScanner se construye con kwargs {set(kw_named)} — "
                "se espera exactamente {path_resolver, path_validator}"
            )
            # 2. Valores correctos (semántica, no nombre local).
            assert _formato_kwarg(kw_named["path_resolver"]) == "self._path_resolver", (
                "path_resolver debe ser self._path_resolver"
            )
            assert _formato_kwarg(kw_named["path_validator"]) == "self._modding_validator", (
                "path_validator debe ser self._modding_validator (el validator de modding, "
                "NO el backup-only del rollback)"
            )
            # 3. El MISMO scanner construido alimenta scan y scan_json.
            for builder_kwarg, attr in (
                ("scan_asset_conflicts", "scan"),
                ("scan_asset_conflicts_json", "scan_json"),
            ):
                valor = kwargs_al_builder.get(builder_kwarg)
                assert valor is not None, f"{builder_kwarg} no se entregó al builder"
                assert isinstance(valor, ast.Attribute) and valor.attr == attr, (
                    f"{builder_kwarg} debe ser <scanner>.{attr} (detecta inversión plain/JSON "
                    "y callbacks viejos del supervisor)"
                )
                assert isinstance(valor.value, ast.Name), (
                    f"{builder_kwarg} debe referir al scanner construido, no a una expresión arbitraria"
                )
                # Identidad EXACTA de nombre: pertenencia al set, nunca
                # substring — "scanner" no casa con {"asset_conflict_scanner"}.
                assert valor.value.id in nombres_destino, (
                    f"{builder_kwarg} usa un scanner distinto del que __init__ construyó "
                    f"(construido en {sorted(nombres_destino)}, usado via '{valor.value.id}')"
                )
        return
    raise AssertionError("No se encontró SupervisorAgent.__init__")
