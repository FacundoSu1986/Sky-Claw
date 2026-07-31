# Inventario del rig para GA — 2026-07-30

> **Estado:** Bloqueado antes de ejecutar mutadores.
>
> **Alcance:** prerequisitos de U-04, T-27 y T-25 para BodySlide y Pandora.
>
> **Código inspeccionado:** `origin/main` en
> `d0412151e79a2c03728f6a0f5ce1cd18076bae08`.
>
> **Verificación:** lectura del filesystem y metadatos PE en Windows; no se
> ejecutó BodySlide, Pandora, MO2 ni Sky-Claw.

## Resultado

El rig contiene Skyrim, MO2, USVFS y Pandora, pero no satisface las
precondiciones para una corrida atribuible y reversible:

- BodySlide no está instalado en el juego ni en los mods de MO2 inspeccionados.
- `pandora_exe`, `bodyslide_exe` y `skyrim_path` están vacíos en el TOML
  inspeccionado. `SKYRIM_PATH`, `PANDORA_EXE`, `BODYSLIDE_EXE` y `MO2_PATH`
  también estaban vacíos en los entornos de proceso, usuario y máquina.
- No se inició `AppContext`: su autodetección puede resolver y persistir rutas.
  Por eso este paquete describe el estado en reposo, no afirma que una ruta sea
  imposible de resolver durante startup.
- El bridge de MO2 referencia un ejecutable congelado de otro checkout. Ese
  binario existe, pero no demuestra correspondencia con el SHA inspeccionado.
- El único perfil encontrado es `Default`; no es un perfil descartable.
- El overwrite real ya contiene 17 archivos. Usarlo como destino de prueba
  impediría atribuir el diff exclusivamente a Pandora.
- MO2 y los runners no estaban activos durante el inventario.

Por estas razones no se ejecutó un canary USVFS ni una corrida directa. Ejecutar
Pandora directamente con `cwd` en el juego podría escribir en `Data`; ejecutar
BodySlide no es posible sin su binario. La ausencia de ejecución preserva el
estado real, pero no cierra U-04, T-27 ni T-25.

## Configuración sanitizada

El TOML observado no contenía rutas operativas para los dos runners:

```text
mo2_root = C:/Modding/MO2
install_dir = C:/Modding
pandora_exe = <vacío>
bodyslide_exe = <vacío>
skyrim_path = <vacío>
```

MO2 declara:

```text
perfil seleccionado = Default
juego = Skyrim Special Edition
ruta del juego = C:/steam/steamapps/common/Skyrim Special Edition
```

El bridge instalado declara un worker bajo:

```text
E:/SkyclawGemini/Sky-Claw-main/dist/SkyClaw.exe
```

No se registran tokens, credenciales ni el contenido de archivos de usuario.

## Versiones y hashes previos

Todos los hashes son SHA-256.

### MO2 y juego

```text
ModOrganizer.exe
  versión: 2.5.2
  bytes: 5028352
  sha256: 442B354A8F34754DA0048654C44D27F51628FEBA54CE46C3187CF58D6C43E622

usvfs_x64.dll
  versión: 0.5.6.1
  bytes: 1854976
  sha256: E2B766F418575021B9D350F384195CE6F23173169B37222CDEF3D7FE5495F8B5

SkyrimSE.exe
  versión: 1.6.1170.0
  bytes: 37157144
  sha256: C434208894F07F604B852F29B8EDC3A58C4DE63DE783373733E72B2B73F33BE9
```

### Pandora y Sky-Claw

```text
Pandora Behaviour Engine+.exe
  versión de producto: 4.3.1-beta
  commit de producto: d6344e394c8a9ecfd2966cc0d84bbbdf73976b19
  bytes: 359424
  sha256: 45609842928F64F1178C3D64F7B8082319346C2E1C03646D88A47A1AE5DE65F7

SkyClaw.exe instalado
  bytes: 58550528
  sha256: 0F941FBD5A6F8706D0EC58E5415D9E8F7DEC832DC26D79FC89E0BA1CD21C4783

SkyClaw.exe referenciado por el bridge
  bytes: 61619263
  sha256: 2D950725BC9CBD13020C3B4BD7C56D3213D7664A91E4F7EF17A624E369F3C727
```

Los dos ejecutables de Sky-Claw tienen hashes distintos. No se infiere cuál
corresponde al código actual.

### Perfil y bridge

```text
profiles/Default/modlist.txt
  líneas: 85
  bytes: 3205
  sha256: 5E6F949A8D442A9B2533695C167BE2828A9B38797C8B210FE134F30D19906F63

profiles/Default/plugins.txt
  líneas: 10
  bytes: 339
  sha256: D328331200854AC9266FF487BB29F4A600EF838B983D034857A674E4393ADC44

plugins/skyclaw_bridge/runtime.py
  bytes: 17337
  sha256: F63FDDEA33A1F27C70A338EBA2FB8F85F387954787C1C811FD7404684243D074

plugins/skyclaw_bridge/bridge_config.json
  bytes: 311
  sha256: 2DA12B8B9D9A3428927A81AF3441F9F039C57E86B246844F734E287F7CBE8A5A
```

## Matriz de escenarios

| Runner | Éxito | Error | Timeout | Cancelación | Proceso cortado |
|---|---|---|---|---|---|
| BodySlide | N/E | N/E | N/E | N/E | N/E |
| Pandora | N/E | N/E | N/E | N/E | N/E |

`N/E` significa `No ejecutado`. Tampoco se ejecutaron promoción, fallo de
promoción, pérdida de lease ni
rollback. No hay hashes posteriores porque no hubo corrida.

## Implicaciones observadas para el plan

Este paquete no crea una decisión arquitectónica. Registra qué ramas del plan
carecen de evidencia y deben resolverse en el PR productivo o ADR
correspondiente.

### BodySlide

No se demostró que `-o` admita una raíz de staging aislada ni que produzca un
manifiesto completo. Por eso este inventario no habilita:

1. staging propiedad de Sky-Claw con promoción de archivos enumerados;
2. snapshot y restore por archivo a partir de un manifiesto completo.

Permanece la tercera rama del plan: fallar cerrado para destinos compartidos.
U-04 sigue abierto; ese cambio productivo requiere su propio PR y tests.

### Pandora

La opción preferida continúa siendo MO2/USVFS con perfil descartable y canary.
El bridge y el perfil actuales no satisfacen esa precondición. No se dispone de
un manifiesto completo de archivos escritos en `Data`, por lo que un rollback
fino sería especulativo. `Pandora_Output` sigue siendo la única raíz que el
código actual considera revertible como directorio completo.

## Próximo punto de control

Antes de ejecutar:

1. Construir el SHA bajo prueba y reinstalar el bridge apuntando a ese artefacto.
2. Instalar y configurar BodySlide dentro del rig o declarar su matriz
   explícitamente no aplicable para la release.
3. Migrar Pandora y BodySlide al broker, con handler y targets declarados para
   cada uno. BodySlide debe ejecutarse en el worker USVFS o materializar todos
   los mods requeridos en staging dedicado. Su canary debe verificar esa vista
   de entrada y fallar cerrado si falta algún input; aislar solo la salida no
   aporta la vista USVFS de los mods.
4. Crear un perfil descartable. No vaciar ni mover el overwrite compartido:
   tomar su snapshot y probar que el runner escribe en
   `SandboxClone.overwrite_copy` o en otra salida aislada declarada.
5. Ejecutar `vfs-health` y conservar la attestation. Después ejecutar un canary
   por runner: el handler health no demuestra la vista VFS de Pandora/BodySlide.
6. Tomar un manifiesto completo de hashes de `Data`, perfil, overwrite y outputs.
7. Recién entonces ejecutar éxito, error, timeout, cancelación y proceso cortado.
8. Comparar hashes posteriores, verificar procesos huérfanos y registrar el
   resultado en un paquete fechado nuevo.

Los pasos 1-6 y el cierre de T-27/U-04 son precondiciones para ejecutar la
matriz. T-25 no puede cerrarse hasta completar y registrar los pasos 7-8.
