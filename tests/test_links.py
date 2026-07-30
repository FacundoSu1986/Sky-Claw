"""Detección de enlaces (symlink/junction) — la primitiva compartida.

Existe porque la detección estaba duplicada en dos validadores
(``vfs_health._link_kind`` y ``overwrite_health._is_link``, cuyo docstring ya
declaraba que espejaba al primero) y estaba por triplicarse en el rollback de
directorios. La consolidación se ancla abajo con un test que **enumera** los
módulos que implementan la detección, no que muestrea uno.

Lo que hace no-trivial a esta primitiva, y por qué no puede ser un ``is_symlink()``
a pelo: ``os.path.islink()`` es **False** para un junction de Windows. Por eso
Python 3.12 tuvo que agregar ``is_junction``/``os.path.isjunction`` — y **3.11, que
este repo soporta y CI corre, no tiene ninguna de las dos**, así que ahí el junction
sólo se ve por el ``st_reparse_tag`` del ``lstat``. Toda esa matriz de
versión/plataforma vive en un solo módulo a propósito.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from sky_claw.app.security.links import is_link, link_kind, link_kind_or_raise, path_present
from tests._symlink_guard import crear_junction, junction_guard, symlink_guard


class TestLinkKind:
    """``link_kind`` nombra el tipo de enlace, o None si es una ruta real."""

    def test_directorio_real_no_es_enlace(self, tmp_path: pathlib.Path) -> None:
        real = tmp_path / "real"
        real.mkdir()

        assert link_kind(real) is None
        assert is_link(real) is False

    def test_archivo_real_no_es_enlace(self, tmp_path: pathlib.Path) -> None:
        archivo = tmp_path / "archivo.txt"
        archivo.write_text("x", encoding="utf-8")

        assert link_kind(archivo) is None
        assert is_link(archivo) is False

    def test_ruta_inexistente_no_es_enlace_y_no_hace_ruido(self, tmp_path: pathlib.Path) -> None:
        """Una ruta que no existe no es un enlace, y preguntarlo no debe lanzar.

        Es la divergencia real entre las dos copias que se consolidan:
        ``vfs_health._link_kind`` envolvía el ``lstat()`` en ``if path.exists()`` y
        ``overwrite_health._is_link`` no, así que el segundo hacía ``lstat`` sobre
        una ruta ausente y se comía un ``FileNotFoundError`` en el ``except``.
        """
        assert link_kind(tmp_path / "no-existe") is None
        assert is_link(tmp_path / "no-existe") is False

    def test_link_kind_or_raise_no_lanza_por_ruta_inexistente(self, tmp_path: pathlib.Path) -> None:
        """``FileNotFoundError`` es la ÚNICA excepción que ``link_kind_or_raise``
        no propaga (review CodeRabbit #404, segunda vuelta).

        "No existe" no es ambiguo como un lock transitorio de AV/indexer —
        reintentarlo no lo cambia, y es el caso NORMAL de un primer run. Dejarlo
        escapar hacía que envolver esta función en ``_fs_op_with_retry`` (como
        hace ``_dir_rollback.__aenter__``) quemara los 5 reintentos con backoff
        y fallara sobre la entrada más común y correcta que hay.
        """
        assert link_kind_or_raise(tmp_path / "no-existe") is None

    @symlink_guard
    def test_symlink_a_directorio(self, tmp_path: pathlib.Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        enlace = tmp_path / "enlace"
        enlace.symlink_to(real, target_is_directory=True)

        assert link_kind(enlace) == "symlink"
        assert is_link(enlace) is True

    @symlink_guard
    def test_symlink_roto_sigue_siendo_un_enlace(self, tmp_path: pathlib.Path) -> None:
        """El caso que ``exists()`` no puede ver: el enlace está, su destino no.

        Es el modo de falla que hacía que ``DirectoryRollback.__aenter__`` tomara
        el camino "primer run, nada que preservar" y después el ``mkdir()`` de la
        herramienta muriera con un ``FileExistsError`` incomprensible.
        """
        roto = tmp_path / "roto"
        roto.symlink_to(tmp_path / "jamas-existio", target_is_directory=True)

        assert roto.exists() is False  # el motivo por el que hace falta esta primitiva
        assert link_kind(roto) == "symlink"
        assert is_link(roto) is True

    @junction_guard
    def test_junction_roto_sigue_siendo_un_enlace(self, tmp_path: pathlib.Path) -> None:
        """El mismo caso que el symlink roto, pero para el reparse tag de 3.11.

        El guard de ``st_reparse_tag`` estaba condicionado a ``path.exists()``,
        que **sigue** el enlace: para un junction cuyo destino se borró, eso
        cortocircuitaba antes de leer el ``lstat`` y ``link_kind`` devolvía
        ``None``. Es el mismo modo de falla que el test de arriba cubre para
        symlinks, sin cubrir para junctions — hasta este test.
        """
        destino = tmp_path / "destino"
        destino.mkdir()
        enlace = tmp_path / "enlace"
        motivo = crear_junction(enlace, destino)
        assert motivo is None, motivo
        destino.rmdir()

        assert enlace.exists() is False  # el motivo por el que hace falta esta primitiva
        assert link_kind(enlace) == "junction"
        assert is_link(enlace) is True

    def test_reparse_tag_no_mount_point_no_es_un_junction(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un ``st_reparse_tag`` distinto de cero no es necesariamente un junction.

        OneDrive Files On-Demand usa ``IO_REPARSE_TAG_CLOUD`` (``0x9000001A``);
        NTFS con deduplicación/HSM tiene los suyos. El ``bool()`` original trataba
        CUALQUIER tag no nulo como junction — bastaba tener el árbol sincronizado
        con OneDrive para que ``DirectoryRollback``/``ProfileSandbox`` abortaran
        con "es un enlace" sobre una carpeta real (review qodo-merge #404).

        No depende de crear un reparse point real (no hay forma portable de
        producir uno que no sea junction/symlink): se simula el ``lstat`` para
        ejercer la rama de comparación sin acoplarse a una plataforma.
        """
        real = tmp_path / "real"
        real.mkdir()
        st_mode_real = real.lstat().st_mode  # antes de parchear: modo de directorio real

        class _LstatConTagAjeno:
            st_mode = st_mode_real  # para que is_symlink() (que también lee lstat) siga viendo un directorio
            st_reparse_tag = 0x9000001A  # IO_REPARSE_TAG_CLOUD, no MOUNT_POINT

        monkeypatch.setattr(pathlib.Path, "lstat", lambda self: _LstatConTagAjeno())

        assert link_kind(real) is None
        assert is_link(real) is False


class TestPathPresent:
    """``path_present`` = "hay algo acá", incluido un enlace roto."""

    def test_directorio_real_esta_presente(self, tmp_path: pathlib.Path) -> None:
        real = tmp_path / "real"
        real.mkdir()

        assert path_present(real) is True

    def test_ruta_inexistente_no_esta_presente(self, tmp_path: pathlib.Path) -> None:
        assert path_present(tmp_path / "no-existe") is False

    @symlink_guard
    def test_symlink_roto_esta_presente_aunque_exists_diga_que_no(self, tmp_path: pathlib.Path) -> None:
        """La razón de ser de la función: ``exists()`` dice False y ``mkdir()``
        falla con ``FileExistsError``. Las dos no pueden ser ciertas a la vez para
        quien tiene que decidir si el camino está libre."""
        roto = tmp_path / "roto"
        roto.symlink_to(tmp_path / "jamas-existio", target_is_directory=True)

        assert roto.exists() is False
        assert path_present(roto) is True

    @junction_guard
    def test_junction_roto_esta_presente_aunque_exists_diga_que_no(self, tmp_path: pathlib.Path) -> None:
        """El mismo caso que el symlink roto, sobre un junction sin destino.

        Es el escenario que ``DirectoryRollback.__aenter__`` interpretaba como
        "primer run, nada que preservar" en 3.11: ``path_present`` decía
        ``False`` y el ``mkdir()`` posterior de la herramienta moría con un
        ``FileExistsError`` que no menciona enlaces.
        """
        destino = tmp_path / "destino"
        destino.mkdir()
        enlace = tmp_path / "enlace"
        motivo = crear_junction(enlace, destino)
        assert motivo is None, motivo
        destino.rmdir()

        assert enlace.exists() is False
        assert path_present(enlace) is True


def test_la_deteccion_de_junctions_vive_en_un_solo_modulo() -> None:
    """Ancla de no-triplicación: **enumera** quién implementa la detección.

    La detección de junctions es la parte que se equivoca sola: exige conocer que
    ``islink()`` no los ve, que ``is_junction`` existe recién en 3.12 y que en 3.11
    hay que leer ``st_reparse_tag``. Estaba escrita dos veces cuando este módulo
    nació; una tercera copia (o una cuarta) rompe este test hasta que delegue.

    Se afirma sobre igualdad literal —no sobre "está en algún lado"— por el mismo
    motivo que ``RITUAL_TOOL_MAP == {...}`` en ``tests/test_ritual_dispatch.py``:
    un ancla que sólo verifica presencia no detecta la copia nueva.

    **Se afirma sobre el AST y no sobre un grep** (mismo criterio que
    ``test_rollback_reconciler.py::test_los_reconciliadores_estan_invocados_en_el_arranque``):
    con substrings, este mismo test se marcaba en rojo por un COMENTARIO que
    explicaba la consolidación. Nombrar un marcador en prosa no es implementarlo;
    lo que importa es el acceso real, que en los dos casos se escribe como
    ``getattr(path, "is_junction", ...)`` / ``getattr(path.lstat(), "st_reparse_tag", ...)``
    porque ninguno de los dos existe en todas las plataformas/versiones.
    """
    paquete = pathlib.Path(__file__).resolve().parent.parent / "sky_claw"
    marcadores = {"is_junction", "st_reparse_tag"}

    def accede_a_un_marcador(ruta: pathlib.Path) -> bool:
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            # Acceso directo: ``path.is_junction()``.
            if isinstance(nodo, ast.Attribute) and nodo.attr in marcadores:
                return True
            # Acceso condicional: ``getattr(obj, "is_junction", default)``.
            if (
                isinstance(nodo, ast.Call)
                and isinstance(nodo.func, ast.Name)
                and nodo.func.id == "getattr"
                and len(nodo.args) >= 2
                and isinstance(nodo.args[1], ast.Constant)
                and nodo.args[1].value in marcadores
            ):
                return True
        return False

    implementan = {
        ruta.relative_to(paquete.parent).as_posix() for ruta in paquete.rglob("*.py") if accede_a_un_marcador(ruta)
    }

    assert implementan == {"sky_claw/app/security/links.py"}


#: Módulos que deciden **borrar o preservar** un artefacto de rollback, y por qué
#: cada uno tiene que consultar la primitiva de enlaces.
#:
#: La familia se enumera y se congela: agregar un módulo que toque estos artefactos
#: sin delegar en ``links`` rompe el ancla de abajo. Es la contracara de
#: ``PRODUCTORES_DEL_NOMBRE`` en ``tests/test_rollback_reconciler.py`` — allá se
#: enumera quién **produce** el residuo, acá quién decide **destruirlo**.
DECIDEN_SOBRE_ARTEFACTOS_DE_ROLLBACK: dict[str, str] = {
    "sky_claw/local/tools/_dir_rollback.py": (
        "move-aside: renombra el target y borra el backup. Sobre un enlace el "
        "rename mueve el enlace y el rmtree lanza (symlink) o atraviesa (junction)."
    ),
    "sky_claw/local/tools/rollback_reconciler.py": (
        "reconcilia al arranque lo que encuentra en disco, SIN el fail-closed de "
        "__aenter__: es el único camino por el que un artefacto enlazado llega a "
        "una operación destructiva."
    ),
    "sky_claw/local/mo2/profile_sandbox.py": (
        "produce el 'rollback-*' del promote y descarta clones con _rmtree_force. "
        "Su fail-closed ya existía pero con is_symlink(), ciego a junctions."
    ),
}


def test_todo_decisor_sobre_artefactos_de_rollback_consulta_la_primitiva() -> None:
    """Ancla de familia: **enumera** los decisores, no muestrea uno.

    El invariante que fija, como propiedad del mecanismo: *decidir borrar o
    preservar un artefacto de rollback exige mirar el ENLACE, no su destino* —
    porque ``shutil.rmtree`` atraviesa un junction y ``Path.exists()`` sigue todo.

    Se verifica por el **import real** del módulo compartido y no por un
    ``is_symlink()`` cualquiera: escribir la detección a mano es exactamente lo que
    dejó dos copias divergentes y un tercer sitio sin cubrir. Un decisor nuevo
    rompe acá hasta que delegue.
    """
    paquete = pathlib.Path(__file__).resolve().parent.parent / "sky_claw"

    def importa_la_primitiva(ruta: pathlib.Path) -> bool:
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        return any(
            isinstance(nodo, ast.ImportFrom) and nodo.module == "sky_claw.app.security.links"
            for nodo in ast.walk(arbol)
        )

    for relativa, motivo in DECIDEN_SOBRE_ARTEFACTOS_DE_ROLLBACK.items():
        ruta = paquete.parent / relativa
        assert ruta.is_file(), f"{relativa} ya no existe; actualizá la familia"
        assert importa_la_primitiva(ruta), f"{relativa} no delega en links: {motivo}"
