"""
Runner para DynDOLOD y TexGen - Pipeline de generación de LODs.

Este módulo proporciona la integración con DynDOLOD y TexGen para generar
LODs de forma automatizada con empaquetado para Mod Organizer 2.

Reference:
    - Patrón subprocess: synthesis_runner.py:303-348
    - DynDOLOD CLI: https://dyndolod.info/Help/Command-Line-Argument
"""

from __future__ import annotations

import asyncio
import configparser
import contextlib
import logging
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from sky_claw.app.security.links import (
    iter_archivos_propios,
    link_kind_or_raise_with_retry,
    rmtree_link_aware,
)
from sky_claw.local.tools._process import assign_kill_on_close_job, close_job, kill_and_reap
from sky_claw.local.tools.output_targets import dyndolod_output_target
from sky_claw.logging_config import pipeline_tx_id_var, subprocess_error_extra

logger = logging.getLogger(__name__)

#: Índice de etapa de DynDOLOD en el DAG de ``sky_claw/local/AGENTS.md`` §1
#: (SOP §5 regla 5). Se define UNA vez y todos los registros de fallo la
#: referencian por nombre: duplicar el literal en cada uno garantiza que un
#: reordenamiento del DAG deje la mitad desactualizada.
#:
#: Es el MISMO 9 que declara ``dyndolod_service.py``, escrito dos veces porque el
#: runner no puede importar la constante del servicio sin cerrar un ciclo (el
#: servicio importa al runner). Nada en el lenguaje acopla las dos: lo hace
#: ``test_la_etapa_del_runner_es_una_constante_y_coincide_con_la_del_servicio``,
#: que exige igualdad — mismo instrumento que ``_LETRAS_ADMINISTRADAS`` usa para
#: la otra lista de este archivo que también está escrita dos veces.
_ETAPA_DYNDOLOD = 9


def _tx_id() -> int | None:
    """Transacción del journal en curso, o ``None`` si el runner corre sin servicio.

    La setea ``DynDOLODPipelineService.execute`` con
    ``correlacion_de_transaccion(tx_id)`` alrededor de su bloque con el runner
    (``sky_claw/logging_config.py``). Acá se LEE, nunca se escribe: el runner no
    conoce el journal ni abre transacciones.

    Por qué existe (SOP §5 regla 5): este runner y su servicio describen el mismo
    incidente desde dos alturas —el servicio sabe la transacción, el runner sabe
    el exit code y la causa técnica que el handler del servicio nunca
    reconstruye—, así que con los dos emitiendo ``pipeline_stage`` un ``COUNT(*)``
    cuenta filas y no incidentes. La métrica correcta es
    ``COUNT(DISTINCT tx_id) WHERE pipeline_stage=9``, y solo existe si el runner
    puede nombrar una transacción de la que no sabe nada.

    ``None`` es un valor honesto y frecuente (uso directo, tests): significa "no
    hay transacción acá", no una clave faltante. Por eso los registros lo emiten
    igual en vez de omitir la clave — misma decisión que tomó el servicio al
    declarar su ``tx_id`` antes del preflight.
    """
    return pipeline_tx_id_var.get()


# S-4: gracia máxima para drenar los buffers residuales tras la salida normal del
# proceso. Si un nieto (p.ej. TexGen lanzado por DynDOLOD) heredó el pipe y
# sobrevive al padre, el write-end nunca cierra y `_drain` no vería EOF jamás; sin
# esta cota, el `gather` del path de éxito colgaría `_execute_process` para siempre.
_DRAIN_GRACE_SECONDS = 10.0

#: Switches de ruta cuyo dueño es ``DynDOLODConfig``: ``extra_args`` no puede
#: redefinirlos (ver ``DynDOLODRunner._extra_args_admisibles``).
#:
#: Es la MISMA lista que la tupla ``switches`` de ``_build_xedit_args``, escrita
#: dos veces: nada en el lenguaje las acopla. El acoplamiento lo impone un test —
#: ``test_letras_administradas_cubre_los_switches_que_emite_el_runner`` lee la
#: tupla por AST y exige igualdad literal contra estas letras. Sin ese ancla,
#: agregar un sexto switch administrado lo dejaba fuera del rechazo y la suite
#: quedaba verde: medido por mutación en la review de #462 (los tests de vector
#: solo se quejaban del ``len`` del argv, y actualizar ese ``len`` —lo que haría
#: cualquiera al agregar un switch— restauraba el verde con el hueco abierto).
_LETRAS_ADMINISTRADAS = "odmpt"

#: El prefijo admite ``/`` además de ``-``, y tolera whitespace líder. La RTL de
#: Delphi reconoce ``/switch`` en ``FindCmdLineSwitch`` y NO está verificado cuál
#: de las dos formas mira el parser de xEdit. La asimetría decide: rechazar un
#: ``/o:`` que el binario quizá ignore no le cuesta nada a ningún caller legítimo
#: —la doc usa ``-`` y nadie emite la otra forma—, mientras que dejar pasar uno
#: que sí honre pisa la raíz administrada, que es exactamente lo que esta regla
#: existe para impedir. Fail-closed sobre la duda, como el resto de
#: ``_extra_args_admisibles``.
_SWITCH_ADMINISTRADO = re.compile(rf"^\s*[-/][{_LETRAS_ADMINISTRADAS}]:", re.IGNORECASE)

#: Game modes de xEdit, sin el prefijo. También los administra ``DynDOLODConfig``
#: (campo tipado ``game_mode``, decisión única en ``__post_init__``), así que
#: ``extra_args`` tampoco puede introducir un segundo game mode.
#:
#: Por qué importa más que los switches de ruta: el game mode es un switch SUELTO,
#: sin ``:``, así que la regla de ``_SWITCH_ADMINISTRADO`` no lo veía. Y un segundo
#: game mode no desvía una ruta — cambia el juego entero: `-tes5` manda a DynDOLOD
#: a buscar la clave de registro de Skyrim LE y morir con
#: ``Fatal: Could not determine ... installation path`` (el error del rig del
#: 2026-08-05), y `-tes5vr` contra un rig SSE ejecuta con las definiciones
#: equivocadas. El parser de xEdit no documenta cuál gana si aparecen dos.
#:
#: Procedencia, sin sobre-declarar: los DOS que Sky-Claw puede emitir (``sse``,
#: ``tes5vr``) NO están escritos acá — los deriva por AST
#: ``test_game_modes_administrados_cubre_los_que_emite_el_runner``, que exige que
#: estén contenidos en este conjunto. El resto son los game modes que documenta
#: xEdit; la lista puede quedar incompleta si xEdit agrega uno, y eso no la
#: invalida: es fail-closed sobre los conocidos, no una prueba de exhaustividad.
_GAME_MODES_ADMINISTRADOS = frozenset(
    {
        "sse",
        "tes5vr",
        "tes5",
        "tes4",
        "fo3",
        "fnv",
        "fo4",
        "fo4vr",
        "fo76",
        "enderal",
        "enderalse",
    }
)

#: Mismo prefijo tolerante que ``_SWITCH_ADMINISTRADO`` (``-``/``/``, whitespace
#: líder) y sin ``:``: el game mode es un switch suelto.
_GAME_MODE_SUELTO = re.compile(r"^\s*[-/]([A-Za-z0-9]+)\s*$")

# Firma de un candidato sin artefacto. Distinto de ``None`` (= no se pudo
# sondear) a propósito: colapsar los dos casos hace fail-open el gate de
# frescura, porque "no lo pude mirar antes" pasa a leerse como "no había nada".
# Tupla vacía: ninguna firma real lo es.
_SIN_ARTEFACTO: tuple[float | int, ...] = ()


# =============================================================================
# EXCEPTIONS
# =============================================================================


class DynDOLODExecutionError(Exception):
    """Base exception for DynDOLOD execution errors."""

    def __init__(
        self,
        message: str,
        return_code: int | None = None,
        stderr: str | None = None,
    ) -> None:
        super().__init__(message)
        self.return_code = return_code
        self.stderr = stderr


class DynDOLODTimeoutError(DynDOLODExecutionError):
    """Raised when DynDOLOD/TexGen execution exceeds timeout."""

    def __init__(self, timeout_seconds: int, tool_name: str = "DynDOLOD") -> None:
        super().__init__(
            message=f"{tool_name} execution timed out after {timeout_seconds} seconds",
            return_code=None,
            stderr=None,
        )
        self.timeout_seconds = timeout_seconds
        self.tool_name = tool_name


class DynDOLODNotFoundError(DynDOLODExecutionError):
    """Raised when DynDOLOD/TexGen executable cannot be found."""

    def __init__(self, executable_path: pathlib.Path) -> None:
        super().__init__(
            message=f"Executable not found: {executable_path}",
            return_code=None,
            stderr=None,
        )
        self.executable_path = executable_path


class DynDOLODValidationError(DynDOLODExecutionError):
    """Raised when validation fails: output artifacts or caller-supplied argv."""

    def __init__(self, message: str, output_path: pathlib.Path | None = None) -> None:
        super().__init__(message=message, return_code=None, stderr=None)
        self.output_path = output_path


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass(frozen=True, slots=True)
class DynDOLODConfig:
    """Configuration for DynDOLOD/TexGen execution.

    Attributes:
        game_path: Ruta al directorio del juego (Skyrim SE/AE).
        mo2_path: Ruta al directorio de Mod Organizer 2.
        mo2_mods_path: Ruta a la carpeta mods de MO2.
        dyndolod_exe: Ruta al ejecutable de DynDOLOD.
        texgen_exe: Ruta al ejecutable de TexGen (opcional).
        data_dir: Carpeta Data del juego (default: ``game_path/Data``).
        ini_dir: Carpeta de los INI del juego (``My Games/Skyrim Special
            Edition``). Sin default derivado: la carpeta Documentos del rig está
            redirigida a OneDrive, así que derivarla a ciegas es frágil. Si es
            ``None`` el switch ``-m:`` no se emite y la herramienta resuelve su
            default por registro.
        plugins_file: Ruta a ``plugins.txt``. Idem: sin default derivado; si es
            ``None`` el switch ``-p:`` no se emite.
        output_root: Raíz administrada única donde la herramienta crea sus
            carpeta de salida (default: ``dyndolod_output_target(game_path)``,
            ver ``output_targets.py``).
        temp_dir: Carpeta temporal (default: ``tempfile.gettempdir()``).
        game_mode: Modo del juego (``"sse"``/``"tes5vr"``). Decisión ÚNICA y
            tipada; si es ``None`` se infiere una vez en ``__post_init__`` por el
            NOMBRE de la carpeta del juego (``Skyrim VR``/``SkyrimVR``). Los
            consumidores (argv y nombre del log) leen el campo, nunca la
            heurística — que vivía duplicada y, buscando ``"VR"`` como substring
            de la ruta entera, volcaba a ``-tes5vr`` cualquier SSE colgado de un
            directorio con esas letras (``CVR``, ``VRamDisk``).
        timeout_seconds: Timeout en segundos para la ejecución (default: 4 horas).
            En modo asistido el reloj del proceso cubre la ventana de interacción
            humana (el humano elige preset/worldspaces en la GUI); el default de
            4 h cubre ambos tiempos a propósito.
        heartbeat_interval: Segundos entre logs de heartbeat.
        preset: Nivel de calidad (Low, Medium, High) — metadato de la corrida
            (eventos, journal, preview). NO viaja en el argv: lo elige el humano
            en el asistente de la herramienta.
    """

    game_path: pathlib.Path
    mo2_path: pathlib.Path
    mo2_mods_path: pathlib.Path
    dyndolod_exe: pathlib.Path
    texgen_exe: pathlib.Path | None = None
    data_dir: pathlib.Path | None = None
    ini_dir: pathlib.Path | None = None
    plugins_file: pathlib.Path | None = None
    output_root: pathlib.Path | None = None
    temp_dir: pathlib.Path | None = None
    game_mode: Literal["sse", "tes5vr"] | None = None
    timeout_seconds: int = 14400  # 4 horas por defecto
    heartbeat_interval: int = 60  # Segundos entre logs de heartbeat
    preset: str = "Medium"  # Low, Medium, High

    def __post_init__(self) -> None:
        """Resuelve defaults derivables y valida que los paths requeridos existan.

        ``data_dir``/``output_root``/``temp_dir`` tienen default DERIVADO (no
        pueden ser ``default_factory``: dependen de ``game_path``). ``ini_dir`` y
        ``plugins_file`` NO se derivan a ciegas: la auto-resolución de la
        herramienta es frágil — en el rig (Documentos redirigida a OneDrive) una
        corrida sin ``-m:`` explícito muere con ``Fatal: Could not find ini``
        (spike 2026-08-05 con TexGen 2.98). Se pasan explícitos cuando el
        operador los configura; si faltan, el switch se omite y el preflight del
        servicio lo advierte.
        """
        object.__setattr__(self, "data_dir", self.data_dir if self.data_dir is not None else self.game_path / "Data")
        object.__setattr__(
            self,
            "output_root",
            self.output_root if self.output_root is not None else dyndolod_output_target(game=self.game_path),
        )
        object.__setattr__(
            self,
            "temp_dir",
            self.temp_dir if self.temp_dir is not None else pathlib.Path(tempfile.gettempdir()),
        )
        # Modo del juego: decisión ÚNICA y tipada. La heurística vivía duplicada
        # en _build_xedit_args y _modo — un fix en uno y no en el otro era el
        # defecto hermano en persona. Acá se resuelve UNA vez; el caller puede
        # pasarlo explícito (Literal, no substring).
        #
        # El default mira el NOMBRE de la carpeta del juego, no el string entero:
        # buscar "VR" como substring en toda la ruta volcaba a -tes5vr cualquier
        # instalación SSE colgada de un directorio con esas letras (C:\VRamDisk\…,
        # …\CVR\…). El modo equivocado no solo cambia el argv: _modo() pasa a
        # buscar el log en *_TES5VR_log.txt, así que el post-check se queda sin
        # su evidencia y degrada a artefacto-solo.
        #
        # Se compara por TOKENS, no por substring, y esta distinción se ganó en
        # tres iteraciones: el substring sobre la ruta entera volcaba a VR un SSE
        # bajo `C:\VRamDisk\`; exigir el nombre canónico `SkyrimVR` hacía lo
        # contrario (una instalación VR real en `Skyrim-VR` o `VR Skyrim` caía a
        # `sse` en silencio); y el substring sobre el NOMBRE seguía casando
        # `Skyrim CVR` o `Skyrim VRamDisk Edition` — el mismo error, más chico
        # (reviews adversariales #441). Partiendo por no-alfanuméricos, `vr` tiene
        # que ser un token ENTERO; `skyrimvr` cubre la forma sin separador. Ante un
        # nombre que no diga ninguna, el operador tiene el override `game_mode`.
        if self.game_mode is None:
            tokens = set(re.split(r"[^a-z0-9]+", self.game_path.name.casefold()))
            es_vr = bool(tokens & {"vr", "skyrimvr"})
            object.__setattr__(self, "game_mode", "tes5vr" if es_vr else "sse")
        if not self.game_path.exists():
            raise ValueError(f"Game path does not exist: {self.game_path}")
        if not self.dyndolod_exe.exists():
            raise DynDOLODNotFoundError(self.dyndolod_exe)


@dataclass(frozen=True, slots=True)
class _PostCheck:
    """Veredicto del post-check de una corrida asistida.

    Existe para que TODO el I/O del post-check —leer el log, parsearlo, resolver
    el staging y sondear el artefacto— cruce UNA sola frontera ``to_thread``, y
    para que el llamador async no tenga que tocar el disco para componer su
    resultado.
    """

    output_path: pathlib.Path | None
    artefacto: bool
    errors: list[str]
    warnings: list[str]


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """Result of a single tool execution (TexGen or DynDOLOD).

    Attributes:
        success: True si la ejecución fue exitosa.
        tool_name: Nombre de la herramienta ejecutada.
        return_code: Código de retorno del proceso.
        stdout: Salida estándar capturada.
        stderr: Salida de error capturada.
        output_path: Path al directorio de salida generado.
        errors: Lista de errores detectados en la salida.
        warnings: Lista de warnings detectados en la salida.
        duration_seconds: Duración de la ejecución en segundos.
    """

    success: bool
    tool_name: str
    return_code: int
    stdout: str
    stderr: str
    output_path: pathlib.Path | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class DynDOLODPipelineResult:
    """Complete result of TexGen + DynDOLOD pipeline.

    Attributes:
        success: True si todo el pipeline fue exitoso.
        texgen_result: Resultado de la ejecución de TexGen.
        dyndolod_result: Resultado de la ejecución de DynDOLOD.
        texgen_mod_path: Path al mod empaquetado de TexGen.
        dyndolod_mod_path: Path al mod empaquetado de DynDOLOD.
        errors: Lista de errores acumulados del pipeline.
    """

    success: bool
    texgen_result: ToolExecutionResult | None
    dyndolod_result: ToolExecutionResult | None
    texgen_mod_path: pathlib.Path | None = None
    dyndolod_mod_path: pathlib.Path | None = None
    errors: list[str] = field(default_factory=list)


# =============================================================================
# DYNDOLLOD RUNNER
# =============================================================================


class DynDOLODRunner:
    """
    Runner asíncrono para TexGen y DynDOLOD con empaquetado automático para MO2.

    DynDOLOD es una herramienta de generación de LODs que crea objetos distantes
    y texturas optimizadas para mejorar la visualización a larga distancia.

    Patrones de salida esperados:
    - TexGen: <TexGen_Output>/ - Contiene texturas LOD
    - DynDOLOD: <DynDOLOD_Output>/ - Contiene DynDOLOD.esp y assets

    Usage:
        config = DynDOLODConfig(
            game_path=Path("C:/Games/Skyrim Special Edition"),
            mo2_path=Path("C:/Modding/MO2"),
            mo2_mods_path=Path("C:/Modding/MO2/mods"),
            dyndolod_exe=Path("C:/Modding/DynDOLOD/DynDOLODx64.exe"),
            texgen_exe=Path("C:/Modding/DynDOLOD/TexGenx64.exe"),
        )

        runner = DynDOLODRunner(config)
        result = await runner.run_full_pipeline(preset="Medium")

        if result.success:
            print(f"TexGen mod: {result.texgen_mod_path}")
            print(f"DynDOLOD mod: {result.dyndolod_mod_path}")
    """

    # Patrones regex para el post-check por LOG (los binarios son apps GUI, PE
    # Subsystem 2: no escriben a stdout/stderr; la evidencia real de una corrida
    # vive en ``Logs/{Tool}_{modo}_log.txt``).
    _ERROR_PATTERN = re.compile(
        r"(?:ERROR|Error|FAIL|Exception|Critical|FATAL):\s*(.+?)(?:\n|$)",
        re.IGNORECASE | re.MULTILINE,
    )
    _WARNING_PATTERN = re.compile(
        r"(?:WARNING|Warning|WARN):\s*(.+?)(?:\n|$)",
        re.IGNORECASE | re.MULTILINE,
    )

    # Nombres de directorios de salida estándar
    TEXGEN_OUTPUT_NAME = "TexGen_Output"
    DYNDOLLOD_OUTPUT_NAME = "DynDOLOD_Output"
    TEXGEN_MOD_NAME = "TexGen Output"
    DYNDOLLOD_MOD_NAME = "DynDOLOD Output"

    def __init__(self, config: DynDOLODConfig) -> None:
        """
        Inicializa el runner de DynDOLOD.

        Args:
            config: Configuración con paths y timeouts.
        """
        self._config = config
        logger.info(
            "DynDOLODRunner inicializado: dyndolod_exe=%s, texgen_exe=%s, timeout=%ds",
            config.dyndolod_exe,
            config.texgen_exe or "N/A",
            config.timeout_seconds,
        )

    async def run_texgen(self, extra_args: list[str] | None = None) -> ToolExecutionResult:
        """
        Ejecuta TexGen (etapa 9, asistida).

        CLI verificado (línea de comandos): TexGenx64.exe -sse -o:"…" -d:"…"
        [-m:"…"] [-p:"…"] -t:"…". Las comillas son de la LÍNEA; en el argv que
        construye :meth:`_build_xedit_args` no van (ver :meth:`_switch_de_ruta`).
        La herramienta es una app GUI (PE Subsystem 2): no escribe a stdout; el
        éxito se decide por exit code + artefacto + log (ver post-check).

        Args:
            extra_args: Argumentos adicionales de línea de comandos.

        Returns:
            ToolExecutionResult con el resultado de la ejecución.

        Raises:
            DynDOLODNotFoundError: Si el ejecutable de TexGen no existe.
            DynDOLODTimeoutError: Si excede el timeout.
            DynDOLODValidationError: Si ``extra_args`` no cumple el contrato
                (ver :meth:`_extra_args_admisibles`). Se lanza ANTES de tocar el
                proceso; ``run_full_pipeline`` la atrapa como
                ``DynDOLODExecutionError`` y la reporta como corrida fallida.
        """
        if self._config.texgen_exe is None:
            # Sin registro, este camino era un fallo MUDO de la etapa 9: devuelve
            # un resultado fallido y `run_full_pipeline` computa success=False
            # (pidió TexGen y TexGen no anduvo), pero el único log del incidente
            # terminaba siendo el agregado del final del pipeline. Es alcanzable
            # de verdad — un rig sin TEXGEN_EXE configurado y el default
            # `run_texgen=True`— y es lo que hacía falsa la premisa de la exención
            # de ese registro agregado ("nunca es la única señal del incidente").
            logger.error(
                "TexGen (stage 9) no está configurado: no se puede ejecutar la etapa asistida",
                extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": _tx_id()},
            )
            return ToolExecutionResult(
                success=False,
                tool_name="TexGen",
                return_code=-1,
                stdout="",
                stderr="TexGen executable not configured",
                errors=["TexGen executable path not provided in configuration"],
            )

        logger.info("Iniciando TexGen para generación de texturas LOD")

        args = self._build_xedit_args(extra_args)

        # Firma del staging ANTES de lanzar: la frescura se decide comparando el
        # árbol consigo mismo (mtime contra mtime), no contra el reloj de pared.
        firmas_previas = await asyncio.to_thread(self._firmas_de_salida, "TexGen")

        try:
            execution_cwd = pathlib.Path.cwd()
            stdout, stderr, return_code, duration = await self._execute_process(
                executable=self._config.texgen_exe,
                args=args,
                tool_name="TexGen",
                cwd=execution_cwd,
            )
        except DynDOLODExecutionError:
            raise
        except (TimeoutError, OSError, RuntimeError) as e:
            logger.exception(
                "Error inesperado ejecutando TexGen: %s",
                e,
                extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": _tx_id()},
            )
            return ToolExecutionResult(
                success=False,
                tool_name="TexGen",
                return_code=-1,
                stdout="",
                stderr=str(e),
                errors=[str(e)],
            )

        post = await asyncio.to_thread(self._post_check, "TexGen", firmas_previas)
        errors, warnings, output_path = post.errors, post.warnings, post.output_path
        success = return_code == 0 and post.artefacto and not errors

        result = ToolExecutionResult(
            success=success,
            tool_name="TexGen",
            return_code=return_code,
            stdout=stdout,
            stderr=stderr,
            output_path=output_path,
            errors=errors,
            warnings=warnings,
            duration_seconds=duration,
        )

        if result.success:
            logger.info(
                "TexGen completado exitosamente en %.1fs: %s",
                duration,
                output_path,
            )
        else:
            # `subprocess_error_extra` es la forma canónica del repo para un fallo
            # de subproceso (misma que `loot/cli.py` y `xedit/runner.py`), y lo
            # que justifica que la etapa la emita el RUNNER y no solo el servicio:
            # el exit code y la evidencia terminal viven acá, y el handler del
            # servicio nunca los reconstruye — solo ve el mensaje agregado.
            # `stderr` llega vacío casi siempre y está bien: estos binarios son
            # apps GUI (PE Subsystem 2) que no escriben a stderr, y esa ausencia
            # es en sí misma parte de la evidencia. La causa real va en el mensaje,
            # parseada del log de la herramienta por el post-check.
            logger.error(
                "TexGen falló (code %d): %s",
                return_code,
                "; ".join(errors) if errors else "Unknown error",
                extra=subprocess_error_extra(
                    operation="run_texgen",
                    tool="TexGen",
                    exit_code=return_code,
                    stderr=stderr,
                    pipeline_stage=_ETAPA_DYNDOLOD,
                    tx_id=_tx_id(),
                ),
            )

        return result

    async def run_dyndolod(
        self,
        preset: str = "Medium",
        extra_args: list[str] | None = None,
    ) -> ToolExecutionResult:
        """
        Ejecuta DynDOLOD (etapa 9, asistida).

        CLI verificado (línea de comandos): DynDOLODx64.exe -sse -o:"…" -d:"…"
        [-m:"…"] [-p:"…"] -t:"…". Las comillas son de la LÍNEA; en el argv que
        construye :meth:`_build_xedit_args` no van (ver :meth:`_switch_de_ruta`).
        La herramienta es una app GUI (PE Subsystem 2): no escribe a stdout; el
        éxito se decide por exit code + artefacto + log (ver post-check).

        Args:
            preset: Nivel de calidad (Low, Medium, High) — METADATO de la corrida
                (viaja por eventos, journal y preview). NO va en el argv: el
                preset lo elige el humano en el asistente GUI.
            extra_args: Argumentos adicionales de línea de comandos.

        Returns:
            ToolExecutionResult con el resultado de la ejecución.

        Raises:
            DynDOLODNotFoundError: Si el ejecutable no existe.
            DynDOLODTimeoutError: Si excede el timeout.
            DynDOLODValidationError: Si ``extra_args`` no cumple el contrato
                (ver :meth:`_extra_args_admisibles`). Se lanza ANTES de tocar el
                proceso; ``run_full_pipeline`` la atrapa como
                ``DynDOLODExecutionError`` y la reporta como corrida fallida.
        """
        logger.info("Iniciando DynDOLOD con preset: %s", preset)

        args = self._build_xedit_args(extra_args)

        # Ver run_texgen: firma previa del staging para el gate de frescura.
        firmas_previas = await asyncio.to_thread(self._firmas_de_salida, "DynDOLOD")

        try:
            execution_cwd = pathlib.Path.cwd()
            stdout, stderr, return_code, duration = await self._execute_process(
                executable=self._config.dyndolod_exe,
                args=args,
                tool_name="DynDOLOD",
                cwd=execution_cwd,
            )
        except DynDOLODExecutionError:
            raise
        except (TimeoutError, OSError, RuntimeError) as e:
            logger.exception(
                "Error inesperado ejecutando DynDOLOD: %s",
                e,
                extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": _tx_id()},
            )
            return ToolExecutionResult(
                success=False,
                tool_name="DynDOLOD",
                return_code=-1,
                stdout="",
                stderr=str(e),
                errors=[str(e)],
            )

        post = await asyncio.to_thread(self._post_check, "DynDOLOD", firmas_previas)
        errors, warnings, output_path = post.errors, post.warnings, post.output_path
        success = return_code == 0 and post.artefacto and not errors

        result = ToolExecutionResult(
            success=success,
            tool_name="DynDOLOD",
            return_code=return_code,
            stdout=stdout,
            stderr=stderr,
            output_path=output_path,
            errors=errors,
            warnings=warnings,
            duration_seconds=duration,
        )

        if result.success:
            logger.info(
                "DynDOLOD completado exitosamente en %.1fs: %s",
                duration,
                output_path,
            )
        else:
            # Ver `run_texgen`: misma forma canónica y mismo motivo. Que los dos
            # lanzadores la emitan es el punto — dejar uno sin cubrir es el
            # defecto hermano que `../../AGENTS.md` documenta como dominante, y
            # acá los dos son literalmente el mismo par de líneas.
            logger.error(
                "DynDOLOD falló (code %d): %s",
                return_code,
                "; ".join(errors) if errors else "Unknown error",
                extra=subprocess_error_extra(
                    operation="run_dyndolod",
                    tool="DynDOLOD",
                    exit_code=return_code,
                    stderr=stderr,
                    pipeline_stage=_ETAPA_DYNDOLOD,
                    tx_id=_tx_id(),
                ),
            )

        return result

    async def _execute_process(
        self,
        executable: pathlib.Path,
        args: list[str],
        tool_name: str,
        timeout: int | None = None,
        cwd: pathlib.Path | None = None,
    ) -> tuple[str, str, int, float]:
        """
        Ejecuta un proceso con manejo de heartbeat para procesos largos.

        REQUISITOS:
        - Usar asyncio.create_subprocess_exec
        - Flag CREATE_NO_WINDOW (0x08000000) en Windows
        - Timeout configurable (default: 14400 segundos = 4 horas)
        - Sistema de Heartbeat: log cada 60 segundos mientras el proceso está vivo
        - Capturar stdout y stderr por separado

        Args:
            executable: Path al ejecutable.
            args: Argumentos del comando.
            tool_name: Nombre de la herramienta para logs.
            timeout: Timeout en segundos (usa config default si es None).
            cwd: Directorio de trabajo explícito del subproceso y raíz del staging.

        Returns:
            tuple[str, str, int, float]: (stdout, stderr, return_code, duration_seconds)

        Raises:
            DynDOLODNotFoundError: Si el ejecutable no existe.
            DynDOLODTimeoutError: Si se excede el timeout.
            DynDOLODExecutionError: Si el proceso falla críticamente.
        """
        effective_timeout = timeout if timeout is not None else self._config.timeout_seconds
        heartbeat_interval = self._config.heartbeat_interval

        # Windows: CREATE_NO_WINDOW to avoid console popups.
        process_cwd = cwd if cwd is not None else pathlib.Path.cwd()
        kwargs: dict[str, Any] = {"cwd": process_cwd}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

        # La línea real, no la lista: es la evidencia que faltaba cuando el argv
        # llegaba mangled (ver _linea_de_comando).
        logger.info(
            "Ejecutando %s: %s (timeout: %ds)",
            tool_name,
            self._linea_de_comando([str(executable), *args]),
            effective_timeout,
        )

        start_time = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                str(executable),
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
        except FileNotFoundError:
            raise DynDOLODNotFoundError(executable) from None
        except OSError as e:
            raise DynDOLODExecutionError(
                f"Failed to start {tool_name}: {e}",
                return_code=None,
                stderr=str(e),
            ) from e

        # U-07/U-02: meter el proceso (y sus descendientes) en un Job Object
        # kill-on-close. Cerrarlo (close_job, en TODA salida) aniquila cualquier
        # nieto que sobreviva —incluso reparentado tras la salida del padre—, el
        # hueco que kill_and_reap no cubre en la salida normal. No-op fuera de Windows.
        job = assign_kill_on_close_job(proc.pid)

        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        async def _drain(
            stream: asyncio.StreamReader,
            target: list[bytes],
        ) -> None:
            """Read stream until EOF, collecting chunks."""
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                target.append(chunk)

        async def _heartbeat_watcher() -> None:
            """Log a heartbeat every heartbeat_interval seconds."""
            while True:
                await asyncio.sleep(heartbeat_interval)
                elapsed = time.monotonic() - start_time
                logger.info(
                    "%s heartbeat: %.1fs elapsed, process still running",
                    tool_name,
                    elapsed,
                )

        drain_out = asyncio.create_task(_drain(proc.stdout, stdout_chunks))
        drain_err = asyncio.create_task(_drain(proc.stderr, stderr_chunks))
        heartbeat = asyncio.create_task(_heartbeat_watcher())

        try:
            await asyncio.wait_for(proc.wait(), timeout=effective_timeout)
        except asyncio.CancelledError:
            # Un shutdown externo debe matar el árbol antes de propagar la
            # cancelación: de otro modo DynDOLOD/TexGen continúa escribiendo
            # después de que la capa superior liberó su lock o hizo rollback.
            await kill_and_reap(proc)
            heartbeat.cancel()
            drain_out.cancel()
            drain_err.cancel()
            await asyncio.gather(heartbeat, drain_out, drain_err, return_exceptions=True)
            close_job(job)
            raise
        except TimeoutError:
            # Global timeout exceeded — kill process tree (evita nietos como
            # TexGen huérfanos) y cancela las tasks de monitoreo.
            await kill_and_reap(proc)
            heartbeat.cancel()
            drain_out.cancel()
            drain_err.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(heartbeat, drain_out, drain_err, return_exceptions=True)
            close_job(job)
            raise DynDOLODTimeoutError(effective_timeout, tool_name) from None
        except Exception as e:
            await kill_and_reap(proc)
            heartbeat.cancel()
            drain_out.cancel()
            drain_err.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(heartbeat, drain_out, drain_err, return_exceptions=True)
            close_job(job)
            raise DynDOLODExecutionError(
                f"Unexpected error during {tool_name} execution: {e}",
                return_code=proc.returncode,
                stderr=str(e),
            ) from e
        else:
            # Process exited normally — cancel heartbeat and wait for drains to finish.
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            # S-4: el proceso ya terminó; drenamos los buffers residuales pero con
            # una cota. Si un nieto heredó el pipe y sigue vivo, el EOF nunca llega
            # y sin timeout este gather colgaría para siempre (igual que el path de
            # timeout, que sí cancela los drains). Agotada la gracia, cancelamos los
            # drains y seguimos con el output parcial (el proceso ya reportó éxito).
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(drain_out, drain_err, return_exceptions=True),
                    timeout=_DRAIN_GRACE_SECONDS,
                )
            except TimeoutError:
                # SIN `pipeline_stage`: el proceso ya salió y su código de salida
                # ya está decidido; esto reporta que la CAPTURA quedó parcial y el
                # propio mensaje dice que se continúa. Etiquetarlo sumaría un
                # fallo de etapa 9 por cada corrida que salió con 0 y artefacto
                # fresco pero dejó un nieto con el pipe heredado.
                logger.warning(
                    "%s: los drains no cerraron en %.1fs tras la salida del proceso "
                    "(posible nieto con pipe heredado); se continúa con output parcial.",
                    tool_name,
                    _DRAIN_GRACE_SECONDS,
                    extra={"operation_type": "dyndolod_drenaje_incompleto", "tx_id": _tx_id()},
                )
                drain_out.cancel()
                drain_err.cancel()
                # gather(return_exceptions=True) sobre tasks ya canceladas captura
                # sus CancelledError como resultados (no relanza); sin `suppress`
                # una cancelación externa del caller propaga como corresponde.
                await asyncio.gather(drain_out, drain_err, return_exceptions=True)
                results = []
            for r in results:
                if isinstance(r, BaseException):
                    # Exento por el mismo motivo que el drain-timeout de arriba:
                    # es diagnóstico del andamiaje de captura, no el veredicto —
                    # que lo dan el exit code, el artefacto y el log.
                    logger.warning(
                        "%s drain task falló inesperadamente: %r",
                        tool_name,
                        r,
                        extra={"operation_type": "dyndolod_drenaje_fallido", "tx_id": _tx_id()},
                    )
            # U-07: salida normal. Cerrar el job mata el nieto que sobrevivió
            # heredando el pipe (el mismo que dispara el drain-timeout de arriba) —
            # ya no es alcanzable por PID porque su padre salió, pero el job lo retiene.
            close_job(job)

        duration = time.monotonic() - start_time
        stdout_text = b"".join(stdout_chunks).decode(errors="replace")
        stderr_text = b"".join(stderr_chunks).decode(errors="replace")

        logger.info(
            "%s finalizado: return_code=%d, duration=%.1fs",
            tool_name,
            proc.returncode if proc.returncode is not None else -1,
            duration,
        )

        return (
            stdout_text,
            stderr_text,
            proc.returncode if proc.returncode is not None else -1,
            duration,
        )

    async def _package_output_as_mod(
        self,
        output_path: pathlib.Path,
        mod_name: str,
    ) -> pathlib.Path:
        """
        Empaqueta la salida de una herramienta como un mod válido para MO2.

        Pasos:
        1. Crear directorio en self._config.mo2_mods_path / mod_name
        2. Copiar todo el contenido de output_path al nuevo directorio
        3. Generar meta.ini válido

        Args:
            output_path: Path al directorio de salida de la herramienta.
            mod_name: Nombre del mod a crear.

        Returns:
            pathlib.Path: Ruta al directorio del mod creado.

        Raises:
            DynDOLODValidationError: Si falla la creación del mod.
        """
        mod_path = self._config.mo2_mods_path / mod_name

        logger.info("Empaquetando mod: %s -> %s", output_path, mod_path)

        try:
            # Verificar que el directorio de salida existe
            if not output_path.exists():
                raise DynDOLODValidationError(
                    f"Output directory does not exist: {output_path}",
                    output_path=output_path,
                )

            # Verificar que tiene contenido
            if not any(output_path.iterdir()):
                raise DynDOLODValidationError(
                    f"Output directory is empty: {output_path}",
                    output_path=output_path,
                )

            # Fail-closed si el destino es un enlace: `mod_path` cuelga de
            # `mods/` del usuario, y el borrado de abajo destruiría el árbol al
            # que apunta en vez del output previo de DynDOLOD. Mismo criterio y
            # mismo orden que `grass_profile.build_config_mod` y
            # `vfs.delete_mod_files`.
            try:
                tipo_de_enlace = await asyncio.to_thread(link_kind_or_raise_with_retry, mod_path)
            except OSError as exc:
                raise DynDOLODValidationError(
                    f"No se pudo inspeccionar el mod de salida '{mod_name}' antes de limpiarlo: {exc}",
                    output_path=mod_path,
                ) from exc
            if tipo_de_enlace is not None:
                raise DynDOLODValidationError(
                    f"El mod de salida '{mod_name}' es un enlace ({tipo_de_enlace}), no un "
                    "directorio real: limpiarlo borraría el árbol al que apunta. Reemplazá "
                    "el enlace por una carpeta real para empaquetar la salida.",
                    output_path=mod_path,
                )

            def _empaquetar_sincrono() -> None:
                # Todo el cuerpo de abajo es I/O bloqueante — copiar la salida
                # de DynDOLOD mueve miles de archivos de LOD, texturas y
                # meshes. Un solo `to_thread` para toda la fase, no uno por
                # llamada: el mismo idioma que `snapshot_manager.py` usa para
                # sus fases de borrado/copia (`_limpiar`, `_calc_dir_size_and_remove`).
                # Correrlo en el hilo del event loop congelaba la UI de
                # NiceGUI y desconectaba WebSockets durante el empaquetado.
                if mod_path.exists():
                    logger.debug("Limpiando directorio existente: %s", mod_path)
                    # `limpiar_readonly`: el árbol es la salida de una corrida
                    # previa de DynDOLOD, y una herramienta externa de Windows
                    # puede dejar sus archivos con FILE_ATTRIBUTE_READONLY. Sin
                    # el flag, el borrado falla con PermissionError y el
                    # empaquetado aborta sobre su propia salida vieja.
                    rmtree_link_aware(mod_path, limpiar_readonly=True)

                mod_path.mkdir(parents=True, exist_ok=True)

                # Copiar contenido
                for item in output_path.iterdir():
                    src = item
                    dst = mod_path / item.name
                    if src.is_dir():
                        shutil.copytree(src, dst)
                    else:
                        shutil.copy2(src, dst)

                # Generar meta.ini
                self._generate_meta_ini(mod_path, mod_name)

            await asyncio.to_thread(_empaquetar_sincrono)

            logger.info("Mod empaquetado exitosamente: %s", mod_path)
            return mod_path

        except DynDOLODValidationError:
            raise
        except PermissionError as e:
            raise DynDOLODValidationError(
                f"Permission denied creating mod: {e}",
                output_path=mod_path,
            ) from e
        except OSError as e:
            raise DynDOLODValidationError(
                f"Failed to package mod: {e}",
                output_path=mod_path,
            ) from e

    async def run_full_pipeline(
        self,
        run_texgen: bool = True,
        preset: str = "Medium",
        texgen_args: list[str] | None = None,
        dyndolod_args: list[str] | None = None,
    ) -> DynDOLODPipelineResult:
        """
        Ejecuta el pipeline completo: TexGen → Empaquetado → DynDOLOD → Empaquetado.

        Flujo:
        1. Ejecutar TexGen (si run_texgen=True)
        2. Empaquetar TexGen_Output como "TexGen Output"
        3. Ejecutar DynDOLOD (después de que TexGen esté listo)
        4. Empaquetar DynDOLOD_Output como "DynDOLOD Output"

        Args:
            run_texgen: Si True, ejecuta TexGen antes de DynDOLOD.
            preset: Nivel de calidad para DynDOLOD (Low, Medium, High).
            texgen_args: Argumentos adicionales para TexGen.
            dyndolod_args: Argumentos adicionales para DynDOLOD.

        Returns:
            DynDOLODPipelineResult con resultados completos.
        """
        logger.info(
            "Iniciando pipeline DynDOLOD completo: run_texgen=%s, preset=%s",
            run_texgen,
            preset,
        )

        errors: list[str] = []
        texgen_result: ToolExecutionResult | None = None
        dyndolod_result: ToolExecutionResult | None = None
        texgen_mod_path: pathlib.Path | None = None
        dyndolod_mod_path: pathlib.Path | None = None

        # Paso 1: Ejecutar TexGen si está habilitado
        if run_texgen:
            try:
                texgen_result = await self.run_texgen(extra_args=texgen_args)

                if texgen_result.success and texgen_result.output_path:
                    # Empaquetar TexGen Output
                    try:
                        texgen_mod_path = await self._package_output_as_mod(
                            texgen_result.output_path,
                            self.TEXGEN_MOD_NAME,
                        )
                    except DynDOLODValidationError as e:
                        errors.append(f"Failed to package TexGen output: {e}")
                        # CON etapa: el mod de TexGen no llegó a `mods/`, así que
                        # MO2 no despliega sus texturas y la etapa no produjo su
                        # resultado. La fórmula de `success` de más abajo ahora lo
                        # mira (antes exigía solo que TexGen hubiera ANDADO, y este
                        # registro quedaba como única señal de una corrida que se
                        # reportaba exitosa), así que el agregado también se emite:
                        # dos registros del runner para un incidente, uno con la
                        # causa y otro con el resultado, que es la forma normal.
                        logger.error(
                            "Error empaquetando TexGen: %s",
                            e,
                            extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": _tx_id()},
                        )
                elif not texgen_result.success:
                    errors.extend(texgen_result.errors)

            except DynDOLODExecutionError as e:
                errors.append(f"TexGen execution failed: {e}")
                # Primer registro del incidente: `run_texgen` relanza esta familia
                # sin loguear (`except DynDOLODExecutionError: raise`) para no
                # duplicar, así que la etapa tiene que salir de acá.
                logger.error(
                    "Error ejecutando TexGen: %s",
                    e,
                    extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": _tx_id()},
                )
                texgen_result = ToolExecutionResult(
                    success=False,
                    tool_name="TexGen",
                    return_code=e.return_code or -1,
                    stdout="",
                    stderr=e.stderr or "",
                    errors=[str(e)],
                )

        # Paso 2: Ejecutar DynDOLOD
        # Nota: DynDOLOD puede ejecutarse sin TexGen si ya existe TexGen_Output
        try:
            dyndolod_result = await self.run_dyndolod(
                preset=preset,
                extra_args=dyndolod_args,
            )

            if dyndolod_result.success and dyndolod_result.output_path:
                # Empaquetar DynDOLOD Output
                try:
                    dyndolod_mod_path = await self._package_output_as_mod(
                        dyndolod_result.output_path,
                        self.DYNDOLLOD_MOD_NAME,
                    )
                except DynDOLODValidationError as e:
                    errors.append(f"Failed to package DynDOLOD output: {e}")
                    logger.error(
                        "Error empaquetando DynDOLOD: %s",
                        e,
                        extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": _tx_id()},
                    )
            elif not dyndolod_result.success:
                errors.extend(dyndolod_result.errors)

        except DynDOLODExecutionError as e:
            errors.append(f"DynDOLOD execution failed: {e}")
            # Ver el gemelo de TexGen: `run_dyndolod` relanza esta familia sin
            # loguear, así que la etapa sale de acá.
            logger.error(
                "Error ejecutando DynDOLOD: %s",
                e,
                extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": _tx_id()},
            )
            dyndolod_result = ToolExecutionResult(
                success=False,
                tool_name="DynDOLOD",
                return_code=e.return_code or -1,
                stdout="",
                stderr=e.stderr or "",
                errors=[str(e)],
            )

        # Determinar éxito general.
        #
        # Las DOS ramas exigen lo mismo: que la herramienta haya andado Y que su
        # mod se haya empaquetado. La rama de TexGen pedía solo lo primero, y esa
        # asimetría dentro de una sola expresión era un falso verde: DynDOLOD lee
        # las texturas de TexGen del staging crudo (`-o:`), no de `mods/`, así que
        # su corrida sale bien y el pipeline reportaba éxito mientras el mod nunca
        # llegaba a `mods/` — MO2 no lo despliega y el juego queda con los meshes
        # de LOD sin las texturas que les corresponden.
        #
        # Se corrige acá (y no en un PR aparte) porque es este PR el que vuelve la
        # contradicción OBSERVABLE: desde que el fallo del empaquetado lleva
        # `pipeline_stage=9`, un dashboard cuenta un fallo de etapa 9 para un
        # `tx_id` cuyo journal commiteó éxito — dos señales persistidas
        # contradiciéndose sobre la misma transacción (review Qodo, PR #471).
        success = (
            dyndolod_result is not None
            and dyndolod_result.success
            and dyndolod_mod_path is not None
            and (
                not run_texgen or (texgen_result is not None and texgen_result.success and texgen_mod_path is not None)
            )
        )

        result = DynDOLODPipelineResult(
            success=success,
            texgen_result=texgen_result,
            dyndolod_result=dyndolod_result,
            texgen_mod_path=texgen_mod_path,
            dyndolod_mod_path=dyndolod_mod_path,
            errors=errors,
        )

        if result.success:
            logger.info(
                "Pipeline DynDOLOD completado exitosamente: TexGen=%s, DynDOLOD=%s",
                texgen_mod_path,
                dyndolod_mod_path,
            )
        else:
            # SIN `pipeline_stage`, y es el registro más tentador de etiquetar del
            # archivo. Es el AGREGADO —el resultado del pipeline, no la causa— y
            # gemelo exacto de `dyndolod_pipeline_failed` (`_log_result_error`) en
            # el servicio: por construcción repite un incidente que un registro
            # más específico y CON etapa ya reportó antes en este mismo método
            # (fallo del lanzador, del empaquetado, o TexGen sin configurar).
            # Etiquetarlo sumaba una fila más por incidente sobre las dos que ya
            # cuestan las dos capas. Su premisa —nunca es la única señal— la
            # verifican dos tests, uno textual y uno de comportamiento
            # (`test_el_registro_agregado_del_runner_siempre_tiene_companero_con_etapa`
            # y `test_texgen_sin_configurar_emite_un_registro_de_etapa_9`).
            logger.error(
                "Pipeline DynDOLOD falló: %s",
                "; ".join(errors) if errors else "Unknown error",
                extra={"operation_type": "dyndolod_runner_pipeline_failed", "tx_id": _tx_id()},
            )

        return result

    # =========================================================================
    # Métodos Auxiliares
    # =========================================================================

    def _build_xedit_args(self, extra_args: list[str] | None) -> list[str]:
        """Construye el argv de TexGen/DynDOLOD contra el contrato verificado.

        Fuente: ``dyndolod.info/Help/Command-Line-Argument`` (doc oficial),
        ``Docs/DynDOLOD-Shortcut.txt`` del paquete y el parser que ambos binarios
        heredan de xEdit (``xeInit.pas``: ``-T:``/``-P:`` con dos puntos y valor).
        El vector viejo (``-game SSE``, ``-p <preset>``, ``-t`` pelado,
        ``--expert``) no existe: se ignora en silencio. ``--expert`` no es un
        argumento (se activa con ``Expert=1`` en el INI) y el preset lo elige el
        humano en el asistente GUI — la etapa 9 es asistida.

        Un ÚNICO helper para los dos ejecutables: comparten el parser; que fueran
        dos funciones distintas es exactamente cómo un fix aterriza en uno y no
        en el otro (el defecto hermano dominante de este repo).

        Args:
            extra_args: Argumentos adicionales (API pública existente).

        Returns:
            Lista de argumentos para create_subprocess_exec.
        """
        # Game mode: switch suelto, sin valor, desde el campo tipado de la config
        # (decisión única en __post_init__). Sin él DynDOLOD arranca en modo
        # -tes5 (Skyrim LE), busca la clave de registro de LE y muere con
        # "Fatal: Could not determine ... installation path" — el error del rig.
        game_mode = "-tes5vr" if self._config.game_mode == "tes5vr" else "-sse"
        args: list[str] = [game_mode]

        # Los cinco -X: de la doc, un elemento de argv por switch: dos puntos,
        # directorios con \ final y SIN comillas (las pone list2cmdline; ver
        # _switch_de_ruta). -o:/-d:/-t: siempre (tienen default derivado);
        # -m:/-p: solo explícitos.
        switches = (
            ("o", self._config.output_root, True),
            ("d", self._config.data_dir, True),
            ("m", self._config.ini_dir, True),
            ("p", self._config.plugins_file, False),
            ("t", self._config.temp_dir, True),
        )
        for letra, ruta, es_directorio in switches:
            if ruta is not None:
                args.append(self._switch_de_ruta(letra, ruta, directorio=es_directorio))

        # SIN guard de truthiness: `if extra_args:` mandaba a `""`, `{}`, `0`,
        # `False` y `()` por el camino de "no hay argumentos" en vez del de
        # validación, o sea que la regla (1) no los veía (hallazgo de CodeRabbit
        # en #462). La ausencia se expresa con `None`, y el que decide es
        # `_extra_args_admisibles` — no la truthiness del valor.
        args.extend(self._extra_args_admisibles(extra_args))

        logger.debug("DynDOLOD/TexGen CLI args: %s", self._linea_de_comando(args))
        return args

    @staticmethod
    def _extra_args_admisibles(extra_args: object) -> list[str]:
        """Los ``extra_args`` del caller, o ``DynDOLODValidationError``. Fail-closed.

        **La ausencia se expresa con ``None``, no con "algo falsy".** ``None`` →
        ``[]``; ``[]`` → ``[]``; una ``list[str]`` no vacía pasa por las cuatro
        reglas; **cualquier otra cosa se rechaza, sea falsy o no**. La distinción
        importa porque antes el llamador hacía ``if extra_args:`` y mandaba ``""``,
        ``{}``, ``0``, ``False`` y ``()`` por el camino de "no hay argumentos": la
        regla (1) no llegaba a verlos y un payload con el tipo equivocado se leía
        como "el operador no pidió extras" (hallazgo de CodeRabbit en #462).
        Colapsar esos valores contra ``[]`` es la misma clase de error que despojar
        comillas en silencio — esconde el bug del caller en vez de reportarlo.

        Cuatro reglas, y ninguna normaliza: **rechazan**.

        1. **Tiene que ser ``list[str]`` (o ``None``).** El valor entra por payload
           y los type hints no validan datos en runtime:
           ``GenerateLodsStrategy._filter_payload`` filtra la CLAVE
           (``texgen_args``/``dyndolod_args`` están en ``_VALID_LOD_KEYS``) sin
           mirar el tipo ni el valor. Un ``str`` suelto se extendería **carácter
           por carácter** sobre el argv.
        2. **Ningún elemento puede traer comillas propias.** Arreglar
           :meth:`_switch_de_ruta` y dejar esta puerta abierta es el defecto hermano
           del repo en su forma pura: ``extra_args`` se extendía verbatim, así que un
           ``-o:"C:\\...\\"`` que entre por acá reproduce el
           ``Can not create path C:\\C:\\...`` sin pasar por el helper.
        3. **Ningún elemento puede redefinir un switch administrado**
           (``-o:``/``-d:``/``-m:``/``-p:``/``-t:``, case-insensitive, y también en
           sus formas ``/o:`` y con whitespace líder — ver ``_SWITCH_ADMINISTRADO``).
           Esos cinco tienen dueño: :class:`DynDOLODConfig`. Que el payload los
           re-suministre crea dos fuentes de verdad y rompe las premisas del
           staging, del post-check y del rollback — y el parser de xEdit no
           documenta cuál gana si el switch aparece dos veces, así que el resultado
           sería indeterminado además de ambiguo.

        4. **Ningún elemento puede introducir un segundo game mode**
           (``-sse``/``-tes5``/``-tes5vr``/… — ver ``_GAME_MODES_ADMINISTRADOS``).
           Lo administra :class:`DynDOLODConfig` por campo tipado, resuelto una
           sola vez en ``__post_init__``. Es un switch SUELTO, sin ``:``, así que
           la regla (3) no lo alcanzaba; y su daño es mayor que el de una ruta
           desviada: ``-tes5`` manda la corrida a buscar la instalación de Skyrim
           LE y muere con ``Could not determine ... installation path`` (el error
           del rig del 2026-08-05), ``-tes5vr`` contra un rig SSE corre con las
           definiciones equivocadas.

        Las (3) y (4) son las correcciones que pidieron las reviews del PR #462
        sobre la (2): con solo la (2), un ``-o:C:\\otra\\ruta\\`` sin comillas
        pasaba y pisaba la raíz administrada, y un ``-tes5`` pelado pasaba y
        cambiaba el juego. `_switch_de_ruta` sigue siendo el ÚNICO constructor de
        los switches de ruta que Sky-Claw administra, y ``_build_xedit_args`` el
        único emisor del game mode.

        Se valida en :meth:`_build_xedit_args` porque es la pieza ÚNICA que los dos
        ejecutables atraviesan: puesta en un solo lanzador, el otro queda sin cubrir.
        ``run_full_pipeline`` ya traduce ``DynDOLODExecutionError`` a un resultado
        fallido, así que esto degrada a rojo reportado, no a excepción sin manejar.

        Sigue fuera de alcance agregar switches ajenos del parser de xEdit
        (``-B:``, ``-C:``, ``-D:``, ``-S:``…): son legítimos como extensión y no
        colisionan con nada que Sky-Claw administre.

        El nombre evita a propósito el prefijo ``_valida``: ese prefijo declara
        pertenencia a la familia del post-check (``test_dyndolod_service.py``,
        "métodos que sondean el disco para dar el veredicto"), y esto no toca disco
        ni decide veredicto. El ancla congelada lo detectó al primer intento.
        """
        # `None` es la ÚNICA forma de decir "sin argumentos extra". Se chequea por
        # identidad y no por truthiness justamente para que `""`/`{}`/`0`/`False`/`()`
        # caigan en el rechazo de abajo en vez de colarse como lista vacía.
        if extra_args is None:
            return []

        if not isinstance(extra_args, list) or not all(isinstance(a, str) for a in extra_args):
            raise DynDOLODValidationError(
                "extra_args tiene que ser list[str] o None: viene de payload y los type hints no "
                "validan en runtime (un str suelto se extendería carácter por carácter). La ausencia "
                "se expresa con None — un valor falsy de otro tipo es un bug del caller, no una lista "
                f"vacía. Recibido: {extra_args!r}"
            )

        con_comillas = [a for a in extra_args if '"' in a]
        if con_comillas:
            raise DynDOLODValidationError(
                "extra_args no puede contener comillas: en un elemento de argv pasan a ser "
                "parte del valor y DynDOLOD muere con 'Can not create path C:\\C:\\...'. "
                f"Pasá la ruta sin comillas. Ofensores: {con_comillas}"
            )

        reservados = [a for a in extra_args if _SWITCH_ADMINISTRADO.match(a)]
        if reservados:
            raise DynDOLODValidationError(
                "extra_args no puede redefinir los switches de ruta que administra DynDOLODConfig "
                "(-o:/-d:/-m:/-p:/-t:): serían dos fuentes de verdad y romperían las premisas del "
                f"staging, el post-check y el rollback. Ofensores: {reservados}"
            )

        modos = [
            a
            for a in extra_args
            if (m := _GAME_MODE_SUELTO.match(a)) and m.group(1).lower() in _GAME_MODES_ADMINISTRADOS
        ]
        if modos:
            raise DynDOLODValidationError(
                "extra_args no puede introducir un segundo game mode: lo administra DynDOLODConfig "
                "(campo `game_mode`, resuelto una sola vez en __post_init__). Un game mode de más no "
                "desvía una ruta, cambia el juego — `-tes5` manda la corrida a buscar la instalación "
                "de Skyrim LE y muere con 'Could not determine installation path', y el parser de "
                f"xEdit no documenta cuál gana si aparecen dos. Ofensores: {modos}"
            )
        return extra_args

    @staticmethod
    def _linea_de_comando(args: list[str]) -> str:
        """Los ``args`` tal como los va a recibir el proceso, para los logs.

        En Windows se serializa con ``subprocess.list2cmdline`` —la misma función
        que usa ``create_subprocess_exec``— en vez de ``" ".join``. No es cosmético:
        el bug de las comillas de :meth:`_switch_de_ruta` era invisible porque los
        dos sitios que loguean el argv mostraban la LISTA, no la línea real, así
        que el ``-o:\\"C:\\...`` mangled nunca apareció en ningún log. Fuera de
        Windows ``list2cmdline`` no aplica (aplica reglas de quoting de MS) y el
        join simple alcanza: ahí ``create_subprocess_exec`` pasa el argv directo.
        """
        if sys.platform == "win32":
            return subprocess.list2cmdline(args)
        return " ".join(args)

    @staticmethod
    def _switch_de_ruta(letra: str, ruta: pathlib.Path, *, directorio: bool) -> str:
        """Formatea ``-X:<ruta>`` como ELEMENTO DE ARGV: dos puntos, ``\\`` final en
        directorios (``-p:`` es un archivo: sin ``\\``) y **sin comillas**.

        Sin los dos puntos el parser de xEdit ignora el switch en silencio.

        **Las comillas de la doc pertenecen a la línea de comandos, no al valor.**
        ``dyndolod.info/Help/Command-Line-Argument`` escribe ``-o:"c:\\Output\\"``
        porque documenta lo que se tipea en un ``.lnk`` o en el campo *Arguments*
        de MO2: ahí las comillas las consume el parser de Windows y nunca llegan
        al programa. Embebidas en el elemento de la lista pasan a ser parte del
        VALOR — ``create_subprocess_exec`` serializa con
        ``subprocess.list2cmdline``, que escapa la comilla interna como ``\\"``, y
        el programa recibe ``-o:"C:\\...\\"`` con las comillas literales adentro.
        ``"C:\\...`` no es una ruta absoluta para DynDOLOD, que le antepone el
        drive actual y muere con ``Can not create path C:\\C:\\...``. Medido en
        rig el 2026-08-10: con este defecto **ninguna** corrida de TexGen ni de
        DynDOLOD podía arrancar (2/2 binarios), incluso con el entorno correcto.

        Quitar el ``\\`` final NO arregla eso: ``-p:"archivo"`` no lleva backslash
        y llega igual con las comillas adentro. Y además contradice al binario,
        que documenta *"All path parameters must be specified with trailing
        backslash"*. Lo que sobra son las comillas: ``list2cmdline`` ya cita el
        argumento cuando hace falta.

        **Pero el fix está verificado solo para rutas SIN espacios.** Cuando el
        argumento trae espacios, ``list2cmdline`` lo cita y duplica el backslash
        final para que no escape su propia comilla de cierre: correcto bajo las
        reglas de la CRT, y **no** bajo las de Delphi, que nunca trata el backslash
        como escape. Medido: el binario recibiría ``-d:...\\Data\\\\`` en vez de
        ``-d:...\\Data\\``. Y esa es la rama que corre SIEMPRE en un rig real —el
        juego vive en ``Skyrim Special Edition`` y los INI bajo ``My Games``—, así
        que el caso sin espacios es el sintético. Si el binario no tolera el
        separador duplicado, el síntoma ``Can not create path`` sigue en producción.
        **Pendiente de T5**, que lo decide en una corrida porque la herramienta ecoa
        la ruta parseada en su log (``Using Output Path:``): dejar el duplicado si lo
        tolera, omitir el ``\\`` final, o línea verbatim. No lo damos por resuelto.

        Anclas, y su alcance exacto — los tests que solo miraban el string que
        Sky-Claw produce congelaron el defecto en vez de atajarlo:

        * ``test_dyndolod_argv_sin_espacios_sobrevive_los_dos_parsers``: con rutas
          sin espacios ``list2cmdline`` no cita, y el parser de la CRT y el de
          Delphi ven ambos exactamente este argv. Verificación real.
        * ``test_dyndolod_argv_con_espacios_diverge_entre_crt_y_delphi``: con
          espacios ``list2cmdline`` cita y **duplica el backslash final** (regla de
          la CRT que Delphi no implementa), así que el binario ve un ``\\`` de más.
          **Pendiente de T5**, y es la rama que corre siempre en un rig real
          (``Skyrim Special Edition``, ``My Games``). Ningún test afirma que ese
          vector sea correcto: se ancla la divergencia medida.
        """
        texto = str(ruta)
        if directorio and not texto.endswith("\\"):
            texto += "\\"
        return f"-{letra}:{texto}"

    def _modo(self) -> str:
        """Modo del juego para el nombre del log (``DynDOLOD_SSE_log.txt``/``_TES5VR``).

        Lee el campo tipado de la config — nunca la heurística de ruta, que
        quedó resuelta en un único punto (``__post_init__``).
        """
        return "TES5VR" if self._config.game_mode == "tes5vr" else "SSE"

    def _candidatos_de_salida(self, tool: str) -> list[pathlib.Path]:
        """Dónde puede haber aterrizado la salida de ``tool``, en orden de preferencia.

        Fuente ÚNICA de la lista: la resolución de salida (``_find_*_output``) y
        el post-check tienen que mirar el mismo conjunto. Que cada uno tuviera el
        suyo es cómo se llega a que el preflight opine sobre un lugar donde el
        tool no escribe.

        TexGen lleva UN solo candidato a propósito (review de #440): su salida son
        texturas, sin artefacto con nombre propio que gatear, así que aceptar la
        raíz como fallback dejaría pasar el ``DynDOLOD_Output`` de una corrida
        previa como si fuera salida de TexGen. DynDOLOD sí puede caer en la raíz
        (interpretación B de ``-o:``) porque su gate exige ``DynDOLOD.esp``.
        """
        root = self._config.output_root
        if root is None:
            return []
        if tool == "TexGen":
            return [root / self.TEXGEN_OUTPUT_NAME]
        return [root / self.DYNDOLLOD_OUTPUT_NAME, root]

    def _firmas_de_salida(self, tool: str) -> dict[pathlib.Path, tuple[float | int, ...] | None]:
        """Firma de cada candidato ANTES de lanzar, para comparar contra la de después.

        La frescura se decide comparando **mtime contra mtime**, no mtime contra
        el reloj de pared. Un umbral temporal necesita una holgura por la
        granularidad del filesystem (FAT32 redondea a 2 s) y esa holgura es
        exactamente una ventana por la que un staging previo pasa por fresco
        (review CodeRabbit, PR #441). Comparando el artefacto consigo mismo no
        hace falta holgura alguna: los dos valores salen del mismo reloj y del
        mismo filesystem.
        """
        return {candidato: self._firma_de_veredicto(tool, candidato) for candidato in self._candidatos_de_salida(tool)}

    @staticmethod
    def _firma_de_veredicto(tool: str, directorio: pathlib.Path) -> tuple[float | int, ...] | None:
        """Firma del artefacto que DECIDE el veredicto: mtime **y tamaño**.

        Tres resultados distintos, y la distinción es el gate: el mtime si se
        pudo leer, :data:`_SIN_ARTEFACTO` si no hay artefacto, y ``None`` si
        **no se pudo sondear**. Colapsar los dos últimos en un mismo centinela
        era fail-OPEN: si la firma previa fallaba por un error transitorio
        (permisos, antivirus, volumen de red) sobre un artefacto que SÍ existía,
        el post-check leía el ``.esp`` rancio, lo veía distinto del centinela y
        lo daba por fresco — el falso verde entero, reconstituido por un
        ``except`` (review adversarial, PR #441).

        **Del artefacto, no del árbol.** Firmar el árbol entero deja pasar una
        corrida abortada: DynDOLOD recrea directorios y sobrescribe LODs
        parcialmente, el operador cierra la GUI antes de que se regenere el
        plugin, el proceso sale con 0 y sin línea de error — y el árbol "cambió",
        así que un ``DynDOLOD.esp`` de la corrida ANTERIOR pasaba por veredicto
        fresco y se empaquetaba un árbol a medio generar (review adversarial,
        PR #441). El gate exige ``DynDOLOD.esp``, así que es ESE archivo el que
        tiene que ser de esta corrida.

        **Va el tamaño además del mtime** porque el mtime solo no basta para ver
        un cambio: el reloj de archivos de Windows avanza de a ~15 ms, así que una
        reescritura dentro del mismo tick que la firma previa deja el mtime
        idéntico y el gate rechaza una corrida real (se cayó así en CI, Windows
        py3.12). En una corrida de verdad —30+ min— el mtime sí cambia, pero un
        gate que depende de que dos escrituras caigan en ticks distintos es un
        falso rojo esperando el rig equivocado.

        TexGen no tiene artefacto con nombre propio (su salida son texturas): se
        firma el árbol agregado (cantidad de archivos, mtime máximo, bytes
        totales), recorrido SIN atravesar enlaces. Que sean archivos y no
        directorios importa — recrear una carpeta no es haber generado texturas.

        **Premisa NO VERIFICADA contra el binario** (review adversarial #441): que
        el ``.esp`` haya cambiado equivale a "corrida completa" solo si DynDOLOD
        persiste el plugin AL FINAL. Si lo escribiera en una fase temprana, una
        corrida que el operador cierra después de ese punto —exit 0, sin log
        flusheado— pasaría el gate con el árbol a medio generar. Nadie confirmó el
        orden de escritura real; queda declarado acá en vez de asumido en silencio.
        Cerrarlo pide una marca de completitud, y la única disponible es el log —
        que puede faltar por diseño (ver ``_leer_log``), así que exigirlo
        convertiría en rojas corridas legítimas. Se decide con el rig, no acá.
        Limitación conocida y no cerrada: sin marcador de completitud, una corrida
        de TexGen abortada que alcanzó a escribir algunas texturas sigue contando
        como fresca. El log es la única evidencia de completitud disponible y
        puede faltar.
        """
        if tool != "TexGen":
            try:
                st = (directorio / "DynDOLOD.esp").stat()
            except FileNotFoundError:
                return _SIN_ARTEFACTO
            except OSError as e:
                # SONDA, no veredicto: devuelve None y el post-check lo trata
                # fail-closed, convirtiéndolo en un error que `run_dyndolod`
                # reporta CON etapa. Acá etiquetar duplicaría ese incidente.
                logger.warning(
                    "No se pudo sondear el artefacto de %s: %s",
                    directorio,
                    e,
                    extra={"operation_type": "dyndolod_sondeo_de_artefacto_fallido", "tx_id": _tx_id()},
                )
                return None
            return (st.st_mtime, st.st_size)

        archivos = 0
        ultimo_mtime = -1.0
        bytes_totales = 0
        try:
            # `iter_archivos_propios` y no `rglob`: `rglob` frena en un symlink pero
            # NO en un junction de Windows, así que entraría a un árbol ajeno y
            # contaría sus archivos como salida de TexGen. La contraparte que borra
            # acá es `DirectoryRollback` (link-aware), y medir con una política y
            # borrar con otra es cómo la cuenta y el efecto divergen — mismo motivo
            # por el que `bodyslide_runner` mide link-aware. Fail-closed: un hijo
            # ilegible levanta OSError y lo captura el `except` de abajo.
            for _, identidad in iter_archivos_propios(directorio):
                archivos += 1
                ultimo_mtime = max(ultimo_mtime, identidad.st_mtime)
                bytes_totales += identidad.st_size
        except OSError as e:
            # Misma sonda que arriba para la firma agregada del árbol de TexGen.
            logger.warning(
                "No se pudo firmar el staging %s: %s",
                directorio,
                e,
                extra={"operation_type": "dyndolod_firma_de_staging_fallida", "tx_id": _tx_id()},
            )
            return None
        if archivos == 0:
            return _SIN_ARTEFACTO
        return (archivos, ultimo_mtime, bytes_totales)

    def _post_check(
        self,
        tool: str,
        firmas_previas: dict[pathlib.Path, tuple[float | int, ...] | None],
    ) -> _PostCheck:
        """Veredicto de la corrida: log + artefacto + frescura. TODO el I/O, acá.

        **Es una función síncrona a propósito, y sus llamadores la envuelven en
        un único ``asyncio.to_thread``.** El post-check toca disco tres veces
        (leer el log, resolver el staging, recorrerlo) y el log de DynDOLOD llega
        a decenas de MB en sesiones largas: hacerlo en el hilo del event loop
        congela la UI de NiceGUI y retrasa el bus de eventos (P2 de
        ``coding_conventions.md``). Una sola frontera para toda la fase, no un
        ``to_thread`` por llamada — el mismo idioma que ``_empaquetar_sincrono``
        usa en ``_package_output_as_mod``.

        Evalúa TODOS los candidatos y se queda con el primero que tiene artefacto
        válido **y** fresco. Elegir el path primero y validarlo después descartaba
        una corrida buena cuando los dos candidatos coexisten: con un
        ``DynDOLOD_Output`` viejo en disco y el ``DynDOLOD.esp`` nuevo escrito
        directo en la raíz, la resolución se quedaba con el viejo y el veredicto
        salía rojo sobre una salida real (review CodeRabbit, PR #441).

        Args:
            tool: ``"TexGen"`` o ``"DynDOLOD"``.
            firmas_previas: salida de :meth:`_firmas_de_salida` tomada ANTES de
                lanzar el proceso.

        Returns:
            :class:`_PostCheck` con el staging elegido, el veredicto de artefacto
            y los errores/warnings del log.
        """
        texto = self._leer_log(tool)
        errors, warnings = self._parse_log(texto) if texto is not None else ([], [])

        candidatos = self._candidatos_de_salida(tool)
        if not candidatos:
            return _PostCheck(output_path=None, artefacto=False, errors=errors, warnings=warnings)

        hubo_artefacto_rancio = False
        no_verificable = False
        for candidato in candidatos:
            if not self._tiene_artefacto(tool, candidato):
                continue
            # Frescura: el artefacto tiene que ser de ESTA corrida. Sin esto, un
            # staging que dejó la corrida anterior satisface el gate igual, y una
            # app GUI que el operador cerró a mitad sale con código 0 sin escribir
            # nada ni dejar línea de error — el falso verde se reconstituye entero.
            previa = firmas_previas.get(candidato)
            actual = self._firma_de_veredicto(tool, candidato)
            if previa is None or actual is None:
                # No se pudo sondear alguna de las dos puntas: no se puede AFIRMAR
                # que el artefacto sea de esta corrida. Fail-closed — dar por
                # fresco lo no verificable es cómo vuelve el falso verde.
                no_verificable = True
                continue
            # Desigualdad, no incremento: el criterio real es "el artefacto cambió",
            # y el mtime no siempre crece. Un snapshot restaurado, una copia con
            # timestamps preservados (`copy2`, extracción de un archivo) o un reloj
            # corrido dejan un .esp con fecha adelantada que una corrida real nunca
            # superaría — falso ROJO sobre una salida buena (review CodeRabbit #441).
            if actual != previa:
                return _PostCheck(output_path=candidato, artefacto=True, errors=errors, warnings=warnings)
            hubo_artefacto_rancio = True

        if no_verificable:
            errors.append(
                f"No se pudo verificar si el artefacto de {tool} es de esta corrida "
                "(el staging no se pudo sondear). No se reporta éxito sobre un estado indeterminado."
            )
        elif hubo_artefacto_rancio:
            errors.append(
                f"El artefacto de {tool} no se regeneró durante la corrida: "
                "es la salida de una corrida anterior, no de ésta."
            )

        # Ninguno pasó: se reporta el candidato primario para que el operador
        # sepa dónde se buscó.
        return _PostCheck(output_path=candidatos[0], artefacto=False, errors=errors, warnings=warnings)

    def _tiene_artefacto(self, tool: str, candidato: pathlib.Path) -> bool:
        """¿``candidato`` contiene una salida válida de ``tool``?

        TexGen no tiene un artefacto con nombre propio que gatear (su salida son
        texturas): el criterio es "existe y no está vacío". DynDOLOD exige
        ``DynDOLOD.esp`` como archivo (U-06).
        """
        if tool != "TexGen":
            return self._validar_salida_dyndolod(candidato)
        try:
            return candidato.exists() and any(candidato.iterdir())
        except OSError as e:
            # Sonda sobre UN candidato: devuelve False y el veredicto lo emite
            # `run_texgen` con la etapa.
            logger.warning(
                "No se pudo sondear el staging de TexGen (%s): %s",
                candidato,
                e,
                extra={"operation_type": "dyndolod_sondeo_de_staging_fallido", "tx_id": _tx_id()},
            )
            return False

    def _leer_log(self, tool: str) -> str | None:
        """Lee el log real de la herramienta: ``Logs/{tool}_{modo}_log.txt``.

        Síncrona a propósito: corre dentro del ``to_thread`` de
        :meth:`_post_check`, que es su único llamador.

        Los binarios son apps GUI (PE Subsystem 2): no escriben a stdout ni
        stderr, así que la evidencia de una corrida vive en su log. Ausencia del
        log → ``None`` (warning, no fallo duro): el gate duro es el artefacto en
        el ``-o:``.
        """
        exe = (
            self._config.texgen_exe
            if tool == "TexGen" and self._config.texgen_exe is not None
            else self._config.dyndolod_exe
        )
        log_path = exe.parent / "Logs" / f"{tool}_{self._modo()}_log.txt"
        try:
            return log_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            # SIN etapa por mandato explícito del SOP §2.9 punto 3: "A missing or
            # unreadable log is a warning, not a failure — the hard gate is the
            # artifact at the -o: root". Etiquetarlo contaría como fallo de la
            # etapa 9 corridas exitosas cuyo log no se escribió.
            logger.warning(
                "Log de %s no encontrado: %s",
                tool,
                log_path,
                extra={"operation_type": "dyndolod_log_ausente", "tx_id": _tx_id()},
            )
            return None
        except OSError as e:
            logger.warning(
                "No se pudo leer el log de %s (%s): %s",
                tool,
                log_path,
                e,
                extra={"operation_type": "dyndolod_log_ilegible", "tx_id": _tx_id()},
            )
            return None

    def _parse_log(self, texto: str) -> tuple[list[str], list[str]]:
        """Extrae errores/warnings del log REAL y registra las rutas resueltas.

        Reutiliza los patrones que antes se aplicaban a stdout: el texto cambió,
        el criterio no. Las rutas que la herramienta resolvió (``Using Temp
        Path:``, ``Using plugin list:``) se loguean a info — evidencia de efecto,
        mejor que un ``--help`` — para el post-check (U-06).
        """
        errors: list[str] = []
        for match in self._ERROR_PATTERN.finditer(texto):
            error_msg = match.group(1).strip()
            if error_msg and error_msg not in errors:
                errors.append(error_msg)

        warnings: list[str] = []
        for match in self._WARNING_PATTERN.finditer(texto):
            warning_msg = match.group(1).strip()
            if warning_msg and warning_msg not in warnings:
                warnings.append(warning_msg)

        for linea in texto.splitlines():
            if "Using" in linea:
                logger.info("Rutas resueltas por la herramienta: %s", linea.strip())

        logger.debug("Parse log: errors=%d, warnings=%d", len(errors), len(warnings))
        return errors, warnings

    def _generate_meta_ini(self, mod_path: pathlib.Path, mod_name: str) -> None:
        """
        Genera el archivo meta.ini para MO2.

        Args:
            mod_path: Path al directorio del mod.
            mod_name: Nombre del mod.
        """
        # S2-FIX: Validate target path is within MO2 mods sandbox.
        from sky_claw.app.security.path_validator import PathValidator, PathViolationError

        meta_ini_path = mod_path / "meta.ini"
        validator = PathValidator(roots=[self._config.mo2_mods_path])
        try:
            validator.validate(meta_ini_path, strict_symlink=False)
        except PathViolationError:
            # CON etapa: `PathViolationError` no es `OSError` ni
            # `DynDOLODValidationError`, así que ningún `except` del runner la
            # atrapa — escapa entera de `run_full_pipeline` hasta el
            # `except Exception` del servicio. Es el único registro QUE EMITE
            # ESTE RUNNER para el incidente (el servicio emite el suyo, como en
            # todos los demás: dos capas, un incidente, y por eso la métrica es
            # COUNT(DISTINCT tx_id)), y además es un hecho distinto y condicional
            # —un bloqueo de sandbox que aborta el empaquetado y tumba la
            # corrida—, no una repetición mecánica de otro registro del runner.
            logger.error(
                "Path traversal blocked for meta.ini: %s",
                meta_ini_path,
                extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": _tx_id()},
            )
            raise

        config = configparser.ConfigParser()
        config["General"] = {
            "modid": "0",
            "version": "1.0.0",
            "name": mod_name,
            "comments": "Generated by Sky Claw",
        }

        try:
            with open(meta_ini_path, "w", encoding="utf-8") as f:
                config.write(f)
            logger.debug("meta.ini generado: %s", meta_ini_path)
        except OSError as e:
            # SIN etapa: loguea y RELANZA, y el `except OSError` de
            # `_package_output_as_mod` convierte esa excepción en el
            # `DynDOLODValidationError` que `run_full_pipeline` sí reporta con
            # etapa. Es la repetición que §5 regla 5 prohíbe contar dos veces.
            # El registro se conserva (en vez de borrarlo, que es lo que la regla
            # prescribe para un guard redundante) porque nombra el artefacto
            # —meta.ini— que el mensaje del handler no menciona.
            logger.error(
                "Error generando meta.ini: %s",
                e,
                extra={"operation_type": "dyndolod_meta_ini_no_escrito", "tx_id": _tx_id()},
            )
            raise

    def _find_texgen_output(self) -> pathlib.Path | None:
        """Staging de TexGen: SOLO ``root/TexGen_Output`` (la herramienta crea su
        carpeta dentro del ``-o:`` — layout por default).

        A diferencia de DynDOLOD, TexGen NO tiene un artefacto con nombre propio
        que gatear (su salida son texturas), así que un fallback a ``root`` sería
        un falso verde: si una corrida previa de DynDOLOD dejó
        ``root/DynDOLOD_Output``, ``any(root.iterdir())`` lo aceptaría como salida
        de TexGen y el empaquetado copiaría el staging de DynDOLOD dentro de
        "TexGen Output". Por eso ``_candidatos_de_salida`` le da UN solo
        candidato: si no existe, el gate de artefacto falla (fail-closed).
        """
        candidatos = self._candidatos_de_salida("TexGen")
        return candidatos[0] if candidatos else None

    def _find_dyndolod_output(self) -> pathlib.Path | None:
        """Staging de DynDOLOD: determinista bajo la raíz administrada (``-o:``).

        Resolución por PATH, sin mirar frescura — la usa quien solo necesita saber
        dónde miraría el post-check. El veredicto real lo da :meth:`_post_check`,
        que evalúa todos los candidatos y elige el que tiene artefacto válido y
        fresco: acá el primero no vacío alcanza.
        """
        candidatos = self._candidatos_de_salida("DynDOLOD")
        if not candidatos:
            return None
        for candidato in candidatos:
            if candidato.exists() and any(candidato.iterdir()):
                return candidato
        return candidatos[0]

    async def validate_dyndolod_output(self, output_path: pathlib.Path) -> bool:
        """Valida la salida de DynDOLOD sin bloquear el event loop.

        API pública (la consume ``DynDOLODPipelineService.execute``); el trabajo
        real —``iterdir``/``glob`` sobre un árbol de GBs— vive en la variante
        síncrona y cruza a un hilo. Dentro del runner, el post-check llama
        directo a :meth:`_validar_salida_dyndolod` porque ya corre en un hilo.

        Args:
            output_path: Path al directorio de salida.

        Returns:
            True si la salida parece válida, False en caso contrario.
        """
        return await asyncio.to_thread(self._validar_salida_dyndolod, output_path)

    def _validar_salida_dyndolod(self, output_path: pathlib.Path) -> bool:
        """
        Valida que la salida de DynDOLOD sea válida.

        Realiza validaciones básicas de integridad:
        - El directorio existe y tiene contenido
        - Contiene DynDOLOD.esp
        - Contiene al menos algunos assets esperados

        Args:
            output_path: Path al directorio de salida.

        Returns:
            True si la salida parece válida, False en caso contrario.
        """
        logger.debug("Validando DynDOLOD output: %s", output_path)

        # NINGUNO de los registros de abajo lleva `pipeline_stage`, y el motivo es
        # el mismo para los seis: este método tiene DOS contextos de llamada con
        # significados distintos, y no puede distinguirlos sin averiguar quién lo
        # llamó. Como SONDA (`_tiene_artefacto`, sobre los dos candidatos de
        # staging de DynDOLOD) un resultado negativo es lo NORMAL — que el primer
        # candidato no tenga el artefacto es el caso esperado, y el veredicto lo
        # emite `run_dyndolod` con la etapa. Como GATE del servicio
        # (`validate_dyndolod_output`) el fallo lo reporta con etapa el `except`
        # del servicio. En ninguno de los dos es este método quien decide, así que
        # etiquetar acá sumaría un fallo de etapa 9 por cada candidato sondeado en
        # corridas perfectamente sanas. El SOP §5 pide ramificar cuando el handler
        # cubre dos momentos; acá no se puede, y se exime con `operation_type`
        # propio por registro (`_REGISTROS_EXENTOS_DE_ETAPA_RUNNER`).
        try:
            # Verificar existencia
            if not output_path.exists():
                logger.warning(
                    "Directorio de salida no existe: %s",
                    output_path,
                    extra={"operation_type": "dyndolod_validacion_sin_directorio", "tx_id": _tx_id()},
                )
                return False

            # Verificar que tiene contenido
            items = list(output_path.iterdir())
            if not items:
                logger.warning(
                    "Directorio de salida vacío: %s",
                    output_path,
                    extra={"operation_type": "dyndolod_validacion_directorio_vacio", "tx_id": _tx_id()},
                )
                return False

            # Buscar DynDOLOD.esp
            esp_files = list(output_path.glob("*.esp"))
            if not esp_files:
                logger.warning(
                    "No se encontró archivo ESP en: %s",
                    output_path,
                    extra={"operation_type": "dyndolod_validacion_sin_esp", "tx_id": _tx_id()},
                )
                return False

            # U-06: exigir DynDOLOD.esp, no cualquier .esp. Antes esto solo
            # logueaba un warning y caía igual en ``return True``, así que un run
            # que dejó ESPs sueltos (o el output de otra tool) se validaba como
            # bueno y los stages siguientes construían sobre LODs inexistentes.
            # ``is_file()`` y no ``exists()``: un DIRECTORIO llamado DynDOLOD.esp
            # pasaría el chequeo de existencia y se empaquetaría sin el plugin.
            dyndolod_esp = output_path / "DynDOLOD.esp"
            if not dyndolod_esp.is_file():
                # El más tentador de etiquetar de los seis —suena a fallo
                # definitivo— y sigue siendo el resultado de sondear UN candidato
                # de dos. Ver el comentario al tope del método.
                logger.error(
                    "DynDOLOD.esp no encontrado en %s; encontrados: %s",
                    output_path,
                    [e.name for e in esp_files],
                    extra={"operation_type": "dyndolod_validacion_sin_dyndolod_esp", "tx_id": _tx_id()},
                )
                return False

            logger.info(
                "DynDOLOD output validado: %s (%d items)",
                output_path,
                len(items),
            )
            return True

        except OSError as e:
            logger.error(
                "Error de I/O validando output %s: %s",
                output_path,
                e,
                extra={"operation_type": "dyndolod_validacion_io_fallida", "tx_id": _tx_id()},
            )
            return False
        except RuntimeError as e:
            logger.exception(
                "Error inesperado validando output %s: %s",
                output_path,
                e,
                extra={"operation_type": "dyndolod_validacion_inesperada", "tx_id": _tx_id()},
            )
            return False


__all__ = [
    "DynDOLODConfig",
    "DynDOLODExecutionError",
    "DynDOLODNotFoundError",
    "DynDOLODPipelineResult",
    "DynDOLODRunner",
    "DynDOLODTimeoutError",
]
