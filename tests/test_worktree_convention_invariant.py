"""Ancla de aislamiento: raíces manual/Codex, transición cerrada y rol principal.

El censo real de git y los casos sintéticos enumeran la misma política. Una
consulta fallida no equivale a una lista vacía que deja pasar la sesión.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
AGENTS_MD = RAIZ / "AGENTS.md"
GITIGNORE = RAIZ / ".gitignore"

NOMBRE_CONVENCION = ".worktrees"
NOMBRE_ANCLA = "test_worktree_convention_invariant"
TITULO_SECCION = "## Una sesión, un worktree"

# Snapshot de transición autorizado el 2026-10-05; cada binding es ruta + rama.
# No permite crear sesiones nuevas bajo C:/Worktrees ni reutilizar otro checkout.
LEGADOS: dict[str, str | None] = {
    "C:/Worktrees/Sky-Claw-math-a": "fix-native-parallax-math-foundation",
    "C:/Worktrees/Sky-Claw-math-b": "fix-native-parallax-spearman-ties",
    "C:/Worktrees/Sky-Claw-p0-alpha209-uia": "research/dyndolod-p0-alpha209-uia",
    "C:/Worktrees/Sky-Claw-r1-packaging-cancel": "fix/dyndolod-r1-packaging-cancel",
    "E:/Skyclaw_Frozen_Runtime_P3": "feat/frozen-runtime-p3-candidate",
    "E:/Skyclaw_Steam_Frozen_Runtime": "feat/steam-frozen-runtime",
    "C:/Users/Facu2/WorkBuddy/Worktrees/Skyclaw_Main_Sync/main-d17cd082": "workbuddy/main-d17cd082",
    "E:/Skyclaw_Main_Sync/.kilo/worktrees/broadleaf-professor": None,
}


def _normalizar(ruta: str | Path) -> str:
    """Comparable entre plataformas: resuelve symlinks y normaliza mayúsculas en Windows."""
    return os.path.normcase(os.path.realpath(str(ruta)))


def listar_worktrees() -> list[tuple[str, str | None]]:
    """Censo completo de git (principal primero); errores se propagan fail-closed."""
    try:
        resultado = subprocess.run(
            ["git", "worktree", "list", "--porcelain", "-z"],
            cwd=str(RAIZ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AssertionError("No se pudo auditar el registro de worktrees") from exc
    assert resultado.returncode == 0, f"git worktree list falló: {resultado.stderr}"
    registros: list[tuple[str, str | None]] = []
    for campo in resultado.stdout.split("\0"):
        if campo.startswith("worktree "):
            registros.append((campo.removeprefix("worktree "), None))
        elif campo.startswith("branch refs/heads/") and registros:
            registros[-1] = (registros[-1][0], campo.removeprefix("branch refs/heads/"))
    return registros


def auditar_registro(
    registros: list[tuple[str, str | None]],
    sesion: Path,
    raiz_codex: Path,
    legados: dict[str, str | None],
    *,
    rol: str = "agente",
) -> list[str]:
    """Aplica una política única al censo real y a todas las recetas sintéticas."""
    if not registros:
        return ["Registro vacío: no se puede verificar el aislamiento"]
    principal = _normalizar(registros[0][0])
    manual = _normalizar(Path(registros[0][0]) / NOMBRE_CONVENCION) + os.sep
    codex = _normalizar(raiz_codex) + os.sep
    actual = _normalizar(sesion)
    errores = []
    if rol not in {"agente", "integracion", "ci"}:
        errores.append(f"Rol desconocido: {rol}")
    if actual not in {_normalizar(ruta) for ruta, _ in registros}:
        errores.append("La sesión no está registrada como worktree")
    if actual == principal and rol == "agente":
        errores.append("La sesión de agente usa el principal; crear un worktree propio")
    for ruta, rama in registros[1:]:
        normalizada = _normalizar(ruta)
        if normalizada.startswith(manual) or normalizada.startswith(codex):
            continue
        if normalizada in legados and legados[normalizada] == rama:
            continue
        errores.append(f"Fuera de convención: {ruta} ({rama})")
    return errores


def test_los_worktrees_no_principales_viven_bajo_la_convencion() -> None:
    """Censa todas las raíces y verifica además el rol de la sesión actual."""
    if not (RAIZ / ".git").exists():
        pytest.skip("Distribución sin metadatos git: no hay registro que auditar")
    registros = listar_worktrees()
    ausentes = [ruta for ruta, _ in registros if not Path(ruta).is_dir()]
    assert not ausentes, f"Worktrees registrados sin directorio; revisar y podar: {ausentes}"
    raiz_codex = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "worktrees"
    rol = "ci" if os.environ.get("GITHUB_ACTIONS") == "true" else os.environ.get("SKYCLAW_WORKTREE_ROLE", "agente")
    errores = auditar_registro(registros, RAIZ, raiz_codex, {_normalizar(r): b for r, b in LEGADOS.items()}, rol=rol)
    assert not errores, "\n".join(errores)


def test_la_convencion_esta_documentada_en_agents_md() -> None:
    """La regla no se borra en silencio: si el texto o su ancla desaparecen, esto falla."""
    contenido = AGENTS_MD.read_text(encoding="utf-8")

    assert TITULO_SECCION in contenido, (
        f"AGENTS.md ya no contiene la sección `{TITULO_SECCION}`. La convención de "
        "worktrees es la que evita que las sesiones se pisen entre sí: si se quita de "
        "la guía, el ancla que la verifica debe quitarse también, en el mismo cambio."
    )
    assert NOMBRE_CONVENCION in contenido, (
        f"AGENTS.md documenta la convención pero no nombra `{NOMBRE_CONVENCION}`; sin la ruta "
        "exacta un agente no puede saber dónde crear su worktree."
    )
    assert NOMBRE_ANCLA in contenido, (
        f"AGENTS.md no referencia el ancla `{NOMBRE_ANCLA}`: la regla y el gate que la "
        "verifica tienen que quedar enlazados, o la regla envejece."
    )


def test_el_ignorar_cubre_el_directorio_de_worktrees() -> None:
    """Ningún worktree de agente puede entrar a un PR."""
    reglas = [
        linea.strip()
        for linea in GITIGNORE.read_text(encoding="utf-8").splitlines()
        if linea.strip() and not linea.strip().startswith("#")
    ]

    assert any(regla.rstrip("/") == NOMBRE_CONVENCION for regla in reglas), (
        f".gitignore no cubre `{NOMBRE_CONVENCION}/`. Sin esa regla, el checkout de un "
        "worktree de agente aparece como untracked en `git status` y puede terminar "
        "commiteado en un PR por accidente."
    )


@pytest.mark.parametrize("relativa", [".worktrees/manual", ".codex/worktrees/sesion/repo"])
def test_admite_raiz_manual_y_checkout_gestionado_por_codex(tmp_path: Path, relativa: str) -> None:
    """Ambas raíces conservan el aislamiento de la sesión."""
    principal = tmp_path / "repo"
    secundaria = principal / relativa if relativa.startswith(".worktrees") else tmp_path / relativa
    assert not auditar_registro(
        [(str(principal), "main"), (str(secundaria), "codex/cambio")],
        secundaria,
        tmp_path / ".codex" / "worktrees",
        {},
    )


@pytest.mark.parametrize("rol", ["agente", "integracion", "ci"])
def test_principal_solo_admite_integracion_o_ci(tmp_path: Path, rol: str) -> None:
    """La familia incluye la sesión principal: no puede quedar excluida del gate."""
    errores = auditar_registro([(str(tmp_path), "main")], tmp_path, tmp_path / "codex", {}, rol=rol)
    assert bool(errores) == (rol == "agente")


def test_legacy_admite_solo_el_binding_exacto_de_ruta_y_rama(tmp_path: Path) -> None:
    """Una exención de transición no autoriza nuevas sesiones bajo la raíz antigua."""
    principal = tmp_path / "repo"
    antiguo = tmp_path / "legado"
    legados = {_normalizar(antiguo): "feat/legado"}
    for ruta, rama, permitido in [
        (antiguo, "feat/legado", True),
        (antiguo, "feat/otro", False),
        (tmp_path / "otro", "feat/legado", False),
    ]:
        errores = auditar_registro([(str(principal), "main"), (str(ruta), rama)], ruta, tmp_path / "codex", legados)
        assert bool(errores) != permitido


def test_registro_vacio_o_sesion_no_registrada_falla_cerrado(tmp_path: Path) -> None:
    """No poder observar el registro no equivale a cumplir la política."""
    assert auditar_registro([], tmp_path, tmp_path / "codex", {})
    assert auditar_registro([(str(tmp_path / "repo"), "main")], tmp_path / "ausente", tmp_path / "codex", {})


@pytest.mark.parametrize("ruta,rama", list(LEGADOS.items()))
def test_cada_binding_de_transicion_participa_del_gate(tmp_path: Path, ruta: str, rama: str | None) -> None:
    """Enumera todas las excepciones, incluyendo su rechazo al cambiar de rama."""
    legados = {_normalizar(r): b for r, b in LEGADOS.items()}
    registros = [(str(tmp_path), "main"), (ruta, rama)]
    assert not auditar_registro(registros, Path(ruta), tmp_path / "codex", legados)
    registros[1] = (ruta, "codex/otra-sesion")
    assert auditar_registro(registros, Path(ruta), tmp_path / "codex", legados)


def test_transicion_documentada_coincide_con_bindings() -> None:
    """Ninguna exención puede aparecer sólo en el texto o sólo en el código."""
    contenido = AGENTS_MD.read_text(encoding="utf-8")
    bloque = contenido.split("<!-- worktree-legacy:start -->")[1].split("<!-- worktree-legacy:end -->")[0]
    bindings = {
        ruta: None if rama == "DETACHED" else rama for ruta, rama in re.findall(r"\| `([^`]+)` \| `([^`]+)` \|", bloque)
    }
    assert bindings == LEGADOS


@pytest.mark.parametrize("relativa", [".worktrees-intruso/sesion", ".codex/worktrees-intruso/sesion", "otra/sesion"])
def test_prefijos_parecidos_no_amplian_las_raices(tmp_path: Path, relativa: str) -> None:
    """Las raíces se comparan con separador de directorio, no sólo por texto."""
    ruta = tmp_path / relativa
    assert auditar_registro(
        [(str(tmp_path), "main"), (str(ruta), "codex/otra")], ruta, tmp_path / ".codex" / "worktrees", {}
    )


def test_consulta_git_fallida_no_se_convierte_en_registro_vacio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un error de transporte/ejecución no es ausencia de worktrees."""
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", "fallo"))
    with pytest.raises(AssertionError, match="git worktree list"):
        listar_worktrees()
