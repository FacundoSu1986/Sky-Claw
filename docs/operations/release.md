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

- Cinco gates en CI: Lint, Mypy, Tests, Security y Build.
- Build PyInstaller y artifact de CI.
- Lockfiles `requirements.lock` y `uv.lock`.
- Runbook de deployment y checklist de rig real.
- La release `v0.2.4`, cuyo workflow terminó correctamente, publicó
  `SkyClawApp.exe`, el SBOM SPDX y `SkyClawApp.exe.bundle.json`.
- `.github/workflows/release.yml` ejecuta `Cosign sign-blob` keyless mediante
  OIDC y publica el bundle junto con el ejecutable y el SBOM.

## Cosign no es Authenticode

El bundle Cosign aporta material para verificar la firma del blob descargado.
No equivale a una firma de publisher de Windows: `sky_claw.spec` mantiene
`codesign_identity=None`, por lo que `SkyClawApp.exe` no lleva Authenticode y
puede seguir mostrando advertencias de SmartScreen.

## Qué no debe afirmarse todavía

Esta actualización documental no verificó de forma independiente la firma
criptográfica de `v0.2.4`, no ejecutó un cold boot de `SkyClawApp.exe` y no
validó un instalador GA en un host limpio. La existencia del workflow, sus
assets y una ejecución exitosa no sustituye esos smokes.

## Gate documental previo

1. El SHA candidato es único y la rama está sincronizada.
2. CI terminó verde sobre ese SHA.
3. El artifact descargado corresponde al SHA.
4. Se realiza cold boot en Windows limpio.
5. Se ejecuta el smoke de MO2/USVFS cuando la release lo promete.
6. README, CHANGELOG, DEPLOYMENT y SECURITY describen el mismo estado.
7. Checksums, firmas y SBOM se publican sólo si fueron generados y verificados.

CI y rig real son gates distintos; ninguno sustituye al otro.
