# Gate T5-v2 DynDOLOD etapa 9 — corrida real 2026-09-10

> **Audiencia:** maintainers, reviewers y agentes.
>
> **Estado:** cierre del REAL-RIG ACCEPTANCE GATE (`sky_claw/local/AGENTS.md`
> §2.9 punto 1). Resultado: **T5-V2 PASS** en ambos binarios.
>
> **Alcance de la evidencia:** corridas de aceptación de lanzamiento. NO
> implementa PR-2 ni P0; NO cambia el `-o:` productivo; NO toca #528 ni la
> freshness.

## Scope

**Qué prueba:** el gate de aceptación que bloquea cambios al launch path — dos
corridas reales separadas (una de TexGen, una de DynDOLOD), lanzadas por el
runner de Sky-Claw (`DynDOLODRunner.run_texgen()` / `run_dyndolod()`), con raíz
administrada que contiene espacios, eco `Using Output Path:` exacto, archivos
físicos dentro de esa raíz, preset rancio presente a propósito sin desvío de
escrituras, y restauración verificada de presets/INIs — **por cada ejecutable
por separado**.

**Qué NO prueba:** la precedencia de presets como problema resuelto (se ejercitó
el procedimiento asistido); los criterios 8–10 del checklist T5-v2 del
[roadmap](../design/plans/2026-08-16-dyndolod-roadmap-v2.md) (ZIP, dos mods
disjuntos vía packaging, invariante de visibilidad TexGen → DynDOLOD), que
corresponden al rig de ownership posterior a PR-2; el binding/ownership de P0;
el contrato por herramienta de #528.

## Baseline

- `origin/main` `8770632118a727a4e6e240c0d60026953c0184aa` — `docs(dyndolod):
  define external work root ownership contract (#570)`.
- Ejecución desde extracción `git archive` de ese SHA (sin tocar ningún working
  tree). Identidad del módulo importado, verificada por el conductor en ambas
  corridas (`module_identity.json`, hash idéntico `1b4a698f…`): blob git
  `b6fb4d094a66e87c6063dbb83a3102ce08fb1bd7`, SHA-256 de archivo
  `02ecdb0f1eb885ef336546b00dcc9bacfe79a652736dd7f98bbb39816f75d1a2`.

## Environment

| Dato | Valor |
|---|---|
| Windows | 10.0.19045 (Windows 10 Pro 22H2, AMD64) |
| Python | 3.11.9 (`.venv` del repo) |
| Game (config `game_path`) | `G:\Modding\Skyrim_Runtime_1.6.1170` |
| MO2 install / instance base | `C:\Modding\ModOrganizer2` · `G:/Modding/MO2/SkyrimSE` (perfil `Default`) |
| Data físico (`-d:`) | junction `C:\Modding\DynDOLOD RigTest\_rig_test\data` → `\\?\G:\rig_dyndolod_rigtest_data` (reparse tag `0xA0000003`) |
| `ini_dir` (`-m:`) | `C:\Modding\DynDOLOD RigTest\_rig_test\ini` |
| `plugins_file` (`-p:`) | `C:\Modding\DynDOLOD RigTest\_rig_test\plugins.txt` (baseline controlado vanilla + CC) |
| `temp_dir` (`-t:`) | `E:\Sky-Claw T5 Rig\temp` |
| TexGen | `C:\Modding\DynDOLOD RigTest\TexGenx64.exe` — 35.794.432 B — SHA-256 `0939bc8f8cbae2e1b38f17d56fecd1b7a941dade0554e944886bd7f5c4807a62` — v3.0.0.209 (Sheson) |
| DynDOLOD | `C:\Modding\DynDOLOD RigTest\DynDOLODx64.exe` — 35.794.432 B — SHA-256 `b67625eb7815111ba9ac232c626ff88b07bb64ff30ff5183c5a04b5f045c3bd0` — v3.0.0.209 (Sheson) |
| Raíz del rig | `E:\Sky-Claw T5 Rig` (contiene espacios; externa a juego, SteamApps, MO2, mods, overwrite, tools, TEMP y carpetas de usuario) |

## TexGen

| Campo | Valor observado |
|---|---|
| API pública | `DynDOLODRunner.run_texgen()` (una corrida; sin monkeypatch ni harness) |
| PID / exe | `10256` · `TexGenx64.exe` (observado por psutil + WMI) |
| argv relevante | `-sse` · `-o:E:\Sky-Claw T5 Rig\TexGen Output\` · `-d:…\data\` · `-m:…\ini\` · `-p:…\plugins.txt` · `-t:E:\Sky-Claw T5 Rig\temp\` |
| `Using Output Path:` | `Using Output Path: E:\Sky-Claw T5 Rig\TexGen Output\` — exacto |
| Completion marker | `[00:45] TexGen completed successfully` |
| Root configurado | `E:\Sky-Claw T5 Rig\TexGen Output\` |
| Output físico | `textures\…` — 1362 archivos, ≈132 MB (manifiesto externo completo con SHA-256) |
| Preset stale | `DynDOLOD_SSE_TexGen.ini` reescrito con `OutputPath=E:\Sky-Claw T5 Rig\Stale TexGen\` (sha `a576309a…`); la GUI **pre-cargó el campo Output con el stale** al abrir (dump UIA 16:45:56); el operador corrigió **solo** el campo Output antes de Start |
| Escrituras al decoy | `Stale TexGen`: **0 archivos** (15 destinos vigilados antes/después; solo cambiaron la raíz administrada y los logs) |
| Runner verdict | `success=True`, `rc=0`, artefacto fresco, marker de ESTA corrida (ventana atribuible: 320.494 → 415.277 B, prefijo re-hasheado), sin terminales — 425,6 s |
| Restoration | 3/3 archivos con SHA-256 idéntico al original (`restoration.json`, `ok=true`); el tool re-grabó el preset al pulsar Start y la restauración devolvió los bytes originales |

## DynDOLOD

| Campo | Valor observado |
|---|---|
| API pública | `DynDOLODRunner.run_dyndolod(preset="Medium")` (una corrida) |
| PID / exe | `6948` · `DynDOLODx64.exe` |
| argv relevante | `-sse` · `-o:E:\Sky-Claw T5 Rig\DynDOLOD Output\` · resto idéntico a TexGen |
| `Using Output Path:` | `Using Output Path: E:\Sky-Claw T5 Rig\DynDOLOD Output\` — exacto |
| Completion markers | `[03:30] DynDOLOD plugins generated successfully` · `[03:30] Occlusion.esp completed successfully` |
| Root configurado | `E:\Sky-Claw T5 Rig\DynDOLOD Output\` |
| Output físico | `DynDOLOD.esm` (141.665 B, `7be6c7aa…`), `DynDOLOD.esp` (258.335 B, `321b4048…`), `Occlusion.esp` (9.366.899 B, `4b355c2d…`) — 1117 archivos, ≈672 MB |
| Preset stale | `DynDOLOD_SSE_Default.ini` **creado** por el rig (no existía; registrado) con `OutputPath=E:\Sky-Claw T5 Rig\Stale DynDOLOD\` (59 B, sha `7db22ff3…`). El wizard simple mantuvo el campo Output en el root del `-o:` (16:54:56); **al entrar en Advanced el campo pasó al `OutputPath` del preset** (`Stale DynDOLOD`, 16:58:57); el operador corrigió **solo** el campo Output de vuelta al root administrado (17:00:48) antes de Medium/OK |
| Escrituras al decoy | `Stale DynDOLOD`: **0 archivos**; la salida TexGen de la corrida 1 quedó intacta (0/0/0) y ningún otro destino vigilado cambió |
| Runner verdict | `success=True`, `rc=0`, artefactos frescos, markers de ESTA corrida (ventana atribuible: 966.019 → 1.931.279 B, prefijo re-hasheado), sin terminales — 748,4 s |
| Restoration | preset creado eliminado (ausencia verificada); `DynDOLOD.ini` y `DynDOLOD_SSE.ini` restaurados con SHA-256 idéntico (`ok=true`) |

## Acceptance matrix

| Criterio | TexGen | DynDOLOD |
| --- | ------ | -------- |
| Runner real (API pública del runner de Sky-Claw) | PASS | PASS |
| Root con espacio | PASS | PASS |
| `Using Output Path:` exacto | PASS | PASS |
| Output físico atribuible | PASS | PASS |
| Preset stale ejercitado | PASS | PASS |
| Cero diversion al decoy | PASS | PASS |
| Restoration verificada | PASS | PASS |

## Serialización — observación acotada

La command line de Windows contiene quoting de `subprocess.list2cmdline`
(duplica la barra final de cada argumento entrecomillado). Lo ÚNICO demostrado
aquí: con `TexGenx64.exe` y `DynDOLODx64.exe` Alpha-209, en este entorno real,
para `-o:` con paths con espacios, el valor efectivo observado mediante
`Using Output Path:` coincidió exactamente con el root configurado y los
archivos aterrizaron allí. **NO SERIALIZATION FAILURE OBSERVED** — para este
rig. No se afirma que la serialización sea universalmente correcta ni que
Delphi acepte siempre esta representación. Observación secundaria no
bloqueante: los ecos de `-t:`/`-m:` muestran separadores duplicados
(`temp\\`, `ini\\Skyrim.ini`), tolerados por Windows y sin efecto funcional
observado (temp del rig vacío; INI/plugins resueltos). No se abre fix
preventivo.

## Precedencia de presets — hallazgo, no resuelto

El PASS del gate NO hace inocuo al preset stale. Medido en esta corrida:

- **TexGen:** el preset stale **influyó en el Output inicial** (auto-carga al
  abrir el wizard).
- **DynDOLOD:** el wizard simple mostró el root administrado; la transición a
  **Advanced** aplicó el `OutputPath` del preset stale.
- En ambos, el operador corrigió únicamente Output antes de generar y cero
  outputs aterrizaron en los decoys.

Conclusión permitida: el procedimiento **asistido** satisfizo el gate.
Conclusión NO permitida: la precedencia de presets está resuelta. Stage 9
continúa siendo asistido; la decisión de mecanismo sigue abierta
(`docs/pending_ooda_status.md`, ítem «Preset de TexGen desvía `OutputPath`»).

## Raw evidence externa

La evidencia cruda vive en la máquina del operador y **NO forma parte de este
repositorio** ni es auditable por CI/GitHub por sí sola:

```text
E:\Sky-Claw T5 Rig\evidence\2026-09-10_1640\
```

Huella de los artefactos clave (SHA-256 recalculados al commitear este
documento; permiten detectar alteración futura del paquete externo):

| Artefacto externo | Bytes | SHA-256 |
|---|---|---|
| `session.json` | 8.937 | `e368efb8497f7e98dcd418f5fee50f725237560ec723fb596523ef919e6e3372` |
| `verdict.md` | 5.598 | `8687066c0d845264eee51f73fff44430857f82b93a7d73c8fd643c7aaed1d2c2` |
| `INFORME_T5V2.md` | 11.521 | `f18e957667ea8225463228ed53664c73ab88197cdda0ba8978302f50b1b6ed37` |
| `TexGen/argv.json` | 1.690 | `00b0eae909e27e72fa1db37046944d60fe48bd38a6a16d5dfd6a0921c7ecf38f` |
| `TexGen/result.json` | 2.188 | `b9a4dd9a5e43cc6041faa403588f85766b4dfc084b07eb58b6510d637b59f47e` |
| `TexGen/outputs.sha256` | 196.028 (1362 líneas) | `51deb7e3005b449e89f86f3aa4dda8b51205cabbd7a4225af99665ba63624407` |
| `TexGen/restoration.json` | 1.235 | `78d10d6d5950a112c263a0678d8d4c0177533342ce7593270b7b748b4b397865` |
| `TexGen/preset_stale.json` | 428 | `da67d94bbe0b57bb82d6ec7685e32c843ed8a30fd533c3a1bd5808966c858f2b` |
| `TexGen/module_identity.json` | 601 | `1b4a698f8c5e1a925c1a720adba8b8f291eaf06bd11bf7d3268a3b715e033f91` |
| `DynDOLOD/argv.json` | 1.702 | `6026f8149da30b48cb2cfb7d167fdb0ee2167cdacf6abb4b9ea28284117a818c` |
| `DynDOLOD/result.json` | 32.916 | `5ad565a803c67b46bb801dd5162e9dfb96a168965e560970aff1ea1c9404abab` |
| `DynDOLOD/outputs.sha256` | 166.965 (1117 líneas) | `a3f9f5cbc6d9455bbf8fd60c0bc35af3d8c85905bf00816651070d2850807646` |
| `DynDOLOD/restoration.json` | 1.056 | `3ac4c74263c5a8b16bfd128b88b5e1b195f6b2f17207e88ba2269b6d4d7c09c7` |
| `DynDOLOD/preset_stale.json` | 432 | `a4c2e33c57f85c4091d84b7e0f15e4e7729bc6cf1579edb8a4fac3ed81cbc2b2` |
| `DynDOLOD/module_identity.json` | 601 | `1b4a698f8c5e1a925c1a720adba8b8f291eaf06bd11bf7d3268a3b715e033f91` |

Los manifests `outputs.sha256` no se commitean (196 KB / 167 KB de rutas
relativas de assets): su huella SHA-256 + conteo de líneas permite probar que
el manifest externo no cambió. Los binarios, outputs (≈132 MB / ≈672 MB), logs
completos y screenshots permanecen externos. Sin secretos en este documento.

## Limitaciones

- Prueba Alpha-209 en este entorno; no valida todas las versiones futuras de
  los binarios.
- Stage 9 continúa siendo asistido: la corrección del campo Output fue humana,
  antes de Start.
- El preset stale SÍ afecta el campo Output (TexGen al abrir el wizard;
  DynDOLOD al entrar en Advanced); el PASS no lo resuelve.
- La raw evidence permanece externa al repo (huellada arriba).
- Este PASS NO valida el external per-tool staging de PR-2, NI el
  ownership/binding de P0, NI #528/UIA Output gate, NI los criterios 8–10 del
  checklist T5-v2 (ZIP, dos mods disjuntos, visibilidad billboards), que
  corresponden al rig posterior a PR-2.
- Las corridas se lanzaron contra el Data del rig (junction declarada), sin
  VFS de MO2: es el diseño del gate de lanzamiento; el rig de ownership
  posterior probará el servicio completo.

## Veredicto

```text
T5-V2 PASS
```

TexGen PASS completo y DynDOLOD PASS completo. El REAL-RIG ACCEPTANCE GATE de
`sky_claw/local/AGENTS.md` §2.9 queda cerrado por esta evidencia.
