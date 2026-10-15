# Release

> **Audiencia:** maintainers y responsables de entrega.
>
> **Estado:** Releases publicadas hasta `v0.2.4`; los cambios del árbol actual
> permanecen en `[Unreleased]`.
>
> **Fuentes canónicas:** `.github/workflows/ci.yml`,
> `.github/workflows/release.yml`, `sky_claw.spec`, `build.bat`,
> `CHANGELOG.md` y `DEPLOYMENT.md`.
>
> **Última verificación:** 2026-07-26 sobre
> `codex/crash-logging-async-safe` `74bdb8f`.

## Qué existe

- Cinco gates en CI: Lint, Mypy, Tests, Security y Build. Ruff, mypy, Bandit y
  coverage incluyen `scripts/` además de sus rutas anteriores.
- Build PyInstaller y artifact de CI. Antes de subirlo, el job `Build` ejecuta
  `python scripts/smoke_frozen_executable.py dist/SkyClawApp.exe` con perfil,
  config, logs y temporales aislados; un timeout o código distinto de cero
  bloquea el artifact.
- Lockfiles `requirements.lock` y `uv.lock`.
- Runbook de deployment y checklist de rig real.
- La release `v0.2.4`, cuyo workflow terminó correctamente, publicó
  `SkyClawApp.exe`, el SBOM SPDX y `SkyClawApp.exe.bundle.json`.
- `.github/workflows/release.yml` ejecuta `Cosign sign-blob` keyless mediante
  OIDC y publica el bundle junto con el ejecutable y el SBOM.

## Cosign no equivale a Authenticode

El bundle Cosign aporta material para verificar la firma del blob descargado,
pero no establece una identidad de publisher de Windows. El workflow contiene
`Cosign sign-blob`, pero no incluye un paso de Authenticode ni `signtool`; esa
ausencia describe el pipeline actual, no la firma PE del artefacto histórico.

## Qué no debe afirmarse todavía

Esta actualización documental no verificó criptográficamente el bundle de
`v0.2.4`, no hizo cold boot de `SkyClawApp.exe` y no inspeccionó la firma PE del
artefacto histórico. Authenticode, identidad de publisher y comportamiento de
SmartScreen permanecen sin verificación independiente aquí. La existencia del
workflow, sus assets y una ejecución exitosa no sustituye esos smokes.

## Smoke automatizado del ejecutable (actualización parcial 2026-10-14)

El job `Build` de CI ejecuta el `SkyClawApp.exe` recién generado con `--help`
antes de subirlo como artifact. El smoke usa un perfil temporal; descarta la
configuración Sky-Claw, las rutas Python, `DOCKER_AUTH_CONFIG`, `SSH_AUTH_SOCK`
y las variables de entorno con nombres comunes de secretos. Impone un timeout
de 60 segundos, termina el árbol del proceso (`taskkill /T /F` en Windows y un
grupo de procesos en POSIX) y recoge el proceso padre con plazos acotados. La
salida va a archivos —no a pipes heredables— y sólo se leen colas acotadas de
stdout, stderr y `crash.log`, evitando que un descendiente deje colgado el
vaciado de handles. Si el proceso falla, esa evidencia se adjunta al error.

El workflow `release.yml` también contiene el mismo smoke inmediatamente después
del build y antes de SBOM, firma Cosign y publicación. Su job conserva
`if: ${{ false }}`: esto protege la futura receta sin reanudar releases ni
cambiar el proceso de firma.

El alcance es deliberadamente acotado: no arranca la GUI, no verifica el cold
boot en una máquina Windows limpia y no prueba MO2/USVFS, herramientas externas,
rollback ni firma. Esos gates manuales siguen vigentes; el smoke automatizado no
los sustituye.

## Gate documental previo

1. El SHA candidato es único y la rama está sincronizada.
2. CI terminó verde sobre ese SHA.
3. El artifact descargado corresponde al SHA.
4. Se realiza cold boot en Windows limpio.
5. Se ejecuta el smoke de MO2/USVFS cuando la release lo promete.
6. README, CHANGELOG, DEPLOYMENT y SECURITY describen el mismo estado.
7. Checksums, firmas y SBOM se publican sólo si fueron generados y verificados.

CI y rig real son gates distintos; ninguno sustituye al otro.
