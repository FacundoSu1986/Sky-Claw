"""PR-3e (ADR 0019 §2.1): fuentes de patchers ``GithubPatcherSettings`` fijadas por commit.

``run_capture`` se reemplaza por un falso de ``git`` que registra argv/env y simula el checkout:
fija el ORDEN (HITL → init → fetch → checkout → rev-parse), el aislamiento de la configuración del
usuario y la caché sin red. La descarga real se verifica en el rig.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
from typing import Any

import pytest

from sky_claw.app.security.hitl import Decision
from sky_claw.local.tools import synthesis_fuentes_git as sg

_SHA = "0123456789abcdef0123456789abcdef01234567"
_URL = "https://github.com/autor/MiPatcher"


class _Hitl:
    def __init__(self, decision: Decision) -> None:
        self.decision = decision
        self.pedidos: list[dict[str, Any]] = []

    async def request_approval(self, **kwargs: Any) -> Decision:
        self.pedidos.append(kwargs)
        return self.decision


class _Git:
    """Falso de ``run_capture`` para ``git``: simula un checkout con archivos y un ``.git``."""

    def __init__(self, *, head: str = _SHA, rc_fetch: int = 0) -> None:
        self.llamadas: list[dict[str, Any]] = []
        self.head = head
        self.rc_fetch = rc_fetch

    async def __call__(self, args: list[str], **kwargs: Any) -> tuple[bytes, bytes, int]:
        config_global = kwargs["env"].get("GIT_CONFIG_GLOBAL")
        # Se lee EN la llamada: después la carpeta de trabajo se publica con otro nombre.
        leido = pathlib.Path(config_global).read_text(encoding="utf-8") if config_global else None
        self.llamadas.append({"args": args, "config_global_leido": leido, **kwargs})
        verbo = next(a for a in args[1:] if a in {"init", "fetch", "checkout", "rev-parse"})
        if verbo == "init":
            repo = pathlib.Path(args[-1])
            (repo / ".git").mkdir(parents=True)
            (repo / ".git" / "config").write_text("[core]", encoding="utf-8")
        elif verbo == "fetch" and self.rc_fetch:
            return b"", b"fatal: fetch fallo simulado", self.rc_fetch
        elif verbo == "checkout":
            repo = pathlib.Path(args[args.index("-C") + 1])
            (repo / "MiPatcher").mkdir()
            (repo / "MiPatcher" / "MiPatcher.csproj").write_text("<Project/>", encoding="utf-8")
            (repo / "MiPatcher" / "Program.cs").write_text("// patcher", encoding="utf-8")
        elif verbo == "rev-parse":
            return f"{self.head}\n".encode(), b"", 0
        return b"", b"", 0


def _git_exe(tmp_path: pathlib.Path) -> pathlib.Path:
    exe = tmp_path / "git" / "git.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"git")
    return exe


async def _obtener(
    tmp_path: pathlib.Path,
    hitl: object,
    git: _Git,
    monkeypatch: pytest.MonkeyPatch,
    *,
    url: str = _URL,
    sha: str = _SHA,
) -> sg.FuentesGit:
    monkeypatch.setattr(sg, "run_capture", git)
    return await sg.obtener_fuentes_git(
        url,
        sha,
        cache_raiz=tmp_path / "cache",
        git=_git_exe(tmp_path),
        hitl=hitl,  # type: ignore[arg-type]
        timeout_s=60,
        max_output_bytes=1_000_000,
    )


async def test_cache_miss_pide_hitl_y_baja_el_commit_en_orden(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hitl, git = _Hitl(Decision.APPROVED), _Git()
    fuentes = await _obtener(tmp_path, hitl, git, monkeypatch)

    assert fuentes.cache_hit is False
    detalle = hitl.pedidos[0]["detail"].splitlines()
    assert [linea for linea in detalle if linea.startswith(("Hosts:", "Repositorio:", "Commit:"))] == [
        "Repositorio: https://github.com/autor/MiPatcher",
        f"Commit: {_SHA}",
        "Hosts: github.com",
    ]
    repo = pathlib.Path(git.llamadas[0]["args"][-1])
    assert [ll["args"][1:] for ll in git.llamadas] == [
        sg.argumentos_git_init(repo),
        sg.argumentos_git_fetch(repo, _URL, _SHA),
        sg.argumentos_git_checkout(repo),
        sg.argumentos_git_rev_parse(repo),
    ]
    assert all(ll["require_job"] is True and ll["max_output_bytes"] == 1_000_000 for ll in git.llamadas)
    # La caché guarda el árbol SIN .git: es lo que se compila y lo que se fija por hash.
    assert (fuentes.raiz / "MiPatcher" / "MiPatcher.csproj").is_file()
    assert not (fuentes.raiz / ".git").exists()


def test_la_configuracion_de_git_es_segura() -> None:
    # Igualdad literal: cada `-c` cierra una vía de escape conocida.
    assert sg.CONFIG_GIT_SEGURA == [
        "-c",
        "core.symlinks=false",  # un symlink del repo no se materializa como enlace
        "-c",
        "core.longpaths=true",
        "-c",
        "protocol.allow=never",  # todo protocolo prohibido salvo...
        "-c",
        "protocol.https.allow=always",  # ...https
        "-c",
        "submodule.recurse=false",
        "-c",
        "credential.helper=",  # sin credenciales guardadas del usuario
        "-c",
        "core.fsmonitor=false",
    ]
    repo = pathlib.Path("C:/cache/repo")
    assert sg.argumentos_git_fetch(repo, _URL, _SHA) == [
        *sg.CONFIG_GIT_SEGURA,
        "-C",
        str(repo),
        "fetch",
        "--depth",
        "1",
        "--no-tags",
        "--no-recurse-submodules",
        "--quiet",
        _URL,
        _SHA,
    ]


async def test_el_entorno_aisla_la_configuracion_del_usuario(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Un `url.insteadOf` del ~/.gitconfig del usuario podría redirigir github.com a otro host.
    monkeypatch.setenv("SKYCLAW_TOKEN_SECRETO", "no-debe-pasar")
    git = _Git()
    await _obtener(tmp_path, _Hitl(Decision.APPROVED), git, monkeypatch)
    for llamada in git.llamadas:
        env = llamada["env"]
        assert "SKYCLAW_TOKEN_SECRETO" not in {k.upper() for k in env}
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert llamada["config_global_leido"] == ""  # un ~/.gitconfig vacío: sin insteadOf del usuario
        # Sin el HOME del usuario: un .netrc/_netrc con github.com autenticaría el fetch.
        assert pathlib.Path(env["HOME"]).parent.name.startswith(".trabajo-")
        assert env["USERPROFILE"] == env["HOME"]
        assert not {"HOMEDRIVE", "HOMEPATH"} & {k.upper() for k in env}
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GCM_INTERACTIVE"] == "never"


async def test_cache_hit_no_pide_hitl_ni_lanza_git(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    primero = await _obtener(tmp_path, _Hitl(Decision.APPROVED), _Git(), monkeypatch)
    hitl, git = _Hitl(Decision.DENIED), _Git()
    segundo = await _obtener(tmp_path, hitl, git, monkeypatch)
    assert segundo.cache_hit is True and segundo.raiz == primero.raiz
    assert hitl.pedidos == [] and git.llamadas == []


async def test_la_url_con_mayusculas_o_git_usa_la_misma_cache(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primero = await _obtener(tmp_path, _Hitl(Decision.APPROVED), _Git(), monkeypatch)
    segundo = await _obtener(
        tmp_path, _Hitl(Decision.DENIED), _Git(), monkeypatch, url="https://github.com/Autor/MiPatcher.git"
    )
    assert segundo.cache_hit is True and segundo.raiz == primero.raiz


async def test_cache_alterada_falla_cerrado(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    primero = await _obtener(tmp_path, _Hitl(Decision.APPROVED), _Git(), monkeypatch)
    (primero.raiz / "MiPatcher" / "Program.cs").write_text("// alterado", encoding="utf-8")
    git = _Git()
    with pytest.raises(sg.FuentesGitError, match="caché"):
        await _obtener(tmp_path, _Hitl(Decision.APPROVED), git, monkeypatch)
    assert git.llamadas == []


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/autor/MiPatcher",
        "https://gitlab.com/autor/MiPatcher",
        "https://usuario:clave@github.com/autor/MiPatcher",
        "https://github.com:8443/autor/MiPatcher",
        "https://github.com/autor/MiPatcher?ref=x",
        "https://github.com/autor/MiPatcher#x",
        "https://github.com/autor",
        "https://github.com/autor/MiPatcher/extra",
        "https://github.com/../MiPatcher",
        "git@github.com:autor/MiPatcher.git",
        "file:///C:/repo",
    ],
)
async def test_url_no_admitida_falla_sin_red(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    hitl, git = _Hitl(Decision.APPROVED), _Git()
    with pytest.raises(sg.FuentesGitError):
        await _obtener(tmp_path, hitl, git, monkeypatch, url=url)
    assert hitl.pedidos == [] and git.llamadas == []


@pytest.mark.parametrize("sha", ["main", "v1.0", "0123456", _SHA.upper(), _SHA + "0", "g" * 40])
async def test_solo_un_sha_completo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, sha: str) -> None:
    hitl, git = _Hitl(Decision.APPROVED), _Git()
    with pytest.raises(sg.FuentesGitError, match="commit"):
        await _obtener(tmp_path, hitl, git, monkeypatch, sha=sha)
    assert hitl.pedidos == [] and git.llamadas == []


@pytest.mark.parametrize("decision", [Decision.DENIED, Decision.TIMEOUT])
async def test_sin_aprobacion_no_se_abre_red(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, decision: Decision
) -> None:
    git = _Git()
    with pytest.raises(sg.FuentesGitError, match="egress"):
        await _obtener(tmp_path, _Hitl(decision), git, monkeypatch)
    assert git.llamadas == []


async def test_sin_hitl_no_se_abre_red(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git = _Git()
    with pytest.raises(sg.FuentesGitError, match="egress"):
        await _obtener(tmp_path, None, git, monkeypatch)
    assert git.llamadas == []


async def test_head_distinto_del_commit_pedido_falla(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(sg.FuentesGitError, match="HEAD"):
        await _obtener(tmp_path, _Hitl(Decision.APPROVED), _Git(head="f" * 40), monkeypatch)
    assert not list((tmp_path / "cache").glob("*/marca.json"))


async def test_fallo_de_git_falla_con_su_salida(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(sg.FuentesGitError, match="fetch fallo simulado"):
        await _obtener(tmp_path, _Hitl(Decision.APPROVED), _Git(rc_fetch=128), monkeypatch)
    assert not list((tmp_path / "cache").glob("*/marca.json"))


async def test_error_del_sistema_es_error_de_fuentes(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _falla(args: list[str], **kwargs: Any) -> tuple[bytes, bytes, int]:
        raise OSError("no se pudo asignar el Job Object")

    monkeypatch.setattr(sg, "run_capture", _falla)
    with pytest.raises(sg.FuentesGitError, match="Job Object"):
        await sg.obtener_fuentes_git(
            _URL,
            _SHA,
            cache_raiz=tmp_path / "cache",
            git=_git_exe(tmp_path),
            hitl=_Hitl(Decision.APPROVED),  # type: ignore[arg-type]
            timeout_s=60,
            max_output_bytes=1_000_000,
        )


async def test_git_inexistente_falla(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sg, "run_capture", _Git())
    with pytest.raises(sg.FuentesGitError, match="git"):
        await sg.obtener_fuentes_git(
            _URL,
            _SHA,
            cache_raiz=tmp_path / "cache",
            git=tmp_path / "no" / "git.exe",
            hitl=_Hitl(Decision.APPROVED),  # type: ignore[arg-type]
            timeout_s=60,
            max_output_bytes=1_000_000,
        )


async def test_cache_raiz_relativa_falla_sin_red(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # git corre con cwd=trabajo: una ruta relativa apuntaría a otra carpeta.
    hitl, git = _Hitl(Decision.APPROVED), _Git()
    monkeypatch.setattr(sg, "run_capture", git)
    with pytest.raises(sg.FuentesGitError, match="absoluta"):
        await sg.obtener_fuentes_git(
            _URL,
            _SHA,
            cache_raiz=pathlib.Path("cache_relativa"),
            git=_git_exe(tmp_path),
            hitl=hitl,  # type: ignore[arg-type]
            timeout_s=60,
            max_output_bytes=1_000_000,
        )
    assert hitl.pedidos == [] and git.llamadas == []


async def test_publica_solo_el_arbol_y_la_marca(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fuentes = await _obtener(tmp_path, _Hitl(Decision.APPROVED), _Git(), monkeypatch)
    assert sorted(p.name for p in fuentes.raiz.parent.iterdir()) == ["arbol", "marca.json"]
    assert not list((tmp_path / "cache").glob(".trabajo-*"))  # el checkout con .git no queda


async def test_un_fallo_no_deja_carpetas_de_trabajo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(sg.FuentesGitError):
        await _obtener(tmp_path, _Hitl(Decision.APPROVED), _Git(rc_fetch=128), monkeypatch)
    assert not list((tmp_path / "cache").glob(".trabajo-*"))


_GIT_REAL = shutil.which("git")


@pytest.mark.skipif(_GIT_REAL is None, reason="requiere git real en PATH")
def test_un_repo_ancestro_no_redirige_ls_remote_ni_fetch(tmp_path: pathlib.Path) -> None:
    # Si la carpeta de trabajo cae dentro de un checkout, git sube buscando un `.git` y carga su
    # config: un `url.insteadOf` del ancestro reescribiría github.com hacia otro host (CodeRabbit,
    # #1017). Los dos hermanos que usan el entorno se verifican: ls-remote (cwd sin `.git`) y el
    # fetch (corre sobre `trabajo/repo`, su propio repo).
    assert _GIT_REAL is not None
    ancestro = tmp_path / "checkout"
    ancestro.mkdir()
    subprocess.run([_GIT_REAL, "init", "--quiet", str(ancestro)], check=True)
    subprocess.run(
        [_GIT_REAL, "-C", str(ancestro), "config", "url.https://mirror.example/.insteadOf", "https://github.com/"],
        check=True,
    )
    trabajo = ancestro / "raiz" / ".trabajo-x"
    env = sg._preparar_entorno_git(trabajo)
    url = "https://github.com/a/r"

    def url_efectiva(*previos: str) -> str:
        salida = subprocess.run(
            [_GIT_REAL, *previos, "ls-remote", "--get-url", url],
            cwd=trabajo,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return salida.stdout.strip()

    assert url_efectiva() == url  # ls-remote: no hereda la config del checkout ancestro
    subprocess.run([_GIT_REAL, *sg.argumentos_git_init(trabajo / "repo")], cwd=trabajo, env=env, check=True)
    assert url_efectiva("-C", str(trabajo / "repo")) == url  # fetch: solo su propio repo
