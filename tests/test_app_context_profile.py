"""Perfil MO2 único para la sesión del agente.

El perfil se resuelve antes del wiring de FOMOD/router y no puede depender del
``Default`` hardcodeado en cada consumidor.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import sky_claw.app_context as app_context
from sky_claw.app.security.path_validator import PathViolationError


def _resolver():
    resolver = getattr(app_context, "_resolve_mo2_profile", None)
    assert resolver is not None, "falta la fuente única del perfil MO2"
    return resolver


def _constructor_prompt():
    constructor = getattr(app_context, "_build_system_prompt", None)
    assert constructor is not None, "falta el prompt parametrizado por perfil"
    return constructor


def test_perfil_cli_prevalece_sobre_entorno() -> None:
    with patch.dict("os.environ", {"MO2_PROFILE": "Entorno"}):
        assert _resolver()(SimpleNamespace(profile="CLI")) == "CLI"


def test_perfil_entorno_prevalece_sobre_default() -> None:
    with patch.dict("os.environ", {"MO2_PROFILE": "Entorno"}):
        assert _resolver()(SimpleNamespace(profile="")) == "Entorno"


def test_perfil_default_sin_configuracion() -> None:
    with patch.dict("os.environ", {}, clear=True):
        assert _resolver()(SimpleNamespace()) == "Default"


def test_objeto_no_string_de_fixture_degrada_al_fallback() -> None:
    with patch.dict("os.environ", {}, clear=True):
        assert _resolver()(SimpleNamespace(profile=object())) == "Default"


def test_perfil_invalido_falla_antes_de_construir_rutas() -> None:
    with pytest.raises(PathViolationError):
        _resolver()(SimpleNamespace(profile="../escape"))


def test_prompt_nombra_el_perfil_de_la_sesion() -> None:
    prompt = _constructor_prompt()("Requiem")

    assert "perfil 'Requiem'" in prompt
    assert "perfil 'Default'" not in prompt


def test_app_context_propaga_un_mismo_perfil_a_fomod_registry_y_router() -> None:
    source = inspect.getsource(app_context.AppContext._start_full_inner)

    assert "profile=active_profile" in source
    assert "mo2_profile=active_profile" in source
    assert 'mo2.root / "profiles" / active_profile' in source
    assert 'mo2_profile = os.path.join(mo2_root, "profiles", "Default")' not in source
