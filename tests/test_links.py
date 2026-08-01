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
import os
import pathlib
from types import SimpleNamespace

import pytest

import sky_claw.app.security.links as links
from sky_claw.app.security.links import (
    is_link,
    link_kind,
    link_kind_or_raise,
    path_present,
    rmtree_link_aware,
)
from tests._symlink_guard import crear_junction, is_junction_real, junction_guard, symlink_guard


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

    def test_detector_fail_closed_reintenta_un_error_transitorio(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un guard destructivo no confunde un fallo de inspección con "ruta real"."""
        detector = links.link_kind_or_raise_with_retry
        objetivo = tmp_path / "real"
        objetivo.mkdir()
        lstat_real = pathlib.Path.lstat
        intentos = 0

        def _lstat_transitorio(self: pathlib.Path) -> os.stat_result:
            nonlocal intentos
            if self == objetivo:
                intentos += 1
                if intentos < 3:
                    raise PermissionError("WinError 5: bloqueo transitorio del indexer")
            return lstat_real(self)

        monkeypatch.setattr(pathlib.Path, "lstat", _lstat_transitorio)

        assert detector(objetivo) is None
        assert intentos == 3

    def test_inspeccion_canonica_devuelve_clasificacion_e_identidad_no_follow(self, tmp_path: pathlib.Path) -> None:
        objetivo = tmp_path / "real"
        objetivo.mkdir()

        tipo, identidad = links.link_kind_and_identity_or_raise_with_retry(objetivo)

        assert tipo is None
        assert identidad is not None
        assert identidad.st_mode == objetivo.lstat().st_mode

    def test_inspeccion_canonica_reintenta_lstat_transitorio(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        objetivo = tmp_path / "real"
        objetivo.mkdir()
        lstat_real = pathlib.Path.lstat
        intentos = 0

        def _lstat_transitorio(self: pathlib.Path) -> os.stat_result:
            nonlocal intentos
            if self == objetivo:
                intentos += 1
                if intentos < 3:
                    raise PermissionError("WinError 5: bloqueo transitorio del indexer")
            return lstat_real(self)

        monkeypatch.setattr(pathlib.Path, "lstat", _lstat_transitorio)

        tipo, identidad = links.link_kind_and_identity_or_raise_with_retry(objetivo)

        assert tipo is None
        assert identidad is not None
        assert intentos == 3

    def test_comparador_canonico_detecta_reemplazo_de_entrada(self, tmp_path: pathlib.Path) -> None:
        primero = tmp_path / "primero"
        segundo = tmp_path / "segundo"
        primero.write_text("uno", encoding="utf-8")
        segundo.write_text("dos", encoding="utf-8")
        _, identidad_primera = links.link_kind_and_identity_or_raise(primero)
        _, identidad_segunda = links.link_kind_and_identity_or_raise(segundo)

        assert identidad_primera is not None
        assert identidad_segunda is not None
        assert links.same_file_identity(identidad_primera, identidad_primera) is True
        assert links.same_file_identity(identidad_primera, identidad_segunda) is False

    def test_comparador_de_direntry_usa_inode_no_follow(self, tmp_path: pathlib.Path) -> None:
        hijo = tmp_path / "hijo"
        hijo.mkdir()
        with os.scandir(tmp_path) as entradas:
            entrada = next(entradas)
            capturada = entrada.stat(follow_symlinks=False)
            inode_capturado = entrada.inode()
        _, observada = links.link_kind_and_identity_or_raise(hijo)

        assert links.same_direntry_identity(capturada, inode_capturado, observada) is True

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


class TestRmtreeLinkAware:
    """El borrado recursivo que no atraviesa enlaces — reemplazo de ``shutil.rmtree``.

    El caso que motiva la primitiva no es el enlace en la RAÍZ (que ``rmtree`` al
    menos rechaza cuando es un symlink) sino el **anidado**: su recorrido interno
    usa ``DirEntry.is_dir()``, ciego al reparse point, así que un árbol por lo
    demás propio con un junction adentro se atraviesa entero y borra el destino
    ajeno sin lanzar nada.
    """

    def test_borra_un_arbol_real_completo(self, tmp_path: pathlib.Path) -> None:
        raiz = tmp_path / "arbol"
        (raiz / "sub" / "hondo").mkdir(parents=True)
        (raiz / "suelto.txt").write_text("x", encoding="utf-8")
        (raiz / "sub" / "hondo" / "dato.bin").write_bytes(b"y")

        rmtree_link_aware(raiz)

        assert not raiz.exists()

    def test_un_arbol_mas_profundo_que_el_limite_de_python_se_borra_en_postorden(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El recorrido no depende de la pila de Python para cada directorio."""
        profundidad = 1_200
        eliminados: list[int] = []

        class _StatDirectorio:
            st_mode = 0o040755
            st_dev = 1
            st_reparse_tag = 0

            def __init__(self, inode: int) -> None:
                self.st_ino = inode

        class _RutaVirtual:
            def __init__(self, valor: str | int) -> None:
                self.nivel = int(valor)

            def lstat(self) -> _StatDirectorio:
                return _StatDirectorio(self.nivel)

            def rmdir(self) -> None:
                eliminados.append(self.nivel)

            def unlink(self) -> None:
                raise AssertionError("el árbol virtual sólo contiene directorios")

            def __str__(self) -> str:
                return str(self.nivel)

        class _ScandirVirtual:
            def __init__(self, ruta: _RutaVirtual) -> None:
                self._ruta = ruta

            def __enter__(self):
                if self._ruta.nivel == profundidad:
                    return iter(())
                return iter((SimpleNamespace(path=str(self._ruta.nivel + 1)),))

            def __exit__(self, *_args: object) -> None:
                return None

        monkeypatch.setattr(links, "pathlib", SimpleNamespace(Path=_RutaVirtual))
        monkeypatch.setattr(links, "os", SimpleNamespace(scandir=_ScandirVirtual))

        links._borrar_recursivo(_RutaVirtual(0), limpiar_readonly=False)

        assert eliminados == list(range(profundidad, -1, -1))

    @junction_guard
    def test_revalida_la_identidad_antes_de_descender(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un swap entre inspección y ``scandir`` aborta sin tocar el destino."""
        ajeno = tmp_path / "arbol_ajeno"
        ajeno.mkdir()
        importante = ajeno / "importante.txt"
        importante.write_text("no borrar", encoding="utf-8")

        raiz = tmp_path / "arbol"
        victima = raiz / "victima"
        victima.mkdir(parents=True)
        (victima / "propio.txt").write_text("borrable", encoding="utf-8")
        preservado = tmp_path / "victima_original"
        scandir_real = os.scandir
        intercambio_hecho = False

        def _scandir_con_intercambio(path: os.PathLike[str] | str | int):
            nonlocal intercambio_hecho
            if pathlib.Path(path) == victima and not intercambio_hecho:
                intercambio_hecho = True
                victima.rename(preservado)
                motivo = crear_junction(victima, ajeno)
                assert motivo is None, motivo
            return scandir_real(path)

        monkeypatch.setattr(os, "scandir", _scandir_con_intercambio)

        with pytest.raises(OSError, match="cambi"):
            rmtree_link_aware(raiz)

        assert intercambio_hecho
        assert importante.read_text(encoding="utf-8") == "no borrar"

    def test_es_no_op_si_la_ruta_no_existe(self, tmp_path: pathlib.Path) -> None:
        """Borrar lo que ya no está cumplió el objetivo; no es un error.

        Lo necesitan los callers que hoy hacen ``if x.exists(): _rmtree_force(x)``
        y los que confían en el no-op interno que tenía una de las dos copias.
        """
        rmtree_link_aware(tmp_path / "jamas-existio")

    @symlink_guard
    def test_un_symlink_anidado_se_desenlaza_sin_tocar_el_destino(self, tmp_path: pathlib.Path) -> None:
        """Ancla de regresión, **no** prueba del fix — verificado, no supuesto.

        ``shutil.rmtree`` ya trata bien el symlink anidado: su recorrido usa
        ``entry.is_dir(follow_symlinks=False)``, así que lo desenlaza en vez de
        atravesarlo (comprobado ejecutándolo: raíz borrada, destino intacto).
        Este test pasaría igual con el ``rmtree`` crudo. Existe para que el
        reemplazo hecho a mano no regresione el caso que ya andaba, y para dar
        cobertura real al recorrido nuevo en el runner de Linux. El caso que de
        verdad pierde datos es el junction de abajo, y sólo Windows lo ejerce.
        """
        ajeno = tmp_path / "arbol_ajeno"
        ajeno.mkdir()
        (ajeno / "importante.txt").write_text("no borrar", encoding="utf-8")

        raiz = tmp_path / "arbol"
        (raiz / "propio").mkdir(parents=True)
        (raiz / "propio" / "mio.txt").write_text("borrable", encoding="utf-8")
        (raiz / "enlace").symlink_to(ajeno, target_is_directory=True)

        rmtree_link_aware(raiz)

        assert not raiz.exists()
        assert (ajeno / "importante.txt").read_text(encoding="utf-8") == "no borrar"

    @junction_guard
    def test_un_junction_anidado_se_desenlaza_sin_tocar_el_destino(self, tmp_path: pathlib.Path) -> None:
        """El que de verdad perdía datos, y sólo Windows puede ejercer.

        ``shutil.rmtree`` acá no lanza: entra por ``scandir`` al destino, borra
        su contenido y recién después quita el reparse point.
        """
        ajeno = tmp_path / "texturas_en_otro_disco"
        ajeno.mkdir()
        (ajeno / "importante.dds").write_bytes(b"no borrar")

        raiz = tmp_path / "arbol"
        (raiz / "propio").mkdir(parents=True)
        if (motivo := crear_junction(raiz / "textures", ajeno)) is not None:
            pytest.fail(f"no se pudo crear el junction que este test existe para ejercer: {motivo}")

        rmtree_link_aware(raiz)

        assert not raiz.exists()
        assert (ajeno / "importante.dds").read_bytes() == b"no borrar"

    @symlink_guard
    def test_un_symlink_en_la_raiz_borra_solo_el_enlace(self, tmp_path: pathlib.Path) -> None:
        ajeno = tmp_path / "arbol_ajeno"
        ajeno.mkdir()
        (ajeno / "importante.txt").write_text("no borrar", encoding="utf-8")
        enlace = tmp_path / "enlace"
        enlace.symlink_to(ajeno, target_is_directory=True)

        rmtree_link_aware(enlace)

        assert not enlace.is_symlink()
        assert (ajeno / "importante.txt").read_text(encoding="utf-8") == "no borrar"

    @junction_guard
    def test_un_junction_en_la_raiz_borra_solo_el_reparse_point(self, tmp_path: pathlib.Path) -> None:
        ajeno = tmp_path / "arbol_ajeno"
        ajeno.mkdir()
        (ajeno / "importante.dds").write_bytes(b"no borrar")
        enlace = tmp_path / "enlace"
        if (motivo := crear_junction(enlace, ajeno)) is not None:
            pytest.fail(f"no se pudo crear el junction que este test existe para ejercer: {motivo}")
        assert is_junction_real(enlace), "el enlace creado no es un junction invisible a islink()"

        rmtree_link_aware(enlace)

        assert not enlace.exists()
        assert (ajeno / "importante.dds").read_bytes() == b"no borrar"

    @symlink_guard
    def test_un_enlace_roto_en_la_raiz_se_borra_igual(self, tmp_path: pathlib.Path) -> None:
        """La ausencia se mide con ``path_present``, no con ``exists()``.

        Con ``exists()`` un enlace colgante se tomaba por "no hay nada acá" y el
        borrado era un no-op silencioso que dejaba la ruta ocupada.
        """
        roto = tmp_path / "roto"
        roto.symlink_to(tmp_path / "jamas-existio", target_is_directory=True)
        assert roto.exists() is False

        rmtree_link_aware(roto)

        assert not roto.is_symlink()

    def test_sin_limpiar_readonly_el_error_se_propaga(self, tmp_path: pathlib.Path) -> None:
        """El default no toca permisos: quien no lo pidió ve el ``OSError``.

        Se simula el fallo en vez de depender de que el FS del runner respete el
        bit de sólo-lectura (en POSIX con uid 0 no lo hace).
        """
        raiz = tmp_path / "arbol"
        raiz.mkdir()
        objetivo = raiz / "porfiado.txt"
        objetivo.write_text("x", encoding="utf-8")

        unlink_real = pathlib.Path.unlink
        chmods: list[pathlib.Path] = []

        def _unlink_que_falla(self: pathlib.Path, missing_ok: bool = False) -> None:
            if self == objetivo:
                raise PermissionError("WinError 5: acceso denegado")
            unlink_real(self, missing_ok=missing_ok)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pathlib.Path, "unlink", _unlink_que_falla)
            mp.setattr("sky_claw.app.security.links._sumar_bit_de_escritura", chmods.append)
            with pytest.raises(PermissionError):
                rmtree_link_aware(raiz)

        assert chmods == [], "no se pidió limpiar_readonly; no debía tocarse ningún permiso"

    def test_con_limpiar_readonly_reintenta_tras_sumar_el_bit(self, tmp_path: pathlib.Path) -> None:
        """El reintento es POR ENTRADA y sólo ante el fallo.

        Las dos copias de ``_rmtree_force`` lo hacían con un ``os.walk`` previo
        sobre todo el árbol — y ese walk desciende por junctions, así que
        chmodeaba el árbol ajeno antes de borrarlo.
        """
        raiz = tmp_path / "arbol"
        raiz.mkdir()
        objetivo = raiz / "porfiado.txt"
        objetivo.write_text("x", encoding="utf-8")

        unlink_real = pathlib.Path.unlink
        fallas = {"n": 0}
        chmods: list[pathlib.Path] = []

        def _unlink_que_falla_una_vez(self: pathlib.Path, missing_ok: bool = False) -> None:
            if self == objetivo and fallas["n"] == 0:
                fallas["n"] += 1
                raise PermissionError("WinError 5: acceso denegado")
            unlink_real(self, missing_ok=missing_ok)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pathlib.Path, "unlink", _unlink_que_falla_una_vez)
            mp.setattr("sky_claw.app.security.links._sumar_bit_de_escritura", chmods.append)
            rmtree_link_aware(raiz, limpiar_readonly=True)

        assert chmods == [objetivo], "el chmod debe alcanzar sólo a la entrada que falló"
        assert not raiz.exists()


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
