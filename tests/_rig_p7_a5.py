"""Rig sintético compartido de P7-A5 (Stock + Generation + Clone + MO2 + Steam).

Extraído del rig de ``tests/test_frozen_runtime_p7_a5_stock_manual_preflight.py``
(P7-A5 PR 1) para que los PRs siguientes no tengan que volver a construirlo ni
depender de la evolución de ese archivo de tests.

Todo el rig vive en ``tmp_path``: no toca Steam, ni MO2, ni Stock, ni el runtime
manual reales. Los binarios son sintéticos y la observación PE se inyecta.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
from collections.abc import Sequence

from sky_claw.local.frozen_runtime.binder_preflight import BinderTransitionPolicy
from sky_claw.local.frozen_runtime.clones import (
    RuntimeCloneLifecycle,
    RuntimeCloneRecord,
    registrar_runtime_clone_record,
)
from sky_claw.local.frozen_runtime.effective_runtime import (
    CompatibilityResult,
    LiveProcessSnapshot,
    Mo2RegistryObservation,
    Mo2RegistryState,
    RuntimeCompatibility,
    RuntimeProbes,
    probe_mo2_instance_identity,
)
from sky_claw.local.frozen_runtime.generations import (
    registrar_generation_metadata,
)
from sky_claw.local.frozen_runtime.models import (
    ManagedSourceProvider,
    ProviderMetadataObservation,
    SourceSnapshotEvidence,
)
from sky_claw.local.frozen_runtime.provisioning import (
    StockReference,
    adoptar_copia_stock,
    construir_referencia_stock,
    crear_runtime_clone_desde_generation,
    publicar_generation_desde_stock,
)
from sky_claw.local.frozen_runtime.storage import (
    generation_dir,
    initialize_frozen_runtime_storage,
)
from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.models import RuntimeIdentity
from sky_claw.local.runtime_vault.runtime_observation import (
    FreshRuntimeObservation,
    RuntimeObservationError,
)
from sky_claw.local.runtime_vault.verification import tree_digest_from_files

VERSION = "1.6.1170.0"


# ---------------------------------------------------------------------------
# Constructores de árboles sintéticos
# ---------------------------------------------------------------------------


def poblar_arbol_skyrim(
    raiz: pathlib.Path,
    *,
    version: str = VERSION,
    con_skse: bool = True,
    sello_extra: bytes = b"",
) -> pathlib.Path:
    """Árbol mínimo con identidad de Skyrim, SKSE del build exacto y un directorio vacío."""
    (raiz / "Data" / "EmptyFolder").mkdir(parents=True, exist_ok=True)
    (raiz / "SkyrimSE.exe").write_bytes(f"SkyrimSE-{version}".encode() + sello_extra)
    (raiz / "SkyrimSELauncher.exe").write_bytes(b"Launcher-sintetico" + sello_extra)
    (raiz / "steam_api64.dll").write_bytes(b"steam-api-sintetica" + sello_extra)
    (raiz / "Data" / "Skyrim.esm").write_bytes(b"esm-payload" + sello_extra)
    if con_skse:
        (raiz / "skse64_loader.exe").write_bytes(b"loader-sintetico" + sello_extra)
        (raiz / "skse64_1_6_1170.dll").write_bytes(b"skse-dll-sintetico" + sello_extra)
    return raiz


def crear_steam_sintetico(
    raiz_steam: pathlib.Path,
    *,
    state_flags: str = "4",
    con_descarga_activa: bool = False,
    appid: str = "489830",
    buildid: str = "13189953",
) -> pathlib.Path:
    """Managed Source sintética con appmanifest (``4`` = IDLE; ``1026`` = ACTIVE con descarga)."""
    steamapps = raiz_steam / "steamapps"
    common = steamapps / "common" / "Skyrim Special Edition"
    poblar_arbol_skyrim(common, sello_extra=b"-steam")
    manifest = steamapps / f"appmanifest_{appid}.acf"
    manifest.write_text(
        f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"buildid"\t\t"{buildid}"\n\t"StateFlags"\t\t"{state_flags}"\n}}\n',
        encoding="utf-8",
    )
    if con_descarga_activa:
        descarga = steamapps / "downloading" / appid
        descarga.mkdir(parents=True, exist_ok=True)
        (descarga / "depot.chunk").write_bytes(b"actualizacion-steam-activa")
    return common


def escribir_ini_mo2(
    ini_path: pathlib.Path,
    *,
    game_path: pathlib.Path,
    base_directory: pathlib.Path | None = None,
    ejecutables: Sequence[tuple[str | None, ...]] | None = None,
) -> None:
    """INI mínimo con ``gamePath`` en formato Qt y, opcionalmente, ``[customExecutables]``."""
    escaped = str(game_path).replace("\\", "\\\\")
    lineas = [
        "[General]",
        f"gamePath=@ByteArray({escaped})",
        "gameName=Skyrim Special Edition",
        "",
    ]
    if ejecutables:
        lineas.append("[customExecutables]")
        for idx, item in enumerate(ejecutables, start=1):
            titulo = item[0]
            binario = item[1]
            workdir = item[2] if len(item) > 2 else None
            args = item[3] if len(item) > 3 else None
            lineas.append(f"{idx}\\title={titulo}")
            lineas.append(f"{idx}\\binary={binario}")
            if workdir is not None:
                lineas.append(f"{idx}\\workingDirectory={workdir}")
            if args is not None:
                lineas.append(f"{idx}\\arguments={args}")
        lineas.append("")
    lineas.append("[Settings]")
    if base_directory is not None:
        lineas.append(f"base_directory={base_directory.as_posix()}")
    ini_path.write_text("\n".join(lineas) + "\n", encoding="utf-8")


def crear_mo2_global_sintetica(
    local_app_data: pathlib.Path,
    *,
    instance_name: str = "Skyrim Special Edition",
    game_path: pathlib.Path,
    base_directory: pathlib.Path | None = None,
    ejecutables: Sequence[tuple[str | None, ...]] | None = None,
) -> pathlib.Path:
    """Instancia GLOBAL de MO2 bajo ``%LOCALAPPDATA%`` (la que identifica HKCU)."""
    inst_dir = local_app_data / "ModOrganizer" / instance_name
    inst_dir.mkdir(parents=True, exist_ok=True)
    escribir_ini_mo2(
        inst_dir / "modorganizer.ini",
        game_path=game_path,
        base_directory=base_directory,
        ejecutables=ejecutables,
    )
    return inst_dir


def crear_config_toml(path: pathlib.Path, skyrim_path: str) -> pathlib.Path:
    """Config de Sky-Claw mínima con ``skyrim_path`` (sólo la clave que el binder lee)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    escaped = skyrim_path.replace("\\", "\\\\")
    path.write_text(f'skyrim_path = "{escaped}"\n', encoding="utf-8")
    return path


def probes_con_registro_simulado(
    *,
    local_app_data: pathlib.Path | None,
    registry_obs: Mo2RegistryObservation,
    compatibilidad: RuntimeCompatibility = RuntimeCompatibility.COMPATIBLE,
) -> RuntimeProbes:
    """Probes sintéticas: identidad PE por contenido, sin procesos vivos y registro simulado."""
    entorno_mo2 = {"LOCALAPPDATA": str(local_app_data)} if local_app_data is not None else {}

    def _identidad(raiz: pathlib.Path) -> RuntimeIdentity:
        exe = pathlib.Path(raiz) / "SkyrimSE.exe"
        if not exe.is_file():
            raise RuntimeObservationError(f"falta SkyrimSE.exe en '{raiz}'")
        contenido = exe.read_bytes()
        if not contenido.startswith(b"SkyrimSE-"):
            raise RuntimeObservationError(f"ejecutable no reconocido como Skyrim en '{raiz}'")
        return RuntimeIdentity(game_key="skyrimse", game_version=VERSION)

    return RuntimeProbes(
        identity=_identidad,
        compatibility=lambda _r: CompatibilityResult(
            state=compatibilidad,
            reason=f"sintético: {compatibilidad.value}",
            game_version=VERSION,
        ),
        processes=lambda: LiveProcessSnapshot(processes=(), indeterminate=False),
        mo2_instance=lambda inst_dir: probe_mo2_instance_identity(
            inst_dir,
            registry_reader=lambda: registry_obs,
            environ=entorno_mo2,
        ),
    )


def snapshot_sha256_recursivo(raices: list[pathlib.Path]) -> dict[str, str]:
    """Huella SHA-256 de cada archivo bajo las raíces dadas (para probar "no mutó")."""
    huellas: dict[str, str] = {}
    for raiz in raices:
        if raiz.is_file():
            huellas[str(raiz)] = hashlib.sha256(raiz.read_bytes()).hexdigest()
            continue
        if not raiz.exists():
            continue
        for actual, dirs, archivos in os.walk(raiz):
            dirs.sort()
            for nombre in sorted(archivos):
                p = pathlib.Path(actual) / nombre
                huellas[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return huellas


def aplicar_parche_identidad_pe(monkeypatch: object) -> None:
    """Observación PE sintética: los árboles de test no dependen de binarios propietarios.

    No es un fixture a propósito: un ``autouse`` definido en un módulo importado no
    se activa en el módulo que lo importa, así que una dependencia implícita acá
    sería una trampa silenciosa (los tests pasarían o fallarían según el import).
    """

    def _identidad(raiz: pathlib.Path, *, expected_game_key: str = "skyrimse") -> FreshRuntimeObservation:
        exe = pathlib.Path(raiz) / "SkyrimSE.exe"
        if not exe.is_file():
            raise RuntimeObservationError(f"SkyrimSE.exe ausente en '{raiz}'")
        raw = exe.read_bytes()
        if not raw.startswith(b"SkyrimSE-"):
            raise RuntimeObservationError(f"PE inválido en '{exe}'")
        return FreshRuntimeObservation(
            game_key=expected_game_key,
            game_version=VERSION,
            observed_exe_path=str(exe),
            observed_at_ns=1,
        )

    monkeypatch.setattr(  # type: ignore[attr-defined]
        "sky_claw.local.frozen_runtime.provisioning.observe_runtime_identity_from_root", _identidad
    )
    monkeypatch.setattr("sky_claw.local.frozen_runtime.clones.observe_runtime_identity_from_root", _identidad)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "sky_claw.local.frozen_runtime.generations.observe_runtime_identity_from_root", _identidad
    )


# ---------------------------------------------------------------------------
# Escenario completo (Stock verificado → Generation → Clone + MO2 global + Steam)
# ---------------------------------------------------------------------------


class RigStockManual:
    """Escenario sintético equivalente al rig real de P7-A5 (Stock + Clone + Manual + Steam + MO2).

    ``steam_activo=True`` publica ``StateFlags=1026`` y una descarga en curso: es el
    escenario que sólo ``STOCK_INDEPENDENT_TRANSITION`` admite.
    """

    def __init__(self, base: pathlib.Path, *, steam_activo: bool = True) -> None:
        self.base = base
        self.managed_source = crear_steam_sintetico(
            base / "SteamLibrary",
            state_flags="1026" if steam_activo else "4",
            con_descarga_activa=steam_activo,
        )
        self.stock_base = poblar_arbol_skyrim(base / "GoldenMaster_Base", sello_extra=b"-stock")
        self.referencia: StockReference = construir_referencia_stock(self.stock_base)
        self.approved_digest: str = self.referencia.reference_digest
        self.approved_digest_file = base / "A1_Evidence" / "stock_reference.approved_digest.txt"
        self.approved_digest_file.parent.mkdir(parents=True, exist_ok=True)
        self.approved_digest_file.write_text(f"{self.approved_digest}\n", encoding="utf-8")

        self.stock_source = pathlib.Path(shutil.copytree(self.stock_base, base / "GoldenMaster_Source"))
        self.storage = base / "FrozenRuntime"
        assert initialize_frozen_runtime_storage(self.storage).success

        copia = adoptar_copia_stock(
            self.stock_source,
            self.referencia,
            storage_root=self.storage,
            managed_source_root=self.managed_source,
        )
        pub = publicar_generation_desde_stock(self.storage, copia, margen_bytes=0)
        assert pub.success and pub.generation_id is not None
        self.generation_id: str = pub.generation_id

        self.clones_parent = base / "FrozenClones"
        self.clones_parent.mkdir(parents=True, exist_ok=True)
        clone_res = crear_runtime_clone_desde_generation(
            self.storage,
            self.generation_id,
            self.clones_parent,
            "clone-p7-01",
            managed_source_root=self.managed_source,
            margen_bytes=0,
        )
        assert clone_res.success and clone_res.clone_root is not None
        self.clone_id = "clone-p7-01"
        self.clone_root: pathlib.Path = clone_res.clone_root

        self.manual_runtime = poblar_arbol_skyrim(base / "Skyrim_Runtime_Manual", sello_extra=b"-manual")

        self.local_app_data = base / "LocalAppData"
        self.mo2_base_data = base / "MO2_Data" / "SkyrimSE"
        (self.mo2_base_data / "mods").mkdir(parents=True, exist_ok=True)
        # INI residual inactivo en base_directory (exactamente como en el rig real A5-R1)
        escribir_ini_mo2(self.mo2_base_data / "modorganizer.ini", game_path=self.managed_source)

        self.mo2_active_global = crear_mo2_global_sintetica(
            self.local_app_data,
            instance_name="Skyrim Special Edition",
            game_path=self.manual_runtime,
            base_directory=self.mo2_base_data,
            ejecutables=[
                ("SKSE", f"{self.manual_runtime.as_posix()}/skse64_loader.exe", self.manual_runtime.as_posix()),
                ("LOOT", (base / "Tools" / "LOOT" / "LOOT.exe").as_posix(), None),
            ],
        )
        self.config_path = crear_config_toml(base / "config.toml", skyrim_path=str(self.manual_runtime))
        self.environ = {"SKYRIM_PATH": str(self.manual_runtime)}

    def probes_globales_activos(self) -> RuntimeProbes:
        return probes_con_registro_simulado(
            local_app_data=self.local_app_data,
            registry_obs=Mo2RegistryObservation(
                state=Mo2RegistryState.OBSERVED,
                current_instance="Skyrim Special Edition",
            ),
        )

    def kwargs_preflight_stock(self) -> dict[str, object]:
        """Parámetros exactos que el preflight exige para READY bajo Steam ACTIVE."""
        return {
            "managed_source_root": self.managed_source,
            "outgoing_manual_runtime_root": self.manual_runtime,
            "approved_stock_reference_digest_file": self.approved_digest_file,
            "stock_source_root": self.stock_source,
            "config_skyrim_path": str(self.manual_runtime),
            "exigir_config_skyrim_path": True,
            "environ": self.environ,
            "probes": self.probes_globales_activos(),
        }


def procedencia_con_vinculo_mo2(
    *,
    mo2_dir: pathlib.Path,
    outgoing_manual_runtime_root: pathlib.Path | None,
    base: str = "hitl:user-authorized",
    transition_policy: BinderTransitionPolicy = BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
) -> str:
    """Procedencia de una aprobación FIXTURE que liga la instancia ``mo2_dir`` y su INI ACTUAL.

    Las aprobaciones del contrato P7-A5 (política Stock o runtime saliente) deben comprometer instancia +
    identidad + INI + política + runtime saliente (``mo2_authorization``). Las fixtures que llaman al binder
    directo lo declaran con esto en vez de saltarse el chequeo; se llama DESPUÉS de dejar el INI en el estado
    a aprobar. Es una función del módulo y no un método porque el repo tiene más de una ``RigStockManual``.
    """
    from sky_claw.local.frozen_runtime.mo2_authorization import (
        IDENTIDAD_AUTORIZADA,
        calcular_token_mo2,
        segmento_de_procedencia,
    )

    token = calcular_token_mo2(
        mo2_instance_dir=mo2_dir,
        mo2_instance_identity=IDENTIDAD_AUTORIZADA,
        mo2_ini_sha256=hashlib.sha256((mo2_dir / "modorganizer.ini").read_bytes()).hexdigest(),
        transition_policy=transition_policy,
        outgoing_manual_runtime_root=outgoing_manual_runtime_root,
    )
    return f"{base}:{segmento_de_procedencia(token)}"


def crear_generation_steam(storage: pathlib.Path, managed_source: pathlib.Path) -> tuple[str, pathlib.Path]:
    """Generation con ``provider=STEAM`` y un Clone asociado (para casos negativos)."""
    staging = storage.parent / "_staging_steam_gen"
    if staging.exists():
        shutil.rmtree(staging)
    poblar_arbol_skyrim(staging, sello_extra=b"-gen-steam")
    files = inventory_tree(staging)
    crit = next(f for f in files if f.rel_path == "SkyrimSE.exe")
    evidencia = SourceSnapshotEvidence(
        observed_at_ns=1000,
        provider=ManagedSourceProvider.STEAM,
        game_key="skyrimse",
        files=files,
        critical_files=(crit,),
        provider_metadata=ProviderMetadataObservation(
            provider=ManagedSourceProvider.STEAM,
            appid="489830",
            buildid="13189953",
        ),
        runtime_identity=RuntimeIdentity(game_key="skyrimse", game_version=VERSION),
        tree_digest=tree_digest_from_files(files),
    )
    meta = registrar_generation_metadata(storage, evidencia)
    gen_path = generation_dir(storage, meta.generation_id)
    shutil.copytree(staging, gen_path)
    shutil.rmtree(staging)

    clone_dir = storage.parent / "FrozenClones" / "clone-steam-01"
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(gen_path, clone_dir)
    registrar_runtime_clone_record(
        storage,
        RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-steam-01",
            source_generation_id=meta.generation_id,
            lifecycle=RuntimeCloneLifecycle.PROVISIONED,
            intended_runtime_setup_id="setup-skse",
            verified_runtime_setup_id="setup-skse",
            root_path=str(clone_dir),
            admitted_role="runtime_clone",
            created_at_ns=1000,
        ),
        managed_source_root=managed_source,
    )
    return "clone-steam-01", clone_dir
