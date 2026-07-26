# AGENTS.md — seguridad

Aplican [`../AGENTS.md`](../AGENTS.md), `SECURITY.md` y las reglas raíz.

## Fuentes obligatorias

`path_validator.py`, `network_gateway.py`, `credential_vault.py`,
`file_permissions.py` y tests de seguridad.

## Invariantes

- Validar y normalizar entradas antes de I/O.
- Paths deben quedar dentro de roots permitidos.
- Egress exige allowlist y fail-closed.
- No registrar ni persistir secretos en texto plano.
- `CredentialVault` y el keyring de `Config` son flujos distintos.

## Verificación segura

Usar rutas temporales y endpoints simulados. Incluir traversal, symlinks,
host/IP alternativo, timeout y redacción.
