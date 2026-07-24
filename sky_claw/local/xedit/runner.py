"""xEdit headless runner — async subprocess wrapper.

Executes SSEEdit (xEdit) scripts in headless mode via
``asyncio.create_subprocess_exec`` with configurable timeout
and input validation to prevent path traversal and command injection.

Phase 2 Extensions:
    - Dynamic Pascal script generation via ScriptGenerator
    - Headless execution with write flags (-IKnowWhatImDoing)
    - ScriptExecutionResult for detailed execution feedback
    - Integration with PatchOrchestrator via execute_patch()
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import pathlib
import re
import tempfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sky_claw.local.tools._process import run_capture, spawn_detached
from sky_claw.local.xedit.output_parser import XEditOutputParser, XEditResult
from sky_claw.local.xedit.script_staging import StagedScript, stage_scripts

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sky_claw.antigravity.security.path_validator import PathValidator
    from sky_claw.local.xedit.patch_orchestrator import PatchPlan

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120

# Only allow safe script names: alphanumeric, underscores, hyphens, dots.
_SAFE_SCRIPT_NAME = re.compile(r"^[a-zA-Z0-9_\-]+\.pas$")

# Only allow safe plugin names: alphanumeric, hyphens, underscores, spaces,
# dots, with valid Skyrim extensions only (.esp, .esm, .esl).  Max 260 chars
# (Windows MAX_PATH).
_SAFE_PLUGIN_NAME = re.compile(r"^[a-zA-Z0-9_\- .]{1,255}\.(esp|esm|esl)$")

# Safe FormID pattern (hexadecimal, 8 characters, optional colon separator)
_SAFE_FORM_ID = re.compile(r"^[0-9A-Fa-f]{6}:?[0-9A-Fa-f]{2}$")

# Valid Skyrim record type signatures (exactly 4 uppercase ASCII chars/underscores)
_SAFE_RECORD_TYPE = re.compile(r"^[A-Z_]{4}$")

# Allowlist of xEdit CLI flags that Sky-Claw may pass.
_ALLOWED_XEDIT_FLAGS: frozenset[str] = frozenset(
    {
        "-IKnowWhatImDoing",
        "-autoload",
        "-autoexit",
        "-SSE",
        "-quickautoclean",
        "-noaliases",
        "-nocrc",
    }
)


# =============================================================================
# EXCEPTION HIERARCHY
# =============================================================================


class XEditError(Exception):
    """Base exception for xEdit operations."""

    pass


class XEditNotFoundError(XEditError, FileNotFoundError):
    """Raised when the xEdit executable is not found."""

    pass


class XEditValidationError(XEditError, ValueError):
    """Raised when input fails validation."""

    pass


class XEditScriptError(XEditError):
    """Error en generación o ejecución de script dinámico."""

    pass


class XEditWriteError(XEditError):
    """Error durante operación de escritura en plugin."""

    pass


class XEditTimeoutError(XEditError, RuntimeError):
    """Raised when xEdit execution times out."""

    pass


# =============================================================================
# DATACLASSES
# =============================================================================


@dataclass
class ScriptExecutionResult:
    """Resultado de ejecución de script xEdit.

    Attributes:
        success: Si la ejecución terminó con exit code 0 y sin errores parseados.
        exit_code: Código de salida del proceso xEdit.
        stdout: Salida estándar capturada.
        stderr: Salida de error capturada.
        records_processed: Número de records procesados (parseado de output).
        errors: Lista de errores encontrados durante la ejecución.
        warnings: Lista de advertencias generadas.
        script_path: Path al script ejecutado (para debugging).
        execution_time: Tiempo de ejecución en segundos.
    """

    success: bool
    exit_code: int
    stdout: str
    stderr: str
    records_processed: int
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    script_path: pathlib.Path | None = None
    execution_time: float = 0.0


# =============================================================================
# SCRIPT GENERATOR
# =============================================================================


class ScriptGenerator:
    """Generador de scripts Pascal para xEdit.

    Esta clase proporciona métodos para generar scripts Pascal dinámicamente
    basados en parámetros de parcheo. Los scripts generados son compatibles
    con xEdit (SSEEdit) y utilizan las funciones de mteFunctions.

    Templates disponibles:
        - forward_record: Forward declaration de records entre plugins
        - merge_leveled_list: Merge de leveled lists (LVLI, LVLN, LVSP)
        - apply_patch: Aplicación de parches genéricos

    Security:
        Todos los strings externos son escapados via _escape_pascal_string()
        para prevenir inyección de código Pascal.
    """

    @staticmethod
    def _escape_pascal_string(val: str) -> str:
        """Escapa caracteres especiales para strings Pascal.

        Previene inyección de código Pascal escapando caracteres que podrían:
        - Romper strings (comillas simples)
        - Inyectar código (comentarios // y bloques {})
        - Causar problemas de escape (backslashes)

        Args:
            val: String a escapar.

        Returns:
            String escapado seguro para incrustar en código Pascal.

        Examples:
            >>> ScriptGenerator._escape_pascal_string("O'Reilly")
            "O''Reilly"
            >>> ScriptGenerator._escape_pascal_string("path\\to\\file")
            "path\\\\to\\\\file"
        """
        if not val:
            return ""
        # Escape backslashes primero (para no doble-escapar)
        escaped = val.replace("\\", "\\\\")
        # Escape comillas simples (Pascal usa '' para representar ')
        escaped = escaped.replace("'", "''")
        return escaped

    TEMPLATE_FORWARD_RECORD = """unit ForwardRecord;
uses mteFunctions, SysUtils;

var
  sourcePlugin, targetPlugin: IInterface;
  processedCount: Integer;

function Initialize: integer;
begin
  sourcePlugin := FileByName('{source_plugin}');
  targetPlugin := FileByName('{target_plugin}');
  processedCount := 0;

  if not Assigned(sourcePlugin) then
  begin
    AddMessage('ERROR: Source plugin not found: {source_plugin}');
    Result := 1;
    Exit;
  end;

  if not Assigned(targetPlugin) then
  begin
    AddMessage('ERROR: Target plugin not found: {target_plugin}');
    Result := 1;
    Exit;
  end;

  AddMessage(Format('Forwarding records from %s to %s', ['{source_plugin}', '{target_plugin}']));
  Result := 0;
end;

function Process(e: IInterface): integer;
var
  formId: string;
  recordSig: string;
begin
  formId := FormID(e);
  recordSig := Signature(e);

  // Check if this is one of the target FormIDs
  if (formId = '{form_id}') or (Pos(formId, '{form_id}') > 0) then
  begin
    AddMessage(Format('Processing record %s (%s)', [formId, recordSig]));
    // Forward the record to target plugin
    // Implementation depends on specific requirements
    Inc(processedCount);
  end;

  Result := 0;
end;

function Finalize: integer;
begin
  AddMessage(Format('ForwardRecord complete. Processed %d records.', [processedCount]));
  Result := 0;
end;

end.
"""

    TEMPLATE_MERGE_LEVELED_LIST = """unit MergeLeveledList;
uses mteFunctions, SysUtils;

var
  mergedPlugin: IInterface;
  processedCount: Integer;
  outputPluginName: string;

function Initialize: integer;
begin
  outputPluginName := '{output_plugin}';
  processedCount := 0;

  AddMessage(Format('Creating merged leveled list plugin: %s', [outputPluginName]));

  // Try to load existing plugin or create new one
  mergedPlugin := FileByName(outputPluginName);
  if not Assigned(mergedPlugin) then
  begin
    mergedPlugin := AddNewFile(outputPluginName);
    if not Assigned(mergedPlugin) then
    begin
      AddMessage('ERROR: Failed to create output plugin: ' + outputPluginName);
      Result := 1;
      Exit;
    end;
  end;

  AddMessage('Output plugin ready: ' + outputPluginName);
  Result := 0;
end;

// Version ganadora por load order IGNORANDO el plugin de salida: en un
// re-run el output ya cargado seria el WinningOverride de sus propios
// records y el guard saltearia todas las fuentes (review Codex PR #238).
function WinnerExcludingOutput(e: IInterface): IInterface;
var
  i: Integer;
  master, ovr: IInterface;
begin
  master := MasterOrSelf(e);
  Result := master;
  for i := OverrideCount(master) - 1 downto 0 do
  begin
    ovr := OverrideByIndex(master, i);
    if not SameText(GetFileName(GetFile(ovr)), outputPluginName) then
    begin
      Result := ovr;
      Exit;
    end;
  end;
end;

function Process(e: IInterface): integer;
var
  recordType: string;
  formId: string;
  newRecord: IInterface;
begin
  Result := 0;
  recordType := Signature(e);
  formId := FormID(e);

  // Skip records que viven en el propio plugin de salida (caso re-run).
  if SameText(GetFileName(GetFile(e)), outputPluginName) then
    Exit;

  // Solo la version ganadora por load order (ignorando el output): sin este
  // guard se copiaba la primera version iterada y se revertian los overrides
  // de la modlist (P0, TECHNICAL_REVIEW.md seccion 4.1).
  if not Equals(e, WinnerExcludingOutput(e)) then
    Exit;

  // Process LVLI, LVLN, LVSP records
  if (recordType = 'LVLI') or (recordType = 'LVLN') or (recordType = 'LVSP') then
  begin
    // Check if this record type is in our target list
    if Pos(recordType, '{record_types}') > 0 then
    begin
      AddMessage(Format('Merging %s record: %s', [recordType, formId]));

      // Copy record to merged plugin
      newRecord := wbCopyElementToRecord(e, mergedPlugin, False, True);
      if Assigned(newRecord) then
      begin
        Inc(processedCount);
      end
      else
      begin
        AddMessage('WARNING: Failed to copy record: ' + formId);
      end;
    end;
  end;

  Result := 0;
end;

function Finalize: integer;
begin
  AddMessage(Format('MergeLeveledList complete. Merged %d records.', [processedCount]));

  // Clean up masters
  CleanMasters(mergedPlugin);
  AddMessage('Masters cleaned.');

  Result := 0;
end;

end.
"""

    TEMPLATE_APPLY_PATCH = """unit ApplyPatch;
uses mteFunctions, SysUtils;

var
  outputPlugin: IInterface;
  processedCount: Integer;
  errorCount: Integer;

function Initialize: integer;
var
  outputName: string;
begin
  outputName := '{output_plugin}';
  processedCount := 0;
  errorCount := 0;

  AddMessage('=== Sky-Claw Patch Application ===');
  AddMessage('Output plugin: ' + outputName);

  // Load or create output plugin
  outputPlugin := FileByName(outputName);
  if not Assigned(outputPlugin) then
  begin
    outputPlugin := AddNewFile(outputName);
    if not Assigned(outputPlugin) then
    begin
      AddMessage('ERROR: Failed to create output plugin');
      Result := 1;
      Exit;
    end;
    AddMessage('Created new output plugin');
  end
  else
  begin
    AddMessage('Using existing output plugin');
  end;

  Result := 0;
end;

function Process(e: IInterface): integer;
var
  recordSig: string;
  formId: string;
  shouldProcess: Boolean;
begin
  recordSig := Signature(e);
  formId := FormID(e);
  shouldProcess := False;

  // Check record signatures to process
  {record_filter}

  if shouldProcess then
  begin
    try
      // Apply patch logic here
      // This is a placeholder for specific patch operations
      Inc(processedCount);

      if (processedCount mod 100) = 0 then
      begin
        AddMessage(Format('Processed %d records...', [processedCount]));
      end;
    except
      on E: Exception do
      begin
        AddMessage(Format('ERROR processing record %s: %s', [formId, E.Message]));
        Inc(errorCount);
      end;
    end;
  end;

  Result := 0;
end;

function Finalize: integer;
begin
  AddMessage('=== Patch Application Summary ===');
  AddMessage(Format('Records processed: %d', [processedCount]));
  AddMessage(Format('Errors encountered: %d', [errorCount]));

  if errorCount > 0 then
  begin
    AddMessage('WARNING: Errors occurred during patching');
    Result := 1;
  end
  else
  begin
    AddMessage('Patch applied successfully');
    Result := 0;
  end;
end;

end.
"""

    @staticmethod
    def generate_forward_script(
        form_id: str,
        source: str,
        target: str,
    ) -> str:
        """Genera script para forward declaration de un record.

        Args:
            form_id: FormID del record a forward (ej: "00012345").
            source: Nombre del plugin fuente.
            target: Nombre del plugin destino.

        Returns:
            Script Pascal completo para forward declaration.

        Raises:
            XEditScriptError: Si los parámetros son inválidos.
        """
        # Validar parámetros
        if not form_id:
            raise XEditScriptError("form_id is required")
        if not source:
            raise XEditScriptError("source plugin is required")
        if not target:
            raise XEditScriptError("target plugin is required")

        # Validar FormID (puede ser simple o con dos puntos)
        clean_form_id = form_id.replace(":", "")
        if not all(c in "0123456789ABCDEFabcdef" for c in clean_form_id):
            raise XEditScriptError(f"Invalid FormID format: {form_id}")

        script = ScriptGenerator.TEMPLATE_FORWARD_RECORD.format(
            form_id=ScriptGenerator._escape_pascal_string(form_id),
            source_plugin=ScriptGenerator._escape_pascal_string(source),
            target_plugin=ScriptGenerator._escape_pascal_string(target),
        )

        logger.debug("Generated forward script for FormID %s: %s -> %s", form_id, source, target)

        return script

    @staticmethod
    def generate_merge_script(
        output_plugin: str,
        record_types: list[str],
    ) -> str:
        """Genera script para merge de leveled lists.

        Args:
            output_plugin: Nombre del plugin de salida (ej: "SkyClaw_Patch.esp").
            record_types: Lista de tipos de record a mergear (ej: ["LVLI", "LVLN"]).

        Returns:
            Script Pascal completo para merge de leveled lists.

        Raises:
            XEditScriptError: Si los parámetros son inválidos.
        """
        # Validar parámetros
        if not output_plugin:
            raise XEditScriptError("output_plugin is required")
        if not record_types:
            raise XEditScriptError("record_types is required")

        # Validar nombre de plugin
        if not _SAFE_PLUGIN_NAME.match(output_plugin):
            raise XEditScriptError(f"Invalid output plugin name: {output_plugin}")

        # Validar tipos de record
        valid_types = {"LVLI", "LVLN", "LVSP"}
        for rt in record_types:
            if rt not in valid_types:
                raise XEditScriptError(f"Invalid record type: {rt}. Must be one of {valid_types}")

        record_types_str = ",".join(record_types)

        script = ScriptGenerator.TEMPLATE_MERGE_LEVELED_LIST.format(
            output_plugin=ScriptGenerator._escape_pascal_string(output_plugin),
            record_types=ScriptGenerator._escape_pascal_string(record_types_str),
        )

        logger.debug("Generated merge script for %s -> %s", record_types_str, output_plugin)

        return script

    @staticmethod
    def generate_patch_script(
        output_plugin: str,
        record_types: list[str] | None = None,
        form_ids: list[str] | None = None,
    ) -> str:
        """Genera script genérico de aplicación de parches.

        Args:
            output_plugin: Nombre del plugin de salida.
            record_types: Tipos de record a procesar (opcional).
            form_ids: FormIDs específicos a procesar (opcional).

        Returns:
            Script Pascal completo para aplicar parches.
        """
        if not output_plugin:
            raise XEditScriptError("output_plugin is required")

        # Construir filtro de records
        record_filter_lines = []

        if record_types:
            for rt in record_types:
                if not _SAFE_RECORD_TYPE.match(rt):
                    raise XEditScriptError(f"Invalid record type: {rt!r}. Must be 4 uppercase ASCII chars.")
                escaped_rt = ScriptGenerator._escape_pascal_string(rt)
                record_filter_lines.append(f"  if recordSig = '{escaped_rt}' then shouldProcess := True;")

        if form_ids:
            for fid in form_ids:
                if not _SAFE_FORM_ID.match(fid):
                    raise XEditScriptError(f"Invalid FormID format: {fid!r}. Must match {_SAFE_FORM_ID.pattern}")
                escaped_fid = ScriptGenerator._escape_pascal_string(fid)
                record_filter_lines.append(f"  if formId = '{escaped_fid}' then shouldProcess := True;")

        # Si no hay filtros específicos, procesar todo
        if not record_filter_lines:
            record_filter_lines.append("  shouldProcess := True; // Process all records")

        record_filter = "\n".join(record_filter_lines)

        script = ScriptGenerator.TEMPLATE_APPLY_PATCH.format(
            output_plugin=ScriptGenerator._escape_pascal_string(output_plugin),
            record_filter=record_filter,  # record_filter es generado internamente, no necesita escape
        )

        logger.debug("Generated patch script for %s", output_plugin)

        return script

    @staticmethod
    def generate_script_from_plan(patch_plan: PatchPlan) -> str:
        """Genera un script Pascal basado en un PatchPlan.

        Args:
            patch_plan: Plan de parcheo con toda la información necesaria.

        Returns:
            Script Pascal generado según el tipo de estrategia.

        Raises:
            XEditScriptError: Si el tipo de estrategia no es soportado.
        """
        from sky_claw.local.xedit.patch_orchestrator import PatchStrategyType

        strategy = patch_plan.strategy_type

        if strategy == PatchStrategyType.FORWARD_DECLARATION:
            # Generar script de forward declaration
            if not patch_plan.form_ids:
                raise XEditScriptError("Forward declaration requires form_ids")

            # Usar el primer FormID y los plugins del plan
            return ScriptGenerator.generate_forward_script(
                form_id=patch_plan.form_ids[0],
                source=patch_plan.target_plugins[0] if patch_plan.target_plugins else "",
                target=patch_plan.output_plugin,
            )

        elif strategy == PatchStrategyType.CREATE_MERGED_PATCH:
            # Deshabilitada por ADR 0001: tras el hotfix T-02 el template hace
            # forward del ganador, pero sigue sin implementar un merge real de
            # entradas (Relev/Delev). Leveled lists → Bashed Patch.
            raise XEditScriptError(
                "La estrategia CREATE_MERGED_PATCH está deshabilitada: no "
                "implementa un merge real de entradas de leveled lists "
                "(Relev/Delev). Usá un Bashed Patch (Wrye Bash) — ADR 0001."
            )

        elif strategy == PatchStrategyType.DELEGATE_BASHED_PATCH:
            # Los planes delegados los ejecuta Wrye Bash, no xEdit: si uno llega
            # hasta acá hay un bug de enrutado en la capa de servicio.
            raise XEditScriptError(
                "Un plan DELEGATE_BASHED_PATCH no genera script xEdit: debe enrutarse al flujo de Wrye Bash (ADR 0001)."
            )

        elif strategy == PatchStrategyType.EXECUTE_XEDIT_SCRIPT:
            # Fail-closed: un plan EXECUTE_XEDIT_SCRIPT llega acá SOLO si su
            # script_path no existe (execute_patch lee el script real cuando
            # está presente). El template genérico (TEMPLATE_APPLY_PATCH) tiene
            # el cuerpo de Process como placeholder sin lógica de parcheo:
            # generaba un .esp vacío con exit 0 y el pipeline reportaba éxito
            # sin resolver nada — un parche placebo, peor que fallar. Mismo
            # precedente que CREATE_MERGED_PATCH (deshabilitada, ADR 0001).
            raise XEditScriptError(
                f"El plan EXECUTE_XEDIT_SCRIPT no tiene un script Pascal real "
                f"(script_path={patch_plan.script_path}). No se genera el template "
                "genérico: es un placebo que no aplica lógica pero reporta éxito. "
                "Agregá el script .pas al bundle o resolvé el conflicto con el "
                "advisor de IA / manualmente en xEdit."
            )

        else:
            raise XEditScriptError(f"Unsupported strategy type: {strategy}")


# =============================================================================
# XEDIT RUNNER
# =============================================================================


class XEditRunner:
    """Async wrapper for xEdit headless execution.

    Supports both read-only script execution and write-mode operations
    with dynamic Pascal script generation.

    Args:
        xedit_path: Path to SSEEdit.exe.
        game_path: Path to the Skyrim SE installation.
        output_dir: Directory for output files (default: xedit_path/output).
        timeout: Maximum execution time in seconds.
        path_validator: Optional validator for path sandboxing.
    """

    def __init__(
        self,
        xedit_path: pathlib.Path,
        game_path: pathlib.Path,
        output_dir: pathlib.Path | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        path_validator: PathValidator | None = None,
    ) -> None:
        self._xedit_path = xedit_path
        self._game_path = game_path
        self._output_dir = output_dir or xedit_path.parent / "output"
        self._timeout = timeout
        self._validator = path_validator
        self._script_generator = ScriptGenerator()

        # Ensure output directory exists
        self._output_dir.mkdir(parents=True, exist_ok=True)

    #: mteFunctions.pas no viene con xEdit; los scripts de Sky-Claw lo requieren.
    MTE_FUNCTIONS_URL = "https://github.com/matortheeternal/TES5EditScripts"

    @property
    def script_generator(self) -> ScriptGenerator:
        """Access the script generator instance."""
        return self._script_generator

    def _assert_mte_functions_available(self, script_content: str) -> None:
        """Falla rápido si *script_content* usa mteFunctions y no está instalado.

        Raises:
            XEditScriptError: Con mensaje accionable (ruta esperada + link de
                descarga) si falta ``Edit Scripts/mteFunctions.pas``.
        """
        # Pascal es case-insensitive: 'uses mtefunctions' también compila contra
        # la librería, así que la detección no puede ser sensible a mayúsculas.
        if "mtefunctions" not in script_content.lower():
            return

        mte_path = self._xedit_path.parent / "Edit Scripts" / "mteFunctions.pas"
        if not mte_path.exists():
            raise XEditScriptError(
                f"El script requiere mteFunctions.pas y no existe en {mte_path}. "
                f"Descargalo de {self.MTE_FUNCTIONS_URL} y copialo a la carpeta "
                "'Edit Scripts' de xEdit."
            )

    async def ensure_scripts_staged(self, script_names: Sequence[str]) -> list[StagedScript]:
        """Stagea scripts Pascal bundleados en la carpeta ``Edit Scripts`` de xEdit.

        ``run_script`` pasa ``-script:<nombre>`` y xEdit lo resuelve contra SU
        ``Edit Scripts/`` — los ``.pas`` del repo no llegan solos ahí. Wrapper
        fino sobre :func:`stage_scripts` (idempotente, byte-compare) porque el
        runner es quien conoce ``xedit_path``; corre en ``to_thread`` (I/O).

        Args:
            script_names: Nombres de scripts del bundle (``foo.pas``).

        Returns:
            Un :class:`StagedScript` por nombre (acción copied/replaced/unchanged).

        Raises:
            PathViolationError: ``xedit_path`` está fuera del sandbox del
                validator (mismo guard que ``run_script`` — evita escribir
                bajo una instalación de xEdit apuntada por config mala o
                comprometida).
            ValueError: Nombre que escapa del bundle (traversal).
            FileNotFoundError: Script inexistente en el bundle o dir de xEdit
                inexistente.
        """
        if self._validator is not None:
            self._validator.validate(self._xedit_path)

        return await asyncio.to_thread(
            stage_scripts,
            self._xedit_path.parent / "Edit Scripts",
            script_names,
        )

    async def run_script(
        self,
        script_name: str,
        plugins: list[str],
        *,
        timeout: int | None = None,
    ) -> XEditResult:
        """Execute an xEdit Pascal script in headless mode (read-only).

        Args:
            script_name: Name of the .pas script (e.g. ``list_conflicts.pas``).
                Must match ``^[a-zA-Z0-9_\\-]+\\.pas$``.
            plugins: List of plugin filenames to load.
                Each must match ``^[a-zA-Z0-9_\\- .]+\\.es[pmlt]$``.
            timeout: Per-call timeout in seconds; ``None`` keeps the runner's
                constructor timeout. Long scans (e.g. the grass worldspaces
                LAND sweep) exceed the 120s default.

        Returns:
            Parsed xEdit result with conflicts and processed plugins.

        Raises:
            XEditNotFoundError: If the xEdit executable doesn't exist.
            XEditValidationError: If inputs fail validation.
            XEditTimeoutError: If xEdit times out.
        """
        self._validate_inputs(script_name, plugins)

        if self._validator is not None:
            self._validator.validate(self._xedit_path)

        if not self._xedit_path.exists():
            raise XEditNotFoundError(f"xEdit executable not found at {self._xedit_path}")

        args = [
            str(self._xedit_path),
            "-SSE",
            "-autoload",
            f"-script:{script_name}",
            f"-D:{self._game_path}",
        ]

        # Append plugins to load.
        for plugin in plugins:
            args.append(plugin)

        logger.info("Running xEdit (read-only): %s", " ".join(args))

        stdout_text, stderr_text, return_code = await self._execute_process(args, timeout=timeout)

        if return_code != 0:
            logger.warning("xEdit exited with code %d: %s", return_code, stderr_text)

        return XEditOutputParser.parse(
            stdout=stdout_text,
            stderr=stderr_text,
            return_code=return_code or 0,
        )

    async def quick_auto_clean(self, plugin: str) -> ScriptExecutionResult:
        """Run SSEEdit QuickAutoClean (``-quickautoclean``) on a single plugin.

        QuickAutoClean removes identical-to-master (ITM) records and deleted
        references from one plugin per invocation, rewriting it in place. The
        official dirty DLC masters (Update/Dawnguard/HearthFires/Dragonborn) are
        the canonical targets; the caller decides which plugins to clean.

        The argument list mirrors the canonical auto-cleaner (PACT):
        ``-quickautoclean -autoexit -autoload "<plugin>"``. ``-autoexit`` is
        mandatory in headless mode — without it xEdit opens its GUI and the
        subprocess blocks until the timeout. ``-D:`` points at the game's real
        ``Data`` folder (where the official masters live).

        Args:
            plugin: Plugin filename to clean (e.g. ``"Update.esm"``). Must match
                ``_SAFE_PLUGIN_NAME`` — the only untrusted input, validated against
                command injection before reaching the subprocess.

        Returns:
            ScriptExecutionResult con el exit code original, la salida parseada y
            ``success=True`` sólo cuando el código es cero y no hay errores.

        Raises:
            XEditValidationError: If the plugin name fails validation.
            XEditNotFoundError: If the xEdit executable doesn't exist.
            XEditTimeoutError: If xEdit times out.
        """
        import time

        if not _SAFE_PLUGIN_NAME.match(plugin):
            raise XEditValidationError(f"Invalid plugin name: {plugin!r}. Must match {_SAFE_PLUGIN_NAME.pattern}")

        if self._validator is not None:
            self._validator.validate(self._xedit_path)

        if not self._xedit_path.exists():
            raise XEditNotFoundError(f"xEdit executable not found at {self._xedit_path}")

        args = [
            str(self._xedit_path),
            "-SSE",
            "-quickautoclean",
            "-autoexit",
            "-autoload",
            f"-D:{self._game_path / 'Data'}",
            plugin,
        ]
        logger.info("Running xEdit QuickAutoClean: %s", " ".join(args))

        start_time = time.monotonic()
        stdout_text, stderr_text, return_code = await self._execute_process(args)
        records, errors, warnings = self._parse_script_output(stdout_text, stderr_text)

        if return_code != 0:
            logger.warning("QuickAutoClean for %s exited with code %d: %s", plugin, return_code, stderr_text)

        return ScriptExecutionResult(
            success=(return_code == 0 and not errors),
            exit_code=return_code,
            stdout=stdout_text,
            stderr=stderr_text,
            records_processed=records,
            errors=errors,
            warnings=warnings,
            execution_time=time.monotonic() - start_time,
        )

    async def run_dynamic_script(
        self,
        script_content: str,
        plugins: list[str],
        flags: list[str] | None = None,
        script_name: str = "dynamic_script.pas",
    ) -> ScriptExecutionResult:
        """Ejecuta un script Pascal generado dinámicamente.

        Este método escribe el script a un archivo temporal y lo ejecuta
        con xEdit en modo headless, capturando toda la salida para análisis.

        Args:
            script_content: Contenido completo del script Pascal.
            plugins: Lista de plugins a procesar.
            flags: Flags adicionales para xEdit (ej: ["-IKnowWhatImDoing"]).
            script_name: Nombre base para el script temporal.

        Returns:
            ScriptExecutionResult con detalles completos de la ejecución.

        Raises:
            XEditNotFoundError: Si xEdit no existe.
            XEditValidationError: Si los plugins son inválidos.
            XEditScriptError: Si hay error en el script.
            XEditTimeoutError: Si la ejecución excede el timeout.
        """
        import time

        start_time = time.monotonic()

        # H-6: validar script_name antes de usarlo como prefix del archivo
        # temporal. Sin esto, un script_name con '../' recorre fuera del sandbox
        # de scripts (NamedTemporaryFile une dir + prefix + random). Mismo check
        # que run_script.
        if not _SAFE_SCRIPT_NAME.match(script_name):
            raise XEditValidationError(f"Invalid script name: {script_name!r}. Must match {_SAFE_SCRIPT_NAME.pattern}")

        # Validar plugins
        for plugin in plugins:
            if not _SAFE_PLUGIN_NAME.match(plugin):
                raise XEditValidationError(f"Invalid plugin name: {plugin!r}. Must match {_SAFE_PLUGIN_NAME.pattern}")

        # Validar path de xEdit
        if self._validator is not None:
            self._validator.validate(self._xedit_path)

        if not self._xedit_path.exists():
            raise XEditNotFoundError(f"xEdit executable not found at {self._xedit_path}")

        # Fallar rápido ANTES de lanzar xEdit si falta mteFunctions (T-09):
        # la alternativa es un error de compilación críptico a mitad del Ritual.
        self._assert_mte_functions_available(script_content)

        # Crear archivo temporal para el script
        script_path: pathlib.Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".pas",
                prefix=script_name.replace(".pas", "_"),
                delete=False,
                dir=str(self._output_dir),
            ) as script_file:
                script_file.write(script_content)
                script_path = pathlib.Path(script_file.name)

            logger.info("Created dynamic script: %s", script_path)
            logger.debug(
                "Script content (%d bytes):\n%s",
                len(script_content),
                script_content[:500],
            )

            # Construir comando
            cmd = self._build_write_command(script_path, plugins, flags)

            logger.info("Running xEdit (dynamic script): %s", " ".join(cmd))

            # Ejecutar
            stdout_text, stderr_text, return_code = await self._execute_process(cmd)

            execution_time = time.monotonic() - start_time

            # Parsear resultados
            records_processed, errors, warnings = self._parse_script_output(stdout_text, stderr_text)

            result = ScriptExecutionResult(
                success=(return_code == 0 and not errors),
                exit_code=return_code,
                stdout=stdout_text,
                stderr=stderr_text,
                records_processed=records_processed,
                errors=errors,
                warnings=warnings,
                script_path=script_path,
                execution_time=execution_time,
            )

            if not result.success:
                logger.warning(
                    "Dynamic script execution failed: exit_code=%d, errors=%d",
                    return_code,
                    len(errors),
                )

            return result

        except XEditTimeoutError:
            raise
        except Exception as e:
            logger.exception("Error executing dynamic script")
            raise XEditScriptError(f"Failed to execute dynamic script: {e}") from e
        finally:
            # U-12: limpiar el .pas temporal (el "paso 6" del docstring que faltaba).
            # Sin esto cada patch/dynamic-run deja un .pas huérfano en output_dir →
            # crecimiento de disco no acotado. El contenido del script ya quedó en los
            # logs (debug) para forense, y ningún caller lee el archivo del result.
            if script_path is not None:
                with contextlib.suppress(OSError):
                    script_path.unlink(missing_ok=True)

    async def execute_patch(
        self,
        patch_plan: PatchPlan,
    ) -> ScriptExecutionResult:
        """Ejecuta un plan de parcheo usando el script generado.

        Protocolo de ejecución:
        1. Generar script Pascal desde PatchPlan
        2. Escribir script a archivo temporal
        3. Ejecutar xEdit con flags -IKnowWhatImDoing -script
        4. Capturar stdout/stderr
        5. Parsear resultados
        6. Limpiar script temporal (si aplica)

        Args:
            patch_plan: Plan de parcheo con estrategia y parámetros.

        Returns:
            ScriptExecutionResult con detalles de la ejecución.

        Raises:
            XEditScriptError: Si falla la generación del script.
            XEditWriteError: Si falla la operación de escritura.
            XEditTimeoutError: Si excede el timeout.
        """
        logger.info(
            "Executing patch plan: strategy=%s, output=%s",
            patch_plan.strategy_type.value,
            patch_plan.output_plugin,
        )

        # Si el plan ya tiene un script path, usarlo
        if patch_plan.script_path and patch_plan.script_path.exists():
            logger.info("Using pre-existing script: %s", patch_plan.script_path)
            script_content = patch_plan.script_path.read_text(encoding="utf-8")
        else:
            # Generar script desde el plan
            try:
                script_content = self._script_generator.generate_script_from_plan(patch_plan)
            except Exception as e:
                logger.error("Failed to generate script from plan: %s", e)
                raise XEditScriptError(f"Script generation failed: {e}") from e

        # Construir flags según estrategia
        flags = ["-IKnowWhatImDoing"]  # Requerido para escritura

        # Ejecutar script dinámico
        result = await self.run_dynamic_script(
            script_content=script_content,
            plugins=patch_plan.target_plugins,
            flags=flags,
            script_name=f"patch_{patch_plan.strategy_type.value}.pas",
        )

        # Verificar resultado
        if not result.success:
            error_msg = f"Patch execution failed: {result.errors}"
            logger.error(error_msg)
            raise XEditWriteError(error_msg)

        logger.info(
            "Patch executed successfully: %d records processed in %.2fs",
            result.records_processed,
            result.execution_time,
        )

        return result

    async def launch_interactive(self, plugins: list[str]) -> asyncio.subprocess.Process:
        """Abre xEdit en modo lectura, posicionado en los plugins del conflicto.

        Es el backend del botón "Abrir en xEdit" del panel de cirugía (T-29):
        cuando el operador decide forwardear un conflicto a mano, esto lanza la
        GUI interactiva con los plugins en disputa ya cargados.

        A diferencia de :meth:`run_script`/:meth:`execute_patch` (headless, con
        ``-autoexit`` y timeout), es un lanzamiento **detached** (fire-and-forget:
        la sesión la cierra el operador) y **sin escrituras automatizadas** — NO
        pasa ``-IKnowWhatImDoing`` ni ``-script``, así que Sky-Claw no aplica
        parches por script desde acá. La sesión interactiva sí es editable: es el
        propósito del botón, que el operador forwardee el conflicto a mano.

        Args:
            plugins: Plugins del conflicto a cargar (winner + losers). xEdit carga
                esos y sus masters, sin diálogo de selección.

        Returns:
            El proceso lanzado (el caller puede trackear su ``pid``).

        Raises:
            XEditValidationError: Si la lista está vacía o algún plugin es inválido.
            XEditNotFoundError: Si el ejecutable de xEdit no existe.
        """
        if not plugins:
            raise XEditValidationError("launch_interactive requires at least one plugin")

        # Mismos guards que run_dynamic_script: inyección en nombres + sandbox del exe.
        for plugin in plugins:
            if not _SAFE_PLUGIN_NAME.match(plugin):
                raise XEditValidationError(f"Invalid plugin name: {plugin!r}. Must match {_SAFE_PLUGIN_NAME.pattern}")

        if self._validator is not None:
            self._validator.validate(self._xedit_path)

        if not self._xedit_path.exists():
            raise XEditNotFoundError(f"xEdit executable not found at {self._xedit_path}")

        cmd = self._build_interactive_command(plugins)
        logger.info("Launching xEdit (interactive, read-only): %s", " ".join(cmd))

        try:
            return await spawn_detached(cmd)
        except FileNotFoundError:
            raise XEditNotFoundError(f"xEdit executable not found at {self._xedit_path}") from None

    def _build_interactive_command(self, plugins: list[str]) -> list[str]:
        """Construye comando para abrir xEdit en una sesión interactiva.

        Espeja el template probado de :meth:`run_script` para cargar plugins:
        ``-autoload`` salta el diálogo de selección de Master/Plugin, y los plugins
        del conflicto van como positional args (xEdit los carga con sus masters).
        ``-D:`` apunta al directorio ``Data`` del juego —no a la raíz de instalación—
        que es donde viven los plugins (igual que :meth:`quick_auto_clean`).

        A diferencia de :meth:`_build_write_command` NO incluye ``-IKnowWhatImDoing``
        ni ``-script`` ni ``-autoexit``: sin escrituras automatizadas y con la GUI
        abierta para el forwardeo manual del operador.

        Args:
            plugins: Plugins del conflicto (ya validados por el caller).

        Returns:
            Lista de argumentos para el lanzamiento.
        """
        cmd = [
            str(self._xedit_path),
            "-SSE",
            "-autoload",
            f"-D:{self._game_path / 'Data'}",
        ]
        cmd.extend(plugins)
        return cmd

    def _build_write_command(
        self,
        script_path: pathlib.Path,
        plugins: list[str],
        flags: list[str] | None = None,
    ) -> list[str]:
        """Construye comando para xEdit en modo escritura.

        Args:
            script_path: Path al script Pascal a ejecutar.
            plugins: Lista de plugins a cargar.
            flags: Flags adicionales para xEdit.

        Returns:
            Lista de argumentos para subprocess execution.
        """
        cmd = [
            str(self._xedit_path),
            "-SSE",
            "-IKnowWhatImDoing",  # Requerido para operaciones de escritura
            f"-script:{script_path}",
            f"-D:{self._game_path}",
        ]

        # Agregar flags adicionales (solo las permitidas)
        if flags:
            for flag in flags:
                if flag not in _ALLOWED_XEDIT_FLAGS:
                    raise XEditValidationError(
                        f"Disallowed xEdit flag: {flag!r}. Allowed: {sorted(_ALLOWED_XEDIT_FLAGS)}"
                    )
                if flag not in cmd:  # Evitar duplicados
                    cmd.append(flag)

        # Agregar plugins (ya validados por el caller)
        for plugin in plugins:
            cmd.append(plugin)

        return cmd

    def _parse_script_output(
        self,
        stdout: str,
        stderr: str,
    ) -> tuple[int, list[str], list[str]]:
        """Parsea salida de script para extraer métricas y mensajes.

        Busca patrones específicos en la salida de xEdit para extraer:
        - Número de records procesados
        - Errores encontrados
        - Advertencias generadas

        Args:
            stdout: Salida estándar de xEdit.
            stderr: Salida de error de xEdit.

        Returns:
            Tupla con (records_processed, errors, warnings).
        """
        errors: list[str] = []
        warnings: list[str] = []
        records_processed = 0

        combined_output = stdout + "\n" + stderr

        # Patrones de búsqueda
        error_patterns = [
            r"ERROR:\s*(.+)",
            r"Error:\s*(.+)",
            r"Exception:\s*(.+)",
            r"Failed to\s*(.+)",
        ]

        warning_patterns = [
            r"WARNING:\s*(.+)",
            r"Warning:\s*(.+)",
            r"ADVERTENCIA:\s*(.+)",
        ]

        records_patterns = [
            r"Processed (\d+) records?",
            r"records? processed:\s*(\d+)",
            r"Merged (\d+) records?",
            r"Complete\.\s*(?:Processed|Merged)\s*(\d+)",
        ]

        # Buscar errores
        for pattern in error_patterns:
            for match in re.finditer(pattern, combined_output, re.IGNORECASE):
                error_msg = match.group(1).strip()
                if error_msg and error_msg not in errors:
                    errors.append(error_msg)

        # Buscar warnings
        for pattern in warning_patterns:
            for match in re.finditer(pattern, combined_output, re.IGNORECASE):
                warning_msg = match.group(1).strip()
                if warning_msg and warning_msg not in warnings:
                    warnings.append(warning_msg)

        # Buscar contador de records
        for pattern in records_patterns:
            for match in re.finditer(pattern, combined_output, re.IGNORECASE):
                try:
                    count = int(match.group(1))
                    records_processed = max(records_processed, count)
                except (ValueError, IndexError):
                    pass

        logger.debug(
            "Parsed output: %d records, %d errors, %d warnings",
            records_processed,
            len(errors),
            len(warnings),
        )

        return records_processed, errors, warnings

    async def _execute_process(
        self,
        args: list[str],
        *,
        timeout: int | None = None,
    ) -> tuple[str, str, int]:
        """Ejecuta un proceso xEdit y captura su salida.

        Args:
            args: Argumentos del comando a ejecutar.
            timeout: Timeout por llamada en segundos; ``None`` usa el del
                constructor.

        Returns:
            Tupla con (stdout, stderr, return_code).

        Raises:
            XEditNotFoundError: Si el ejecutable no existe.
            XEditTimeoutError: Si excede el timeout.
        """
        effective_timeout = timeout if timeout is not None else self._timeout
        try:
            stdout, stderr, returncode = await run_capture(args, timeout=effective_timeout)
        except FileNotFoundError:
            raise XEditNotFoundError(f"xEdit executable not found at {self._xedit_path}") from None
        except TimeoutError:
            raise XEditTimeoutError(f"xEdit timed out after {effective_timeout}s") from None

        stdout_text = stdout.decode(errors="replace")
        stderr_text = stderr.decode(errors="replace")

        return stdout_text, stderr_text, returncode

    def _validate_inputs(self, script_name: str, plugins: list[str]) -> None:
        """Validate script name and plugin names against injection."""
        if not _SAFE_SCRIPT_NAME.match(script_name):
            raise XEditValidationError(f"Invalid script name: {script_name!r}. Must match {_SAFE_SCRIPT_NAME.pattern}")

        for plugin in plugins:
            if not _SAFE_PLUGIN_NAME.match(plugin):
                raise XEditValidationError(f"Invalid plugin name: {plugin!r}. Must match {_SAFE_PLUGIN_NAME.pattern}")
