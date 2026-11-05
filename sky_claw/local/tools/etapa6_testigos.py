"""Testigo durable de la fase 1 de la etapa 6 (ADR 0016 §6/§7.2, PR D).

**El problema que cierra.** La fase 1 toma un testigo del artefacto *antes* de que el
operador construya; la fase 2 lo compara para demostrar que el parche se escribió en esta
corrida. Ese testigo viajaba sólo dentro del tool-result, y el resultado vive en el
``ReactiveStore`` de la GUI —un diccionario en RAM, sin persistencia
(``sky_claw/app/gui/state/reactive_store.py``)—; en la superficie del agente LLM ni
siquiera podía viajar porque ``GenerateBashedPatchParams`` sólo expone ``fase`` (A-4).
Cerrar la GUI entre las dos fases (o pedir la verificación al agente) obligaba a declarar
``frescura_probada=False``.

**Identidad del testigo (por qué NO se clavea por la capa ganadora).** En la primera
corrida el parche no existe en ninguna capa, así que ``bashed_patch_en_overlay`` cae al
``Data`` físico; cuando el operador construye dentro de MO2 bajo la USVFS, el archivo
aparece en ``overwrite`` o en un mod de salida habilitado. Clavear por la ruta ganadora
haría que la fase 1 guardara bajo ``Data`` y la fase 2 buscara bajo ``overwrite``: fallo
del 100 % en el caso más importante. La identidad invariante del slot virtual es el par
``(plugins_txt canónico, destino Data canónico)``, que fija instancia de MO2, perfil y
raíz del juego sin acoplarse a qué capa gana en cada instante.

**Dónde vive y cómo se serializa.** En :meth:`~sky_claw.config.SystemPaths.runtime_state_dir`
(``~/.sky_claw/state/etapa6_testigos.json``), resuelto al operar y **no** en
``.skyclaw_backups/``: dos procesos lanzados desde directorios de trabajo distintos ven el
mismo testigo (ADR 0011 P0.2). Como el daemon y ``system_tools`` construyen instancias
distintas de :class:`AlmacenJSON` sobre ese mismo archivo (y pueden coexistir dos procesos
de Sky-Claw), toda operación leer-modificar-reemplazar se serializa **por ruta canónica**
tanto dentro del proceso (``_LOCKS_POR_RUTA``) como entre procesos mediante un lockfile
hermano (``fcntl.flock`` en POSIX / ``msvcrt.locking`` en Windows).

**Regla de seguridad.** El testigo durable es una *observación previa*, nunca autoridad.
Si el archivo es un enlace o está corrupto, :meth:`AlmacenJSON.leer` devuelve ``None`` y
la etapa degrada a ``frescura_probada=False`` sin tocar el archivo; :meth:`AlmacenJSON.guardar`
y :meth:`AlmacenJSON.olvidar` tampoco pisan un archivo ilegible —levantan
:class:`AlmacenDeTestigosIlegibleError` para que el servicio declare
``error_de_almacen`` y preserve el archivo para diagnóstico—.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import pathlib
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from sky_claw.app.security.links import link_kind_or_raise
from sky_claw.config import SystemPaths
from sky_claw.local.tools.wrye_bash_asistida import (
    DecisionDeTestigo,
    TestigoPrevio,
    decidir_testigo_de_preparacion,
    es_generacion_valida,
)

logger = logging.getLogger(__name__)

#: Índice canónico de la etapa 6 en el DAG (SOP §5 regla 5), anclado por igualdad contra
#: ``wrye_bash_service._ETAPA_WRYE_BASH`` en ``tests/test_etapa6_testigo_durable.py``.
_ETAPA_WRYE_BASH = 6

NOMBRE_DEL_ARCHIVO = "etapa6_testigos.json"
_TIMEOUT_LOCK_SEGUNDOS = 5.0
_PASO_REINTENTO_SEGUNDOS = 0.01

#: Presupuesto de reintentos de ``os.replace`` ante una violación de uso compartido
#: transitoria en Windows (Defender o el indexador con el JSON abierto sin
#: ``FILE_SHARE_DELETE``): hasta ``_REINTENTOS_DE_REEMPLAZO`` reintentos con backoff
#: exponencial desde ``_PASO_DE_REEMPLAZO_SEGUNDOS`` (≈ 0.02 + 0.04 + 0.08 + 0.16 + 0.32 s).
_REINTENTOS_DE_REEMPLAZO = 5
_PASO_DE_REEMPLAZO_SEGUNDOS = 0.02
#: ``ERROR_ACCESS_DENIED`` (5) y ``ERROR_SHARING_VIOLATION`` (32): los únicos ``winerror`` que
#: significan "otro proceso lo tiene abierto ahora", y por tanto los únicos que se reintentan.
_WINERROR_TRANSITORIOS = frozenset({5, 32})

_PATRON_HUELLA = re.compile(r"[0-9a-f]{64}")

#: Locks reentrantes por ruta canónica para serializar múltiples instancias de
#: ``AlmacenJSON`` dentro del mismo proceso antes de competir por el lock del SO.
_GUARDIA_DE_LOCKS = threading.Lock()
_LOCKS_POR_RUTA: dict[str, threading.RLock] = {}


if sys.platform == "win32":
    import msvcrt

    def _intentar_lock_so(fd: int) -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EDEADLK):
                return False
            raise

    def _liberar_lock_so(fd: int) -> None:
        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _intentar_lock_so(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return False
            raise

    def _liberar_lock_so(fd: int) -> None:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)


class AlmacenDeTestigosIlegibleError(OSError):
    """El archivo del almacén existe pero no es un objeto JSON regular y legible, o no se
    pudo tomar su exclusión cross-process.

    Hereda de :class:`OSError` para que el servicio pueda tratar en un solo borde los
    fallos de almacenamiento sin confundirlos con errores de programación. Se levanta en
    escritura/borrado para **no sobrescribir** un archivo corrupto ni atravesar enlaces.
    """


def _ahora() -> str:
    """Marca de tiempo ISO-8601 en UTC con precisión de segundos."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def nueva_generacion() -> str:
    """Identificador opaco de 128 bits (32 hex en minúsculas) para una época de preparación."""
    return secrets.token_hex(16)


_CLAVES_TESTIGO_DURABLE: frozenset[str] = frozenset({"existia", "sha256", "observado_en", "generacion"})
#: ``huella_contexto`` es opcional al LEER: los registros que escribió PR D no la traían, y se
#: leen como "contexto desconocido" (``None``) en vez de descartarse.
_CLAVES_TESTIGO_DURABLE_CON_CONTEXTO: frozenset[str] = _CLAVES_TESTIGO_DURABLE | {"huella_contexto"}


def es_huella_valida(valor: object) -> bool:
    """True si *valor* es un digest SHA-256 en hex minúscula (64 caracteres)."""
    return isinstance(valor, str) and _PATRON_HUELLA.fullmatch(valor) is not None


def _validar_generacion_explicita(generacion: str) -> None:
    if not es_generacion_valida(generacion):
        raise ValueError(f"generacion obligatoria de 32 hex en minúsculas para consumo CAS: {generacion!r}")


def _canonizar_ruta(ruta: pathlib.Path) -> str:
    """``resolve(strict=False)`` + ``normpath`` + ``normcase``, igual que ``handoffs.clave_de_artifact``."""
    return os.path.normcase(os.path.normpath(str(pathlib.Path(ruta).resolve(strict=False))))


def _lock_intraproceso(ruta: pathlib.Path) -> threading.RLock:
    clave = _canonizar_ruta(ruta)
    with _GUARDIA_DE_LOCKS:
        existente = _LOCKS_POR_RUTA.get(clave)
        if existente is None:
            existente = threading.RLock()
            _LOCKS_POR_RUTA[clave] = existente
        return existente


def _ruta_del_lockfile(ruta: pathlib.Path) -> pathlib.Path:
    return ruta.parent / f".{ruta.name}.lock"


@dataclass(frozen=True, slots=True)
class ClaveDeTestigo:
    """Identidad del slot virtual del Bashed Patch: ``(plugins_txt, destino_data)``.

    - ``plugins_txt`` fija la instancia de MO2 y el perfil (``<mo2>/profiles/<p>/plugins.txt``).
    - ``destino_data`` fija la instalación del juego y el nombre canónico del artefacto
      (``<game>/Data/<artefacto>``), invariante respecto de qué capa del overlay gane en
      cada fase.
    """

    __test__ = False

    plugins_txt: str
    destino_data: str

    def como_texto(self) -> str:
        """Serialización inyectiva en JSON: ningún carácter en las rutas puede colisionar."""
        return json.dumps([self.plugins_txt, self.destino_data], ensure_ascii=False, separators=(",", ":"))


def clave_de_testigo(*, plugins_txt: pathlib.Path, destino_data: pathlib.Path) -> ClaveDeTestigo:
    """Construye la clave canónica del slot virtual para ``(plugins_txt, destino_data)``."""
    return ClaveDeTestigo(
        plugins_txt=_canonizar_ruta(plugins_txt),
        destino_data=_canonizar_ruta(destino_data),
    )


@dataclass(frozen=True, slots=True)
class TestigoDurable:
    """Un :class:`TestigoPrevio` con la marca de cuándo se observó y su ``generacion``.

    Delega el invariante de dominio en :class:`TestigoPrevio` y :func:`es_generacion_valida`
    para que no existan dos validadores que puedan divergir (lección S-10 de ADR 0016 §9).
    """

    __test__ = False

    existia: bool
    sha256: str | None
    observado_en: str
    generacion: str = field(default_factory=nueva_generacion)
    #: Digest del contexto de entrada del perfil con el que se tomó el testigo
    #: (:func:`~sky_claw.local.tools.wrye_bash_asistida.huella_de_contexto`). ``None`` =
    #: desconocido (registro de PR D, o ``plugins.txt`` ilegible al preparar): no puede
    #: afirmar que el contexto no cambió.
    huella_contexto: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.existia, bool):
            raise ValueError(f"existia debe ser bool, no {type(self.existia).__name__}")
        if self.sha256 is not None and not isinstance(self.sha256, str):
            raise ValueError(f"sha256 debe ser str o None, no {type(self.sha256).__name__}")
        TestigoPrevio(existia=self.existia, sha256=self.sha256)
        if not isinstance(self.observado_en, str) or not self.observado_en:
            raise ValueError("un testigo durable necesita marca de tiempo no vacía")
        if not es_generacion_valida(self.generacion):
            raise ValueError(f"generacion debe ser 32 hex en minúsculas: {self.generacion!r}")
        if self.huella_contexto is not None and not es_huella_valida(self.huella_contexto):
            raise ValueError(f"huella_contexto debe ser SHA-256 hex en minúsculas o None: {self.huella_contexto!r}")

    @classmethod
    def desde(
        cls,
        testigo: TestigoPrevio,
        *,
        observado_en: str | None = None,
        generacion: str | None = None,
        huella_contexto: str | None = None,
    ) -> TestigoDurable:
        return cls(
            existia=testigo.existia,
            sha256=testigo.sha256,
            observado_en=observado_en or _ahora(),
            generacion=nueva_generacion() if generacion is None else generacion,
            huella_contexto=huella_contexto,
        )

    def como_testigo_previo(self) -> TestigoPrevio:
        return TestigoPrevio(existia=self.existia, sha256=self.sha256)

    def coincide_con(self, *, testigo: TestigoPrevio, generacion: str) -> bool:
        """True sólo si ``testigo`` y ``generacion`` (obligatoria) describen exactamente esta época."""
        _validar_generacion_explicita(generacion)
        return self.como_testigo_previo() == testigo and self.generacion == generacion

    def como_dict(self) -> dict[str, object]:
        return {
            "existia": self.existia,
            "sha256": self.sha256,
            "observado_en": self.observado_en,
            "generacion": self.generacion,
            "huella_contexto": self.huella_contexto,
        }

    @classmethod
    def desde_dict(cls, crudo: object) -> TestigoDurable | None:
        """Forma canónica o ``None``. Nunca lanza sobre datos leídos de disco."""
        if not isinstance(crudo, dict):
            return None
        claves = set(crudo.keys())
        if not (_CLAVES_TESTIGO_DURABLE <= claves <= _CLAVES_TESTIGO_DURABLE_CON_CONTEXTO):
            return None
        huella_contexto = crudo.get("huella_contexto")
        if huella_contexto is not None and not es_huella_valida(huella_contexto):
            return None
        existia = crudo.get("existia")
        sha256 = crudo.get("sha256")
        observado_en = crudo.get("observado_en")
        generacion = crudo.get("generacion")
        if not isinstance(existia, bool) or not (sha256 is None or isinstance(sha256, str)):
            return None
        if not isinstance(observado_en, str) or not observado_en or not es_generacion_valida(generacion):
            return None
        try:
            return cls(
                existia=existia,
                sha256=sha256,
                observado_en=observado_en,
                generacion=generacion,
                huella_contexto=huella_contexto,
            )
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class ResultadoDePreparacionDurable:
    """Resultado atómico de observar + decidir + persistir bajo la exclusión del almacén."""

    __test__ = False

    decision: DecisionDeTestigo
    durable: TestigoDurable | None


def _resolver_preparacion_durable(
    *,
    actual: TestigoDurable | None,
    observado_ahora: TestigoPrevio | None,
    observado_en: str | None,
    verificar_contexto: bool = False,
    contexto_ahora: str | None = None,
) -> tuple[DecisionDeTestigo, TestigoDurable | None, bool]:
    """Única implementación de la transición de época para memoria y JSON.

    Con ``verificar_contexto`` la época nueva (o la misma, con el contexto al día) se
    persiste con ``huella_contexto=contexto_ahora``; sin él, el registro queda sin huella.
    """
    decision = decidir_testigo_de_preparacion(
        observado_ahora=observado_ahora,
        durable=None if actual is None else actual.como_testigo_previo(),
        verificar_contexto=verificar_contexto,
        contexto_ahora=contexto_ahora,
        contexto_durable=None if actual is None else actual.huella_contexto,
    )
    if decision.persistir and decision.testigo is not None:
        misma_epoca = actual is not None and actual.como_testigo_previo() == decision.testigo
        generacion = actual.generacion if misma_epoca and actual is not None else nueva_generacion()
        vigente = TestigoDurable.desde(
            decision.testigo,
            observado_en=observado_en,
            generacion=generacion,
            huella_contexto=contexto_ahora if verificar_contexto else None,
        )
        return decision, vigente, True
    return decision, actual, False


@runtime_checkable
class AlmacenDeTestigos(Protocol):
    """Superficie que consume ``WryeBashPipelineService``.

    Las mutaciones de producción pasan exclusivamente por :meth:`sincronizar_preparacion`
    (transacción atómica ``cargar → observar → decidir → escribir``) y
    :meth:`olvidar_si_coincide` (compare-and-delete sobre ``(testigo, generacion)``).
    :meth:`guardar` y :meth:`olvidar` quedan para configuración/limpieza directa en tests.
    """

    def leer(self, clave: ClaveDeTestigo) -> TestigoDurable | None: ...

    def guardar(
        self,
        clave: ClaveDeTestigo,
        testigo: TestigoPrevio,
        *,
        observado_en: str | None = None,
        huella_contexto: str | None = None,
    ) -> TestigoDurable: ...

    def olvidar(self, clave: ClaveDeTestigo) -> None: ...

    def sincronizar_preparacion(
        self,
        clave: ClaveDeTestigo,
        observar: Callable[[], TestigoPrevio | None],
        *,
        observado_en: str | None = None,
        contexto: Callable[[], str | None] | None = None,
    ) -> ResultadoDePreparacionDurable: ...

    def olvidar_si_coincide(
        self,
        clave: ClaveDeTestigo,
        *,
        testigo: TestigoPrevio,
        generacion: str,
    ) -> bool: ...


class AlmacenEnMemoria:
    """Almacén de proceso para pruebas unitarias sin tocar el filesystem."""

    def __init__(self) -> None:
        self._datos: dict[str, TestigoDurable] = {}
        self._lock = threading.RLock()

    def leer(self, clave: ClaveDeTestigo) -> TestigoDurable | None:
        with self._lock:
            return self._datos.get(clave.como_texto())

    def guardar(
        self,
        clave: ClaveDeTestigo,
        testigo: TestigoPrevio,
        *,
        observado_en: str | None = None,
        huella_contexto: str | None = None,
    ) -> TestigoDurable:
        durable = TestigoDurable.desde(testigo, observado_en=observado_en, huella_contexto=huella_contexto)
        with self._lock:
            self._datos[clave.como_texto()] = durable
        return durable

    def olvidar(self, clave: ClaveDeTestigo) -> None:
        with self._lock:
            self._datos.pop(clave.como_texto(), None)

    def sincronizar_preparacion(
        self,
        clave: ClaveDeTestigo,
        observar: Callable[[], TestigoPrevio | None],
        *,
        observado_en: str | None = None,
        contexto: Callable[[], str | None] | None = None,
    ) -> ResultadoDePreparacionDurable:
        with self._lock:
            actual = self._datos.get(clave.como_texto())
            observado_ahora = observar()
            decision, vigente, persistir = _resolver_preparacion_durable(
                actual=actual,
                observado_ahora=observado_ahora,
                observado_en=observado_en,
                verificar_contexto=contexto is not None,
                contexto_ahora=None if contexto is None else contexto(),
            )
            if persistir and vigente is not None:
                self._datos[clave.como_texto()] = vigente
            return ResultadoDePreparacionDurable(decision=decision, durable=vigente)

    def olvidar_si_coincide(
        self,
        clave: ClaveDeTestigo,
        *,
        testigo: TestigoPrevio,
        generacion: str,
    ) -> bool:
        _validar_generacion_explicita(generacion)
        with self._lock:
            actual = self._datos.get(clave.como_texto())
            if actual is None or not actual.coincide_con(testigo=testigo, generacion=generacion):
                return False
            del self._datos[clave.como_texto()]
            return True


class AlmacenJSON:
    """Archivo JSON único por usuario con exclusión por ruta canónica (intraproceso +
    lockfile cross-process) y escritura atómica (``mkstemp`` + ``os.replace``).

    Acepta una ruta fija o un callable sin argumentos (como usa :func:`almacen_por_defecto`)
    para que construir el servicio no evalúe rutas ni toque el disco en frío.
    """

    def __init__(self, ruta: pathlib.Path | Callable[[], pathlib.Path]) -> None:
        self._origen_ruta = ruta

    @property
    def ruta(self) -> pathlib.Path:
        origen = self._origen_ruta
        return pathlib.Path(origen() if callable(origen) else origen)

    def leer(self, clave: ClaveDeTestigo) -> TestigoDurable | None:
        ruta = self.ruta
        try:
            self._rechazar_enlaces(ruta)
            self._rechazar_rutas_irregulares(ruta)
            try:
                ruta.lstat()
            except FileNotFoundError:
                return None
            with self._seccion_critica(ruta, escritura=False):
                datos = self._cargar_objeto(ruta)
        except AlmacenDeTestigosIlegibleError as exc:
            logger.warning(
                "Almacén de testigos de etapa 6 ilegible (%s): %s. La lectura degrada a None.",
                ruta,
                exc,
                extra={"pipeline_stage": _ETAPA_WRYE_BASH, "tx_id": None},
            )
            return None
        except OSError as exc:
            logger.warning(
                "No se pudo inspeccionar el almacén de testigos de etapa 6 (%s): %s. La lectura degrada a None.",
                ruta,
                exc,
                extra={"pipeline_stage": _ETAPA_WRYE_BASH, "tx_id": None},
            )
            return None

        return self._interpretar_entrada(ruta, clave, datos)

    def _interpretar_entrada(
        self,
        ruta: pathlib.Path,
        clave: ClaveDeTestigo,
        datos: dict[str, object],
    ) -> TestigoDurable | None:
        crudo = datos.get(clave.como_texto())
        durable = TestigoDurable.desde_dict(crudo)
        if durable is None and crudo is not None:
            logger.warning(
                "Entrada inválida en %s para %s: se descarta sin afectar las demás.",
                ruta,
                clave.como_texto(),
                extra={"pipeline_stage": _ETAPA_WRYE_BASH, "tx_id": None},
            )
        return durable

    def guardar(
        self,
        clave: ClaveDeTestigo,
        testigo: TestigoPrevio,
        *,
        observado_en: str | None = None,
        huella_contexto: str | None = None,
    ) -> TestigoDurable:
        durable = TestigoDurable.desde(testigo, observado_en=observado_en, huella_contexto=huella_contexto)
        ruta = self.ruta
        with self._seccion_critica(ruta, escritura=True):
            datos = self._cargar_objeto(ruta)
            datos[clave.como_texto()] = durable.como_dict()
            self._escribir_atomico(ruta, datos)
        return durable

    def olvidar(self, clave: ClaveDeTestigo) -> None:
        ruta = self.ruta
        with self._seccion_critica(ruta, escritura=True):
            datos = self._cargar_objeto(ruta)
            if datos.pop(clave.como_texto(), None) is not None:
                self._escribir_atomico(ruta, datos)

    def sincronizar_preparacion(
        self,
        clave: ClaveDeTestigo,
        observar: Callable[[], TestigoPrevio | None],
        *,
        observado_en: str | None = None,
        contexto: Callable[[], str | None] | None = None,
    ) -> ResultadoDePreparacionDurable:
        """Ejecuta ``cargar → observar → decidir → escribir`` bajo un único lock del almacén.

        ``contexto`` (opcional) observa la huella del contexto de entrada del perfil DENTRO del
        lock, justo después de ``observar``: activa la vigencia contextual del testigo
        (#866, H-1). Sin él la transición es la de PR D y el registro queda sin huella.
        """
        ruta = self.ruta
        with self._seccion_critica(ruta, escritura=True):
            datos = self._cargar_objeto(ruta)
            actual = self._interpretar_entrada(ruta, clave, datos)
            observado_ahora = observar()
            decision, vigente, persistir = _resolver_preparacion_durable(
                actual=actual,
                observado_ahora=observado_ahora,
                observado_en=observado_en,
                verificar_contexto=contexto is not None,
                contexto_ahora=None if contexto is None else contexto(),
            )
            if persistir and vigente is not None:
                datos[clave.como_texto()] = vigente.como_dict()
                self._escribir_atomico(ruta, datos)
            return ResultadoDePreparacionDurable(decision=decision, durable=vigente)

    def olvidar_si_coincide(
        self,
        clave: ClaveDeTestigo,
        *,
        testigo: TestigoPrevio,
        generacion: str,
    ) -> bool:
        """Borra ``clave`` sólo si el registro actual coincide con ``(testigo, generacion)``."""
        _validar_generacion_explicita(generacion)
        ruta = self.ruta
        with self._seccion_critica(ruta, escritura=True):
            datos = self._cargar_objeto(ruta)
            actual = self._interpretar_entrada(ruta, clave, datos)
            if actual is None or not actual.coincide_con(testigo=testigo, generacion=generacion):
                return False
            datos.pop(clave.como_texto(), None)
            self._escribir_atomico(ruta, datos)
            return True

    @contextlib.contextmanager
    def _seccion_critica(self, ruta: pathlib.Path, *, escritura: bool) -> Iterator[None]:
        """Serializa toda la operación sobre ``ruta`` entre hilos/instancias y entre procesos.

        **Invariante de la lectura (L-1).** ``leer`` (``escritura=False``) también toma el lock
        del SO sobre ``.etapa6_testigos.json.lock``. Sólo lo omite —y queda cubierta por el lock
        intraproceso— cuando el lockfile no se puede abrir por ``EACCES``/``EPERM``/``EROFS``:
        un directorio de sólo lectura donde ningún escritor puede ejecutar ``os.replace``, así
        que no hay escritura concurrente que excluir.
        """
        with _lock_intraproceso(ruta):
            try:
                self._rechazar_enlaces(ruta)
                self._rechazar_rutas_irregulares(ruta)
            except AlmacenDeTestigosIlegibleError:
                raise
            except OSError as exc:
                raise AlmacenDeTestigosIlegibleError(f"no se pudo inspeccionar '{ruta}': {exc}") from exc

            if escritura:
                try:
                    ruta.parent.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise AlmacenDeTestigosIlegibleError(f"no se pudo preparar '{ruta.parent}': {exc}") from exc
                self._rechazar_enlaces(ruta)
                self._rechazar_rutas_irregulares(ruta)

            ruta_lock = _ruta_del_lockfile(ruta)
            banderas = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            try:
                fd_lock = os.open(ruta_lock, banderas, 0o600)
            except OSError as exc:
                if not escritura and exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                    # Directorio de sólo lectura en ``leer``: el lock intraproceso ya nos cubre.
                    yield
                    return
                raise AlmacenDeTestigosIlegibleError(f"no se pudo abrir el lockfile '{ruta_lock}': {exc}") from exc

            self._verificar_fd_regular(fd_lock, ruta_lock)

            adquirido = False
            try:
                limite = time.monotonic() + _TIMEOUT_LOCK_SEGUNDOS
                while True:
                    try:
                        if _intentar_lock_so(fd_lock):
                            adquirido = True
                            break
                    except OSError as exc:
                        raise AlmacenDeTestigosIlegibleError(f"fallo al bloquear '{ruta_lock}': {exc}") from exc
                    if time.monotonic() >= limite:
                        raise AlmacenDeTestigosIlegibleError(
                            f"timeout ({_TIMEOUT_LOCK_SEGUNDOS}s) esperando el lock de '{ruta}'"
                        )
                    time.sleep(_PASO_REINTENTO_SEGUNDOS)
                yield
            finally:
                if adquirido:
                    _liberar_lock_so(fd_lock)
                with contextlib.suppress(OSError):
                    os.close(fd_lock)

    @staticmethod
    def _rechazar_enlaces(ruta: pathlib.Path) -> None:
        for candidato in (ruta.parent, ruta, _ruta_del_lockfile(ruta)):
            tipo = link_kind_or_raise(candidato)
            if tipo is not None:
                raise AlmacenDeTestigosIlegibleError(
                    f"'{candidato}' es un {tipo}: el almacén de testigos no atraviesa enlaces"
                )

    @staticmethod
    def _rechazar_rutas_irregulares(ruta: pathlib.Path) -> None:
        """Exige directorio en ``ruta.parent`` y archivo regular en ``ruta`` y su ``.lock``."""
        try:
            st_padre = os.lstat(ruta.parent)
        except FileNotFoundError:
            st_padre = None
        if st_padre is not None and not stat.S_ISDIR(st_padre.st_mode):
            raise AlmacenDeTestigosIlegibleError(f"'{ruta.parent}' existe pero no es un directorio regular")
        for archivo in (ruta, _ruta_del_lockfile(ruta)):
            try:
                st = os.lstat(archivo)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(st.st_mode):
                raise AlmacenDeTestigosIlegibleError(
                    f"'{archivo}' no es un archivo regular: el almacén de testigos rechaza archivos especiales"
                )

    @staticmethod
    def _verificar_fd_regular(fd: int, ruta: pathlib.Path) -> None:
        """Cierra la ventana TOCTOU ``lstat → open`` verificando ``fstat(fd)``."""
        try:
            st_fd = os.fstat(fd)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise AlmacenDeTestigosIlegibleError(f"no se pudo inspeccionar el descriptor de '{ruta}': {exc}") from exc
        if not stat.S_ISREG(st_fd.st_mode):
            with contextlib.suppress(OSError):
                os.close(fd)
            raise AlmacenDeTestigosIlegibleError(
                f"'{ruta}' dejó de ser un archivo regular al abrirlo: se rechaza sin leer"
            )

    def _cargar_objeto(self, ruta: pathlib.Path) -> dict[str, object]:
        """Devuelve ``{}`` si el archivo aún no existe; lanza :class:`AlmacenDeTestigosIlegibleError`
        si existe y no es un objeto JSON regular (para que ni lector ni escritor lo pisen)."""
        try:
            self._rechazar_enlaces(ruta)
            self._rechazar_rutas_irregulares(ruta)
        except AlmacenDeTestigosIlegibleError:
            raise
        except OSError as exc:
            raise AlmacenDeTestigosIlegibleError(f"no se pudo inspeccionar '{ruta}': {exc}") from exc

        banderas = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(ruta, banderas)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise AlmacenDeTestigosIlegibleError(f"no se pudo abrir '{ruta}': {exc}") from exc

        self._verificar_fd_regular(fd, ruta)
        if sys.platform != "win32" and hasattr(os, "O_NONBLOCK"):
            with contextlib.suppress(OSError):
                flags_actuales = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, flags_actuales & ~os.O_NONBLOCK)

        try:
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                crudo = json.load(fh)
        except (OSError, ValueError) as exc:
            raise AlmacenDeTestigosIlegibleError(f"no se pudo decodificar '{ruta}': {exc}") from exc

        if not isinstance(crudo, dict):
            raise AlmacenDeTestigosIlegibleError(f"'{ruta}' no contiene un objeto JSON en la raíz")
        return crudo

    def _escribir_atomico(self, ruta: pathlib.Path, datos: dict[str, object]) -> None:
        """Reemplazo atómico bajo la sección crítica. Todo fallo de E/S sale como
        :class:`AlmacenDeTestigosIlegibleError` (``__cause__`` conserva el ``OSError``)."""
        try:
            ruta.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AlmacenDeTestigosIlegibleError(f"no se pudo preparar '{ruta.parent}': {exc}") from exc
        self._rechazar_enlaces(ruta)
        self._rechazar_rutas_irregulares(ruta)
        self._barrer_temporales_huerfanos(ruta)
        try:
            tmp_fd, tmp_name = tempfile.mkstemp(dir=str(ruta.parent), prefix=f".{ruta.stem}.", suffix=".tmp")
        except OSError as exc:
            raise AlmacenDeTestigosIlegibleError(f"no se pudo crear temporal en '{ruta.parent}': {exc}") from exc
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(datos, fh, ensure_ascii=False, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            _reemplazar_con_reintento(tmp_name, ruta)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        _fsync_directorio_padre(ruta.parent)

    @staticmethod
    def _barrer_temporales_huerfanos(ruta: pathlib.Path) -> None:
        """Borra los ``.<stem>.*.tmp`` que dejó un proceso muerto entre ``mkstemp`` y ``os.replace``.

        Sólo es seguro porque TODA escritura de este almacén ocurre bajo ``_seccion_critica``:
        quien llega acá tiene el lock exclusivo, así que ningún temporal presente puede ser de
        una escritura en vuelo. Sólo se tocan archivos **regulares** (``lstat``, sin seguir
        enlaces); un enlace o un directorio con ese nombre se deja como está. Best-effort: un
        fallo al barrer no impide escribir.
        """
        prefijo = f".{ruta.stem}."
        try:
            candidatos = [p for p in ruta.parent.iterdir() if p.name.startswith(prefijo) and p.name.endswith(".tmp")]
        except OSError:
            return
        for candidato in candidatos:
            try:
                if stat.S_ISREG(os.lstat(candidato).st_mode):
                    os.unlink(candidato)
            except OSError:
                logger.warning(
                    "No se pudo barrer el temporal huérfano %s del almacén de testigos.",
                    candidato,
                    extra={"pipeline_stage": _ETAPA_WRYE_BASH, "tx_id": None},
                )


def _es_windows() -> bool:
    """Indirección de ``sys.platform`` para poder ejercitar el reintento de Windows en POSIX."""
    return sys.platform == "win32"


def _es_violacion_de_uso_compartido(exc: OSError) -> bool:
    """True sólo para un ``PermissionError`` de Windows con ``winerror`` 5/32 (transitorio)."""
    return (
        _es_windows() and isinstance(exc, PermissionError) and getattr(exc, "winerror", None) in _WINERROR_TRANSITORIOS
    )


def _reemplazar_con_reintento(origen: str, destino: pathlib.Path) -> None:
    """``os.replace`` con reintento acotado ante una violación de uso compartido en Windows.

    Defender o el indexador pueden tener el JSON abierto unos milisegundos sin
    ``FILE_SHARE_DELETE``; reintentar con backoff lo absorbe sin degradar la corrida a
    ``error_de_almacen``. Fuera de Windows, o ante cualquier otro error, no hay reintento:
    se levanta :class:`AlmacenDeTestigosIlegibleError` al primer fallo. Agotado el
    presupuesto también (con el último ``OSError`` como ``__cause__``).
    """
    espera = _PASO_DE_REEMPLAZO_SEGUNDOS
    for intento in range(_REINTENTOS_DE_REEMPLAZO + 1):
        try:
            os.replace(origen, destino)
            return
        except OSError as exc:
            if intento == _REINTENTOS_DE_REEMPLAZO or not _es_violacion_de_uso_compartido(exc):
                raise AlmacenDeTestigosIlegibleError(f"no se pudo reemplazar '{destino}': {exc}") from exc
            time.sleep(espera)
            espera *= 2


def _fsync_directorio_padre(directorio: pathlib.Path) -> None:
    """Hace durable la entrada de directorio tras ``os.replace`` en POSIX (best-effort)."""
    if sys.platform == "win32":
        return
    banderas = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd_dir = os.open(directorio, banderas)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(fd_dir)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd_dir)


def almacen_por_defecto() -> AlmacenJSON:
    """Almacén productivo en ``SystemPaths.runtime_state_dir() / "etapa6_testigos.json"``.

    Resuelve ``runtime_state_dir()`` al momento de operar (no al construir) para respetar
    la regla de composición perezosa del orquestador y el aislamiento por test de
    ``tests/conftest.py``.
    """
    return AlmacenJSON(lambda: SystemPaths.runtime_state_dir() / NOMBRE_DEL_ARCHIVO)


__all__ = [
    "NOMBRE_DEL_ARCHIVO",
    "AlmacenDeTestigos",
    "AlmacenDeTestigosIlegibleError",
    "AlmacenEnMemoria",
    "AlmacenJSON",
    "ClaveDeTestigo",
    "ResultadoDePreparacionDurable",
    "TestigoDurable",
    "almacen_por_defecto",
    "clave_de_testigo",
    "nueva_generacion",
]
