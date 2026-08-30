"""Caracterización del provider de deps del ritual de grass (PR3).

PR3 del Strangler Fig extrae ``SupervisorAgent._build_grass_dependencies`` y
``_resolve_grass_mo2_controller`` a :class:`GrassRuntimeDepsProvider`
(``sky_claw/app/orchestrator/grass_runtime_deps.py``). Estas anclas fijan:

- Contrato lazy: construir el provider NO resuelve paths (en la GUI
  ``MO2_PATH``/``SKYRIM_PATH`` se hidratan DESPUÉS de construir el supervisor;
  review Codex #301). La resolución ocurre al invocar el provider.
- Clona el perfil ACTIVO, no ``Default`` (finding #3 de la extracción original).
- Usa el validator de modding, no el rollback backup-only (finding #2).
- Resolución all-or-nothing: sin MO2 o sin Skyrim → ``None`` (el servicio
  responde con su error de contrato accionable, no un crash).
- MO2Controller PROPIO del ritual, aislado del AppContext y memoizado entre
  corridas (review Codex #305 C2 + follow-up H4).
- Frontera: el provider no conoce a ``SupervisorAgent`` (AST).
- Frontera inversa: ``SupervisorAgent`` ya no contiene la lógica del dominio
  grass (AST) — reintroducir los métodos rompe esta ancla (mutación T11).
- Wiring: ``__init__`` construye el provider y lo pasa a
  ``build_orchestration_composition`` (AST, no string matching).
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from unittest.mock import MagicMock

from sky_claw.app.orchestrator.grass_runtime_deps import GrassRuntimeDepsProvider
from sky_claw.app.security.path_validator import PathValidator
from sky_claw.local.mo2.grass_profile import GrassProfileManager
from sky_claw.local.mo2.vfs import MO2Controller
from sky_claw.local.tools.grass_cache_service import GrassRuntimeDeps


def _provider_con_resolver(
    mo2_root: pathlib.Path | None,
    game_path: pathlib.Path | None,
    validator: PathValidator,
    *,
    profile_name: str = "Default",
) -> GrassRuntimeDepsProvider:
    """Provider con resolver doblado y validator real (patrón del test original)."""
    resolver = MagicMock()
    resolver.get_mo2_path.return_value = mo2_root
    resolver.get_skyrim_path.return_value = game_path
    return GrassRuntimeDepsProvider(
        path_resolver=resolver,
        path_validator=validator,
        profile_name=profile_name,
    )


# ---------------------------------------------------------------------------
# Caracterización comportamental (ported de test_supervisor_grass_wiring.py)
# ---------------------------------------------------------------------------


def test_provider_con_paths_resueltos_arma_las_4_deps_reales(tmp_path: pathlib.Path) -> None:
    """Con MO2_PATH y SKYRIM_PATH resueltos, arma las 4 deps reales."""
    mo2_root = tmp_path / "MO2"
    mo2_root.mkdir()
    game = tmp_path / "Skyrim"
    game.mkdir()
    provider = _provider_con_resolver(mo2_root, game, PathValidator(roots=[tmp_path]))

    deps = provider()

    assert isinstance(deps, GrassRuntimeDeps)
    assert isinstance(deps.profile_manager, GrassProfileManager)
    assert isinstance(deps.mo2, MO2Controller)
    assert deps.game_path == game
    assert deps.overwrite_grass_dir == mo2_root / "overwrite" / "Grass"


def test_provider_clona_el_perfil_activo(tmp_path: pathlib.Path) -> None:
    """Finding #3: se clona el perfil ACTIVO, no 'Default'."""
    mo2_root = tmp_path / "MO2"
    mo2_root.mkdir()
    game = tmp_path / "Skyrim"
    game.mkdir()
    provider = _provider_con_resolver(mo2_root, game, PathValidator(roots=[tmp_path]), profile_name="MiModlist")

    deps = provider()

    assert deps is not None
    assert deps.profile_manager._source_profile == "MiModlist"


def test_provider_usa_el_validator_de_modding(tmp_path: pathlib.Path) -> None:
    """Finding #2: MO2Controller/GrassProfileManager reciben el validator de
    modding inyectado, no el rollback backup-only."""
    mo2_root = tmp_path / "MO2"
    mo2_root.mkdir()
    game = tmp_path / "Skyrim"
    game.mkdir()
    modding = PathValidator(roots=[tmp_path])
    provider = _provider_con_resolver(mo2_root, game, modding)

    deps = provider()

    assert deps is not None
    assert deps.profile_manager._validator is modding


def test_provider_sin_mo2_devuelve_none(tmp_path: pathlib.Path) -> None:
    """Sin MO2_PATH la resolución es all-or-nothing: None (no deps parciales)."""
    game = tmp_path / "Skyrim"
    game.mkdir()
    provider = _provider_con_resolver(None, game, PathValidator(roots=[tmp_path]))

    assert provider() is None


def test_provider_sin_skyrim_devuelve_none(tmp_path: pathlib.Path) -> None:
    """Sin SKYRIM_PATH (Fase C necesita game_path) también None."""
    mo2_root = tmp_path / "MO2"
    mo2_root.mkdir()
    provider = _provider_con_resolver(mo2_root, None, PathValidator(roots=[tmp_path]))

    assert provider() is None


def test_provider_controller_aislado_del_appcontext(tmp_path: pathlib.Path) -> None:
    """Codex #305 C2: el ritual usa un MO2Controller PROPIO (aislado del del
    AppContext): ``close_game()`` mata TODOS los PIDs trackeados, así que
    compartir tracking con el juego lanzado por la tool normal lo mataría."""
    mo2_root = tmp_path / "MO2"
    mo2_root.mkdir()
    game = tmp_path / "Skyrim"
    game.mkdir()
    provider = _provider_con_resolver(mo2_root, game, PathValidator(roots=[tmp_path]))

    deps = provider()

    assert deps is not None
    # La instancia es propia del ritual: el memo estaba vacío antes de la
    # resolución (no se inyectó un controller externo).
    assert isinstance(deps.mo2, MO2Controller)
    assert deps.mo2 is provider._mo2_controller
    assert deps.profile_manager._controller is deps.mo2


def test_provider_memoiza_el_controller(tmp_path: pathlib.Path) -> None:
    """Follow-up H4: el controller propio se memoiza — dos resoluciones devuelven
    la MISMA instancia (crear una nueva por llamada partiría el tracking de PIDs
    entre corridas del ritual)."""
    mo2_root = tmp_path / "MO2"
    mo2_root.mkdir()
    game = tmp_path / "Skyrim"
    game.mkdir()
    provider = _provider_con_resolver(mo2_root, game, PathValidator(roots=[tmp_path]))

    primera = provider()
    segunda = provider()

    assert primera is not None and segunda is not None
    assert primera.mo2 is segunda.mo2


# ---------------------------------------------------------------------------
# Laziness (review Codex #301): la resolución ocurre al ejecutar el ritual
# ---------------------------------------------------------------------------


def test_construir_el_provider_no_resuelve_paths(tmp_path: pathlib.Path) -> None:
    """La construcción NO llama a los getters: MO2_PATH/SKYRIM_PATH se hidratan
    después de construir el supervisor en la GUI."""
    resolver = MagicMock()
    GrassRuntimeDepsProvider(
        path_resolver=resolver,
        path_validator=PathValidator(roots=[tmp_path]),
        profile_name="Sesion",
    )

    resolver.get_mo2_path.assert_not_called()
    resolver.get_skyrim_path.assert_not_called()


def test_la_resolucion_ocurre_al_invocar_el_provider(tmp_path: pathlib.Path) -> None:
    """Trayendo el falso negativo: un provider "pre-resuelto" (que resolviera en
    __init__) dejaría a los getters llamados una vez ANTES del primer call."""
    mo2_root = tmp_path / "MO2"
    mo2_root.mkdir()
    game = tmp_path / "Skyrim"
    game.mkdir()
    resolver = MagicMock()
    resolver.get_mo2_path.return_value = mo2_root
    resolver.get_skyrim_path.return_value = game
    provider = GrassRuntimeDepsProvider(
        path_resolver=resolver,
        path_validator=PathValidator(roots=[tmp_path]),
        profile_name="Sesion",
    )

    deps = provider()

    assert deps is not None
    resolver.get_mo2_path.assert_called_once_with()
    resolver.get_skyrim_path.assert_called_once_with()


# ---------------------------------------------------------------------------
# Anclas estructurales (AST) — fronteras del seam extraído
# ---------------------------------------------------------------------------


def _arbol_de(clase_o_modulo) -> ast.Module:
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


def test_provider_no_conoce_al_supervisor() -> None:
    """T1 (AST): el provider no importa ni referencia a SupervisorAgent.

    Es AST-real: detecta referencias en nombres, atributos e imports, incluidos
    imports perezosos o locales al cuerpo de funciones.
    """
    inventario = _inventario_nombres_e_imports(_arbol_de(GrassRuntimeDepsProvider))

    assert "SupervisorAgent" not in inventario, (
        "grass_runtime_deps referencia 'SupervisorAgent' — el flujo es provider ← composition, nunca al revés"
    )
    assert "sky_claw.app.orchestrator.supervisor" not in inventario


_METODOS_GRASS_DESTERRADOS = frozenset({"_build_grass_dependencies", "_resolve_grass_mo2_controller"})
_CONSTRUCTORES_GRASS_DESTERRADOS = frozenset({"MO2Controller", "GrassProfileManager", "GrassRuntimeDeps"})


def _arbol_supervisor() -> ast.Module:
    from sky_claw.app.orchestrator import supervisor

    return _arbol_de(supervisor)


def _cuerpo_supervisor(arbol: ast.Module) -> ast.ClassDef:
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == "SupervisorAgent":
            return nodo
    raise AssertionError("No se encontró la clase SupervisorAgent en supervisor.py")


def test_supervisor_ya_no_tiene_los_metodos_de_grass() -> None:
    """T2/T11 (AST): los métodos extraídos no vigilen como parche post-hoc.

    Reintroducir ``_build_grass_dependencies`` o ``_resolve_grass_mo2_controller``
    en la clase (mutación natural del refactor) rompe la ancla.
    """
    clase = _cuerpo_supervisor(_arbol_supervisor())
    nombres = {m.name for m in clase.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}

    assert _METODOS_GRASS_DESTERRADOS.isdisjoint(nombres), (
        f"SupervisorAgent recuperó métodos de grass: {_METODOS_GRASS_DESTERRADOS & nombres}"
    )


def test_supervisor_no_construye_objetos_del_dominio_grass() -> None:
    """T2/T11 (AST): ningún call site del módulo construye los tipos del dominio.

    Resuelve aliases ``from X import Y as Z`` (módulo y locales) para capturar
    alias-dodging, y compara el nombre FINAL de llamadas dotted.
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

    colision = _CONSTRUCTORES_GRASS_DESTERRADOS & llamadas
    assert not colision, (
        f"supervisor.py sigue construyendo {colision} — esos objetos viven en "
        "GrassRuntimeDepsProvider (sky_claw/app/orchestrator/grass_runtime_deps.py)"
    )

    # La memoria del controller tampoco puede reaparecer como atributo del
    # supervisor (era ``self._mo2_controller``).
    inventario = _inventario_nombres_e_imports(arbol)
    assert "_mo2_controller" not in inventario


def test_supervisor_cablea_el_provider_a_la_composition() -> None:
    """Wiring (AST): __init__ construye el provider y lo pasa al builder.

    Ancla las dos mitades del sillón: quitar la construcción del provider O
    quitar el kwarg al builder rompe el test — el caso "provider construido
    pero no cableado" no se deja pasar. Además fija los kwargs exactos del
    constructor: ``path_validator=self._modding_validator`` es load-bearing
    (pasar el rollback backup-only ``_path_validator`` haría fallar el ritual
    en runtime sin que el comportamiento local del provider lo detecte).
    """
    arbol = _arbol_supervisor()
    clase = _cuerpo_supervisor(arbol)
    kwargs_esperados = {
        "path_resolver": "self._path_resolver",
        "path_validator": "self._modding_validator",
        "profile_name": "self.profile_name",
    }
    for miembro in clase.body:
        if isinstance(miembro, ast.FunctionDef) and miembro.name == "__init__":
            construcciones = []
            kwargs_al_builder: dict[str, ast.expr] = {}
            for sub in ast.walk(miembro):
                if (
                    isinstance(sub, ast.Assign)
                    and isinstance(sub.value, ast.Call)
                    and isinstance(sub.value.func, ast.Name)
                    and sub.value.func.id == "GrassRuntimeDepsProvider"
                    and any(isinstance(t, ast.Name) and t.id == "grass_runtime_deps_provider" for t in sub.targets)
                ):
                    construcciones.append(sub)
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "build_orchestration_composition"
                ):
                    for kw in sub.keywords:
                        kwargs_al_builder[kw.arg] = kw.value
            assert construcciones, "__init__ no construye GrassRuntimeDepsProvider"
            kwargs_reales = {}
            for kw in construcciones[0].value.keywords:
                v = kw.value
                if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) and v.value.id == "self":
                    kwargs_reales[kw.arg] = f"self.{v.attr}"
                elif isinstance(v, ast.Name):
                    kwargs_reales[kw.arg] = v.id
            assert kwargs_reales == kwargs_esperados, (
                f"GrassRuntimeDepsProvider se construye con {kwargs_reales} — "
                f"se espera exactamente {kwargs_esperados} "
                "(path_validator debe ser _modding_validator, no el rollback backup-only)"
            )
            provider_kw = kwargs_al_builder.get("grass_runtime_deps_provider")
            assert isinstance(provider_kw, ast.Name) and provider_kw.id == "grass_runtime_deps_provider", (
                "build_orchestration_composition debe recibir el provider construido, "
                "no otro callable (p. ej. un método del supervisor)"
            )
            return
    raise AssertionError("No se encontró SupervisorAgent.__init__")
