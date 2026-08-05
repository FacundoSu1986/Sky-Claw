"""Pandora Behavior Engine Runner for Sky-Claw.

Implements M-02 Pandora Runner specifications.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sky_claw.local.tools._process import run_capture
from sky_claw.local.tools.output_targets import pandora_output_target

if TYPE_CHECKING:
    import pathlib

logger = logging.getLogger(__name__)

#: Nombre del log que Pandora emite (README de
#: ``Monitor221hz/Pandora-Behaviour-Engine-Plus``, sección de logging).
#:
#: **Su UBICACIÓN no está verificada contra un rig real** — se asume junto al
#: ejecutable, la convención que comparten BodySlide (``Log_BS.txt``) y el resto
#: de las herramientas de modding, y se prueba también bajo el directorio de
#: salida. Que sea una suposición es tolerable por cómo se consume: un log que no
#: aparece deja el veredicto exactamente como está hoy (exit code), nunca lo
#: vuelve rojo. Confirmar la ruta es parte del smoke de rig pendiente (U-04).
NOMBRE_DEL_LOG_DE_PANDORA = "Engine.log"

#: Severidades que invalidan la corrida. Verificado contra el README: ``ERROR`` es
#: *"prevented engine from completing work"* y ``FATAL`` *"complete engine
#: failure"*. ``WARN`` (*"unexpected condition"*) queda AFUERA a propósito: es la
#: severidad con que aparece la reversión de un nodo inválido —el comportamiento
#: error-tolerant— y también las advertencias rutinarias. Hacerla fallar
#: convertiría corridas sanas en falsos rojos, que es tan malo como el falso
#: verde que este módulo cierra. Se cuenta y se surface, no decide.
_SEVERIDADES_QUE_FALLAN = ("ERROR", "FATAL")

#: ``[Severity]: [Component] > [Data] > [Operation] > [Input] > [Status]``. Los
#: corchetes del README son marcadores de campo, así que no está verificado si la
#: línea real sale ``ERROR:`` o ``[ERROR]:``; se aceptan ambas. Anclado al
#: principio de línea para no matchear la palabra dentro de un mensaje.
_PATRON_DE_SEVERIDAD = re.compile(
    r"^[ \t]*\[?(INFO|WARN|ERROR|FATAL)\]?[ \t]*:[ \t]*(.*)$",
    re.MULTILINE,
)

#: Tope de líneas conservadas por categoría. El texto termina en el prompt de un
#: LLM y en la UI; un log de miles de nodos fallados no aporta más que los
#: primeros.
_MAX_LINEAS_REPORTADAS = 20

#: Techo de lectura del tramo nuevo del log. Un run que loguee cientos de MB no
#: debe cargarse entero en memoria dentro de un hilo del pool.
_MAX_BYTES_DE_LECTURA = 8 * 1024 * 1024

#: Cuánto de la cola cruda del log se adjunta cuando no hay severidades parseadas.
#: Igual que el tope de líneas: ese texto termina en el prompt de un LLM.
_MAX_BYTES_DE_COLA = 2000


class PandoraExecutionError(Exception):
    pass


class PandoraTimeoutError(PandoraExecutionError):
    """Elevada cuando Pandora excede su timeout (U-10).

    Dedicada (en vez de un ``PandoraResult`` con ``success=False``) para que el
    timeout se PROPAGUE por el context manager del lock/rollback en lugar de salir
    limpio: prerequisito del rollback transaccional de salida (U-04). Deriva de
    :class:`PandoraExecutionError`, así que los callers que ya capturan la base la
    traducen al contrato ``success=False`` sin cambios.
    """

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(f"Pandora excedió el timeout de {timeout_seconds:g}s ejecutando el Behavior Engine")


@dataclass(frozen=True, slots=True)
class PandoraConfig:
    pandora_exe: pathlib.Path
    game_path: pathlib.Path
    timeout_seconds: float = 300.0


@dataclass
class PandoraResult:
    success: bool
    return_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    #: Detalle canónico del fallo, vacío en éxito (contrato ``success``/``message``
    #: de ``AGENTS.md``). Lo puebla el runner porque Pandora es una app GUI: su
    #: ``stdout``/``stderr`` puede venir vacío, y entonces el service armaba el
    #: message desde ``stderr or stdout or ""`` → vacío → ``normalize_tool_result``
    #: caía a "error desconocido".
    message: str = ""
    #: Líneas ``WARN`` atribuibles a esta corrida. No deciden el veredicto; viajan
    #: para que el operador vea qué revirtió el motor.
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _VeredictoDelLog:
    """Lo que ``Engine.log`` dice de ESTA corrida.

    ``fallas`` sólo se puebla con texto ATRIBUIBLE a esta corrida: afirmar un
    fallo sobre texto que puede pertenecer a otra convertiría un run exitoso en
    un falso rojo — el modo de fallo simétrico al que este mecanismo cierra.

    ``texto`` es la cola cruda del tramo. Existe porque el veredicto se apoya en
    un patrón de severidad **no verificado contra un log real**: si el formato
    fuera otro, ``fallas`` quedaría vacío y sin la cola cruda no habría ninguna
    pista de por qué. Es la red bajo la suposición, no decoración.
    """

    texto: str = ""
    fallas: tuple[str, ...] = ()
    advertencias: tuple[str, ...] = ()


def _rutas_candidatas_del_log(exe: pathlib.Path, output: pathlib.Path | None) -> tuple[pathlib.Path, ...]:
    """Dónde puede estar ``Engine.log``, sin elegir una sola a ciegas.

    Se sondean las dos ubicaciones plausibles en vez de apostar a una: junto al
    ejecutable (convención del resto de las herramientas) y bajo el directorio de
    salida que Sky-Claw le pasa por ``--output``. Mirar de más es gratis y evita
    que una suposición equivocada apague el mecanismo en silencio.
    """
    candidatas = [exe.parent / NOMBRE_DEL_LOG_DE_PANDORA]
    if output is not None:
        candidatas.append(output / NOMBRE_DEL_LOG_DE_PANDORA)
    return tuple(dict.fromkeys(candidatas))


def _marcas_del_log(rutas: tuple[pathlib.Path, ...]) -> dict[pathlib.Path, int]:
    """Tamaño de cada candidata ANTES de spawnear; ausente ⇒ 0.

    Esta marca es la que hace la atribución, y es deliberadamente distinta del
    mecanismo del hermano ``bodyslide_runner._leer_log``: BodySlide acota la
    corrida por una firma de inicio (``[#Log``) documentada en su fuente, y para
    Pandora **no hay una firma verificada**. Un offset en bytes no depende de
    ningún formato de log, así que no puede envejecer mal con una versión nueva
    del motor.
    """
    marcas: dict[pathlib.Path, int] = {}
    for ruta in rutas:
        try:
            marcas[ruta] = ruta.stat().st_size
        except OSError:
            marcas[ruta] = 0
    return marcas


def _leer_engine_log(marcas: dict[pathlib.Path, int]) -> _VeredictoDelLog:
    """Lee el tramo APENDEADO por esta corrida y lo clasifica por severidad.

    Best-effort ante I/O: el log es señal adicional, no la condición de éxito. Si
    falta, si no creció o si se rotó (quedó más chico que su marca), se devuelve
    un veredicto no atribuible: el texto se conserva como diagnóstico pero no
    decide.
    """
    for ruta, marca in marcas.items():
        try:
            tamano = ruta.stat().st_size
            if tamano <= marca:
                # No creció (log ausente o sin escritura) o se rotó (más chico que
                # la marca). En ambos casos no hay tramo atribuible a esta corrida.
                if tamano < marca:
                    logger.warning("'%s' se achicó durante la corrida; su contenido no se usa para decidir.", ruta)
                continue
            with ruta.open("rb") as handle:
                handle.seek(marca)
                crudo = handle.read(min(tamano - marca, _MAX_BYTES_DE_LECTURA))
        except OSError as exc:
            logger.warning("No se pudo leer '%s': %s", ruta, exc)
            continue

        texto = crudo.decode("utf-8", errors="replace")
        fallas: list[str] = []
        advertencias: list[str] = []
        for severidad, resto in _PATRON_DE_SEVERIDAD.findall(texto):
            linea = f"{severidad}: {resto}".strip()
            if severidad in _SEVERIDADES_QUE_FALLAN:
                fallas.append(linea)
            elif severidad == "WARN":
                advertencias.append(linea)
        return _VeredictoDelLog(
            texto=texto.strip()[-_MAX_BYTES_DE_COLA:],
            fallas=tuple(fallas[:_MAX_LINEAS_REPORTADAS]),
            advertencias=tuple(advertencias[:_MAX_LINEAS_REPORTADAS]),
        )
    return _VeredictoDelLog()


class PandoraRunner:
    """Asynchronous runner for Pandora Behavior Engine."""

    def __init__(self, config: PandoraConfig):
        self.config = config

    @property
    def output_path(self) -> pathlib.Path:
        output_path = pandora_output_target(game=self.config.game_path)
        if output_path is None:
            raise PandoraExecutionError(
                "No se pudo resolver game_path para construir el destino administrado Pandora_Output."
            )
        return output_path

    async def run_pandora(self) -> PandoraResult:
        """Ejecuta Pandora en modo automático para Skyrim Special Edition."""
        logger.info("[M-02] Executing Pandora Behavior Engine...")
        start_time = time.monotonic()
        game_path = self.config.game_path.resolve()

        # Verificado contra el README de Monitor221hz/Pandora-Behaviour-Engine-Plus,
        # sección "Startup Arguments": las opciones declaradas son `--output` (o
        # `-o`), `--auto_run`, `--auto_close`, `--tesv` y `--skyrim_debug64`.
        #
        # Este runner pasaba además `--game "Skyrim Special Edition"` y `--auto`:
        # ninguno de los dos existe.
        #
        # `--auto_run`/`--auto_close` sí están confirmados en el README: corre el
        # motor reusando los mismos mods activos cacheados de la última corrida
        # exitosa, y cierra el motor automáticamente al terminar un lanzamiento.
        # Su semántica exacta de disparo (¿corre de inmediato, o solo habilita
        # cerrar sin intervención una vez que corrió?) no está probada en rig
        # real — pero omitirlos por completo es estrictamente peor: sin ellos
        # Pandora queda esperando en la GUI hasta el timeout de 300s y hace
        # rollback, con cero chance de completar. Con ellos hay lectura verificada
        # de que es el par documentado para correr desatendido. Pendiente de smoke
        # en rig real (`docs/pending_ooda_status.md`, U-04).
        #
        # `--tesv <ruta>` también está confirmado en el README ("Sets the path to
        # the game directory... Intended for users with Wabbajack 'Stock Game'
        # setup or with multiple installations."), o sea que declara el directorio
        # de juego explícitamente en vez de depender de que Pandora lo infiera de
        # `cwd` (que este runner ya fija a `game_path` más abajo, vía
        # ``run_capture(..., cwd=str(game_path))``). Pasarlo de más no rompe nada
        # y saca la corrida de depender de una detección implícita no verificada.
        args = [
            str(self.config.pandora_exe),
            "--auto_run",
            "--auto_close",
            "--tesv",
            str(game_path),
            "--output",
            str(self.output_path),
        ]

        # Marca del log ANTES de spawnear: es lo que acota el tramo a esta corrida.
        # Off-loop porque toca disco, igual que el post-check de ``bodyslide_runner``.
        rutas_de_log = _rutas_candidatas_del_log(self.config.pandora_exe, self.output_path)
        marcas = await asyncio.to_thread(_marcas_del_log, rutas_de_log)

        try:
            stdout, stderr, return_code = await run_capture(
                args,
                timeout=self.config.timeout_seconds,
                cwd=str(game_path),
            )
        except TimeoutError as exc:
            logger.error("Pandora execution timed out.")
            raise PandoraTimeoutError(self.config.timeout_seconds) from exc
        except Exception as exc:
            logger.error("Pandora execution failed: %s", exc)
            raise PandoraExecutionError(f"Failed to execute Pandora: {exc}") from exc

        # El exit code NO reporta el resultado del parcheo: el motor es
        # error-tolerant por diseño (revierte el nodo inválido y sigue), así que
        # sale 0 habiendo descartado mods de animación. `Engine.log` es la única
        # señal de qué se aplicó. Ver el bloque de constantes de arriba.
        veredicto = await asyncio.to_thread(_leer_engine_log, marcas)
        success = return_code == 0 and not veredicto.fallas

        message = ""
        if not success:
            message = self._diagnostico(return_code=return_code, veredicto=veredicto)
            logger.error("[M-02] Pandora no produjo una corrida válida: %s", message)

        return PandoraResult(
            success=success,
            return_code=return_code,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
            duration_seconds=time.monotonic() - start_time,
            message=message,
            warnings=list(veredicto.advertencias),
        )

    @staticmethod
    def _diagnostico(*, return_code: int, veredicto: _VeredictoDelLog) -> str:
        """Arma la causa legible del fallo.

        Nombrar POR QUÉ falló importa especialmente acá: el desenlace nuevo
        —"salió con código 0 pero descartó mods"— es indistinguible de un éxito
        para cualquiera que mire sólo el exit code, que es exactamente cómo
        llegaba hasta ahora.
        """
        if veredicto.fallas:
            return (
                f"Pandora terminó con código {return_code} pero {NOMBRE_DEL_LOG_DE_PANDORA} "
                f"reporta {len(veredicto.fallas)} falla(s). El motor es error-tolerant: revierte el "
                "nodo inválido y sigue, así que el exit code no refleja qué mods de animación se "
                "aplicaron. Los behaviors de los mods fallados NO se generaron.\n" + "\n".join(veredicto.fallas)
            )
        causa = f"Pandora terminó con código {return_code}."
        if veredicto.texto:
            # El log creció pero no se le reconoció ninguna severidad: o el fallo no
            # se loguea con el formato del README, o el formato cambió. Adjuntar la
            # cola cruda es lo único que deja diagnosticar cuál de las dos es.
            causa += f"\n--- {NOMBRE_DEL_LOG_DE_PANDORA} ---\n{veredicto.texto}"
        return causa
