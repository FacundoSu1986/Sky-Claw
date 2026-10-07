# B1 — Permisos de las herramientas cerradas de la R-suite (registro con hash)

> **Fecha:** 2026-10-07 · **Base:** `origin/main` `97dcc7a`.
>
> **Estado:** B1 (lectura de permisos) **CERRADO** · B6-L (invocación directa de helpers) **ABIERTO**.
>
> **Nivel de evidencia:** `SUPPORTED_BY_REPO_EVIDENCE`. Es la transcripción de lo que registró el P0
> (consultado en origen el 2026-09-19 y marcado allí `VERIFIED` contra la página del autor). **No se
> re-verificó contra Nexus**: el egress del entorno bloquea `www.nexusmods.com` el 2026-10-07.
>
> **Datos con hash:** [`data/2026-10-07_b1_permisos_r_suite.json`](data/2026-10-07_b1_permisos_r_suite.json),
> `sha256` `05ffca3dee16f2ea946749d920874e3e4e0162cae6e71274b6402250b31bde7b` (JSON canónico sin su propio
> campo `sha256`). Lo ancla `tests/test_clean_room_invariant.py`.
>
> Los informes de este directorio son evidencia fechada, no estado vivo (ver [README](README.md)).

## Qué se cierra y qué no

- **B1 — cerrado.** El plan P0 v3 (§3.2) define B1 como leer los permisos de ParallaxR, BENDr y
  VRAMr en Nexus. Esa lectura está hecha y registrada en la evidencia P0 (§2 y §7); este registro la
  consolida, le agrega hash y la acopla al contrato de materiales.
- **B6-L — abierto.** Es otra pregunta: si el autor permite que un tercero invoque sus helpers
  directamente. La evidencia dice que no hay autorización explícita; el paso siguiente (R-LIC-1) es
  un pedido escrito al autor con respuesta fechada. Mientras tanto `MaterialStepId.PARALLAXR`,
  `BENDR` y `VRAMR` siguen `BLOCKED` y rige `MANUAL_ONLY`.
- **Reserva.** La transcripción es de segunda mano (documento del repo → este registro). No
  reemplaza el texto verbatim de las tres páginas: ver "Cómo mejorar la evidencia".

## Permisos registrados

| | ParallaxR | BENDr | VRAMr |
|---|---|---|---|
| Página Nexus (mod) · versión registrada | 124711 · v3.0318 | 121578 · v3.0331 | 90557 · v16.0310 |
| Subir a otros sitios | no | no | no |
| Modificación | con permiso | con permiso | con permiso |
| Conversión | no | no | no |
| Uso de assets | con permiso | con permiso | con permiso |
| Notas del autor | ver abajo | ninguna | ver abajo |
| Ejecutar la copia instalada del usuario | sí | sí | sí |
| Redistribuir o bundlear | no | no | no |
| Invocación directa de helpers (B6-L) | **abierta** | **abierta** | **abierta**, condicionada a consentimiento escrito |

Para VRAMr la evidencia dice «igual patrón de permisos» que BENDr y no los lista por separado.

Notas del autor, tal como las registró el P0:

- ParallaxR: «It should be fine for you to upload/share ParallaxR Outputs»; «You may not share any
  of the ParallaxR mod files itself without checking the license restrictions».
- VRAMr: «You may prepare for Collections or Wabbajacks VRAMr outputs and distribute these on Nexus
  without my permission. No other activity permitted without prior written consent.»

## Lectura conservadora para Sky-Claw (no es asesoramiento legal)

| Pregunta | Con la evidencia actual |
|---|---|
| ¿Ejecutar la copia instalada por el usuario (modo asistido)? | Sí: no se invocan helpers ni se redistribuye nada. |
| ¿Invocar sus helpers directamente? | No, hasta tener permiso explícito (B6-L). |
| ¿Bundlear o redistribuir archivos de la herramienta, incluidos sus assets y su configuración? | No: no se autoriza subirlos a otros sitios y el uso de assets requiere permiso. |
| ¿Copiar sus constantes, listas de exclusión o presets al generador nativo? | No: lo prohíbe la política clean-room y el uso de assets requiere permiso. |
| ¿Compartir salidas generadas por la herramienta? | La nota del autor dice que debería estar bien. No cubre entrenar modelos con ellas: criterio conservador, no. |
| ¿Implementar una alternativa independiente? | Estos permisos describen qué se puede hacer con los archivos de cada mod; no mencionan reimplementaciones independientes. Es el supuesto del clean-room; la decisión legal final es del operador. |

## Cómo mejorar la evidencia

1. **Texto verbatim.** Pegar la sección "Permissions and credits" de cada página (o permitir el host
   `www.nexusmods.com` en el acceso de red del entorno): se reemplaza la transcripción por el texto
   literal con fecha y hash, y el nivel de evidencia sube.
2. **B6-L.** Cursar R-LIC-1 (pedido escrito al autor de la R-suite) y registrar la respuesta fechada.
3. **Vigencia.** El registro vale para las versiones registradas: releer los permisos si cambia la
   versión y antes de cualquier decisión que dependa de ellos.

## Referencias

- Evidencia P0: `docs/design/research/2026-09-19-pre-lod-material-p0-evidence.md` §2 (filas Nexus) y
  §7 (tabla B6-L).
- Plan P0 v3, §3.2 (B1 y B6-L): `docs/design/plans/2026-08-19-pre-lod-material-pipeline-v3.md`.
- Política: [`CLEAN_ROOM.md`](../../CLEAN_ROOM.md) y [ADR 0013](../adr/0013-clean-room-native-parallax.md).
- Contrato: `sky_claw/local/tools/material_contract.py` (`MATERIAL_PIPELINE`, `MaterialBlocker.B6_L`).
