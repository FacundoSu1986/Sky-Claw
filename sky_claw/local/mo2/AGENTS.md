# AGENTS.md — MO2, perfiles y USVFS

Aplican [`../AGENTS.md`](../AGENTS.md), que es el SOP canónico, y las reglas
raíz.

## Fuentes obligatorias

`profile_sandbox.py`, `vfs_broker.py`, `vfs_worker.py`,
`vfs_attestation.py`, `vfs_contracts.py` y ADR 0007.

## Invariantes

- Instancia y perfil explícitos; drift falla cerrado.
- El canary debe probar visibilidad virtual, no sólo el directorio físico.
- La promoción usa baseline, backup, escritura transaccional y rollback.
- Si falla el rollback, preservar el backup.
- Timeout/cancelación termina y recolecta (`reap`) el worker y todo su árbol de
  procesos descendientes.
- `ganadores.py` (NP-A0d) contesta UNA vez «¿qué capa provee esta ruta virtual en el overlay del perfil?». Recibe `enabled` ya
  normalizado por `vfs_attestation.read_enabled_mods` (no parsea el modlist: un test lo ancla), recorre con
  `links.iter_archivos_propios` (no entra a enlaces), trata `overwrite` como la capa más alta y un `*.mohidden` como ausente, y
  falla cerrado ante un mod habilitado sin carpeta, un nombre repetido o un árbol que pasa el tope. Es una vista ESTRUCTURAL:
  no prueba que USVFS mapee lo resuelto (eso es el canary del broker) ni considera el game data físico.
  Su único consumidor es `native_parallax/diagnostico_perfil.py` (NP-A0e/A0g, solo lectura: avisa cuando otro mod, `overwrite` o un
  mod de control tapa la entrada o la salida del generador); está clasificado en la Familia A del censo de
  `tests/test_mo2_modlist_priority.py` porque interpreta la prioridad, y el censo de `tests/test_native_parallax_diagnostico_perfil.py` congela
  que `indexar_ganadores` se llame solo desde ahí.

## Verificación segura

Los tests no sustituyen el
[smoke real](../../../docs/operations/real_rig_validation.md).
