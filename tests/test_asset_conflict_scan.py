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


def test_parse_modlist_funciona_cuando_el_validator_acepta(tmp_path: pathlib.Path) -> None:
    """T-S4 (happy-path): con el asset dentro de la root, el validator acepta y
    ``parse_modlist`` devuelve los mods habilitados (mayor prioridad primero).

    Complementa a T-S2/T-S3 (rechazo): sin este test, un guard que rechazara
    TODO pasaría la suite — el fail-closed se demuestra porque también abre
    cuando corresponde."""
    mo2 = tmp_path / "mo2"
    mods = mo2 / "mods"
    mods.mkdir(parents=True)
    profiles = mo2 / "profiles" / "Sesion"
    profiles.mkdir(parents=True)
    (profiles / "modlist.txt").write_text("+ModBajo\n-ModDeshabilitado\n+ModAlto\n", encoding="utf-8")

    validator = PathValidator(roots=[mo2])
    scanner, _ = _scanner_con_resolver(mods, path_validator=validator)
    detector = scanner.detector

    assert detector.parse_modlist() == ["ModAlto", "ModBajo"]


def _crear_symlink_o_junction(target: pathlib.Path, link: pathlib.Path) -> None:
    """Crea un symlink o directory junction compatible con Windows y POSIX."""
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        if target.is_dir():
            try:
                import _winapi

                _winapi.CreateJunction(str(target), str(link))
                return
            except (ImportError, AttributeError, OSError):
                pass
        pytest.skip(f"No se pudo crear symlink/junction en esta plataforma: {link} -> {target}")


def test_scan_mod_directory_rechaza_symlink_o_junction_fuera_del_sandbox(tmp_path: pathlib.Path) -> None:
    """T-S5: Un mod que es un symlink/junction apuntando fuera del sandbox lanza
    PathViolationError de forma fail-closed en scan_mod_directory() incluso cuando
    calculate_checksums=False."""
    sandbox = tmp_path / "sandbox"
    mo2 = sandbox / "MO2"
    mods = mo2 / "mods"
    mods.mkdir(parents=True)

    outside = tmp_path / "outside"
    outside_mod = outside / "ModExterno"
    outside_mod.mkdir(parents=True)
    (outside_mod / "textures").mkdir()
    (outside_mod / "textures" / "shared.dds").write_bytes(b"EXTERNO")

    link_mod = mods / "ModExterno"
    _crear_symlink_o_junction(outside_mod, link_mod)

    validator = PathValidator(roots=[sandbox])
    detector = AssetConflictDetector(mo2_mods_path=mods, path_validator=validator)

    with pytest.raises(PathViolationError):
        detector.scan_mod_directory("ModExterno", calculate_checksums=False)


def test_scan_mod_directory_rechaza_symlink_roto_fuera_del_sandbox_antes_de_exists(tmp_path: pathlib.Path) -> None:
    """T-S5b: Un mod symlink/junction apuntando a un path externo inexistente/roto
    se valida y rechaza con PathViolationError ANTES de evaluar exists() (fail-closed)."""
    sandbox = tmp_path / "sandbox"
    mo2 = sandbox / "MO2"
    mods = mo2 / "mods"
    mods.mkdir(parents=True)

    outside = tmp_path / "outside"
    outside_mod = outside / "ModInexistente"
    outside_mod.mkdir(parents=True)

    link_mod = mods / "BrokenMod"
    _crear_symlink_o_junction(outside_mod, link_mod)
    outside_mod.rmdir()  # El link apunta a un destino inexistente fuera de las roots

    validator = PathValidator(roots=[sandbox])
    detector = AssetConflictDetector(mo2_mods_path=mods, path_validator=validator)

    with pytest.raises(PathViolationError):
        detector.scan_mod_directory("BrokenMod", calculate_checksums=False)


def test_scan_mod_directory_rechaza_nested_symlink_o_junction_fuera_del_sandbox_antes_de_descenso(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-S6: Un subdirectorio symlink/junction que apunta fuera de las roots autorizadas
    lanza PathViolationError de forma fail-closed ANTES de descender o enumerar
    archivos del target externo (validate-before-descent)."""
    sandbox = tmp_path / "sandbox"
    mo2 = sandbox / "MO2"
    mods = mo2 / "mods"
    mod_a = mods / "ModA"
    mod_a.mkdir(parents=True)
    (mod_a / "textures").mkdir()
    (mod_a / "textures" / "local.dds").write_bytes(b"LOCAL")

    # Target externo con un archivo que NO debe ser alcanzado ni enumerado
    outside = tmp_path / "outside"
    outside_target = outside / "secret_assets"
    outside_target.mkdir(parents=True)
    (outside_target / "secret.dds").write_bytes(b"SECRET")

    nested_link = mod_a / "textures" / "nested_outside"
    _crear_symlink_o_junction(outside_target, nested_link)

    validator = PathValidator(roots=[sandbox])
    validated_paths: list[pathlib.Path] = []
    original_validate = validator.validate

    def spy_validate(path: str | pathlib.Path, *, strict_symlink: bool = True) -> pathlib.Path:
        p = pathlib.Path(path)
        validated_paths.append(p)
        return original_validate(p, strict_symlink=strict_symlink)

    validator.validate = spy_validate  # type: ignore[assignment]

    import os

    walk_roots_yielded: list[pathlib.Path] = []
    original_walk = os.walk

    def spy_walk(top: str | os.PathLike[str], **kwargs: object):
        for root, dirs, files in original_walk(top, **kwargs):  # type: ignore[arg-type]
            walk_roots_yielded.append(pathlib.Path(root))
            yield root, dirs, files

    import sky_claw.local.assets.asset_scanner as asset_scanner_module

    monkeypatch.setattr(asset_scanner_module.os, "walk", spy_walk)

    detector = AssetConflictDetector(mo2_mods_path=mods, path_validator=validator)

    with pytest.raises(PathViolationError):
        detector.scan_mod_directory("ModA", calculate_checksums=False)

    # Validate-before-descent:
    # 1. os.walk NUNCA descendió al directorio externo nested_link
    assert nested_link not in walk_roots_yielded, (
        f"os.walk descendió a {nested_link} antes de validar: {walk_roots_yielded}"
    )
    # 2. NINGÚN archivo dentro del target externo fue validado, recorrido ni enumerado
    archivos_validados_str = [str(p) for p in validated_paths]
    assert not any("secret.dds" in s for s in archivos_validados_str), (
        f"El escáner enumeró archivos del target externo antes de validar el directorio: {archivos_validados_str}"
    )


def test_scan_mod_directory_funciona_con_mod_legitimo_en_sandbox(tmp_path: pathlib.Path) -> None:
    """T-S7 (happy-path): un mod legítimo con assets dentro de la root autorizada
    es recorrido normalmente y mapea todos sus assets."""
    sandbox = tmp_path / "sandbox"
    mo2 = sandbox / "MO2"
    mods = mo2 / "mods"
    mod_a = mods / "ModLegitimo"
    (mod_a / "textures").mkdir(parents=True)
    (mod_a / "textures" / "a.dds").write_bytes(b"DDS_DATA")
    (mod_a / "meshes").mkdir(parents=True)
    (mod_a / "meshes" / "b.nif").write_bytes(b"NIF_DATA")

    validator = PathValidator(roots=[sandbox])
    detector = AssetConflictDetector(mo2_mods_path=mods, path_validator=validator)

    assets = detector.scan_mod_directory("ModLegitimo", calculate_checksums=False)

    assert "textures/a.dds" in assets
    assert "meshes/b.nif" in assets
    assert assets["textures/a.dds"].asset_type == AssetType.TEXTURE
    assert assets["meshes/b.nif"].asset_type == AssetType.MESH
    assert assets["textures/a.dds"].size_bytes == 8


def test_detect_conflicts_bloquea_symlink_externo_con_checksum_desactivado(tmp_path: pathlib.Path) -> None:
    """T-S8: detect_conflicts() (ruta productiva de scan()) propaga PathViolationError
    cuando un mod habilitado es un symlink a un directorio externo, aunque
    calculate_checksums sea False."""
    sandbox = tmp_path / "sandbox"
    mo2 = sandbox / "MO2"
    mods = mo2 / "mods"
    mods.mkdir(parents=True)
    profiles = mo2 / "profiles" / "Default"
    profiles.mkdir(parents=True)
    (profiles / "modlist.txt").write_text("+ModExterno\n", encoding="utf-8")

    outside = tmp_path / "outside"
    outside_mod = outside / "ModExterno"
    outside_mod.mkdir(parents=True)
    (outside_mod / "textures").mkdir()
    (outside_mod / "textures" / "shared.dds").write_bytes(b"DATA")

    link_mod = mods / "ModExterno"
    _crear_symlink_o_junction(outside_mod, link_mod)

    validator = PathValidator(roots=[sandbox])
    detector = AssetConflictDetector(mo2_mods_path=mods, profile_name="Default", path_validator=validator)

    with pytest.raises(PathViolationError):
        detector.detect_conflicts()


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


def _targets_de(nodo: ast.Assign | ast.AnnAssign) -> list[ast.expr]:
    """Normaliza los targets de una asignación simple o anotada."""
    return nodo.targets if isinstance(nodo, ast.Assign) else [nodo.target]


def _unica_llamada_al_builder(miembro: ast.FunctionDef) -> ast.Call:
    """Devuelve la única llamada a build_orchestration_composition en __init__."""
    llamadas = [
        sub
        for sub in ast.walk(miembro)
        if isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Name)
        and sub.func.id == "build_orchestration_composition"
    ]
    assert len(llamadas) == 1, (
        f"__init__ debe tener EXACTAMENTE UNA llamada a build_orchestration_composition "
        f"(encontradas {len(llamadas)}) — múltiples llamadas podrían mezclar contratos"
    )
    return llamadas[0]


def test_no_quedan_seams_bound_del_supervisor_al_builder() -> None:
    """Guardrail de familia (enumera, no muestrea).

    De todos los kwargs entregados a ``build_orchestration_composition``, los
    que son métodos bound del supervisor (``self.<método>``) deben ser
    exactamente cero: ninguno.

    Tras PR4, el único seam de dominio residual era
    ``plugin_limit_guard ← self._run_plugin_limit_guard``. PR5 (plugin-limit)
    lo extrajo a ``PluginLimitGuard.run`` (callable estrecho), así que el
    inventario exige ahora ``{}``. Un callable bound que reaparezca sin pasar
    por su extracción rompe este ancla (inventario exhaustivo, no un caso por
    cada seam).
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
        builder_call = _unica_llamada_al_builder(miembro)
        for kw in builder_call.keywords:
            assert kw.arg is not None, (
                "build_orchestration_composition no puede usar `**kwargs` "
                "porque impediría inventariar exhaustivamente los seams bound al supervisor"
            )
            valor = kw.value
            if (
                isinstance(valor, ast.Attribute)
                and isinstance(valor.value, ast.Name)
                and valor.value.id == "self"
                and valor.attr in nombres_metodo
            ):
                bound_del_supervisor[kw.arg] = valor.attr
        break

    assert bound_del_supervisor == {}, (
        f"Seams bound al supervisor entregados al builder: {bound_del_supervisor}. "
        "Tras PR5 (plugin-limit) el seam de domain debe estar extraído: "
        "PluginLimitGuard entrega su .run como callable estrecho al builder. "
        "Si reaparece un bound method de SupervisorAgent es wiring nuevo, no refactor."
    )


def _referencia_logica(nodo: ast.expr) -> tuple[str, str] | None:
    """Normaliza una referencia a valor, sin depender de la forma superficial.

    - ``scanner``            (``ast.Name``)                → ``("name", "scanner")``
    - ``self._asset_scanner`` (``ast.Attribute`` de self)  → ``("self_attr", "_asset_scanner")``
    - cualquier otra cosa (llamadas, sumas, …)             → ``None``

    Así T9 compara la IDENTIDAD LÓGICA del objeto y no el estilo de
    asignación: ``scanner.scan`` y ``self._asset_scanner.scan`` son la misma
    referencia cuando el scanner se construyó bajo esa referencia.
    """
    if isinstance(nodo, ast.Name):
        return ("name", nodo.id)
    if isinstance(nodo, ast.Attribute) and isinstance(nodo.value, ast.Name) and nodo.value.id == "self":
        return ("self_attr", nodo.attr)
    return None


def test_supervisor_cablea_el_scanner_a_la_composition() -> None:
    """T9 (AST): ``__init__`` construye AssetConflictScanner SOLO con
    ``path_resolver=self._path_resolver`` y ``path_validator=self._modding_validator``
    y pasa el ``.scan`` / ``.scan_json`` del MISMO objeto al builder.

    El oracle comprueba SEMÁNTICA, no estilo AST: no le importa si el scanner
    se construye en una variable local (``asset_conflict_scanner = AssetConflictScanner(...)``)
    o directamente en un atributo (``self._asset_scanner = AssetConflictScanner(...)``),
    ni si los callbacks nombran la variable local o el atributo de ``self``
    (ambas son REFACTORES EQUIVALENTES que deben pasar; un WIRING DIFERENTE
    —otro scanner, otro método, callback viejo del supervisor, alias reasignado— debe fallar).

    Invariantes:
      - EXACTAMENTE UNA construcción productiva de ``AssetConflictScanner``;
        un scanner duplicado rompe el test.
      - EXACTAMENTE UNA llamada a ``build_orchestration_composition``; múltiples
        llamadas no pueden mezclar contratos ni ocultar wires inválidos.
      - TODOS los argumentos se inventarían: ni posicionales, ni ``**extras``,
        ni kwargs inesperados, tanto en el scanner como en el builder.
      - ``path_validator`` apunta al validator de modding (no ``_path_validator``
        del rollback ni un validator fabricado).
      - el MISMO objeto construido alimenta ``scan_asset_conflicts`` (vía
        ``.scan``) y ``scan_asset_conflicts_json`` (vía ``.scan_json``); se
        detecta la inversión plain/JSON y los callbacks viejos del supervisor.
      - se rastrea el binding vigente (local y self.attr) por orden de línea
        hasta el builder: reasignar una referencia a otro valor invalida el
        binding para el oracle; soporta Assign y AnnAssign; NO hay dataflow general.

    Mutaciones de contraste ejecutadas en el PR: la forma ``self.attr`` pura
    pasa (equivalente); un SEGUNDO scanner cableado, el swap plain/JSON,
    ``self.scan_asset_conflicts``, ``path_validator=self._path_validator``,
    ``path_validator=fabricado()``, un kwarg extra, ``**extras`` en scanner,
    ``**extras`` en builder, reasignación de alias local/self y segunda llamada al builder fallan.
    """
    arbol = _arbol_supervisor()
    clase = _cuerpo_supervisor(arbol)
    for miembro in clase.body:
        if not (isinstance(miembro, ast.FunctionDef) and miembro.name == "__init__"):
            continue

        # Recorremos las asignaciones (simples y anotadas) en ORDEN DE LÍNEA para
        # poder rastrear bindings vigentes (target = referencia) sin dataflow general.
        asignaciones = sorted(
            (n for n in ast.walk(miembro) if isinstance(n, (ast.Assign, ast.AnnAssign))),
            key=lambda n: n.lineno,
        )

        construcciones = [
            sub
            for sub in asignaciones
            if sub.value is not None
            and isinstance(sub.value, ast.Call)
            and isinstance(sub.value.func, ast.Name)
            and sub.value.func.id == "AssetConflictScanner"
        ]
        assert len(construcciones) == 1, (
            f"__init__ debe construir EXACTAMENTE UN AssetConflictScanner "
            f"(encontrados {len(construcciones)}) — un segundo scanner cableado "
            "sería wiring distinto, no un refactor equivalente"
        )

        construccion = construcciones[0]
        call = construccion.value
        assert isinstance(call, ast.Call)  # estrechamiento para el type-checker
        kw_named: dict[str, ast.expr] = {}
        for kw in call.keywords:
            if kw.arg is None:
                raise AssertionError(
                    "AssetConflictScanner recibe `**kwargs` no nominales — "
                    "el contrato del scanner es path_resolver + path_validator explícitos"
                )
            kw_named[kw.arg] = kw.value

        # 1. Contrato exacto del constructor: sin posicionales ni kwargs raros.
        assert call.args == [], (
            "AssetConflictScanner debe construirse solo con kwargs nominales "
            f"(recibió {len(call.args)} argumentos posicionales)"
        )
        assert set(kw_named) == {"path_resolver", "path_validator"}, (
            f"AssetConflictScanner se construye con kwargs {set(kw_named)} — "
            "se espera exactamente {path_resolver, path_validator}"
        )
        assert _formato_kwarg(kw_named["path_resolver"]) == "self._path_resolver", (
            "path_resolver debe ser self._path_resolver"
        )
        assert _formato_kwarg(kw_named["path_validator"]) == "self._modding_validator", (
            "path_validator debe ser self._modding_validator (el validator de modding, "
            "NO el backup-only del rollback ni un validator fabricado)"
        )

        # 2. Exigir exactamente UNA llamada a build_orchestration_composition.
        builder_call = _unica_llamada_al_builder(miembro)

        # 3. Rastrear bindings vigentes de la identidad lógica del scanner
        #    desde su construcción hasta la llamada al builder.
        scanner_id = "asset_conflict_scanner#1"
        bindings: dict[tuple[str, str], str] = {}
        for target in _targets_de(construccion):
            ref = _referencia_logica(target)
            if ref is not None:
                bindings[ref] = scanner_id
        assert any(v == scanner_id for v in bindings.values()), (
            "La construcción del scanner debe asignarse a un nombre local o a un "
            "atributo de self (no a suscripciones ni targets dinámicos)"
        )

        for sub in asignaciones:
            if sub.lineno <= construccion.lineno:
                continue
            if sub.lineno >= builder_call.lineno:
                continue
            val_ref = _referencia_logica(sub.value) if sub.value is not None else None
            val_id = bindings.get(val_ref) if val_ref is not None else None
            for target in _targets_de(sub):
                target_ref = _referencia_logica(target)
                if target_ref is not None:
                    if val_id == scanner_id:
                        bindings[target_ref] = scanner_id
                    else:
                        bindings[target_ref] = "other"

        # 4. Los callbacks del builder deben provenir de una referencia vigente del scanner.
        kwargs_al_builder: dict[str, ast.expr] = {}
        for kw in builder_call.keywords:
            if kw.arg is None:
                raise AssertionError(
                    "build_orchestration_composition recibe `**kwargs` no nominales — "
                    "los contratos del composition root deben ser explícitos"
                )
            kwargs_al_builder[kw.arg] = kw.value

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
            ref_base = _referencia_logica(valor.value)
            assert ref_base is not None and bindings.get(ref_base) == scanner_id, (
                f"{builder_kwarg} usa una referencia que no resuelve al scanner construido "
                f"(ref: {ref_base}, bindings: {bindings}) — identidad lógica rota"
            )
        return
    raise AssertionError("No se encontró SupervisorAgent.__init__")
