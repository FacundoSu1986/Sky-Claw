"""Testigo durable de la fase 1 de la etapa 6 (ADR 0016 §6/§7, PR D).

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

**Dónde vive.** En :meth:`~sky_claw.config.SystemPaths.runtime_state_dir`
(``~/.sky_claw/state/etapa6_testigos.json``), resuelto al operar y **no** en
``.skyclaw_backups/``: dos procesos lanzados desde directorios de trabajo distintos tienen
que ver el mismo testigo (ADR 0011 P0.2), y en los tests la fixture autouse de
``conftest.py`` lo aísla por caso sin ensuciar el árbol del repo.

**Regla de seguridad.** El testigo durable es una *observación previa*, nunca autoridad.
Si el archivo es un enlace o está corrupto, :meth:`AlmacenJSON.leer` devuelve ``None`` y
la etapa degrada a ``frescura_probada=False`` sin tocar el archivo; :meth:`AlmacenJSON.guardar`
y :meth:`AlmacenJSON.olvidar` tampoco pisan un archivo ilegible —levantan
:class:`AlmacenDeTestigosIlegibleError` para que el servicio declare
``error_de_almacen`` y preserve el archivo para diagnóstico—.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import pathlib
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from sky_claw.app.security.links import link_kind_or_raise
from sky_claw.config import SystemPaths
from sky_claw.local.tools.wrye_bash_asistida import TestigoPrevio

logger = logging.getLogger(__name__)

NOMBRE_DEL_ARCHIVO = "etapa6_testigos.json"


class AlmacenDeTestigosIlegibleError(OSError):
    """El archivo del almacén existe pero no es un objeto JSON regular y legible.

    Hereda de :class:`OSError` para que el servicio pueda tratar en un solo borde los
    fallos de almacenamiento sin confundirlos con errores de programación. Se levanta en
    escritura/borrado para **no sobrescribir** un archivo corrupto ni atravesar enlaces.
    """


def _ahora() -> str:
    """Marca de tiempo ISO-8601 en UTC con precisión de segundos."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonizar_ruta(ruta: pathlib.Path) -> str:
    """``resolve(strict=False)`` + ``normpath`` + ``normcase``, igual que ``handoffs.clave_de_artifact``."""
    return os.path.normcase(os.path.normpath(str(pathlib.Path(ruta).resolve(strict=False))))


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
    """Un :class:`TestigoPrevio` con la marca de cuándo se observó.

    Delega el invariante de dominio en :class:`TestigoPrevio` para que no existan dos
    validadores que puedan divergir (lección S-10 de ADR 0016 §9).
    """

    __test__ = False

    existia: bool
    sha256: str | None
    observado_en: str

    def __post_init__(self) -> None:
        # ``isinstance(1, int)`` es True pero ``isinstance(1, bool)`` es False: sin este
        # guard ``TestigoPrevio`` aceptaría ``1`` como verdad booleana.
        if not isinstance(self.existia, bool):
            raise ValueError(f"existia debe ser bool, no {type(self.existia).__name__}")
        if self.sha256 is not None and not isinstance(self.sha256, str):
            raise ValueError(f"sha256 debe ser str o None, no {type(self.sha256).__name__}")
        TestigoPrevio(existia=self.existia, sha256=self.sha256)
        if not isinstance(self.observado_en, str) or not self.observado_en:
            raise ValueError("un testigo durable necesita marca de tiempo no vacía")

    @classmethod
    def desde(cls, testigo: TestigoPrevio, *, observado_en: str | None = None) -> TestigoDurable:
        return cls(existia=testigo.existia, sha256=testigo.sha256, observado_en=observado_en or _ahora())

    def como_testigo_previo(self) -> TestigoPrevio:
        return TestigoPrevio(existia=self.existia, sha256=self.sha256)

    def como_dict(self) -> dict[str, object]:
        return {"existia": self.existia, "sha256": self.sha256, "observado_en": self.observado_en}

    @classmethod
    def desde_dict(cls, crudo: object) -> TestigoDurable | None:
        """Forma canónica o ``None``. Nunca lanza sobre datos leídos de disco."""
        if not isinstance(crudo, dict):
            return None
        existia = crudo.get("existia")
        sha256 = crudo.get("sha256")
        observado_en = crudo.get("observado_en")
        if not isinstance(existia, bool) or not (sha256 is None or isinstance(sha256, str)):
            return None
        if not isinstance(observado_en, str) or not observado_en:
            return None
        try:
            return cls(existia=existia, sha256=sha256, observado_en=observado_en)
        except ValueError:
            return None


@runtime_checkable
class AlmacenDeTestigos(Protocol):
    """Superficie mínima que consume ``WryeBashPipelineService``."""

    def leer(self, clave: ClaveDeTestigo) -> TestigoDurable | None: ...

    def guardar(
        self, clave: ClaveDeTestigo, testigo: TestigoPrevio, *, observado_en: str | None = None
    ) -> TestigoDurable: ...

    def olvidar(self, clave: ClaveDeTestigo) -> None: ...


class AlmacenEnMemoria:
    """Almacén de proceso para pruebas unitarias sin tocar el filesystem."""

    def __init__(self) -> None:
        self._datos: dict[str, TestigoDurable] = {}
        self._lock = threading.RLock()

    def leer(self, clave: ClaveDeTestigo) -> TestigoDurable | None:
        with self._lock:
            return self._datos.get(clave.como_texto())

    def guardar(
        self, clave: ClaveDeTestigo, testigo: TestigoPrevio, *, observado_en: str | None = None
    ) -> TestigoDurable:
        durable = TestigoDurable.desde(testigo, observado_en=observado_en)
        with self._lock:
            self._datos[clave.como_texto()] = durable
        return durable

    def olvidar(self, clave: ClaveDeTestigo) -> None:
        with self._lock:
            self._datos.pop(clave.como_texto(), None)


class AlmacenJSON:
    """Archivo JSON único por usuario con escritura atómica (``mkstemp`` + ``os.replace``).

    Acepta una ruta fija o un callable sin argumentos (como usa :func:`almacen_por_defecto`)
    para que construir el servicio no evalúe rutas ni toque el disco en frío.
    """

    def __init__(self, ruta: pathlib.Path | Callable[[], pathlib.Path]) -> None:
        self._origen_ruta = ruta
        self._lock = threading.RLock()

    @property
    def ruta(self) -> pathlib.Path:
        origen = self._origen_ruta
        return pathlib.Path(origen() if callable(origen) else origen)

    def leer(self, clave: ClaveDeTestigo) -> TestigoDurable | None:
        ruta = self.ruta
        with self._lock:
            try:
                datos = self._cargar_objeto(ruta)
            except AlmacenDeTestigosIlegibleError as exc:
                logger.warning(
                    "Almacén de testigos de etapa 6 ilegible (%s): %s. La lectura degrada a None.",
                    ruta,
                    exc,
                    extra={"pipeline_stage": "wrye_bash"},
                )
                return None
            crudo = datos.get(clave.como_texto())
        durable = TestigoDurable.desde_dict(crudo)
        if durable is None and crudo is not None:
            logger.warning(
                "Entrada inválida en %s para %s: se descarta sin afectar las demás.",
                ruta,
                clave.como_texto(),
                extra={"pipeline_stage": "wrye_bash"},
            )
        return durable

    def guardar(
        self, clave: ClaveDeTestigo, testigo: TestigoPrevio, *, observado_en: str | None = None
    ) -> TestigoDurable:
        durable = TestigoDurable.desde(testigo, observado_en=observado_en)
        ruta = self.ruta
        with self._lock:
            datos = self._cargar_objeto(ruta)
            datos[clave.como_texto()] = durable.como_dict()
            self._escribir_atomico(ruta, datos)
        return durable

    def olvidar(self, clave: ClaveDeTestigo) -> None:
        ruta = self.ruta
        with self._lock:
            datos = self._cargar_objeto(ruta)
            if datos.pop(clave.como_texto(), None) is not None:
                self._escribir_atomico(ruta, datos)

    @staticmethod
    def _rechazar_enlaces(ruta: pathlib.Path) -> None:
        for candidato in (ruta.parent, ruta):
            tipo = link_kind_or_raise(candidato)
            if tipo is not None:
                raise AlmacenDeTestigosIlegibleError(
                    f"'{candidato}' es un {tipo}: el almacén de testigos no atraviesa enlaces"
                )

    def _cargar_objeto(self, ruta: pathlib.Path) -> dict[str, object]:
        """Devuelve ``{}`` si el archivo aún no existe; lanza :class:`AlmacenDeTestigosIlegibleError`
        si existe y no es un objeto JSON regular (para que ni lector ni escritor lo pisen)."""
        try:
            self._rechazar_enlaces(ruta)
        except AlmacenDeTestigosIlegibleError:
            raise
        except OSError as exc:
            raise AlmacenDeTestigosIlegibleError(f"no se pudo inspeccionar '{ruta}': {exc}") from exc

        try:
            with ruta.open("r", encoding="utf-8") as fh:
                crudo = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise AlmacenDeTestigosIlegibleError(f"no se pudo decodificar '{ruta}': {exc}") from exc

        if not isinstance(crudo, dict):
            raise AlmacenDeTestigosIlegibleError(f"'{ruta}' no contiene un objeto JSON en la raíz")
        return crudo

    def _escribir_atomico(self, ruta: pathlib.Path, datos: dict[str, object]) -> None:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        self._rechazar_enlaces(ruta)
        try:
            tmp_fd, tmp_name = tempfile.mkstemp(dir=str(ruta.parent), prefix=f".{ruta.stem}.", suffix=".tmp")
        except OSError as exc:
            raise AlmacenDeTestigosIlegibleError(f"no se pudo crear temporal en '{ruta.parent}': {exc}") from exc
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(datos, fh, ensure_ascii=False, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, ruta)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise


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
    "TestigoDurable",
    "almacen_por_defecto",
    "clave_de_testigo",
]
