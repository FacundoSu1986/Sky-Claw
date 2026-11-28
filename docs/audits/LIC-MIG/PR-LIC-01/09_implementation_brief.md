# PR-LIC-01 — 09 · Implementation brief (implementación **no autorizada todavía**)

**Estado:** propuesta. No se ejecutó ninguno de estos cambios.
**Fase de implementación sugerida:** PR-LIC-04 (avisos y empaquetado), con las dependencias de
PR-LIC-02 que se indican.
**Holds:** `GPL_MIGRATION_HOLD` **ACTIVO** (la licencia del proyecto **no** cambia) ·
`RELEASE_PUBLICATION_HOLD` **ACTIVO** (no se publica nada).
**STOP:** tras este documento el trabajo se detiene hasta autorización explícita.

---

## 1. Objetivo del cambio

Convertir los avisos de terceros de **documentación dispersa** en un **artefacto verificable y
entregable**:

1. una fuente de verdad legible por máquina de los componentes efectivamente distribuidos;
2. los textos de licencia que hoy no existen en el repo;
3. `THIRD_PARTY_NOTICES.md` **generado** de esa fuente (no editado a mano);
4. los avisos **dentro del ejecutable** y accesibles desde el propio programa;
5. un validador fail-closed que impida que un componente redistribuido quede sin texto o sin aviso.

## 2. Write-set exacto propuesto

| # | Archivo | Acción | Por qué (justificación por archivo) |
|---|---|---|---|
| W1 | `licenses/third_party/COMPONENTS.json` | **NUEVO** (~124 entradas) | fuente de verdad: mismos campos que la matriz; permite validar en CI y regenerar los avisos. Sin esto, el `.md` vuelve a desincronizarse en el primer PR |
| W2 | `licenses/third_party/texts/*.txt` (16 archivos) | **NUEVO** | textos estándar agrupados (MIT, Apache-2.0, BSD-2/3, ISC, PSF-2.0, MPL-2.0, LGPL-2.1-or-later, GPL-2.0-or-later-WITH-Bootloader-exception, OFL-1.1, Zlib, CC0, 0BSD, Unlicense, MIT-0, HPND) |
| W3 | `licenses/third_party/component_texts/<componente>/…` (≈14 archivos) | **NUEVO** | textos **agregados o específicos** que no pueden agruparse: `numpy`, `pillow`, `grpcio`, `pycryptodomex`, `aiohttp`+`llhttp`, `certifi` (MPL-2.0 íntegro), `prometheus-client` (decorator BSD-2), `python-json-logger` (NOTICE), 4 × OFL de fuentes con su copyright, avisos Apache-2.0 de iconos/Roboto |
| W4 | `licenses/third_party/README.md` | **NUEVO** | qué es cada carpeta, de dónde salió cada texto (URL + fecha) y cómo se regenera. Sin esto, el paquete se vuelve arqueología |
| W5 | `scripts/generar_avisos_terceros.py` | **NUEVO** | genera `THIRD_PARTY_NOTICES.md` desde W1 y **valida** (ver §6). Un solo generador, sin duplicar lógica |
| W6 | `THIRD_PARTY_NOTICES.md` | **MODIFICADO** (regenerado) | hoy cubre sólo Hermes; pasa a cubrir los 118 componentes redistribuidos, preservando la sección de Hermes **textualmente** |
| W7 | `pyproject.toml` | **MODIFICADO** (2 líneas) | `license = "MIT"` + `license-files = ["LICENSE"]` (PEP 639). Hoy el paquete **no declara licencia**; el binario sí. Cambio de metadatos, **no** de régimen legal |
| W8 | `sky_claw/app/licenses.py` | **NUEVO** (~120 líneas) | localiza el paquete de avisos (`sys._MEIPASS` si está congelado, repo si no), lo copia a un directorio y lo imprime si hay stdout. Sin red, sin GUI, sin estado |
| W9 | `sky_claw/__main__.py` | **MODIFICADO** (~10 líneas) | bandera de salida temprana `--licenses [DIR]`, al mismo nivel que `--vfs-worker` (antes del parser, sin tocar `--mode`). Escritura de archivos porque el EXE es `console=False`: imprimir sin más no entrega nada |
| W10 | `sky_claw.spec` | **MODIFICADO** (1 línea en `datas`) | `("licenses/third_party", "licenses/third_party")`. Sin esto los avisos no viajan en el EXE y el punto 4 queda incumplido |
| W11 | `tests/test_licencias_terceros.py` | **NUEVO** (~250 líneas) | AAA + validaciones negativas (§6) |
| W12 | `docs/development/licencias_terceros.md` | **NUEVO** (breve) | cómo se agrega un componente y cómo se regenera; enlazado desde `CONTRIBUTING.md` (opcional, 1 línea) |

**No se toca:** `LICENSE`, `.github/workflows/*` (B15 y `release.yml` quedan como están), `scripts/`
de PR-LIC-03A/B, `requirements*.lock`, `uv.lock`, `build.bat`, ningún asset gráfico, ninguna fuente,
ningún archivo de Auto Parallax ni del bridge MO2.

Tamaño del cambio: **≈ 12 archivos, ~45 archivos contando los textos**, +≈ 1,5 MB en el repo
(≈ 300 KB dentro del EXE comprimido con UPX).

## 3. Cambios mínimos: lo que **no** se hace (y por qué)

| Tentación | Por qué no |
|---|---|
| Un `LICENSE` por componente | 41 MIT idénticos: ruido que oculta los textos que sí difieren |
| Recortar el LICENSE de numpy "a lo importante" | recortar un aviso agregado es exactamente el incumplimiento que se quiere evitar |
| Generar el aviso en tiempo de release en vez de versionarlo | el aviso debe ser revisable en el PR, no un artefacto efímero de CI |
| Reescribir `THIRD_PARTY_NOTICES.md` a mano | se desincroniza; el generador + test lo impide |
| Mostrar las licencias sólo en la GUI | la GUI no arranca en CLI/headless; el EXE debe cumplir sin ella |
| Aprovechar para "arreglar" la licencia del proyecto | `GPL_MIGRATION_HOLD` lo prohíbe |
| Incluir en el aviso los 94 paquetes del lock | 1 no es alcanzable (`sv-ttk`); declarar lo que no se distribuye también es un error |

## 4. Estrategia de empaquetado de los avisos legales

Cuatro capas, de la más cercana al destinatario a la más lejana:

| Capa | Mecanismo | Estado tras PR-LIC-04 |
|---|---|---|
| 1. Dentro del EXE | `datas` (W10) → disponible en `sys._MEIPASS/licenses/third_party/` | **implementado** |
| 2. Desde el propio programa | `SkyClawApp.exe --licenses [DIR]` (W8/W9): copia el paquete a `DIR` (por defecto `%LOCALAPPDATA%\SkyClaw\licenses`) e imprime la ruta; si hay consola, además imprime el texto | **implementado** |
| 3. Repositorio | `licenses/` + `THIRD_PARTY_NOTICES.md` en el commit que produjo el build (el `build_manifest` ya registra el commit) | **implementado** |
| 4. Junto al artefacto de release | `SkyClawApp.exe` + `THIRD_PARTY_NOTICES.md` + `licenses/` en el mismo zip | **requiere decisión del propietario** (cambia la forma del release: hoy es un EXE suelto). Se especifica, no se asume |

Nota técnica: en un onefile de PyInstaller los `datas` se extraen a un directorio temporal y
**desaparecen al cerrar**. Por eso la capa 2 copia los archivos a una ruta persistente: sin eso, la
capa 1 existe pero es inalcanzable para el usuario (y sería un cumplimiento de fachada).

## 5. Interacción con la GUI (opcional, no mínima)

`--mode gui` → "Acerca de" → "Licencias de terceros" que lista `COMPONENTS.json` y abre la carpeta
copiada. Se propone como **PR-LIC-04b** separable: no es necesaria para cumplir, y agregar UI tiene
su propio costo de tests.

## 6. Tests AAA y validaciones negativas (fail-closed)

Naming en español, patrón del repo (`tests/test_*.py`). Todos deterministas y sin red.

### 6.1 Estructura AAA

| Test | Arrange | Act | Assert |
|---|---|---|---|
| `test_componentes_json_esquema` | JSON del repo | valida esquema | 23 campos presentes en cada entrada, sin duplicados de `component_id` |
| `test_cada_componente_redistribuido_tiene_texto` | matriz + `COMPONENTS.json` | busca el texto | toda entrada con `attribution_required=SI` resuelve a un archivo existente **y** con hash coincidente |
| `test_textos_no_estan_truncados` | texto en repo vs texto fuente sellado | comparación de hash/longitud | igualdad exacta (no "empieza igual") |
| `test_notice_generado_coincide_con_el_archivo` | generador | regenera en memoria | el resultado es **idéntico byte a byte** a `THIRD_PARTY_NOTICES.md` |
| `test_licencia_del_paquete_declarada` | `pyproject.toml` | parsea | `project.license == "MIT"` y `license-files` incluye `LICENSE` |
| `test_spec_incluye_los_avisos` | `sky_claw.spec` | parsea `datas` | contiene `licenses/third_party` |
| `test_cli_licencias_escribe_archivos` | tmp_path, monkeypatch de `sys._MEIPASS` | `--licenses` | los archivos existen, el conteo coincide con `COMPONENTS.json` |
| `test_hermes_preservado` | texto actual de la sección Hermes | render final | la sección sigue presente con los mismos datos de licencia/copyright |

### 6.2 Validaciones negativas (el test **debe** fallar si…)

1. …un componente redistribuido no tiene texto → `ValueError` con el `component_id`.
2. …un texto contiene marcadores `TODO`/`PENDIENTE`/`⚠` → falla (nada de placeholders publicados).
3. …un `license_concluded` vacío no está marcado como no resuelto en `unresolved_questions` → falla.
4. …se agrega un componente a `COMPONENTS.json` sin entrada en la matriz (fantasma) → falla.
5. …se quita de la matriz un componente que sigue en el lock sin justificar → falla (drift detectado).
6. …`--licenses` no encuentra el paquete de avisos (build roto) → **exit code ≠ 0** con mensaje
   explícito; nunca "sale bien sin hacer nada".
7. …el hash de un texto en el repo no coincide con el registrado → falla (detección de edición
   silenciosa de un texto legal).

## 7. Riesgos y mitigación

| Riesgo | Prob. | Impacto | Mitigación |
|---|---|---|---|
| Aumentar el tamaño del EXE | alta | bajo | textos ~300 KB comprimidos; se mide en el primer build controlado |
| `--licenses` interactuando con `console=False` | media | medio | escribir siempre a archivo; probar en el EXE congelado (test opt-in, skip en Linux) |
| Divergencia entre matriz (fase de auditoría) y `COMPONENTS.json` | alta | medio | el generador **deriva** de la matriz y el test de drift falla si se separan |
| Publicar un aviso que declare cumplimiento inexistente | media | **alto** | prohibido por el test de placeholders + revisión del texto; §4.4 del borrador declara lo pendiente |
| Copiar un texto con copyright equivocado | media | alto | sólo se copian textos con hash sellado; los copyright salen del artefacto o dicen explícitamente que no existen |
| Que el cambio se lea como "migración a GPL" | media | **alto** | `pyproject.toml` declara MIT; `LICENSE` intacto; el aviso lo dice en §1 |
| Regeneración del NOTICE con nombres en español en un documento en inglés | alta | bajo | PR-LIC-04 normaliza `component_name` a inglés en `COMPONENTS.json` (los nombres de la matriz son de auditoría) |

## 8. Rollback

- El cambio es **puramente aditivo** salvo tres archivos (`THIRD_PARTY_NOTICES.md`, `pyproject.toml`,
  `sky_claw.spec`) y un punto de entrada (`__main__.py`).
- Rollback = `git revert <commit>`: no hay migraciones, ni estado persistente, ni datos de usuario.
- El único efecto residual posible es un directorio de avisos copiado en `%LOCALAPPDATA%\SkyClaw\licenses`
  (borrable; no se toca nada más del perfil del usuario).
- No hay interacción con locks, CI ni release: los workflows no cambian.

## 9. Estimación de esfuerzo

| Bloque | Horas |
|---|---|
| W1 `COMPONENTS.json` + normalización de nombres | 4 |
| W2/W3 textos + verificación de hashes | 3 |
| W5 generador + W6 regeneración | 3 |
| W8/W9 `--licenses` + W10 spec | 3 |
| W11 tests AAA + negativos | 5 |
| W4/W12 documentación | 1.5 |
| Revisión, ruff/mypy, ajustes | 2.5 |
| **Total** | **≈ 22 h** (2,5–3 días de un ingeniero) |

Dependencias que **pueden** recortar alcance: si PR-LIC-02 no resolvió aún `webencodings`,
`sass.dart.js`, `docutils`, `grpcio`-MPL y `certifi`-fuente, esos ítems entran al aviso **marcados
como pendientes** (no bloquean PR-LIC-04, sí la declaración de "avisos completos").

## 10. Exclusiones explícitas de alcance

No forman parte de PR-LIC-04 (ni de este brief):

1. Cambiar `LICENSE` de MIT a GPL-3.0, ni evaluar esa migración.
2. Resolver Auto Parallax (misión separada; sigue `REMEDIATION_REQUIRED`).
3. Resolver la calificación jurídica de la frontera MO2.
4. Implementar el mecanismo LGPL (fuente correspondiente, relinking) → **PR-LIC-02**.
5. Decidir sobre los gráficos → **PR-LIC-05** (requiere al propietario).
6. Reemplazar/regenerar assets o fuentes.
7. Reactivar B15 o `release.yml`, publicar EXE, tocar Cosign.
8. Modificar `scripts/` de PR-LIC-03A/B, locks, `build.bat` o workflows.
9. Reescribir `auto_parallax_regla.py` o el bridge.
10. Crear el mecanismo de "código fuente correspondiente" para LGPL/MPL (declararlo no es cumplirlo).
11. Certificar cumplimiento, compatibilidad de licencias o aptitud legal del producto.

## 11. Criterio de "listo para revisar" (no "listo para publicar")

- [ ] `python -m pytest tests/test_licencias_terceros.py` verde **y** los 7 negativos fallan cuando deben.
- [ ] `ruff` y `mypy` sin errores nuevos.
- [ ] `THIRD_PARTY_NOTICES.md` regenerado == generado (test de drift).
- [ ] `SkyClawApp.exe --licenses` produce archivos en un build controlado local (sin publicar).
- [ ] El aviso declara los pendientes de `07_unresolved_legal_obligations.md` sin adornos.
- [ ] `LICENSE` sigue siendo MIT y `GPL_MIGRATION_HOLD`/`RELEASE_PUBLICATION_HOLD` siguen vigentes.
