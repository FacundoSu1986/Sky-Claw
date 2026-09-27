"""Ancla estructural GP2-S4A: la frontera de seguridad del slice.

Este archivo existe para que una violación accidental de la HARD SAFETY BOUNDARY
sea difícil de comitear. No prueba comportamiento: prueba AUSENCIA de capacidad.

- ``SB-01``: los cuatro módulos de S4-A no importan ni llaman
  ``SetSecurityInfo`` / ``SetNamedSecurityInfoW`` / ``SetFileSecurityW``.
- ``SB-02``: ``protection_journal`` (modelo puro) no importa el journal de
  aplicación ``sky_claw.app.db.journal`` (GP2-T16).
- ``SB-03``: ningún módulo de S4-A importa ``apply_target_dacl_by_handle`` ni
  ningún mutador de rollback de descriptores.
- ``SB-04``: S4-A no afirma ``COMMITTED`` ni ``ARCHIVING_BACKUP`` productivos.
- ``SB-05``: la CLI del helper sigue aceptando sólo ``--operation-id`` y
  ``--staging-digest`` (sin ``--golden-root``/``--authorized-plan-path``/etc.).
- ``SB-06``: el candidato nunca se copia al destino autoritativo (sin
  ``copyfile``/``shutil.copy``/``os.replace`` hacia ``authorized_plan.json``).

Si algún assert de acá falla, el commit está prohibido hasta explicar por qué.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from sky_claw.local.runtime_vault.privileged_boundary import HELPER_CLI_ALLOWED_FLAGS

_PAQUETE = pathlib.Path(__file__).resolve().parents[1] / "sky_claw" / "local" / "runtime_vault"

_MODULOS_S4A = (
    "authorized_plan.py",
    "authorized_plan_store.py",
    "protection_journal.py",
    "protection_journal_store.py",
)

#: Símbolos que NINGÚN módulo de S4-A puede referenciar.
_SIMBOLOS_PROHIBIDOS = frozenset(
    {
        "SetSecurityInfo",
        "SetNamedSecurityInfoW",
        "SetFileSecurityW",
        "SetFileSecurity",
        "apply_target_dacl_by_handle",
        "apply_rollback_security_descriptor",
        "restore_pre_sd",
        "set_kernel_object_sacl",
        "AdjustTokenPrivileges",
        "SeDebugPrivilege",
    }
)

#: Módulos que S4-A no puede importar (frontera de aplicación / GUI).
_MODULOS_PROHIBIDOS = frozenset(
    {
        "sky_claw.app.db.journal",
        "sky_claw.app.gui",
        "sky_claw.app.web",
    }
)

#: Módulos cuyas primitivas de copia convertirían staging en autoridad.
_MODULOS_DE_COPIA = frozenset({"os", "shutil", "pathlib"})

#: Atributos de copia prohibidos sobre esos módulos.
_COPIAS_PROHIBIDAS = frozenset({"copyfile", "copy", "copy2", "copytree", "replace"})

#: Símbolo representativo de cada módulo del slice (prueba de cableado en __init__).
_SIMBOLO_POR_MODULO = {
    "authorized_plan": "AuthorizedPlan",
    "authorized_plan_store": "DurableAuthorizedPlan",
    "protection_journal": "ProtectionJournal",
    "protection_journal_store": "DurableProtectionJournal",
}


def _arbol(modulo: str) -> ast.AST:
    ruta = _PAQUETE / modulo
    assert ruta.is_file(), f"no existe el módulo esperado del slice: {ruta}"
    return ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))


def _simbolos_referenciados(arbol: ast.AST) -> set[str]:
    return {nodo.id for nodo in ast.walk(arbol) if isinstance(nodo, ast.Name)} | {
        nodo.attr for nodo in ast.walk(arbol) if isinstance(nodo, ast.Attribute)
    }


def _modulos_importados(arbol: ast.AST) -> set[str]:
    modulos: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            modulos.update(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.level == 0 and nodo.module:
            modulos.add(nodo.module)
    return modulos


class TestFronteraDeSeguridad:
    @pytest.mark.parametrize("modulo", _MODULOS_S4A)
    def test_sb01_sin_primitivas_mutadoras_de_seguridad(self, modulo: str) -> None:
        arbol = _arbol(modulo)
        prohibidos = _simbolos_referenciados(arbol) & _SIMBOLOS_PROHIBIDOS
        assert prohibidos == set(), f"{modulo} referencia primitivas mutadoras prohibidas: {sorted(prohibidos)}"

    @pytest.mark.parametrize("modulo", _MODULOS_S4A)
    def test_sb02_sin_imports_de_la_frontera_de_aplicacion(self, modulo: str) -> None:
        arbol = _arbol(modulo)
        importados = _modulos_importados(arbol)
        prohibidos = {m for m in importados if any(m == p or m.startswith(p + ".") for p in _MODULOS_PROHIBIDOS)}
        assert prohibidos == set(), f"{modulo} importa módulos prohibidos: {sorted(prohibidos)}"

    def test_sb03_journal_puro_no_importa_el_journal_de_aplicacion(self) -> None:
        arbol = _arbol("protection_journal.py")
        assert "sky_claw.app.db.journal" not in _modulos_importados(arbol)

    def test_sb04_sin_committed_ni_archiving_productivos(self) -> None:
        """S4-A puede CONOCER los estados (son normativos) pero no escribirlos."""
        arbol = _arbol("protection_journal_store.py")
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.FunctionDef) and nodo.name in {"commit", "mark_committed", "archive_backup"}:
                pytest.fail(f"S4-A no debe exponer la función productiva '{nodo.name}'")
        assert "PrematureCommitError" in _simbolos_referenciados(arbol)

    def test_sb05_cli_del_helper_con_dos_flags(self) -> None:
        assert set(HELPER_CLI_ALLOWED_FLAGS) == {"--operation-id", "--staging-digest"}

    @pytest.mark.parametrize("modulo", _MODULOS_S4A)
    def test_sb06_staging_nunca_se_copia_al_destino_autoritativo(self, modulo: str) -> None:
        """Ni ``copyfile`` ni ``replace``: la promoción RE-LEE y RE-SERIALIZA."""
        arbol = _arbol(modulo)
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call) or not isinstance(nodo.func, ast.Attribute):
                continue
            base = nodo.func.value
            if isinstance(base, ast.Name) and base.id in _MODULOS_DE_COPIA:
                assert nodo.func.attr not in _COPIAS_PROHIBIDAS, (
                    f"{modulo} usa la copia prohibida '{base.id}.{nodo.func.attr}': staging nunca es autoridad"
                )

    def test_sb07_modulos_del_slice_estan_cableados(self) -> None:
        import sky_claw.local.runtime_vault as pkg

        for raiz, simbolo in _SIMBOLO_POR_MODULO.items():
            assert (_PAQUETE / f"{raiz}.py").is_file(), f"falta el módulo del slice: {raiz}.py"
            assert hasattr(pkg, simbolo), f"{simbolo} no está exportado por el paquete"
            assert simbolo in pkg.__all__, f"{simbolo} no está en __init__.__all__"

    def test_sb08_namespace_solo_agrega_dos_objetos(self) -> None:
        """``trusted_namespace`` gana dos object_name, sin tocar los existentes."""
        from sky_claw.local.runtime_vault import trusted_namespace

        for objeto in ("trusted_goldens.json", "golden_admission_record.json"):
            spec = trusted_namespace.build_namespace_dacl_spec(objeto)
            assert spec is not None
        assert trusted_namespace.AUTHORIZED_PLAN_OBJECT == "authorized_plan.json"
        assert trusted_namespace.PROTECTION_JOURNAL_OBJECT == "protection_journal.json"
