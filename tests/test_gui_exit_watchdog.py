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
    await asyncio.sleep(GRACIA / 5)

    assert apagados == [], "apagó antes de agotar el período de gracia: un F5 cerraría la app"


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
