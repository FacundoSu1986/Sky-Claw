"""Formato de los mensajes al operador sobre la etapa 9: la fuente única que usan el notificador y ``/lod_status``.

Las funciones de ``dyndolod_mensajes`` son puras: se prueban sin canal ni bus. Lo que estos tests fijan:

* la duración y el tamaño se leen como un humano (y una duración TRUNCA: no se redondea hacia arriba);
* **todo texto dinámico de un payload que llega al mensaje, llega escapado.** La propiedad es del
  MECANISMO, no de cada campo: se enumeran por introspección los campos de texto de los payloads y se
  congela por igualdad literal cuáles se muestran (ver «La regla que más se viola» en ``AGENTS.md``).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from sky_claw.app.core.event_payloads import (
    DynDOLODPipelineCompletedPayload,
    DynDOLODPipelineProgressPayload,
    DynDOLODPipelineStartedPayload,
)
from sky_claw.app.orchestrator import dyndolod_mensajes as mensajes


@pytest.mark.parametrize(
    ("segundos", "esperado"),
    [
        (0, "0s"),
        (0.9, "0s"),
        (59.9, "59s"),  # trunca: round() daría "1m 0s" y una duración no se redondea hacia arriba
        (60, "1m 0s"),
        (272.5, "4m 32s"),
        (3600, "1h 0m 0s"),
        (14399, "3h 59m 59s"),
        (-5, "0s"),
    ],
)
def test_la_duracion_se_lee_como_un_humano(segundos: float, esperado: str) -> None:
    assert mensajes.duracion_legible(segundos) == esperado


@pytest.mark.parametrize(
    ("bytes_", "esperado"),
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (4_300_000, "4.1 MiB"),
        (5 * 1024**3, "5.0 GiB"),
        (-5, "0 B"),
    ],
)
def test_el_tamano_se_lee_como_un_humano(bytes_: int, esperado: str) -> None:
    assert mensajes.tamano_legible(bytes_) == esperado


def test_el_peor_caso_de_un_aviso_de_actividad_entra_en_un_mensaje() -> None:
    """Nombre del log y última línea de 5000 caracteres de `&` (el peor caso de escape, x5) siguen bajo 4096."""
    payload = DynDOLODPipelineProgressPayload(
        kind="stalled",
        log="&" * 5000,
        elapsed_seconds=10.0,
        idle_seconds=900.0,
        log_size_bytes=100,
        last_line="&" * 5000,
    )

    texto = mensajes.formatear_progreso(payload)

    assert len(texto) < 4096, "el sender partiría el mensaje en medio de una etiqueta HTML"
    assert texto.count("<pre>") == texto.count("</pre>")
    assert texto.count("<code>") == texto.count("</code>")


# ---------------------------------------------------------------------------
# Ancla: todo texto dinámico del payload que llega al mensaje, llega escapado
# ---------------------------------------------------------------------------

_HOSTIL = "<x&y>"
_HOSTIL_ESCAPADO = "&lt;x&amp;y&gt;"

_BASE_STARTED: dict[str, Any] = {"preset": "Medium", "run_texgen": True}
_BASE_COMPLETED_FALLO: dict[str, Any] = {
    "preset": "Medium",
    "run_texgen": True,
    "success": False,
    "texgen_success": True,
    "dyndolod_success": False,
    "errors": ("boom",),
    "duration_seconds": 1.0,
    "rolled_back": True,
}


def _campos_de_texto(modelo: type[BaseModel]) -> list[str]:
    """Campos ``str`` y ``tuple[str, ...]`` del payload, por INTROSPECCIÓN (no por una lista escrita a mano)."""
    return [nombre for nombre, campo in modelo.model_fields.items() if campo.annotation in (str, tuple[str, ...])]


_BASE_PROGRESO: dict[str, Any] = {
    "kind": "stalled",
    "log": "DynDOLOD_SSE_log.txt",
    "elapsed_seconds": 10.0,
    "idle_seconds": 900.0,
    "log_size_bytes": 100,
    "last_line": "x",
}

_CASOS_DE_ESCAPE: list[tuple[type[BaseModel], dict[str, Any], str]] = (
    [
        (DynDOLODPipelineStartedPayload, _BASE_STARTED, campo)
        for campo in _campos_de_texto(DynDOLODPipelineStartedPayload)
    ]
    + [
        (DynDOLODPipelineCompletedPayload, _BASE_COMPLETED_FALLO, campo)
        for campo in _campos_de_texto(DynDOLODPipelineCompletedPayload)
    ]
    + [
        (DynDOLODPipelineProgressPayload, _BASE_PROGRESO, campo)
        for campo in _campos_de_texto(DynDOLODPipelineProgressPayload)
    ]
)


def _mensaje_con_el_campo_hostil(modelo: type[BaseModel], base: dict[str, Any], campo: str) -> str:
    es_texto_plano = modelo.model_fields[campo].annotation is str
    payload = modelo(**{**base, campo: _HOSTIL if es_texto_plano else (_HOSTIL,)})
    if isinstance(payload, DynDOLODPipelineStartedPayload):
        return mensajes.formatear_inicio(payload)
    if isinstance(payload, DynDOLODPipelineProgressPayload):
        return mensajes.formatear_progreso(payload)
    assert isinstance(payload, DynDOLODPipelineCompletedPayload)
    return mensajes.formatear_fin(payload)


class TestEscapeDeTextoDinamico:
    """La propiedad es del MECANISMO, no de cada campo: un campo de texto nuevo que se muestre sin escapar rompe esto."""

    @pytest.mark.parametrize(
        ("modelo", "base", "campo"),
        _CASOS_DE_ESCAPE,
        ids=[f"{modelo.__name__}.{campo}" for modelo, _base, campo in _CASOS_DE_ESCAPE],
    )
    def test_ningun_campo_de_texto_llega_crudo_al_mensaje(
        self, modelo: type[BaseModel], base: dict[str, Any], campo: str
    ) -> None:
        mensaje = _mensaje_con_el_campo_hostil(modelo, base, campo)

        assert _HOSTIL not in mensaje, f"{modelo.__name__}.{campo} llega sin escapar: Telegram rechazaría el aviso"

    def test_los_campos_de_texto_que_el_mensaje_muestra_estan_congelados(self) -> None:
        """Enumera por introspección cuáles campos de texto se muestran y los congela por igualdad literal.

        Un campo de texto nuevo que el mensaje empiece a mostrar entra a este conjunto y rompe la igualdad: obliga a
        decidir (y a que el test de arriba verifique) que se escapa.
        """
        mostrados = {
            (modelo.__name__, campo)
            for modelo, base, campo in _CASOS_DE_ESCAPE
            if _HOSTIL_ESCAPADO in _mensaje_con_el_campo_hostil(modelo, base, campo)
        }

        assert mostrados == {
            ("DynDOLODPipelineStartedPayload", "preset"),
            ("DynDOLODPipelineStartedPayload", "operator_instructions"),
            ("DynDOLODPipelineCompletedPayload", "errors"),
            ("DynDOLODPipelineProgressPayload", "log"),
            ("DynDOLODPipelineProgressPayload", "last_line"),
        }
