# parallax_unattended — contrato de esta capa

## Qué es

La capa que decide si una corrida de parallax **sirve**, para que la tubería pueda correr sin
operador. Consume dos artefactos:

- `informe_parallax.json` (lo escribe el generador; acá es **entrada de sólo lectura**)
- los `_p.dds` de la raíz de salida (se releen desde disco)

## Reglas duras

1. **Clean-room ([CLEAN_ROOM.md](../../../CLEAN_ROOM.md), [ADR 0013](../../../docs/adr/0013-clean-room-native-parallax.md)).**
   Esta capa forma parte de la zona limpia del pipeline nativo de parallax: rigen las mismas
   prohibiciones de lectura de secciones en cuarentena, scripts o binarios de herramientas
   cerradas.
2. **No importar `sky_claw.local.native_parallax`.** Ni sus dataclasses, ni sus constantes, ni
   su decodificador. El generador es otro dueño y puede cambiar por dentro; si esta capa
   dependiera de sus objetos, un refactor suyo redefiniría en silencio qué significa "listo".
   El contrato es el JSON, con su `version_del_lote` ([ADR 0014](../../../docs/adr/0014-native-parallax-producto-brief-y-dependencias.md)).
   Lo que el auditor y el generador deben decidir IGUAL pese a no compartir código (hoy: qué cabecera DDS es una textura 2-D simple) se ancla con un
   test que compara las dos copias sobre toda la matriz de cabeceras: `tests/test_dds_2d_simple_paridad.py`.
3. **No escribir nada.** Ni archivos, ni logs, ni caché. Sólo lectura y un valor de retorno.
4. **No agregar dependencias.** Nada de numpy ni Pillow: la decodificación BC4 de `auditor.py`
   es Python puro a propósito, para que el chequeo corra donde corre el runner y no donde hay
   un entorno de investigación.
5. **Fail-closed.** Un estado que no se entiende no es "probablemente bien": es
   `INCONSISTENTE`. Un formato que no se puede medir no es "verificado": va a `no_verificados`.
6. **`PASS` es el único éxito.** `REVIEW` devuelve un código distinto de 0 y distinto de
   `FAIL`, para que quien llama no tenga que parsear texto.

## Por qué existe (no borrar este párrafo)

El generador tiene dos ejes por archivo: `estado` (dónde terminó) y `veredicto` (si sirve).
`conteos` agrega el primero y el código de salida del generador se deriva de `completo`, que
también mira el primero. Medido en el rig real: con `--incluir-revision`, ocho texturas con
`veredicto: REVIEW` quedaron `estado: ESCRITA`, `conteos.EN_REVISION` dio 0 y el proceso salió
0. Esta capa contesta la pregunta que faltaba.

## Lo que el informe tiene que traer (y qué pasa si falta)

- `omitidas_al_enumerar` (lista de textos): lo que la enumeración dejó afuera. **Obligatorio**:
  sin el censo no se puede probar que no se omitió nada, así que su ausencia o una forma
  inválida es `INCONSISTENTE`, y un censo no vacío es `REVIEW` (`RUTAS_OMITIDAS`). Lo escribe
  `native_parallax/cli.py`; no se subió `VERSION_DEL_LOTE` porque entra a la procedencia e
  invalidaría todas las cachés.
- `interrupcion` (texto no vacío o `null`): por qué se cortó la escritura del lote (candado perdido, disco). **Obligatorio**, como el censo anterior: sin la clave no se
  prueba que no hubo corte. Un texto es `FAIL` (`LOTE_INTERRUMPIDO`) aunque ninguna textura figure en duda; el estado `SIN_CONFIRMAR` (no se sabe si quedó en el mod) es `FAIL`
  y solo es legal dentro de una interrupción declarada y sin `efecto`. Tampoco se subió `VERSION_DEL_LOTE` por esto.
- `otras_rutas` con efecto `RETIRADA`: el auditor exige **ausencia** en disco
  (`RETIRADA_PRESENTE` es falla dura). Una `_p.dds` obsoleta que el juego sigue cargando con el
  log en verde es exactamente el falso verde de esta capa.

## Lo que esta capa todavía no verifica (declarado, no resuelto)

- `PASS` es **inalcanzable** con el generador vigente: sin umbrales calibrados todo archivo sale
  `REVIEW`. No se "arregla" acá; se documenta.
- No hay punto de entrada de producción (nada importa esta capa ni hay `python -m`).
- La desviación estándar del auditor es global: un relieve que ocupa una fracción chica de la
  textura puede quedar enmascarado.
- `dictar` y `auditar` validan cada uno la matriz `(estado, efecto)`; el ancla
  `test_dictar_y_auditar_coinciden_en_la_matriz_estado_efecto` congela la matriz legal.

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
