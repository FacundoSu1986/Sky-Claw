"""Etapa 6 asistida — preparación y veredicto del Bashed Patch (ADR 0016).

**Por qué existe este módulo.** ``Wrye Bash`` no expone build headless del Bashed Patch:
``Mopy/bash/barg.py`` declara ``-b/--backup`` como *backup de settings* y **ningún** flag
reconstruye el parche (verificado contra el parser oficial; el detalle y la historia del
bug viven en :class:`sky_claw.local.tools.wrye_bash_runner.WryeBashHeadlessUnsupportedError`).
Hasta ADR 0016 eso dejaba la etapa 6 en un fail-closed honesto pero *caro*:
``WryeBashPipelineService.execute_pipeline`` tomaba los locks ``bashed-patch`` +
``load-order``, emitía el ``ActionManifest`` y abría una TX para un ritual que no puede
correr, y recién ahí el runner elevaba.

La decisión es modelar la etapa 6 como **asistida**, en dos fases explícitas:

1. :func:`preparar_etapa_asistida` — Sky-Claw dice QUÉ hacer y toma la huella PREVIA del
   artefacto. No muta nada y por eso no toma locks ni abre TX.
2. :func:`veredicto_etapa_asistida` — Sky-Claw verifica lo que el operador construyó en
   la GUI de Wrye Bash (lanzado desde MO2). Solo evidencia en disco.

**Propiedad central: el veredicto no se deriva de ningún exit code.** No hay subproceso
que consultar — este módulo no spawnea nada (anclado por AST en
``tests/test_wrye_bash_asistida.py``) —, así que el éxito se decide por evidencia
observable: el artefacto existe, es un archivo **regular** (no un symlink ni un junction),
no está vacío y está **habilitado** en el ``plugins.txt`` del perfil. Es la misma clase de
defecto que ``test_contrato_veredicto_de_exito.py`` enumera para el resto de los runners
—un veredicto apoyado en algo que no reporta el resultado del trabajo—, con la variante
"no hay código de salida que consultar".

**Fail-closed.** ``plugins.txt`` ilegible, artefacto ilegible o artefacto que es un enlace
nunca producen verde: lo consume el guard de la etapa 9
(:mod:`sky_claw.local.validators.etapas_previas_lods`) para decidir si lanza DynDOLOD, y
un falso verde ahí encadena stages sobre un parche que no existe. "No lo pude leer" no es
"está".

**Frescura, con límite declarado.** El veredicto prueba que el artefacto es de ESTA
corrida solo cuando el llamador trae la ``huella_previa`` de la fase 1; sin ella el
resultado es honesto (``frescura_probada=False``) en vez de deducir un cambio por
``mtime``. Comparar marcas de tiempo es justamente lo que el contrato de LOOT rechazó
(``CHANGED``/``NO_CHANGE`` por estado observable, no por ``mtime``). Los dos paths no
mienten en ninguna dirección: un parche viejo con huella previa idéntica es
``SIN_CAMBIO``, y un parche presente sin testigo previo es ``OK`` con la frescura
declarada como no probada.

**Límite de la detección de enlaces.** Es local y por ``lstat``, como
``artifact_digest``: best-effort ante mutación concurrente del filesystem, sin garantía
TOCTOU-fuerte entre la inspección y la apertura.
"""

from __future__ import annotations

import enum
import hashlib
import os
import pathlib
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sky_claw.local.loot.outcome import parse_load_order_bytes
from sky_claw.local.tools.wrye_bash_runner import BASHED_PATCH_NAME

if TYPE_CHECKING:
    from collections.abc import Callable

#: Chunk de lectura de la huella: el Bashed Patch puede llegar a decenas de MB en load
#: orders grandes y no se carga entero en memoria (mismo criterio que ``artifact_digest``).
_CHUNK = 1024 * 1024

#: ``FILE_ATTRIBUTE_REPARSE_POINT`` (Windows). Un junction no es un symlink para
#: ``os.path.islink``/``stat.S_ISLNK``, pero sí tiene este atributo: es la misma
#: ceguera que hubo que cerrar en el borrado recursivo (#404, #405) y en el conteo de
#: artefactos de BodySlide (#433).
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class EstadoEnPlugins(enum.StrEnum):
    """Estado del artefacto en el ``plugins.txt`` del perfil (vocabulario cerrado).

    El artefacto tiene que estar **habilitado**: un Bashed Patch construido pero
    deshabilitado no lo carga el motor y la etapa 9 lo ignora.
    """

    HABILITADO = "HABILITADO"
    DESHABILITADO = "DESHABILITADO"
    AUSENTE = "AUSENTE"
    ILEGIBLE = "ILEGIBLE"


class MotivoAsistido(enum.StrEnum):
    """Motivo del veredicto (vocabulario cerrado).

    ``ESPERANDO_OPERADOR`` no es un fallo: es la fase 1, que **nunca** declara éxito
    porque todavía no se construyó nada. ``OK`` es el único estado exitoso.
    """

    OK = "OK"
    ESPERANDO_OPERADOR = "ESPERANDO_OPERADOR"
    ARTEFACTO_AUSENTE = "ARTEFACTO_AUSENTE"
    ARTEFACTO_ENLACE = "ARTEFACTO_ENLACE"
    ARTEFACTO_ILEGIBLE = "ARTEFACTO_ILEGIBLE"
    ARTEFACTO_NO_REGULAR = "ARTEFACTO_NO_REGULAR"
    ARTEFACTO_VACIO = "ARTEFACTO_VACIO"
    PLUGINS_ILEGIBLE = "PLUGINS_ILEGIBLE"
    NO_HABILITADO = "NO_HABILITADO"
    SIN_CAMBIO = "SIN_CAMBIO"


@dataclass(frozen=True, slots=True)
class HuellaDeArtefacto:
    """Identidad observable del artefacto, sin seguir enlaces.

    ``sha256`` es ``None`` siempre que no se pueda afirmar identidad de CONTENIDO:
    artefacto ausente, enlace (no se sigue), no regular o ilegible. Que sea ``None``
    no significa "vacío" — para eso está ``bytes``.
    """

    ruta: str
    existe: bool
    es_enlace: bool
    es_regular: bool
    ilegible: bool
    bytes: int
    sha256: str | None


@dataclass(frozen=True, slots=True)
class PreparacionAsistida:
    """Fase 1: lo que el operador necesita para construir, más el testigo previo."""

    artefacto: str
    plugins_txt: str
    huella_previa: HuellaDeArtefacto
    estado_plugins: EstadoEnPlugins
    instrucciones: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VeredictoAsistido:
    """Fase 2: veredicto derivado de evidencia en disco (nunca de un exit code)."""

    success: bool
    motivo: MotivoAsistido
    artefacto: str
    huella: HuellaDeArtefacto
    estado_plugins: EstadoEnPlugins
    frescura_probada: bool


#: Pasos que Sky-Claw le da al operador. Nombran la herramienta, el artefacto y MO2
#: porque la salida del ritual la lee un humano, no un log (anclado por test).
PASOS_ASISTIDOS: tuple[str, ...] = (
    "1. Abrí MO2 y lanzá 'Wrye Bash' desde el ejecutable del perfil (no desde el explorador): "
    "así hereda la vista virtual del perfil.",
    "2. En la pestaña 'Mods', elegí 'Bashed Patch, 0.esp' y pulsá 'Rebuild Patch'.",
    f"3. Dejá SOLO 'Leveled Lists' habilitado: la etapa 6 no fusiona magia, clima ni "
    f"parámetros acústicos (SOP §2.6). El artefacto destino es '{BASHED_PATCH_NAME}'.",
    "4. Confirmá el rebuild, esperá a que termine y cerrá Wrye Bash.",
    "5. Verificá en MO2 que el plugin figure ACTIVO en el perfil; después volvé a Sky-Claw "
    "y pedí la verificación de la etapa 6.",
)


def _es_reparse_point(st: os.stat_result) -> bool:
    """True si el ``lstat`` marca un reparse point (junction en Windows)."""
    atributos = getattr(st, "st_file_attributes", 0)
    return bool(atributos & _FILE_ATTRIBUTE_REPARSE_POINT)


def huella_de_artefacto(ruta: pathlib.Path) -> HuellaDeArtefacto:
    """Huella observable de *ruta*: ``lstat`` primero, lectura acotada por chunks después.

    Nunca sigue un enlace (ni un junction): un enlace a un parche ajeno no puede
    validar nuestra corrida. Un ``OSError`` distinto de "no existe" marca
    ``ilegible`` y deja ``sha256`` en ``None`` — fail-closed, no "vacío".
    """
    try:
        st = os.lstat(ruta)
    except FileNotFoundError:
        return HuellaDeArtefacto(str(ruta), False, False, False, False, 0, None)
    except OSError:
        return HuellaDeArtefacto(str(ruta), True, False, False, True, 0, None)

    es_enlace = stat.S_ISLNK(st.st_mode) or _es_reparse_point(st)
    es_regular = stat.S_ISREG(st.st_mode) and not es_enlace
    if not es_regular:
        return HuellaDeArtefacto(str(ruta), True, es_enlace, False, False, 0, None)

    digest = hashlib.sha256()
    total = 0
    try:
        with ruta.open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                digest.update(chunk)
                total += len(chunk)
    except OSError:
        return HuellaDeArtefacto(str(ruta), True, False, True, True, 0, None)

    return HuellaDeArtefacto(str(ruta), True, False, True, False, total, digest.hexdigest())


def estado_en_plugins_txt(plugins_txt: pathlib.Path, nombre: str) -> EstadoEnPlugins:
    """Estado de *nombre* en el ``plugins.txt`` del perfil.

    Reusa :func:`sky_claw.local.loot.outcome.parse_load_order_bytes` —el parser cuya
    semántica está verificada contra libloadorder (``*`` activa, ``#`` comenta,
    ``\\r\\n``/``\\n`` indistintos)— en vez de escribir un segundo lector que pudiera
    divergir del que ya usa el contrato de LOOT.

    Si el archivo aparece más de una vez (el caso ``+X``/``-X`` duplicado de #835) gana
    ``HABILITADO``: alcanza con que UNA línea lo active para que el motor lo cargue.
    """
    try:
        data = plugins_txt.read_bytes()
    except FileNotFoundError:
        return EstadoEnPlugins.AUSENTE
    except OSError:
        return EstadoEnPlugins.ILEGIBLE

    objetivo = nombre.casefold()
    encontrado = False
    for entrada, activo in parse_load_order_bytes(data):
        if entrada.decode("utf-8", errors="replace").strip().casefold() != objetivo:
            continue
        encontrado = True
        if activo:
            return EstadoEnPlugins.HABILITADO
    return EstadoEnPlugins.DESHABILITADO if encontrado else EstadoEnPlugins.AUSENTE


@dataclass(frozen=True, slots=True)
class _Observacion:
    """Todo lo que el veredicto puede mirar, reunido una sola vez."""

    huella: HuellaDeArtefacto
    estado_plugins: EstadoEnPlugins
    huella_previa: str | None

    def sin_cambio(self) -> bool:
        return self.huella_previa is not None and self.huella.sha256 == self.huella_previa


def _ausente(o: _Observacion) -> bool:
    return not o.huella.existe


def _enlace(o: _Observacion) -> bool:
    return o.huella.es_enlace


def _ilegible(o: _Observacion) -> bool:
    return o.huella.ilegible


def _no_regular(o: _Observacion) -> bool:
    return not o.huella.es_regular


def _vacio(o: _Observacion) -> bool:
    return o.huella.bytes == 0


def _plugins_ilegible(o: _Observacion) -> bool:
    return o.estado_plugins is EstadoEnPlugins.ILEGIBLE


def _no_habilitado(o: _Observacion) -> bool:
    return o.estado_plugins is not EstadoEnPlugins.HABILITADO


def _sin_cambio(o: _Observacion) -> bool:
    return o.sin_cambio()


#: Reglas del veredicto, EN ORDEN: la primera que aplica gana. El orden es contrato
#: (``tests/test_wrye_bash_asistida.py::test_la_precedencia_de_motivos_esta_congelada``)
#: porque varias causas coexisten —un artefacto deshabilitado Y sin cambios es
#: ``NO_HABILITADO``, no ``SIN_CAMBIO``— y el operador necesita la causa más específica.
REGLAS_DEL_VEREDICTO: tuple[tuple[MotivoAsistido, Callable[[_Observacion], bool]], ...] = (
    (MotivoAsistido.ARTEFACTO_AUSENTE, _ausente),
    (MotivoAsistido.ARTEFACTO_ENLACE, _enlace),
    (MotivoAsistido.ARTEFACTO_ILEGIBLE, _ilegible),
    (MotivoAsistido.ARTEFACTO_NO_REGULAR, _no_regular),
    (MotivoAsistido.ARTEFACTO_VACIO, _vacio),
    (MotivoAsistido.PLUGINS_ILEGIBLE, _plugins_ilegible),
    (MotivoAsistido.NO_HABILITADO, _no_habilitado),
    (MotivoAsistido.SIN_CAMBIO, _sin_cambio),
)

#: Los motivos de fallo, en el orden en que se evalúan. Derivado de la tabla para que
#: no puedan divergir (agregar una regla sin su motivo, o al revés, lo rompe).
PRECEDENCIA_DE_MOTIVOS: tuple[MotivoAsistido, ...] = tuple(motivo for motivo, _ in REGLAS_DEL_VEREDICTO)

_MENSAJES: dict[MotivoAsistido, str] = {
    MotivoAsistido.ARTEFACTO_AUSENTE: (
        f"No existe '{BASHED_PATCH_NAME}' en el Data del juego: la etapa 6 no se construyó."
    ),
    MotivoAsistido.ARTEFACTO_ENLACE: (
        f"'{BASHED_PATCH_NAME}' es un enlace (symlink/junction): no se sigue, porque validaría "
        "un artefacto ajeno como propio."
    ),
    MotivoAsistido.ARTEFACTO_ILEGIBLE: f"'{BASHED_PATCH_NAME}' existe pero no se pudo leer: sin evidencia no hay veredicto.",
    MotivoAsistido.ARTEFACTO_NO_REGULAR: f"'{BASHED_PATCH_NAME}' no es un archivo regular (¿es un directorio?).",
    MotivoAsistido.ARTEFACTO_VACIO: f"'{BASHED_PATCH_NAME}' mide 0 bytes: no es un Bashed Patch.",
    MotivoAsistido.PLUGINS_ILEGIBLE: "No se pudo leer el plugins.txt del perfil: no se puede afirmar que el plugin esté activo.",
    MotivoAsistido.NO_HABILITADO: (
        f"'{BASHED_PATCH_NAME}' no está habilitado en el plugins.txt del perfil: el motor no lo carga "
        "y la etapa 9 lo ignoraría."
    ),
    MotivoAsistido.SIN_CAMBIO: (
        f"'{BASHED_PATCH_NAME}' tiene la misma huella que en la preparación: el archivo no se regeneró en esta corrida."
    ),
}


def preparar_etapa_asistida(*, artefacto: pathlib.Path, plugins_txt: pathlib.Path) -> PreparacionAsistida:
    """Fase 1: instrucciones para el operador + testigo previo del artefacto.

    No muta nada, no toma locks, no abre TX. La huella previa es lo que después
    permite **probar** frescura; sin ella, la fase 2 solo puede declarar existencia.
    """
    return PreparacionAsistida(
        artefacto=str(artefacto),
        plugins_txt=str(plugins_txt),
        huella_previa=huella_de_artefacto(artefacto),
        estado_plugins=estado_en_plugins_txt(plugins_txt, BASHED_PATCH_NAME),
        instrucciones=PASOS_ASISTIDOS,
    )


def veredicto_etapa_asistida(
    *,
    artefacto: pathlib.Path,
    plugins_txt: pathlib.Path,
    huella_previa: str | None = None,
) -> VeredictoAsistido:
    """Fase 2: veredicto por evidencia en disco, en el orden de ``REGLAS_DEL_VEREDICTO``.

    ``huella_previa`` (el ``sha256`` que devolvió la fase 1) es opcional: sin ella el
    veredicto puede ser ``OK`` pero declara ``frescura_probada=False``. Con ella, un
    archivo idéntico al de la preparación es ``SIN_CAMBIO`` — el operador no reconstruyó.
    """
    observacion = _Observacion(
        huella=huella_de_artefacto(artefacto),
        estado_plugins=estado_en_plugins_txt(plugins_txt, BASHED_PATCH_NAME),
        huella_previa=huella_previa,
    )
    for motivo, aplica in REGLAS_DEL_VEREDICTO:
        if aplica(observacion):
            return VeredictoAsistido(
                success=False,
                motivo=motivo,
                artefacto=str(artefacto),
                huella=observacion.huella,
                estado_plugins=observacion.estado_plugins,
                frescura_probada=False,
            )
    return VeredictoAsistido(
        success=True,
        motivo=MotivoAsistido.OK,
        artefacto=str(artefacto),
        huella=observacion.huella,
        estado_plugins=observacion.estado_plugins,
        frescura_probada=huella_previa is not None,
    )


def preparacion_como_resultado(preparacion: PreparacionAsistida) -> dict[str, object]:
    """Contrato canónico de tool-result (``success`` + ``message``) para la fase 1.

    ``success`` es ``False`` a propósito: preparar **no** construye nada, así que
    declarar éxito sería el falso verde que la etapa 6 entera existe para cerrar. El
    campo ``pendiente`` es el que distingue "esperando al operador" de "falló".
    """
    return {
        "success": False,
        "pendiente": True,
        "motivo": MotivoAsistido.ESPERANDO_OPERADOR.value,
        "message": (
            f"Etapa 6 (Bashed Patch) requiere tu intervención: construí '{BASHED_PATCH_NAME}' en la GUI "
            "de Wrye Bash lanzado desde MO2 y después pedí la verificación."
        ),
        "artefacto": preparacion.artefacto,
        "plugins_txt": preparacion.plugins_txt,
        "huella_previa": preparacion.huella_previa.sha256,
        "estado_en_plugins": preparacion.estado_plugins.value,
        "instrucciones": list(preparacion.instrucciones),
    }


def veredicto_como_resultado(veredicto: VeredictoAsistido) -> dict[str, object]:
    """Contrato canónico de tool-result para la fase 2 (``message`` vacío en éxito)."""
    return {
        "success": veredicto.success,
        "message": "" if veredicto.success else _MENSAJES[veredicto.motivo],
        "motivo": veredicto.motivo.value,
        "artefacto": veredicto.artefacto,
        "sha256": veredicto.huella.sha256,
        "bytes": veredicto.huella.bytes,
        "estado_en_plugins": veredicto.estado_plugins.value,
        "frescura_probada": veredicto.frescura_probada,
    }
