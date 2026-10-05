"""El archivo sólo autoriza borrar el mismo commit, en todas las superficies."""

from __future__ import annotations

import os
import re
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
    (repo / "archivo.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "archivo.txt")
    git(repo, "commit", "-m", "Base")
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
    git(repo, "config", f"url.{remoto.as_uri()}.insteadOf", "https://github.com/FacundoSu1986/Sky-Claw.git")
    git(repo, "remote", "set-url", "origin", "https://github.com/FacundoSu1986/Sky-Claw.git")
    wrapper = tmp_path / "ejecutar.ps1"
    wrapper.write_text(
        r"""param([string]$Script, [string]$Bundle, [string]$Manifest,
    [switch]$Execute, [switch]$IncludeRemote, [switch]$All, [string]$Branch)
$ErrorActionPreference = 'Stop'
function git {
    if ($args[0] -eq 'remote' -and $args[1] -eq 'get-url') {
        $global:LASTEXITCODE = 0
        if ($env:ARCHIVE_FALLO -eq 'origin-ajeno') { return 'https://github.com/otro/archivo.git' }
        return 'https://github.com/FacundoSu1986/Sky-Claw.git'
    }
    $salida = & $env:ARCHIVE_GIT @args 2>&1
    $codigo = $LASTEXITCODE
    if ($env:ARCHIVE_CARRERA -eq 'checkout' -and $args[0] -eq 'branch') {
        & $env:ARCHIVE_GIT worktree add $env:ARCHIVE_LATE_DIR archivada-local 2>&1 | Out-Null
    }
    if ($env:ARCHIVE_CARRERA -eq 'checkout-remoto' -and $args[0] -eq 'branch') {
        & $env:ARCHIVE_GIT worktree add -b archivada-remota $env:ARCHIVE_LATE_DIR refs/remotes/origin/archivada-remota 2>&1 | Out-Null
    }
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
        if ($env:ARCHIVE_FALLO -eq 'repo-default' -and
            ($args -notcontains '--repo' -or $args -notcontains 'github.com/FacundoSu1986/Sky-Claw')) {
            $global:LASTEXITCODE = 9; return
        }
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
    if ($Branch) { & $Script -Bundle $Bundle -Branch $Branch }
    elseif ($All) { & $Script -Bundle $Bundle -All }
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
    rama: str = "",
    cwd: Path | None = None,
    git_dir: str = "",
) -> subprocess.CompletedProcess[str]:
    """Ejercita el script productivo con guardas y refs reales en disco."""
    script = "restore.ps1" if restaurar else "delete-branches.ps1"
    script_temporal = Path(archivo["repo"]) / "archive" / "git-branches" / script
    script_temporal.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RAIZ / "archive" / "git-branches" / script, script_temporal)
    args = [
        str(POWERSHELL),
        "-NoProfile",
        "-File",
        str(archivo["wrapper"]),
        "-Script",
        str(script_temporal),
        "-Bundle",
        str(archivo["bundle"]),
    ]
    if restaurar:
        args += ["-Branch", rama] if rama else ["-All"]
    else:
        args += ["-Manifest", str(archivo["manifest"]), "-Execute"]
        if remoto:
            args += ["-IncludeRemote"]
    env = dict(
        os.environ,
        ARCHIVE_GIT=str(GIT),
        ARCHIVE_FALLO=fallo,
        ARCHIVE_CARRERA=carrera,
        ARCHIVE_NUEVO=nuevo,
        ARCHIVE_LATE_DIR=str(Path(archivo["repo"]).parent / "checkout-tardio"),
    )
    if git_dir:
        env["GIT_DIR"] = git_dir
    return subprocess.run(
        args,
        cwd=cwd or archivo["repo"],
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


@pytest.mark.parametrize(
    "ref", ["refs/heads/archivada-remota", "refs/remotes/origin/archivada-remota", "archivada-remota"]
)
def test_restauracion_individual_distingue_refs_del_mismo_nombre(archivo: dict[str, Path | str], ref: str) -> None:
    """Los dos tips son seleccionables; un nombre corto ambiguo requiere aclaración."""
    repo = Path(archivo["repo"])
    git(repo, "branch", "-D", "archivada-remota")
    resultado = ejecutar(archivo, restaurar=True, rama=ref)
    if ref == "archivada-remota":
        assert resultado.returncode != 0
    else:
        assert resultado.returncode == 0, resultado.stdout + resultado.stderr
        esperado = archivo["base"] if ref.startswith("refs/heads/") else archivo["remoto_sha"]
        assert git(repo, "rev-parse", "refs/heads/archivada-remota") == esperado


def test_borrado_se_ancla_al_clon_del_script_y_al_repo_explicito_de_github(archivo: dict[str, Path | str]) -> None:
    """Otro cwd y el default de gh no cambian el repositorio auditado y mutado."""
    repo = Path(archivo["repo"])
    ajeno = repo.parent / "clon-ajeno"
    git(repo, "clone", "--branch", "main", str(archivo["bundle"]), str(ajeno))
    git(ajeno, "branch", "archivada-local", str(archivo["base"]))
    resultado = ejecutar(archivo, cwd=ajeno, fallo="repo-default")
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    assert git(ajeno, "rev-parse", "refs/heads/archivada-local") == archivo["base"]
    assert git(repo, "branch", "--format=%(refname:short)") == "main"


def test_origin_de_otro_repo_aborta_sin_borrar(archivo: dict[str, Path | str]) -> None:
    """El archivo de Sky-Claw no autoriza borrar un fork u otro repositorio."""
    resultado = ejecutar(archivo, remoto=True, fallo="origin-ajeno")
    assert resultado.returncode != 0
    assert git(Path(archivo["repo"]), "rev-parse", "refs/heads/archivada-local") == archivo["base"]


def test_git_dir_heredado_no_redirige_el_borrado_a_otro_clon(archivo: dict[str, Path | str]) -> None:
    """-C no neutraliza GIT_DIR: un entorno local heredado debe abortar primero."""
    repo = Path(archivo["repo"])
    ajeno = repo.parent / "clon-env-ajeno"
    git(repo, "clone", "--branch", "main", str(archivo["bundle"]), str(ajeno))
    git(ajeno, "branch", "archivada-local", str(archivo["base"]))
    resultado = ejecutar(archivo, git_dir=str(ajeno / ".git"))
    assert resultado.returncode != 0
    assert git(ajeno, "rev-parse", "refs/heads/archivada-local") == archivo["base"]
    assert git(repo, "rev-parse", "refs/heads/archivada-local") == archivo["base"]


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


@pytest.mark.parametrize("guarda", ["abierto", "worktree", "worktree-tardio"])
def test_conserva_ramas_con_pr_abierto_o_worktree(archivo: dict[str, Path | str], guarda: str) -> None:
    """El compare-and-delete mantiene las guardas que protegen sesiones activas."""
    repo = Path(archivo["repo"])
    if guarda == "worktree":
        git(repo, "worktree", "add", str(repo.parent / "checkout"), "archivada-local")
    resultado = ejecutar(archivo, fallo=guarda, carrera="checkout" if guarda == "worktree-tardio" else "")
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    assert git(repo, "rev-parse", "refs/heads/archivada-local") == archivo["base"]


def test_checkout_tardio_de_rama_solo_remota_protege_ambas_superficies(archivo: dict[str, Path | str]) -> None:
    """Una rama local nueva en uso veta también el borrado de su hermano remoto."""
    repo = Path(archivo["repo"])
    git(repo, "branch", "-D", "archivada-remota")
    resultado = ejecutar(archivo, remoto=True, carrera="checkout-remoto")
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    assert git(repo, "rev-parse", "refs/heads/archivada-remota") == archivo["remoto_sha"]
    assert (
        git(repo, "ls-remote", "--heads", "origin", "refs/heads/archivada-remota").split()[0] == archivo["remoto_sha"]
    )


def test_pack_truncado_aborta_aunque_list_heads_y_verify_pasaran(archivo: dict[str, Path | str]) -> None:
    """Los headers válidos no prueban que los commits estén preservados."""
    bundle = Path(archivo["bundle"])
    contenido = bundle.read_bytes()
    bundle.write_bytes(contenido[: contenido.index(b"PACK")])
    repo = Path(archivo["repo"])
    assert git(repo, "bundle", "list-heads", str(bundle))
    git(repo, "bundle", "verify", str(bundle))
    resultado = ejecutar(archivo)
    assert resultado.returncode != 0
    assert git(repo, "rev-parse", "refs/heads/archivada-local") == archivo["base"]


@pytest.mark.parametrize("operacion", ["rebase-merge", "rebase-apply", "rebase-update-refs", "bisect"])
def test_conserva_rama_activa_aunque_worktree_aparezca_detached(archivo: dict[str, Path | str], operacion: str) -> None:
    """Enumera operaciones de Git que ocultan la rama y sus hermanas en porcelain."""
    repo = Path(archivo["repo"])
    checkout = repo.parent / "checkout-activo"
    git(repo, "worktree", "add", str(checkout), "archivada-local")
    (checkout / "archivo.txt").write_text("rama\n", encoding="utf-8")
    git(checkout, "add", "archivo.txt")
    git(checkout, "commit", "-m", "Trabajo de la rama")
    hermana = git(checkout, "rev-parse", "HEAD")
    if operacion == "rebase-update-refs":
        git(checkout, "branch", "-f", "archivada-remota", hermana)
        git(checkout, "commit", "--allow-empty", "-m", "Commit después de la rama hermana")
    if operacion == "bisect":
        git(checkout, "commit", "--allow-empty", "-m", "Segundo commit de la rama")
    tip = git(checkout, "rev-parse", "HEAD")
    git(repo, "bundle", "create", str(archivo["bundle"]), "--branches", "--remotes")
    if operacion == "bisect":
        git(checkout, "bisect", "start", tip, str(archivo["base"]))
    else:
        (repo / "archivo.txt").write_text("main\n", encoding="utf-8")
        git(repo, "add", "archivo.txt")
        git(repo, "commit", "-m", "Cambio conflictivo en main")
        opcion = {"rebase-merge": "--merge", "rebase-apply": "--apply", "rebase-update-refs": "--update-refs"}[
            operacion
        ]
        args = [str(GIT), "rebase", opcion, "main"]
        conflicto = subprocess.run(args, cwd=checkout, capture_output=True, text=True, timeout=20, check=False)
        assert conflicto.returncode != 0
        if operacion == "rebase-update-refs":
            git_dir = Path(git(checkout, "rev-parse", "--absolute-git-dir"))
            assert "refs/heads/archivada-remota" in (git_dir / "rebase-merge/update-refs").read_text()
    assert "detached" in git(repo, "worktree", "list", "--porcelain")
    resultado = ejecutar(archivo)
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    assert git(repo, "rev-parse", "refs/heads/archivada-local") == tip
    if operacion == "rebase-update-refs":
        assert git(repo, "rev-parse", "refs/heads/archivada-remota") == hermana


def test_familia_de_scripts_del_archivo_es_exacta() -> None:
    """Un mutador nuevo debe sumarse a las recetas conductuales de esta familia."""
    assert {p.name for p in (RAIZ / "archive" / "git-branches").glob("*.ps1")} == {"delete-branches.ps1", "restore.ps1"}
    source = (RAIZ / "archive" / "git-branches" / "delete-branches.ps1").read_text(encoding="utf-8-sig")
    receta = re.search(r"\$activeOperationFiles = @\(([^)]+)\)", source)
    assert receta is not None
    assert set(re.findall(r"'([^']+)'", receta.group(1))) == {
        "rebase-merge/head-name",
        "rebase-merge/update-refs",
        "rebase-apply/head-name",
        "BISECT_START",
    }
