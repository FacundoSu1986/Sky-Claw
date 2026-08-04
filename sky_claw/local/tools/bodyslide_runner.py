"""BodySlide Runner for Sky-Claw.

Implements M-03 BodySlide Runner specifications.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import time
from dataclasses import dataclass

from sky_claw.app.security.links import iter_archivos_propios
from sky_claw.local.tools._process import run_capture
from sky_claw.local.tools.bodyslide_config import sembrar_config_de_bodyslide
from sky_claw.local.tools.output_targets import bodyslide_output_root

logger = logging.getLogger(__name__)

#: Extensiones que cuentan como salida real de un batch build. ``.nif`` son las
#: mallas y ``.tri`` los morfos; BodySlide no produce ningún otro artefacto de
#: build. Se comparan en minúsculas porque el FS de Windows no distingue caso.
_EXTENSIONES_DE_ARTEFACTO = (".nif", ".tri")

#: Nombre del log que ``Log::Initialize`` (``src/utils/Log.cpp``) crea al lado del
#: ejecutable. Es el ÚNICO diagnóstico que BodySlide emite: el binario se linkea
#: ``/SUBSYSTEM:Windows`` (``BodySlide.vcxproj``), así que no escribe a stdout ni
#: a stderr nunca.
_NOMBRE_DEL_LOG = "Log_BS.txt"

#: Cuánto del final del log se adjunta al mensaje de fallo. El log acumula hasta
#: cuatro corridas antes de rotar, así que leerlo entero traería ruido viejo.
_COLA_DEL_LOG_BYTES = 4096

#: Firma que ``Log::Initialize`` escribe al arrancar cada corrida
#: (``src/utils/Log.cpp``: ``signature = "[#Log"`` + fecha). Es el único
#: delimitador que permite atribuir una línea del log a ESTA corrida y no a una
#: de las tres anteriores que el archivo todavía conserva.
_FIRMA_DE_CORRIDA = "[#Log"

#: Prefijo con que ``GroupBuild`` (línea 4752) reporta cada outfit que no pudo
#: construir: ``wxLogError("Failed to build '%s': %s", ...)``. Es la ÚNICA señal
#: acotada de fallo parcial, porque el exit code sigue siendo 0 con ``ret == 3``.
_MARCA_DE_FALLO_PARCIAL = "Failed to build"


class BodySlideExecutionError(Exception):
    pass


class BodySlideTimeoutError(BodySlideExecutionError):
    """Elevada cuando BodySlide excede su timeout (U-10).

    Dedicada (en vez de un ``BodySlideResult`` con ``success=False``) para que el
    timeout se PROPAGUE por el context manager del lock/rollback en lugar de salir
    limpio: prerequisito del rollback transaccional de salida (U-04). Deriva de
    :class:`BodySlideExecutionError`, así que los callers que ya capturan la base
    la traducen al contrato ``success=False`` sin cambios.
    """

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(f"BodySlide excedió el timeout de {timeout_seconds:g}s durante la generación batch")


@dataclass(frozen=True, slots=True)
class BodySlideConfig:
    bodyslide_exe: pathlib.Path
    game_path: pathlib.Path
    timeout_seconds: float = 600.0


@dataclass
class BodySlideResult:
    success: bool
    return_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    #: Detalle canónico del fallo, vacío en éxito (contrato ``success``/``message``
    #: de AGENTS.md). Lo puebla el runner porque ``stdout``/``stderr`` de un binario
    #: ``/SUBSYSTEM:Windows`` son SIEMPRE vacíos: sin esto, un fallo de BodySlide
    #: llegaba al LLM como ``success=False`` con ``message=""``.
    message: str = ""
    #: Cantidad de ``.nif``/``.tri`` encontrados bajo el destino tras la corrida.
    #: Es la evidencia de que el build ocurrió — ver :meth:`BodySlideRunner.run_batch`.
    artifacts_built: int = 0


def _contar_artefactos(destino: pathlib.Path) -> int:
    """Cuenta ``.nif``/``.tri`` bajo *destino*, recursivamente y sin cruzar enlaces.

    Recursivo porque ``BuildListBodies`` compone cada salida como
    ``datapath + currentSet.GetOutputPath()``: las mallas aterrizan en subárboles
    (``meshes/actors/character/...``), nunca sueltas en la raíz del target. Un
    conteo plano daría cero en todo build real.

    ``iter_archivos_propios`` y no ``Path.rglob`` (review Copilot #433): ``rglob``
    decide si desciende con ``is_dir(follow_symlinks=False)``, que frena en un
    symlink pero **no en un junction de Windows** —``IO_REPARSE_TAG_MOUNT_POINT``
    no es un symlink para ``os.path.islink``—, la misma ceguera que hubo que
    cerrar en el borrado (#404, #405). Acá el costo sería que mallas AJENAS
    detrás de un enlace validaran nuestro build: el post-check daría por
    construido algo que la herramienta nunca escribió, que es exactamente el
    falso verde que este post-check existe para cerrar.

    Un destino inexistente cuenta cero en vez de lanzar: que BodySlide no haya
    creado siquiera el directorio es justamente uno de los desenlaces que este
    conteo tiene que poder reportar.
    """
    return sum(
        1 for ruta, _identidad in iter_archivos_propios(destino) if ruta.suffix.lower() in _EXTENSIONES_DE_ARTEFACTO
    )


def _leer_log(exe: pathlib.Path) -> tuple[str, bool]:
    """Devuelve ``(texto de la corrida actual, si quedó acotado a ella)``.

    ``Log::Initialize`` (``src/utils/Log.cpp``) escribe una firma ``[#Log <fecha>]``
    al arrancar cada corrida y **sólo rota el archivo tras la cuarta**, así que el
    log contiene hasta cuatro corridas superpuestas. Acotar por la ÚLTIMA firma es
    lo que separa "esta corrida falló" de "alguna de las tres anteriores falló".

    El segundo elemento dice si ese recorte se pudo hacer. Importa porque si la
    firma no aparece en la cola leída no hay forma de saber a qué corrida pertenece
    cada línea, y afirmar un fallo sobre texto no atribuible convertiría un build
    exitoso en un falso ROJO — tan malo como el falso verde que este módulo cierra.
    En ese caso el texto se sigue mostrando como diagnóstico, pero no se usa para
    decidir.

    Best-effort ante I/O: el log es diagnóstico adicional, no la condición de
    éxito. Si falta (``LogLevel=-1`` en ``Config.xml``, o un fallo tan temprano que
    ``Log::Initialize`` no llegó a correr) el resto del veredicto no cambia.
    """
    log = exe.parent / _NOMBRE_DEL_LOG
    try:
        with log.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - _COLA_DEL_LOG_BYTES))
            texto = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return "", False

    corte = texto.rfind(_FIRMA_DE_CORRIDA)
    if corte == -1:
        return texto.strip(), False
    return texto[corte:].strip(), True


class BodySlideRunner:
    """Asynchronous runner for BodySlide batch generation."""

    def __init__(self, config: BodySlideConfig):
        self.config = config

    async def run_batch(
        self,
        group: str,
        output_path: str,
        *,
        preset: str | None = None,
        build_morphs: bool = True,
    ) -> BodySlideResult:
        """Execute BodySlide in batch mode for a specific group and output path.

        Format: ``BodySlide.exe -gbuild <Group> -t <Output> [-p <Preset>] [-tri]``

        Los nombres de opción están verificados contra
        ``ousnius/BodySlide-and-Outfit-Studio``,
        ``src/program/BodySlideApp.cpp::OnCmdLineParsed``, que declara ``gbuild``
        (batch build por grupos), ``t`` (directorio de salida), ``p`` (preset),
        ``tri`` y ``preview``. Este runner pasaba ``-b``/``-o``: ninguno de los dos
        es una opción declarada, así que el batch nunca se ejecutó.

        ``build_morphs`` va en ``True`` por defecto porque sin ``-tri``,
        ``GroupBuild`` pasa ``cmdTri=false`` a ``BuildListBodies`` y no se emiten
        los ``.tri`` — los archivos que OBody NG / RaceMenu cargan en memoria para
        los morfos dinámicos. Un build sin morfos es silenciosamente incompleto,
        no una variante razonable por defecto.

        ``preset`` se omite del comando cuando es ``None`` en vez de mandarse
        vacío: ``-p ""`` sobrescribiría ``SelectedPreset`` en ``BodySlide.xml``,
        mientras que omitirlo conserva el comportamiento declarado ("defaults to
        last used preset").

        **El éxito NO se deriva del exit code.** ``GroupBuild`` captura el
        resultado de ``BuildListBodies`` (línea 4741) y lo usa sólo para elegir un
        mensaje de log; después sale por ``sliderView->Close(true)`` (línea 4763),
        y el ejecutable no define ``SetExitCode`` ni override de ``OnRun`` en
        ninguna parte. O sea que **termina en 0 aunque ``ret == 3``** ("the
        following sets failed") y aunque no haya construido nada — el caso de MO2
        sin USVFS, donde no ve ningún ``SliderSet`` y itera una lista vacía.
        Por eso el veredicto exige además evidencia en disco: sin artefacto no
        hubo build, diga lo que diga el proceso.

        Esto es lo que vuelve ALCANZABLE el puente ``_BodySlideRunFallidoError``
        de ``run_bodyslide_batch``: ese puente convierte ``success=False`` en la
        excepción que dispara el rollback de ``DirectoryRollback``, y mientras el
        éxito se derivara del exit code nunca se cruzaba.
        """
        logger.info(f"[M-03] Executing BodySlide batch for group {group}...")
        start_time = time.monotonic()

        # Antes de spawnear: apagar los modales de arranque. Los dos chequeos que
        # los abren (``OnInit`` líneas 243 y 2798) leen el ``Config.xml`` y NO la
        # línea de comandos, así que pasar ``-t`` no alcanza — ver
        # ``bodyslide_config``. Off-loop porque toca disco. Best-effort: si no se
        # puede sembrar, la corrida sigue y falla —si falla— por su propia causa.
        raiz_de_salida = bodyslide_output_root(game=self.config.game_path)
        if raiz_de_salida is not None:
            await asyncio.to_thread(
                sembrar_config_de_bodyslide,
                exe_dir=self.config.bodyslide_exe.parent,
                game_path=self.config.game_path,
                output_root=raiz_de_salida,
            )

        args = [str(self.config.bodyslide_exe), "-gbuild", group, "-t", output_path]
        if preset:
            args += ["-p", preset]
        if build_morphs:
            args.append("-tri")

        try:
            stdout, stderr, return_code = await run_capture(
                args,
                timeout=self.config.timeout_seconds,
                cwd=str(self.config.game_path),
            )
        except TimeoutError as exc:
            logger.error("BodySlide execution timed out.")
            raise BodySlideTimeoutError(self.config.timeout_seconds) from exc
        except Exception as e:
            logger.error(f"BodySlide execution failed: {e}")
            raise BodySlideExecutionError(f"Failed to execute BodySlide: {e}") from e

        # Off-loop: recorrer un árbol de mallas es I/O real y este runner corre en
        # el event loop del daemon (mismo criterio que ``_process.kill_and_reap``).
        destino = pathlib.Path(output_path)
        artefactos = await asyncio.to_thread(_contar_artefactos, destino)
        log, log_es_de_esta_corrida = await asyncio.to_thread(_leer_log, self.config.bodyslide_exe)
        # Fallo PARCIAL: algunos outfits fallaron y otros no, así que hay artefactos
        # en disco y el exit code sigue siendo 0 — el conteo por sí solo no lo ve.
        # Sólo se afirma si el texto se pudo atribuir a ESTA corrida (ver _leer_log).
        fallo_parcial = log_es_de_esta_corrida and _MARCA_DE_FALLO_PARCIAL in log
        success = return_code == 0 and artefactos > 0 and not fallo_parcial

        message = ""
        if not success:
            message = self._diagnostico(
                return_code=return_code,
                artefactos=artefactos,
                destino=destino,
                log=log,
                fallo_parcial=fallo_parcial,
            )
            logger.error("[M-03] BodySlide no produjo un build válido: %s", message)

        return BodySlideResult(
            success=success,
            return_code=return_code,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
            duration_seconds=time.monotonic() - start_time,
            message=message,
            artifacts_built=artefactos,
        )

    @staticmethod
    def _diagnostico(
        *,
        return_code: int,
        artefactos: int,
        destino: pathlib.Path,
        log: str,
        fallo_parcial: bool,
    ) -> str:
        """Arma la causa legible del fallo, nombrando el destino inspeccionado.

        Nombrar el destino no es decorativo: el fallo más frecuente y más difícil
        de leer es "salió bien pero no construyó nada", y ahí saber CUÁL directorio
        se miró es la diferencia entre diagnosticar y adivinar.
        """
        if return_code != 0:
            causa = f"BodySlide terminó con código {return_code} (artefactos en '{destino}': {artefactos})."
        elif fallo_parcial:
            causa = (
                f"BodySlide construyó {artefactos} artefacto(s) en '{destino}' pero reportó outfits "
                "fallidos en su log. Es un build PARCIAL: el exit code sigue siendo 0, así que la "
                "única señal es el log. Las mallas de los outfits fallidos no existen."
            )
        else:
            causa = (
                f"BodySlide terminó con código 0 pero no generó ninguna malla en '{destino}'. "
                "Su exit code no refleja el resultado del build, así que un batch vacío "
                "—típicamente porque no encontró SliderSets visibles— sale como éxito."
            )
        return f"{causa}\n--- {_NOMBRE_DEL_LOG} ---\n{log}" if log else causa
