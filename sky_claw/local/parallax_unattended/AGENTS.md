# parallax_unattended — contrato de esta capa

## Qué es

La capa que decide si una corrida de parallax **sirve**, para que la tubería pueda correr sin
operador. Consume dos artefactos:

- `informe_parallax.json` (lo escribe el generador; acá es **entrada de sólo lectura**)
- los `_p.dds` de la raíz de salida (se releen desde disco)

## Reglas duras

1. **No importar `sky_claw.local.native_parallax`.** Ni sus dataclasses, ni sus constantes, ni
   su decodificador. El generador es otro dueño y puede cambiar por dentro; si esta capa
   dependiera de sus objetos, un refactor suyo redefiniría en silencio qué significa "listo".
   El contrato es el JSON, con su `version_del_lote`.
2. **No escribir nada.** Ni archivos, ni logs, ni caché. Sólo lectura y un valor de retorno.
3. **No agregar dependencias.** Nada de numpy ni Pillow: la decodificación BC4 de `auditor.py`
   es Python puro a propósito, para que el chequeo corra donde corre el runner y no donde hay
   un entorno de investigación.
4. **Fail-closed.** Un estado que no se entiende no es "probablemente bien": es
   `INCONSISTENTE`. Un formato que no se puede medir no es "verificado": va a `no_verificados`.
5. **`PASS` es el único éxito.** `REVIEW` devuelve un código distinto de 0 y distinto de
   `FAIL`, para que quien llama no tenga que parsear texto.

## Por qué existe (no borrar este párrafo)

El generador tiene dos ejes por archivo: `estado` (dónde terminó) y `veredicto` (si sirve).
`conteos` agrega el primero y el código de salida del generador se deriva de `completo`, que
también mira el primero. Medido en el rig real: con `--incluir-revision`, ocho texturas con
`veredicto: REVIEW` quedaron `estado: ESCRITA`, `conteos.EN_REVISION` dio 0 y el proceso salió
0. Esta capa contesta la pregunta que faltaba.

## Cómo extender

- Un **estado** nuevo del generador: agregarlo a `ESTADOS_CONOCIDOS` **y** darle su rama en
  `dictar`. El test de enumeración falla hasta que se haga, a propósito.
- Un **efecto** nuevo del escritor: si deja archivo, agregarlo a `EFECTOS_QUE_DEJAN_ARCHIVO`.
  Si no deja, no.
- Un **motivo** nuevo: decidir a qué conjunto pertenece (`MOTIVOS_DE_INCONSISTENCIA` o
  `MOTIVOS_DE_FALLA`) o si es de revisión, y escribir su caso.

## Lo que esta capa NO hace

No calibra umbrales de calidad (eso es `gates.py`, del generador), no arregla el shader, no
decide sobre PGPatcher y no publica el mod. Si algo de eso falta, el veredicto correcto es
`REVIEW`, no un `PASS` optimista.
