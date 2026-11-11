# Alta de un build nuevo de TexGen/DynDOLOD en el contrato de UI

> **Audiencia:** operador con el rig Windows y la herramienta instalada; quien revisa el PR del alta.
>
> **Estado:** procedimiento **preparado y probado solo con una GUI simulada**. Nunca se corrió contra la GUI real de
> un build: ese es el primer paso del rig (ver «Lo que no está medido»).
>
> **Fuentes canónicas:** `scripts/dyndolod_medir_contrato_uia.py` (la herramienta),
> `sky_claw/local/tools/dyndolod_uia_contrato.py` (`CONTRATOS_DE_UI`, el contrato congelado) y
> `sky_claw/local/tools/dyndolod_uia_preflight.py` (`ObservadorUIA`, el puerto de lectura).

## Cuándo hace falta

El modo desatendido de la etapa 9 solo opera builds **medidos en un rig real**. Hoy hay dos contratos congelados, ambos
**3.0.0.209, modo SSE**:

| Herramienta | `sha256` del ejecutable |
|---|---|
| TexGen | `0939bc8f8cbae2e1b38f17d56fecd1b7a941dade0554e944886bd7f5c4807a62` |
| DynDOLOD | `b67625eb7815111ba9ac232c626ff88b07bb64ff30ff5183c5a04b5f045c3bd0` |

Cualquier otro build (una actualización de la herramienta, un rig VR, una compilación distinta) corta con
`UNATTENDED_BUILD_UNSUPPORTED`. Eso es **fail-closed a propósito**: operar una GUI cuyos controles no se midieron es
justo lo que el contrato existe para impedir.

## Qué hace la herramienta y qué NO

**Mide y compara. No autoriza nada.** Es de **solo lectura**: observa por el puerto `ObservadorUIA` (tres métodos, los
tres de lectura), nunca importa al driver mutante, no enfoca ventanas, no pulsa botones ni escribe en los controles. Lo
único que escribe es el informe que vos pedís con `--salida`, y se rechaza si cae dentro del directorio de la
herramienta. Un test por AST lo congela (`tests/test_dyndolod_uia_driver_superficie.py`).

Produce dos cosas separadas:

1. una **medición** (`medir`): hechos de la UI de UN proceso en UN momento, más el SHA-256 del ejecutable **calculado del
   archivo** (no declarado por vos);
2. un **diagnóstico** (`diagnosticar`): por cada pieza del contrato congelado, si en el build medido `coincide`,
   `difiere` o está `no_observado`.

**No emite un contrato listo para pegar.** Hay campos que ningún dato medido define: el mecanismo de escritura del
Output, la política de cada modal (¿quién aprueba continuar?) y la evidencia. Los decide una persona, con el informe del
rig, en el PR del alta.

## Requisitos

- Windows, en el rig, con la herramienta **abierta desde el ejecutable exacto** que se quiere dar de alta.
- **Una sola instancia** de ese ejecutable. Con dos, la medición se niega: no sabe cuál medir.
- Fuera de Windows (o sin UI Automation) `medir` falla cerrado con el motivo. `diagnosticar` **no** necesita UIA: corre
  sobre los informes ya guardados, en cualquier máquina.

## Procedimiento

La UI de estas herramientas cambia de ventana durante una corrida (ventana principal, aviso de arranque, diálogo
terminal), así que se toma **una instantánea por momento** y después se las diagnostica juntas.

**1. Reposo.** Con la herramienta recién abierta, sin tocar nada:

```bash
python -m scripts.dyndolod_medir_contrato_uia medir --tool DynDOLOD --exe "D:\Tools\DynDOLOD\DynDOLODx64.exe" --modo SSE --etiqueta reposo --salida "C:\informes\dyndolod_reposo.json"
```

**2. Cada aviso de arranque conocido.** Si la herramienta muestra un aviso al arrancar (por ejemplo, el de
`DynDOLOD.DLL` no encontrada), dejalo abierto **sin pulsar nada** y medí con otra etiqueta:

```bash
python -m scripts.dyndolod_medir_contrato_uia medir --tool DynDOLOD --exe "D:\Tools\DynDOLOD\DynDOLODx64.exe" --modo SSE --etiqueta modal-de-arranque --salida "C:\informes\dyndolod_modal.json"
```

**3. Diálogo terminal.** Corré la herramienta a mano hasta que muestre el diálogo final («Save and Exit»…), dejalo abierto y
medí:

```bash
python -m scripts.dyndolod_medir_contrato_uia medir --tool DynDOLOD --exe "D:\Tools\DynDOLOD\DynDOLODx64.exe" --modo SSE --etiqueta dialogo-terminal --salida "C:\informes\dyndolod_terminal.json"
```

**4. Diagnóstico.** Con los informes juntos y el `sha256` del build **congelado** con el que se compara (tabla de arriba):

```bash
python -m scripts.dyndolod_medir_contrato_uia diagnosticar --contra-sha b67625eb7815111ba9ac232c626ff88b07bb64ff30ff5183c5a04b5f045c3bd0 --exe "D:\Tools\DynDOLOD\DynDOLODx64.exe" "C:\informes\dyndolod_reposo.json" "C:\informes\dyndolod_modal.json" "C:\informes\dyndolod_terminal.json"
```

`--exe` es el ejecutable del build medido: su directorio se usa para comprobar que existe el preset que el contrato espera.
Con `--exigir-compatible` el comando sale con código 3 si el veredicto no es `compatible` (útil en un script).

## Cómo leer el veredicto

| Veredicto | Qué significa | Qué hacer |
|---|---|---|
| `compatible` | **Todas** las piezas del contrato coinciden y **todas** se observaron | El contrato congelado describe este build. Falta igual la prueba real del rig (R0) |
| `difiere` | Al menos una pieza no coincide (selector que ya no resuelve o resuelve a más de un control, ventana principal con otra clase, preset ausente) | El detalle dice cuál. Es un build nuevo con UI distinta: hace falta medirlo entero |
| `incompleto` | Nada difiere, pero falta observar algo (típicamente un diálogo) | Tomá la instantánea que falta. **Nunca** se interpreta como «sí» |

Un diálogo que no coincide queda `no_observado` y no `difiere`: desde afuera no se distingue «el diálogo cambió» de «no se
capturó ese momento». El detalle lista las ventanas que sí se vieron.

## Cómo se da de alta de verdad

El diagnóstico es la evidencia, no el alta. El alta es un PR que:

1. agrega el contrato nuevo a `CONTRATOS_DE_UI` (`sky_claw/local/tools/dyndolod_uia_contrato.py`) con los campos que el
   informe define **y los que decide una persona**: `mecanismo_output`, la `politica` de cada modal y la `evidencia`;
2. deja el informe del rig (los JSON de arriba y la corrida real) en `docs/validation/<fecha>_<build>/`;
3. actualiza el congelamiento por igualdad literal de `tests/test_dyndolod_uia_contrato.py`: un build sin test no entra;
4. se prueba en un rig real con la corrida desatendida completa (la re-certificación R0 del plan de la etapa 9).

## Lo que no está medido

- **Que la GUI real de Alpha-209 produzca el mismo informe** que la GUI simulada derivada del contrato. Es el primer
  paso del rig: correr los pasos 1 a 4 sobre el build congelado y esperar `compatible`. Si no da `compatible`, la
  herramienta o el contrato tienen un defecto y hay que entenderlo antes de usarla con un build nuevo.
- **El criterio de «instrucción de un diálogo»**: el script la busca como el nombre de algún control o como el título de la
  ventana. Es un criterio de la herramienta, no algo medido en Windows real.
- Un `AutomationId` puede cambiar entre lanzamientos (el rig T5A lo midió inestable para el campo Output): el informe los
  registra, pero el contrato de Output se apoya en la clase, no en el id.
