"""Actividad del log de la etapa 9: ¿sigue escribiendo la herramienta?

**Qué cierra.** La etapa 9 dura de minutos a decenas de minutos y es ASISTIDA: el
operador —a veces remoto— tiene que cerrar la ventana de cada herramienta. El único
pulso de la corrida era un ``heartbeat`` que iba al log local, así que quien la
seguía por Telegram no podía distinguir una herramienta que trabaja de una que
espera una acción o está colgada.

**Qué hace y qué NO.** Reporta hechos OBSERVABLES del archivo de log —tamaño, mtime y
última línea— y decide cuándo vale la pena avisar. No infiere fases del texto
(patrones de un log de terceros que envejecen en silencio), no toca el veredicto de la
corrida y no actúa sobre un estancamiento: sólo lo informa. La última línea cuenta
sola: al terminar, la de una corrida real commiteada es el propio diálogo de cierre
(``Exit TexGen, zip and exit, check log or restart? ...``), que es justo lo que un
operador remoto necesita ver si olvidó cerrar la ventana.

**Cómo está partido.** :class:`RastreadorDeActividad` es una máquina de estados pura
con reloj inyectado (se prueba sin dormir); :func:`tomar_muestra` es la única parte con
I/O y es bloqueante (el caller la corre en un hilo). El servicio compone las dos.

**Calibración (límite declarado).** Los defaults salen de UNA corrida real commiteada
(``docs/validation/2026-09-13_pr580_real_rig/``): el mayor silencio entre marcas de
tiempo consecutivas del log fue de 65 s en DynDOLOD y 5 s en TexGen. El umbral de
estancamiento está a más de 10x de eso; es un criterio de margen, NO una calibración:
presets pesados o mundos grandes pueden callar más. Un falso estancamiento le enseña al
operador a ignorar el aviso, así que los defaults se equivocan hacia avisar tarde. Los
umbrales son configurables.
"""

from __future__ import annotations

import enum
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sky_claw.app.security import links

if TYPE_CHECKING:
    import pathlib

#: Largo máximo de la última línea que viaja al operador. El mensaje de Telegram tiene
#: un techo de 4096 caracteres y el texto se ESCAPA antes de enviarse (``&`` pasa a
#: ``&amp;``, x5): un tope acá mantiene el peor caso por debajo del límite.
MAX_CARACTERES_DE_LINEA = 300


class TipoDeAviso(enum.StrEnum):
    """Los tres avisos posibles. El VALOR viaja en el payload del evento: no se renombra a la ligera."""

    PROGRESO = "progress"
    ESTANCADO = "stalled"
    REANUDADO = "resumed"


@dataclass(frozen=True, slots=True)
class ConfiguracionDeObservacion:
    """Cada cuánto sondear el log y cuándo avisar. Todos los valores son positivos.

    Args:
        intervalo_de_sondeo_s: cada cuánto se lee la cola del log.
        intervalo_de_progreso_s: cada cuánto se avisa "sigue en curso" mientras no
            haya estancamiento.
        umbral_de_estancamiento_s: cuánto silencio del log (sin cambio de tamaño,
            mtime ni archivo) cuenta como estancamiento. Ver "Calibración" arriba.
        max_bytes_de_cola: cuánto del final del archivo se lee para sacar la última línea.
    """

    intervalo_de_sondeo_s: float = 15.0
    intervalo_de_progreso_s: float = 600.0
    umbral_de_estancamiento_s: float = 900.0
    max_bytes_de_cola: int = 16384

    def __post_init__(self) -> None:
        for nombre in (
            "intervalo_de_sondeo_s",
            "intervalo_de_progreso_s",
            "umbral_de_estancamiento_s",
            "max_bytes_de_cola",
        ):
            valor = getattr(self, nombre)
            if valor <= 0:
                raise ValueError(f"{nombre} debe ser positivo, recibió {valor!r}")


@dataclass(frozen=True, slots=True)
class MuestraDeLog:
    """Lo que se pudo observar del log en un instante."""

    nombre: str
    tamano: int
    mtime: float
    ultima_linea: str


@dataclass(frozen=True, slots=True)
class AvisoDeActividad:
    """Un aviso listo para publicarse. ``log`` vacío = todavía no hay un log de esta corrida."""

    tipo: TipoDeAviso
    log: str
    segundos_en_curso: float
    segundos_sin_actividad: float
    tamano_del_log: int
    ultima_linea: str


class RastreadorDeActividad:
    """Decide cuándo avisar a partir de una serie de muestras. Puro: el reloj lo da el caller.

    **Qué cuenta como actividad:** que cambie la firma ``(nombre, tamaño, mtime)``. El
    nombre entra porque TexGen termina y DynDOLOD empieza su propio log; el mtime porque
    un binario puede reescribir sin crecer.

    **Un estancamiento se avisa UNA vez por episodio** y una reanudación lo cierra; si no,
    un log callado una hora serían decenas de mensajes. Si coinciden estancamiento y
    progreso en el mismo sondeo, sale el estancamiento (es lo que importa). Mientras hay un
    episodio abierto no hay avisos de progreso, y al reanudar la cuenta del progreso
    vuelve a empezar (si no, el progreso "atrasado" saldría pegado a la reanudación).

    Usar un reloj MONÓTONO (``time.monotonic``): sólo se comparan diferencias, y el mtime
    del archivo no se mezcla con el reloj del proceso.
    """

    def __init__(self, config: ConfiguracionDeObservacion, *, inicio: float) -> None:
        self._config = config
        self._inicio = inicio
        self._firma: tuple[str, int, float] | None = None
        self._ultima_actividad = inicio
        self._ultimo_progreso = inicio
        self._estancado = False

    def observar(self, muestra: MuestraDeLog | None, *, ahora: float) -> AvisoDeActividad | None:
        """Registra una observación (``None`` = no hay log de esta corrida) y devuelve un aviso, si toca."""
        hubo_actividad = False
        if muestra is not None:
            firma = (muestra.nombre, muestra.tamano, muestra.mtime)
            if firma != self._firma:
                self._firma = firma
                hubo_actividad = True

        if hubo_actividad:
            silencio_previo = ahora - self._ultima_actividad
            self._ultima_actividad = ahora
            if self._estancado:
                self._estancado = False
                self._ultimo_progreso = ahora
                return self._aviso(TipoDeAviso.REANUDADO, muestra, ahora, silencio_previo)

        silencio = ahora - self._ultima_actividad
        if self._estancado:
            return None
        if silencio >= self._config.umbral_de_estancamiento_s:
            self._estancado = True
            return self._aviso(TipoDeAviso.ESTANCADO, muestra, ahora, silencio)
        if ahora - self._ultimo_progreso >= self._config.intervalo_de_progreso_s:
            self._ultimo_progreso = ahora
            return self._aviso(TipoDeAviso.PROGRESO, muestra, ahora, silencio)
        return None

    def _aviso(
        self, tipo: TipoDeAviso, muestra: MuestraDeLog | None, ahora: float, silencio: float
    ) -> AvisoDeActividad:
        return AvisoDeActividad(
            tipo=tipo,
            log=muestra.nombre if muestra is not None else "",
            segundos_en_curso=ahora - self._inicio,
            segundos_sin_actividad=silencio,
            tamano_del_log=muestra.tamano if muestra is not None else 0,
            ultima_linea=muestra.ultima_linea if muestra is not None else "",
        )


def _ultima_linea(cola: bytes) -> str:
    """Última línea no vacía de ``cola``, recortada. ``errors="replace"``: es texto para mostrar, no un veredicto."""
    for linea in reversed(cola.decode("utf-8", errors="replace").splitlines()):
        limpia = linea.strip()
        if limpia:
            if len(limpia) > MAX_CARACTERES_DE_LINEA:
                return limpia[: MAX_CARACTERES_DE_LINEA - 1] + "…"
            return limpia
    return ""


def tomar_muestra(ruta: pathlib.Path, max_bytes_de_cola: int) -> MuestraDeLog | None:
    """Observa el log en ``ruta``. BLOQUEANTE: el caller la corre en un hilo.

    Devuelve ``None`` —nunca lanza— si no hay nada utilizable: la ruta no existe, no es un
    archivo regular, es un enlace (symlink o junction: la detección vive en
    :mod:`sky_claw.app.security.links`) o el sistema de archivos falló. Un ``sharing
    violation`` mientras la herramienta escribe es lo esperable en Windows, y el
    observador no puede ser el que rompa una corrida de 30+ minutos por una lectura que
    se puede reintentar en el próximo sondeo.
    """
    try:
        tipo, estado = links.link_kind_and_identity_or_raise(ruta)
        if estado is None or tipo is not None or not stat.S_ISREG(estado.st_mode):
            return None
        with ruta.open("rb") as archivo:
            if estado.st_size > max_bytes_de_cola:
                archivo.seek(estado.st_size - max_bytes_de_cola)
            cola = archivo.read(max_bytes_de_cola)
    except OSError:
        return None
    return MuestraDeLog(
        nombre=ruta.name,
        tamano=estado.st_size,
        mtime=estado.st_mtime,
        ultima_linea=_ultima_linea(cola),
    )
