# Runbook P7 — provisionar un Golden Master desde una copia Stock verificada

- **Estado:** preparado por P7-A1 (2026-11-05). **No ejecutado** sobre instalaciones reales.
- **Alcance:** `Stock verificado → Generation registrada → Runtime Clone independiente`.
  El **Desired Runtime** y el **binding a MO2** siguen siendo operaciones del binder
  con aprobación del propietario y **NO** forman parte de este runbook.
- **Regla de oro:** nada de este runbook se ejecuta sin aprobación explícita del
  propietario para cada mutación (§4). Steam, MO2, Skyrim, la copia Stock y el
  runtime manual existente **no se modifican**.

## 1. Modelo mental y límites

```text
preparación (operador, aprobada)                provisionamiento (código P7-A1)
──────────────────────────────────              ───────────────────────────────────
Stock (intacto, sólo lectura)                   adoptar_copia_stock()
  + copia NUEVA "fuente candidata"  ──hash──►   verifica contra una REFERENCIA
    (copia de contenido, sin                   independiente (TreeDigest +
     hardlinks/reparse; + SKSE                  RuntimeIdentity + críticos +
     del build exacto, etc.)                    membership de directorios)
                                                publicar_generation_desde_stock()
                                                └─► versions/<generation-id>/
                                                    + sidecar v2 (referencia
                                                      completa embebida)
                                                    + metadata provider="stock"
                                                crear_runtime_clone_desde_generation()
                                                └─► Clone en <clone-parent>/<id>/
                                                    PROVISIONED (setup SKSE
                                                    content-bound)
```

La referencia y el sidecar son **schema v2**: el sidecar embebe la referencia
completa y el loader recomputa `reference_digest`, así que un sidecar con sólo
64 caracteres (v1) no se acepta: hay que **re-publicar la Generation** con el
código actual. La `directory_membership` (incluye directorios vacíos) se
verifica a tres vías: referencia == sidecar == árbol observado.

**Qué NO implementa P7-A1 (gaps declarados, no disimulados):**

1. **`RuntimeSetupManifest` completo (SFR-22).** Existe una evidencia mínima
   `RuntimeSetupEvidence` (hashes de `SkyrimSE.exe` + loader y DLL de SKSE del
   build exacto) y `PROVISIONED` exige esa compatibilidad, pero **no** hay store
   de manifests ni aplicación/reproducción de setup por versión. En este
   runbook, los archivos de setup (SKSE, root files) se incorporan a la **fuente
   candidata** antes de la referencia; no se "inventan" al crear el Clone.
2. **Desired Runtime / binder productivo.** `active.json` no se toca;
   `EMISION_PRODUCTIVA_HABILITADA=False`. El binding a MO2 requiere un paso
   posterior con aprobación y, para el preflight actual, **Managed Source IDLE**.
3. **Procedencia Steam de la copia Stock.** La referencia es un ancla del
   operador: detecta drift y evita que el escritor auto-autorice otro árbol, pero
   por sí sola no demuestra que la copia provenga de Steam. Debe cruzarse contra
   una captura de Managed Source cuando Steam esté IDLE (paso futuro, P7-A2 /
   P3b) y quedar registrado así.

## 2. Preconditions (verificar, no asumir)

| # | Chequeo | Evidencia |
|---|---|---|
| P1 | Storage Frozen Runtime **no existe** o está explícitamente aprobado para inicializarse | `C:\Users\Facu2\.sky_claw\frozen-runtime` ausente en P7-A0 |
| P2 | No hay MO2/Skyrim/SKSE running | `Get-CimInstance Win32_Process` |
| P3 | Steam no está siendo manipulado por el operador; estado actual informativo (puede estar ACTIVE: el provisionamiento local **no** lo necesita) | appmanifest + `steamapps\downloading` (sólo lectura) |
| P4 | Backup de MO2/config/Steam-manifest ya hecho y hashes verificados (§5) | manifest de hashes |
| P5 | Espacio libre suficiente (§3) | `Get-PSDrive` / `shutil.disk_usage` |
| P6 | Referencia Stock independiente construida, revisada y aprobada (§6, A1) | JSON de referencia + `reference_digest` |
| P7 | Aprobaciones A1–A4 registradas (§4) | registro de aprobación del propietario |

## 3. Elegir storage root y estimar espacio

**Admisión (`admitir_storage_root`):** ruta absoluta; sin symlink/junction/reparse
en root ni ancestros; si existe debe ser directorio; nombre no reservado de
Windows; **fuera** de `steamapps` / `steamapps\common`; sin solape con la Managed
Source. El **Clone no puede vivir dentro del storage root** (ni el storage dentro
del Clone). Tampoco puede estar dentro de `steamapps`.

Sugerencias de layout (una vez aprobado; **no crear todavía**):

```text
<storage-root>\            p. ej. G:\Modding\FrozenRuntime   (~17–20 GiB)
  versions\<generation>\
  state\{active.json, generations\, clones\, ...}
<clone-parent>\            p. ej. G:\Modding\FrozenClones     (~17–20 GiB)
  <clone-id>\
```

`<clone-parent>` **no** lo crea el provisionador: se crea y se admite en la
**preparación de A4** (§7, paso A4-prep), antes de publicar la Generation. El
código exige que exista, sea directorio y no esté redirigido; sin ese paso A4b
fallaría después de haber publicado la Generation.

**Estimación conservadora (árbol Stock = 16,96 GiB / 16.207 archivos):**

| Operación | Espacio requerido | Nota |
|---|---|---|
| Fuente candidata preparada (Stock + SKSE) | ≈ 17 GiB | copia de contenido, sin hardlinks |
| Publicar Generation | ≈ 17 GiB + margen 512 MiB | copia completa desde la fuente |
| Crear Runtime Clone | ≈ 17 GiB + margen 512 MiB | copia completa desde la Generation |
| Referencia Stock (JSON) | < 5 MiB | fuera de la fuente |
| **Total recomendado libre** | **≈ 36–38 GiB en el volumen del storage + 17 GiB en el del clone-parent** | pueden ser el mismo volumen: ≈ 55 GiB |

La API estima antes de copiar (`ProvisioningSpaceError` si no alcanza). Un ENOSPC
a mitad deja un árbol parcial **sin metadata** (UNKNOWN, no destructivo): no se
borra automáticamente.

## 4. Autorizaciones explícitas (una por mutación)

| ID | Operación | Escribe en | Aprobación requerida |
|---|---|---|---|
| A1 | Construir la **referencia independiente** y aprobar su `reference_digest` | archivo de referencia fuera de Stock | propietario (firma/digest registrado) |
| A2 | Inicializar el **storage** (`initialize_frozen_runtime_storage`) | `<storage-root>` | propietario (es una mutación productiva) |
| A3 | Preparar la **fuente candidata** (copia Stock + SKSE/root files) | directorio nuevo fuera de Stock/Steam | propietario |
| A4 | **Publicar Generation** y **crear Clone** (dos comandos separados) | storage + clone-parent | propietario; backup previo verificado |
| A5 | Registrar **Desired Runtime** / **binding MO2** (binder) | `active.json`, `modorganizer.ini`, config Sky-Claw | **fuera de P7-A1**: requiere habilitar emisión productiva + Steam IDLE + HITL |
| A6 | Ejecutar P7-A…P7-G reales | lanzamientos MO2/SKSE/Skyrim | propietario; sólo en ventana controlada |

> `EMISION_PRODUCTIVA_HABILITADA` **no** se cambia en este runbook. Si A5 se
> autoriza en el futuro, el cambio de esa constante es un PR propio con auditoría.

## 5. Backups con hashes SHA-256 (antes de cualquier mutación)

| Objeto | Contenido | Verificación |
|---|---|---|
| Instancia MO2 | `…\ModOrganizer\Skyrim Special Edition\ModOrganizer.ini` + `profiles\prueva huevo 3\` | copia + `Get-FileHash` SHA-256 por archivo |
| Base MO2 | `G:\Modding\MO2\SkyrimSE\{profiles,overwrite}` (y `mods/downloads` si se respalda completo) | conteo + hashes de texto; tamaños de binarios |
| Config Sky-Claw | `C:\Users\Facu2\.sky_claw\config.toml` | copia + SHA-256 |
| `appmanifest_489830.acf` | copia de **evidencia**; **nunca** editar | SHA-256 |
| Runtime/Stock actuales | inventario + hashes de archivos críticos (evidencia P7-A0) | `inventory_*.jsonl` + `backup_hashes.json` |
| Fuente candidata (nueva) | referencia JSON + SHA de los binarios de setup | `reference_digest` + hashes |

Destino sugerido: `I:\Skyclaw_Backups\P7-A1_<fecha>\` (607 GB libres en P7-A0) o
`E:\Skyclaw_A1_Evidence\`. **Restauración:** con MO2 cerrado, copiar el snapshot y
comparar SHA-256; para `active.json`/journal usar sólo el recovery del binder
(nunca edición manual); Steam no se restaura.

## 6. Construir la referencia independiente (A1, READ-ONLY sobre la fuente)

```powershell
$py = "E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe"
$wt = "E:\Skyclaw_Main_Sync\.worktrees\<worktree-P7-A1>"   # o el checkout aprobado
& $py -c @"
import json, pathlib, sys
sys.path.insert(0, r'$wt')
from sky_claw.local.frozen_runtime import construir_referencia_stock
fuente = pathlib.Path(r'<RUTA_FUENTE_CANDIDATA>')
salida = pathlib.Path(r'<RUTA_EVIDENCIA>\stock_reference.json')
ref = construir_referencia_stock(fuente, reference_kind='p7-a1-owner-approved')
salida.write_text(json.dumps(ref.como_dict(), indent=2), encoding='utf-8')
print('reference_digest=', ref.reference_digest)
print('tree_digest=', ref.expected_tree.digest, 'files=', ref.expected_tree.files, 'bytes=', ref.expected_tree.bytes)
print('membership_digest=', ref.directory_membership.digest, 'dirs=', ref.directory_membership.directory_count)
print('runtime=', ref.expected_runtime)
"@
Get-FileHash '<RUTA_EVIDENCIA>\stock_reference.json' -Algorithm SHA256
```

El propietario revisa `reference_digest`, árbol/identidad, **membership de
directorios** y **aprueba A1**. Sin esa aprobación no se sigue. La referencia se
guarda fuera de la fuente; si la fuente cambia un solo byte **o un directorio
vacío** (que el TreeDigest no ve), `adoptar_copia_stock` la rechaza. Una
referencia capturada con schema v1 es incompatible: recapturarla con el código
actual (schema v2) y volver a aprobar A1.

## 7. Provisionamiento (A2–A4, comandos separados)

```powershell
$py = "E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe"
$wt = "E:\Skyclaw_Main_Sync\.worktrees\<worktree-P7-A1>"
& $py -c @"
import json, pathlib, sys
sys.path.insert(0, r'$wt')
from sky_claw.local.frozen_runtime import (
    initialize_frozen_runtime_storage, cargar_referencia_stock,
    adoptar_copia_stock, publicar_generation_desde_stock,
    admitir_clone_root_path,
)

storage = pathlib.Path(r'<STORAGE_ROOT>')
fuente  = pathlib.Path(r'<RUTA_FUENTE_CANDIDATA>')
ref = cargar_referencia_stock(
    json.loads(pathlib.Path(r'<RUTA_EVIDENCIA>\stock_reference.json').read_text(encoding='utf-8'))
)

# A2 (sólo si el propietario aprobó la inicialización; idempotente y no destructivo)
print(initialize_frozen_runtime_storage(storage))

# A3 verificación de la fuente contra la referencia (READ-ONLY)
copia = adoptar_copia_stock(fuente, ref)
print('ADOPTADA', copia.reference_digest, copia.tree_digest)

# A4-prep (aprobación A4): crear y admitir el padre del Clone ANTES de publicar.
# `crear_runtime_clone_desde_generation` exige que ya exista: NO lo crea.
padre = pathlib.Path(r'<CLONE_PARENT>')
if not padre.exists():
    padre.mkdir(parents=True, exist_ok=False)
assert padre.is_dir(), f'el padre del Clone no es directorio: {padre!r}'
contenido = {p.name for p in padre.iterdir()}
assert contenido <= {'<CLONE_ID>'}, f'el padre del Clone tiene contenido ajeno al Clone: {sorted(contenido)}'
# Admisión READ-ONLY de la ruta del Clone: fuera de storage/versions, steamapps, etc.
admitido = admitir_clone_root_path(padre / '<CLONE_ID>', storage_root=storage)
print('clone_parent_admitido=', admitido.parent)

# A4a publicar Generation (sólo si A4-prep pasó)
res = publicar_generation_desde_stock(storage, copia)
print(res.success, res.message)
assert res.success, res.message
print('generation_id=', res.generation_id)
"@
```

Luego (A4b, en una invocación separada):

```powershell
& $py -c @"
import pathlib, sys
sys.path.insert(0, r'$wt')
from sky_claw.local.frozen_runtime import crear_runtime_clone_desde_generation
res = crear_runtime_clone_desde_generation(
    pathlib.Path(r'<STORAGE_ROOT>'),
    '<GENERATION_ID>',
    pathlib.Path(r'<CLONE_PARENT>'),
    'clone-golden-1.6.1170',
)
print(res.success, res.message)
assert res.success, res.message
print('clone_root=', res.clone_root)
print('runtime_setup_id=', res.runtime_setup_id)
print('lifecycle=', res.clone_record.lifecycle)
"@
```

Verificaciones posteriores (READ-ONLY):

```powershell
& $py -c @"
import pathlib, sys
sys.path.insert(0, r'$wt')
from sky_claw.local.frozen_runtime import verificar_generation, verificar_runtime_clone, verificar_runtime_setup
storage = pathlib.Path(r'<STORAGE_ROOT>')
print(verificar_generation(storage, '<GENERATION_ID>').state)
print(verificar_runtime_clone(storage, 'clone-golden-1.6.1170').state)
print(verificar_runtime_setup(pathlib.Path(r'<CLONE_PARENT>\clone-golden-1.6.1170')).setup_id)
"@
```

**Criterio de éxito de A4:** Generation `VALID`; Clone `PROVISIONED` y
`verificar_runtime_clone` `VALID`; `st_nlink==1` en todo el Clone; sin reparse;
sin inodos compartidos con la Generation ni con la Managed Source; el árbol de la
fuente y de la Generation no cambió (hashes).

**Atomicidad y procedencia (P7-A1):** publicación y creación corren dentro de
**una sola** sección de `participant_lock` (copy → verificación → sidecar →
metadata → verificación final). El **sidecar se escribe antes que la metadata**:
si el sidecar falla, no queda una Generation “válida” sin procedencia. El sidecar
v2 embebe la referencia completa y se cruza contra la metadata de autoridad
(provider `stock`, tree, runtime, críticos) y contra la membership observada.
Qué revisar en el mensaje si falla: “sidecar … obsoleto” ⇒ re-publicar con el
código actual; “no coincide con la referencia/metadata” ⇒ drift o tamper,
detenerse y restaurar desde backup, **no** editar el JSON a mano.

## 8. Cierre/aborto y recuperación

- **Abortar antes de A2/A4** si falla cualquier precondición, la referencia no
  está aprobada, el espacio es insuficiente, hay journal/recovery pendiente o un
  proceso MO2/Skyrim/SKSE está vivo.
- **Fallo al publicar Generation:** el sidecar se escribe antes que la metadata;
  un fallo previo deja árbol y/o sidecar sin metadata (`UNKNOWN`/no
  descubrible), sin borrado automático. Decidir con el propietario si se retira
  manualmente tras verificar que no hay metadata asociada. `state/` no queda con
  journal: el provisionamiento no escribe journal.
- **Fallo al crear Clone:** si el árbol quedó copiado y el registro quedó en
  `PROVISIONING` (fallo entre registrar y publicar `PROVISIONED`), **reintentar
  con el mismo id/Generation/parent completa el mismo Clone** (recupera y
  re-verifica; no lo pide borrar). Si quedó un árbol **sin registro**, o un
  registro `PROVISIONED` **sin árbol**, el reintento falla cerrado con
  “reconciliación manual requerida”: no se borra ni se adopta nada solo.
- **Nunca** borrar/reemplazar archivos de Steam, MO2, Stock o del runtime manual
  como parte de este runbook.
- **Crash/kill testing:** SÓLO en entorno desechable (storage y árboles sintéticos
  en `%TEMP%`/`tmp_path`), nunca sobre instalaciones reales.

## 9. Después del Clone: uso con MO2 (fases posteriores, no este runbook)

1. **A5 (binder):** registrar `Desired Runtime` y repuntar `gamePath` de MO2 con
   el binder existente, con aprobación HITL y `EMISION_PRODUCTIVA_HABILITADA`
   habilitada en un PR propio. Requisitos actuales del preflight: Managed Source
   **IDLE** (hoy ACTIVE), sin procesos MO2/Skyrim/SKSE, journal coherente.
2. **P7-A … P7-G (ver §10).** Hasta que A5 no se ejecute, el inspector P7 seguirá
   `BLOCKED` (desired_runtime/clone_record/version.skyrim/mo2.game_path).

## 10. Matriz P7 y umbrales

| ID | Prueba | Evidencia requerida | PASS | Abortar si |
|---|---|---|---|---|
| P7-A | abrir MO2 desde el Clone | log `mo_interface.log` «using game plugin … at <clone>»; INI `gamePath`; captura UI | abre y `gamePath`=Clone | persiste defaults, error visible, gamePath≠Clone |
| P7-B | SKSE desde MO2 | `mo_interface.log` + `usvfs-*.log` con `skse64_loader.exe` y `SkyrimSE.exe` desde el Clone | cadena MO2→SKSE arranca | arranca desde Steam/launcher |
| P7-C | ruta real del proceso | `Win32_Process`/psutil: `ExecutablePath` de `SkyrimSE.exe` y loader | ruta = `<clone>\SkyrimSE.exe` | ruta fuera del Clone |
| P7-D | arranques repetidos sin pasos manuales | N arranques consecutivos; logs/PIDs; estado Steam; sin interacción manual | **N≥5** arranques OK (umbral propuesto; requiere aprobación) | prompt de Steam, downgrade manual, falla un arranque |
| P7-E | update de Steam no toca el Clone | inventario + hashes del Clone antes/después; appmanifest/manifest sólo lectura; hash del appmanifest antes/después | inventario/hashes del Clone idénticos; **Steam puede cambiar sus propios hashes** sin contar como fallo | cambia un archivo del Clone o Sky-Claw escribió una superficie de Steam |
| P7-F | recuperación/rollback reales | journal/transiciones/recovery; hashes de las 4 superficies; pruebas de kill en entorno **desechable** | journal restaura runtime operativo y superficies | journal corrupto/UNKNOWN, superficies divergentes |
| P7-G | SFR-21 sin manipular Steam | hashes del appmanifest/config Steam antes/después; sin solo-lectura ni restauración; inventario del Clone igual | Sky-Claw no escribió superficies de Steam y SFR-21 se cumple | cualquier escritura de Sky-Claw a Steam o dependencia de downgrade manual |

**Umbral D:** N no está definido por el repo; la propuesta `N≥5` (al menos 2 con
update pendiente y 3 después de la actualización real, o secuencia equivalente
aprobada por el propietario) debe quedar aprobada antes de ejecutar P7-D.

## 11. Responsables y trazabilidad

- Operador: `Facundo` (propietario de las instalaciones reales y aprobador).
- Ejecución técnica: sesión P7 con un worktree propio y el PR de P7-A1 como base.
- Evidencia: carpeta `E:\Skyclaw_A1_Evidence\` (referencia, hashes, salidas de
  cada comando, manifest de evidencias).
- Este runbook **no autoriza** por sí mismo ninguna mutación: cada paso remite a
  una aprobación A1–A6.
