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

> **Conciliación operativa 2026-11-07 (A1–A4 ejecutadas, A5/A6 pendientes):**
> la referencia histórica de P1 (`C:\Users\Facu2\.sky_claw\frozen-runtime`
> ausente en P7-A0) corresponde al chequeo previo al bootstrap. En las fases
> A1–A4 ejecutadas el 2026-11-07 quedaron creados la referencia A1, el
> almacenamiento A2 en `G:\Modding\FrozenRuntime`, la Generation
> `1.6.1170.0__7949c58cfd27` y el clon `clone-p7-01` en `G:\Modding\FrozenClones\clone-p7-01`.
> No repetir esas operaciones ni alterar esos artefactos sin nueva aprobación.

## 3. Elegir storage root y estimar espacio

**Admisión (`admitir_storage_root`):** ruta absoluta; sin symlink/junction/reparse
en root ni ancestros; si existe debe ser directorio; nombre no reservado de
Windows; **fuera** de `steamapps` / `steamapps\common`; sin solape con la Managed
Source. El **Clone no puede vivir dentro del storage root** (ni el storage dentro
del Clone). Tampoco puede estar dentro de `steamapps`.

El operador debe verificar **antes de A2** que `<storage-root>` no esté contenido
en ni contenga la fuente Stock ni la Managed Source. `admitir_storage_root`
rechaza `steamapps/common` (y el root/ancestros `steamapps`), pero **no todos los
subdirectorios de `steamapps`**: una ruta dentro de `steamapps/staging`,
`steamapps/downloading` o `steamapps/temp` es área administrada por Steam y debe
rechazarse manualmente (§7, gates previos a A2).

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

**Registro de la aprobación:** el propietario guarda el `reference_digest`
aprobado en un archivo aparte (`<RUTA_EVIDENCIA>\stock_reference.approved_digest.txt`)
y hashea también ese archivo. `cargar_referencia_stock` sólo prueba la
integridad interna de la referencia; **no** puede probar que sea la aprobada en
A1, así que A2/A3 comparan explícitamente contra ese registro (§7).

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
managed_source = pathlib.Path(r'<MANAGED_SOURCE_ROOT>')
padre = pathlib.Path(r'<CLONE_PARENT>')

# Gate A1: comparar contra el digest aprobado por separado (el loader sólo prueba
# integridad interna). Si no coincide, abortar sin tocar storage.
aprobado = pathlib.Path(r'<RUTA_EVIDENCIA>\stock_reference.approved_digest.txt').read_text(encoding='utf-8').strip()
ref = cargar_referencia_stock(
    json.loads(pathlib.Path(r'<RUTA_EVIDENCIA>\stock_reference.json').read_text(encoding='utf-8'))
)
if ref.reference_digest != aprobado:
    raise SystemExit('reference_digest no coincide con el aprobado en A1: ABORTAR')
if not managed_source.is_dir():
    raise SystemExit(f'Managed Source no es directorio: {managed_source!r}')

# Gates previos a A2: storage no solapa fuente/Managed Source ni queda en steamapps.
for otro in (fuente, managed_source):
    if storage == otro or otro in storage.parents or storage in otro.parents:
        raise SystemExit(f'solape storage/otro: {storage} vs {otro}')
if any(p.casefold() == 'steamapps' for p in storage.parts):
    raise SystemExit('storage dentro de steamapps: área administrada por Steam')

# A2 (sólo si el propietario aprobó la inicialización; idempotente y no destructivo)
print(initialize_frozen_runtime_storage(storage))

# A3 verificación de la fuente contra la referencia (READ-ONLY; rechaza solapes)
copia = adoptar_copia_stock(fuente, ref, storage_root=storage, managed_source_root=managed_source)
print('ADOPTADA', copia.reference_digest, copia.tree_digest)

# A4-prep (aprobación A4): ADMITIR la ruta ANTES de crear nada (no crear dentro de
# steamapps ni bajo un ancestro junction); luego crear el padre si falta y RE-ADMITIR.
admitido = admitir_clone_root_path(
    padre / '<CLONE_ID>', storage_root=storage, managed_source_root=managed_source
)
if not padre.exists():
    padre.mkdir(parents=True, exist_ok=False)
if not padre.is_dir():
    raise SystemExit(f'el padre del Clone no es directorio: {padre!r}')
admitido = admitir_clone_root_path(
    padre / '<CLONE_ID>', storage_root=storage, managed_source_root=managed_source
)
contenido = {p.name for p in padre.iterdir()}
if not contenido <= {'<CLONE_ID>'}:
    raise SystemExit(f'el padre del Clone tiene contenido ajeno al Clone: {sorted(contenido)}')
print('clone_parent_admitido=', admitido.parent)

# A4a publicar Generation (sólo si A4-prep pasó)
res = publicar_generation_desde_stock(storage, copia)
print(res.success, res.message)
if not res.success:
    raise SystemExit(res.message)
print('generation_id=', res.generation_id)
"@
```

Luego (A4b, en una invocación separada):

```powershell
& $py -c @"
import pathlib, sys
sys.path.insert(0, r'$wt')
from sky_claw.local.frozen_runtime import crear_runtime_clone_desde_generation
managed_source = pathlib.Path(r'<MANAGED_SOURCE_ROOT>')
if not managed_source.is_dir():
    raise SystemExit(f'Managed Source no es directorio: {managed_source!r}')
res = crear_runtime_clone_desde_generation(
    pathlib.Path(r'<STORAGE_ROOT>'),
    '<GENERATION_ID>',
    pathlib.Path(r'<CLONE_PARENT>'),
    '<CLONE_ID>',
    managed_source_root=managed_source,
)
print(res.success, res.message)
if not res.success:
    raise SystemExit(res.message)
print('clone_root=', res.clone_root)
print('runtime_setup_id=', res.runtime_setup_id)
print('lifecycle=', res.clone_record.lifecycle)
"@
```

Verificaciones posteriores (READ-ONLY):

```powershell
& $py -c @"
import json, pathlib, sys
sys.path.insert(0, r'$wt')
from sky_claw.local.frozen_runtime import (
    StockSourceError, adoptar_copia_stock, cargar_referencia_stock, leer_runtime_clone_record,
    verificar_generation, verificar_runtime_clone, verificar_runtime_setup,
)
storage = pathlib.Path(r'<STORAGE_ROOT>')
fuente = pathlib.Path(r'<RUTA_FUENTE_CANDIDATA>')
managed_source = pathlib.Path(r'<MANAGED_SOURCE_ROOT>')
ref = cargar_referencia_stock(
    json.loads(pathlib.Path(r'<RUTA_EVIDENCIA>\stock_reference.json').read_text(encoding='utf-8'))
)
# Re-verificar la fuente tras A4: si cambió (bytes O membership), adoptar falla.
try:
    adoptar_copia_stock(fuente, ref, storage_root=storage, managed_source_root=managed_source)
except StockSourceError as exc:
    raise SystemExit(f'FUENTE CAMBIÓ tras A4: {exc}')
print(verificar_generation(storage, '<GENERATION_ID>').state)
print(verificar_runtime_clone(storage, '<CLONE_ID>', managed_source_root=managed_source).state)
# El setup observado debe coincidir con el verificado al publicar el registro:
# un cambio de SKSE compatible-pero-distinto dejaría VALID sin este chequeo.
registro = leer_runtime_clone_record(storage, '<CLONE_ID>')
setup_id = verificar_runtime_setup(pathlib.Path(registro.root_path)).setup_id
if setup_id != registro.verified_runtime_setup_id:
    raise SystemExit('setup ID observado no coincide con el registro: ABORTAR')
print(setup_id)
"@
```

**Criterio de éxito de A4:** Generation `VALID`; Clone `PROVISIONED` y
`verificar_runtime_clone` `VALID`; `st_nlink==1` en todo el Clone; sin reparse;
sin inodos compartidos con la Generation ni con la Managed Source; el árbol de la
fuente **y su membership** no cambiaron (la re-adopción post-A4 lo demuestra);
`reference_digest` aprobado == cargado.

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

### 8.1 Taxonomía de recuperación y rollback del binder (P7-A5 PR 2)

El subsistema transaccional distingue cuatro cuadrantes operativos rigurosos para la gestión de anomalías:

1. **Rollback en proceso con autoría conocida:**
   - Ocurre dentro del ciclo de vida del mismo proceso cuando se intercepta un fallo antes de `FINALIZE` (p. ej. error de preflight bajo lock, fallo en persistencia de config o divergencia de readback tras liberar la exclusión).
   - El binder conserva en memoria la evidencia de autoría (`ini_escritura_intentada` y la huella `ini_bytes_tras_escritura` leída bajo el handle de exclusión OS).
   - **Guard fail-closed de evidencia suficiente:** si la transición mutó el INI pero no se pudo obtener `ini_bytes_tras_escritura` bajo exclusión, el binder aborta con `BinderVerificationError` antes de CAS/FINALIZE. La autoría se degrada a desconocida (`ini_escritura_intentada = None`), impidiendo falsos éxitos ante mutaciones ajenas no comprobables.
   - Si el disco refleja exactamente lo escrito por la transición o un prefijo rasgado propio, restaura atómicamente el estado previo desde el snapshot durable.
   - Si se detecta mutación externa posterior a la liberación de exclusión, rechaza la autoría, no sobrescribe el archivo en disco y bifurca a preservación forense.

2. **Recuperación en frío sin autoría demostrable:**
   - Ocurre al invocar `recuperar_transicion_interrumpida()` desde un proceso nuevo tras reinicio, crash inesperado o terminación abrupta.
   - Al carecer de contexto volátil de autoría (`escritura_intentada is None`), el mecanismo opera **fail-closed**.
   - Cualquier archivo cuyo contenido difiera del snapshot inicial es clasificado como `escritura_ajena = True`.
   - **No se sobrescribe a ciegas el archivo en disco.** El contenido divergente se preserva en `storage/recovery_evidence/<transition_id>/` y se emite `requiere_dueno = True`.

3. **Intervención explícita del propietario:**
   - Ni el binder ni la recuperación automática resuelven escrituras ajenas sin autorización humana.
   - Ante clasificaciones de `escritura_ajena` o journals en `PENDING_*`, el propietario debe auditar la evidencia forense archivada.
   - Opciones soberanas del operador:
     a) Descartar la mutación ajena y forzar restauración del baseline pasando `escritura_intentada=True`.
     b) Adoptar el estado existente en disco y sincronizar la FSM mediante `journal.reconciliar_journal()`.

4. **Garantías de corte eléctrico no establecidas:**
   - `POWER_LOSS_GUARANTEE = NOT_CLAIMED`.
   - El sistema garantiza y verifica recuperación ante caídas de proceso (`PROCESS_CRASH_RECOVERY = DEMONSTRATED`) mediante `os.replace` atómico dentro del mismo volumen.
   - **No se promete ni reclama durabilidad ante cortes de energía o caídas del kernel** (sin WAL transaccional, sin `fsync` a nivel de directorio ni barreras de caché en controlador físico).

## 9. Después del Clone: uso con MO2 (fases posteriores, no este runbook)

1. **A5 (binder):** registrar `Desired Runtime` y repuntar `gamePath` de MO2 con
   el binder existente, con aprobación HITL y `EMISION_PRODUCTIVA_HABILITADA`
   habilitada en un PR propio.
   - **Nota P7-A5 PR 1 (solo lectura):** `evaluar_binder_preflight` y
     `scripts/p7_readonly_inspection.py` incorporan `BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION`
     (procedencia Stock v2 + digest externo A1 aprobado), admisión explícita de
     `outgoing_manual_runtime_root` e identidad activa de instancia MO2
     (`MO2_INSTANCE_IDENTITY`), manteniendo `STRICT_PROVIDER_IDLE` por defecto y
     `EMISION_PRODUCTIVA_HABILITADA = False`. La migración transaccional de
     `[customExecutables]` y la habilitación productiva pertenecen a PRs posteriores.
   - **Nota P7-A5 PR 3 / PR 4 (cableado y transporte Telegram, emisión cerrada):**
     - **PR 3 + #909 + #914 (consumidor autorizado):** desde 2026-11-11 existe
       `sky_claw/local/frozen_runtime/autorizacion_hitl_transicion.py`
       (`HITL_PRODUCTIVE = WIRED_BUT_GATED`), ampliado con **#909**
       (`2146a27f82de1651cd431d0508ac818502d54a2c`: evidencia de config con
       `config_path`/`skyrim_path` obligatorio, diagnóstico y
       `abandonar_transicion_consumida` pre-PENDING en
       `transition_abandonment.py`) y **#914**
       (`a076ec2ce5f5b437fac76e0879d42cb3bcf5a242`: vínculo criptográfico
       `mo2auth=<sha256>` en `mo2_authorization.py` que liga instancia,
       identidad, bytes completos de `modorganizer.ini`, política y runtime
       saliente en `ejecutar()` y `reanudar()`).
     - **PR 4 (adaptador de transporte Telegram):**
       `sky_claw/app/comms/golden_master_hitl.py` cablea el protocolo sobre
       `TelegramWebhook`, `TelegramPolling`, `TelegramSender` y
       `owner_consent.verificar_identidad_telegram` desde updates crudos, con
       coordinador single-flight fuera del event loop en `AppContext`,
       `telegram_mode` y `frontend_bridge` (`TRANSPORT_ADAPTER = WIRED_BUT_GATED`).
     - **Estado real de cada gate (vigente):**
       `EMISION_PRODUCTIVA_HABILITADA = False`,
       `HITL_PRODUCTIVE = WIRED_BUT_GATED`,
       `TRANSPORT_ADAPTER = WIRED_BUT_GATED`,
       `PRIMERA_TRANSICION_REAL = NOT_EXECUTED`,
       `CANDIDATE_OBJECT = NOT_CREATED`,
       `REANUDACION_POST_PENDING = REQUIERE_DUENO`,
       `MODORGANIZER_EXE_COMPAT = NOT_MEASURED_ON_LIVE_MO2` (exclusión Win32
       `FILE_SHARE_NONE` medida contra agregadores sintéticos; comportamiento
       de `ModOrganizer.exe` en vivo ante `ERROR_SHARING_VIOLATION` sin medir),
       `POWER_LOSS_GUARANTEE = NOT_CLAIMED` (#911).
     - **Restricción `ACTIVE_GLOBAL` vs MO2 portable:** el preflight y la
       autorización (`evaluar_binder_preflight`, `MO2_INSTANCE_IDENTITY`) exigen
       que `mo2_instance_dir` coincida con la instancia activa global registrada
       en HKCU/AppData (`ACTIVE_GLOBAL`); una instancia portable no registrada
       como activa global queda clasificada `AMBIGUOUS` y falla cerrado en el
       selector de instancia.
     **Corrección de este runbook (§9.1):** el requisito de A5 **no** es
     uniformemente «Steam IDLE». Bajo `STRICT_PROVIDER_IDLE` (por defecto) Steam
     debe estar IDLE; bajo `STOCK_INDEPENDENT_TRANSITION` se admite Steam
     **ACTIVE** siempre que haya digest externo A1 aprobado, procedencia Stock v2
     y se conserven las comprobaciones de independencia física del Clone (un
     hardlink sigue bloqueando). La política efectiva que se aprueba viaja en el
     `approval_provenance` de la aprobación.
     **Reanudación:** si un fallo ocurre **antes** de publicar el `PENDING`, la
     misma aprobación consumida completa la transición
     (`reanudar(transition_id)`) — y sólo si el estado actual sigue coincidiendo
     campo por campo con lo que el propietario aprobó (instancia de MO2, hash
     del INI, política, procedencia); si algo cambió, se rechaza.
     Si el `PENDING` ya está publicado, la aprobación está gastada y el caso
     requiere al dueño: `reanudar` en ese estado es **sólo diagnóstico y no
     escribe nada**, y la recuperación se pide explícitamente
     (`binder_recovery.recuperar_transicion_interrumpida` +
     `journal.reconciliar_journal` + nueva aprobación). No existe re-emisión
     automática.
2. **P7-A … P7-G (ver §10).** Hasta que A5 no se ejecute efectivamente, el inspector P7 seguirá
   `BLOCKED` (`listo_para_p7=False`: `desired_runtime`/`clone_record`/`version.skyrim`/`mo2.game_path`).

## 10. Matriz P7 y umbrales

| ID | Prueba | Evidencia requerida | PASS | Abortar si |
|---|---|---|---|---|
| P7-A | abrir MO2 desde el Clone | log `mo_interface.log` «using game plugin … at <clone>»; INI `gamePath`; captura UI | abre y `gamePath`=Clone | persiste defaults, error visible, gamePath≠Clone |
| P7-B | SKSE desde MO2 | `mo_interface.log` + `usvfs-*.log` con `skse64_loader.exe` y `SkyrimSE.exe` desde el Clone | cadena MO2→SKSE arranca | arranca desde Steam/launcher |
| P7-C | ruta real del proceso | `Win32_Process`/psutil: `ExecutablePath` de `SkyrimSE.exe` y loader | ruta = `<clone>\SkyrimSE.exe` | ruta fuera del Clone |
| P7-D | arranques repetidos sin pasos manuales | N arranques consecutivos; logs/PIDs; estado Steam; sin interacción manual | **N≥5** arranques OK (umbral propuesto; requiere aprobación) | prompt de Steam, downgrade manual, falla un arranque |
| P7-E | update de Steam no toca el Clone | inventario + hashes del Clone antes/después; appmanifest/manifest sólo lectura; hash del appmanifest antes/después | inventario/hashes del Clone idénticos; **Steam puede cambiar sus propios hashes** sin contar como fallo | cambia un archivo del Clone o Sky-Claw escribió una superficie de Steam |
| P7-F | recuperación/rollback reales | journal/transiciones/recovery; hashes de las 4 superficies; pruebas de kill en entorno **desechable** | journal restaura runtime operativo y superficies | journal corrupto/UNKNOWN, superficies divergentes |
| P7-G | SFR-21 sin manipular Steam | **atribución de escrituras por proceso** (p. ej. handles/ETW) sobre `appmanifest_*.acf`, `steamapps/staging/**`, `steamapps/downloading/**`, `steamapps/temp/**` y config del cliente; inventario del Clone antes/después; sin solo-lectura ni restauración. Hashes antes/después **NO** alcanzan: si Steam escribe durante la prueba no identifican al autor | Sky-Claw no escribió ninguna superficie de Steam (atribuido por proceso) y SFR-21 se cumple | cualquier escritura de Sky-Claw a Steam o dependencia de downgrade manual |

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
