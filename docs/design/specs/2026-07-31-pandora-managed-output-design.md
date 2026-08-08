# Salida administrada y reversible de Pandora

## Contexto

Pandora se ejecuta actualmente como proceso directo con `cwd` en la raíz del
juego y sin argumento de salida. El código debe tratar por ello tres destinos
posibles: `Data`, `game/Pandora_Output` y
`pandora_exe.parent/Pandora_Output`. Solo los dos directorios dedicados pueden
protegerse con `DirectoryRollback`; mover o restaurar `Data` completo sería
destructivo.

La documentación oficial vigente de Pandora declara `--output`/`-o` para fijar
una ruta absoluta y recomienda esa opción en MO2. Este cambio usará esa palanca
para eliminar la selección implícita de destino antes de abordar la migración
independiente al broker USVFS.

## Decisión

`PandoraRunner` derivará internamente una única salida:

```text
<game_path resuelto>/Pandora_Output
```

Cada ejecución incluirá exactamente una vez:

```text
--output <ruta absoluta administrada>
```

La ruta no será configurable por el usuario, el LLM ni los callers. Exponerla
convertiría una entrada externa en el directorio que `DirectoryRollback` mueve
y restaura. Los callers existentes heredan el comportamiento mediante el mismo
`PandoraRunner`, sin cambios en el JSON público de tools.

## Flujo y propiedad

1. El runner resuelve `game_path` y calcula el `Pandora_Output` canónico.
2. El servicio ejecuta preflight sobre esa salida o, si todavía no existe,
   sobre su padre escribible.
3. El `ActionManifest` enumera la salida exacta, no `Data` ni la raíz del juego.
4. `DirectoryRollback` protege únicamente esa salida durante la ejecución.
5. Éxito conserva el árbol regenerado. Error, timeout y cancelación restauran
   el estado anterior o eliminan el parcial del primer run.
6. El reconciliador de arranque busca residuos de Pandora bajo la raíz que
   contiene la salida administrada.

GUI y agente LLM ya convergen en `PandoraPipelineService`; no se crearán dos
mecanismos. El camino del agente debe fallar cerrado cuando no reciba
`lock_manager` y `snapshot_manager`, porque ejecutar directamente omitiría el
rollback que este contrato promete.

## Manejo de errores

- Si no pueden resolverse ejecutable o juego, Pandora no se inicia.
- Si el preflight no puede demostrar escritura en la salida o su padre, Pandora
  no se inicia.
- Si preparar el move-aside falla, Pandora no se inicia.
- Un resultado non-zero se convierte en excepción dentro del contexto para
  activar el restore existente.
- Timeout y cancelación conservan sus excepciones actuales después del rollback.
- Un restore fallido deja la transacción pendiente para recuperación manual; no
  se afirmará `rolled_back` falsamente.

No se agregará fallback a `Data`, al directorio del ejecutable ni a una ruta
proporcionada externamente.

## Alternativas descartadas

### Sandbox MO2 lanzable antes de fijar la salida

Es reutilizable para T-27, pero exige perfil y mod temporales, manifiesto firmado
ampliado, cleanup tras `worker_exit` y reconciliación de residuos. No hace falta
asumir ese riesgo para eliminar primero la salida ambigua.

### Migración completa de Pandora al broker en este PR

Mezclaría worker, perfil descartable, canary, ambos entry points y promoción
HITL. Esa superficie impide atribuir con claridad una regresión y contradice la
regla de un runner y un mecanismo revisable por PR.

### Ruta de salida configurable

Permitiría que configuración o input del agente apunten el rollback a un árbol
compartido. La salida fija elimina esa clase de error por construcción.

## Pruebas

El cambio seguirá TDD. Las regresiones deben fallar antes de editar producción
y cubrir:

- argv con un solo `--output`, ruta absoluta y espacios preservados;
- igualdad entre la ruta del runner, preflight, manifiesto y rollback;
- ausencia de `Data` y `pandora_exe.parent` entre los targets productivos;
- éxito, non-zero, timeout, cancelación y primer run sin salida previa;
- restauración byte-idéntica y transacción pendiente si falla el restore;
- camino GUI y camino LLM usando el mismo servicio;
- camino LLM sin managers que falla antes de invocar el runner;
- reconciliación limitada a la raíz administrada;
- ancla documental: U-04 continúa parcial por BodySlide y T-27 continúa
  parcial por la vista USVFS pendiente.

## Estado y evidencia

Este PR podrá afirmar:

- verificado ejecutando tests: Sky-Claw siempre construye el comando con salida
  absoluta y todos los mecanismos internos usan esa misma ruta;
- verificado leyendo la fuente oficial: Pandora documenta `--output`/`-o`;
- no verificado en runtime: que Pandora 4.3.1-beta respete la ruta en el rig real
  bajo éxito, error, timeout y cancelación.

`docs/pending_ooda_status.md` mantendrá U-04 y T-27 como parciales. El modo
implícito `Data` dejará de ser deuda de código, pero pasará a la matriz de smoke
real; BodySlide seguirá abierto. No se declarará GA ni se ejecutará T-25.

## Fuera de alcance

- agregar `pandora` al allowlist del worker VFS;
- crear perfiles o mods temporales de MO2;
- implementar canary USVFS o promoción post-diff;
- cambiar argumentos de auto-run existentes distintos de `--output`;
- ejecutar Pandora, MO2 o Skyrim reales;
- modificar contratos públicos `success`/`message`.
