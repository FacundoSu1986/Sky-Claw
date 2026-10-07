"""Seguimiento de la etapa 9: el último estado CONOCIDO, lo que responde ``/lod_status``.

El notificador avisa (PUSH) cuando pasa algo; esto responde cuando el operador PREGUNTA (PULL). Nace de
la misma fuente —los eventos ``pipeline.dyndolod.*`` del bus— y de los mismos formateadores
(``dyndolod_mensajes``), así que lo que el operador consulta y lo que recibió no pueden divergir.

Propiedades que estos tests fijan:

* **Distingue cuatro estados con honestidad**: una instancia que NO ejecuta la etapa 9 (sin supervisor),
  una que todavía no tuvo corridas, una en curso y una terminada. Decir «sin corridas» en una instancia
  que no puede tenerlas sugeriría que mañana podría haberlas.
* **Es de solo lectura y nunca lanza**: ``on_event`` actualiza memoria y listo (un evento malformado no
  puede ensuciar la DLQ del bus); ``resumen_html`` sólo lee.
* **Un aviso rezagado no resucita una corrida terminada**: el bus no garantiza orden entre eventos.
* **Todo texto dinámico que llega al resumen, llega escapado**, anclado enumerando por introspección los
  campos de texto de los payloads (ver «La regla que más se viola» en ``AGENTS.md``).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from sky_claw.app.core.event_bus import CoreEventBus, Event
from sky_claw.app.core.event_payloads import (
    DynDOLODPipelineCompletedPayload,
    DynDOLODPipelineProgressPayload,
    DynDOLODPipelineStartedPayload,
)
from sky_claw.app.orchestrator.dyndolod_seguimiento import SeguimientoDeEtapa9


class _Reloj:
    """Reloj manual: el test decide cuánto tiempo pasó."""

    def __init__(self) -> None:
        self.ahora = 1_000.0

    def __call__(self) -> float:
        return self.ahora

    def avanzar(self, segundos: float) -> None:
        self.ahora += segundos


def _inicio(preset: str = "Medium", run_texgen: bool = True) -> Event:
    payload = DynDOLODPipelineStartedPayload(preset=preset, run_texgen=run_texgen).to_log_dict()
    return Event(topic="pipeline.dyndolod.started", payload=payload)


def _actividad(**cambios: Any) -> Event:
    base: dict[str, Any] = {
        "kind": "progress",
        "log": "DynDOLOD_SSE_log.txt",
        "elapsed_seconds": 600.0,
        "idle_seconds": 0.0,
        "log_size_bytes": 4_300_000,
        "last_line": "[04:12] Generating LOD for Tamriel",
    }
    base.update(cambios)
    return Event(topic="pipeline.dyndolod.progress", payload=DynDOLODPipelineProgressPayload(**base).to_log_dict())


def _fin(**cambios: Any) -> Event:
    base: dict[str, Any] = {
        "preset": "Medium",
        "run_texgen": True,
        "success": True,
        "texgen_success": True,
        "dyndolod_success": True,
        "errors": (),
        "duration_seconds": 272.5,
        "rolled_back": False,
    }
    base.update(cambios)
    return Event(topic="pipeline.dyndolod.completed", payload=DynDOLODPipelineCompletedPayload(**base).to_log_dict())


def _seguimiento(reloj: _Reloj, *, conectado: bool = True) -> SeguimientoDeEtapa9:
    seguimiento = SeguimientoDeEtapa9(reloj=reloj)
    if conectado:
        seguimiento.suscribir(MagicMock())
    return seguimiento


class TestEstados:
    def test_una_instancia_sin_supervisor_lo_declara(self) -> None:
        seguimiento = _seguimiento(_Reloj(), conectado=False)

        resumen = seguimiento.resumen_html()

        assert "no ejecuta la etapa 9" in resumen
        assert "sin corridas" not in resumen, "no se sugiere que podría haberlas: esta instancia no las tiene"

    async def test_si_nunca_se_conecto_un_evento_no_cambia_la_respuesta(self) -> None:
        """Sin bus suscripto no hay evento legítimo: ni uno entregado a mano convierte a la instancia en ejecutora."""
        seguimiento = _seguimiento(_Reloj(), conectado=False)

        await seguimiento.on_event(_inicio())

        assert "no ejecuta la etapa 9" in seguimiento.resumen_html()

    def test_conectada_y_sin_corridas_lo_declara(self) -> None:
        resumen = _seguimiento(_Reloj()).resumen_html()

        assert "sin corridas registradas" in resumen
        assert "desde el arranque" in resumen, "el estado vive en memoria: no habla de corridas de otras sesiones"

    async def test_una_corrida_en_curso_muestra_preset_texgen_y_antiguedad(self) -> None:
        reloj = _Reloj()
        seguimiento = _seguimiento(reloj)
        await seguimiento.on_event(_inicio(preset="Medium", run_texgen=True))
        reloj.avanzar(300)

        resumen = seguimiento.resumen_html()

        assert "en curso" in resumen
        assert "<code>Medium</code>" in resumen
        assert "TexGen: sí" in resumen
        assert "desde hace 5m 0s" in resumen
        assert "Todavía no hubo avisos de actividad" in resumen

    async def test_una_corrida_sin_texgen_lo_dice(self) -> None:
        seguimiento = _seguimiento(_Reloj())
        await seguimiento.on_event(_inicio(run_texgen=False))

        assert "TexGen: no" in seguimiento.resumen_html()

    async def test_muestra_el_ultimo_aviso_de_actividad_y_cuanto_hace_que_llego(self) -> None:
        reloj = _Reloj()
        seguimiento = _seguimiento(reloj)
        await seguimiento.on_event(_inicio())
        reloj.avanzar(600)
        await seguimiento.on_event(
            _actividad(kind="stalled", idle_seconds=900.0, last_line="Exit TexGen, zip and exit?")
        )
        reloj.avanzar(120)

        resumen = seguimiento.resumen_html()

        assert "Último aviso de actividad, hace 2m 0s" in resumen
        assert "sin actividad" in resumen
        assert "<pre>Exit TexGen, zip and exit?</pre>" in resumen
        assert "Todavía no hubo avisos" not in resumen

    async def test_solo_cuenta_el_aviso_de_actividad_mas_reciente(self) -> None:
        seguimiento = _seguimiento(_Reloj())
        await seguimiento.on_event(_inicio())
        await seguimiento.on_event(_actividad(last_line="primera línea"))
        await seguimiento.on_event(_actividad(last_line="segunda línea"))

        resumen = seguimiento.resumen_html()

        assert "segunda línea" in resumen
        assert "primera línea" not in resumen

    @pytest.mark.parametrize(
        ("cambios", "encabezado"),
        [
            ({}, "✅"),
            ({"success": False, "errors": ("boom",), "dyndolod_success": False}, "❌"),
            ({"success": False, "cancelled": True, "errors": ("cancelado",)}, "⏹"),
        ],
        ids=["exito", "fallo", "cancelacion"],
    )
    async def test_una_corrida_terminada_muestra_el_resultado_y_cuanto_hace(
        self, cambios: dict[str, Any], encabezado: str
    ) -> None:
        reloj = _Reloj()
        seguimiento = _seguimiento(reloj)
        await seguimiento.on_event(_inicio())
        await seguimiento.on_event(_fin(**cambios))
        reloj.avanzar(60)

        resumen = seguimiento.resumen_html()

        assert resumen.startswith(encabezado)
        assert "4m 32s" in resumen, "la duración que informó la corrida"
        assert "Terminó hace 1m 0s" in resumen
        assert "en curso" not in resumen

    async def test_un_fin_sin_inicio_se_muestra_igual(self) -> None:
        """Si el proceso se suscribió a mitad de una corrida, el fin llega sin su inicio: el dato sigue siendo cierto."""
        seguimiento = _seguimiento(_Reloj())
        await seguimiento.on_event(_fin())

        assert seguimiento.resumen_html().startswith("✅")


class TestOrdenYReinicio:
    async def test_un_aviso_de_actividad_tras_el_fin_no_resucita_la_corrida(self) -> None:
        """El bus no garantiza orden: un aviso rezagado llega DESPUÉS del `completed` de su propia corrida."""
        seguimiento = _seguimiento(_Reloj())
        await seguimiento.on_event(_inicio())
        await seguimiento.on_event(_fin())

        await seguimiento.on_event(_actividad(last_line="rezagado"))

        resumen = seguimiento.resumen_html()
        assert resumen.startswith("✅")
        assert "rezagado" not in resumen

    async def test_un_aviso_de_actividad_sin_corrida_se_ignora(self) -> None:
        seguimiento = _seguimiento(_Reloj())

        await seguimiento.on_event(_actividad(last_line="huérfano"))

        resumen = seguimiento.resumen_html()
        assert "sin corridas registradas" in resumen
        assert "huérfano" not in resumen

    async def test_un_nuevo_inicio_descarta_el_estado_de_la_corrida_anterior(self) -> None:
        reloj = _Reloj()
        seguimiento = _seguimiento(reloj)
        await seguimiento.on_event(_inicio(preset="Low"))
        await seguimiento.on_event(_actividad(last_line="de la corrida vieja"))
        await seguimiento.on_event(_fin(success=False, errors=("falló la vieja",)))
        reloj.avanzar(500)

        await seguimiento.on_event(_inicio(preset="High"))

        resumen = seguimiento.resumen_html()
        assert "en curso" in resumen and "<code>High</code>" in resumen
        assert "de la corrida vieja" not in resumen
        assert "falló la vieja" not in resumen
        assert "desde hace 0s" in resumen, "la antigüedad se cuenta desde el NUEVO inicio"


class TestRobustez:
    @pytest.mark.parametrize(
        "evento",
        [
            Event(topic="pipeline.dyndolod.started", payload={}),
            Event(topic="pipeline.dyndolod.started", payload={"preset": 123}),
            Event(topic="pipeline.dyndolod.progress", payload={"kind": "otro"}),
            Event(topic="pipeline.dyndolod.completed", payload={"success": "no es un bool"}),
            Event(topic="pipeline.dyndolod.desconocido", payload={"x": 1}),
            Event(topic="otro.topic", payload={}),
        ],
        ids=[
            "inicio_vacio",
            "inicio_invalido",
            "actividad_invalida",
            "fin_invalido",
            "topic_desconocido",
            "otro_topic",
        ],
    )
    async def test_un_evento_malformado_o_ajeno_se_ignora_sin_lanzar(self, evento: Event) -> None:
        seguimiento = _seguimiento(_Reloj())
        antes = seguimiento.resumen_html()

        await seguimiento.on_event(evento)  # no debe lanzar: el bus lo mandaría a la DLQ

        assert seguimiento.resumen_html() == antes

    async def test_un_payload_serializado_con_listas_tambien_se_entiende(self) -> None:
        """Una DLQ/JSON convierte las tuplas en listas: el seguimiento no depende de eso."""
        seguimiento = _seguimiento(_Reloj())
        evento = _fin(success=False, errors=("boom",))
        payload = dict(evento.payload)
        payload["errors"] = list(payload["errors"])
        payload["log_paths"] = list(payload["log_paths"])

        await seguimiento.on_event(Event(topic=evento.topic, payload=payload))

        assert seguimiento.resumen_html().startswith("❌")


class TestSuscripcion:
    def test_suscribir_toma_el_patron_del_pipeline_y_deja_a_la_instancia_conectada(self) -> None:
        bus = MagicMock()
        seguimiento = SeguimientoDeEtapa9(reloj=_Reloj())

        seguimiento.suscribir(bus)

        bus.subscribe.assert_called_once_with("pipeline.dyndolod.*", seguimiento.on_event)
        assert "no ejecuta la etapa 9" not in seguimiento.resumen_html()

    async def test_sigue_las_tres_etapas_de_una_corrida_por_un_bus_real(self) -> None:
        seguimiento = SeguimientoDeEtapa9()
        bus = CoreEventBus()
        seguimiento.suscribir(bus)
        await bus.start()

        async def _esperar(fragmento: str) -> None:
            async def _girar() -> None:
                while fragmento not in seguimiento.resumen_html():
                    await asyncio.sleep(0)

            await asyncio.wait_for(_girar(), timeout=5)

        try:
            await bus.publish(_inicio(preset="Ultra"))
            await _esperar("en curso")
            await bus.publish(_actividad(kind="stalled", idle_seconds=900.0, last_line="esperando la ventana"))
            await _esperar("esperando la ventana")
            await bus.publish(_fin(success=False, errors=("la herramienta falló",)))
            await _esperar("la herramienta falló")
        finally:
            await bus.stop()

        assert seguimiento.resumen_html().startswith("❌")


# ---------------------------------------------------------------------------
# Peor caso de longitud
# ---------------------------------------------------------------------------


class TestLongitud:
    async def test_el_peor_caso_de_una_corrida_en_curso_entra_en_un_mensaje(self) -> None:
        seguimiento = _seguimiento(_Reloj())
        await seguimiento.on_event(_inicio(preset="&" * 5000))
        await seguimiento.on_event(
            _actividad(kind="stalled", idle_seconds=900.0, log="&" * 5000 + "_log.txt", last_line="&" * 5000)
        )

        resumen = seguimiento.resumen_html()

        assert len(resumen) < 4096, "el sender partiría el mensaje en medio de una etiqueta HTML"
        assert resumen.count("<pre>") == resumen.count("</pre>")

    async def test_el_peor_caso_de_una_corrida_terminada_entra_en_un_mensaje(self) -> None:
        seguimiento = _seguimiento(_Reloj())
        await seguimiento.on_event(_inicio())
        await seguimiento.on_event(_fin(success=False, errors=tuple("&" * 5000 for _ in range(6))))

        resumen = seguimiento.resumen_html()

        assert len(resumen) < 4096
        assert resumen.count("<pre>") == resumen.count("</pre>")


# ---------------------------------------------------------------------------
# Ancla: todo texto dinámico del payload que llega al resumen, llega escapado
# ---------------------------------------------------------------------------

_HOSTIL = "<x&y>"
_HOSTIL_ESCAPADO = "&lt;x&amp;y&gt;"


def _campos_de_texto(modelo: type[BaseModel]) -> list[str]:
    """Campos ``str`` y ``tuple[str, ...]`` del payload, por INTROSPECCIÓN (no por una lista escrita a mano)."""
    return [nombre for nombre, campo in modelo.model_fields.items() if campo.annotation in (str, tuple[str, ...])]


_MODELOS = (DynDOLODPipelineStartedPayload, DynDOLODPipelineProgressPayload, DynDOLODPipelineCompletedPayload)
_CASOS_DE_ESCAPE = [(modelo, campo) for modelo in _MODELOS for campo in _campos_de_texto(modelo)]


async def _resumen_con_el_campo_hostil(modelo: type[BaseModel], campo: str) -> str:
    """Arma el escenario que HACE visible ese payload en el resumen y devuelve el texto resultante."""

    def _valor(base: dict[str, Any]) -> dict[str, Any]:
        es_texto_plano = modelo.model_fields[campo].annotation is str
        return {**base, campo: _HOSTIL if es_texto_plano else (_HOSTIL,)}

    seguimiento = _seguimiento(_Reloj())
    if modelo is DynDOLODPipelineStartedPayload:
        payload = DynDOLODPipelineStartedPayload(**_valor({"preset": "Medium", "run_texgen": True}))
        await seguimiento.on_event(Event(topic="pipeline.dyndolod.started", payload=payload.to_log_dict()))
    elif modelo is DynDOLODPipelineProgressPayload:
        await seguimiento.on_event(_inicio())
        payload_p = DynDOLODPipelineProgressPayload(
            **_valor(
                {
                    "kind": "stalled",
                    "log": "DynDOLOD_SSE_log.txt",
                    "elapsed_seconds": 10.0,
                    "idle_seconds": 900.0,
                    "log_size_bytes": 100,
                    "last_line": "x",
                }
            )
        )
        await seguimiento.on_event(Event(topic="pipeline.dyndolod.progress", payload=payload_p.to_log_dict()))
    else:
        await seguimiento.on_event(_inicio())
        payload_c = DynDOLODPipelineCompletedPayload(
            **_valor(
                {
                    "preset": "Medium",
                    "run_texgen": True,
                    "success": False,
                    "texgen_success": True,
                    "dyndolod_success": False,
                    "errors": ("boom",),
                    "duration_seconds": 1.0,
                    "rolled_back": True,
                }
            )
        )
        await seguimiento.on_event(Event(topic="pipeline.dyndolod.completed", payload=payload_c.to_log_dict()))
    return seguimiento.resumen_html()


class TestEscapeDeTextoDinamico:
    """La propiedad es del MECANISMO: un campo de texto nuevo que el resumen muestre sin escapar rompe esto."""

    @pytest.mark.parametrize(
        ("modelo", "campo"),
        _CASOS_DE_ESCAPE,
        ids=[f"{modelo.__name__}.{campo}" for modelo, campo in _CASOS_DE_ESCAPE],
    )
    async def test_ningun_campo_de_texto_llega_crudo_al_resumen(self, modelo: type[BaseModel], campo: str) -> None:
        resumen = await _resumen_con_el_campo_hostil(modelo, campo)

        assert _HOSTIL not in resumen, f"{modelo.__name__}.{campo} llega sin escapar: Telegram rechazaría la respuesta"

    async def test_los_campos_de_texto_que_el_resumen_muestra_estan_congelados(self) -> None:
        """Enumera por introspección cuáles campos se muestran y los congela por igualdad literal.

        Un campo de texto nuevo que el resumen empiece a mostrar entra a este conjunto y rompe la igualdad:
        obliga a decidir (y a que el test de arriba verifique) que se escapa.
        """
        mostrados = {
            (modelo.__name__, campo)
            for modelo, campo in _CASOS_DE_ESCAPE
            if _HOSTIL_ESCAPADO in await _resumen_con_el_campo_hostil(modelo, campo)
        }

        assert mostrados == {
            ("DynDOLODPipelineStartedPayload", "preset"),
            ("DynDOLODPipelineProgressPayload", "log"),
            ("DynDOLODPipelineProgressPayload", "last_line"),
            ("DynDOLODPipelineCompletedPayload", "errors"),
        }
