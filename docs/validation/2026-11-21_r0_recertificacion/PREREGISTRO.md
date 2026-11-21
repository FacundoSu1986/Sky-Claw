# PREREGISTRO — R0 Ronda 6 (R0-2: identidad del bundle del bridge, condición 3 del HOLD)

- **Fecha de preregistro:** 2026-11-21, redactado **antes** de abrir MO2 y antes de cualquier `vfs-health` de esta ronda; no se edita después.
- **SHA certificado:** `676f4c29ebea50fde8a8817a887521812ea40a2c` (`origin/main`, squash del PR #980). `_CONTENCION_DEL_WORKER_VERIFICADA = False` (línea 922 de `sky_claw/local/mo2/brokered_dyndolod.py`).
- **`.exe` congelado:** `E:\SkyClaw_R0_Rig\exe_676f4c29\SkyClawApp.exe` · `88.296.345` B · SHA-256 `ef7777c163e8c03524ae6f08f4171beee2957cfbd677c013db6f3d94bfdc9e55` (verificado a las 10:12:21 −03:00, antes del primer uso; el lanzador lo re-verifica antes de cada corrida y se niega a correr si no coincide).
- **Digest esperado del bundle:** `24a1869e4e6213d799e940934dab0900fac2fc0de30babdceb28517749d256b0`.
- **Esta ronda no levanta el HOLD ni propone levantarlo.** No se cambia código.

## R0-0 ya hecho (antes de este preregistro)

- `quarantine_pre`: `rc=0`, «sin cuarentena persistida».
- `install-vfs-bridge` con **este** exe: `rc=0`. `bridge_config.json` registra `bundle_digest = 24a1869e…d256b0` y `worker_executable = E:\SkyClaw_R0_Rig\exe_676f4c29\SkyClawApp.exe`.
- Digest independiente (`scripts/digest_bundle.py`, que carga `protocol.py` de 676f4c29 por ruta con `python -I`) de la copia instalada: `24a1869e…d256b0`.

## Alteración que se va a aplicar (paso 2)

- **Archivo:** `E:\SkyClaw_R0_Rig\mo2\plugins\skyclaw_bridge\__init__.py` (solo la copia aislada de MO2).
- **Byte:** offset `3`, `0x4D` (`M`) → `0x6D` (`m`), dentro del docstring `"""MO2 entry point…"""`. No cambia el comportamiento del módulo: el plugin tiene que cargar igual.
- **Digest esperado del bundle alterado** (calculado sobre una copia en `backup/simulacro_alterado`): `18e25d59d7c339fa95cf94417da4e72a1980140839411706beda824e24913848`. Prefijo de 12: `18e25d59d7c3`.
- SHA-256 de `__init__.py`: `c4e493cf…40c47` (bueno) → `924cb401…3eade2` (alterado).

## Secuencia y resultado esperado por paso

MO2 lo abre y lo cierra **Facundo desde el Explorador**. Nunca se cierra MO2 mientras corre un `vfs-health`: el lanzador deja un `DONE` al terminar y recién ahí se pide cerrar.

| Paso | Estado de MO2 | Acción | Esperado (`PASS`) |
|---|---|---|---|
| 1 | Sesión A, bundle bueno | `vfs-health` | `rc=0`, `Probe VFS correcto`, `worker_exit` con `job_empty=True`, sin cuarentena. Aceptar el `hello` implica `declarado == digest del producto` (`_verificar_el_bundle_del_bridge` solo retorna con igualdad); el producto es `24a1869e…` por `bridge_config.json`. |
| 2 | MO2 cerrado → alterar el byte → Sesión B | `vfs-health` | `rc≠0`; el broker registra el rechazo con el texto `el MO2 bridge instalado (18e25d59d7c3) no es el bundle de esta versión de Sky-Claw (24a1869e4e62): reinstalalo con `--mode install-vfs-bridge` y reabrí MO2`; no se lanza ningún job (sin `launch timings` en `mo_interface.log`) y no hay cuarentena. |
| 4 | Sesión B **abierta** (bundle alterado cargado) | `install-vfs-bridge` y después `vfs-health` | La reinstalación da `rc=0` y el disco vuelve a `24a1869e…`, pero el `vfs-health` **sigue rechazado con el prefijo `18e25d59d7c3`**: el proceso en ejecución conserva el digest que calculó al cargar. Sin cuarentena. Si el broker aceptara, sería `FAIL` (el plugin estaría leyendo el disco por `hello`). |
| 3 | MO2 cerrado → `install-vfs-bridge` → Sesión C | `vfs-health` | La reinstalación da `rc=0` con digest `24a1869e…`; el `vfs-health` da `rc=0` y `Probe VFS correcto`: el bridge reinstalado se acepta. Sin cuarentena. |

El paso 4 se corre antes que el 3 porque necesita MO2 abierto con el bundle alterado cargado, que es el estado que deja el paso 2. El paso 3 se hace con su propia reinstalación con MO2 cerrado, tal como lo pide el runbook.

## Reglas de parada

- SHA-256 del exe distinto del registrado → `BLOQUEADO` y no se sigue.
- Cualquier cuarentena → se para, se registra con su causa, se verifican los procesos (incluido `SkyClawApp.exe --vfs-worker`), `--mode vfs-quarantine` `status` y, solo con esa verificación, `release --vfs-evidence "…"`. No se borra el marcador a mano.
- Una instalación que falle con MO2 abierto (paso 4) se registra tal cual (es un resultado, no se reintenta con otro método).
- Contacto con la instancia global de MO2 o con el MO2 de `E:\Sectores\...` → se para y se registra.
