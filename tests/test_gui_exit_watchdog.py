"""Watchdog de cierre: el .exe debe poder morir cuando el usuario cierra la ventana.

Cierre del post-mortem del WinError 10048. Las PRs #361, #362, #363, #366 y
#367 hicieron que el proceso PUEDA morir limpio (sin deadlock, con checkpoint
del WAL, con cleanup que no se saltea, cerrando los ``/ws/ui``). Faltaba el
eslabón que las activa: **nada podía decirle al proceso que muera.**

- ``sky_claw.spec`` compila con ``console=False`` → subsystem ``WINDOWS_GUI``,
  sin consola adjunta: no hay ``CTRL_CLOSE_EVENT`` al que engancharse
  (``SetConsoleCtrlHandler`` no sirve acá, no hay consola).
- ``gui_mode`` llama ``run_nicegui(..., show=True)`` y ``ui.run`` NO usa
  ``native=True``: abre el navegador DEL SISTEMA. Cerrar esa pestaña no le
  manda absolutamente nada al proceso Python.

Resultado: ``app.on_shutdown`` nunca corría en el .exe, el proceso quedaba
vivo reteniendo 8080/8765, y el arranque siguiente moría con ``WinError 10048``.

El watchdog cierra el círculo: cuando el último cliente se desconecta y no
vuelve dentro del período de gracia, dispara ``app.shutdown()`` — que es lo
que enciende toda la cadena de teardown ya construida.

El período de gracia existe porque un F5, una reconexión de socket o navegar
entre páginas desconectan al cliente momentáneamente: apagar al primer
disconnect cerraría la app cada vez que el usuario refresca.
"""

from __future__ import annotations

import asyncio

import pytest

from sky_claw.antigravity.gui._bootloader import ExitWatchdog

GRACIA = 0.05


def _hacer_watchdog(clientes: list[int]) -> tuple[ExitWatchdog, list[bool]]:
    """Watchdog con contador y apagado inyectados (sin NiceGUI de por medio).

    ``clientes`` es una lista de un elemento para poder mutar el conteo desde
    el test y simular reconexiones.
    """
    apagados: list[bool] = []
    watchdog = ExitWatchdog(
        grace_seconds=GRACIA,
        contar_clientes=lambda: clientes[0],
        apagar=lambda: apagados.append(True),
    )
    return watchdog, apagados


async def test_no_apaga_antes_de_que_expire_la_gracia() -> None:
    """Un disconnect momentáneo (F5) no debe cerrar la app."""
    watchdog, apagados = _hacer_watchdog([0])

    watchdog.cliente_desconectado()
    try:
        await asyncio.sleep(GRACIA / 5)
        assert apagados == [], "apagó antes de agotar el período de gracia: un F5 cerraría la app"
    finally:
        # La cuenta regresiva sigue viva: cancelarla acá y no depender del
        # teardown del event loop (que dejaría una task pendiente).
        watchdog.cancelar()


async def test_apaga_cuando_expira_la_gracia_sin_clientes() -> None:
    """El caso que cierra el post-mortem: el usuario cerró la ventana y no volvió."""
    watchdog, apagados = _hacer_watchdog([0])

    watchdog.cliente_desconectado()
    await asyncio.sleep(GRACIA * 4)

    assert apagados == [True], (
        "el watchdog no disparó el apagado: sin esto el proceso queda vivo "
        "reteniendo 8080/8765 y el arranque siguiente da WinError 10048"
    )


async def test_reconectar_dentro_de_la_gracia_cancela_el_apagado() -> None:
    """Si el cliente vuelve (F5, reconexión de socket), la app sigue viva."""
    clientes = [0]
    watchdog, apagados = _hacer_watchdog(clientes)

    watchdog.cliente_desconectado()
    await asyncio.sleep(GRACIA / 5)
    clientes[0] = 1
    watchdog.cliente_conectado()
    await asyncio.sleep(GRACIA * 4)

    assert apagados == [], "reconectar dentro de la gracia no canceló la cuenta regresiva"


async def test_no_apaga_si_al_expirar_todavia_hay_clientes() -> None:
    """Guard de carrera: el conteo al VENCER es el que manda.

    Si un cliente se reconectó sin que ``cliente_conectado`` llegara a
    cancelar la cuenta regresiva, el re-chequeo al expirar debe salvarlo.
    """
    clientes = [0]
    watchdog, apagados = _hacer_watchdog(clientes)

    watchdog.cliente_desconectado()
    clientes[0] = 1  # volvió, pero nadie canceló la cuenta regresiva
    await asyncio.sleep(GRACIA * 4)

    assert apagados == [], "apagó con clientes vivos: falta el re-chequeo autoritativo al expirar"


async def test_desconexiones_repetidas_no_apilan_cuentas_regresivas() -> None:
    """Varios disconnect seguidos no deben disparar varios apagados."""
    watchdog, apagados = _hacer_watchdog([0])

    for _ in range(5):
        watchdog.cliente_desconectado()
    await asyncio.sleep(GRACIA * 4)

    assert apagados == [True], f"se apiló más de una cuenta regresiva: {len(apagados)} apagados"


async def test_cancelar_detiene_el_watchdog() -> None:
    """``cancelar()`` debe frenar una cuenta regresiva en curso (apagado ya iniciado por otra vía)."""
    watchdog, apagados = _hacer_watchdog([0])

    watchdog.cliente_desconectado()
    watchdog.cancelar()
    await asyncio.sleep(GRACIA * 4)

    assert apagados == [], "cancelar() no detuvo la cuenta regresiva"


# ---------------------------------------------------------------------------
# Hallazgos de revisión
# ---------------------------------------------------------------------------


async def test_la_gracia_se_cuenta_desde_la_ultima_desconexion() -> None:
    """Con varios clientes, la gracia corre desde que se va el ÚLTIMO, no el primero.

    Con dos pestañas abiertas: A se va en t=0 y arranca la cuenta regresiva; B
    se va en t=0.8·gracia. Si la cuenta sigue siendo la de A, el proceso se
    apaga 0.2·gracia después de que B se fue — el usuario pierde casi toda la
    ventana que el período de gracia le promete.
    """
    clientes = [2]
    watchdog, apagados = _hacer_watchdog(clientes)

    clientes[0] = 1  # se fue A, queda B
    watchdog.cliente_desconectado()
    await asyncio.sleep(GRACIA * 0.8)

    clientes[0] = 0  # ahora sí se fue B
    watchdog.cliente_desconectado()
    try:
        await asyncio.sleep(GRACIA * 0.5)
        assert apagados == [], (
            "apagó a mitad de la gracia del último cliente: la cuenta regresiva seguía siendo la del primero en irse"
        )
        await asyncio.sleep(GRACIA)
        assert apagados == [True], "nunca apagó tras irse el último cliente"
    finally:
        watchdog.cancelar()


async def test_no_arranca_la_cuenta_si_quedan_clientes() -> None:
    """Una desconexión con otros clientes vivos no debe programar nada."""
    watchdog, apagados = _hacer_watchdog([1])

    watchdog.cliente_desconectado()
    try:
        await asyncio.sleep(GRACIA * 4)
        assert apagados == [], "apagó pese a que quedaba un cliente conectado"
    finally:
        watchdog.cancelar()


async def test_una_falla_del_apagado_no_se_traga_en_silencio() -> None:
    """Convención §2.5: re-lanzar las excepciones desconocidas tras loggear.

    Si ``app.shutdown()`` explota y el watchdog se lo traga, el proceso queda
    vivo con los puertos tomados —justo el fallo que esto viene a evitar— y sin
    rastro más allá de una línea de log.
    """
    from sky_claw.antigravity.gui._bootloader import ExitWatchdog

    def _apagar_roto() -> None:
        raise RuntimeError("shutdown explotó")

    watchdog = ExitWatchdog(
        grace_seconds=GRACIA,
        contar_clientes=lambda: 0,
        apagar=_apagar_roto,
    )

    watchdog.cliente_desconectado()
    tarea = watchdog._cuenta_regresiva
    assert tarea is not None

    with pytest.raises(RuntimeError, match="shutdown explotó"):
        await tarea


async def test_cuenta_clientes_con_socket_vivo_pese_al_reconnect_solapado() -> None:
    """El conteo no puede basarse solo en ``has_socket_connection``.

    En NiceGUI 3.12.1 ``Client.handle_disconnect`` hace ``self.tab_id = None``
    INCONDICIONALMENTE, y ``has_socket_connection`` es literalmente
    ``tab_id is not None``. En un reconnect solapado —el socket nuevo completa
    el handshake antes de que se procese la desconexión del viejo—
    ``_num_connections`` sigue en 1 (hay socket vivo) pero ``tab_id`` ya quedó
    en None. Contar por ``has_socket_connection`` daría CERO y el watchdog
    apagaría la app con el usuario conectado.
    """
    from sky_claw.antigravity.gui._bootloader import contar_clientes_con_socket_vivo

    class _ClienteReconnectSolapado:
        has_socket_connection = False  # tab_id quedó en None
        _num_connections = {"doc-1": 1}  # pero el socket de reemplazo sigue vivo

    class _ClienteRealmenteIdo:
        has_socket_connection = False
        _num_connections = {"doc-2": 0}

    assert contar_clientes_con_socket_vivo({"a": _ClienteReconnectSolapado()}) == 1, (
        "contó 0 durante un reconnect solapado: el watchdog apagaría la app mientras el socket de reemplazo sigue vivo"
    )
    assert contar_clientes_con_socket_vivo({"b": _ClienteRealmenteIdo()}) == 0


def test_cuenta_clientes_degrada_a_has_socket_connection() -> None:
    """Si una versión futura de NiceGUI saca ``_num_connections``, no romper."""
    from sky_claw.antigravity.gui._bootloader import contar_clientes_con_socket_vivo

    class _ClienteSinPrivado:
        has_socket_connection = True

    assert contar_clientes_con_socket_vivo({"a": _ClienteSinPrivado()}) == 1


async def test_apaga_si_nadie_llega_a_conectarse_nunca() -> None:
    """El navegador se cerró antes de completar el handshake.

    Sin socket nunca hay ``on_disconnect``, así que sin un plazo inicial no se
    programa NADA y el proceso queda vivo para siempre con 8080/8765 tomados —
    exactamente el modo de falla que el watchdog viene a cerrar.
    """
    watchdog, apagados = _hacer_watchdog([0])

    watchdog.iniciar(plazo_primera_conexion=GRACIA)
    try:
        await asyncio.sleep(GRACIA * 4)
        assert apagados == [True], (
            "nadie se conectó nunca y el watchdog no armó el plazo inicial: "
            "el proceso queda vivo con los puertos tomados"
        )
    finally:
        watchdog.cancelar()


async def test_la_primera_conexion_cancela_el_plazo_inicial() -> None:
    """Si el usuario sí llega a conectarse, el plazo inicial no debe matarlo."""
    clientes = [0]
    watchdog, apagados = _hacer_watchdog(clientes)

    watchdog.iniciar(plazo_primera_conexion=GRACIA)
    await asyncio.sleep(GRACIA / 5)
    clientes[0] = 1
    watchdog.cliente_conectado()
    try:
        await asyncio.sleep(GRACIA * 4)
        assert apagados == [], "el plazo inicial mató la app pese a que el usuario se conectó"
    finally:
        watchdog.cancelar()


def test_la_gracia_sale_de_config_y_no_esta_hardcodeada() -> None:
    """Convención §3: nada de umbrales hardcodeados — deben salir de ``config.py``."""
    from sky_claw import config
    from sky_claw.antigravity.gui import _bootloader

    assert isinstance(config.GUI_EXIT_WATCHDOG_GRACE_SECONDS, (int, float))
    assert isinstance(config.GUI_EXIT_WATCHDOG_FIRST_CONNECT_SECONDS, (int, float))
    assert _bootloader._EXIT_WATCHDOG_GRACE_SECONDS == config.GUI_EXIT_WATCHDOG_GRACE_SECONDS
    # Sin esta segunda igualdad, hardcodear el plazo inicial en _bootloader.py
    # pasaría sin que nadie se entere: el isinstance de arriba mira config, no
    # el módulo que la consume.
    assert _bootloader._EXIT_WATCHDOG_FIRST_CONNECT_SECONDS == config.GUI_EXIT_WATCHDOG_FIRST_CONNECT_SECONDS


@pytest.mark.parametrize("crudo", ["nan", "NaN", "inf", "Infinity", "-inf", "1e400"])
def test_umbral_env_rechaza_valores_no_finitos(crudo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un umbral no finito desactiva el watchdog EN SILENCIO.

    ``float("nan") <= 0`` y ``float("inf") <= 0`` son ambos ``False`` (las
    comparaciones con NaN siempre lo son, e ``inf`` sí es > 0), así que un
    ``SKY_CLAW_GUI_EXIT_GRACE_SECONDS=nan`` pasaba el guard y llegaba a
    ``asyncio.sleep``. Verificado empíricamente: tanto ``sleep(nan)`` como
    ``sleep(inf)`` NUNCA retornan — la cuenta regresiva no se dispara jamás y
    el watchdog queda desactivado sin un solo error a la vista, devolviendo
    justo el bug que este PR viene a cerrar.
    """
    from sky_claw.config import _umbral_env

    monkeypatch.setenv("SKY_CLAW_UMBRAL_DE_PRUEBA", crudo)

    assert _umbral_env("SKY_CLAW_UMBRAL_DE_PRUEBA", 15.0) == 15.0, (
        f"aceptó el umbral no finito '{crudo}': el watchdog queda desactivado en silencio"
    )


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [("30", 30.0), ("7.5", 7.5), ("  20  ", 20.0)],
)
def test_umbral_env_acepta_valores_validos(crudo: str, esperado: float, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard de no-regresión: el override legítimo tiene que seguir funcionando."""
    from sky_claw.config import _umbral_env

    monkeypatch.setenv("SKY_CLAW_UMBRAL_DE_PRUEBA", crudo)

    assert _umbral_env("SKY_CLAW_UMBRAL_DE_PRUEBA", 15.0) == esperado


# ---------------------------------------------------------------------------
# Cableado por modo
# ---------------------------------------------------------------------------


def test_gui_mode_arma_el_watchdog(monkeypatch) -> None:
    """El modo GUI (el .exe de escritorio) debe cerrarse al cerrar la ventana."""
    from sky_claw.antigravity.modes import gui_mode

    capturado: dict[str, object] = {}
    monkeypatch.setattr(gui_mode, "run_nicegui", lambda args, **kw: capturado.update(kw))

    gui_mode.run_gui_mode(object())

    assert capturado.get("exit_on_last_client") is True, (
        "gui_mode no arma el watchdog: cerrar la ventana no mata el proceso"
    )


def test_web_mode_no_arma_el_watchdog(monkeypatch) -> None:
    """El modo web es un SERVIDOR: no puede morirse porque se fue el último navegador."""
    from sky_claw.antigravity.modes import web_mode

    capturado: dict[str, object] = {}
    monkeypatch.setattr(web_mode, "run_nicegui", lambda args, **kw: capturado.update(kw))

    web_mode.run_web_mode(object())

    assert not capturado.get("exit_on_last_client"), (
        "web_mode armó el watchdog: un servidor headless se apagaría al irse el último cliente"
    )
