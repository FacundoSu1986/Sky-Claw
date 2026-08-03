# Plan de Implementación Corregido: Autoinstalación de SKSE

## Goal
Implementar la autoinstalación robusta de SKSE copiando directamente todos los binarios y la carpeta Data a la carpeta raíz del juego, con chequeos de QA extremo (Adversarial) incluyendo limpieza de versiones previas (Dirty Upgrades) y descargas en streaming.

## Arquitectura Real del Proyecto

Basado en el análisis del código existente, la implementación debe:

1. **Extender `ToolsInstaller`** en `sky_claw/local/tools_installer.py` siguiendo el patrón existente de `ensure_loot`, `ensure_xedit`, y `ensure_pandora`
2. **Usar la firma correcta**: `(self, install_dir: pathlib.Path, session: aiohttp.ClientSession) -> InstallResult`
3. **Respetar las dependencias de seguridad**: `HITLGuard`, `NetworkGateway`, `PathValidator`
4. **NO exponer al Agente LLM** (`external_tools.py`) - solo accesible vía HITL
5. **Agregar constantes al scanner** en `sky_claw/local/discovery/scanner.py` para detección

---

## Tarea 1: Configuración de SKSE y Constantes de Seguridad

### Files a Modificar
- `sky_claw/local/tools_installer.py` - Añadir diccionario de configuración SKSE
- `sky_claw/local/discovery/scanner.py` - Añadir nombres de loader para detección

### Paso 1: Añadir configuración SKSE en tools_installer.py

```python
# sky_claw/local/tools_installer.py (después de las constantes de URLs de GitHub)

# ---------------------------------------------------------------------------
# SKSE Configuration
# ---------------------------------------------------------------------------

from sky_claw.local.discovery.environment import SkyrimEdition

SKSE_CONFIG: dict[str, dict[str, str]] = {
    "AE": {
        "url": "https://skse.silverlock.org/beta/skse64_2_02_06.7z",
        "dll": "skse64_1_6_1170.dll",
        "loader": "skse64_loader.exe",
        "steam_loader": "skse64_steam_loader.dll",
    },
    "SE": {
        "url": "https://skse.silverlock.org/beta/skse64_2_00_20.7z",
        "dll": "skse64_1_5_97.dll",
        "loader": "skse64_loader.exe",
        "steam_loader": "skse64_steam_loader.dll",
    },
    "GOG": {
        "url": "https://skse.silverlock.org/beta/skse64_2_02_06_gog.7z",
        "dll": "skse64_1_6_1170.dll",
        "loader": "skse64_loader.exe",
        "steam_loader": "skse64_steam_loader.dll",
    },
    "LE": {
        "url": "https://skse.silverlock.org/beta/skse_1_07_03.7z",
        "dll": "skse_1_9_32.dll",
        "loader": "skse_loader.exe",
        "steam_loader": None,  # LE no tiene steam_loader
    },
}
```

### Paso 2: Añadir constantes en scanner.py

```python
# sky_claw/local/discovery/scanner.py (después de _XEDIT_NAMES)

_SKSE_LOADER_NAMES = ("skse64_loader.exe", "skse_loader.exe")
```

Y agregar SKSE a la lista `tool_defs` en `_scan_inner`:

```python
tool_defs = [
    # ... existing tools ...
    (
        "skse",
        _SKSE_LOADER_NAMES,
        "Extensor del motor (SKSE)",
        "https://skse.silverlock.org/",
        True,  # is_critical = True
    ),
]
```

---

## Tarea 2: Implementar ensure_skse con Limpieza de Dirty Upgrades

### Files a Modificar
- `sky_claw/local/tools_installer.py` - Método `ensure_skse`
- `tests/test_tools_installer.py` - Tests para limpieza y configuración

### Firma del Método

```python
async def ensure_skse(
    self,
    install_dir: pathlib.Path,
    session: aiohttp.ClientSession,
    edition: SkyrimEdition | None = None,
) -> InstallResult:
    """Ensure SKSE is available in the game directory, downloading if necessary.

    Args:
        install_dir: Skyrim game root directory (where SkyimSE.exe lives).
        session: Active HTTP session.
        edition: Optional Skyrim edition override. If None, auto-detect from DLL presence.

    Returns:
        :class:`InstallResult` with the path to ``skse64_loader.exe``.

    Raises:
        ToolInstallError: If installation fails or edition is unsupported (MS Store).
    """
```

### Implementación - Fase 1: Validación y Limpieza

```python
async def ensure_skse(
    self,
    install_dir: pathlib.Path,
    session: aiohttp.ClientSession,
    edition: SkyrimEdition | None = None,
) -> InstallResult:
    from sky_claw.local.discovery.environment import SkyrimEdition

    self._validator.validate(install_dir)

    # Determinar edición
    if edition is None:
        edition = self._detect_skse_edition_from_existing(install_dir)

    ed_key = self._edition_to_config_key(edition)
    cfg = SKSE_CONFIG.get(ed_key)

    if cfg is None:
        raise ToolInstallError(
            f"Edición {ed_key} no compatible (probablemente MS Store, que no soporta SKSE)."
        )

    # Idempotencia: verificar si ya está instalado correctamente
    loader_path = install_dir / cfg["loader"]
    dll_path = install_dir / cfg["dll"]

    if loader_path.exists() and dll_path.exists():
        logger.info("SKSE ya instalado en %s", loader_path)
        return InstallResult(
            tool_name="SKSE",
            exe_path=loader_path,
            version="existing",
            already_existed=True,
        )

    # LIMPIEZA DE DIRTY UPGRADES: Eliminar DLLs huérfanos de otras versiones
    await self._cleanup_orphaned_skse_dlls(install_dir, cfg)

    # Continuar con descarga...
```

### Helper: Detección de Edición desde DLLs Existentes

```python
def _detect_skse_edition_from_existing(self, game_dir: pathlib.Path) -> SkyrimEdition:
    """Detect SKSE edition from existing DLLs in game directory."""
    # AE/1.6+ DLL
    if (game_dir / "skse64_1_6_1170.dll").exists():
        return SkyrimEdition.AE
    # SE/1.5 DLL
    if (game_dir / "skse64_1_5_97.dll").exists():
        return SkyrimEdition.SE
    # LE DLL
    if (game_dir / "skse_1_9_32.dll").exists():
        return SkyrimEdition.LE
    # Default to AE (most common)
    return SkyrimEdition.AE

def _edition_to_config_key(self, edition: SkyrimEdition) -> str:
    """Convert SkyrimEdition enum to SKSE_CONFIG dictionary key."""
    mapping = {
        SkyrimEdition.AE: "AE",
        SkyrimEdition.SE: "SE",
        SkyrimEdition.LE: "LE",
        SkyrimEdition.GOG: "GOG",
    }
    return mapping.get(edition, "AE")
```

### Helper: Limpieza de DLLs Huérfanos (con manejo Read-Only)

```python
async def _cleanup_orphaned_skse_dlls(
    self,
    game_dir: pathlib.Path,
    current_cfg: dict[str, str],
) -> None:
    """Remove orphaned SKSE DLLs from previous versions (dirty upgrade cleanup).

    Handles Windows Read-Only attribute by temporarily clearing it.
    Preserves skse64_steam_loader.dll as it's shared across versions.
    """
    import stat

    protected_names = {current_cfg["dll"], current_cfg.get("steam_loader"), "skse64_steam_loader.dll"}
    protected_names.discard(None)

    for old_dll in game_dir.glob("skse*.dll"):
        if old_dll.name in protected_names:
            continue

        try:
            # Manejar atributo Read-Only en Windows
            if old_dll.exists():
                try:
                    mode = old_dll.stat().st_mode
                    if not (mode & stat.S_IWRITE):
                        old_dll.chmod(mode | stat.S_IWRITE)
                        logger.debug("Cleared read-only flag on %s", old_dll)
                except OSError:
                    pass  # Ignorar si no se puede cambiar permisos

                old_dll.unlink()
                logger.info("Removed orphaned SKSE DLL: %s", old_dll.name)
        except PermissionError as exc:
            logger.warning(
                "No se pudo eliminar %s (¿Skyrim o MO2 están abiertos?): %s",
                old_dll.name,
                exc,
            )
            raise ToolInstallError(
                f"Permiso denegado al limpiar {old_dll.name}. Cierra Skyrim y Mod Organizer."
            ) from exc
        except OSError as exc:
            logger.warning("Error limpiando %s: %s", old_dll.name, exc)
```

---

## Tarea 3: Descarga Segura y Extracción Determinista

### Implementación - Fase 2: Descarga con HITL y Gateway

```python
    # Solicitar aprobación HITL
    decision = await self._hitl.request_approval(
        request_id=f"install-skse-{ed_key}",
        reason=f"Instalar SKSE para Skyrim {ed_key}?",
        url=cfg["url"],
        detail=(
            f"URL: {cfg['url']}\n"
            f"Loader: {cfg['loader']}\n"
            f"DLL: {cfg['dll']}\n"
            f"Source: skse.silverlock.org (sitio oficial)"
        ),
    )

    if decision is not Decision.APPROVED:
        raise ToolInstallError(f"SKSE installation denied by operator (decision={decision.value})")

    # Descargar archivo .7z
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = pathlib.Path(tmpdir)
        archive_name = cfg["url"].split("/")[-1]
        archive_path = tmp_path / archive_name

        # Descarga segura vía NetworkGateway con timeout
        await self._download_skse_archive(session, cfg["url"], archive_path)

        # Extraer con protección zip-slip
        extract_path = tmp_path / "extracted"
        self._extract(archive_path, extract_path)

        # Buscar loader excluyendo __MACOSX
        skse_root = self._find_skse_root(extract_path, cfg["loader"])

        # Copiar archivos al directorio del juego
        await self._copy_skse_files(skse_root, game_dir, cfg)

    # Retornar resultado
    logger.info("SKSE %s instalado en %s", ed_key, loader_path)
    return InstallResult(
        tool_name="SKSE",
        exe_path=loader_path,
        version=cfg["url"].split("_")[-1].replace(".7z", ""),
        already_existed=False,
    )
```

### Helper: Descarga de Archivo SKSE

```python
async def _download_skse_archive(
    self,
    session: aiohttp.ClientSession,
    url: str,
    dest_path: pathlib.Path,
) -> None:
    """Download SKSE archive via NetworkGateway with timeout and validation."""
    import hashlib

    self._validator.validate(dest_path)

    timeout = aiohttp.ClientTimeout(total=120, sock_read=60)
    downloaded = 0
    hasher = hashlib.sha256()

    logger.info("Descargando SKSE desde %s ...", url)

    resp = await self._gateway.request(
        "GET",
        url,
        session,
        headers={"Accept": "application/octet-stream"},
        timeout=timeout,
        allowed_redirect_hosts=("skse.silverlock.org",),
    )

    try:
        resp.raise_for_status()
        with dest_path.open("wb") as fh:
            async for chunk in resp.content.iter_chunked(_DOWNLOAD_CHUNK_SIZE):
                fh.write(chunk)
                hasher.update(chunk)
                downloaded += len(chunk)

                if downloaded % (10 * _DOWNLOAD_CHUNK_SIZE) == 0:
                    logger.info("  ... %d bytes descargados", downloaded)

    except (aiohttp.ClientError, OSError, TimeoutError) as exc:
        logger.error("Download failed for SKSE: %s", exc)
        if dest_path.exists():
            dest_path.unlink()
        raise ToolInstallError(f"Error descargando SKSE: {exc}") from exc
    finally:
        resp.release()

    if downloaded == 0:
        dest_path.unlink(missing_ok=True)
        raise ToolInstallError("SKSE download returned empty content")

    logger.info(
        "SKSE descargado (%d bytes, sha256=%s...)",
        downloaded,
        hasher.hexdigest()[:16],
    )
```

### Helper: Búsqueda Determinista del Root de SKSE

```python
def _find_skse_root(self, extract_path: pathlib.Path, loader_name: str) -> pathlib.Path:
    """Find SKSE root directory, excluding __MACOSX and other artifacts.

    Returns the parent directory containing the loader executable.
    """
    valid_loaders = [
        p for p in extract_path.rglob(loader_name)
        if "__MACOSX" not in p.parts and p.is_file()
    ]

    if not valid_loaders:
        raise ToolInstallError(
            f"No se encontró {loader_name} válido en el archivo SKSE. "
            "El archivo puede estar corrupto o tener una estructura inesperada."
        )

    # Usar el primero encontrado (debería ser único)
    skse_root = valid_loaders[0].parent
    logger.debug("SKSE root encontrado: %s", skse_root)

    return skse_root
```

### Helper: Copia Controlada de Archivos

```python
async def _copy_skse_files(
    self,
    skse_root: pathlib.Path,
    game_dir: pathlib.Path,
    cfg: dict[str, str],
) -> None:
    """Copy SKSE binaries and Data folder to game directory.

    Handles PermissionError by providing actionable user guidance.
    """
    import shutil

    copied_count = 0

    for item in skse_root.iterdir():
        target = game_dir / item.name

        if item.is_file():
            if item.suffix.lower() in (".dll", ".exe"):
                # Manejar archivos read-only existentes
                if target.exists():
                    try:
                        import stat
                        mode = target.stat().st_mode
                        if not (mode & stat.S_IWRITE):
                            target.chmod(mode | stat.S_IWRITE)
                    except OSError:
                        pass

                shutil.copy2(item, target)
                copied_count += 1
                logger.debug("Copiado: %s → %s", item.name, target)

        elif item.is_dir() and item.name == "Data":
            # Copiar carpeta Data recursivamente
            if target.exists():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copytree(item, target)
            logger.info("Copiada carpeta Data de SKSE")

    if copied_count == 0:
        raise ToolInstallError(
            f"No se copiaron archivos SKSE desde {skse_root}. "
            "Verifica que el archivo descargado contenga los binarios esperados."
        )

    logger.info("SKSE: %d archivos copiados a %s", copied_count, game_dir)
```

---

## Tarea 4: Tests Completos en Español (TDD)

### File: `tests/test_tools_installer.py`

Añadir una nueva clase de tests después de `TestEnsureXedit`:

```python
# ---------------------------------------------------------------------------
# ToolsInstaller.ensure_skse
# ---------------------------------------------------------------------------


class TestEnsureSkse:
    """Tests para la instalación automática de SKSE."""

    @pytest.mark.asyncio
    async def test_retorna_existente_sin_descarga(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """Cuando skse64_loader.exe ya existe, retornarlo inmediatamente."""
        from sky_claw.local.discovery.environment import SkyrimEdition

        loader = tmp_path / "skse64_loader.exe"
        dll = tmp_path / "skse64_1_6_1170.dll"
        loader.write_text("fake-loader", encoding="utf-8")
        dll.write_text("fake-dll", encoding="utf-8")

        session = MagicMock(spec=aiohttp.ClientSession)
        result = await installer.ensure_skse(tmp_path, session, SkyrimEdition.AE)

        assert result.already_existed is True
        assert result.exe_path == loader
        assert result.tool_name == "SKSE"

    @pytest.mark.asyncio
    async def test_limpia_dlls_huerfanos_antes_de_instalar(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """Eliminar DLLs de versiones anteriores de SKSE (dirty upgrade)."""
        from sky_claw.local.discovery.environment import SkyrimEdition

        # Usuario tenía SE instalado antes (versión 1.5.97)
        old_dll = tmp_path / "skse64_1_5_97.dll"
        old_dll.write_text("old-version", encoding="utf-8")

        # Steam loader compartido debe preservarse
        steam_loader = tmp_path / "skse64_steam_loader.dll"
        steam_loader.write_text("shared-steam", encoding="utf-8")

        # Mockear para fallar después de la limpieza
        async def fake_download(*args, **kwargs):
            raise ToolInstallError("Stop after cleanup")

        installer._download_skse_archive = fake_download  # type: ignore[method-assign]

        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match="Stop after cleanup"):
            await installer.ensure_skse(tmp_path, session, SkyrimEdition.AE)

        # El viejo DLL debió ser eliminado
        assert not old_dll.exists()
        # Pero steam_loader debe preservarse
        assert steam_loader.exists()

    @pytest.mark.asyncio
    async def test_falla_con_edicion_ms_store(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """MS Store no es compatible con SKSE."""
        from sky_claw.local.discovery.environment import SkyrimEdition

        session = MagicMock(spec=aiohttp.ClientSession)

        # Crear una edición falsa que no esté en SKSE_CONFIG
        with patch.dict(
            "sky_claw.local.tools_installer.SKSE_CONFIG",
            {},
            clear=True,
        ):
            with pytest.raises(ToolInstallError, match="no compatible"):
                await installer.ensure_skse(tmp_path, session, SkyrimEdition.AE)

    @pytest.mark.asyncio
    async def test_descarga_y_extrae_correctamente(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """Descargar SKSE y extraer ignorando __MACOSX."""
        import zipfile
        from sky_claw.local.discovery.environment import SkyrimEdition

        # Crear archivo .zip falso con estructura realista + trampa __MACOSX
        archive_path = tmp_path / "skse_fake.zip"

        with zipfile.ZipFile(archive_path, "w") as zf:
            # Estructura real de SKSE
            zf.writestr("skse64_2_02/skse64_loader.exe", "fake-loader-binary")
            zf.writestr("skse64_2_02/skse64_1_6_1170.dll", "fake-dll-binary")
            zf.writestr("skse64_2_02/Data/SKSE/Plugins/plugin.txt", "fake-plugin")

            # Trampa __MACOSX (debe ser ignorada)
            zf.writestr("__MACOSX/skse64_2_02/skse64_loader.exe", "macosx-fake")

        # Leer bytes para simular descarga
        archive_bytes = archive_path.read_bytes()
        archive_path.unlink()

        # Mock de descarga
        async def mock_download(session, url, dest):
            dest.write_bytes(archive_bytes)

        installer._download_skse_archive = mock_download  # type: ignore[method-assign]

        game_dir = tmp_path / "game"
        game_dir.mkdir()

        session = MagicMock(spec=aiohttp.ClientSession)

        # Auto-aprobar HITL
        async def _auto_approve() -> None:
            await asyncio.sleep(0.01)
            await installer._hitl.respond("install-skse-AE", approved=True)

        asyncio.create_task(_auto_approve())

        result = await installer.ensure_skse(game_dir, session, SkyrimEdition.AE)

        # Verificaciones
        assert result.already_existed is False
        assert result.tool_name == "SKSE"
        assert (game_dir / "skse64_loader.exe").exists()
        assert (game_dir / "skse64_1_6_1170.dll").exists()
        assert (game_dir / "Data" / "SKSE" / "Plugins" / "plugin.txt").exists()

        # __MACOSX NO debe haber sido copiado
        assert not (game_dir / "__MACOSX").exists()

    @pytest.mark.asyncio
    async def test_hitl_denial_raises(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """Cuando operador deniega, lanzar ToolInstallError."""
        from sky_claw.local.discovery.environment import SkyrimEdition

        game_dir = tmp_path / "game"
        game_dir.mkdir()

        # Mock de descarga (nunca debería llamarse si HITL deniega)
        installer._download_skse_archive = AsyncMock()  # type: ignore[method-assign]

        session = MagicMock(spec=aiohttp.ClientSession)

        async def _auto_deny() -> None:
            await asyncio.sleep(0.01)
            await installer._hitl.respond("install-skse-AE", approved=False)

        asyncio.create_task(_auto_deny())

        with pytest.raises(ToolInstallError, match="denied"):
            await installer.ensure_skse(game_dir, session, SkyrimEdition.AE)

        # La descarga nunca debió ocurrir
        installer._download_skse_archive.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_permiso_denegado_si_juego_abierto(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """Si hay DLLs bloqueados por el juego, dar error accionable."""
        from sky_claw.local.discovery.environment import SkyrimEdition
        import stat

        game_dir = tmp_path / "game"
        game_dir.mkdir()

        # Crear DLL old con atributo read-only que no se pueda remover
        old_dll = game_dir / "skse64_1_5_97.dll"
        old_dll.write_text("locked", encoding="utf-8")
        old_dll.chmod(stat.S_IREAD)  # Solo lectura

        session = MagicMock(spec=aiohttp.ClientSession)

        # En Windows real esto fallaría, pero en Linux simulamos el error
        with patch.object(
            installer,
            "_cleanup_orphaned_skse_dlls",
            side_effect=PermissionError("DLL in use"),
        ):
            with pytest.raises(ToolInstallError, match="Cierra Skyrim"):
                await installer.ensure_skse(game_dir, session, SkyrimEdition.AE)
```

---

## Tarea 5: Integración con el Escáner de Entorno

### Modificación: `sky_claw/local/discovery/scanner.py`

```python
# Después de las constantes de herramientas existentes (~línea 94)

_SKSE_LOADER_NAMES = ("skse64_loader.exe", "skse_loader.exe")
```

Y en `_scan_inner`, agregar a `tool_defs`:

```python
tool_defs = [
    # ... existing: loot, xedit, pandora, wrye_bash, dyndolod ...
    (
        "skse",
        _SKSE_LOADER_NAMES,
        "Extensor del motor (SKSE) - Requiere instalación manual o auto-instalador",
        "https://skse.silverlock.org/",
        True,  # is_critical = True
    ),
]
```

---

## Restricciones de Seguridad y QA

### Checklist de Verificación Adversarial

- [ ] **Zip-slip protection**: `_extract` ya implementa validación
- [ ] **Timeout explícito**: 120s total, 60s sock_read
- [ ] **NetworkGateway**: Todas las descargas pasan por egress control
- [ ] **HITL mandatory**: Aprobación humana requerida antes de descargar
- [ ] **PathValidator**: sandbox validation en todos los paths
- [ ] **Read-Only handling**: chmod temporal para archivos bloqueados
- [ ] **__MACOSX exclusion**: filtrado en `_find_skse_root`
- [ ] **Size validation**: verificar download no vacío
- [ ] **Idempotencia**: skip si loader + DLL correctos existen
- [ ] **Dirty upgrade cleanup**: eliminar DLLs de versiones anteriores
- [ ] **No exposición a Agente LLM**: solo HITL vía GUI

---

## Notas Importantes

### ARCHIVOS GUI NO EXISTEN
Los archivos mencionados en el plan original:
- `sky_claw/app/gui/controllers/ritual_runner.py` ❌ NO EXISTE
- `sky_claw/app/gui/views/forge_dashboard.py` ❌ NO EXISTE

La arquitectura actual del proyecto **no tiene capa GUI** en este repositorio. La integración GUI deberá hacerse en un PR separado cuando exista la capa de presentación.

### SkyrimEdition Import Correcto

El import correcto es:

```python
from sky_claw.local.discovery.environment import SkyrimEdition
```

Este módulo ya existe en el repositorio y contiene la definición de `SkyrimEdition` como `StrEnum`.

### Fallback para Extracción 7z

El método `_extract` ya soporta `.7z` vía `py7zr`. **Verificado**: el paquete `py7zr==0.22.0` ya está presente en `requirements.lock`, por lo que no es necesario añadirlo.

---

## Comandos de Validación

```bash
# Ejecutar tests específicos
python -m pytest tests/test_tools_installer.py::TestEnsureSkse -v

# Linting
ruff check sky_claw/ tests/
ruff format --check sky_claw/ tests/

# Type checking
mypy sky_claw/ tests/

# Verificar imports
python -c "from sky_claw.local.tools_installer import SKSE_CONFIG, ToolsInstaller; print('OK')"
```
