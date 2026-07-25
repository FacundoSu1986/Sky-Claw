# Release

> **Audiencia:** maintainers y responsables de entrega.
>
> **Estado:** Parcial; existe packaging y CI, no una release GA firmada
> verificada en el árbol actual.
>
> **Fuentes canónicas:** `.github/workflows/ci.yml`,
> `.github/workflows/release.yml`, `sky_claw.spec`, `build.bat`,
> `CHANGELOG.md` y `DEPLOYMENT.md`.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Qué existe

- Cinco gates en CI: Lint, Mypy, Tests, Security y Build.
- Build PyInstaller y artifact de CI.
- Lockfiles `requirements.lock` y `uv.lock`.
- Runbook de deployment y checklist de rig real.

## Qué no debe afirmarse todavía

La documentación actual no aporta evidencia de:

- tag de versión publicado;
- binario firmado y validado;
- firma cosign;
- SBOM publicado como artefacto de release;
- instalador GA validado en un host limpio.

Estas capacidades deben figurar como pendientes hasta que el workflow y una
release concreta produzcan evidencia inspeccionable.

## Gate documental previo

1. El SHA candidato es único y la rama está sincronizada.
2. CI terminó verde sobre ese SHA.
3. El artifact descargado corresponde al SHA.
4. Se realiza cold boot en Windows limpio.
5. Se ejecuta el smoke de MO2/USVFS cuando la release lo promete.
6. README, CHANGELOG, DEPLOYMENT y SECURITY describen el mismo estado.
7. Checksums, firmas y SBOM se publican sólo si fueron generados y verificados.

CI y rig real son gates distintos; ninguno sustituye al otro.
