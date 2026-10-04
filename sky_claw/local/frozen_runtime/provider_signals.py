"""Señales advisory del proveedor Steam para el gate de estabilidad (P1).

Regla del ADR: observaciones específicas del proveedor sobre un contrato
independiente del proveedor. Ninguna señal de acá es autoridad: el árbol
(inventario PRE/POST sellado) es la evidencia primaria; estas señales son
evidencia auxiliar y fail-closed.

Heurísticas documentadas (advisory; no es un contrato de Valve):

- ``StateFlags`` distinto de ``"4"`` ⇒ el cliente no reporta "fully installed".
- ``BytesToDownload > BytesDownloaded`` ⇒ descarga en curso.
- ``UpdateResult`` distinto de ``""``/``"0"`` ⇒ el cliente reporta un resultado
  de actualización pendiente o no exitoso.
- ``steamapps/downloading/<appid>`` no vacío ⇒ actividad de descarga.
- ``steamapps/temp/<appid>`` no vacío ⇒ staging temporal activo.

Lecturas ESTRICTAMENTE read-only: nunca se modifica el manifest ni estado del
proveedor (SFR-11). La respuesta a actividad es UNSTABLE/esperar, nunca
bloquear o matar Steam.
"""

from __future__ import annotations

import os
import pathlib
import time

from sky_claw.local.frozen_runtime._vdf import parse_vdf_file
from sky_claw.local.frozen_runtime.errors import MalformedVdfError
from sky_claw.local.frozen_runtime.models import (
    ManagedSource,
    ProviderActivitySignals,
    ProviderMetadataObservation,
)

_IDLE_STATE_FLAGS = "4"


def manifest_path_for(source: ManagedSource) -> pathlib.Path:
    """Ruta canónica del ``appmanifest_<appid>.acf`` de la library del source."""
    return source.library_steamapps / f"appmanifest_{source.appid}.acf"


def _str_or_none(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _int_or_none(data: dict[str, object], key: str) -> int | None:
    value = _str_or_none(data, key)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def read_steam_manifest_observation(
    source: ManagedSource, *, observed_at_ns: int | None = None
) -> ProviderMetadataObservation:
    """Lee (sólo lectura) el manifest de Steam y lo reduce a metadata advisory.

    Un manifest ausente o malformado NO lanza: se registra explícitamente como
    ``manifest_readable=False`` con su motivo; el caller decide si eso es
    INDETERMINATE para su gate (la estabilidad sí lo exige).
    """
    ts = observed_at_ns if observed_at_ns is not None else time.time_ns()
    path = manifest_path_for(source)
    if not path.is_file():
        return ProviderMetadataObservation(
            provider=source.provider,
            appid=source.appid,
            manifest_path=path,
            manifest_readable=False,
            manifest_parse_error="el manifest no existe",
            observed_at_ns=ts,
        )
    try:
        data = parse_vdf_file(path)
    except MalformedVdfError as exc:
        return ProviderMetadataObservation(
            provider=source.provider,
            appid=source.appid,
            manifest_path=path,
            manifest_readable=False,
            manifest_parse_error=str(exc),
            observed_at_ns=ts,
        )
    # En el ACF real los campos viven dentro de "AppState".
    fields: dict[str, object] = data
    app_state = data.get("AppState")
    if isinstance(app_state, dict):
        fields = app_state
    return ProviderMetadataObservation(
        provider=source.provider,
        appid=source.appid,
        buildid=_str_or_none(fields, "buildid"),
        state_flags=_str_or_none(fields, "StateFlags"),
        bytes_to_download=_int_or_none(fields, "BytesToDownload"),
        bytes_downloaded=_int_or_none(fields, "BytesDownloaded"),
        update_result=_str_or_none(fields, "UpdateResult"),
        install_dir=_str_or_none(fields, "installdir"),
        manifest_path=path,
        manifest_readable=True,
        observed_at_ns=ts,
    )


def _dir_nonempty(path: pathlib.Path) -> bool:
    """True si el directorio existe y contiene entradas (advisory, fail-closed).

    Directorio inexistente ⇒ False (sin señal). Directorio existente pero
    ilegible, o ruta existente que no es directorio ⇒ True (estado inesperado
    se trata como actividad, nunca como quietud).
    """
    if not path.exists():
        return False
    if not path.is_dir():
        return True
    try:
        with os.scandir(path) as entries:
            for _ in entries:
                return True
    except OSError:
        return True
    return False


def observe_provider_activity(source: ManagedSource, *, observed_at_ns: int | None = None) -> ProviderActivitySignals:
    """Observa (sólo lectura) todas las señales advisory de actividad de Steam."""
    ts = observed_at_ns if observed_at_ns is not None else time.time_ns()
    metadata = read_steam_manifest_observation(source, observed_at_ns=ts)
    downloading_dir = source.library_steamapps / "downloading" / source.appid
    temp_dir = source.library_steamapps / "temp" / source.appid
    return ProviderActivitySignals(
        provider=source.provider,
        manifest_readable=metadata.manifest_readable,
        manifest_parse_error=metadata.manifest_parse_error,
        state_flags=metadata.state_flags,
        bytes_to_download=metadata.bytes_to_download,
        bytes_downloaded=metadata.bytes_downloaded,
        update_result=metadata.update_result,
        buildid=metadata.buildid,
        downloading_dir_nonempty=_dir_nonempty(downloading_dir),
        temp_dir_nonempty=_dir_nonempty(temp_dir),
        observed_at_ns=ts,
    )
