"""
Runner para Mutagen Synthesis - Pipeline de parcheo automatizado.

Este módulo proporciona la integración con Synthesis, la herramienta CLI de Mutagen
para ejecutar pipelines de patchers de forma headless.

Reference:
    - Patrón subprocess: xedit/runner.py:975-1021
    - Synthesis CLI: https://github.com/Mutagen-Modding/Synthesis
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import hashlib
import logging
import os
import pathlib
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from sky_claw.app.security.links import rmtree_link_aware
from sky_claw.app.security.path_validator import PathViolationError, assert_safe_component
from sky_claw.local.tools._process import run_capture
from sky_claw.local.tools.patcher_pipeline import (
    PatcherPipeline,
    PatcherPipelineError,
    SynthesisSettingsProfile,
    is_x86_dotnet_path,
)
from sky_claw.local.validators.plugin_header import PluginHeaderError, read_plugin_header

logger = logging.getLogger(__name__)

#: Tope de masters por plugin impuesto por el motor Bethesda y Mutagen (0xFE).
_MAX_TES4_MASTERS = 254

#: Tope combinado de stdout/stderr capturados desde Synthesis CLI (ADR 0018/0019).
_DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024

#: Lista blanca mínima de variables OS heredables por Synthesis CLI sin filtrar secretos
#: (API keys, tokens de Telegram, credenciales) hacia patchers comunitarios.
#: ``USERNAME`` es obligatoria: bajo MO2, usvfs 0.5.6.1 hace ``std::string(getenv("USERNAME"))``
#: sin chequear NULL al inicializarse en cada proceso enganchado, y sin ella el CLI muere con
#: 0xC0000005 en ``usvfs_x64.dll+0xa6783`` antes de arrancar (ancla:
#: ``tests/test_entorno_curado_usvfs_username.py``). No es un secreto: ``USERPROFILE`` ya lo contiene.
_SYNTHESIS_ALLOWED_OS_ENV: frozenset[str] = frozenset(
    {
        "ALLUSERSPROFILE",
        "APPDATA",
        "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "PROCESSOR_LEVEL",
        "PROCESSOR_REVISION",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
    }
)


class _EspStatus(enum.Enum):
    """Estado de un sondeo de ``Synthesis.esp`` — OSError nunca es ausencia."""

    ABSENT = "absent"  # el archivo no existe (la corrida puede haberlo creado)
    PRESENT = "present"  # existe y su contenido fue hasheado
    UNREADABLE = "unreadable"  # existe pero no pudo inspeccionarse: indeterminado


@dataclass(frozen=True, slots=True)
class _EspProbe:
    """Sondeo de ``Synthesis.esp`` con los tres estados distinguibles.

    ``digest`` es sha256 del contenido (streaming, 1 MiB) y solo se setea con
    status PRESENT. La identidad de contenido prevalece sobre la metadata: un
    reemplazo que conserva tamaño/timestamp pero cambia los bytes es
    modificación, y reescribir los mismos bytes no es producción nueva.
    """

    status: _EspStatus
    digest: str | None = None


_ESP_HASH_CHUNK = 1 << 20  # 1 MiB


def _probe_esp_sync(esp_path: pathlib.Path) -> _EspProbe:
    """Sondeo bloqueante (corre en executor): stat + sha256 streaming."""
    try:
        esp_path.stat()
    except FileNotFoundError:
        return _EspProbe(_EspStatus.ABSENT)
    except OSError:
        return _EspProbe(_EspStatus.UNREADABLE)
    digest = hashlib.sha256()
    try:
        with esp_path.open("rb") as f:
            for chunk in iter(lambda: f.read(_ESP_HASH_CHUNK), b""):
                digest.update(chunk)
    except OSError:
        return _EspProbe(_EspStatus.UNREADABLE)
    return _EspProbe(_EspStatus.PRESENT, digest.hexdigest())


async def _probe_esp(esp_path: pathlib.Path) -> _EspProbe:
    """Sondea el ESP esperado fuera del event loop.

    Mismo patrón que ``validate_synthesis_esp`` (``run_in_executor`` sobre el
    default executor): la lectura completa del archivo no bloquea el loop.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _probe_esp_sync, esp_path)


_is_x86_dotnet_path = is_x86_dotnet_path


# =============================================================================
# EXCEPTIONS
# =============================================================================


#: Variables que ubican el perfil del usuario. Un proceso de la Fase 1 (git, restore, build) no las ve
#: con su valor real: el ``Directory.Build.targets`` de Synthesis 0.36.6 corre
#: ``RD /S /Q "%USERPROFILE%\\.nuget\\packages\\$(PackageId)"`` en cada build, y un ``.netrc`` del
#: usuario autenticaría un fetch.
VARIABLES_DE_PERFIL = frozenset({"HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "DOTNET_CLI_HOME"})


def con_perfil_privado(env: dict[str, str], hogar: pathlib.Path) -> dict[str, str]:
    """Reemplaza el perfil del usuario por *hogar* (privado y vacío) en *env*, sin importar mayúsculas."""
    for clave in [k for k in env if k.upper() in VARIABLES_DE_PERFIL]:
        del env[clave]
    env.update({"HOME": str(hogar), "USERPROFILE": str(hogar), "DOTNET_CLI_HOME": str(hogar)})
    return env


def construir_entorno_dotnet(
    *,
    dotnet_root: pathlib.Path | None,
    temp_dir: pathlib.Path | None,
) -> dict[str, str]:
    """Entorno mínimo seguro + controles anti-servidor .NET 10, para TODO proceso .NET de Synthesis.

    Lo usan el CLI (Fase 2) y el build de patchers (Fase 1): una sola lista blanca
    (``_SYNTHESIS_ALLOWED_OS_ENV``, censada en ``tests/test_entorno_curado_usvfs_username.py``).
    No hereda ``os.environ`` completo para evitar filtrar secretos (API keys,
    tokens) hacia patchers comunitarios (ADR 0018/0019). Desactiva servidores
    persistentes de MSBuild/Roslyn, sanea ``Program Files (x86)\\dotnet`` en
    ``PATH`` siempre y antepone ``dotnet_root`` x64 cuando está configurado.
    ``PROGRAMFILES`` es obligatoria para ``dotnet restore`` (NuGet; evidencia de rig PR-3b).
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key.upper() in _SYNTHESIS_ALLOWED_OS_ENV:
            env[key] = value

    # Controles .NET 10 / MSBuild / Roslyn (ADR 0019 §2.4).
    env["DOTNET_CLI_USE_MSBUILD_SERVER"] = "0"
    env["MSBUILDDISABLENODEREUSE"] = "1"
    env["UseSharedCompilation"] = "false"
    env["DOTNET_NOLOGO"] = "1"
    env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"

    if temp_dir is not None:
        temp_str = str(temp_dir)
        env["TEMP"] = temp_str
        env["TMP"] = temp_str
        env["TMPDIR"] = temp_str

    # Sanear PATH x86 SIEMPRE (incluso si dotnet_root es None, ADR 0019 §2.4).
    path_keys = [key for key in env if key.upper() == "PATH"]
    path_key = path_keys[0] if path_keys else "PATH"
    entries = [entry for key in path_keys for entry in env[key].split(os.pathsep) if entry]
    for extra_key in path_keys[1:]:
        del env[extra_key]
    filtered = [entry for entry in entries if not _is_x86_dotnet_path(entry)]

    if dotnet_root is not None:
        dotnet_dir = str(dotnet_root)
        env["DOTNET_ROOT"] = dotnet_dir
        filtered = [
            dotnet_dir,
            *[
                entry
                for entry in filtered
                if os.path.normcase(os.path.normpath(entry)) != os.path.normcase(os.path.normpath(dotnet_dir))
            ],
        ]

    if filtered:
        env[path_key] = os.pathsep.join(filtered)
    elif path_key in env:
        env[path_key] = ""

    return env


class SynthesisExecutionError(Exception):
    """Error durante la ejecución de Synthesis."""

    def __init__(
        self,
        message: str,
        return_code: int | None = None,
        stderr: str | None = None,
    ) -> None:
        super().__init__(message)
        self.return_code = return_code
        self.stderr = stderr


class SynthesisTimeoutError(SynthesisExecutionError):
    """Error cuando Synthesis excede el timeout configurado."""

    def __init__(self, timeout_seconds: int) -> None:
        super().__init__(
            message=f"Synthesis timed out after {timeout_seconds}s",
            return_code=None,
            stderr=None,
        )
        self.timeout_seconds = timeout_seconds


class SynthesisNotFoundError(SynthesisExecutionError):
    """Error cuando el ejecutable de Synthesis no se encuentra."""

    def __init__(self, synthesis_path: pathlib.Path) -> None:
        super().__init__(
            message=f"Synthesis executable not found at {synthesis_path}",
            return_code=None,
            stderr=None,
        )


class SynthesisValidationError(SynthesisExecutionError):
    """Error cuando el ESP generado está corrupto o es inválido."""

    def __init__(self, message: str, esp_path: pathlib.Path | None = None) -> None:
        super().__init__(message=message, return_code=None, stderr=None)
        self.esp_path = esp_path


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass(frozen=True, slots=True)
class SynthesisConfig:
    """Configuración para ejecutar Synthesis.

    Attributes:
        game_path: Ruta al directorio del juego (Skyrim SE/AE).
        mo2_path: Ruta al directorio de Mod Organizer 2.
        output_path: Ruta donde se genera Synthesis.esp.
        synthesis_exe: Ruta al ejecutable de Synthesis CLI.
        timeout_seconds: Timeout en segundos para la ejecución.
        profile: Identificador del perfil Synthesis (nickname o GUID), enviado
            como ``--ProfileIdentifier`` y resuelto contra PipelineSettings V2.
        mo2_profile: Nombre del perfil MO2 activo, separado del identificador
            Synthesis; se usa solo para resolver el load order del perfil.
        pipeline_settings_path: Settings V2 reales de Synthesis CLI. Por omisión,
            ``PipelineSettings.json`` en el directorio de trabajo actual.
        data_folder_path: ``Data`` físico del juego; por omisión ``game_path/Data``.
        load_order_file_path: ``plugins.txt`` del perfil MO2 activo, usado por
            el CLI como listado con marcas de activación. Si no se pasa, se
            resuelve ``profiles/<mo2_profile>/plugins.txt`` bajo la raíz de datos.
    """

    game_path: pathlib.Path
    mo2_path: pathlib.Path
    output_path: pathlib.Path
    synthesis_exe: pathlib.Path
    timeout_seconds: int = 300
    profile: str = "Default"
    pipeline_settings_path: pathlib.Path | None = None
    data_folder_path: pathlib.Path | None = None
    load_order_file_path: pathlib.Path | None = None
    mo2_profile: str | None = None
    extra_data_folder: pathlib.Path | None = None
    working_dir: pathlib.Path | None = None
    temp_dir: pathlib.Path | None = None
    dotnet_root: pathlib.Path | None = None
    persistence_mode: Literal["None", "Text"] = "None"
    persistence_path: pathlib.Path | None = None
    target_runtime: str | None = None
    block_building_within_mo2: bool = False
    shortcircuit: bool | None = None
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES
    require_patcher_receipts: bool = False
    #: PR-3f (ADR 0019 §2.1): ID canónico → shim de la Fase 1. Con mapa, el traductor de PR-3c convierte
    #: cada patcher en un ``CliPatcherSettings`` que apunta a su shim. Todavía NO viaja al worker
    #: brokered: ese runner lo rechaza (el transporte llega con el cableado del servicio, PR-3g).
    cli_executables: Mapping[str, pathlib.Path] | None = None

    def __post_init__(self) -> None:
        """Valida el ejecutable y completa rutas derivables sin adivinar el perfil."""
        if not self.synthesis_exe.exists():
            raise SynthesisNotFoundError(self.synthesis_exe)
        if type(self.max_output_bytes) is not int or self.max_output_bytes <= 0:
            raise SynthesisExecutionError(
                f"Synthesis max_output_bytes must be a positive int, got {self.max_output_bytes!r}"
            )
        if self.persistence_mode not in {"None", "Text"}:
            raise SynthesisExecutionError(f"Unsupported Synthesis persistence_mode: {self.persistence_mode!r}")
        if self.persistence_mode == "Text" and (
            self.persistence_path is None or not str(self.persistence_path).strip()
        ):
            raise SynthesisExecutionError(
                "Synthesis persistence_path is required when persistence_mode is 'Text' (RunPersistencePreparer.cs:28)"
            )
        if self.shortcircuit is False:
            raise SynthesisExecutionError(
                "Synthesis CLI 0.36.6 declares --Shortcircuit as a bool switch with Default=true and cannot be "
                "disabled via CLI flags; use shortcircuit=True or None"
            )
        if self.extra_data_folder is not None:
            if self.working_dir is None:
                raise SynthesisExecutionError(
                    "Synthesis extra_data_folder requires working_dir whose Data directory matches extra_data_folder"
                )
            if (self.working_dir / "Data").resolve() != self.extra_data_folder.resolve():
                raise SynthesisExecutionError(
                    "Synthesis extra_data_folder must match working_dir / 'Data' for Synthesis 0.36.6"
                )
        if self.working_dir is not None and not self.output_path.is_absolute():
            raise SynthesisExecutionError("Synthesis output_path must be absolute when working_dir is configured")
        if self.dotnet_root is not None:
            dotnet_root = self.dotnet_root
            dotnet_name = dotnet_root.name.casefold()
            # Si pasaron el ejecutable dotnet(.exe) en lugar de su directorio raíz,
            # normalizar al padre para no generar ".../dotnet.exe/dotnet.exe".
            # Cuidar no truncar un directorio válido llamado "dotnet" (p. ej. Program Files\dotnet).
            if not dotnet_root.is_dir() and (
                dotnet_name == "dotnet.exe" or (dotnet_name == "dotnet" and dotnet_root.is_file())
            ):
                dotnet_root = dotnet_root.parent
                object.__setattr__(self, "dotnet_root", dotnet_root)
            if dotnet_root.exists() and not dotnet_root.is_dir():
                raise SynthesisExecutionError(f"Synthesis dotnet_root must be a directory, not a file: {dotnet_root}")
            if is_x86_dotnet_path(str(dotnet_root)):
                raise SynthesisExecutionError(
                    f"Synthesis dotnet_root cannot target x86 .NET ({dotnet_root}); configure an x64 .NET root"
                )
        if self.pipeline_settings_path is None:
            object.__setattr__(self, "pipeline_settings_path", pathlib.Path.cwd() / "PipelineSettings.json")
        if self.data_folder_path is None:
            object.__setattr__(self, "data_folder_path", self.game_path / "Data")
        if (
            self.working_dir is not None
            and self.data_folder_path is not None
            and not self.data_folder_path.is_absolute()
        ):
            raise SynthesisExecutionError("Synthesis data_folder_path must be absolute when working_dir is configured")
        mo2_profile = self.mo2_profile or self.profile
        if self.load_order_file_path is None:
            try:
                mo2_profile = assert_safe_component(mo2_profile, field="MO2 profile")
            except PathViolationError as exc:
                raise SynthesisExecutionError(f"Cannot resolve MO2 profile safely: {exc}") from exc
            candidate = self.mo2_path / "profiles" / mo2_profile / "plugins.txt"
            if not candidate.is_symlink() and candidate.is_file():
                object.__setattr__(self, "load_order_file_path", candidate)


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    """Resultado de la ejecución de Synthesis.

    Attributes:
        success: True si la ejecución fue exitosa.
        output_esp: Path al ESP generado (o None si falló).
        return_code: Código de retorno del proceso.
        stdout: Salida estándar capturada.
        stderr: Salida de error capturada.
        patchers_executed: Lista de patchers que se ejecutaron.
        errors: Lista de errores detectados en la salida.
    """

    success: bool
    output_esp: pathlib.Path | None
    return_code: int
    stdout: str
    stderr: str
    patchers_executed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    output_esps: tuple[pathlib.Path, ...] = ()


# =============================================================================
# SYNTHESIS RUNNER
# =============================================================================


class SynthesisRunner:
    """
    Ejecuta pipelines de Mutagen Synthesis en modo CLI headless.

    Synthesis es una herramienta de patching automatizado que ejecuta
    múltiples patchers en secuencia para generar un plugin final.

    Usage:
        config = SynthesisConfig(
            game_path=Path("C:/Games/Skyrim Special Edition"),
            mo2_path=Path("C:/Modding/MO2"),
            output_path=Path("C:/Modding/MO2/overwrite"),
            synthesis_exe=Path("C:/Modding/Synthesis/Synthesis.CLI.exe"),
        )

        runner = SynthesisRunner(config)
        result = await runner.run_pipeline(
            patcher_ids=["LeveledListsPatcher", "EnemyLevelScalingPatcher"]
        )

        if result.success:
            print(f"ESP generado: {result.output_esp}")
    """

    # Patrones regex para parsear salida real de Synthesis 0.36.6 (ConsoleReporter / ErrorClassifier)
    # y conservar compatibilidad con marcadores genéricos. Permitir corchetes "]" internos en Nickname.
    _CONSOLE_START_PATTERN = re.compile(r"^[ \t]*\[([^\r\n]+)\][ \t]+Starting run\.[ \t\r]*$", re.MULTILINE)
    _CONSOLE_SUCCESS_PATTERN = re.compile(r"^[ \t]*\[([^\r\n]+)\][ \t]+Run successful\.[ \t\r]*$", re.MULTILINE)
    _CONSOLE_EXPORT_PATTERN = re.compile(
        r"^[ \t]*Exported patch to final destination:[ \t]*(.+?)[ \t\r]*$",
        re.IGNORECASE | re.MULTILINE,
    )
    _PATCHER_PATTERN = re.compile(
        r"(?:Running|Executing|Patchers?:)\s*[:\[]?\s*['\"]?([A-Za-z0-9_.]+)",
        re.IGNORECASE,
    )
    _ERROR_PATTERN = re.compile(
        r"(?:"
        r"(?:Overall error|Error detected|Process exited in failure|\[[^\r\n]+\]\s+Preparation error):\s*(.*?)"
        r"|^[ \t]*(?:[A-Za-z0-9_.]+\.)?(?:Mo2BuildBlockedException|Mo2BuildException"
        r"|NonAdjacentSplitModsException|TooManyMastersException|MissingModException)\b\s*:[^\n]*"
        r"|\berror\s+(?:MSB|CS)\d+\s*:\s*[^\n]+"
        r"|(?:ERROR|Error|FAIL|Exception|Critical):\s*(.+?)"
        r")(?:\n|$)",
        re.IGNORECASE | re.MULTILINE,
    )
    _SUCCESS_PATTERN = re.compile(
        r"(?:Exported patch to final destination:"
        r"|\[[^\r\n]+\]\s+Run successful\."
        r"|(?:Successfully|Complete|Finished|Generated)\s+(?:patch|output|plugin))",
        re.IGNORECASE,
    )

    def __init__(self, config: SynthesisConfig) -> None:
        """
        Inicializa el runner de Synthesis.

        Args:
            config: Configuración con paths y timeout.
        """
        self._config = config
        logger.info(
            "SynthesisRunner inicializado: exe=%s, timeout=%ds",
            config.synthesis_exe,
            config.timeout_seconds,
        )

    def inspect_settings(self, patcher_ids: list[str] | None = None) -> SynthesisSettingsProfile:
        """Valida el archivo V2, el perfil y el load order MO2 antes de mutar."""
        settings_path = self._config.pipeline_settings_path
        if settings_path is None or not settings_path.is_file():
            raise SynthesisExecutionError(
                f"Synthesis PipelineSettings.json not found: {settings_path}",
                stderr="Set SYNTHESIS_PIPELINE_SETTINGS or place PipelineSettings.json in the current working directory.",
            )
        data_folder = self._config.data_folder_path
        if data_folder is None or not data_folder.is_dir():
            raise SynthesisExecutionError(f"Skyrim Data folder not found: {data_folder}")
        load_order = self._config.load_order_file_path
        if load_order is None or load_order.is_symlink() or not load_order.is_file():
            raise SynthesisExecutionError(
                "The active MO2 profile has no readable regular plugins.txt; refusing to use the global plugin list."
            )
        try:
            return PatcherPipeline.inspect_synthesis_settings(settings_path, self._config.profile, patcher_ids)
        except PatcherPipelineError as exc:
            raise SynthesisExecutionError(f"Invalid Synthesis pipeline settings: {exc}", stderr=str(exc)) from exc

    @staticmethod
    def _fingerprint_file(path: pathlib.Path, *, label: str) -> str:
        """SHA-256 streaming de una entrada que el preflight va a consumir."""
        digest = hashlib.sha256()
        try:
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(_ESP_HASH_CHUNK), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise SynthesisExecutionError(f"Cannot fingerprint {label} at {path}: {exc}", stderr=str(exc)) from exc
        return digest.hexdigest()

    def settings_fingerprint(self) -> str:
        """SHA-256 del archivo V2 para fijar la configuración validada del run."""
        settings_path = self._config.pipeline_settings_path
        if settings_path is None:
            raise SynthesisExecutionError("Synthesis PipelineSettingsPath is required")
        return self._fingerprint_file(settings_path, label="Synthesis pipeline settings")

    def load_order_fingerprint(self) -> str:
        """SHA-256 del plugins.txt MO2 activo; nunca usa el listing global."""
        load_order_path = self._config.load_order_file_path
        if load_order_path is None or load_order_path.is_symlink() or not load_order_path.is_file():
            raise SynthesisExecutionError(
                "The active MO2 profile has no readable regular plugins.txt; refusing to use the global plugin list."
            )
        return self._fingerprint_file(load_order_path, label="active MO2 profile plugins.txt")

    def _snapshot_load_order(self, expected_fingerprint: str) -> pathlib.Path:
        """Copia los bytes verificados del plugins.txt para quitar la carrera con la CLI."""
        load_order_path = self._config.load_order_file_path
        if load_order_path is None or load_order_path.is_symlink() or not load_order_path.is_file():
            raise SynthesisExecutionError(
                "The active MO2 profile has no readable regular plugins.txt; refusing to use the global plugin list."
            )

        digest = hashlib.sha256()
        temporary_path: pathlib.Path | None = None
        try:
            with (
                load_order_path.open("rb") as source,
                tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix="skyclaw-synthesis-plugins-",
                    suffix=".txt",
                    delete=False,
                ) as destination,
            ):
                temporary_path = pathlib.Path(destination.name)
                for chunk in iter(lambda: source.read(_ESP_HASH_CHUNK), b""):
                    digest.update(chunk)
                    destination.write(chunk)
                destination.flush()
        except OSError as exc:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    logger.critical("No se pudo retirar el snapshot temporal %s: %s", temporary_path, cleanup_error)
            raise SynthesisExecutionError(f"Cannot snapshot active MO2 plugins.txt: {exc}", stderr=str(exc)) from exc

        assert temporary_path is not None
        if digest.hexdigest() != expected_fingerprint:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                logger.critical("No se pudo retirar el snapshot temporal %s: %s", temporary_path, cleanup_error)
            raise SynthesisExecutionError("The active MO2 plugins.txt changed while being snapshotted")
        return temporary_path

    def output_base_paths(self, patcher_ids: list[str] | None = None) -> tuple[pathlib.Path, ...]:
        """Rutas base administradas por los grupos seleccionados (antes de split)."""
        profile = self.inspect_settings(patcher_ids)
        return tuple(self._config.output_path / f"{name}.esp" for name in profile.group_names)

    def discover_output_files(self, patcher_ids: list[str] | None = None) -> list[pathlib.Path]:
        """Descubre todos los outputs ESP de los grupos, incluidos ``_2``, ``_3``, ….

        Solo acepta archivos regulares con el patrón exacto del grupo configurado;
        un symlink, directorio o colisión de nombres no se sigue ni se atribuye.
        """
        profile = self.inspect_settings(patcher_ids)
        return self._discover_output_files_for_groups(profile.group_names)

    def _discover_output_files_for_groups(self, group_names: tuple[str, ...]) -> list[pathlib.Path]:
        output_dir = self._config.output_path
        if output_dir.is_symlink():
            raise SynthesisValidationError(f"Synthesis output path is not a regular directory: {output_dir}")
        if not output_dir.exists():
            return []
        if not output_dir.is_dir():
            raise SynthesisValidationError(f"Synthesis output path is not a regular directory: {output_dir}")

        matches: list[tuple[int, tuple[int, str], pathlib.Path]] = []
        seen_names: set[str] = set()
        try:
            entries = list(output_dir.iterdir())
        except OSError as exc:
            raise SynthesisExecutionError(f"Cannot inspect Synthesis output directory {output_dir}: {exc}") from exc

        for entry in entries:
            for group_index, group_name in enumerate(group_names):
                pattern = re.compile(rf"^{re.escape(group_name)}(?:_([2-9]|[1-9][0-9]+))?\.esp$", re.IGNORECASE)
                match = pattern.fullmatch(entry.name)
                if match is None:
                    continue
                folded_name = entry.name.casefold()
                if folded_name in seen_names:
                    raise SynthesisValidationError(f"Case-insensitive Synthesis output collision: {entry.name}")
                seen_names.add(folded_name)
                if entry.is_symlink() or not entry.is_file():
                    raise SynthesisValidationError(f"Synthesis output is not a regular file: {entry}")
                split_suffix = match.group(1)
                split_order = (len(split_suffix), split_suffix) if split_suffix is not None else (1, "1")
                matches.append((group_index, split_order, entry))
                break

        return [path for _, _, path in sorted(matches, key=lambda item: (item[0], item[1], item[2].name.casefold()))]

    async def _probe_output_files(self, group_names: tuple[str, ...]) -> dict[pathlib.Path, _EspProbe]:
        """Sondea outputs existentes conservando el estado unreadable.

        La ruta estricta ``discover_output_files`` se usa para retirar/snapshotear
        archivos y rechaza objetos que no sean ESP regulares. El probe de
        atribución, en cambio, registra esos nombres como ``UNREADABLE`` para
        distinguirlos de una ausencia y fallar cerrado después del proceso.
        Nunca sigue symlinks.
        """
        output_dir = self._config.output_path
        if output_dir.is_symlink():
            raise SynthesisValidationError(f"Synthesis output path is not a regular directory: {output_dir}")
        if not output_dir.exists():
            return {}
        if not output_dir.is_dir():
            raise SynthesisValidationError(f"Synthesis output path is not a regular directory: {output_dir}")
        try:
            entries = list(output_dir.iterdir())
        except OSError as exc:
            raise SynthesisExecutionError(f"Cannot inspect Synthesis output directory {output_dir}: {exc}") from exc

        matches: list[tuple[int, tuple[int, str], pathlib.Path, bool]] = []
        seen_names: set[str] = set()
        for entry in entries:
            for group_index, group_name in enumerate(group_names):
                pattern = re.compile(rf"^{re.escape(group_name)}(?:_([2-9]|[1-9][0-9]+))?\.esp$", re.IGNORECASE)
                match = pattern.fullmatch(entry.name)
                if match is None:
                    continue
                folded_name = entry.name.casefold()
                if folded_name in seen_names:
                    raise SynthesisValidationError(f"Case-insensitive Synthesis output collision: {entry.name}")
                seen_names.add(folded_name)
                try:
                    regular_file = not entry.is_symlink() and entry.is_file()
                except OSError:
                    regular_file = False
                split_suffix = match.group(1)
                split_order = (len(split_suffix), split_suffix) if split_suffix is not None else (1, "1")
                matches.append((group_index, split_order, entry, regular_file))
                break

        matches.sort(key=lambda item: (item[0], item[1], item[2].name.casefold()))
        result: dict[pathlib.Path, _EspProbe] = {}
        readable_paths: list[pathlib.Path] = []
        for _, _, path, regular_file in matches:
            if regular_file:
                readable_paths.append(path)
            else:
                result[path] = _EspProbe(_EspStatus.UNREADABLE)
        probes = await asyncio.gather(*(_probe_esp(path) for path in readable_paths))
        for path, probe in zip(readable_paths, probes, strict=True):
            result[path] = probe
        return {path: result[path] for _, _, path, _ in matches}

    async def run_pipeline(
        self,
        patcher_ids: list[str],
        extra_args: list[str] | None = None,
        *,
        expected_settings_fingerprint: str | None = None,
        expected_load_order_fingerprint: str | None = None,
        cli_executables: Mapping[str, pathlib.Path] | None = None,
    ) -> SynthesisResult:
        """Ejecuta el contrato ``run-pipeline`` de Synthesis CLI V2.

        ``cli_executables`` (PR-3h): mapa de shims de la Fase 1 de ESTA corrida; ``None`` usa el de
        ``SynthesisConfig``. El servicio lo prepara por corrida porque depende de los patchers elegidos.

        ``patcher_ids`` selecciona patchers del perfil V2; no se traducen a un
        ``--patcher`` inexistente. Un settings temporal conserva los patchers y
        grupos seleccionados y desactiva la escritura lateral de load order.
        """
        logger.info(
            "Iniciando pipeline Synthesis con %d patchers: %s",
            len(patcher_ids),
            ", ".join(patcher_ids),
        )

        settings_fingerprint = (
            expected_settings_fingerprint
            if expected_settings_fingerprint is not None
            else await asyncio.to_thread(self.settings_fingerprint)
        )
        load_order_fingerprint = (
            expected_load_order_fingerprint
            if expected_load_order_fingerprint is not None
            else await asyncio.to_thread(self.load_order_fingerprint)
        )
        if await asyncio.to_thread(self.settings_fingerprint) != settings_fingerprint:
            raise SynthesisExecutionError("Synthesis PipelineSettings.json changed after preflight; refusing to run")
        if await asyncio.to_thread(self.load_order_fingerprint) != load_order_fingerprint:
            raise SynthesisExecutionError("The active MO2 plugins.txt changed after preflight; refusing to run")
        profile = await asyncio.to_thread(self.inspect_settings, patcher_ids)
        if await asyncio.to_thread(self.settings_fingerprint) != settings_fingerprint:
            raise SynthesisExecutionError("Synthesis PipelineSettings.json changed while it was being validated")
        if await asyncio.to_thread(self.load_order_fingerprint) != load_order_fingerprint:
            raise SynthesisExecutionError("The active MO2 plugins.txt changed while it was being validated")
        settings_path = self._config.pipeline_settings_path
        assert settings_path is not None
        temporary_settings: pathlib.Path | None = None
        temporary_load_order: pathlib.Path | None = None
        ephemeral_temp_dir: pathlib.Path | None = None
        process_error: BaseException | None = None
        try:
            dotnet_override: pathlib.Path | None = None
            if self._config.dotnet_root is not None:
                dotnet_override = self._config.dotnet_root / ("dotnet.exe" if sys.platform == "win32" else "dotnet")
            try:
                temporary_settings, profile = await asyncio.to_thread(
                    PatcherPipeline.prepare_synthesis_cli_settings,
                    settings_path,
                    self._config.profile,
                    patcher_ids,
                    dotnet_path_override=dotnet_override,
                    cli_executables=(cli_executables if cli_executables is not None else self._config.cli_executables),
                )
            except PatcherPipelineError as exc:
                raise SynthesisExecutionError(f"Invalid Synthesis pipeline settings: {exc}", stderr=str(exc)) from exc
            if await asyncio.to_thread(self.settings_fingerprint) != settings_fingerprint:
                raise SynthesisExecutionError(
                    "Synthesis PipelineSettings.json changed while preparing the CLI settings"
                )
            if await asyncio.to_thread(self.load_order_fingerprint) != load_order_fingerprint:
                raise SynthesisExecutionError("The active MO2 plugins.txt changed while preparing the CLI settings")
            temporary_load_order = await asyncio.to_thread(self._snapshot_load_order, load_order_fingerprint)
            if await asyncio.to_thread(self.load_order_fingerprint) != load_order_fingerprint:
                raise SynthesisExecutionError("The active MO2 plugins.txt changed while creating its CLI snapshot")

            if self._config.temp_dir is None:
                mkdtemp_task = asyncio.create_task(asyncio.to_thread(tempfile.mkdtemp, prefix="skyclaw-synthesis-tmp-"))
                try:
                    ephemeral_temp_dir = pathlib.Path(await mkdtemp_task)
                except BaseException:
                    if mkdtemp_task.done() and not mkdtemp_task.cancelled() and mkdtemp_task.exception() is None:
                        with contextlib.suppress(OSError):
                            rmtree_link_aware(pathlib.Path(mkdtemp_task.result()), limpiar_readonly=True)
                    else:

                        def _cleanup_late_temp(done_task: asyncio.Task[str]) -> None:
                            if not done_task.cancelled() and done_task.exception() is None:
                                with contextlib.suppress(OSError):
                                    rmtree_link_aware(pathlib.Path(done_task.result()), limpiar_readonly=True)

                        mkdtemp_task.add_done_callback(_cleanup_late_temp)
                    raise

            args = self._build_cli_args(
                patcher_ids,
                extra_args,
                pipeline_settings_path=temporary_settings,
                load_order_file_path=temporary_load_order,
            )
            probe_before = await self._probe_output_files(profile.group_names)
            try:
                stdout, stderr, return_code = await self._execute_process(args, temp_dir_override=ephemeral_temp_dir)
            except SynthesisExecutionError:
                raise
            except (TimeoutError, OSError, RuntimeError) as exc:
                logger.exception("Error inesperado ejecutando Synthesis: %s", exc)
                return SynthesisResult(
                    success=False,
                    output_esp=None,
                    return_code=-1,
                    stdout="",
                    stderr=str(exc),
                    patchers_executed=[],
                    errors=[str(exc)],
                )

            if await asyncio.to_thread(self.settings_fingerprint) != settings_fingerprint:
                raise SynthesisExecutionError("Synthesis PipelineSettings.json changed while Synthesis was running")
            if await asyncio.to_thread(self.load_order_fingerprint) != load_order_fingerprint:
                raise SynthesisExecutionError("The active MO2 plugins.txt changed while Synthesis was running")

            _, patchers_executed, errors = self.parse_output(stdout, stderr)
            if return_code != 0:
                errors.append(f"Synthesis CLI exited with code {return_code}")
            if self._config.require_patcher_receipts:
                combined_receipt_output = stdout + "\n" + stderr
                started_counts = Counter(
                    m.group(1).strip() for m in self._CONSOLE_START_PATTERN.finditer(combined_receipt_output)
                )
                succeeded_counts = Counter(
                    m.group(1).strip() for m in self._CONSOLE_SUCCESS_PATTERN.finditer(combined_receipt_output)
                )
                expected_counts = Counter(profile.patcher_nicknames)
                for expected_name, expected_total in expected_counts.items():
                    started_seen = started_counts[expected_name]
                    succeeded_seen = succeeded_counts[expected_name]
                    if started_seen < expected_total or succeeded_seen < expected_total:
                        errors.append(
                            f"Synthesis patcher {expected_name!r} has incomplete execution receipts "
                            f"(expected {expected_total}, started {started_seen}, succeeded {succeeded_seen})"
                        )
                export_count = sum(1 for _ in self._CONSOLE_EXPORT_PATTERN.finditer(combined_receipt_output))
                expected_exports = len(profile.group_names)
                if export_count < expected_exports:
                    errors.append(
                        "Synthesis CLI output is missing 'Exported patch to final destination:' "
                        f"confirmations (expected {expected_exports}, saw {export_count})"
                    )

            try:
                probe_after = await self._probe_output_files(profile.group_names)
            except (OSError, SynthesisExecutionError, SynthesisValidationError) as exc:
                probe_after = {}
                errors.append(str(exc))

            output_esps = tuple(path for path, probe in probe_after.items() if probe.status is _EspStatus.PRESENT)
            for group_name in profile.group_names:
                group_paths = [path for path in probe_after if self._output_belongs_to_group(path.name, group_name)]
                if not group_paths:
                    errors.append(f"Synthesis reported completion but {group_name}.esp was not produced")
                    continue
                unreadable_before = [
                    path
                    for path, probe in probe_before.items()
                    if self._output_belongs_to_group(path.name, group_name) and probe.status is _EspStatus.UNREADABLE
                ]
                if unreadable_before:
                    errors.append(
                        f"Synthesis output for group {group_name!r} could not be inspected before the run; "
                        "the output is not attributable to this run"
                    )
                unreadable_after = [path for path in group_paths if probe_after[path].status is _EspStatus.UNREADABLE]
                if unreadable_after:
                    errors.append(f"Synthesis output for group {group_name!r} could not be inspected after the run")
                absent_after = [path for path in group_paths if probe_after[path].status is _EspStatus.ABSENT]
                if absent_after:
                    errors.append(f"Synthesis output for group {group_name!r} disappeared during inspection")
                base_output = next(
                    (
                        path
                        for path in group_paths
                        if path.name.casefold() == f"{group_name}.esp".casefold()
                        and probe_after[path].status is _EspStatus.PRESENT
                    ),
                    None,
                )
                if base_output is None:
                    errors.append(f"Synthesis base output {group_name}.esp is missing or unreadable")
                present_paths = [path for path in group_paths if probe_after[path].status is _EspStatus.PRESENT]
                if not present_paths:
                    continue
                unchanged = [
                    path
                    for path in present_paths
                    if path in probe_before
                    and probe_before[path].status is _EspStatus.PRESENT
                    and probe_after[path].digest == probe_before[path].digest
                ]
                if unchanged:
                    errors.append(
                        f"Synthesis output for group {group_name!r} was not modified by this run: "
                        + ", ".join(path.name for path in unchanged)
                    )
                if not any(
                    path not in probe_before
                    or probe_before[path].status is _EspStatus.ABSENT
                    or (
                        probe_before[path].status is _EspStatus.PRESENT
                        and probe_after[path].digest != probe_before[path].digest
                    )
                    for path in present_paths
                ):
                    errors.append(f"Synthesis output for group {group_name!r} is not attributable to this run")

            primary_output = next(
                (path for path in output_esps if path.name.casefold() == f"{profile.group_names[0]}.esp".casefold()),
                None,
            )
            success = return_code == 0 and not errors and primary_output is not None
            result = SynthesisResult(
                success=success,
                output_esp=primary_output if success else None,
                return_code=return_code,
                stdout=stdout,
                stderr=stderr,
                patchers_executed=patchers_executed,
                errors=errors,
                output_esps=output_esps,
            )

            if result.success:
                logger.info(
                    "Pipeline Synthesis exitoso: %d archivo(s) de salida (%d patchers)",
                    len(result.output_esps),
                    len(patchers_executed),
                )
            else:
                logger.error(
                    "Pipeline Synthesis falló (code %d): %s",
                    return_code,
                    "; ".join(errors) if errors else "Unknown error",
                )
            return result
        except BaseException as exc:
            process_error = exc
            raise
        finally:
            cleanup_errors: list[str] = []
            for temporary_path in (temporary_load_order, temporary_settings):
                if temporary_path is None:
                    continue
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    cleanup_errors.append(f"{temporary_path}: {cleanup_error}")
            if ephemeral_temp_dir is not None:
                try:
                    await asyncio.to_thread(rmtree_link_aware, ephemeral_temp_dir, limpiar_readonly=True)
                except OSError as cleanup_error:
                    cleanup_errors.append(f"{ephemeral_temp_dir}: {cleanup_error}")
            if cleanup_errors:
                message = "Could not remove temporary Synthesis input(s): " + "; ".join(cleanup_errors)
                # Best-effort: los temporales viven en %TEMP%; no degradan el
                # veredicto de un run que ya terminó exitosamente.
                logger.warning(message)
                if process_error is not None:
                    process_error.add_note(message)

    @staticmethod
    def _output_belongs_to_group(filename: str, group_name: str) -> bool:
        pattern = re.compile(rf"^{re.escape(group_name)}(?:_([2-9]|[1-9][0-9]+))?\.esp$", re.IGNORECASE)
        return pattern.fullmatch(filename) is not None

    def _build_cli_args(
        self,
        patcher_ids: list[str],
        extra_args: list[str] | None = None,
        *,
        pipeline_settings_path: pathlib.Path | None = None,
        load_order_file_path: pathlib.Path | None = None,
    ) -> list[str]:
        """Construye el argv oficial de ``Synthesis.Bethesda.CLI run-pipeline``."""
        settings_path = pipeline_settings_path or self._config.pipeline_settings_path
        if settings_path is None:
            raise SynthesisExecutionError("Synthesis PipelineSettingsPath is required")
        load_order_path = load_order_file_path or self._config.load_order_file_path

        args: list[str] = [
            str(self._config.synthesis_exe),
            "run-pipeline",
            "--OutputDirectory",
            str(self._config.output_path),
            "--PipelineSettingsPath",
            str(settings_path),
            "--ProfileIdentifier",
            self._config.profile,
        ]
        if self._config.data_folder_path is not None:
            args.extend(["--DataFolderPath", str(self._config.data_folder_path)])
        if load_order_path is not None:
            args.extend(["--LoadOrderFilePath", str(load_order_path)])
        if self._config.extra_data_folder is not None:
            args.extend(["--ExtraDataFolder", str(self._config.extra_data_folder)])
        if self._config.persistence_mode == "Text":
            assert self._config.persistence_path is not None
            args.extend(
                [
                    "--PersistenceMode",
                    "Text",
                    "--PersistencePath",
                    str(self._config.persistence_path),
                ]
            )
        if self._config.target_runtime is not None:
            args.extend(["--TargetRuntime", self._config.target_runtime])
        if self._config.block_building_within_mo2:
            args.append("--BlockBuildingWithinMo2")
        if self._config.shortcircuit is True:
            args.append("--Shortcircuit")
        if extra_args:
            protected_flags = {
                "--blockbuildingwithinmo2",
                "--datapath",
                "--datafolderpath",
                "--extradatafolder",
                "--game-path",
                "--loadorderfilepath",
                "--no-prompt",
                "--output-path",
                "--outputdirectory",
                "--patcher",
                "--persistencemode",
                "--persistencepath",
                "--pipelinesettingspath",
                "--profile",
                "--profileidentifier",
                "--shortcircuit",
                "--targetruntime",
            }
            # Alias cortos de CommandLineParser (`-o`, `-s`, `-d`, `-p`, `-l`, `-e`, `-r`, `-m`, `-t`);
            # también en forma pegada (`-svalue`, `-e/x`, `-mNone`).
            protected_short = ("-o", "-s", "-d", "-p", "-l", "-e", "-r", "-m", "-t")
            for argument in extra_args:
                option = argument.partition("=")[0].casefold()
                if option in protected_flags or option.startswith(protected_short):
                    raise SynthesisExecutionError(f"extra_args cannot override managed Synthesis option {argument!r}")
            args.extend(extra_args)

        logger.debug("CLI args: %s", " ".join(args))
        return args

    def _build_subprocess_env(
        self,
        *,
        temp_dir_override: pathlib.Path | None = None,
    ) -> dict[str, str]:
        """Entorno del CLI: delega en :func:`construir_entorno_dotnet` (única lista blanca)."""
        effective_temp = temp_dir_override if temp_dir_override is not None else self._config.temp_dir
        return construir_entorno_dotnet(dotnet_root=self._config.dotnet_root, temp_dir=effective_temp)

    async def _execute_process(
        self,
        args: list[str],
        *,
        temp_dir_override: pathlib.Path | None = None,
    ) -> tuple[str, str, int]:
        """
        Ejecuta el proceso de Synthesis y captura su salida.

        Patrón tomado de xedit/runner.py:975-1021 y endurecido con Job Object
        obligatorio, límite de bytes y entorno mínimo (ADR 0019).

        Args:
            args: Argumentos del comando a ejecutar.

        Returns:
            Tupla con (stdout, stderr, return_code).

        Raises:
            SynthesisNotFoundError: Si el ejecutable no existe.
            SynthesisTimeoutError: Si excede el timeout.
        """
        cwd = str(self._config.working_dir) if self._config.working_dir is not None else None
        try:
            stdout, stderr, returncode = await run_capture(
                args,
                timeout=self._config.timeout_seconds,
                cwd=cwd,
                env=self._build_subprocess_env(temp_dir_override=temp_dir_override),
                require_job=True,
                max_output_bytes=self._config.max_output_bytes,
            )
        except FileNotFoundError:
            raise SynthesisNotFoundError(self._config.synthesis_exe) from None
        except TimeoutError:
            raise SynthesisTimeoutError(self._config.timeout_seconds) from None

        stdout_text = stdout.decode(errors="replace")
        stderr_text = stderr.decode(errors="replace")

        return stdout_text, stderr_text, returncode

    def parse_output(
        self,
        stdout: str,
        stderr: str,
    ) -> tuple[bool, list[str], list[str]]:
        """
        Parsea salida para detectar éxito/fallo y patchers ejecutados.

        Args:
            stdout: Salida estándar del proceso.
            stderr: Salida de error del proceso.

        Returns:
            Tupla con (success, patchers_executed, errors).
        """
        combined_output = stdout + "\n" + stderr

        # Detectar patchers ejecutados (ConsoleReporter real + patrón genérico legacy)
        patchers_executed: list[str] = []
        for match in self._CONSOLE_SUCCESS_PATTERN.finditer(combined_output):
            patcher_name = match.group(1).strip()
            if patcher_name and patcher_name not in patchers_executed:
                patchers_executed.append(patcher_name)
        for match in self._PATCHER_PATTERN.finditer(combined_output):
            patcher_name = match.group(1)
            if patcher_name not in patchers_executed:
                patchers_executed.append(patcher_name)

        # Detectar errores (ConsoleReporter / ErrorClassifier / MSBuild / Roslyn + genéricos)
        errors: list[str] = []
        for match in self._ERROR_PATTERN.finditer(combined_output):
            captured = match.group(0).strip()
            if captured and captured not in errors:
                errors.append(captured)

        # Determinar éxito
        success = bool(self._SUCCESS_PATTERN.search(combined_output))

        # Si hay errores detectados, el éxito es False
        if errors:
            success = False

        logger.debug(
            "Parse output: success=%s, patchers=%d, errors=%d",
            success,
            len(patchers_executed),
            len(errors),
        )

        return success, patchers_executed, errors

    async def validate_synthesis_esp(self, esp_path: pathlib.Path) -> bool:
        """Valida que el ESP generado sea un plugin TES4 íntegro vía parser canónico.

        Delega la lectura binaria en :func:`read_plugin_header` (anclado por
        ``tests/test_tes4_parser_invariant.py``):
        - Existe como archivo regular (nunca symlink).
        - Tamaño >= 100 bytes.
        - Cabecera TES4 y subrecords ``MAST`` legibles sin truncado.
        - Conteo de masters <= 254 (límite 0xFE del motor / Mutagen).
        """
        logger.debug("Validando ESP: %s", esp_path)

        try:
            loop = asyncio.get_running_loop()

            def _check_esp() -> tuple[bool, str | int]:
                if esp_path.is_symlink():
                    return False, "ESP es un enlace simbólico"
                if not esp_path.is_file():
                    return False, "ESP no existe o no es un archivo regular"
                file_size = esp_path.stat().st_size
                if file_size < 100:
                    return False, f"ESP muy pequeño ({file_size} bytes)"
                try:
                    header = read_plugin_header(esp_path)
                except PluginHeaderError as exc:
                    return False, str(exc)
                if len(header.masters) > _MAX_TES4_MASTERS:
                    return (
                        False,
                        f"ESP excede el límite de {_MAX_TES4_MASTERS} masters ({len(header.masters)})",
                    )
                return True, file_size

            valid, result = await loop.run_in_executor(None, _check_esp)

            if not valid:
                logger.warning("Error validando ESP %s: %s", esp_path, result)
                return False

            logger.info("ESP validado correctamente: %s (%s bytes)", esp_path, result)
            return True

        except OSError as e:
            logger.error("Error de I/O validando ESP %s: %s", esp_path, e)
            return False
        except RuntimeError as e:
            logger.exception("Error inesperado validando ESP %s: %s", esp_path, e)
            return False
