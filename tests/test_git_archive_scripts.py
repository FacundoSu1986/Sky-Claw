"""El archivo sólo autoriza borrar el mismo commit, en todas las superficies."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(not POWERSHELL or not GIT, reason="Requiere git y PowerShell")


def git(repo: Path, *args: str) -> str:
    """Ejecuta git únicamente sobre el repositorio temporal del test."""
    return subprocess.run(
        [str(GIT), *args], cwd=repo, capture_output=True, text=True, check=True, timeout=20
    ).stdout.strip()


@pytest.fixture
def archivo(tmp_path: Path) -> dict[str, Path | str]:
    """Disco real; sólo se simula GitHub para no consultar ni borrar ramas del usuario."""
    repo = tmp_path / "repo"
    remoto = tmp_path / "origin.git"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Prueba de archivo")
    git(repo, "config", "user.email", "prueba@example.invalid")
    git(repo, "commit", "--allow-empty", "-m", "Base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "archivada-local")
    git(repo, "branch", "archivada-remota")
    git(repo, "init", "--bare", str(remoto))
    git(repo, "remote", "add", "origin", str(remoto))
    git(repo, "push", "origin", "archivada-local", "archivada-remota")
    git(repo, "commit", "--allow-empty", "-m", "Tip remoto distinto del local")
    remoto_sha = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "origin", f"{remoto_sha}:refs/heads/archivada-remota")
    bundle = tmp_path / "archivo.bundle"
    git(repo, "bundle", "create", str(bundle), "--branches", "--remotes")
    manifiesto = tmp_path / "MANIFEST.tsv"
    manifiesto.write_text(
        "\n".join(
            f"refs/heads/{nombre}\tlocal\tMERGED\t#1\tfecha\t{nombre}"
            for nombre in ("archivada-local", "archivada-remota")
        ),
        encoding="utf-8",
    )
    # El script reconoce GitHub, pero git redirige TODO el tráfico al bare temporal.
    git(repo, "config", f"url.{remoto.as_uri()}.insteadOf", "https://github.com/prueba/archivo.git")
    git(repo, "remote", "set-url", "origin", "https://github.com/prueba/archivo.git")
    wrapper = tmp_path / "ejecutar.ps1"
    wrapper.write_text(
        r"""param([string]$Script, [string]$Bundle, [string]$Manifest,
    [switch]$Execute, [switch]$IncludeRemote, [switch]$All)
$ErrorActionPreference = 'Stop'
function git {
    if ($args[0] -eq 'remote' -and $args[1] -eq 'get-url') {
        $global:LASTEXITCODE = 0
        return 'https://github.com/prueba/archivo.git'
    }
    $salida = & $env:ARCHIVE_GIT @args 2>&1
    $codigo = $LASTEXITCODE
    if ($env:ARCHIVE_FALLO -eq 'bundle' -and $args[0] -eq 'bundle' -and $args[1] -eq 'list-heads') {
        $codigo = 9
    }
    if ($env:ARCHIVE_CARRERA -eq 'local' -and $args[0] -eq 'update-ref' -and $args[1] -eq '-d') {
        & $env:ARCHIVE_GIT update-ref $args[2] $env:ARCHIVE_NUEVO
        $salida = & $env:ARCHIVE_GIT @args 2>&1
        $codigo = $LASTEXITCODE
    }
    if ($env:ARCHIVE_CARRERA -eq 'remota' -and $args[0] -eq 'push') {
        & $env:ARCHIVE_GIT push origin "$($env:ARCHIVE_NUEVO):refs/heads/archivada-remota" 2>&1 | Out-Null
        $salida = & $env:ARCHIVE_GIT @args 2>&1
        $codigo = $LASTEXITCODE
    }
    $global:LASTEXITCODE = $codigo
    $salida
}
function gh {
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'pr') {
        if ($env:ARCHIVE_FALLO -eq 'pr') { $global:LASTEXITCODE = 9; return }
        if ($env:ARCHIVE_FALLO -eq 'abierto') { return '[{"headRefName":"archivada-local"}]' }
        if ($env:ARCHIVE_FALLO -eq 'limite') {
            return (@(1..500 | ForEach-Object { @{headRefName="otra-$_"} }) | ConvertTo-Json -Compress)
        }
        return '[]'
    }
    # Permite demostrar el defecto anterior sin tocar un remoto real de GitHub.
    $nombre = $args[-1] -replace '^repos/prueba/archivo/git/refs/heads/', ''
    & $env:ARCHIVE_GIT push origin ":refs/heads/$nombre" 2>&1
}
try {
    if ($All) { & $Script -Bundle $Bundle -All }
    else { & $Script -Bundle $Bundle -Manifest $Manifest -Execute:$Execute -IncludeRemote:$IncludeRemote }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    exit 0
} catch { Write-Host $_; exit 1 }
""",
        encoding="utf-8-sig",
    )
    return {
        "repo": repo,
        "bundle": bundle,
        "manifest": manifiesto,
        "wrapper": wrapper,
        "base": base,
        "remoto_sha": remoto_sha,
    }


def ejecutar(
    archivo: dict[str, Path | str],
    *,
    remoto: bool = False,
    fallo: str = "",
    carrera: str = "",
    nuevo: str = "",
    restaurar: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Ejercita el script productivo con guardas y refs reales en disco."""
    script = "restore.ps1" if restaurar else "delete-branches.ps1"
    args = [
        str(POWERSHELL),
        "-NoProfile",
        "-File",
        str(archivo["wrapper"]),
        "-Script",
        str(RAIZ / "archive" / "git-branches" / script),
        "-Bundle",
        str(archivo["bundle"]),
    ]
    if restaurar:
        args += ["-All"]
    else:
        args += ["-Manifest", str(archivo["manifest"]), "-Execute"]
        if remoto:
            args += ["-IncludeRemote"]
    env = dict(os.environ, ARCHIVE_GIT=str(GIT), ARCHIVE_FALLO=fallo, ARCHIVE_CARRERA=carrera, ARCHIVE_NUEVO=nuevo)
    return subprocess.run(
        args,
        cwd=archivo["repo"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


@pytest.mark.parametrize("superficie", ["local", "remota"])
def test_no_borra_commits_posteriores_al_archivo(archivo: dict[str, Path | str], superficie: str) -> None:
    """El nombre archivado no autoriza borrar un tip nuevo ni local ni remoto."""
    repo = Path(archivo["repo"])
    git(repo, "commit", "--allow-empty", "-m", "Trabajo posterior al archivo")
    nuevo = git(repo, "rev-parse", "HEAD")
    nombre = "archivada-local" if superficie == "local" else "archivada-remota"
    if superficie == "local":
        git(repo, "update-ref", f"refs/heads/{nombre}", nuevo)
    else:
        git(repo, "push", "origin", f"{nuevo}:refs/heads/{nombre}")
    resultado = ejecutar(archivo, remoto=superficie == "remota")
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    actual = (
        git(repo, "rev-parse", f"refs/heads/{nombre}")
        if superficie == "local"
        else git(repo, "ls-remote", "--heads", "origin", f"refs/heads/{nombre}").split()[0]
    )
    assert actual == nuevo


def test_borra_solo_tips_iguales_usando_sha_de_cada_superficie(archivo: dict[str, Path | str]) -> None:
    """Las refs local y remota del mismo nombre pueden tener SHA archivados distintos."""
    resultado = ejecutar(archivo, remoto=True)
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    repo = Path(archivo["repo"])
    assert git(repo, "branch", "--format=%(refname:short)") == "main"
    assert not git(repo, "ls-remote", "--heads", "origin")


@pytest.mark.parametrize("fallo", ["bundle", "pr", "limite"])
def test_consultas_incompletas_abortan_sin_borrar(archivo: dict[str, Path | str], fallo: str) -> None:
    """Salida parcial o lista truncada nunca convierten una guarda fallida en permiso."""
    resultado = ejecutar(archivo, fallo=fallo)
    assert resultado.returncode != 0
    assert git(Path(archivo["repo"]), "rev-parse", "refs/heads/archivada-local") == archivo["base"]


@pytest.mark.parametrize("superficie", ["local", "remota"])
def test_cambio_entre_verificacion_y_borrado_conserva_el_commit(
    archivo: dict[str, Path | str], superficie: str
) -> None:
    """La comparación del tip también participa en la operación atómica de borrado."""
    repo = Path(archivo["repo"])
    git(repo, "commit", "--allow-empty", "-m", "Escritor concurrente")
    nuevo = git(repo, "rev-parse", "HEAD")
    resultado = ejecutar(archivo, remoto=superficie == "remota", carrera=superficie, nuevo=nuevo)
    assert resultado.returncode != 0
    if superficie == "local":
        assert git(repo, "rev-parse", "refs/heads/archivada-local") == nuevo
    else:
        assert git(repo, "ls-remote", "--heads", "origin", "refs/heads/archivada-remota").split()[0] == nuevo


def test_restaurar_todas_no_pisa_un_destino_modificado(archivo: dict[str, Path | str]) -> None:
    """-All tiene la misma protección que la restauración individual sin -Force."""
    repo = Path(archivo["repo"])
    git(repo, "commit", "--allow-empty", "-m", "Restauración con trabajo nuevo")
    nuevo = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "restored/archivada-local", nuevo)
    resultado = ejecutar(archivo, restaurar=True)
    assert resultado.returncode != 0
    assert git(repo, "rev-parse", "refs/heads/restored/archivada-local") == nuevo


def test_nombre_remoto_completo_no_colisiona_con_sufijos(archivo: dict[str, Path | str]) -> None:
    """Una rama prefijo/nombre no demuestra que exista origin/nombre."""
    repo = Path(archivo["repo"])
    git(repo, "push", "origin", ":refs/heads/archivada-remota")
    git(repo, "push", "origin", f"{archivo['base']}:refs/heads/prefijo/archivada-remota")
    resultado = ejecutar(archivo, remoto=True)
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    assert (
        git(repo, "ls-remote", "--heads", "origin", "refs/heads/prefijo/archivada-remota").split()[0] == archivo["base"]
    )


@pytest.mark.parametrize("guarda", ["abierto", "worktree"])
def test_conserva_ramas_con_pr_abierto_o_worktree(archivo: dict[str, Path | str], guarda: str) -> None:
    """El compare-and-delete mantiene las guardas que protegen sesiones activas."""
    repo = Path(archivo["repo"])
    if guarda == "worktree":
        git(repo, "worktree", "add", str(repo.parent / "checkout"), "archivada-local")
    resultado = ejecutar(archivo, fallo=guarda)
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    assert git(repo, "rev-parse", "refs/heads/archivada-local") == archivo["base"]


def test_familia_de_scripts_del_archivo_es_exacta() -> None:
    """Un mutador nuevo debe sumarse a las recetas conductuales de esta familia."""
    assert {p.name for p in (RAIZ / "archive" / "git-branches").glob("*.ps1")} == {"delete-branches.ps1", "restore.ps1"}
