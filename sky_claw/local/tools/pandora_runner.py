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
#: failure"*. La lectura más literal de ``ERROR`` —"impidió que ESTE trabajo se
#: completara"— es justamente la reversión de un nodo inválido: el motor no
#: completó ESE nodo (lo revirtió) aunque siguió con el resto. Bajo esa lectura,
#: el escenario que motiva este módulo (mods de animación descartados en
#: silencio) ya cae en ``ERROR`` y ``_SEVERIDADES_QUE_FALLAN`` lo cubre.
#:
#: **La severidad EXACTA de la reversión no está verificada contra un rig
#: real.** No hay cita del README que la nombre explícitamente (a diferencia de
#: ``ERROR``/``FATAL``, que sí citan su propia definición) — es la lectura más
#: literal, no un hecho confirmado. ``WARN`` (*"unexpected condition, potential
#: issue"*) queda AFUERA a propósito: si la reversión resultara loguearse como
#: ``WARN`` en vez de ``ERROR``, este veredicto NO cerraría el escenario que lo
#: motiva y ``_SEVERIDADES_QUE_FALLAN`` necesitaría crecer (hallazgo qodo, PR
#: #439; el smoke pendiente de rig real, U-04, es lo único que puede confirmar
#: cuál de las dos lecturas es la correcta — ver ``docs/pending_ooda_status.md``).
#: Hasta entonces, WARN se cuenta y se surface, no decide: hacerla fallar sin
#: verificación convertiría corridas sanas en falsos rojos, que es tan malo como
#: el falso verde que este módulo cierra.
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
#:
#: Conserva el PRIMER tramo del apéndice, no la cola: un run que loguee más de
#: 8 MiB de ``INFO`` antes de un ``ERROR`` final lo perdería (hallazgo qodo, PR
#: #439). No se cambió a leer desde el final porque no hay evidencia de que
#: Pandora loguee a ese volumen en una corrida normal — hacerlo sería otra
#: suposición sin cita, la misma clase de riesgo que la severidad de ``WARN``
#: de arriba. Ver ``docs/pending_ooda_status.md``.
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


@dataclass(frozen=True, slots=True)
class _MarcaDelLog:
    """Tamaño E IDENTIDAD de una candidata, capturados ANTES de spawnear.

    La identidad (``st_dev``/``st_ino``) es lo que cierra un hueco que el
    tamaño solo no puede: si Pandora trunca y recrea ``Engine.log`` en vez de
    apendear —comportamiento no verificado contra un rig real—, el archivo
    nuevo puede crecer más allá de la marca vieja aunque sea un archivo
    DISTINTO. Comparar sólo tamaños (``tamano_nuevo > marca``) leería desde un
    offset que ya no tiene relación con el contenido real: un ``ERROR`` en los
    primeros bytes del archivo recreado quedaría fuera del tramo leído,
    produciendo el mismo falso verde que este módulo existe para cerrar
    (hallazgo qodo, PR #439). ``ino=0``/``dev=0`` marca "sin identidad previa"
    (archivo ausente al medir): no hay nada contra qué comparar, así que el
    chequeo de identidad se omite y se lee desde el byte 0 del archivo nuevo.
    """

    tamano: int
    ino: int
    dev: int


def _marcas_del_log(rutas: tuple[pathlib.Path, ...]) -> dict[pathlib.Path, _MarcaDelLog | None]:
    """Tamaño e identidad de cada candidata ANTES de spawnear; ausente ⇒ marca en cero.

    Esta marca es la que hace la atribución, y es deliberadamente distinta del
    mecanismo del hermano ``bodyslide_runner._leer_log``: BodySlide acota la
    corrida por una firma de inicio (``[#Log``) documentada en su fuente, y para
    Pandora **no hay una firma verificada**. Un offset en bytes no depende de
    ningún formato de log, así que no puede envejecer mal con una versión nueva
    del motor.

    ``None`` = marca NO VERIFICABLE (la ruta existe pero no se pudo medir: lock de
    otro proceso, permisos, error transitorio de red en un share). Deliberadamente
    distinto de la marca en cero (ausente): con cero se lee el archivo COMPLETO, y
    sobre un log que ya acumula corridas anteriores eso atribuiría a ESTA corrida
    los ``ERROR``/``FATAL`` de las anteriores — un falso rojo, tan malo como el
    falso verde que este módulo cierra (review CodeRabbit, PR #439). Sólo
    ``FileNotFoundError`` justifica la marca en cero: es el caso esperado y
    frecuente, dado que se sondean DOS candidatas y lo normal es que sólo una
    exista.
    """
    marcas: dict[pathlib.Path, _MarcaDelLog | None] = {}
    for ruta in rutas:
        try:
            estado = ruta.stat()
            marcas[ruta] = _MarcaDelLog(tamano=estado.st_size, ino=estado.st_ino, dev=estado.st_dev)
        except FileNotFoundError:
            marcas[ruta] = _MarcaDelLog(tamano=0, ino=0, dev=0)
        except OSError as exc:
            logger.warning(
                "No se pudo medir '%s' antes de la corrida; su contenido no se usa para decidir: %s", ruta, exc
            )
            marcas[ruta] = None
    return marcas


def _leer_engine_log(marcas: dict[pathlib.Path, _MarcaDelLog | None]) -> _VeredictoDelLog:
    """Lee el tramo APENDEADO por esta corrida y lo clasifica por severidad.

    Best-effort ante I/O: el log es señal adicional, no la condición de éxito. Si
    falta, si no creció, si se rotó (quedó más chico que su marca), si CAMBIÓ DE
    IDENTIDAD (truncado y recreado, ver ``_MarcaDelLog``) o si su marca no es
    verificable (``None``), se devuelve un veredicto no atribuible: el texto se
    conserva como diagnóstico pero no decide.
    """
    for ruta, marca in marcas.items():
        if marca is None:
            # Marca no verificable: no hay base fiable para acotar el tramo a
            # esta corrida. Ya se logueó el warning al medir; no repetirlo acá.
            continue
        try:
            estado = ruta.stat()
            tamano = estado.st_size
            # Identidad distinta ⇒ el archivo que existía a la marca ya NO es
            # este: se truncó y recreó, se borró y recreó, o lo rotó otra
            # herramienta. El tamaño puede haber crecido de casualidad más allá
            # de la marca vieja; leer desde ese offset leería contenido sin
            # relación con lo que la marca midió. Sin identidad previa
            # (``marca.ino == 0``, archivo ausente al medir) no hay nada que
            # comparar y se sigue de largo al chequeo de tamaño normal.
            identidad_cambio = marca.ino != 0 and (estado.st_ino != marca.ino or estado.st_dev != marca.dev)
            if identidad_cambio:
                logger.warning(
                    "'%s' cambió de identidad durante la corrida (truncado/recreado); su contenido no se usa para decidir.",
                    ruta,
                )
                continue
            if tamano <= marca.tamano:
                # No creció (log ausente o sin escritura) o se rotó (más chico que
                # la marca). En ambos casos no hay tramo atribuible a esta corrida.
                if tamano < marca.tamano:
                    logger.warning("'%s' se achicó durante la corrida; su contenido no se usa para decidir.", ruta)
                continue
            with ruta.open("rb") as handle:
                handle.seek(marca.tamano)
                crudo = handle.read(min(tamano - marca.tamano, _MAX_BYTES_DE_LECTURA))
        except FileNotFoundError:
            # Esperado: la marca fue 0 porque no existía, y sigue sin existir (o
            # dejó de existir entre la marca y la lectura). No es una condición
            # de error digna de warning en cada corrida normal (review Copilot,
            # PR #439) — con dos candidatas sondeadas, lo típico es que una falte.
            continue
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
        elif veredicto.texto and not veredicto.fallas and not veredicto.advertencias:
            # El log creció pero ninguna línea matcheó `_PATRON_DE_SEVERIDAD`: o
            # el formato real de Pandora difiere del asumido (README, sección de
            # logging, no verificado contra un log real), o la corrida escribió
            # texto sin severidad reconocible. `_diagnostico` sólo adjunta la
            # cola cruda cuando `not success` — acá `success` es True, así que
            # sin este log la única red del formato no verificado se descarta en
            # silencio, dejando CERO señal diagnóstica para el escenario exacto
            # que este módulo existe para cerrar (hallazgo qodo, PR #439).
            logger.warning(
                "[M-02] Engine.log creció pero ninguna línea matcheó el patrón de severidad esperado; "
                "el veredicto sigue por exit code. Cola: %s",
                veredicto.texto,
            )

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
