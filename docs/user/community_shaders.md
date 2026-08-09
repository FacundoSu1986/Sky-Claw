# Community Shaders

> **Audiencia:** usuarios y operadores de una instalación local.
>
> **Estado:** Parcial. La capacidad está implementada y anclada por tests, pero no tiene
> smoke sobre un rig real.
>
> **Fuentes canónicas:** `sky_claw/local/tools_installer.py`
> (`ensure_community_shaders`), `sky_claw/app/agent/tools/external_tools.py`,
> `sky_claw/app/gui/controllers/ritual_runner.py`,
> `sky_claw/app/gui/views/forge_dashboard.py`,
> `sky_claw/local/discovery/scanner.py` y `sky_claw/local/local_config.py`.
>
> **Última verificación:** 2026-08-09 sobre `origin/main` `67eaeec5` más este cambio.

## Objetivo

Qué instala Sky-Claw cuando se le pide Community Shaders, qué comprueba antes de
descargar, desde dónde se lanza y cómo interpretar cada desenlace.

Queda fuera: la configuración de los shaders dentro del juego, el rendimiento resultante
y cualquier comparación con ENB. Sky-Claw prepara el entorno; no ajusta ni valida el
resultado visual.

## Qué es y qué no es

Community Shaders es una **capacidad opcional de preparación del entorno**, no una etapa
del pipeline de modding. No aparece en el DAG del
[SOP técnico](../pipeline/skyrim_sop.md), no tiene una tool de orquestación que lo
ejecute y no altera el orden canónico de herramientas.

Es **opt-in**: no se instala en un flujo por defecto. Hay que pedirlo explícitamente.

## Qué instala

Tres mods de Mod Organizer 2, cada uno extraído a `mods/<Nombre>/` con su `meta.ini`. El
orden es de dependencia, que además es el orden más barato para abortar:

| Orden | Mod | Origen | Por qué |
|---|---|---|---|
| 1 | `Address Library for SKSE Plugins` | Nexus 32444 | Dependencia declarada por Community Shaders |
| 2 | `SSE Engine Fixes` | Nexus 17230 | Requisito duro de runtime: sin `EngineFixes.dll`, Community Shaders aborta al cargar |
| 3 | `Community Shaders` | Release de GitHub `community-shaders/skyrim-community-shaders` | El componente principal |

De SSE Engine Fixes se instala **el plugin SKSE**, nunca el preloader: el preloader se
extrae a la raíz del juego y en `mods/` quedaría inerte bajo el VFS de MO2. El archivo
que publica Nexus es un FOMOD con ramas SE y AE; Sky-Claw resuelve la rama de tu edición
y descarta el resto.

## Qué NO hace

- **No activa los mods.** No escribe `modlist.txt`. Sky-Claw deja constancia de los
  nombres instalados, pero hoy ninguna ruta de producción los consume: **activar los
  tres mods en MO2 es un paso manual tuyo**.
- **No instala SKSE.** Community Shaders es un plugin de SKSE: SKSE tiene que estar antes.
  El panel de la GUI tiene su propio instalador de SKSE.
- **No instala el preloader de SSE Engine Fixes.** Va en la raíz del juego y es un paso
  manual (o de Root Builder de MO2).
- **No desinstala ENB** ni resuelve la incompatibilidad por vos.

## Requisitos previos

Todos se comprueban antes de pedir la primera aprobación y antes del primer byte de
descarga. Si alguno falla, la operación aborta sin descargar nada:

1. **Edición del juego:** Special Edition o Anniversary Edition. Legendary Edition y una
   edición no reconocida abortan.
2. **API key de Nexus configurada.** Address Library y SSE Engine Fixes solo existen en
   Nexus; sin descargador la operación no arranca.
3. **Versión del juego.** En Special Edition, exactamente `1.5.97`. En Anniversary
   Edition, `1.6.1170` o superior — `1.6.640` y otras AE intermedias no están soportadas.
   Una versión vacía o ilegible aborta: no se aprueba lo que no se puede leer.
4. **SKSE instalado** en la raíz del juego (`skse64_loader.exe` o `skse_loader.exe`).
5. **ENB ausente.** Se detecta por `enbseries.ini` o el directorio `enbseries/` en la raíz
   del juego. Community Shaders se auto-deshabilita cuando ENB está presente, así que
   instalarlo sería un no-op silencioso.
6. **Preloader de SSE Engine Fixes presente** en la raíz del juego (`d3dx9_42.dll`). Sin
   él, Engine Fixes no carga y Community Shaders aborta su comprobación de runtime.
7. **Directorio `mods/` válido** según el validador de rutas.

## Desde dónde se lanza

Las dos superficies llaman al mismo mecanismo y pasan por la misma aprobación de descarga.

### Interfaz gráfica

Panel → tarjeta **«Renderizado Moderno»**. Los estados visibles y el flujo de aprobación
están en [interfaz gráfica](gui.md).

### Agente LLM

La tool `setup_tools` acepta `community_shaders`, pero **no** está en su lista por
defecto (`loot`, `xedit`, `pandora`, `bodyslide`, `ngio`): hay que nombrarlo. El agente
necesita `mo2_root` y `skyrim_path` en la configuración para detectar edición y versión.

## Aprobación humana

Cada componente pide su propia aprobación de descarga antes de bajar nada, con el nombre
del archivo, su tamaño y el origen. Esa aprobación **no** se autoaprueba con «Modo local»:
es egress de red y la decide una persona. Consultar el [flujo seguro](safe_workflows.md)
antes de aprobar sobre un perfil real.

Una denegación a mitad de la secuencia aborta los componentes restantes y **conserva** lo
que ya se instaló. No hay rollback de los componentes anteriores.

## Estado persistido

Los nombres de los tres mods se guardan en `community_shaders_mods`, estado administrado
por Sky-Claw que no se edita a mano. Es un **registro de lo instalado**, no un disparador
de activación: su semántica exacta y sus límites están en
[configuración](configuration.md).

## Detección

Sky-Claw considera Community Shaders instalado cuando existen **ambos** bajo
`mods/Community Shaders/`:

- `SKSE/Plugins/CommunityShaders.dll`
- el directorio `Shaders/`

Se comprueba el archivo físico en `mods/`, no el VFS de MO2, que el escaneo no ve. Un mod
con el DLL pero sin `Shaders/` no cuenta como instalado y la GUI conserva la acción de
reparación.

## Desenlaces

| Desenlace | Qué significa | Qué hacer |
|---|---|---|
| Instalación completa | Los tres mods en `mods/` y los nombres registrados | Activarlos manualmente en MO2 |
| Componente ya instalado | El sentinel de ese mod ya estaba: no se vuelve a descargar | Nada; la operación es idempotente por componente |
| Abortado por un requisito | Ningún byte descargado, nada modificado | Resolver el requisito y repetir; ver [diagnóstico](troubleshooting.md) |
| Denegado a mitad | Lo anterior queda instalado; el resto no se intentó | Repetir cuando se pueda aprobar; los componentes ya instalados se saltan |
| Instalado con aviso de persistencia | Los mods están en disco pero `community_shaders_mods` no se pudo guardar | Los mods son utilizables: activarlos igual en MO2. Para regenerar el registro, repetir la instalación: es idempotente y no vuelve a descargar. No borrar nada |

Las **dos** superficies avisan. La GUI lo muestra como notificación de advertencia; el
agente LLM devuelve la operación como fallida, con el detalle de lo que sí quedó en disco,
y nombra que el guardado no se aplicó. Ninguna informa éxito total cuando no hay dónde
escribir. En todos los casos los mods ya están en disco y se pueden activar a mano.

## Integridad de las descargas

- Se verifica el hash cuando el origen lo publica; un desajuste aborta y borra el archivo
  descargado.
- En la descarga por streaming (GitHub y Nexus con cuenta premium), si el origen no
  publica digest la descarga se acepta y el hash completo se registra en el log. Es
  confianza en el primer uso, no verificación.
- En el **fallback manual de Nexus**, si Nexus no publica ningún hash utilizable el
  archivo se acepta **sin validar y sin registrar hash**: sólo queda un aviso en el log
  de que no se pudo validar. Ese camino no ofrece ninguna evidencia de integridad.
- Hay un techo de bytes durante el streaming. Excederlo aborta y **no** se reintenta: un
  techo superado no se arregla repitiendo.
- Si el directorio del mod no existía, Sky-Claw extrae y verifica en un staging propio y
  sólo lo publica al terminar. Un fallo limpia ese payload temporal, nunca un destino que
  MO2 o el operador haya creado durante la operación.
- Si el directorio **ya existía**, la reparación escribe directamente sobre él. Sky-Claw
  nunca lo borra, pero un fallo de extracción o verificación puede dejarlo con contenido
  a medias: revisar ese directorio antes de reintentar. Una denegación no escribe nada.

## Fallos y recuperación

Los síntomas concretos, su evidencia y la acción reversible correspondiente están en
[diagnóstico y resolución](troubleshooting.md). Ante un estado ambiguo, preservar la
evidencia antes de repetir cualquier operación.

## Límites conocidos

- **La activación no está automatizada.** Sky-Claw registra los nombres instalados, pero
  ninguna ruta de producción los consume todavía: hay que activar los tres mods en MO2 a
  mano.
- **La GUI no puede reparar el registro.** Una vez que el mod se detecta, su tarjeta pasa
  a `Instalado` y deja de ofrecer la instalación, así que recuperar
  `community_shaders_mods` sólo es posible por el agente.
- **Sin validación en un rig real.** Lo documentado proviene de código y tests con la red
  simulada; ninguna prueba descarga de Nexus o GitHub reales.
- **Descarga desde Nexus sin cuenta premium:** la API sólo genera enlaces directos para
  cuentas premium, así que esa vía cae al fallback manual, donde un file sin hash
  utilizable se acepta sin validar. El recorrido completo no se comprobó de extremo a
  extremo.
- **Sin hashes fijados.** La verificación depende de lo que publique cada origen.
- El preloader de Engine Fixes se identifica por un nombre de archivo conocido. Si el
  proyecto original lo renombrara, la comprobación fallaría hasta actualizar el código.

## Referencias

- `ensure_community_shaders` en `sky_claw/local/tools_installer.py`.
- Tests ancla: `tests/test_tools_installer_community_shaders.py`,
  `tests/test_ritual_install.py`, `tests/test_local_config_persistencia.py`,
  `tests/test_setup_tools_persistencia.py`.
- [Interfaz gráfica](gui.md) · [Configuración](configuration.md) ·
  [Diagnóstico](troubleshooting.md) · [Flujo seguro](safe_workflows.md)
