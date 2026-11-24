"""Siembra fail-closed del ``Config.xml`` de BodySlide — precondición de un build headless.

``BodySlideApp::OnInit`` abre dos ``wxMessageBox`` MODALES antes de construir
nada, y **ninguno de los dos mira la línea de comandos**:

- línea 243 — ``GetOutputDataPath()`` no legible/escribible. Esa función
  (``BodySlideAppOutfits.cpp``) devuelve ``Config["OutputDataPath"]`` o, si está
  vacío, ``Config["GameDataPath"]``: el ``-t`` que le pasamos NO participa.
- línea 2798 — TargetGame ausente y ``GameDataPath`` vacío (el modal de
  configuración: ``SetDefaultConfig`` muestra ``ShowSetup`` cuando
  ``targetGame == -1``, sin ninguna comprobación de ``cmdQuiet`` — el modal
  bloquea un proceso headless).

Un modal en un proceso headless no se puede cerrar: ``run_capture`` espera a
``communicate()`` hasta agotar el timeout y recién ahí mata el árbol —
posiblemente a mitad de escritura. ``CREATE_NO_WINDOW`` no cubre esto: suprime
consolas, no ventanas GUI.

**Merge, nunca sobrescritura.** El ``Config.xml`` es del usuario: trae listas
negras de BSA por juego, luces, límites de sliders y su ``ProjectPath``. Este
módulo toca exactamente las claves que necesita y deja el resto intacto.

**Fail-closed (PR 1).** La configuración es precondición, no mejora best-effort:
si el XML está corrupto, si contiene entidades/DTD no permitidos, si el target
de destino no es escribible o si ``TargetGame`` no está configurado y es válido
(``-1`` o ausente), la siembra **lanza** ``BodySlideConfigPreconditionError`` y
el runner no lanza el spawn. Motivo de la migración desde el contrato
best-effort: un modal de setup o un fallback a ``AppDir`` (``BuildListBodies``
escribe ``GameDataPath=<AppDir>`` en ``Config.xml`` — upstream, confirmado) son
más peligrosos que un abort legible.

``TargetGame`` NO se semea: Sky-Claw no adivina el juego del usuario. El valor
real lo puso BodySlide (o el usuario) al configurar la herramienta la primera
vez; si no existe o está fuera del rango válido (0..9), el build se aborta con el
motivo legible ANTES del spawn. Cuando el caller declara el juego objetivo
(``expected_target_game``), ``TargetGame`` debe coincidir con él EXACTAMENTE —
um enum válido del juego equivocado NO es precondición válida (P1-5).

``ProjectPath`` queda deliberadamente FUERA del conjunto sembrado: es la
respuesta a "dónde están los ``SliderSets``", que bajo MO2 sólo la puede dar la
USVFS. Decidirla acá sería fijar en config una respuesta que corresponde a la
capa de VFS.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import pathlib
import uuid
import xml.etree.ElementTree as ET

import defusedxml.ElementTree as DefusedElementTree
from defusedxml import (
    DefusedXmlException,
    DTDForbidden,
    EntitiesForbidden,
    ExternalReferenceForbidden,
)

logger = logging.getLogger("SkyClaw.BodySlideConfig")

#: Nombre del archivo que ``BodySlideApp::OnInit`` (línea 65, ``Config.LoadConfig``)
#: carga desde el directorio del ejecutable.
NOMBRE_DEL_CONFIG = "Config.xml"

#: Rangos válidos de TargetGame en el upstream ``src/utils/TargetGame.h``
#: (FO3=0 … SF=9, en el orden de ``GameUtil::TargetGames``).
_TARGET_GAME_MIN = 0
_TARGET_GAME_MAX = 9

#: LogLevel=3 — ``src/utils/Log.cpp``: -1 Off, 0 wxLogFatalError, 1 wxLogError,
#: 2 wxLogWarning, 3 wxLogMessage. Es el máximo, y el único que expone
#: ``wxLogMessage`` (p. ej. "process started", "Command-line build applied saved
#: BuildSelection…") — la visibilidad de errores relevante para el post-check.
_LOG_LEVEL_DEL_SEMBRADOR = "3"


class BodySlideConfigPreconditionError(Exception):
    """La configuración de BodySlide no puede ser precondición de un build headless.

    La causa es siempre legible en el mensaje (qué clave está ausente/inválida,
    si el XML no se parseó, etc.); el caller (el runner) la traduce a
    ``success=False`` sin spawnear el proceso.
    """


def sembrar_config_de_bodyslide(
    *,
    exe_dir: pathlib.Path,
    game_path: pathlib.Path,
    output_root: pathlib.Path,
    expected_target_game: int | None = None,
) -> bool:
    """Aplica las claves que hacen posible un build headless. Devuelve True.

    Es :func:`calcular_config_sembrada` más la escritura atómica: los bytes que se
    escriben son EXACTAMENTE los que devuelve el cálculo (el journal de PR 2b
    registra su hash antes de escribir).

    Raises:
        BodySlideConfigPreconditionError: ver :func:`calcular_config_sembrada`, o si
            la escritura falla.
    """
    blob = calcular_config_sembrada(
        exe_dir=exe_dir,
        game_path=game_path,
        output_root=output_root,
        expected_target_game=expected_target_game,
    )
    config = exe_dir / NOMBRE_DEL_CONFIG
    # Escritura ATÓMICA: tmp en el MISMO directorio (misma unidad → rename real)
    # y ``os.replace`` recién cuando el árbol se serializó entero. Escribir
    # directo sobre el destino lo trunca si el ``write`` muere a mitad —disco
    # lleno, AV que toma el handle—, y eso destruiría la config del usuario para
    # arreglar un modal. Mismo patrón que ``mo2.ini_editor._write_atomic``.
    tmp = config.with_name(f"{config.name}.{uuid.uuid4().hex}.tmp")
    try:
        _escribir_temporal(tmp, blob)
        os.replace(tmp, config)
    except OSError as exc:
        raise BodySlideConfigPreconditionError(f"Config.xml ilegible en '{config}' ({exc})") from exc
    finally:
        # Best-effort: un error de unlink acá no debe enmascarar la causa raíz.
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
    return True


def _escribir_temporal(tmp: pathlib.Path, blob: bytes) -> None:
    """Escribe el temporal de la siembra (punto único: los tests simulan acá un fallo a mitad)."""
    tmp.write_bytes(blob)


def calcular_config_sembrada(
    *,
    exe_dir: pathlib.Path,
    game_path: pathlib.Path,
    output_root: pathlib.Path,
    expected_target_game: int | None = None,
) -> bytes:
    """Bytes del ``Config.xml`` sembrado, SIN escribir nada. Determinista sobre el original.

    Raises:
        BodySlideConfigPreconditionError: si el XML está corrupto o contiene
            entidades/DTD/referencias externas, si ``GameDataPath`` no es escribible
            o ``TargetGame`` no existe o no es un entero válido en [0, 9].
            Adicionalmente, cuando ``expected_target_game`` está seteado,
            ``TargetGame`` DEBE coincidir con él — un enum válido pero del juego
            equivocado es un fallo de pre-condición, no una coincidencia.
    """
    config = exe_dir / NOMBRE_DEL_CONFIG

    # P0-B: Config.xml enlazado — la siembra escribe por clave a través del
    # enlace al destino externo. Sin verificación de destino estable, un enlace
    # re-targeted durante la corrida sobrescribiría otro archivo (P0-B, auditoría).
    # Fail-closed: no se toca ni se siembra nada; el runner aborta antes del spawn.
    if config.is_symlink():
        try:
            destino = os.readlink(config)
        except OSError:
            destino = "?"
        raise BodySlideConfigPreconditionError(
            f"Config.xml es un enlace ('{destino}') — la siembra fail-closed no "
            "escribe ni siembra a través de enlaces (P0-B); desenlaza el archivo "
            "antes de un build headless"
        )

    valores = {
        # Sin esto BodySlide resuelve el Data por clave de registro y, si falla,
        # abre el modal de la línea 2798.
        "GameDataPath": str((game_path / "Data").resolve()),
        # Lo que apaga el chequeo de escritura de la línea 243: apunta la ruta que
        # BodySlide considera "su salida" al subárbol administrado.
        "OutputDataPath": str(output_root.resolve()),
        # Se fuerza a "false" para apagar el "Continue saving files to the working
        # directory?" de BuildListBodies.
        "WarnMissingGamePath": "false",
        # LogLevel=3: visibilidad máxima (wxLogMessage) para que
        # "Failed to build …" / "applied saved BuildSelection …" queden en el log.
        "LogLevel": _LOG_LEVEL_DEL_SEMBRADOR,
    }

    try:
        raiz = _cargar_o_crear(config)
    except (DTDForbidden, EntitiesForbidden, ExternalReferenceForbidden) as exc:
        # Mismo criterio que ``fomod.parser``: un DTD/entidad/referencia externa
        # es una inyección, no un archivo mal formado. Aborta el build headless:
        # BodySlide no debe correr con una configuración hostil.
        raise BodySlideConfigPreconditionError(
            f"Config.xml con entidades/referencias peligrosas rechazado: {config}: {exc}"
        ) from exc
    except (DefusedElementTree.ParseError, DefusedXmlException, OSError) as exc:
        raise BodySlideConfigPreconditionError(f"Config.xml ilegible en '{config}': {exc}") from exc

    _exigir_config_inequivoca(raiz, config, claves=("TargetGame", *valores))
    _validar_target_game(raiz, config, expected_target_game=expected_target_game)

    for clave, valor in valores.items():
        # ``find`` toma el PRIMER nodo, que es el mismo que lee
        # ``ConfigurationManager``: reusarlo (en vez de append) es lo que hace a
        # esta función idempotente y evita que un duplicado con el valor viejo
        # gane en silencio.
        nodo = raiz.find(clave)
        if nodo is None:
            nodo = ET.SubElement(raiz, clave)
        nodo.text = valor

    # Misma serialización que escribía la siembra (``ElementTree.write`` a archivo):
    # cambiarla cambiaría los bytes y el hash que el journal registra.
    salida = io.BytesIO()
    ET.ElementTree(raiz).write(salida, encoding="utf-8", xml_declaration=False)
    return salida.getvalue()


def _exigir_config_inequivoca(raiz: ET.Element, config: pathlib.Path, *, claves: tuple[str, ...]) -> None:
    """Exige que BodySlide lea el MISMO valor que valida y siembra Sky-Claw (review CodeRabbit #1003).

    Upstream v6.2.0 (``ConfigurationManager.cpp``): ``LoadConfig`` sólo carga la
    raíz ``Config``; ``FindCI`` compara nombres SIN distinguir mayúsculas y se
    queda con la ÚLTIMA coincidencia. Una raíz distinta, una clave duplicada o
    una clave con otra grafía harían que BodySlide use un ``TargetGame`` o un
    ``LogLevel`` distinto del validado/sembrado (otro juego, o un log sin el
    marcador de conflicto). Fail-closed: se rechaza antes de mutar.
    """
    if raiz.tag != "Config":
        raise BodySlideConfigPreconditionError(
            f"Config.xml en '{config}' tiene raíz <{raiz.tag}>: BodySlide sólo carga la raíz <Config>"
        )
    for clave in claves:
        nodos = [hijo.tag for hijo in raiz if isinstance(hijo.tag, str) and hijo.tag.casefold() == clave.casefold()]
        if len(nodos) > 1 or (nodos and nodos[0] != clave):
            raise BodySlideConfigPreconditionError(
                f"{clave} ambiguo en '{config}' ({', '.join(nodos)}): BodySlide no distingue mayúsculas y usa "
                "la última coincidencia — se exige una sola clave con su grafía canónica"
            )


def _validar_target_game(
    raiz: ET.Element,
    config: pathlib.Path,
    *,
    expected_target_game: int | None = None,
) -> None:
    """Exige un TargetGame presente, válido y DEL JUEGO EXPECTADO.

    Fail-closed: abortar ANTES de cualquier mutación (órden transaccional de P0-1).

    Upstream (``SetDefaultConfig``): ``currentTarget == -1`` → ``ShowSetup()``
    (modal ¡sin gate de cmdQuiet!). En headless el modal es un hang duro, y
    Sky-Claw no adivina el juego — tampoco se atreve a setearlo.

    ``expected_target_game`` (P1-5, auditoría): un TargetGame válido pero del
    juego equivocado NO es válido para este build. Si el caller declara el juego
    objetivo, la configuración debe coincidir exactamente con él.
    """
    nodo = raiz.find("TargetGame")
    # Se valida el texto que se CONSERVA en Config.xml, no una copia normalizada:
    # ``str.strip()`` quita espacios Unicode (U+00A0) que ``atoi`` no salta.
    crudo = (nodo.text or "") if nodo is not None else ""
    texto = crudo.strip(" \t\n\r\v\f")
    if not texto:
        raise BodySlideConfigPreconditionError(
            f"TargetGame no está configurado en '{config}' — BodySlide mostraría el modal de setup; "
            "configura el juego en el Config.xml antes de un build headless"
        )
    # BodySlide lo lee con ``atoi``: sólo dígitos ASCII significan lo mismo para
    # los dos (``int("４") == 4``, ``atoi("４") == 0``).
    if not crudo.isascii() or not texto.isdigit():
        raise BodySlideConfigPreconditionError(f"TargetGame no es un entero ASCII ('{texto}') en '{config}'")
    try:
        valor = int(texto)
    except ValueError:
        raise BodySlideConfigPreconditionError(f"TargetGame no es un entero válido ('{texto}') en '{config}'") from None
    if not (_TARGET_GAME_MIN <= valor <= _TARGET_GAME_MAX):
        raise BodySlideConfigPreconditionError(
            f"TargetGame={valor} fuera del rango válido [{_TARGET_GAME_MIN}, {_TARGET_GAME_MAX}] en '{config}'"
        )
    if expected_target_game is not None and valor != expected_target_game:
        raise BodySlideConfigPreconditionError(
            f"TargetGame={valor} en '{config}' no coincide con el juego objetivo esperado "
            f"({expected_target_game}) — abortando antes del spawn"
        )


def _cargar_o_crear(config: pathlib.Path) -> ET.Element:
    """Devuelve la raíz del ``Config.xml`` existente, o una nueva si no hay.

    Un ``Config.xml`` ausente es el peor caso, no el trivial: es exactamente el
    estado en que ``GameDataPath`` queda vacío y BodySlide cae en la resolución
    por registro. El TargetGame ausente se valida DESPUÉS de la carga como
    precondición fail-closed (la siembra no inventa el juego).
    """
    if config.is_file():
        # ``defusedxml`` y no el ``ElementTree`` de la stdlib (Bandit B314): este
        # archivo vive junto al exe, en un directorio que Sky-Claw no controla del
        # todo —lo puede haber puesto un instalador de mods—, así que es entrada no
        # confiable. Mismo tratamiento que ``fomod.parser`` ya le da al
        # ``ModuleConfig.xml``.
        return DefusedElementTree.parse(
            config,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        ).getroot()
    return ET.Element("Config")
