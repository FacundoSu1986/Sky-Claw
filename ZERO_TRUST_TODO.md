# Zero-Trust Post-Purge TODO

Los siguientes archivos mantenidos aún usan `os.environ` y deben refactorizarse
a inyección explícita en el siguiente sprint:

- `sky_claw/__main__.py` — todos los defaults de paths/puertos vía `SKY_CLAW_*`.
- `sky_claw/config.py` — `_load_from_env()` con gate `SKY_CLAW_ALLOW_ENV_OVERRIDES`.
- `sky_claw/local/auto_detect.py` — `LOCALAPPDATA`.
- `sky_claw/local/tools_installer.py` — posibles paths de entorno.
- `sky_claw/antigravity/orchestrator/supervisor.py` — `SKYRIM_PATH`, `MO2_PATH`, `WRYE_BASH_PATH`.
- `sky_claw/antigravity/tools/dyndolod_service.py` — `SKYRIM_PATH`, `MO2_PATH`, `DYNDLOD_EXE`, etc.
- `sky_claw/antigravity/tools/synthesis_service.py` — `SKYRIM_PATH`, `MO2_PATH`, `SYNTHESIS_EXE`.
- `sky_claw/antigravity/tools/xedit_service.py` — `XEDIT_PATH`, `SKYRIM_PATH`.
- `sky_claw/antigravity/core/path_resolver.py` — `LOCALAPPDATA`, `MO2_PATH`, `MO2_PROFILE`.
- `sky_claw/antigravity/security/file_permissions.py` — `USERNAME`.

Acción recomendada:
1. Consolidar todos los paths en un `LocalPathResolver` inyectado.
2. Consolidar secretos en `CredentialVault.get_key(name)` con backend keyring.
3. Eliminar cualquier `os.environ.get` restante en producción.
