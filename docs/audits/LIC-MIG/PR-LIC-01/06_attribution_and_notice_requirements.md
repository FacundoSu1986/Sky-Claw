# PR-LIC-01 — 06 · Requisitos de atribución y diseño del paquete de avisos

**Pregunta:** ¿qué avisos son *obligatorios*, y cómo se entregan a quien recibe el software, sabiendo
que el producto se distribuye como **un único `SkyClawApp.exe`**?

---

## 1. Obligación por licencia (sólo lo exigible, sin adornos)

| Licencia | Texto | Copyright | NOTICE | Fuente | Notas |
|---|---|---|---|---|---|
| MIT | **sí** | **sí** | no | no | "The above copyright notice and this permission notice shall be included in all copies" |
| MIT-0 | no | no | no | no | sin cláusula de atribución (`cffi`, `h11`) |
| BSD-2 / BSD-3 | **sí** | **sí** | no | no | BSD-3 prohíbe usar nombres de titulares para promocionar |
| ISC | **sí** | **sí** | no | no | equivalente a MIT en la práctica |
| Apache-2.0 | **sí** | **sí** | **sí si el componente trae NOTICE** (§4d) | **sí** si se modifica (§4b) | incluye concesión de patentes y cláusula de terminación |
| PSF-2.0 | **sí** | **sí** | — | — | CPython y 4 paquetes |
| MPL-2.0 | **sí** | **sí** | — | **sí, por archivo cubierto** (§3.1/3.2) | copyleft débil a nivel archivo |
| LGPL-2.1-or-later | **sí** | **sí** | — | **sí** (obra cubierta) | relinking/reemplazo: obligación de ingeniería, no de texto |
| GPL-2.0-or-later **WITH Bootloader Exception** | **sí** | **sí** | — | **sí si se modifica** | el embedding está autorizado por la excepción |
| OFL-1.1 | **sí** | **sí** | — | no | RFN: ver documento 04 |
| Zlib / 0BSD / CC0-1.0 / Unlicense | no exigible | no | no | no | se conservan por cortesía y trazabilidad |
| HPND (Pillow) | **sí** | **sí** | no | no | "the above copyright notice … appear in all copies" |

**Regla que se aplica en todo el paquete:** se distribuye **el texto que viajaba en el artefacto
exacto** cuando existe (prueba de lo recibido); el texto canónico de SPDX sólo se usa como referencia
o cuando el artefacto **no** trae texto (certifi, avisos de NiceGUI, fuentes, PyInstaller, CPython,
OpenSSL, libffi).

## 2. El problema central: se distribuye un EXE, no un repositorio

Los avisos en el repositorio de GitHub **no** cumplen la obligación frente a quien recibe el
ejecutable. Hay que decidir cómo viajan. Opciones evaluadas:

| Opción | Cumple | Riesgo | Veredicto |
|---|---|---|---|
| A. Sólo `THIRD_PARTY_NOTICES.md` en el repo | **no** | el destinatario del EXE nunca lo ve | descartada |
| B. Embeber el paquete de avisos dentro del EXE (`datas`) | **sí**, si es accesible | en onefile los archivos viven en `sys._MEIPASS` (temporal): no son "acompañantes" visibles | **necesaria pero no suficiente** |
| C. B + comando `--licenses` que los imprime/extrae | **sí** | superficie nueva mínima, sin red, sin efectos | **recomendada** |
| D. B + "Acerca de → Licencias de terceros" en la GUI | **sí** para el usuario de GUI | depende de que la GUI arranque | **recomendada como complemento** |
| E. `THIRD_PARTY_NOTICES.md` + `licenses/` como archivos **junto al EXE** en el artefacto de release (zip) | **sí**, y es lo más robusto | exige cambiar la forma del release (hoy se publica un EXE suelto) | **recomendada para PR-LIC-04** (decisión del propietario) |
| F. Página web externa con los avisos | parcial | no controlamos permanencia; no es "acompañar" la copia | descartada como único mecanismo |

**Conclusión de diseño:** C + D son el mínimo que hace que la obligación se cumpla *desde el propio
programa distribuido*; E es el refuerzo que conviene acordar con el propietario porque el hold de
release está por levantarse recién en PR-LIC-04. No se implementa nada en esta fase.

## 3. Estructura propuesta (evaluada, no asumida)

No existe hoy ningún directorio `licenses/`. Se evaluó `licenses/third_party/` y se propone:

```
licenses/
├── README.md                      # qué es esto y cómo se regenera (1 página)
├── third_party/
│   ├── COMPONENTS.json            # FUENTE DE VERDAD: 124 filas, mismos campos que la matriz
│   ├── texts/
│   │   ├── MIT.txt                # textos agrupados por licencia (deduplicados)
│   │   ├── Apache-2.0.txt
│   │   ├── BSD-3-Clause.txt
│   │   ├── LGPL-2.1-or-later.txt
│   │   ├── OFL-1.1.txt
│   │   ├── GPL-2.0-or-later-WITH-Bootloader-exception.txt
│   │   └── … (16 textos + los específicos por componente)
│   └── component_texts/           # textos que NO son estándar (numpy, pillow, grpcio,
│       │                          # pycryptodomex, aiohttp/llhttp, certifi-MPL, python-json-logger)
│       └── numpy-2.4.6/LICENSE.txt
└── (nada más: sin duplicar por componente lo que ya está agrupado)
```

Justificación de las decisiones:

| Decisión | Alternativa descartada | Por qué |
|---|---|---|
| Textos **agrupados por licencia** + índice de componentes | una carpeta de licencia por cada uno de los 94 paquetes | 41 componentes MIT producirían 41 copias idénticas del mismo texto: ruido que oculta las diferencias reales |
| `COMPONENTS.json` como fuente de verdad | editar `THIRD_PARTY_NOTICES.md` a mano | el .md se desincroniza en el primer PR; el JSON se puede validar en CI |
| `component_texts/` para los agregados | recortar el LICENSE de numpy a "la parte que importa" | recortar un aviso agregado es exactamente lo que genera incumplimientos |
| `THIRD_PARTY_NOTICES.md` **generado** del JSON + prosa propia en secciones estables | dos fuentes | el archivo que lee un humano y el que valida CI deben ser el mismo |

## 4. Contenido mínimo de `THIRD_PARTY_NOTICES.md` (borrador en documento 08)

Orden propuesto, pensado para que sirva tanto a un humano como a una auditoría:

1. **Alcance y versión**: qué build, qué commit, qué perfiles (`base`, `native-parallax`).
2. **Licencia del proyecto** (MIT, sin cambios: `GPL_MIGRATION_HOLD` vigente).
3. **Cómo obtener los textos**: ruta dentro del programa (`--licenses`), ruta en el repo, archivos
   acompañantes si existen.
4. **Componentes por licencia**: tabla `componente · versión · copyright` agrupada por texto.
5. **Componentes con licencia agregada** (numpy, pillow, grpcio, pycryptodomex): remisión al texto
   completo en `component_texts/`, con la lista de terceros que contiene cada uno.
6. **Avisos especiales**:
   - PyInstaller bootloader (GPL-2.0-or-later **WITH Bootloader Exception**) y sus rthooks Apache-2.0.
   - CPython (PSF-2.0) y los terceros de su distribución Windows.
   - OpenSSL (Apache-2.0), SQLite (dominio público), libffi (MIT).
   - Fuentes OFL-1.1 (con RFN de Cinzel) y fuentes Apache-2.0 de los iconos.
   - Recursos gráficos (con el estado real de procedencia, sin adornos).
7. **Copyleft débil**: componentes LGPL-2.1-or-later y MPL-2.0, con remisión explícita a la
   obligación de fuente que **no** se satisface con este aviso (ver §5).
8. **Atribuciones preexistentes preservadas**: Hermes Agent (Nous Research, MIT).
9. **Bloqueos vigentes**: Auto Parallax (`LEGAL_REVIEW_REQUIRED`) y frontera MO2
   (`PENDIENTE_REVISION_JURIDICA`), declarados como **pendientes**, no como resueltos.
10. **Registro de cambios del propio aviso** (fecha + commit).

## 5. Lo que el aviso **no** puede pretender

- Incluir el texto de la LGPL **no** cumple la LGPL: falta la fuente correspondiente y el mecanismo de
  relinking/reemplazo en un onefile (PR-LIC-02). El aviso debe decirlo.
- Incluir el texto de MPL-2.0 **no** cumple MPL si hay archivos cubiertos modificados sin fuente
  disponible (certifi, y eventualmente grpcio/orjson).
- Un aviso que declare "todas las licencias verificadas" sería falso hoy: 15 filas están en
  `LEGAL_REVIEW_REQUIRED` y 5 en `EVIDENCE_MISSING`.
- La declaración del propietario sobre el dragón no convierte la imagen en "propia y libre": sigue
  siendo `OWNER_ATTESTED`.

## 6. Requisitos de metadatos del proyecto (hueco detectado)

`pyproject.toml` **no declara licencia** (ni `license`, ni `license-files`, ni classifiers de
licencia). Consecuencias verificables:

- `importlib.metadata` del paquete `sky-claw` informa licencia vacía → cualquier consumidor que
  intente leerla obtiene `None`.
- El SBOM del proyecto (cuando se genere sobre el wheel) declararía `NOASSERTION` para el propio
  Sky-Claw, que es correcto pero evitable.
- El ejecutable sí declara `LegalCopyright` MIT en `VS_VERSION_INFO` (vía `sky_claw.spec`), es decir:
  el binario dice más que el paquete.

Propuesta mínima (PEP 639, sin cambiar de licencia): `license = "MIT"` y `license-files = ["LICENSE"]`
en `[project]`. Va al write-set del documento 09 como cambio **de metadatos**, no de régimen legal.

## 7. Criterio de aceptación del paquete de avisos (para PR-LIC-04)

1. Toda fila de la matriz con `attribution_required = SI` tiene entrada en `COMPONENTS.json` **y** su
   texto accesible en `texts/` o `component_texts/`.
2. Ninguna entrada del aviso cita un componente que no esté en la matriz (sin fantasmas).
3. El texto de cada componente agregado se reproduce **completo**.
4. `--licenses` imprime exactamente lo mismo que el `.md` (misma fuente de verdad).
5. Los avisos viajan dentro del EXE **y** (si el propietario lo aprueba) junto a él.
6. El aviso declara los pendientes reales; **no** declara cumplimiento global.
