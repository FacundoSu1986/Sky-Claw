"""Notificador de operador de DynDOLOD: eventos del bus -> mensajes al operador.

Cierra el hueco que la auditoría de la etapa 9 midió en el lado remoto: el
servicio publicaba ``pipeline.dyndolod.started``/``completed`` pero NADIE los
consumía, así que un operador que seguía la corrida desde Telegram no recibía ni
el aviso de fallo ni el log que lo explica.

Propiedades que estos tests fijan:

* El callback del bus **no espera al canal** (un ``sendMessage`` con backoff de 429
  no puede retener al bus ni al pipeline) y **nunca lanza** (el bus lo mandaría a
  la DLQ y un evento malformado no debe ensuciarla).
* Los envíos salen **en orden** desde UN worker: el bus despacha una tarea por
  callback y no garantiza orden.
* Todo texto dinámico va **escapado**: las líneas de log de DynDOLOD son
  ``<Error: ...>`` y el modo HTML de Telegram las rechazaría — justo el aviso de
  fallo se perdería.
* El adjunto es la **cola** del log, cortada en un límite de línea, y sólo de
  archivos ``*_log.txt`` regulares (no enlaces).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import pathlib
from typing import Any
from unittest.mock import MagicMock

import pytest

from sky_claw.app.core.event_bus import Event
from sky_claw.app.core.event_payloads import (
    DynDOLODPipelineCompletedPayload,
    DynDOLODPipelineProgressPayload,
    DynDOLODPipelineStartedPayload,
)
from sky_claw.app.orchestrator import dyndolod_operator_notifier as mod
from sky_claw.app.orchestrator.dyndolod_operator_notifier import (
    DynDOLODOperatorNotifier,
    instalar_notificador_de_dyndolod,
)


class CanalFalso:
    """Canal de operador en memoria, con puntos de control para tiempo y fallos."""

    def __init__(self) -> None:
        self.avisos: list[str] = []
        self.adjuntos: list[tuple[str, bytes, str]] = []
        self.bloqueo: asyncio.Event | None = None
        self.fallar_el_primer_aviso_con: BaseException | None = None
        self.fallar_el_primer_adjunto_con: BaseException | None = None
        self.entrada = asyncio.Event()

    async def avisar(self, texto_html: str) -> None:
        self.entrada.set()
        if self.bloqueo is not None:
            await self.bloqueo.wait()
        if self.fallar_el_primer_aviso_con is not None:
            falla, self.fallar_el_primer_aviso_con = self.fallar_el_primer_aviso_con, None
            raise falla
        self.avisos.append(texto_html)

    async def adjuntar(self, *, nombre: str, contenido: bytes, descripcion: str) -> None:
        if self.fallar_el_primer_adjunto_con is not None:
            falla, self.fallar_el_primer_adjunto_con = self.fallar_el_primer_adjunto_con, None
            raise falla
        self.adjuntos.append((nombre, contenido, descripcion))


def _evento_started(**cambios: Any) -> Event:
    payload = DynDOLODPipelineStartedPayload(preset="Medium", run_texgen=True, **cambios)
    return Event(topic="pipeline.dyndolod.started", payload=payload.to_log_dict())


def _evento_completed(**cambios: Any) -> Event:
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


def _evento_progreso(**cambios: Any) -> Event:
    base: dict[str, Any] = {
        "kind": "progress",
        "log": "DynDOLOD_SSE_log.txt",
        "elapsed_seconds": 1384.0,
        "idle_seconds": 0.0,
        "log_size_bytes": 4_300_000,
        "last_line": "[04:12] Generating LOD for Tamriel",
    }
    base.update(cambios)
    return Event(topic="pipeline.dyndolod.progress", payload=DynDOLODPipelineProgressPayload(**base).to_log_dict())


async def _esperar(condicion: Any, *, descripcion: str) -> None:
    """Gira hasta que ``condicion()`` sea verdadera. El tiempo es red de seguridad, no la prueba."""

    async def _girar() -> None:
        while not condicion():
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(_girar(), timeout=5)
    except TimeoutError:  # pragma: no cover - sólo si el comportamiento falla
        pytest.fail(f"nunca se cumplió: {descripcion}")


async def _barrera(notificador: DynDOLODOperatorNotifier, canal: CanalFalso) -> None:
    """Espera a que el worker termine TODO lo encolado antes de esta llamada.

    Un único worker entrega en orden: cuando sale el aviso centinela, el aviso y los
    adjuntos de cada notificación anterior ya salieron (o ya fallaron). Es lo que
    distingue "no se adjuntó nada" de "todavía no se adjuntó": un ``sleep(0)`` no
    sirve para afirmar una negativa, porque la lectura del log pasa por
    ``to_thread`` y un worker defectuoso adjuntaría DESPUÉS de la aserción (un test
    verde por falta de espera). El centinela se retira de ``canal.avisos`` para no
    contaminar las aserciones del test.
    """
    payload = DynDOLODPipelineStartedPayload(preset="__barrera__", run_texgen=False).to_log_dict()
    await notificador.on_event(Event(topic="pipeline.dyndolod.started", payload=payload))
    await _esperar(lambda: any("__barrera__" in aviso for aviso in canal.avisos), descripcion="aviso centinela")
    canal.avisos[:] = [aviso for aviso in canal.avisos if "__barrera__" not in aviso]


@contextlib.asynccontextmanager
async def _notificador(canal: CanalFalso, **kwargs: Any):
    notificador = DynDOLODOperatorNotifier(canal, **kwargs)
    worker = asyncio.create_task(notificador.run())
    try:
        yield notificador
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker


# ---------------------------------------------------------------------------
# Mensajes
# ---------------------------------------------------------------------------


class TestMensajes:
    async def test_el_inicio_nombra_preset_texgen_y_como_cerrar_cada_herramienta(self) -> None:
        canal = CanalFalso()
        async with _notificador(canal) as notificador:
            await notificador.on_event(_evento_started())
            await _esperar(lambda: canal.avisos, descripcion="aviso de inicio")

        [aviso] = canal.avisos
        assert "<b>DynDOLOD" in aviso
        assert "<code>Medium</code>" in aviso
        assert "TexGen: sí" in aviso
        # Evidencia de rig (docs/validation/2026-09-13_pr580_real_rig/: texgen-log.txt y dyndolod-log.txt
        # traen el texto del diálogo final; final-report.md, «Terminación elegida»): las variantes «Zip»
        # borran la carpeta de salida y «Exit DynDOLOD» no guarda.
        assert "«Exit TexGen»" in aviso
        assert "«Save and Exit»" in aviso
        assert "Zip" in aviso

    async def test_una_corrida_sin_texgen_no_pide_cerrar_texgen(self) -> None:
        canal = CanalFalso()
        payload = DynDOLODPipelineStartedPayload(preset="High", run_texgen=False).to_log_dict()
        async with _notificador(canal) as notificador:
            await notificador.on_event(Event(topic="pipeline.dyndolod.started", payload=payload))
            await _esperar(lambda: canal.avisos, descripcion="aviso de inicio")

        assert "TexGen: no" in canal.avisos[0]
        assert "«Exit TexGen»" not in canal.avisos[0]

    async def test_el_exito_informa_duracion_y_no_adjunta_nada(self, tmp_path: pathlib.Path) -> None:
        # Un log existente y adjuntable: sin él la aserción sería vacía (un notificador que
        # adjuntara también en éxito no tendría qué adjuntar).
        log = tmp_path / "DynDOLOD_SSE_log.txt"
        log.write_bytes(b"[00:47] DynDOLOD completed successfully\n")
        canal = CanalFalso()
        async with _notificador(canal) as notificador:
            await notificador.on_event(_evento_completed(log_paths=(str(log),)))
            await _barrera(notificador, canal)

        [aviso] = canal.avisos
        assert aviso.startswith("✅")
        assert "4m 32s" in aviso
        assert "TexGen: ✓ · DynDOLOD: ✓" in aviso
        assert canal.adjuntos == [], "un éxito no necesita el log: sólo un fallo lo adjunta"

    async def test_un_exito_sin_texgen_lo_declara(self) -> None:
        canal = CanalFalso()
        async with _notificador(canal) as notificador:
            await notificador.on_event(_evento_completed(run_texgen=False, texgen_success=False))
            await _barrera(notificador, canal)

        assert "TexGen: no se ejecutó · DynDOLOD: ✓" in canal.avisos[0]

    async def test_una_cancelacion_posterior_al_commit_es_un_exito_con_nota(self) -> None:
        """El servicio publica ``success=True, cancelled=True`` si lo cancelado fue el post-proceso."""
        canal = CanalFalso()
        async with _notificador(canal) as notificador:
            await notificador.on_event(_evento_completed(success=True, cancelled=True))
            await _barrera(notificador, canal)

        [aviso] = canal.avisos
        assert aviso.startswith("✅")
        assert "post-proceso" in aviso, "el operador debe saber que lo cancelado fue el post-proceso"

    async def test_una_cancelacion_no_adjunta_logs(self, tmp_path: pathlib.Path) -> None:
        """Lo cancelado no es un fallo de la herramienta: su log parcial no explica nada."""
        log = tmp_path / "DynDOLOD_SSE_log.txt"
        log.write_bytes(b"[00:03] Loading plugins...\n")
        canal = CanalFalso()
        async with _notificador(canal) as notificador:
            await notificador.on_event(
                _evento_completed(success=False, cancelled=True, errors=("cancelado",), log_paths=(str(log),))
            )
            await _barrera(notificador, canal)

        assert canal.avisos[0].startswith("⏹️")
        assert canal.adjuntos == []

    async def test_un_fallo_sin_lineas_de_error_lo_declara(self) -> None:
        canal = CanalFalso()
        async with _notificador(canal) as notificador:
            await notificador.on_event(_evento_completed(success=False, errors=()))
            await _barrera(notificador, canal)

        assert "Sin detalle" in canal.avisos[0]

    async def test_se_muestran_pocos_errores_y_se_cuenta_el_resto(self) -> None:
        canal = CanalFalso()
        errores = tuple(f"fallo número {i}" for i in range(5))
        async with _notificador(canal) as notificador:
            await notificador.on_event(_evento_completed(success=False, errors=errores))
            await _barrera(notificador, canal)

        [aviso] = canal.avisos
        assert "fallo número 0" in aviso and "fallo número 1" in aviso
        assert "fallo número 2" not in aviso, "el mensaje de Telegram no es el log: se muestran pocos errores"
        assert "(+3 más en el log)" in aviso

    async def test_el_fallo_escapa_el_html_de_las_lineas_de_log(self) -> None:
        canal = CanalFalso()
        linea = "<Error: Unresolved FormID [0207B5B9] Error in Dawnguard.esm [ACTI:02014760]> & más"
        async with _notificador(canal) as notificador:
            await notificador.on_event(_evento_completed(success=False, errors=(linea,), dyndolod_success=False))
            await _esperar(lambda: canal.avisos, descripcion="aviso de fallo")

        [aviso] = canal.avisos
        assert aviso.startswith("❌")
        assert "&lt;Error: Unresolved FormID" in aviso
        assert "<Error:" not in aviso, "una línea de log sin escapar rompe el parseo HTML de Telegram"
        assert "&amp; más" in aviso

    async def test_el_peor_caso_de_escape_sigue_dentro_del_limite_de_telegram(self) -> None:
        canal = CanalFalso()
        errores = tuple("&" * 5000 for _ in range(6))
        async with _notificador(canal) as notificador:
            await notificador.on_event(_evento_completed(success=False, errors=errores))
            await _esperar(lambda: canal.avisos, descripcion="aviso de fallo")

        assert len(canal.avisos[0]) < 4096, "el sender partiría el mensaje en medio de una etiqueta HTML"
        assert canal.avisos[0].count("<pre>") == canal.avisos[0].count("</pre>")

    async def test_la_cancelacion_se_distingue_del_fallo(self) -> None:
        canal = CanalFalso()
        async with _notificador(canal) as notificador:
            await notificador.on_event(
                _evento_completed(success=False, cancelled=True, errors=("Pipeline cancelado antes de completarse.",))
            )
            await _esperar(lambda: canal.avisos, descripcion="aviso de cancelación")

        [aviso] = canal.avisos
        assert aviso.startswith("⏹️")
        assert "cancelada" in aviso
        assert "❌" not in aviso

    @pytest.mark.parametrize(
        ("rolled_back", "frase"),
        [(True, "Rollback: confirmado"), (False, "Rollback: NO confirmado")],
    )
    async def test_el_estado_del_rollback_se_declara_sin_adornarlo(self, rolled_back: bool, frase: str) -> None:
        canal = CanalFalso()
        async with _notificador(canal) as notificador:
            await notificador.on_event(_evento_completed(success=False, errors=("boom",), rolled_back=rolled_back))
            await _esperar(lambda: canal.avisos, descripcion="aviso de fallo")

        assert frase in canal.avisos[0]


# ---------------------------------------------------------------------------
# Avisos de actividad del log (progreso, estancamiento, reanudación)
# ---------------------------------------------------------------------------


class TestAvisosDeActividad:
    async def test_el_progreso_informa_el_log_su_tamano_y_la_ultima_linea(self) -> None:
        canal = CanalFalso()
        async with _notificador(canal) as notificador:
            await notificador.on_event(_evento_progreso())
            await _barrera(notificador, canal)

        [aviso] = canal.avisos
        assert aviso.startswith("⏳")
        assert "23m 4s" in aviso
        assert "<code>DynDOLOD_SSE_log.txt</code>" in aviso
        assert "4.1 MiB" in aviso
        assert "<pre>[04:12] Generating LOD for Tamriel</pre>" in aviso
        assert "Último cambio" not in aviso, "un log que acaba de cambiar no necesita aclarar cuándo cambió"
        assert canal.adjuntos == [], "un aviso de actividad no adjunta logs: sólo un fallo de herramienta lo hace"

    async def test_el_progreso_de_un_log_quieto_dice_hace_cuanto_cambio(self) -> None:
        canal = CanalFalso()
        async with _notificador(canal) as notificador:
            await notificador.on_event(_evento_progreso(idle_seconds=125.0))
            await _barrera(notificador, canal)

        assert "Último cambio del log hace 2m 5s" in canal.avisos[0]

    async def test_el_estancamiento_avisa_que_puede_estar_esperando_una_accion(self) -> None:
        canal = CanalFalso()
        async with _notificador(canal) as notificador:
            await notificador.on_event(
                _evento_progreso(kind="stalled", idle_seconds=900.0, last_line="Exit TexGen, zip and exit?")
            )
            await _barrera(notificador, canal)

        [aviso] = canal.avisos
        assert aviso.startswith("⚠")
        assert "sin actividad" in aviso
        assert "15m 0s" in aviso
        assert "esperando una acción" in aviso, (
            "la etapa es asistida: el operador remoto tiene que ver la causa probable"
        )
        assert "<pre>Exit TexGen, zip and exit?</pre>" in aviso

    async def test_la_reanudacion_cierra_el_episodio_y_dice_cuanto_duro(self) -> None:
        canal = CanalFalso()
        async with _notificador(canal) as notificador:
            await notificador.on_event(_evento_progreso(kind="resumed", idle_seconds=723.0))
            await _barrera(notificador, canal)

        [aviso] = canal.avisos
        assert aviso.startswith("▶")
        assert "reanudada" in aviso
        assert "Volvió a escribir tras 12m 3s sin cambios" in aviso, "la reanudación dice cuánto estuvo callado"
        assert "Último cambio" not in aviso

    async def test_sin_log_de_esta_corrida_no_inventa_un_archivo(self) -> None:
        canal = CanalFalso()
        async with _notificador(canal) as notificador:
            await notificador.on_event(
                _evento_progreso(kind="stalled", log="", log_size_bytes=0, last_line="", idle_seconds=900.0)
            )
            await _barrera(notificador, canal)

        [aviso] = canal.avisos
        assert "Todavía no hay un log de esta corrida" in aviso
        assert "<code>" not in aviso and "<pre>" not in aviso

    async def test_la_ultima_linea_se_escapa_y_el_peor_caso_entra_en_un_mensaje(self) -> None:
        canal = CanalFalso()
        linea = "<Error: Unresolved FormID [0207B5B9]> & " + "&" * 5000
        async with _notificador(canal) as notificador:
            await notificador.on_event(_evento_progreso(kind="stalled", idle_seconds=900.0, last_line=linea))
            await _barrera(notificador, canal)

        [aviso] = canal.avisos
        assert "&lt;Error: Unresolved FormID" in aviso
        assert "<Error:" not in aviso, "una línea de log sin escapar rompe el parseo HTML de Telegram"
        assert len(aviso) < 4096, "el sender partiría el mensaje en medio de una etiqueta HTML"
        assert aviso.count("<pre>") == aviso.count("</pre>")


# ---------------------------------------------------------------------------
# Adjunto del log
# ---------------------------------------------------------------------------


class TestAdjuntoDelLog:
    async def test_un_fallo_adjunta_la_cola_del_log_en_un_limite_de_linea(self, tmp_path: pathlib.Path) -> None:
        log = tmp_path / "DynDOLOD_SSE_log.txt"
        lineas = [f"[00:{i % 60:02d}] linea numero {i:05d}\n".encode() for i in range(20000)]
        log.write_bytes(b"".join(lineas))
        canal = CanalFalso()
        async with _notificador(canal, max_bytes_de_adjunto=4096) as notificador:
            await notificador.on_event(_evento_completed(success=False, errors=("boom",), log_paths=(str(log),)))
            await _esperar(lambda: canal.adjuntos, descripcion="adjunto del log")

        [(nombre, contenido, descripcion)] = canal.adjuntos
        assert nombre == "DynDOLOD_SSE_log.txt"
        assert len(contenido) <= 4096
        assert contenido.endswith(lineas[-1]), "la cola del archivo es lo que explica el fallo"
        assert contenido.startswith(b"[00:"), "la primera línea del adjunto debe estar completa"
        assert "Cola" in descripcion and "DynDOLOD_SSE_log.txt" in descripcion
        assert canal.avisos, "el aviso de texto sale antes que el adjunto"

    async def test_un_log_chico_se_adjunta_entero(self, tmp_path: pathlib.Path) -> None:
        log = tmp_path / "TexGen_SSE_log.txt"
        log.write_bytes(b"[00:47] TexGen completed successfully\n")
        canal = CanalFalso()
        async with _notificador(canal) as notificador:
            await notificador.on_event(_evento_completed(success=False, errors=("boom",), log_paths=(str(log),)))
            await _esperar(lambda: canal.adjuntos, descripcion="adjunto del log")

        assert canal.adjuntos[0][1] == b"[00:47] TexGen completed successfully\n"
        assert "Cola" not in canal.adjuntos[0][2], "no se llama 'cola' a un archivo entero"

    async def test_se_adjunta_a_lo_sumo_la_cantidad_configurada(self, tmp_path: pathlib.Path) -> None:
        rutas = []
        for nombre in ("TexGen_SSE_log.txt", "DynDOLOD_SSE_log.txt", "Otro_SSE_log.txt"):
            ruta = tmp_path / nombre
            ruta.write_bytes(b"x\n")
            rutas.append(str(ruta))
        canal = CanalFalso()
        async with _notificador(canal, max_adjuntos=2) as notificador:
            await notificador.on_event(_evento_completed(success=False, errors=("boom",), log_paths=tuple(rutas)))
            await _barrera(notificador, canal)  # el tercero llegaría ANTES de la barrera si el tope no se respetara

        assert [a[0] for a in canal.adjuntos] == ["TexGen_SSE_log.txt", "DynDOLOD_SSE_log.txt"]

    async def test_con_cero_adjuntos_permitidos_solo_sale_el_aviso(self, tmp_path: pathlib.Path) -> None:
        log = tmp_path / "DynDOLOD_SSE_log.txt"
        log.write_bytes(b"x\n")
        canal = CanalFalso()
        async with _notificador(canal, max_adjuntos=0) as notificador:
            await notificador.on_event(_evento_completed(success=False, errors=("boom",), log_paths=(str(log),)))
            await _barrera(notificador, canal)

        assert canal.adjuntos == []
        assert canal.avisos[0].startswith("❌")

    async def test_no_se_adjunta_lo_que_no_es_un_log_regular_de_herramienta(
        self, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        secreto = tmp_path / "secreto.txt"
        secreto.write_bytes(b"no debe salir\n")
        sin_forma = tmp_path / "credenciales.ini"
        sin_forma.write_bytes(b"token=abc\n")
        ausente = tmp_path / "TexGen_SSE_log.txt"
        directorio = tmp_path / "Otro_SSE_log.txt"
        directorio.mkdir()
        vacio = tmp_path / "Vacio_SSE_log.txt"
        vacio.write_bytes(b"")
        canal = CanalFalso()
        rutas = tuple(str(r) for r in (secreto, sin_forma, ausente, directorio, vacio))
        with caplog.at_level(logging.WARNING, logger=mod.__name__):
            async with _notificador(canal) as notificador:
                await notificador.on_event(_evento_completed(success=False, errors=("boom",), log_paths=rutas))
                await _barrera(notificador, canal)

        assert canal.adjuntos == [], "sólo archivos *_log.txt regulares y no vacíos pueden salir hacia el chat"
        assert canal.avisos, "el aviso de texto se entrega igual"
        # Rechazar por el filtro es un descarte esperado, no un error: si el filtro dependiera de que `open()` falle,
        # cada ruta rechazada ensuciaría el log de warnings del operador.
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    async def test_un_enlace_simbolico_no_se_adjunta_aunque_se_llame_como_un_log(self, tmp_path: pathlib.Path) -> None:
        secreto = tmp_path / "secreto.txt"
        secreto.write_bytes(b"no debe salir\n")
        enlace = tmp_path / "DynDOLOD_SSE_log.txt"
        try:
            enlace.symlink_to(secreto)
        except (OSError, NotImplementedError):
            pytest.skip("la plataforma no permite crear enlaces simbólicos (sin privilegio): caso no ejercitable acá")
        canal = CanalFalso()
        async with _notificador(canal) as notificador:
            await notificador.on_event(_evento_completed(success=False, errors=("boom",), log_paths=(str(enlace),)))
            await _barrera(notificador, canal)

        assert canal.adjuntos == [], "la ruta viaja en un evento y el destino es un chat externo: un enlace la desvía"

    async def test_un_log_ilegible_no_tumba_el_aviso(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        log = tmp_path / "DynDOLOD_SSE_log.txt"
        log.write_bytes(b"x\n")

        def _explota(_ruta: pathlib.Path, _max: int) -> bytes:
            raise PermissionError("sharing violation")

        monkeypatch.setattr(mod, "_leer_cola_de_log", _explota)
        canal = CanalFalso()
        with caplog.at_level(logging.WARNING, logger=mod.__name__):
            async with _notificador(canal) as notificador:
                await notificador.on_event(_evento_completed(success=False, errors=("boom",), log_paths=(str(log),)))
                await _barrera(notificador, canal)  # si el worker hubiera muerto, la barrera no saldría

        assert canal.adjuntos == []
        assert canal.avisos[0].startswith("❌"), "el aviso de texto ya había salido"
        assert any("no se pudo adjuntar" in r.getMessage() for r in caplog.records)

    async def test_un_adjunto_que_falla_no_impide_el_siguiente(
        self, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        rutas = []
        for nombre in ("TexGen_SSE_log.txt", "DynDOLOD_SSE_log.txt"):
            ruta = tmp_path / nombre
            ruta.write_bytes(b"x\n")
            rutas.append(str(ruta))
        canal = CanalFalso()
        canal.fallar_el_primer_adjunto_con = RuntimeError("telegram: 413 Request Entity Too Large")
        with caplog.at_level(logging.WARNING, logger=mod.__name__):
            async with _notificador(canal) as notificador:
                await notificador.on_event(_evento_completed(success=False, errors=("boom",), log_paths=tuple(rutas)))
                await _barrera(notificador, canal)

        assert [a[0] for a in canal.adjuntos] == ["DynDOLOD_SSE_log.txt"], (
            "el log de DynDOLOD no se pierde por el de TexGen"
        )
        assert any("no se pudo adjuntar" in r.getMessage() for r in caplog.records)

    def test_el_limite_de_tamano_del_adjunto_es_inclusivo(self, tmp_path: pathlib.Path) -> None:
        """Un log de EXACTAMENTE ``max_bytes`` entra entero; con un byte más, se trunca en límite de línea."""
        exacto = tmp_path / "Exacto_SSE_log.txt"
        exacto.write_bytes(b"uno\ndos\ntres\n")  # 13 bytes
        assert mod._preparar_adjunto(exacto, 13) == ("Exacto_SSE_log.txt", b"uno\ndos\ntres\n", False)

        de_mas = tmp_path / "DeMas_SSE_log.txt"
        de_mas.write_bytes(b"uno\ndos\ntres\n")
        nombre, contenido, truncado = mod._preparar_adjunto(de_mas, 12) or ("", b"", False)
        assert truncado is True
        assert contenido == b"dos\ntres\n", "la cola arranca en la primera línea completa que cabe"
        assert nombre == "DeMas_SSE_log.txt"


# ---------------------------------------------------------------------------
# Desacople, orden y robustez
# ---------------------------------------------------------------------------


class TestDesacopleYRobustez:
    async def test_el_callback_del_bus_no_espera_al_canal(self) -> None:
        canal = CanalFalso()
        canal.bloqueo = asyncio.Event()  # el canal se queda "enviando" (p. ej. backoff de 429)
        async with _notificador(canal) as notificador:
            await asyncio.wait_for(notificador.on_event(_evento_started()), timeout=1)
            await asyncio.wait_for(notificador.on_event(_evento_completed()), timeout=1)
            await asyncio.wait_for(canal.entrada.wait(), timeout=5)
            canal.bloqueo.set()
            await _esperar(lambda: len(canal.avisos) == 2, descripcion="ambos avisos")

    async def test_los_avisos_salen_en_el_orden_de_los_eventos(self) -> None:
        canal = CanalFalso()
        canal.bloqueo = asyncio.Event()
        async with _notificador(canal) as notificador:
            await notificador.on_event(_evento_started())
            await notificador.on_event(_evento_completed(success=False, errors=("boom",)))
            await notificador.on_event(_evento_completed())
            await asyncio.wait_for(canal.entrada.wait(), timeout=5)
            canal.bloqueo.set()
            await _esperar(lambda: len(canal.avisos) == 3, descripcion="tres avisos")

        assert [a[0] for a in canal.avisos] == ["🛠", "❌", "✅"], canal.avisos

    async def test_un_fallo_del_canal_no_mata_al_worker(self, caplog: pytest.LogCaptureFixture) -> None:
        canal = CanalFalso()
        canal.fallar_el_primer_aviso_con = RuntimeError("telegram caído")
        with caplog.at_level(logging.WARNING, logger=mod.__name__):
            async with _notificador(canal) as notificador:
                await notificador.on_event(_evento_started())
                await notificador.on_event(_evento_completed())
                await _esperar(lambda: canal.avisos, descripcion="el segundo aviso, tras el fallo del primero")

        assert canal.avisos[0].startswith("✅")
        assert any("no se pudo entregar" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize(
        "evento",
        [
            Event(topic="pipeline.dyndolod.completed", payload={}),
            Event(topic="pipeline.dyndolod.completed", payload={"success": "no es un bool"}),
            Event(topic="pipeline.dyndolod.started", payload={"preset": 123}),
            Event(topic="pipeline.dyndolod.completed", payload={"errors": "no es tupla", "success": False}),
            Event(topic="pipeline.dyndolod.progress", payload={}),
            Event(topic="pipeline.dyndolod.progress", payload={"kind": "otro"}),
            Event(topic="pipeline.dyndolod.desconocido", payload={"x": 1}),
            Event(topic="otro.topic", payload={}),
        ],
        ids=[
            "vacio",
            "tipo_invalido",
            "started_invalido",
            "errors_invalido",
            "progreso_vacio",
            "progreso_kind_invalido",
            "topic_desconocido",
            "otro_topic",
        ],
    )
    async def test_un_evento_malformado_o_ajeno_se_ignora_sin_lanzar(self, evento: Event) -> None:
        canal = CanalFalso()
        async with _notificador(canal) as notificador:
            await notificador.on_event(evento)  # no debe lanzar: el bus lo mandaría a la DLQ
            await _barrera(notificador, canal)  # y NADA del evento ignorado sale antes del centinela

        assert canal.avisos == []
        assert canal.adjuntos == []

    @pytest.mark.parametrize(
        "kwargs",
        [{"max_bytes_de_adjunto": 0}, {"max_adjuntos": -1}, {"capacidad_de_cola": 0}],
        ids=["bytes_cero", "adjuntos_negativos", "cola_cero"],
    )
    def test_los_limites_invalidos_se_rechazan_al_construir(self, kwargs: dict[str, int]) -> None:
        with pytest.raises(ValueError, match="positivos"):
            DynDOLODOperatorNotifier(CanalFalso(), **kwargs)

    async def test_el_payload_serializado_con_listas_tambien_se_entiende(self) -> None:
        """Una DLQ/JSON convierte las tuplas en listas: el notificador no depende de eso."""
        canal = CanalFalso()
        evento = _evento_completed(success=False, errors=("boom",))
        payload = dict(evento.payload)
        payload["errors"] = list(payload["errors"])
        payload["log_paths"] = list(payload["log_paths"])
        async with _notificador(canal) as notificador:
            await notificador.on_event(Event(topic=evento.topic, payload=payload))
            await _esperar(lambda: canal.avisos, descripcion="aviso de fallo")

        assert canal.avisos[0].startswith("❌")

    async def test_si_la_cola_se_llena_se_descarta_lo_mas_viejo_y_se_cuenta(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        canal = CanalFalso()
        canal.bloqueo = asyncio.Event()
        with caplog.at_level(logging.WARNING, logger=mod.__name__):
            async with _notificador(canal, capacidad_de_cola=2) as notificador:
                await notificador.on_event(_evento_started())
                await asyncio.wait_for(canal.entrada.wait(), timeout=5)  # el worker tomó el primero y quedó bloqueado
                for i in range(5):
                    await notificador.on_event(_evento_completed(success=False, errors=(f"fallo {i}",)))
                assert notificador.descartados == 3
                canal.bloqueo.set()
                await _esperar(lambda: len(canal.avisos) == 3, descripcion="el primero + los 2 más nuevos")

        assert "fallo 3" in canal.avisos[1]
        assert "fallo 4" in canal.avisos[2]
        assert any("descart" in r.getMessage() for r in caplog.records)

    async def test_el_worker_inactivo_se_cancela_limpiamente(self) -> None:
        notificador = DynDOLODOperatorNotifier(CanalFalso())
        worker = asyncio.create_task(notificador.run())
        await asyncio.sleep(0)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    async def test_instalar_suscribe_el_patron_del_pipeline_y_devuelve_el_notificador(self) -> None:
        bus = MagicMock()
        canal = CanalFalso()

        notificador = instalar_notificador_de_dyndolod(event_bus=bus, canal=canal)

        bus.subscribe.assert_called_once_with("pipeline.dyndolod.*", notificador.on_event)
        assert isinstance(notificador, DynDOLODOperatorNotifier)

    def test_los_topics_que_el_notificador_atiende_son_los_que_publica_el_servicio(self) -> None:
        """Ancla de acoplamiento: si el servicio renombra un topic, el notificador no queda sordo."""
        import ast

        from sky_claw.local.tools import dyndolod_service

        fuente = pathlib.Path(dyndolod_service.__file__).read_text(encoding="utf-8")
        publicados = {
            n.value
            for n in ast.walk(ast.parse(fuente))
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("pipeline.dyndolod.")
        }
        assert {mod.TOPIC_INICIO, mod.TOPIC_FIN, mod.TOPIC_PROGRESO} <= publicados
        assert mod.PATRON_DE_SUSCRIPCION == "pipeline.dyndolod.*"
