# Interfaz gráfica

> **Audiencia:** usuarios de la aplicación NiceGUI.
>
> **Estado:** Implementado con algunas capacidades dependientes del entorno.
>
> **Fuentes canónicas:** `sky_claw/app/gui/sky_claw_gui.py`,
> `sky_claw/app/gui/views/forge_dashboard.py` y
> `sky_claw/app/gui/controllers/ritual_runner.py`.
>
> **Última verificación:** 2026-08-08 sobre `fix/dyndolod-cli-contrato-asistida`
> `e8427063`.

## Arranque

```powershell
.\.venv\Scripts\python.exe -m sky_claw --mode gui
```

El binario congelado selecciona GUI por defecto. El bootloader escanea el
entorno, inicia servicios y publica el resultado en el store reactivo.

## Secciones

| Sección | Propósito |
|---|---|
| Panel | Estado, telemetría, chat y rituales disponibles |
| Mods | Inventario y selección |
| Conflictos | Conflictos detectados y resoluciones |
| Descargas | Historial, acciones y aprobaciones relacionadas |
| Ajustes | Configuración visible de la aplicación |

La disponibilidad de un ritual deriva del escaneo real; no se debe interpretar
una tarjeta visible como prueba de que el ejecutable o el perfil sean válidos.

## Flujo de ritual

1. Seleccionar una acción.
2. Revisar el preflight y la tool detectada.
3. Examinar el preview cuando exista.
4. Si la ruta incluye un gate HITL, aprobar o denegar la solicitud. Los cinco
   rituales cableados al dispatcher —LOOT, xEdit, Wrye Bash, Pandora y
   DynDOLOD— pasan por ese gate; si el gate no está disponible, fallan
   cerrados. El Panel expone además tarjetas que sólo instalan y no ejecutan.
5. Esperar el resultado normalizado y revisar la evidencia.

`ritual_runner.py` consume `normalize_tool_result()` para presentar una vista
común. El resultado visual no sustituye la revisión del journal, logs o
artefactos cuando la operación cambia archivos.

La ruta LLM general es `lock-only` y no muestra este gate por herencia; sólo los
handlers que inyectan `HITLGuard`, como ciertas descargas e instalaciones,
solicitan aprobación propia.

## Estados de una tarjeta

Una tarjeta deriva su estado del último escaneo, y el botón que ofrece deriva del
cableado real: nunca promete una acción que no tenga a dónde ir.

| Estado | Etiqueta | Qué hace el botón |
|---|---|---|
| Sin escaneo todavía | `Verificando…` | Nada: sólo un aviso interino |
| Detectado y ejecutable | `Disponible` | `Ejecutar` despacha el ritual |
| Detectado, sin ejecución propia | `Disponible` | `Instalado` es un indicador; no despacha nada |
| No detectado, con auto-instalador | `No instalado` | `Instalar` descarga e instala |
| No detectado, sin auto-instalador | `No instalado` | Nada: aviso interino, sin acción cableada |

`Instalado` es el caso de un componente que carga el juego, no Sky-Claw: no hay nada que
ejecutar. No debe leerse como un fallo.

## Instalación desde una tarjeta

Una tarjeta `No instalado` con auto-instalador descarga a través del instalador de
herramientas. La aprobación de descarga abre el modal de la pestaña que apretó
`Instalar` y no se autoaprueba con «Modo local». Una instalación y un ritual nunca se
solapan: comparten el mismo cerrojo.

La tarjeta **«Renderizado Moderno»** instala Community Shaders y sus dependencias. Sus
requisitos previos se comprueban antes de descargar, y un resultado con aviso significa
que los mods quedaron en disco pero no se pudo guardar la configuración que el
orquestador necesita para activarlos. El detalle completo está en
[Community Shaders](community_shaders.md).

## Seguridad operativa

No activar autoaprobación en un perfil de uso real sin haber completado la
[validación del rig](../operations/real_rig_validation.md). Ante un estado
ambiguo, detener nuevas ejecuciones y seguir
[troubleshooting](troubleshooting.md).
