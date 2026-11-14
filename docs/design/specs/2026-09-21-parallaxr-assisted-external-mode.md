# Especificación Técnica: ParallaxR Assisted External Mode (PR-A0)

> **Estado:** APROBADO (PR-A0).
> **Fecha:** 2026-09-21.
> **Contexto:** `docs/design/research/2026-09-19-pre-lod-material-p0-evidence.md`.

---

## 1. Objetivo

Definir e implementar el primer corte (PR-A0) del modo **PARALLAXR_ASSISTED_EXTERNAL** en Sky-Claw.

Este modo permite a Sky-Claw asistir al usuario en la preparación y validación previa (preflight) de su instalación local de ParallaxR, capturando evidencia inmutable de la herramienta y marcadores de corridas previas, y generando un handoff manual estructurado para que el usuario ejecute el entrypoint oficial `ParallaxR.BAT` a través de Mod Organizer 2 (MO2).

---

## 2. Modelo de Soporte y Relación con B6-L

El adaptador automatizado histórico concebido para invocar helpers internos (`MakeUnpack.exe`, `ExtractBSA.exe`, `LooseCopy.exe`, etc.) permanece bloqueado por **B6-L** (evidencia en `docs/design/research/2026-09-19-pre-lod-material-p0-evidence.md` §7).

**Decisión canónica:**
- **B6-L permanece ABIERTO para `DIRECT_HELPER_API`.**
- El modo `PARALLAXR_ASSISTED_EXTERNAL` **evita completamente dicha interfaz interna**. No "resuelve" ni reinterpreta B6-L.
- `MaterialStepId.PARALLAXR` en `sky_claw/local/tools/material_contract.py` mantiene su estado `MaterialReadiness.BLOCKED` con blocker `MaterialBlocker.B6_L`. PR-A0 no representa la implementación del paso material automatizado.
- `EXTERNAL_TOOL_REGISTRY` permanece congelado en 7 herramientas. ParallaxR no se añade al registro en este PR.
- Política de Sky-Claw:
  - `USER_MANAGED`: El usuario descarga e instala el mod manualmente desde Nexus Mods.
  - `NO_VENDOR` / `NO_BUNDLE` / `NO_REDISTRIBUTE`: Sky-Claw no empaqueta ni distribuye binarios de ParallaxR ni componentes de terceros incluidos por el autor.
  - `NO_MODIFY`: Los archivos de la instalación del usuario no son modificados, parchados ni tocados por Sky-Claw.
  - `OFFICIAL_ENTRYPOINT_ONLY`: La única interfaz reconocida es el script oficial `ParallaxR.BAT`.
  - `NO_AUTO_EXECUTION`: Sky-Claw no ejecuta procesos ni scripts en este modo.

---

## 3. Límites y Fronteras de Responsabilidad

- **Entrada requerida:** Una ruta resuelta a la carpeta de mods de MO2 (`mods_dir: Path`). Sky-Claw no realiza auto-detección no acotada de instancias de MO2 dentro de este módulo.
- **Sin decisión de USVFS:** La evidencia clasifica `R_SUITE_VFS = REAL_RIG_REQUIRED`. Sky-Claw no declara si ParallaxR requiere o no USVFS; el usuario lo ejecuta a través de su entorno habitual de MO2.
- **Aislamiento de carriles:** Cero dependencias o imports de subsistemas de broker VFS (#613), SKSE (#611/#615) o Runtime Vault (#614).

---

## 4. Contrato de Descubrimiento y Resultado

El descubrimiento es determinista para un snapshot estable, no recursivo, side-effect-free respecto del filesystem y estrictamente acotado:

1. Se inspeccionan únicamente los directorios **hijos directos** de `mods_dir`:
   `candidate = child / "ParallaxR.BAT"`
2. Si `candidate` es un archivo regular verificado de forma fail-closed (`stat()` / `lstat()`, sin ocultar errores de inspección ni entradas reparsadas rotas o cíclicas):
   - Se valida su contención física dentro de `mods_dir` con resolución estricta (`resolve(strict=True)`).
   - Se colecta como candidato válido.
3. Resultados posibles en `ParallaxRAssistedPreflight`:
   - 0 candidatos válidos → `status = MISSING`, `success = False`, `message` no vacío.
   - 1 candidato válido → `status = READY`, `success = True`, `message = ""` (o `INVALID_SELECTION`, `success = False` si se especificó una selección explícita divergente).
   - >1 candidatos válidos:
     - Sin selección explícita → `status = AMBIGUOUS`, `success = False`, `message` no vacío (falla cerrado; no se aplica orden alfabético, fecha más reciente ni "primer hallazgo").
     - Con selección explícita que coincide exactamente con uno de los candidatos descubiertos → `status = READY`, `success = True`, `message = ""` para dicho candidato.
     - Con selección explícita externa o no coincidente → `status = INVALID_SELECTION`, `success = False`, `message` no vacío.
   - Mutación concurrente durante captura de evidencia → `status = EVIDENCE_UNSTABLE`, `success = False`, `message` no vacío.
   - Error de inspección o lectura durante el descubrimiento (`mods_dir` o entradas hijas), validación física o fingerprint (`OSError`, `PermissionError`, `WinError 21`/`123`/`1921`, `ELOOP`, junctions o symlinks rotos/circulares) → `status = EVIDENCE_INCOMPLETE`, `success = False`, `message` no vacío sin exponer detalles crudos del sistema. Las funciones públicas `discover_parallaxr_candidates()` y `discover_output_markers()` elevan `ParallaxREvidenceIncompleteError` preservando los hallazgos parciales ya observados; `run_parallaxr_assisted_preflight()` captura esa excepción, conserva `candidate_mod_roots` / `existing_output_candidates` parciales y nunca declara `READY`, `AMBIGUOUS` ni `MISSING` ante evidencia incompleta.

**Regla de resultado:** `success: bool` y `message: str` acompañan al enum `status`. `success == True` se cumple si y solo si `status == READY`, y en dicho caso `message == ""` canónicamente. `success == True` significa **exclusivamente** que el preflight tuvo éxito y se puede generar un handoff; **NO** significa que ParallaxR se haya ejecutado ni completado con éxito.

---

## 5. Validación de Contención de Rutas

Para garantizar la seguridad y evitar escapes de ruta (symlinks maliciosos, reparse points, path traversal):
- Durante el descubrimiento físico de entradas existentes (`mods_dir`, directorio mod y entrypoint/marcador), las rutas se resuelven con `.resolve(strict=True)` acotando la captura de `RuntimeError` por bucle (`"symlink loop"`) a `OSError(errno.ELOOP)` para degradar fail-closed a `EVIDENCE_INCOMPLETE`.
- En la utilidad pública `validate_path_containment()` y al normalizar `explicit_selection` (tras un descubrimiento completo), se utiliza `.resolve(strict=False)` con la misma acotación de bucle para preservar el rechazo determinista (`INVALID_SELECTION`) de rutas seleccionadas inexistentes o irresolubles.
- Se comprueba contención mediante `resolved_path.is_relative_to(resolved_parent)`.
- Se valida la contención tanto de `mod_root` como de `entrypoint` antes de inspeccionar archivos dentro del candidato.
- Todo enlace que resuelva fuera de `mods_dir` es rechazado de inmediato.

---

## 6. Contrato de Fingerprint Coherente (Anti-TOCTOU)

Para evitar inconsistencias donde `size`/`mtime_ns` correspondan a un estado del archivo y `sha256` a otro:
1. **Descriptor único por intento:** Se abre el archivo una sola vez (`handle = file_path.open("rb")`).
2. **Snapshot antes y después:**
   - Se ejecuta `before = os.fstat(handle.fileno())`.
   - Se calcula el hash SHA-256 leyendo chunks de 64 KB desde el **mismo descriptor**.
   - Se ejecuta `after = os.fstat(handle.fileno())`.
   - Se valida estabilidad del descriptor: `st_dev`, `st_ino`, `st_size`, `st_mtime_ns`.
3. **Validación de identidad de ruta:** Tras la lectura del descriptor, se comprueba `path.stat()` contra `after` para confirmar que la ruta no fue reemplazada por otro archivo durante la lectura.
4. **Retry acotado:** Si se detecta mutación en cualquiera de los puntos, se reintenta hasta `_FINGERPRINT_MAX_ATTEMPTS = 2` veces.
5. **Fail-closed:** Si tras agotar los intentos el archivo sigue cambiando, se eleva `ParallaxREvidenceUnstableError` y el preflight degrada a `status = EVIDENCE_UNSTABLE` con `success = False`.
6. **Misma operación para entrypoints y marcadores:** Tanto `ParallaxR.BAT` como `ParallaxROutput.tmp` son capturados mediante la misma función interna `_fingerprint_file_consistently`.
7. **Alcance de la garantía:** La captura garantiza coherencia fotográfica **durante** la operación de fingerprint. No constituye un lock persistente ni previene cambios posteriores al preflight. La revalidación de evidencia corresponderá a futuras fases en el borde de ejecución (PR-A1/A2).
8. **Semántica:** El hash SHA-256 constituye **evidencia local de identidad e inmutabilidad** capturada durante el preflight. **NO** representa firma criptográfica de editor, verificación de autenticidad de upstream ni condición de "código confiable".

---

## 7. Evidencia de Marcadores de Salida Preexistentes

ParallaxR deposita un marcador `ParallaxROutput.tmp` en la carpeta de salida generada.
- Sky-Claw realiza una inspección **read-only** de los directorios hijos directos de `mods_dir` buscando `child / "ParallaxROutput.tmp"`.
- Se extrae metainformación inmutable (`path`, `size_bytes`, `mtime_ns`, `sha256`) mediante captura coherente.
- **Invariantes:**
  - La presencia de marcadores **NO** altera el estado `READY` de la instalación.
  - La presencia de marcadores **NO** implica que el output esté activo/habilitado en el perfil actual de MO2.
  - El hash del marcador es estático entre corridas; por lo tanto, el hash **NO** implica evidencia de frescura.
  - No se eliminan, renombran ni mutan marcadores existentes.

---

## 8. Handoff Manual (`ParallaxRManualHandoff`)

Función de transformación pura `prepare_parallaxr_manual_handoff(preflight)`:
- Requiere estrictamente `preflight.status == ParallaxRAssistedStatus.READY` y `preflight.success is True`.
- Falla cerrado ante cualquier otro estado (`MISSING`, `AMBIGUOUS`, `INVALID_SELECTION`, `EVIDENCE_UNSTABLE`, `EVIDENCE_INCOMPLETE`).
- No vuelve a abrir el archivo ni a calcular hashes; traslada fielmente la evidencia capturada por el preflight.
- Estructura inmutable resultante:
  - `mode`: `"manual_official_entrypoint"`
  - `entrypoint`: `Path` al `ParallaxR.BAT` validado.
  - `mod_root`: `Path` a la carpeta del mod.
  - `fingerprint`: `ParallaxRInstallationEvidence` capturada.
  - `requires_user_launch`: `True`
  - `recommended_launcher`: `"MO2"`
  - `direct_helper_invocation`: `False`
  - `sky_claw_launches_process`: `False`
  - `instruction`: `"Ejecutá el entrypoint oficial de ParallaxR desde MO2."`
  - `success`: `True`
  - `message`: `""`

---

## 9. Acciones Prohibidas

El módulo productivo `sky_claw/local/tools/parallaxr_assisted.py` tiene estrictamente prohibido:
1. Importar o invocar `subprocess`, `Popen`, `asyncio.create_subprocess_*`, `os.system` o runners de procesos de Sky-Claw.
2. Hacer referencia en código a los ejecutables de helpers internos (`MakeUnpack.exe`, `ExtractBSA.exe`, `LooseCopy.exe`, `Exclusions.exe`, `ParallaxRFilter.exe`, `HeightMap.exe`, `OutputQC.exe`, `AntiSleep.ps1`). El único entrypoint de terceros admitido es `ParallaxR.BAT`.
3. Escribir, modificar, renombrar, eliminar, tocar (`touch`) o cambiar permisos (`chmod`) sobre ningún archivo o carpeta.
4. Modificar la configuración de MO2, perfiles o `modlist.txt`.
5. Realizar peticiones de red o descargas.

---

## 10. Slices Futuros (Documentación de Alcance)

- **PR-A1: Observación de corrida manual.**
  - Operador notifica "Ya inicié ParallaxR".
  - Observación read-only del proceso en ejecución sin control de ciclo de vida ni kill.
  - Registro de ventana temporal de inicio.
- **PR-A2: Validación post-corrida.**
  - Inspección de `mtime_ns` del marcador dentro de la ventana de corrida.
  - Validación de logs generados y descarte de colisiones con texturas `_p.dds` existentes.
- **PR-A3: Superficie GUI / HITL.**
  - Selector interactivo ante estado `AMBIGUOUS`.
  - Visualización del preflight e instrucciones de handoff.
  - Botones de acción "Ya inicié" / "Cancelar observación".
