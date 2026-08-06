"""Ancla de familia + test de alcance del lock cross-process de T-31.

La propiedad que estos tests congelan (enunciada como mecanismo, no como
recordatorio — AGENTS.md): *el lock cross-process solo protege si TODOS los
mutadores de ``ToolsInstaller`` participan, y protege durante TODO el ciclo de
instalación*. Un mutador nuevo rompe el ancla hasta que se le escribe su
receta. La introspección cubre QUÉ rutas participan y que no haya adquisiciones
duplicadas en llamadas indirectas; el test de alcance cubre CUÁNTO dura el lock.
"""

from __future__ import annotations

import asyncio
import inspect
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

import sky_claw.local.tools_installer as tools_installer_module
from sky_claw.local.tools_installer import ToolsInstaller

#: La familia COMPLETA de mutadores. Nueve, NO ocho: ``ensure_skse`` muta el
#: directorio del juego con el mismo TOCTOU (chequeo → descarga → copia →
#: limpieza de DLL huérfanos) y la enumeración original de T-31 ya se había
#: dejado uno afuera — que es precisamente el defecto que la tarea existe para
#: cerrar. ``ensure_community_shaders`` entra con la misma lógica: muta
#: ``mods/`` con el mismo ciclo (chequeo → descarga → extracción → limpieza).
#: Congelar 7 habría repetido el error que estamos arreglando.
MUTADORES_DE_INSTALACION = frozenset(
    {
        "ensure_loot",
        "ensure_xedit",
        "ensure_pandora",
        "ensure_bodyslide",
        "ensure_skse",
        "ensure_ngio",
        "ensure_community_shaders",
        "_ensure_github_mod",
        "_ensure_nexus_mod",
    }
)

#: Receta de lock por mutador: el ``resource_id`` canónico que debe tomar.
#: ``ensure_ngio`` NO toma lock propio (delega todo en ``_ensure_github_mod`` /
#: ``_ensure_nexus_mod``); su receta es la de sus delegados vía ContextVar de
#: reentrada.
RECETAS_DE_LOCK = {
    "ensure_loot": "install_dir",
    "ensure_xedit": "install_dir",
    "ensure_pandora": "install_dir",
    "ensure_bodyslide": "install_dir",
    "ensure_skse": "game_dir",
    "ensure_ngio": "delega (mod_dir de sus componentes)",
    "ensure_community_shaders": "delega (mod_dir de sus componentes)",
    "_ensure_github_mod": "mod_dir",
    "_ensure_nexus_mod": "mod_dir",
}


def _mutadores_detectados() -> frozenset[str]:
    """Corrutinas ``ensure_*`` / ``_ensure_*`` DEFINIDAS en ``ToolsInstaller`` (no heredadas)."""
    return frozenset(
        nombre
        for nombre, fn in inspect.getmembers(ToolsInstaller, inspect.iscoroutinefunction)
        if (nombre.startswith("ensure_") or nombre.startswith("_ensure_"))
        and fn.__module__ == tools_installer_module.__name__
    )


def test_la_familia_de_mutadores_esta_congelada() -> None:
    """Un mutador nuevo no puede entrar sin pasar por estos tests."""
    assert _mutadores_detectados() == MUTADORES_DE_INSTALACION, (
        "cambió la familia de mutadores de ToolsInstaller: cada miembro necesita "
        "su receta de lock cross-process y el ancla de comportamiento"
    )


def test_todo_mutador_tiene_receta_de_lock() -> None:
    """Sin receta, un mutador quedaría fuera de los tests de comportamiento."""
    assert set(RECETAS_DE_LOCK) == _mutadores_detectados(), (
        "RECETAS_DE_LOCK y MUTADORES_DE_INSTALACION divergieron: "
        "todo mutador detectado debe tener su receta documentada"
    )


class _LockFake:
    """Fake del DistributedLockManager que REGISTRA adquisición/liberación.

    La introspección sola no puede demostrar la DURACION del lock — solo
    enumera; este fake mide cuántas veces se adquirió y se liberó.
    """

    def __init__(self) -> None:
        self.held = False
        self.acquire_lock = AsyncMock(side_effect=self._acquire)
        self.release_lock = AsyncMock(side_effect=self._release)
        self.renew_lock = AsyncMock(return_value=True)

    async def _acquire(self, *args: object, **kwargs: object) -> MagicMock:
        self.held = True
        lock = MagicMock()
        lock.resource_id = args[0]
        lock.agent_id = args[1]
        return lock

    async def _release(self, *args: object, **kwargs: object) -> bool:
        self.held = False
        return True


def _build_installer(lock_fake: _LockFake) -> ToolsInstaller:
    class _Guard:
        async def request_approval(self, **kwargs: object) -> object:
            from sky_claw.app.security.hitl import Decision

            return Decision.APPROVED

    gateway = MagicMock()
    gateway.request = AsyncMock(side_effect=RuntimeError("red no disponible en el fixture"))
    return ToolsInstaller(
        hitl=_Guard(),  # type: ignore[arg-type]
        gateway=gateway,
        path_validator=MagicMock(),
        lock_manager=lock_fake,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_lock_tomado_incluso_en_el_early_return(tmp_path: pathlib.Path) -> None:
    """El lock se toma ANTES del chequeo de existencia y se libera al final.

    Adquirir el lock solo alrededor de la extracción deja vivo el TOCTOU del
    chequeo de "ya instalado": dos procesos pueden ver el directorio vacío a la
    vez y ambos descargar.
    """
    lock_fake = _LockFake()
    installer = _build_installer(lock_fake)

    install_dir = tmp_path / "LOOT"
    install_dir.mkdir(parents=True, exist_ok=True)
    (install_dir / "loot.exe").write_text("fake", encoding="utf-8")

    await installer.ensure_loot(install_dir, session=object())

    assert lock_fake.acquire_lock.await_count == 1, "el lock debe tomarse incluso si el tool ya existe"
    assert lock_fake.release_lock.await_count == 1, "el lock debe liberarse al salir"

    # P2a: el resource_id canónico normalizado y el TTL configurado llegan al
    # manager. Sin esto, un resource_id incorrecto (o un ttl olvidado) pasaría
    # la suite en verde aunque el lock proteja el directorio equivocado.
    from sky_claw.local.tools_installer import _install_lock_resource_id

    assert lock_fake.acquire_lock.await_args.args[0] == _install_lock_resource_id(install_dir)
    assert lock_fake.acquire_lock.await_args.args[1] == "tools-installer"
    assert lock_fake.acquire_lock.await_args.kwargs["ttl"] == 3600.0


@pytest.mark.asyncio
async def test_lock_se_libera_con_error_a_mitad_de_instalacion(tmp_path: pathlib.Path) -> None:
    """Un error a mitad NO deja el lock tomado (finally)."""
    lock_fake = _LockFake()
    installer = _build_installer(lock_fake)

    install_dir = tmp_path / "LOOT" / "no-existe"
    install_dir.mkdir(parents=True, exist_ok=True)

    # ensure_loot sin loot.exe: va a la red. El gateway mockeado devuelve el
    # RuntimeError declarado en _build_installer.
    with pytest.raises(RuntimeError, match="red no disponible"):
        await installer.ensure_loot(install_dir, session=object())

    assert lock_fake.release_lock.await_count == 1, "el lock debe liberarse en finally aunque falle la instalación"


@pytest.mark.asyncio
async def test_lock_se_libera_con_cancelacion(tmp_path: pathlib.Path) -> None:
    """asyncio.CancelledError tampoco deja el lock tomado (finally, no except)."""
    lock_fake = _LockFake()
    installer = _build_installer(lock_fake)

    install_dir = tmp_path / "LOOT" / "no-existe-2"
    install_dir.mkdir(parents=True, exist_ok=True)

    # El gateway se CUELGA (no falla): la task queda bloqueada en el request y
    # solo la cancelación la despierta. Si fallara rápido, la task terminaría
    # sola antes de cancelarla y el assert de CancelledError fallaría.
    async def _colgada(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    installer._gateway.request = _colgada  # type: ignore[method-assign]

    task = asyncio.create_task(installer.ensure_loot(install_dir, session=object()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert lock_fake.release_lock.await_count == 1, "la cancelación debe liberar el lock vía finally"


@pytest.mark.asyncio
async def test_ensure_ngio_delega_sin_duplicar_locks_por_componente() -> None:
    """ensure_ngio NO toma lock propio; sus componentes van con lock por mod_dir.

    Un componente no debe frenar a otro (locks independientes por mod_dir), y
    las llamadas delegadas no pueden re-adquirir el lock que el caller ya tiene
    (reentrada → deadlock o doble adquisición).
    """
    # Verificación estructural: ensure_ngio no adquiere locks directamente.
    fuente = inspect.getsource(ToolsInstaller.ensure_ngio)
    assert "acquire_lock" not in fuente
    # S2: enumeración, no muestreo — eliminar la delegación a Nexus (Address
    # Library/Grass Cache Helper) SIN tocar la de GitHub dejaría el test en
    # verde con `or`. La familia completa de delegaciones queda congelada.
    assert "_ensure_github_mod(" in fuente
    assert "_ensure_nexus_mod(" in fuente


@pytest.mark.asyncio
async def test_ensure_community_shaders_delega_sin_duplicar_locks_por_componente() -> None:
    """ensure_community_shaders NO toma lock propio; delega en los helpers por mod_dir.

    Espejo del ancla de ``ensure_ngio``: la delegación a Nexus (Address Library
    y SSE Engine Fixes) y a GitHub (core de Community Shaders) queda congelada
    por enumeración — quitar una de las dos sin tocar este test deja el ancla
    roja, igual que un mutador nuevo sin receta.
    """
    # Verificación estructural: no adquiere locks directamente (receta "delega").
    fuente = inspect.getsource(ToolsInstaller.ensure_community_shaders)
    assert "acquire_lock" not in fuente
    assert "_ensure_github_mod(" in fuente
    assert "_ensure_nexus_mod(" in fuente


@pytest.mark.asyncio
async def test_cancelacion_durante_extraccion_no_libera_el_lock_hasta_que_el_hilo_termina(
    tmp_path: pathlib.Path,
) -> None:
    """S1 (fix V1): cancelar DURANTE la extracción NO libera el lock con el hilo vivo.

    Este es el caso que el test de cancelación viejo NO cubría: allí la
    cancelación caía en un await de red (``asyncio.Event().wait()``, sin hilo
    vivo), donde liberar el lock es seguro. Acá el hilo de ``_extract`` sigue
    escribiendo en ``install_dir`` después de la cancelación: liberar el lock
    en ese momento sería exactamente la corrupción cross-process de T-31 (un
    segundo proceso adquiere y pisa una instalación en vuelo). El release solo
    puede ocurrir DESPUÉS de que el hilo terminó.

    Se usa ``tmp_path`` (NO un path hardcodeado): el test exige partir de un
    directorio VACÍO, y un path fijo persistía ``loot.exe`` entre corridas
    fallidas — el ensure_loot veía ``already_existed=True`` y el hilo nunca
    arrancaba.
    """
    import threading

    from sky_claw.local.tools_installer import ReleaseAsset

    lock_fake = _LockFake()
    installer = _build_installer(lock_fake)

    install_dir = tmp_path / "install"
    install_dir.mkdir(parents=True, exist_ok=True)

    thread_iniciado = threading.Event()
    liberar_thread = threading.Event()
    orden: list[str] = []
    asset = ReleaseAsset(
        name="loot.zip",
        size=1,
        download_url="https://api.github.com/x",
        browser_download_url="https://github.com/x",
    )

    installer._find_github_asset = AsyncMock(return_value=(asset, "1.0.0"))  # type: ignore[method-assign]
    installer._download_asset = AsyncMock(return_value=install_dir / "loot.zip")  # type: ignore[method-assign]

    def extract_lento(archive: object, dest: pathlib.Path) -> None:
        orden.append("hilo:inicio")
        thread_iniciado.set()
        liberar_thread.wait(timeout=10)
        orden.append("hilo:fin")
        (dest / "loot.exe").write_text("fake", encoding="utf-8")

    installer._extract = extract_lento  # type: ignore[method-assign]

    task = asyncio.create_task(installer.ensure_loot(install_dir, session=object()))
    # El threading.Event().set() desde el worker es thread-safe; la espera NO
    # puede bloquear el event loop (congelaría la task que debe arrancar el
    # hilo). Se cede al loop hasta que el thread confirme su arranque.
    for _ in range(500):
        if thread_iniciado.is_set():
            break
        await asyncio.sleep(0.01)
    assert thread_iniciado.is_set(), "el hilo de extracción no arrancó"

    task.cancel()
    await asyncio.sleep(0.05)
    # El hilo sigue escribiendo: el lock NO debe haberse liberado.
    assert lock_fake.release_lock.await_count == 0, (
        "la cancelación durante la extracción NO puede liberar el lock con el hilo vivo mutando el directorio"
    )
    assert orden == ["hilo:inicio"], "el hilo sigue vivo y el release no debe correr"

    liberar_thread.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert lock_fake.release_lock.await_count == 1, "el lock debe liberarse recién cuando el hilo terminó la mutación"
    assert orden == ["hilo:inicio", "hilo:fin"]


@pytest.mark.asyncio
async def test_reentrada_misma_cadena_no_readquiere_ni_relibera() -> None:
    """S3: la reentrancia del ContextVar adquiere UNA vez y libera UNA vez.

    El passthrough de ``ensure_ngio`` → ``_ensure_github_mod``/``_ensure_nexus_mod``
    comparte resource_id si dos componentes usaran el mismo directorio. Un
    re-adquirir en la cadena sería deadlock o doble adquisición; un release del
    passthrough liberaría el lock del caller con el cuerpo todavía activo.
    """
    from sky_claw.local.tools_installer import _bajo_lock_de_instalacion

    lock_fake = _LockFake()
    resource_id = "tools-install:reentrante"

    # `with` múltiples = `with a: with b:` — el segundo __aenter__ corre dentro
    # del cuerpo del primero, que es exactamente la reentrancia que se verifica:
    # la cadena del ContextVar del caller ya contiene resource_id.
    async with (
        _bajo_lock_de_instalacion(lock_fake, resource_id, ttl=3600.0),
        _bajo_lock_de_instalacion(lock_fake, resource_id, ttl=3600.0),
    ):
        pass

    assert lock_fake.acquire_lock.await_count == 1, "la reentrada no debe re-adquirir el lock"
    assert lock_fake.release_lock.await_count == 1, "el passthrough no debe liberar el lock del dueño"
