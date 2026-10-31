"""El INI y la DLL del plugin de relieve automático (Auto Parallax) tal como el overlay del perfil los entrega (#834 ítem 3).

La elegibilidad (``elegibilidad.py``) lee la FORMA de las mallas. Pero el plugin solo enciende relieve si ``bAutoEnableParallax`` está activo en su
INI (sección ``[Experimental]``; el valor con que se distribuye es ``false``): con el INI apagado ninguna forma lo usa, por elegible que sea, y
decir ``SI`` es decir algo que en el juego no pasa. Este módulo lee ese ajuste del archivo que GANA en el overlay (``SKSE/Plugins/AutoParallax.ini``)
y comprueba si la DLL (``SKSE/Plugins/AutoParallax.dll``) está suelta en el perfil.

Cuatro estados del INI (``AjusteDelIni``) y solo uno apaga un ``SI``:

- ``ACTIVO`` / ``APAGADO``: el INI es legible y la clave vale ``true``/``1`` o ``false``/``0``. ``APAGADO`` es el único que cambia un veredicto.
- ``AUSENTE``: ningún mod del perfil entrega el INI. No se sabe qué hace el plugin sin él (puede vivir fuera de MO2): se avisa, no se cambia nada.
- ``NO_DETERMINADO``: el INI está pero no se puede afirmar el valor (ilegible, demasiado grande, clave ausente, valor que no es un booleano claro,
  sección o clave duplicada). Mismo trato que ``AUSENTE``: lo que no se observa no se da por apagado ni por activo.

**Lo que NO está verificado:** que la versión instalada del plugin lea el INI con estas reglas (la rama pública leída no está fijada a un SHA) ni la
matriz DLL × INI × tipo de objeto, que necesita el rig. La DLL solo se comprueba por presencia: no se lee su versión.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from sky_claw.local.mo2.ganadores import GanadoresDelPerfil

__all__ = [
    "CARPETA_DE_PLUGINS_SKSE",
    "MAXIMO_DEL_INI",
    "RUTA_DE_LA_DLL",
    "RUTA_DEL_INI",
    "AjusteDelIni",
    "EstadoDelPlugin",
    "interpretar_ini",
    "leer_estado_del_plugin",
]

#: Subárbol Data que se indexa (con el nombre canónico que usa el juego; en Windows el disco no distingue mayúsculas).
CARPETA_DE_PLUGINS_SKSE: Final = "SKSE/Plugins"
#: Rutas virtuales (en minúsculas, como las compara el índice de ganadores) del INI y la DLL del paquete del plugin.
RUTA_DEL_INI: Final = "skse/plugins/autoparallax.ini"
RUTA_DE_LA_DLL: Final = "skse/plugins/autoparallax.dll"
#: Un INI de ajustes pesa unos cientos de bytes: más que esto no es el archivo que se espera y no se interpreta.
MAXIMO_DEL_INI: Final = 64 * 1024

_SECCION: Final = "experimental"
_CLAVE: Final = "bautoenableparallax"
_VERDADEROS: Final = frozenset({"true", "1"})
_FALSOS: Final = frozenset({"false", "0"})


class AjusteDelIni(Enum):
    ACTIVO = "ACTIVO"
    APAGADO = "APAGADO"
    AUSENTE = "AUSENTE"
    NO_DETERMINADO = "NO_DETERMINADO"


@dataclass(frozen=True, slots=True)
class EstadoDelPlugin:
    ini: AjusteDelIni
    capa_del_ini: str | None  #: Mod que entrega el INI que gana; ``None`` si no hay.
    dll_presente: bool  #: ``AutoParallax.dll`` suelta en el overlay. Falso NO prueba que no esté instalada: puede estar fuera de MO2.
    detalle: str  #: Por qué ``NO_DETERMINADO`` (vacío en el resto).

    @property
    def apaga_el_relieve(self) -> bool:
        """Solo un INI legible con la clave apagada cambia un veredicto: lo no observado nunca lo apaga."""
        return self.ini is AjusteDelIni.APAGADO

    def avisos(self) -> tuple[str, ...]:
        """Lo que el operador tiene que saber aunque el INI no cambie ningún veredicto. Vacío cuando el INI está activo y la DLL, a la vista."""
        avisos: list[str] = []
        if self.ini is AjusteDelIni.APAGADO:
            avisos.append(
                f"el INI del plugin (mod {self.capa_del_ini!r}) tiene bAutoEnableParallax apagado: el plugin no enciende relieve en ninguna "
                "forma. Activalo para que una salida SI se vea"
            )
        elif self.ini is AjusteDelIni.AUSENTE:
            avisos.append(
                "ningún mod del perfil entrega SKSE/Plugins/AutoParallax.ini: no se pudo comprobar bAutoEnableParallax (por defecto está apagado)"
            )
        elif self.ini is AjusteDelIni.NO_DETERMINADO:
            avisos.append(f"no se pudo comprobar bAutoEnableParallax en el INI del plugin: {self.detalle}")
        if not self.dll_presente:
            avisos.append(
                "ningún mod del perfil entrega SKSE/Plugins/AutoParallax.dll: si el plugin no está instalado, ninguna salida se usa "
                "(puede estar instalado fuera de MO2, y eso no se ve acá)"
            )
        return tuple(avisos)


def interpretar_ini(datos: bytes) -> tuple[AjusteDelIni, str]:
    """El ajuste ``bAutoEnableParallax`` de *datos* y, si no se puede afirmar, por qué. Nunca lanza ante un contenido raro."""
    if len(datos) > MAXIMO_DEL_INI:
        return AjusteDelIni.NO_DETERMINADO, f"el INI pesa más de {MAXIMO_DEL_INI} bytes"
    try:
        texto = datos.decode("utf-8-sig")
    except UnicodeDecodeError:
        return AjusteDelIni.NO_DETERMINADO, "el INI no es texto UTF-8"
    # Estricto a propósito: una sección o una clave repetidas son ambiguas (cuál lee el plugin no está verificado) y no se adivina. Sin
    # comentarios en línea: «false ; x» no se reconoce como booleano en vez de suponer que el plugin lo recorta.
    # ``default_section``: ``[DEFAULT]`` es una herencia propia de configparser que el plugin no conoce; con un nombre que ningún encabezado puede
    # tener (el patrón de encabezado no cruza saltos de línea) ese valor deja de heredarse a ``[Experimental]`` y ya no cuenta como declarado ahí.
    lector = configparser.ConfigParser(
        strict=True, interpolation=None, inline_comment_prefixes=None, default_section="\n"
    )
    try:
        lector.read_string(texto)
    except configparser.Error:
        return AjusteDelIni.NO_DETERMINADO, "el INI no se pudo interpretar (formato inválido o clave repetida)"
    secciones = [s for s in lector.sections() if s.lower() == _SECCION]
    if len(secciones) != 1:
        return AjusteDelIni.NO_DETERMINADO, "el INI no tiene una única sección [Experimental]"
    valor = lector[secciones[0]].get(_CLAVE)  # configparser baja a minúsculas los nombres de clave
    if valor is None:
        return AjusteDelIni.NO_DETERMINADO, "el INI no define bAutoEnableParallax"
    valor = valor.strip().lower()
    if valor in _VERDADEROS:
        return AjusteDelIni.ACTIVO, ""
    if valor in _FALSOS:
        return AjusteDelIni.APAGADO, ""
    return AjusteDelIni.NO_DETERMINADO, "bAutoEnableParallax no vale true/false ni 1/0"


def leer_estado_del_plugin(indice: GanadoresDelPerfil) -> EstadoDelPlugin:
    """El estado del plugin según el overlay. *indice* es el de ``SKSE/Plugins`` del perfil (``diagnostico_perfil.cargar_plugin_del_perfil``)."""
    dll_presente = indice.ganador(RUTA_DE_LA_DLL) is not None
    ganador = indice.ganador(RUTA_DEL_INI)
    if ganador is None:
        return EstadoDelPlugin(AjusteDelIni.AUSENTE, None, dll_presente, "")
    try:
        if ganador.archivo.stat().st_size > MAXIMO_DEL_INI:
            return EstadoDelPlugin(
                AjusteDelIni.NO_DETERMINADO, ganador.capa, dll_presente, f"el INI pesa más de {MAXIMO_DEL_INI} bytes"
            )
        datos = ganador.archivo.read_bytes()
    except OSError:
        return EstadoDelPlugin(AjusteDelIni.NO_DETERMINADO, ganador.capa, dll_presente, "el INI no se pudo leer")
    ajuste, detalle = interpretar_ini(datos)
    return EstadoDelPlugin(ajuste, ganador.capa, dll_presente, detalle)
