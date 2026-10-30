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

**Por qué este lector y no el de LOOT, y qué toma de cada uno.** El ``plugins.txt`` de un
perfil MO2 lo escribe MO2 y lo lee el hermano de la etapa 9
(``etapas_previas_lods._plugins_habilitados``) con ``utf-8-sig``: **con BOM de por medio**,
un lector sin esa tolerancia dice ``AUSENTE`` donde el guard de DynDOLOD dice habilitado, y
el operador recibe un rojo con el diagnóstico equivocado. Por eso la decodificación es
``utf-8-sig`` (la del consumidor aguas abajo) y la SINTAXIS de líneas se delega en
``loot.outcome.parse_load_order_bytes``, cuya semántica de activación (``*`` activa, ``#``
comenta, ``\\r\\n``/``\\n`` indistintos) está verificada contra libloadorder. Lo que NO se
hereda de LOOT es la regla de duplicados: acá alcanza con que UNA línea active el plugin
—es la semántica de activación del motor, no la del editor de load order— y el caso queda
anclado por test propio: no se apoya en el contrato del censo de prioridad de mods (#835), que
es otro archivo, con otro consumidor y otra semántica de duplicados.
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

#: BOM UTF-8. ``read_text(encoding="utf-8-sig")`` lo descarta al decodificar; acá se
#: descarta sobre los bytes para no perder el ``OSError`` de lectura ni mezclar dos
#: estrategias (bytes para el parser, texto para el nombre).
_BOM_UTF8 = b"\xef\xbb\xbf"


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
class TestigoPrevio:
    """Lo que la fase 1 observó del artefacto, transportado a la fase 2.

    ``existia`` distingue los dos casos que un ``sha256 | None`` confunde: *"lo miré y
    no estaba"* (``existia=False``) contra *"no te traje testigo"* (``None`` donde se
    espera un ``TestigoPrevio``). La diferencia es real: en la PRIMERA corrida el
    artefacto no existe, y su sola aparición posterior prueba que esta corrida lo
    escribió. Sin esta distinción, el caso más importante —crear el parche desde
    cero— era el único donde la frescura no se podía demostrar.

    **``existia=True`` exige ``sha256``, y lo exige acá** (``__post_init__``). La forma
    ``{existia: True, sha256: None}`` no significa nada —"estaba, pero no puedo decir
    cuál"— y sin este invariante dos consumidores distintos la producían por caminos
    distintos: :meth:`desde` la generaba al ver un artefacto *ilegible*, y
    ``_testigo_desde_resultado`` la aceptaba de vuelta desde el tool-result. Con el
    testigo así, :meth:`_Observacion.sin_cambio` no podía demostrar frescura y
    ``frescura_demostrable`` sí — dos veredictos contradictorios sobre la misma
    evidencia. Un artefacto presente pero sin identidad de contenido **no es un
    testigo**: es ``None``.

    Tres estados, no dos: *no estaba* (``existia=False``), *estaba con esta identidad*
    (``existia=True, sha256=...``) y *no sé* (``None``), que es lo único honesto cuando
    el archivo existía pero era un enlace, un directorio o no se pudo leer.
    """

    #: Nombre que empieza con "Test": sin esto pytest intenta recolectar la clase.
    __test__ = False

    existia: bool
    sha256: str | None

    def __post_init__(self) -> None:
        if self.existia and self.sha256 is None:
            raise ValueError("un testigo que afirma existencia necesita la identidad del contenido")
        if not self.existia and self.sha256 is not None:
            raise ValueError("un testigo que afirma ausencia no puede traer identidad de contenido")

    @classmethod
    def desde(cls, huella: HuellaDeArtefacto) -> TestigoPrevio | None:
        """El testigo que la huella permite afirmar, o ``None`` si no permite ninguno.

        Ausencia probada (``existia=False``) es un testigo: la aparición posterior del
        archivo demuestra por sí sola que esta corrida lo escribió. Un artefacto que
        existe pero del que no se pudo tomar identidad —enlace, directorio, lectura
        fallida— **no** es ausencia ni presencia: se devuelve ``None`` y la frescura
        queda declarada no probada, en vez de fabricar un ``existia=False`` que
        convertiría una aparición futura en "frescura demostrada" sin haber mirado.
        """
        if not huella.existe:
            return cls(existia=False, sha256=None)
        if huella.sha256 is None:
            return None
        return cls(existia=True, sha256=huella.sha256)


@dataclass(frozen=True, slots=True)
class PreparacionAsistida:
    """Fase 1: lo que el operador necesita para construir, más el testigo previo."""

    artefacto: str
    plugins_txt: str
    huella_previa: HuellaDeArtefacto
    #: ``None`` = la fase 1 no pudo tomar testigo (artefacto presente sin identidad
    #: de contenido). Distinto de ``TestigoPrevio(existia=False)``, que sí es una
    #: observación: "lo miré y no estaba".
    testigo_previo: TestigoPrevio | None
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

    Decodifica con ``utf-8-sig`` (la misma que el consumidor aguas abajo,
    ``etapas_previas_lods._plugins_habilitados``: con BOM, un lector sin esa tolerancia
    reporta ``AUSENTE`` sobre un perfil válido) y delega la SINTAXIS en
    :func:`sky_claw.local.loot.outcome.parse_load_order_bytes`, cuya semántica está
    verificada contra libloadorder (``*`` activa, ``#`` comenta, ``\\r\\n``/``\\n``
    indistintos), en vez de escribir un segundo parser que pudiera divergir.

    Si el archivo aparece más de una vez (el caso ``+X``/``-X`` duplicado de #835) gana
    ``HABILITADO``: alcanza con que UNA línea lo active para que el motor lo cargue.

    **Un byte inválido en cualquier parte del archivo lo vuelve** ``ILEGIBLE``: se valida
    el archivo ENTERO como UTF-8 estricto antes de parsear. El decodificado era por
    entrada y con ``errors="replace"``, así que una línea corrupta —incluso una que no
    fuera el plugin buscado— se sustituía por U+FFFD y el archivo seguía respondiendo
    ``HABILITADO``. El consumidor aguas abajo (``_plugins_habilitados``, ``utf-8-sig``
    ``strict``) devuelve ``None`` para ese mismo archivo: dos lectores del mismo perfil
    afirmando cosas distintas, y el de esta etapa era el optimista.
    """
    try:
        data = plugins_txt.read_bytes()
    except FileNotFoundError:
        return EstadoEnPlugins.AUSENTE
    except OSError:
        return EstadoEnPlugins.ILEGIBLE

    # utf-8-sig, como el consumidor aguas abajo: sin esto, un BOM al inicio deja el
    # nombre de la primera línea pegado a él y el plugin se reporta AUSENTE.
    data = data.removeprefix(_BOM_UTF8)
    # Validación ESTRICTA del archivo completo — el mismo criterio que utf-8-sig
    # ``strict``: "no pude leer el perfil" jamás colapsa en "el plugin no está".
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return EstadoEnPlugins.ILEGIBLE

    objetivo = nombre.casefold()
    encontrado = False
    for entrada, activo in parse_load_order_bytes(data):
        if entrada.decode("utf-8").strip().casefold() != objetivo:
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
    testigo: TestigoPrevio | None

    def sin_cambio(self) -> bool:
        return self.testigo is not None and self.testigo.existia and self.testigo.sha256 == self.huella.sha256

    def frescura_demostrable(self) -> bool:
        """Con testigo, un cambio (o una ausencia previa) prueba que ESTA corrida escribió."""
        if self.testigo is None:
            return False
        return not self.testigo.existia or self.testigo.sha256 != self.huella.sha256


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

#: Mensaje por motivo. Es TOTAL sobre ``MotivoAsistido`` y un test lo ata por igualdad
#: a ``REGLAS_DEL_VEREDICTO``: un motivo nuevo sin mensaje explotaría con ``KeyError``
#: en producción (``veredicto_como_resultado``) sin que ningún test lo viera.
_MENSAJES: dict[MotivoAsistido, str] = {
    MotivoAsistido.OK: "",
    MotivoAsistido.ESPERANDO_OPERADOR: (
        f"La etapa 6 está esperando que construyas '{BASHED_PATCH_NAME}' en la GUI de Wrye Bash."
    ),
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
    huella = huella_de_artefacto(artefacto)
    return PreparacionAsistida(
        artefacto=str(artefacto),
        plugins_txt=str(plugins_txt),
        huella_previa=huella,
        testigo_previo=TestigoPrevio.desde(huella),
        estado_plugins=estado_en_plugins_txt(plugins_txt, BASHED_PATCH_NAME),
        instrucciones=PASOS_ASISTIDOS,
    )


def veredicto_etapa_asistida(
    *,
    artefacto: pathlib.Path,
    plugins_txt: pathlib.Path,
    testigo: TestigoPrevio | None = None,
) -> VeredictoAsistido:
    """Fase 2: veredicto por evidencia en disco, en el orden de ``REGLAS_DEL_VEREDICTO``.

    ``testigo`` (lo que devolvió la fase 1) es opcional: sin él el veredicto puede ser
    ``OK`` pero declara ``frescura_probada=False``. Con él, un archivo idéntico al de la
    preparación es ``SIN_CAMBIO`` — el operador no reconstruyó— y uno que **no existía**
    en la fase 1 prueba frescura por su sola aparición.
    """
    observacion = _Observacion(
        huella=huella_de_artefacto(artefacto),
        estado_plugins=estado_en_plugins_txt(plugins_txt, BASHED_PATCH_NAME),
        testigo=testigo,
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
        frescura_probada=observacion.frescura_demostrable(),
    )


def preparacion_como_resultado(preparacion: PreparacionAsistida) -> dict[str, object]:
    """Contrato canónico de tool-result (``success`` + ``message``) para la fase 1.

    ``success`` es ``False`` a propósito: preparar **no** construye nada, así que
    declarar éxito sería el falso verde que la etapa 6 entera existe para cerrar.

    **``pendiente`` NO es un contrato ratificado todavía.** El dispatcher y
    ``ToolStateMachine`` solo conocen ``PENDING → RUNNING → COMPLETED/FAILED``, y no hay
    ningún consumidor de este campo en ``sky_claw/app/`` (verificado por grep): tal como
    está, una etapa *asistida* se va a ver como una etapa *fallida*. El mapeo se decide y
    se congela en el PR B de ADR 0016 §7, junto con el cableado de las tres superficies;
    hasta entonces el campo viaja marcado con ``pendiente_no_ratificado=True`` para que
    nadie lo consuma creyendo que el contrato está cerrado.
    """
    return {
        "success": False,
        "pendiente": True,
        "pendiente_no_ratificado": True,
        "motivo": MotivoAsistido.ESPERANDO_OPERADOR.value,
        "message": (
            f"Etapa 6 (Bashed Patch) requiere tu intervención: construí '{BASHED_PATCH_NAME}' en la GUI "
            "de Wrye Bash lanzado desde MO2 y después pedí la verificación."
        ),
        "artefacto": preparacion.artefacto,
        "plugins_txt": preparacion.plugins_txt,
        "testigo_previo": (
            None
            if preparacion.testigo_previo is None
            else {
                "existia": preparacion.testigo_previo.existia,
                "sha256": preparacion.testigo_previo.sha256,
            }
        ),
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
