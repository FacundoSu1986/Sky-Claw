# Evidencia primaria del rig standalone (sanitizada)

Copias **redactadas** de los artefactos que el informe cita como fuente primaria del
standalone: log del harness (`run-log.txt`), resultados JSON de cada intento
(`attempt1-*`, `attempt2-*`), comandos efectivos, presets antes/después, preflight,
journal y los pedidos/decisiones HITL. Se publican para que un revisor pueda auditar
las acciones exactly-once, distinguir el intento aceptado de los reintentos y verificar
los cortes fail-closed, sin depender de la máquina del operador.

**No se publican** los logs crudos completos de TexGen/DynDOLOD (multi-MB y sin valor
probatorio adicional): sus extractos están en `../texgen-log-excerpt.txt` y
`../dyndolod-log-excerpt.txt`. El rig original se conserva para revisión offline.

Sanitización aplicada: mismas reglas que el resto del paquete (identidad de cuenta,
host y rutas de usuario redactadas; UTF-8 sin BOM; LF). No hay secretos en estos
archivos: los pedidos HITL no contienen tokens.
