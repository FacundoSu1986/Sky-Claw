"""
Pipeline de Parcheo para Mutagen Synthesis.

Este módulo define la configuración y gestión del pipeline de patchers
que se ejecutan con Synthesis CLI.

Reference:
    - Synthesis CLI: https://github.com/Mutagen-Modding/Synthesis
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import pathlib
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sky_claw.app.security.path_validator import PathValidator

from sky_claw.app.security.path_validator import PathViolationError

logger = logging.getLogger(__name__)


# =============================================================================
# EXCEPTIONS
# =============================================================================


class PatcherPipelineError(Exception):
    """Error en la configuración del pipeline de patchers."""

    pass


def _is_split_output_name(group_name: str, candidate: str) -> bool:
    """Si ``candidate`` colisiona con el espacio ``<grupo>_N`` de splits."""
    prefix = f"{group_name.casefold()}_"
    candidate_folded = candidate.casefold()
    if not candidate_folded.startswith(prefix):
        return False
    suffix = candidate_folded[len(prefix) :]
    if not suffix.isascii() or not suffix.isdigit():
        return False
    # Match the runner's ``_2``, ``_3``, … ``_10`` pattern without converting
    # an unbounded settings string to int (Python caps decimal conversions).
    return (len(suffix) == 1 and suffix in "23456789") or (len(suffix) > 1 and suffix[0] in "123456789")


class PatcherNotFoundError(PatcherPipelineError):
    """Error cuando un patcher no se encuentra en el pipeline."""

    def __init__(self, patcher_id: str) -> None:
        super().__init__(f"Patcher not found in pipeline: {patcher_id}")
        self.patcher_id = patcher_id


class PatcherConfigError(PatcherPipelineError):
    """Error en la configuración de un patcher."""

    def __init__(self, patcher_id: str, reason: str) -> None:
        super().__init__(f"Invalid config for patcher '{patcher_id}': {reason}")
        self.patcher_id = patcher_id
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SynthesisSettingsProfile:
    """Vista validada del perfil V2 que consume ``Synthesis.CLI``.

    ``patcher_ids`` incluye solo patchers habilitados en grupos habilitados y
    ``group_names`` solo grupos que efectivamente ejecutarán al menos uno. El
    conteo de masters del load order no equivale al fan-in real de un patcher;
    ``split_if_max_masters_exceeded`` conserva el bit efectivo del perfil. Si
    falta, se aplica el default upstream 0.36.6 (``True``); solo un valor
    presente pero no booleano queda como desconocido (``None``) y fail-closed.
    """

    profile_identifier: str
    patcher_ids: tuple[str, ...]
    group_names: tuple[str, ...]
    split_if_max_masters_exceeded: bool | None


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass(frozen=True, slots=True)
class PatcherDefinition:
    """
    Definición de un patcher de Mutagen Synthesis.

    Attributes:
        patcher_id: ID único del patcher (ej: "LeveledListsPatcher").
        enabled: Si el patcher está habilitado para ejecución.
        order: Orden de ejecución (menor = antes).
        config: Configuración específica del patcher (key-value).
    """

    patcher_id: str
    enabled: bool = True
    order: int = 0
    config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Valida que el patcher_id no esté vacío."""
        if not self.patcher_id or not self.patcher_id.strip():
            raise PatcherConfigError("", "patcher_id cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        """
        Serializa el patcher a diccionario.

        Returns:
            Diccionario con los datos del patcher.
        """
        return {
            "patcher_id": self.patcher_id,
            "enabled": self.enabled,
            "order": self.order,
            "config": self.config,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PatcherDefinition:
        """
        Crea un PatcherDefinition desde un diccionario.

        Args:
            data: Diccionario con los datos del patcher.

        Returns:
            Nueva instancia de PatcherDefinition.

        Raises:
            PatcherConfigError: Si faltan campos requeridos.
        """
        if "patcher_id" not in data:
            raise PatcherConfigError("", "missing required field: patcher_id")

        return cls(
            patcher_id=data["patcher_id"],
            enabled=data.get("enabled", True),
            order=data.get("order", 0),
            config=data.get("config", {}),
        )


# =============================================================================
# PATCHER PIPELINE
# =============================================================================


class PatcherPipeline:
    """
    Gestiona el pipeline de patchers para Synthesis.

    El pipeline mantiene una lista ordenada de patchers con sus configuraciones,
    y puede serializarse para consumo de Synthesis CLI.

    Usage:
        # Crear pipeline desde cero
        pipeline = PatcherPipeline()
        pipeline.add_patcher(PatcherDefinition(
            patcher_id="LeveledListsPatcher",
            enabled=True,
            order=1,
        ))

        # Guardar configuración
        pipeline.to_json(Path("synthesis_pipeline.json"))

        # Cargar desde archivo
        pipeline = PatcherPipeline.from_json(Path("synthesis_pipeline.json"))

        # Obtener patchers habilitados para Synthesis
        enabled = pipeline.get_enabled_patchers()
    """

    def __init__(
        self,
        pipeline_config_path: pathlib.Path | None = None,
        path_validator: PathValidator | None = None,
    ) -> None:
        """
        Inicializa el pipeline de patchers.

        Args:
            pipeline_config_path: Path opcional al archivo de configuración.
            path_validator: Optional PathValidator for sandbox enforcement.
        """
        self._patchers: dict[str, PatcherDefinition] = {}
        self._config_path = pipeline_config_path
        self._path_validator = path_validator

        # Cargar configuración inicial si existe
        if pipeline_config_path and pipeline_config_path.exists():
            try:
                self._load_from_path(pipeline_config_path)
                logger.info(
                    "Pipeline cargado desde %s: %d patchers",
                    pipeline_config_path,
                    len(self._patchers),
                )
            # Acotado (T-10): un config corrupto degrada a pipeline vacío, pero
            # PathViolationError debe PROPAGAR (S2-FIX) — el except Exception
            # previo la tragaba. KeyError/TypeError/ValueError cubren from_dict
            # y json.JSONDecodeError; OSError la lectura del archivo.
            except (OSError, ValueError, KeyError, TypeError, PatcherPipelineError) as e:
                logger.warning(
                    "Error cargando pipeline desde %s: %s. Iniciando vacío.",
                    pipeline_config_path,
                    e,
                )

    @staticmethod
    def _read_synthesis_profile(
        settings_path: pathlib.Path,
        profile_identifier: str,
    ) -> tuple[
        dict[str, Any],
        dict[str, Any],
        list[tuple[int, dict[str, Any], str, list[tuple[int, dict[str, Any], str, tuple[str, ...]]]]],
    ]:
        """Lee y valida la vista V2 que ``run-pipeline`` realmente consume.

        El esquema de settings sigue la convención canónica PascalCase de Synthesis
        0.36.6 (``Profiles``, ``Nickname``, ``ID``, ``Groups``, ``Name``, ``Patchers``).
        La resolución de ``profile_identifier`` contra ``Nickname`` o ``ID`` es
        insensible a mayúsculas (fail-closed ante ambigüedades), mientras que las claves
        JSON siguen el contrato upstream.
        """
        if not isinstance(profile_identifier, str) or not profile_identifier.strip():
            raise PatcherPipelineError("Synthesis profile identifier must be a non-empty string")

        try:
            raw_settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PatcherPipelineError(f"Cannot read Synthesis pipeline settings at {settings_path}: {exc}") from exc

        if not isinstance(raw_settings, dict):
            raise PatcherPipelineError("Synthesis pipeline settings must be a JSON object")
        version = raw_settings.get("Version", 2)
        if version != 2:
            raise PatcherPipelineError(f"Synthesis CLI settings version 2 is required (found {version!r})")

        raw_profiles = raw_settings.get("Profiles")
        if not isinstance(raw_profiles, list):
            raise PatcherPipelineError("Synthesis pipeline settings must contain a Profiles array")

        identifier = profile_identifier.casefold()
        matches: list[dict[str, Any]] = []
        for candidate in raw_profiles:
            if not isinstance(candidate, dict):
                raise PatcherPipelineError("Every Synthesis profile must be a JSON object")
            aliases = (candidate.get("Nickname"), candidate.get("ID"))
            if any(isinstance(alias, str) and alias.casefold() == identifier for alias in aliases):
                matches.append(candidate)
        if len(matches) != 1:
            reason = "not found" if not matches else "ambiguous"
            raise PatcherPipelineError(f"Synthesis profile {profile_identifier!r} is {reason} in {settings_path}")
        profile = matches[0]
        if profile.get("ExportAsMasterFiles") is True:
            raise PatcherPipelineError("Synthesis ExportAsMasterFiles is unsupported; Sky-Claw manages ESP outputs")

        raw_groups = profile.get("Groups")
        if not isinstance(raw_groups, list):
            raise PatcherPipelineError("Synthesis profile must contain a V2 Groups array")

        # (group index, group, output base name, [(patcher index, patcher, canonical id, aliases)])
        active_groups: list[
            tuple[int, dict[str, Any], str, list[tuple[int, dict[str, Any], str, tuple[str, ...]]]]
        ] = []
        seen_group_names: set[str] = set()
        for group_index, group in enumerate(raw_groups):
            if not isinstance(group, dict):
                raise PatcherPipelineError("Every Synthesis patcher group must be a JSON object")
            if group.get("On") is not True:
                continue
            name = group.get("Name")
            if not isinstance(name, str) or not name.strip():
                raise PatcherPipelineError("An enabled Synthesis group has no valid output name")
            if (
                name != name.strip()
                or name in {".", ".."}
                or name.endswith((".", " "))
                or any(char in name for char in '<>:\\"/\\\\|?*')
                or any(ord(char) < 32 for char in name)
            ):
                raise PatcherPipelineError(f"Unsafe Synthesis output group name: {name!r}")
            folded_name = name.casefold()
            if folded_name in seen_group_names:
                raise PatcherPipelineError(f"Duplicate Synthesis output group name: {name!r}")
            seen_group_names.add(folded_name)

            raw_patchers = group.get("Patchers")
            if not isinstance(raw_patchers, list):
                raise PatcherPipelineError(f"Enabled Synthesis group {name!r} must contain a Patchers array")
            enabled_patchers: list[tuple[int, dict[str, Any], str, tuple[str, ...]]] = []
            for patcher_index, patcher in enumerate(raw_patchers):
                if not isinstance(patcher, dict):
                    raise PatcherPipelineError(f"A patcher in Synthesis group {name!r} is not a JSON object")
                if patcher.get("On") is not True:
                    continue
                id_value = patcher.get("ID")
                nickname = patcher.get("Nickname")
                aliases_list: list[str] = []
                seen_aliases: set[str] = set()
                for value in (id_value, nickname):
                    if isinstance(value, str) and value.strip():
                        alias = value.strip()
                        if alias.casefold() not in seen_aliases:
                            aliases_list.append(alias)
                            seen_aliases.add(alias.casefold())
                aliases = tuple(aliases_list)
                if not aliases:
                    raise PatcherPipelineError(f"An enabled patcher in group {name!r} has neither ID nor Nickname")
                canonical_id = id_value.strip() if isinstance(id_value, str) and id_value.strip() else aliases[0]
                enabled_patchers.append((patcher_index, patcher, canonical_id, aliases))

            if enabled_patchers:
                active_groups.append((group_index, group, name, enabled_patchers))

        if not active_groups:
            raise PatcherPipelineError(f"Synthesis profile {profile_identifier!r} has no enabled patchers")
        group_names = [name for _, _, name, _ in active_groups]
        for index, group_name in enumerate(group_names):
            for other_name in group_names[index + 1 :]:
                if _is_split_output_name(group_name, other_name) or _is_split_output_name(other_name, group_name):
                    raise PatcherPipelineError(
                        f"Synthesis group names {group_name!r} and {other_name!r} overlap split output filenames"
                    )
        return raw_settings, profile, active_groups

    @staticmethod
    def _select_synthesis_entries(
        active_groups: list[tuple[int, dict[str, Any], str, list[tuple[int, dict[str, Any], str, tuple[str, ...]]]]],
        patcher_ids: list[str] | None,
    ) -> list[tuple[int, int, str, tuple[str, ...]]]:
        """Resuelve selectores conservando la identidad de cada entrada V2."""
        entries: list[tuple[int, int, str, tuple[str, ...]]] = []
        alias_to_entry: dict[str, list[int]] = {}
        for group_index, _, _, patchers in active_groups:
            for patcher_index, _, canonical_id, aliases in patchers:
                entry_index = len(entries)
                entries.append((group_index, patcher_index, canonical_id, aliases))
                for alias in aliases:
                    alias_to_entry.setdefault(alias.casefold(), []).append(entry_index)

        if patcher_ids is None:
            selected_entry_indices = set(range(len(entries)))
        else:
            if not patcher_ids:
                raise PatcherPipelineError("At least one Synthesis patcher must be selected")
            selected_entry_indices = set()
            requested: set[str] = set()
            for patcher_id in patcher_ids:
                if not isinstance(patcher_id, str) or not patcher_id.strip():
                    raise PatcherPipelineError("Synthesis patcher IDs must be non-empty strings")
                folded_id = patcher_id.strip().casefold()
                if folded_id in requested:
                    raise PatcherPipelineError(f"Duplicate Synthesis patcher selection: {patcher_id!r}")
                requested.add(folded_id)
                matches = alias_to_entry.get(folded_id, [])
                if not matches:
                    raise PatcherPipelineError(
                        f"Selected patcher {patcher_id!r} is not enabled in the Synthesis profile"
                    )
                if len(matches) != 1:
                    raise PatcherPipelineError(f"Selected patcher {patcher_id!r} is ambiguous in the Synthesis profile")
                entry_index = matches[0]
                if entry_index in selected_entry_indices:
                    raise PatcherPipelineError(f"Multiple selectors refer to the same patcher: {patcher_id!r}")
                selected_entry_indices.add(entry_index)

        return [entry for index, entry in enumerate(entries) if index in selected_entry_indices]

    @classmethod
    def inspect_synthesis_settings(
        cls,
        settings_path: pathlib.Path,
        profile_identifier: str,
        patcher_ids: list[str] | None = None,
    ) -> SynthesisSettingsProfile:
        """Valida perfil/patchers y devuelve solo las unidades que se ejecutarían.

        ``patcher_ids`` acepta el ID o el nickname de cada patcher. Cuando es
        ``None``, se seleccionan todos los patchers actualmente habilitados en
        los grupos habilitados. Una selección desconocida o ambigua falla antes
        de lanzar el proceso externo.
        """
        _, profile, active_groups = cls._read_synthesis_profile(settings_path, profile_identifier)
        selected_entries = cls._select_synthesis_entries(active_groups, patcher_ids)
        selected_group_indices = {entry[0] for entry in selected_entries}
        group_names = tuple(name for index, _, name, _ in active_groups if index in selected_group_indices)
        selected_patcher_ids = tuple(entry[2] for entry in selected_entries)
        # Synthesis 0.36.6 model default: SplitIfMaxMastersExceeded = true.
        # Preserve fail-closed behavior for a malformed, explicitly present value.
        split_value = profile.get("SplitIfMaxMastersExceeded", True)
        split_enabled = split_value if type(split_value) is bool else None
        return SynthesisSettingsProfile(
            profile_identifier=profile_identifier,
            patcher_ids=selected_patcher_ids,
            group_names=group_names,
            split_if_max_masters_exceeded=split_enabled,
        )

    @classmethod
    def prepare_synthesis_cli_settings(
        cls,
        settings_path: pathlib.Path,
        profile_identifier: str,
        patcher_ids: list[str] | None = None,
    ) -> tuple[pathlib.Path, SynthesisSettingsProfile]:
        """Materializa un settings temporal V2 sin mutar el archivo del usuario.

        El CLI recibe solo el perfil/patchers seleccionados. Además se desactiva
        ``UpdateLoadOrderAfterRun`` en la copia de ejecución: Sky-Claw no delega
        al proceso externo una escritura no transaccional sobre el perfil MO2.
        El caller es dueño de borrar el archivo temporal que devuelve.
        """
        raw_settings, profile, active_groups = cls._read_synthesis_profile(settings_path, profile_identifier)
        selected_entries = cls._select_synthesis_entries(active_groups, patcher_ids)
        selected_entry_keys = {(group_index, patcher_index) for group_index, patcher_index, _, _ in selected_entries}
        selected_group_indices = {group_index for group_index, _, _, _ in selected_entries}
        profile_info = SynthesisSettingsProfile(
            profile_identifier=profile_identifier,
            patcher_ids=tuple(entry[2] for entry in selected_entries),
            group_names=tuple(name for index, _, name, _ in active_groups if index in selected_group_indices),
            split_if_max_masters_exceeded=(
                profile.get("SplitIfMaxMastersExceeded", True)
                if type(profile.get("SplitIfMaxMastersExceeded", True)) is bool
                else None
            ),
        )

        selected_groups: list[dict[str, Any]] = []
        for group_index, group, group_name, patchers in active_groups:
            chosen_patchers = [
                copy.deepcopy(patcher)
                for patcher_index, patcher, _, _ in patchers
                if (group_index, patcher_index) in selected_entry_keys
            ]
            if not chosen_patchers:
                continue
            group_copy = copy.deepcopy(group)
            group_copy["Name"] = group_name
            group_copy["On"] = True
            group_copy["Patchers"] = chosen_patchers
            selected_groups.append(group_copy)

        if not selected_groups:
            raise PatcherPipelineError("Selected Synthesis patchers do not map to an enabled output group")

        run_profile = copy.deepcopy(profile)
        run_profile["Groups"] = selected_groups
        # This side effect is not part of the output plugin transaction and
        # would bypass the caller's MO2-profile rollback/USVFS boundary.
        run_profile["UpdateLoadOrderAfterRun"] = False
        run_settings = copy.deepcopy(raw_settings)
        run_settings["Version"] = 2
        run_settings["Profiles"] = [run_profile]

        temporary_path: pathlib.Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix="skyclaw-synthesis-",
                suffix=".json",
                delete=False,
            ) as settings_file:
                temporary_path = pathlib.Path(settings_file.name)
                json.dump(run_settings, settings_file, indent=2, ensure_ascii=False)
                settings_file.write("\n")
                settings_file.flush()
                os.fsync(settings_file.fileno())
        except OSError as exc:
            if temporary_path is not None:
                with contextlib.suppress(OSError):
                    temporary_path.unlink(missing_ok=True)
            raise PatcherPipelineError(f"Cannot prepare temporary Synthesis settings: {exc}") from exc

        assert temporary_path is not None
        return temporary_path, profile_info

    def _load_from_path(self, path: pathlib.Path) -> None:
        """
        Carga el pipeline desde un archivo JSON.

        Args:
            path: Path al archivo JSON.

        Raises:
            PatcherPipelineError: Si el archivo está corrupto.
        """
        # S2-FIX: Validate path before opening.
        if self._path_validator is not None:
            try:
                self._path_validator.validate(path, strict_symlink=False)
            except PathViolationError:
                logger.error("Path traversal blocked for pipeline load: %s", path)
                raise

        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise PatcherPipelineError("Pipeline config must be a JSON object")

        patchers_data = data.get("patchers", [])
        if not isinstance(patchers_data, list):
            raise PatcherPipelineError("'patchers' must be a list")

        for patcher_data in patchers_data:
            patcher = PatcherDefinition.from_dict(patcher_data)
            self._patchers[patcher.patcher_id] = patcher

    def add_patcher(self, patcher: PatcherDefinition) -> None:
        """
        Añade un patcher al pipeline.

        Si ya existe un patcher con el mismo ID, se reemplaza.

        Args:
            patcher: Definición del patcher a añadir.
        """
        existing = self._patchers.get(patcher.patcher_id)
        self._patchers[patcher.patcher_id] = patcher

        if existing:
            logger.debug(
                "Patcher reemplazado: %s (order %d -> %d)",
                patcher.patcher_id,
                existing.order,
                patcher.order,
            )
        else:
            logger.debug(
                "Patcher añadido: %s (order %d, enabled=%s)",
                patcher.patcher_id,
                patcher.order,
                patcher.enabled,
            )

    def remove_patcher(self, patcher_id: str) -> bool:
        """
        Elimina un patcher del pipeline.

        Args:
            patcher_id: ID del patcher a eliminar.

        Returns:
            True si se eliminó, False si no existía.
        """
        if patcher_id in self._patchers:
            del self._patchers[patcher_id]
            logger.debug("Patcher eliminado: %s", patcher_id)
            return True

        logger.debug("Patcher no encontrado para eliminar: %s", patcher_id)
        return False

    def get_patcher(self, patcher_id: str) -> PatcherDefinition | None:
        """
        Obtiene un patcher por su ID.

        Args:
            patcher_id: ID del patcher.

        Returns:
            PatcherDefinition o None si no existe.
        """
        return self._patchers.get(patcher_id)

    def get_enabled_patchers(self) -> list[PatcherDefinition]:
        """
        Obtiene la lista de patchers habilitados, ordenados por orden de ejecución.

        Returns:
            Lista de PatcherDefinition habilitados, ordenados por 'order'.
        """
        enabled = [p for p in self._patchers.values() if p.enabled]
        return sorted(enabled, key=lambda p: p.order)

    def get_all_patchers(self) -> list[PatcherDefinition]:
        """
        Obtiene todos los patchers del pipeline, ordenados por orden de ejecución.

        Returns:
            Lista de todos los PatcherDefinition, ordenados por 'order'.
        """
        return sorted(self._patchers.values(), key=lambda p: p.order)

    def enable_patcher(self, patcher_id: str) -> bool:
        """
        Habilita un patcher existente.

        Args:
            patcher_id: ID del patcher a habilitar.

        Returns:
            True si se habilitó, False si no existe.
        """
        patcher = self._patchers.get(patcher_id)
        if patcher is None:
            return False

        if not patcher.enabled:
            # Crear nueva instancia con enabled=True (frozen dataclass)
            self._patchers[patcher_id] = PatcherDefinition(
                patcher_id=patcher.patcher_id,
                enabled=True,
                order=patcher.order,
                config=patcher.config,
            )
            logger.debug("Patcher habilitado: %s", patcher_id)

        return True

    def disable_patcher(self, patcher_id: str) -> bool:
        """
        Deshabilita un patcher existente.

        Args:
            patcher_id: ID del patcher a deshabilitar.

        Returns:
            True si se deshabilitó, False si no existe.
        """
        patcher = self._patchers.get(patcher_id)
        if patcher is None:
            return False

        if patcher.enabled:
            # Crear nueva instancia con enabled=False (frozen dataclass)
            self._patchers[patcher_id] = PatcherDefinition(
                patcher_id=patcher.patcher_id,
                enabled=False,
                order=patcher.order,
                config=patcher.config,
            )
            logger.debug("Patcher deshabilitado: %s", patcher_id)

        return True

    def set_patcher_order(self, patcher_id: str, new_order: int) -> bool:
        """
        Cambia el orden de ejecución de un patcher.

        Args:
            patcher_id: ID del patcher.
            new_order: Nuevo orden de ejecución.

        Returns:
            True si se cambió, False si no existe.
        """
        patcher = self._patchers.get(patcher_id)
        if patcher is None:
            return False

        # Crear nueva instancia con el nuevo orden (frozen dataclass)
        self._patchers[patcher_id] = PatcherDefinition(
            patcher_id=patcher.patcher_id,
            enabled=patcher.enabled,
            order=new_order,
            config=patcher.config,
        )
        logger.debug("Orden de patcher cambiado: %s -> %d", patcher_id, new_order)
        return True

    def serialize_for_synthesis(self) -> dict[str, Any]:
        """
        Serializa configuración para consumo de Synthesis CLI.

        El formato de salida es compatible con los argumentos de Synthesis CLI,
        permitiendo pasar directamente los patchers habilitados.

        Returns:
            Diccionario con la configuración serializada.
        """
        enabled_patchers = self.get_enabled_patchers()

        return {
            "patchers": [p.patcher_id for p in enabled_patchers],
            "patcher_configs": {p.patcher_id: p.config for p in enabled_patchers if p.config},
            "total_enabled": len(enabled_patchers),
            "total_disabled": len(self._patchers) - len(enabled_patchers),
        }

    def to_dict(self) -> dict[str, Any]:
        """
        Serializa el pipeline completo a diccionario.

        Returns:
            Diccionario con todos los patchers del pipeline.
        """
        return {
            "version": 1,
            "patchers": [p.to_dict() for p in self.get_all_patchers()],
        }

    @classmethod
    def from_json(cls, json_path: pathlib.Path) -> PatcherPipeline:
        """
        Carga pipeline desde archivo JSON.

        Args:
            json_path: Path al archivo JSON de configuración.

        Returns:
            Nueva instancia de PatcherPipeline.

        Raises:
            FileNotFoundError: Si el archivo no existe.
            PatcherPipelineError: Si el archivo está corrupto.
        """
        if not json_path.exists():
            raise FileNotFoundError(f"Pipeline config not found: {json_path}")

        pipeline = cls(pipeline_config_path=json_path)
        return pipeline

    def to_json(self, json_path: pathlib.Path) -> None:
        """
        Guarda configuración del pipeline en archivo JSON.

        Args:
            json_path: Path donde guardar el archivo.
        """
        # S2-FIX: Validate path before writing.
        if self._path_validator is not None:
            try:
                self._path_validator.validate(json_path, strict_symlink=False)
            except PathViolationError:
                logger.error("Path traversal blocked for pipeline save: %s", json_path)
                raise

        data = self.to_dict()

        # Crear directorio padre si no existe
        json_path.parent.mkdir(parents=True, exist_ok=True)

        # Escritura atómica tmp->rename: un crash a mitad del dump dejaba el
        # JSON de config truncado/corrupto y el próximo from_json fallaba
        # (§2.1 reporte de consistencia). Se escribe a un tmp único en el mismo
        # dir (para que os.replace sea atómico — mismo filesystem) y se renombra
        # sobre el destino. Mismo patrón que vfs._write_modlist_atomic.
        tmp = json_path.parent / f".{json_path.name}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, json_path)
        except Exception:
            # Limpiar el tmp huérfano si el dump o el rename fallan.
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise

        logger.info(
            "Pipeline guardado en %s: %d patchers",
            json_path,
            len(self._patchers),
        )

    def __len__(self) -> int:
        """Retorna el número total de patchers en el pipeline."""
        return len(self._patchers)

    def __contains__(self, patcher_id: str) -> bool:
        """Verifica si un patcher está en el pipeline."""
        return patcher_id in self._patchers

    def __repr__(self) -> str:
        """Representación del pipeline."""
        enabled = len(self.get_enabled_patchers())
        total = len(self._patchers)
        return f"PatcherPipeline(patchers={total}, enabled={enabled})"
