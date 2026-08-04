"""El exit code de BodySlide no reporta el resultado del build — el artefacto sí.

**El mecanismo.** ``BodySlideApp::GroupBuild`` captura el resultado del build
(``int ret = BuildListBodies(...)``, línea 4741) y lo usa SÓLO para elegir un
mensaje de log o abrir un diálogo; después sale por ``sliderView->Close(true)``
(línea 4763). No hay ``SetExitCode`` ni override de ``OnRun`` en todo el
ejecutable, así que **el proceso termina en 0 aunque ``ret == 3``** ("the
following sets failed", con ``failedOutfits`` poblado).

Eso deja dos falsos verdes que ``success = return_code == 0`` no distingue:

1. **Build parcial** — algunos outfits fallaron, otros no. El target queda
   NO vacío, así que ``DirectoryRollback._sin_regenerar`` lo cuenta como
   regenerado y descarta el backup. Salida parcial conservada y reportada como
   éxito.
2. **Build vacío** — bajo MO2 sin USVFS, BodySlide no ve ningún ``SliderSet``
   (viven dispersos en ``mods/*/CalienteTools/BodySlide/``), así que
   ``GroupBuild`` itera una lista vacía y sale 0 sin construir nada.
   ``DirectoryRollback`` protege el DATO (restaura el contenido previo al ver el
   target vacío), pero la tool igual reporta ``success=True``.

**Por qué importa más de lo que parece.** ``run_bodyslide_batch`` ya tiene el
puente fallo→excepción correcto (``_BodySlideRunFallidoError`` →
``DirectoryRollback`` restaura), pero se dispara con ``result.success == False``.
Mientras el éxito se derive del exit code, ese puente es **inalcanzable** para el
modo de fallo más común del ejecutable. El post-check del artefacto es lo que lo
vuelve alcanzable.
"""

from __future__ import annotations

import pathlib
import threading
from unittest.mock import AsyncMock, patch

import pytest

from sky_claw.local.tools.bodyslide_runner import BodySlideConfig, BodySlideRunner
from tests._symlink_guard import crear_junction, is_junction_real, junction_guard


def _runner(tmp_path: pathlib.Path) -> BodySlideRunner:
    return BodySlideRunner(
        BodySlideConfig(bodyslide_exe=tmp_path / "BodySlide.exe", game_path=tmp_path / "game"),
    )


def _captura(return_code: int = 0) -> AsyncMock:
    """``run_capture`` que simula un proceso GUI: stdout/stderr vacíos.

    No es una simplificación del doble: ``BodySlide.exe`` se linkea
    ``/SUBSYSTEM:Windows`` (``BodySlide.vcxproj``), así que **nunca** escribe a
    stdout/stderr. Un doble que devolviera texto ahí estaría probando un
    ejecutable que no existe.
    """
    return AsyncMock(return_value=(b"", b"", return_code))


async def test_exit_cero_sin_mallas_no_es_exito(tmp_path: pathlib.Path) -> None:
    """Exit 0 con el target vacío ⇒ ``success=False``.

    Es el caso de MO2 sin USVFS: cero ``SliderSets`` visibles, cero outfits
    construidos, exit 0. Hoy se reporta como éxito y las etapas siguientes del
    pipeline consumen mallas que nunca se generaron.
    """
    runner = _runner(tmp_path)
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    destino.mkdir(parents=True)

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", _captura(0)):
        result = await runner.run_batch("CBBE", str(destino))

    assert result.return_code == 0
    assert result.success is False
    assert result.artifacts_built == 0


async def test_exit_cero_sin_el_directorio_tampoco_es_exito(tmp_path: pathlib.Path) -> None:
    """El target puede no existir siquiera — no debe explotar ni reportar éxito."""
    runner = _runner(tmp_path)
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"  # nunca creado

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", _captura(0)):
        result = await runner.run_batch("CBBE", str(destino))

    assert result.success is False
    assert result.artifacts_built == 0


async def test_mensaje_nombra_la_causa_en_vez_de_quedar_vacio(tmp_path: pathlib.Path) -> None:
    """El fallo tiene que traer causa legible.

    ``_bodyslide_dict_de_resultado`` deriva ``message`` de ``stderr or stdout``, y
    para un binario ``/SUBSYSTEM:Windows`` los dos son SIEMPRE vacíos: un fallo
    llegaba al LLM como ``success=False, message=""``. El runner es quien tiene
    que poblar la causa.
    """
    runner = _runner(tmp_path)
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    destino.mkdir(parents=True)

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", _captura(0)):
        result = await runner.run_batch("CBBE", str(destino))

    assert result.message, "un fallo sin mensaje es un fallo sobre el que nadie puede actuar"
    assert str(destino) in result.message


@pytest.mark.parametrize("artefacto", ["cuerpo.nif", "morfos.tri"])
async def test_con_artefacto_es_exito_y_mensaje_vacio(tmp_path: pathlib.Path, artefacto: str) -> None:
    """Con mallas o morfos en el target, el run es exitoso y ``message`` va vacío.

    ``message`` vacío en éxito es el contrato canónico del repo (AGENTS.md).
    """
    runner = _runner(tmp_path)
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    destino.mkdir(parents=True)
    (destino / artefacto).write_bytes(b"\x00")

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", _captura(0)):
        result = await runner.run_batch("CBBE", str(destino))

    assert result.success is True
    assert result.artifacts_built == 1
    assert result.message == ""


async def test_cuenta_artefactos_en_subdirectorios(tmp_path: pathlib.Path) -> None:
    """BodySlide escribe bajo ``meshes/actors/...``: el conteo debe ser recursivo.

    ``BuildListBodies`` compone el destino como ``datapath + currentSet.GetOutputPath()``,
    así que las mallas aterrizan en subárboles, nunca sueltas en la raíz del target.
    Un conteo no recursivo daría cero en TODO build real.
    """
    runner = _runner(tmp_path)
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    anidado = destino / "meshes" / "actors" / "character"
    anidado.mkdir(parents=True)
    (anidado / "femalebody_0.nif").write_bytes(b"\x00")
    (anidado / "femalebody_1.nif").write_bytes(b"\x00")

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", _captura(0)):
        result = await runner.run_batch("CBBE", str(destino))

    assert result.success is True
    assert result.artifacts_built == 2


async def test_exit_no_cero_sigue_siendo_fallo_aunque_haya_artefactos(tmp_path: pathlib.Path) -> None:
    """El post-check AGREGA una condición de fallo, no reemplaza la del exit code.

    Un exit non-zero con salida parcial en disco sigue siendo un fallo: es
    exactamente el escenario en que ``DirectoryRollback`` debe restaurar.
    """
    runner = _runner(tmp_path)
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    destino.mkdir(parents=True)
    (destino / "parcial.nif").write_bytes(b"\x00")

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", _captura(3)):
        result = await runner.run_batch("CBBE", str(destino))

    assert result.success is False
    assert result.return_code == 3


async def test_build_parcial_no_es_exito_aunque_haya_artefactos(tmp_path: pathlib.Path) -> None:
    """`ret == 3` con salida parcial en disco NO es éxito (review CodeRabbit #433).

    Es el falso verde que el conteo de artefactos por sí solo no ataja: algunos
    outfits fallaron y otros no, así que el target queda NO vacío y el exit code
    sigue siendo 0. La señal acotada que emite la herramienta es
    ``wxLogError("Failed to build '%s': %s", ...)`` en ``GroupBuild`` (línea 4752),
    que aterriza en ``Log_BS.txt``.
    """
    runner = _runner(tmp_path)
    runner.config.bodyslide_exe.parent.mkdir(parents=True, exist_ok=True)
    (runner.config.bodyslide_exe.parent / "Log_BS.txt").write_text(
        "[#Log 2026-08-04]\nStarted batch build\nFailed to build 'Vestido': missing shape data\n",
        encoding="utf-8",
    )
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    destino.mkdir(parents=True)
    (destino / "cuerpo.nif").write_bytes(b"\x00")  # algunos SÍ se construyeron

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", _captura(0)):
        result = await runner.run_batch("CBBE", str(destino))

    assert result.artifacts_built == 1
    assert result.success is False, "un build parcial no puede reportarse como éxito"
    assert "Vestido" in result.message


async def test_fallo_de_una_corrida_anterior_no_contamina_la_actual(tmp_path: pathlib.Path) -> None:
    """El log acumula hasta 4 corridas: sólo cuenta lo posterior al último ``[#Log``.

    ``Log::Initialize`` (``src/utils/Log.cpp``) escribe una firma ``[#Log <fecha>]``
    al arrancar cada corrida y sólo rota tras la cuarta. Sin acotar por esa firma,
    un "Failed to build" viejo convertiría en fallo a un build actual perfectamente
    exitoso — un falso ROJO, que es tan malo como el falso verde que este PR cierra.
    """
    runner = _runner(tmp_path)
    runner.config.bodyslide_exe.parent.mkdir(parents=True, exist_ok=True)
    (runner.config.bodyslide_exe.parent / "Log_BS.txt").write_text(
        "[#Log 2026-08-01]\nFailed to build 'Vestido': missing shape data\n"
        "[#Log 2026-08-04]\nAll group build sets processed successfully!\n",
        encoding="utf-8",
    )
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    destino.mkdir(parents=True)
    (destino / "cuerpo.nif").write_bytes(b"\x00")

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", _captura(0)):
        result = await runner.run_batch("CBBE", str(destino))

    assert result.success is True, "un fallo de una corrida previa no es un fallo de esta"
    assert result.message == ""


async def test_no_cuenta_artefactos_detras_de_un_enlace(tmp_path: pathlib.Path) -> None:
    """El conteo no atraviesa enlaces (review Copilot #433).

    ``Path.rglob`` decide si desciende con ``is_dir(follow_symlinks=False)``: frena
    en un symlink pero **no en un junction de Windows**, porque
    ``IO_REPARSE_TAG_MOUNT_POINT`` no es un symlink para ``os.path.islink``. Contar
    a través de un enlace haría que mallas AJENAS validaran nuestro build — el
    post-check pasaría sin que la herramienta hubiera escrito nada.
    """
    runner = _runner(tmp_path)
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    destino.mkdir(parents=True)
    ajeno = tmp_path / "mallas_ajenas"
    ajeno.mkdir()
    (ajeno / "de_otro_mod.nif").write_bytes(b"\x00")
    try:
        (destino / "enlace").symlink_to(ajeno, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("crear symlinks requiere privilegios no disponibles en este entorno")

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", _captura(0)):
        result = await runner.run_batch("CBBE", str(destino))

    assert result.artifacts_built == 0, "las mallas detrás del enlace no son salida de este build"
    assert result.success is False


async def test_fallo_parcial_se_detecta_aunque_el_log_de_la_corrida_sea_enorme(tmp_path: pathlib.Path) -> None:
    """Un log grande no puede desactivar la detección de fallo parcial (review qodo #433).

    La firma ``[#Log`` va al PRINCIPIO de la corrida. Leyendo una cola fija, un
    batch con muchos outfits la deja fuera de la ventana: el texto pasa a ser
    "no atribuible", la detección se apaga y el build parcial vuelve a reportarse
    como éxito — evadiendo el ``DirectoryRollback`` y dejando mallas incompletas.
    Es el mismo falso verde que este post-check existe para cerrar, reintroducido
    por el recorte.

    Se busca la firma hacia atrás en vez de asumir que entra en una ventana fija.
    """
    runner = _runner(tmp_path)
    runner.config.bodyslide_exe.parent.mkdir(parents=True, exist_ok=True)
    relleno = "\n".join(f"Processing outfit numero {i:05d} con un nombre largo para ocupar bytes" for i in range(400))
    (runner.config.bodyslide_exe.parent / "Log_BS.txt").write_text(
        f"[#Log 2026-08-04]\nStarted batch build\n{relleno}\nFailed to build 'Vestido': missing shape data\n",
        encoding="utf-8",
    )
    assert (runner.config.bodyslide_exe.parent / "Log_BS.txt").stat().st_size > 20_000, (
        "el log tiene que superar la cola"
    )

    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    destino.mkdir(parents=True)
    (destino / "cuerpo.nif").write_bytes(b"\x00")

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", _captura(0)):
        result = await runner.run_batch("CBBE", str(destino))

    assert result.success is False, "el tamaño del log no puede decidir si se detecta el fallo parcial"
    assert "Vestido" in result.message


def test_leer_log_termina_si_el_archivo_se_trunca_a_mitad(tmp_path: pathlib.Path) -> None:
    """La búsqueda hacia atrás no puede quedar girando (review qodo #433).

    El tamaño se captura una vez al abrir. Si BodySlide rota el log mientras se
    lee —``Log::Initialize`` lo trunca al arrancar la quinta corrida— los ``read``
    posteriores devuelven ``b""``: sin garantía de progreso el ``while`` gira para
    siempre quemando un hilo del pool de ``asyncio.to_thread``, que es una
    denegación de servicio del daemon, no un error de lectura.

    Se ejercita en un hilo con ``join(timeout)`` para que un bucle infinito se
    manifieste como fallo acotado en vez de colgar la suite.
    """
    from sky_claw.local.tools.bodyslide_runner import _leer_log

    exe = tmp_path / "BodySlide.exe"
    log = tmp_path / "Log_BS.txt"
    log.write_bytes(b"x" * 100_000)  # sin la firma: fuerza el recorrido completo

    class _HandleQueSeVacia:
        """Reporta el tamaño original pero ya no tiene bytes que entregar."""

        def __enter__(self) -> _HandleQueSeVacia:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def seek(self, *_args: object) -> int:
            return 0

        def tell(self) -> int:
            return 100_000

        def read(self, *_args: object) -> bytes:
            return b""

    resultado: list[tuple[str, bool]] = []
    with patch.object(pathlib.Path, "open", lambda *_a, **_k: _HandleQueSeVacia()):
        hilo = threading.Thread(target=lambda: resultado.append(_leer_log(exe)), daemon=True)
        hilo.start()
        hilo.join(timeout=5.0)

    assert not hilo.is_alive(), "_leer_log quedó girando: la lectura sin progreso no corta el bucle"
    assert resultado == [("", False)]


@junction_guard
async def test_no_cuenta_artefactos_detras_de_un_junction(tmp_path: pathlib.Path) -> None:
    """El caso que ``rglob`` SÍ contaba de más — la mitad Windows del invariante.

    El test de symlink de arriba no ancla nada por sí solo: ``Path.rglob`` **ya**
    frenaba en un symlink (decide con ``is_dir(follow_symlinks=False)``), así que
    un revert a ``rglob`` seguiría pasándolo. El junction es donde las dos
    políticas divergen, porque ``IO_REPARSE_TAG_MOUNT_POINT`` no es un symlink
    para ``os.path.islink`` (review CodeRabbit #433).

    Se afirma también lo que habría contado el recorrido ingenuo: sin eso, el
    test no demuestra que atrapa la regresión que dice atrapar.
    """
    ajeno = tmp_path / "mallas_ajenas"
    ajeno.mkdir()
    (ajeno / "de_otro_mod.nif").write_bytes(b"\x00")

    runner = _runner(tmp_path)
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    destino.mkdir(parents=True)
    assert crear_junction(destino / "enlace", ajeno) is None
    assert is_junction_real(destino / "enlace")

    # El recorrido que este post-check reemplaza: entra al junction y cuenta ajeno.
    rglob_ingenuo = sum(1 for f in destino.rglob("*") if f.suffix.lower() == ".nif" and f.is_file())

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", _captura(0)):
        result = await runner.run_batch("CBBE", str(destino))

    assert rglob_ingenuo == 1, "el rglob crudo tiene que contar la malla ajena (si no, el test no prueba nada)"
    assert result.artifacts_built == 0, "las mallas detrás del junction no son salida de este build"
    assert result.success is False
    assert (ajeno / "de_otro_mod.nif").exists(), "no se toca el árbol ajeno"


async def test_log_de_bodyslide_se_incorpora_al_mensaje(tmp_path: pathlib.Path) -> None:
    """``Log_BS.txt`` es el ÚNICO diagnóstico real que produce BodySlide.

    ``Log::Initialize`` (``src/utils/Log.cpp``) instala un ``wxLogStream`` sobre
    ``<dir del exe>/Log_BS.txt``; no hay salida por stdout/stderr. Si el runner no
    lo lee, el operador no tiene con qué diagnosticar un fallo.
    """
    runner = _runner(tmp_path)
    runner.config.bodyslide_exe.parent.mkdir(parents=True, exist_ok=True)
    (runner.config.bodyslide_exe.parent / "Log_BS.txt").write_text(
        "[#Log 2026-08-03]\nFailed to build 'Vestido': missing shape data\n",
        encoding="utf-8",
    )
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    destino.mkdir(parents=True)

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", _captura(0)):
        result = await runner.run_batch("CBBE", str(destino))

    assert result.success is False
    assert "missing shape data" in result.message
