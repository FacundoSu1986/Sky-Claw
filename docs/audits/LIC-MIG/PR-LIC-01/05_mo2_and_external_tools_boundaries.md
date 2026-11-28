# PR-LIC-01 — 05 · Fronteras de integración MO2 y herramientas externas (LIC01-F)

**Pregunta de la fase:** ¿qué de la integración con Mod Organizer 2 es código propio, qué es API
externa, qué se ejecuta *dentro* del proceso de MO2 y qué viaja realmente en `SkyClawApp.exe`?
**Restricción respetada:** ninguna modificación de integración, ningún cambio de código.

---

## 1. Reconciliación del bloqueo `mo2_integration_boundary_pending_legal`

La misión advierte que "la última referencia era de 10 archivos" y ordena verificarlo. Resultado:

| Fuente | Valor | Naturaleza |
|---|---|---|
| Misión (referencia previa) | `mo2_integration_boundary_pending_legal=10` | recuerdo de un build preservado (no disponible) |
| `tests/test_build_controlado.py:76` (fixture) | `mo2_integration_boundary_pending_legal=8` | **fixture de test**, no medición real |
| `scripts/reconciliar_sbom.py:1342` | `len(set(mo2_bridge_files))` | el valor se **calcula** del inventario |
| Repositorio (hoy) | **4 archivos fuente**: `__init__.py`, `plugin.py`, `protocol.py`, `runtime.py` (1755 líneas) | `ls` + `wc -l` |

**Inferencia explícita (marcada como tal, no como hecho):** con 4 archivos fuente, el CArchive
contiene 4 entradas de datos (`datas` del spec) y el PYZ contiene 4 entradas de bytecode con nombre
punteado (`sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.*`), ambas ramas de
`_clasificar_entrada_base`. Eso da **8** entradas, que es exactamente el número del fixture. Las
**10** podrían incluir entradas de directorio (`…/skyclaw_bridge` y su padre), que el clasificador
también captura por prefijo.

**Conclusión honesta:** *no puede afirmarse* cuál es el número real en el ejecutable actual sin el
Analysis TOC (EVIDENCE_MISSING). Lo que sí se afirma: **el conjunto de archivos no creció** (4
fuentes, mismo contenido declarado), y el bloqueo debe seguir vigente mientras no exista revisión
jurídica. **No se "resolvió" el bloqueo bajando el número.**

## 2. Qué hay exactamente en la frontera

### 2.1 Código propio que se ejecuta dentro del proceso de MO2

`sky_claw/local/mo2/plugin_bundle/skyclaw_bridge/` — plugin Python de MO2:

| Archivo | Líneas | Importa del entorno de MO2 |
|---|---|---|
| `plugin.py` | 320 | **`import mobase`**, `from PyQt6.QtCore import QCoreApplication, QObject, Qt, pyqtSignal, pyqtSlot, qInfo, qWarning` |
| `protocol.py` | 143 | stdlib |
| `runtime.py` | 1284 | stdlib (`ctypes`, `subprocess`, …) — "núcleo testeable sin imports de mobase/PyQt" |
| `__init__.py` | 8 | — |

Verificado por grep en todo el repo: **`mobase` y `PyQt6` se importan únicamente en `plugin.py`**.

### 2.2 La frontera es de proceso, no de licencia

El diseño es deliberadamente defensivo: socket local autenticado, digest del bundle, hilo Qt
dedicado, comandos de allowlist. **Todo eso es excelente ingeniería y ningún argumento jurídico.**
La misión es explícita: *no concluir automáticamente que una frontera de proceso garantiza
independencia jurídica*. Y hay tres hechos que impiden concluirla:

1. El plugin **se carga dentro del proceso de MO2** (importa `mobase` y `PyQt6`, usa `qInfo`,
   `pyqtSignal`, `QueuedConnection`): comparte espacio de direcciones e inicialización de Qt.
2. Depende de la **API interna** de MO2 (`mobase` expone objetos de MO2) y de sus **convenciones de
   plugin**.
3. Se **distribuye**: viaja en el EXE (`datas`) y además `bridge_installer.py` lo **copia** al árbol
   de la instancia MO2 del usuario.

Ninguno de los tres hechos *decide* el análisis de obra derivada. Pero los tres lo dejan abierto, y
por eso la fila de la matriz queda en `LEGAL_REVIEW_REQUIRED`, no en "sin obligación".

### 2.3 Lo que NO viaja en el ejecutable

| Componente | Por qué no | Verificación |
|---|---|---|
| MO2 core / `mobase` | el EXE no importa `mobase` (sólo el bundle del plugin, que se ejecuta en MO2) | grep en `sky_claw/` |
| `PyQt6` / Qt | no está en `requirements.lock` ni en `pyproject.toml`; no lo importa ningún módulo que el EXE cargue | grep en locks y pyproject: **sin coincidencias** |
| `usvfs` | lo provee MO2 en runtime | — |
| xEdit / SSEEdit, LOOT, Synthesis, DynDOLOD | herramientas que instala el usuario; Sky-Claw sólo las invoca | `sky_claw/local/xedit/`, `tools_installer` |

## 3. Licencias de las piezas externas (texto primario, ref fija)

| Componente | Repo/ref | Licencia verificada | Archivo |
|---|---|---|---|
| Mod Organizer 2 | `ModOrganizer2/modorganizer` `master` @ `4ba22f0d8b6f` | **GPL-3.0** (texto íntegro) | `evidence/github/mo2/LICENSE` |
| `usvfs` | `ModOrganizer2/usvfs` `master` @ `57f1ea5e6ad1` | **GPL-3.0-or-later** (preámbulo explícito) | `evidence/github/mo2-usvfs/LICENSE` |
| Plugin Python (`mobase`) | `ModOrganizer2/modorganizer-plugin_python` `master` | **SIN archivo de licencia** (verificado por API: `license: None`; el árbol raíz no contiene `LICENSE`/`COPYING`) | `evidence/github/mo2-python-plugin/README.md` |
| PyQt6 / Qt | Riverbank / The Qt Company | **GPL-3.0** o comercial | no recuperado en esta fase (EVIDENCE_MISSING) |

**Hallazgo incómodo y relevante:** el repositorio que produce `mobase` **no declara licencia**. Es
decir, no existe una "licencia de mobase" citable: sólo puede inferirse el paraguas GPL-3.0 de MO2.
Cualquier texto que afirme "mobase está bajo X" sin esta salvedad sería inventado.

## 4. Preservación de los bloqueos (obligatoria)

| Bloqueo | Estado exigido | Estado observado |
|---|---|---|
| `mo2_integration_boundary_pending_legal` | vigente | **vigente**: `reconciliar_sbom.py` lo emite siempre que haya archivos del bridge; `build_controlado.py` lo clasifica como blocker jurídico conocido y **no** lo convierte en aprobación |
| `pending_legal_review` (activos IA, Auto Parallax) | vigente | **vigente**: el clasificador del inventario marca `PENDIENTE_REVISION_JURIDICA` para `IA_DECLARADA_O_C2PA`, `ADAPTACION_UPSTREAM_GPL` y `MO2_PLUGIN_IN_PROCESS` |
| `legal_certification` | **false** siempre | confirmado en `build_controlado.py` (`"legal_certification": False`) |

El reconciliador ya emite el paquete SPDX `SPDXRef-Package-SkyClaw-MO2-Bridge` con
`licenseDeclared: MIT`, `licenseConcluded: NOASSERTION` y
`compatibility_assessment=NOASSERTION`. **Eso es correcto y este informe no lo cambia**: PR-LIC-01
no tiene autoridad para convertir ese `NOASSERTION` en una conclusión.

## 5. Preguntas que quedan para revisión jurídica (no técnicas)

1. ¿El plugin `skyclaw_bridge` es obra separada de MO2 o queda alcanzado por GPL-3.0 al enlazarse
   contra `mobase`/Qt en el mismo proceso?
2. Si se concluyera lo segundo: ¿qué implicaría para un producto que hoy se distribuye como MIT?
3. ¿La copia del bundle al árbol de MO2 (`bridge_installer`) constituye distribución de una obra
   que interactúa con GPL-3.0? ¿Cambia algo que el destinatario sea el mismo usuario?
4. ¿Qué obligaciones nacen de que `PyQt6` sea GPL-3.0 en el entorno del operador?

Ninguna de estas preguntas se responde en PR-LIC-01. Se registran para **PR-LIC-02**, que es donde
vive el mecanismo de cumplimiento.
