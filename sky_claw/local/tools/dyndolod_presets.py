"""Presets de TexGen/DynDOLOD en la etapa 9 desatendida: verificación y restauración (#661 P5/P8).

**Por qué existe.** La configuración de una corrida desatendida —preset de
calidad, worldspaces, parámetros de TexGen— la carga cada herramienta de SU
preset (``<exe>\\Edit Scripts\\DynDOLOD\\Presets\\DynDOLOD_SSE_*.ini``). Sky-Claw
no los edita. Pero P0-B midió dos hechos que obligan a tratarlos:

* las dos herramientas **reescriben su preset al iniciar** una corrida (TexGen:
  sólo ``OutputPath``; DynDOLOD: +46 bytes): estado del usuario modificado por
  una corrida que Sky-Claw lanzó, así que Sky-Claw lo restaura byte-exacto;
* el ``OutputPath`` del preset es lo único que Sky-Claw corrige por UIA, así que
  la **huella de configuración** que el operador aprueba lo excluye: es SHA-256
  del archivo sin sus líneas ``OutputPath=``. Todo lo demás —incluida la lista de
  worldspaces— queda cubierto por la huella.

**Durable, no un ``finally``.** :meth:`GuardaDePresets.proteger` escribe el
snapshot (bytes + SHA-256, o ``ABSENT``) en ``runtime_state_dir`` ANTES del
spawn; :meth:`GuardaDePresets.restaurar` lo aplica y lo verifica por SHA al
terminar, y recién entonces borra el registro. Si Sky-Claw muere en el medio, el
registro sobrevive y :meth:`GuardaDePresets.reconciliar` lo aplica al comienzo de
la próxima corrida (bajo el mismo lock de etapa 9). Un restore que no se puede
probar es :class:`PresetError` y la corrida no se declara exitosa.

**Límite declarado.** Si el operador abre la herramienta a mano mientras queda un
registro pendiente, la reconciliación siguiente le pisa el preset con el del
snapshot. Es el estado previo a la corrida de Sky-Claw, que es exactamente lo que
el contrato promete devolver.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import pathlib
import tempfile
from dataclasses import dataclass

from sky_claw.app.security.links import link_kind_or_raise, rmtree_link_aware

logger = logging.getLogger(__name__)

#: Un preset real pesa decenas de KB (Default: 24 061 B). La cota existe para
#: que un archivo hostil no se cargue entero en memoria ni se copie al estado.
TAMANO_MAXIMO_DE_PRESET = 4 * 1024 * 1024

#: Versión del schema del registro durable. Cerrado: un campo de más es corrupto.
VERSION_DEL_REGISTRO = 1
_CAMPOS_DEL_REGISTRO = ("version", "ruta", "presente", "sha256", "tamano")
_NOMBRE_DEL_REGISTRO = "registro.json"
_NOMBRE_DEL_CONTENIDO = "contenido.bin"
_CLAVE_EXCLUIDA = b"outputpath"


class PresetError(Exception):
    """Un preset no se pudo leer, proteger o restaurar de forma demostrable."""


class PresetCambiadoError(PresetError):
    """El preset en disco ya no es la configuración aprobada: no se snapshotea ni se lanza."""


@dataclass(frozen=True, slots=True)
class EstadoDePreset:
    """Lo que se sabe de un preset en un instante. ``presente=False`` es ``ABSENT``."""

    ruta: pathlib.Path
    presente: bool
    sha256: str | None = None
    huella: str | None = None
    preset_de_calidad: str | None = None
    tamano: int | None = None


def _sin_lineas_de_output(contenido: bytes) -> bytes:
    conservadas = []
    for linea in contenido.split(b"\n"):
        clave = linea.split(b"=", 1)[0].strip().lower() if b"=" in linea else b""
        if clave == _CLAVE_EXCLUIDA:
            continue
        conservadas.append(linea)
    return b"\n".join(conservadas)


def huella_de_configuracion(contenido: bytes) -> str:
    """SHA-256 del preset SIN sus líneas ``OutputPath=`` (bytes, sin decodificar).

    Trabaja sobre bytes a propósito: la codificación del INI no está medida y
    decodificar para comparar inventaría equivalencias. La clave se compara sin
    mayúsculas y sin espacios alrededor; el resto del archivo, byte a byte.
    """
    return hashlib.sha256(_sin_lineas_de_output(contenido)).hexdigest()


def valor_de_clave(contenido: bytes, *, seccion: str, clave: str) -> str | None:
    """``clave=valor`` dentro de ``[seccion]``, o ``None``. Sólo para lectura/aviso.

    Decodifica latin-1 (sin pérdida byte→carácter) y compara sección y clave sin
    mayúsculas. Dos apariciones de la misma clave en la sección son ambigüedad:
    ``None``, para que el llamador no elija una.
    """
    texto = contenido.decode("latin-1")
    actual: str | None = None
    encontrados: list[str] = []
    for linea_cruda in texto.splitlines():
        linea = linea_cruda.strip()
        if linea.startswith("[") and linea.endswith("]"):
            actual = linea[1:-1].strip().lower()
            continue
        if actual != seccion.lower() or "=" not in linea:
            continue
        nombre, valor = linea.split("=", 1)
        if nombre.strip().lower() == clave.lower():
            encontrados.append(valor.strip())
    return encontrados[0] if len(encontrados) == 1 else None


def _exigir_archivo_real(ruta: pathlib.Path) -> None:
    try:
        tipo = link_kind_or_raise(ruta)
    except OSError as exc:
        raise PresetError(f"no se pudo inspeccionar {ruta}: {exc}") from exc
    if tipo is not None:
        raise PresetError(f"{ruta} es un {tipo}: Sky-Claw no verifica ni restaura presets a través de enlaces")


def _leer_acotado(ruta: pathlib.Path) -> bytes:
    try:
        with ruta.open("rb") as archivo:
            contenido = archivo.read(TAMANO_MAXIMO_DE_PRESET + 1)
    except OSError as exc:
        raise PresetError(f"no se pudo leer {ruta}: {exc}") from exc
    if len(contenido) > TAMANO_MAXIMO_DE_PRESET:
        raise PresetError(f"{ruta} supera {TAMANO_MAXIMO_DE_PRESET} bytes: no es un preset")
    return contenido


def leer_estado_de_preset(
    ruta: pathlib.Path,
    *,
    seccion: str,
    clave_de_calidad: str | None,
) -> EstadoDePreset:
    """Estado del preset en disco. Un enlace o un archivo ilegible es :class:`PresetError`."""
    _exigir_archivo_real(ruta)
    if not ruta.exists():
        return EstadoDePreset(ruta=ruta, presente=False)
    if not ruta.is_file():
        raise PresetError(f"{ruta} existe y no es un archivo")
    contenido = _leer_acotado(ruta)
    return EstadoDePreset(
        ruta=ruta,
        presente=True,
        sha256=hashlib.sha256(contenido).hexdigest(),
        huella=huella_de_configuracion(contenido),
        preset_de_calidad=(
            valor_de_clave(contenido, seccion=seccion, clave=clave_de_calidad) if clave_de_calidad else None
        ),
        tamano=len(contenido),
    )


@dataclass(frozen=True, slots=True)
class RegistroDePreset:
    """El snapshot durable de UN preset, tal como quedó escrito en el estado."""

    directorio: pathlib.Path
    ruta: pathlib.Path
    presente: bool
    sha256: str | None
    tamano: int | None


def _escribir_atomico(destino: pathlib.Path, contenido: bytes) -> None:
    """Temporal en el MISMO directorio + fsync + ``os.replace`` (nunca trunca el destino)."""
    descriptor, temporal = tempfile.mkstemp(prefix=f".{destino.name}.", suffix=".skyclaw-tmp", dir=destino.parent)
    try:
        with os.fdopen(descriptor, "wb") as archivo:
            archivo.write(contenido)
            archivo.flush()
            os.fsync(archivo.fileno())
        os.replace(temporal, destino)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporal)
        raise


def _clave_de_ruta(ruta: pathlib.Path) -> str:
    return hashlib.sha256(os.path.normcase(str(ruta.absolute())).encode("utf-8")).hexdigest()[:32]


class GuardaDePresets:
    """Snapshot durable antes del spawn, restore verificado después, reconciliación de residuos."""

    def __init__(self, directorio_estado: pathlib.Path) -> None:
        self._directorio = directorio_estado

    @property
    def directorio(self) -> pathlib.Path:
        return self._directorio

    def _leer_registro(self, directorio: pathlib.Path) -> RegistroDePreset:
        try:
            crudo = json.loads((directorio / _NOMBRE_DEL_REGISTRO).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PresetError(f"registro de preset ilegible en {directorio}: {exc}") from exc
        if not isinstance(crudo, dict) or set(crudo) != set(_CAMPOS_DEL_REGISTRO):
            raise PresetError(f"registro de preset con schema desconocido en {directorio}")
        version, ruta, presente = crudo["version"], crudo["ruta"], crudo["presente"]
        sha, tamano = crudo["sha256"], crudo["tamano"]
        if version != VERSION_DEL_REGISTRO or not isinstance(ruta, str) or not isinstance(presente, bool):
            raise PresetError(f"registro de preset inválido en {directorio}")
        if presente and (not isinstance(sha, str) or isinstance(tamano, bool) or not isinstance(tamano, int)):
            raise PresetError(f"registro PRESENT sin sha/tamaño en {directorio}")
        if not presente and (sha is not None or tamano is not None):
            raise PresetError(f"registro ABSENT con contenido en {directorio}")
        return RegistroDePreset(
            directorio=directorio,
            ruta=pathlib.Path(ruta),
            presente=presente,
            sha256=sha if presente else None,
            tamano=tamano if presente else None,
        )

    def pendientes(self) -> list[RegistroDePreset]:
        """Registros que quedaron sin restaurar (corrida que murió a mitad)."""
        if not self._directorio.exists():
            return []
        try:
            directorios = sorted(p for p in self._directorio.iterdir() if p.is_dir())
        except OSError as exc:
            raise PresetError(f"no se pudo listar el estado de presets {self._directorio}: {exc}") from exc
        registros: list[RegistroDePreset] = []
        for directorio in directorios:
            if not (directorio / _NOMBRE_DEL_REGISTRO).exists():
                # El registro se escribe ÚLTIMO y `proteger` recién devuelve
                # después: sin registro no hubo spawn, así que el directorio no
                # protege nada (o es lo que dejó un restore ya verificado). Si
                # bloqueara, toda corrida futura —asistida incluida— moriría en
                # PRESET_RESTORE_FAILED hasta un borrado manual.
                logger.warning(
                    "snapshot de preset incompleto descartado (sin registro): %s",
                    directorio,
                    extra={"operation_type": "dyndolod_preset_snapshot_incompleto"},
                )
                self._descartar(directorio)
                continue
            registros.append(self._leer_registro(directorio))
        return registros

    @staticmethod
    def _descartar(directorio: pathlib.Path) -> None:
        try:
            rmtree_link_aware(directorio)
        except OSError as exc:
            raise PresetError(f"no se pudo descartar el snapshot incompleto {directorio}: {exc}") from exc

    def proteger(self, ruta: pathlib.Path, *, huella_aprobada: str | None) -> RegistroDePreset:
        """Escribe el snapshot durable de ``ruta`` (bytes + SHA, o ABSENT). ANTES del spawn.

        ``huella_aprobada`` es OBLIGATORIA: la huella que el operador aprobó, o
        ``None`` si lo aprobado es un preset ABSENT. Se compara contra los MISMOS
        bytes que se snapshotean; si no coinciden es :class:`PresetCambiadoError` y
        no se escribe nada. Así un cambio entre la verificación y el snapshot no
        termina snapshoteado —y después restaurado como estado del usuario— sin
        que nadie lo haya aprobado.
        """
        _exigir_archivo_real(ruta)
        directorio = self._directorio / _clave_de_ruta(ruta)
        if directorio.exists():
            raise PresetError(f"hay un snapshot sin reconciliar para {ruta}: se reconcilia antes de una corrida nueva")
        presente = ruta.exists()
        contenido = _leer_acotado(ruta) if presente else b""
        huella_en_disco = huella_de_configuracion(contenido) if presente else None
        if huella_en_disco != huella_aprobada:
            raise PresetCambiadoError(f"el preset {ruta} ya no es la configuración aprobada: no se snapshotea")
        sha = hashlib.sha256(contenido).hexdigest() if presente else None
        registro = RegistroDePreset(
            directorio=directorio,
            ruta=ruta,
            presente=presente,
            sha256=sha,
            tamano=len(contenido) if presente else None,
        )
        creado = False
        try:
            directorio.mkdir(parents=True, exist_ok=False)
            creado = True
            if presente:
                _escribir_atomico(directorio / _NOMBRE_DEL_CONTENIDO, contenido)
            # El registro va DESPUÉS del contenido: un registro presente siempre
            # tiene su contenido completo detrás.
            _escribir_atomico(
                directorio / _NOMBRE_DEL_REGISTRO,
                json.dumps(
                    {
                        "version": VERSION_DEL_REGISTRO,
                        "ruta": str(ruta),
                        "presente": presente,
                        "sha256": sha,
                        "tamano": registro.tamano,
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
            )
        except BaseException as exc:
            # Sin registro no hubo spawn: el directorio a medias no protege nada y
            # bloquearía toda reconciliación futura. Sólo se borra lo que creó
            # ESTA llamada (`creado`), nunca un directorio ajeno.
            if creado:
                with contextlib.suppress(OSError):
                    rmtree_link_aware(directorio)
            if isinstance(exc, OSError):
                raise PresetError(f"no se pudo escribir el snapshot de {ruta}: {exc}") from exc
            raise
        logger.info(
            "preset protegido antes del spawn: %s (%s)",
            ruta,
            "PRESENT " + (sha or "")[:12] if presente else "ABSENT",
            extra={"operation_type": "dyndolod_preset_protegido"},
        )
        return registro

    def restaurar(self, registro: RegistroDePreset) -> None:
        """Devuelve el preset al snapshot y lo PRUEBA por SHA; recién ahí borra el registro."""
        ruta = registro.ruta
        _exigir_archivo_real(ruta)
        try:
            if registro.presente:
                contenido = (registro.directorio / _NOMBRE_DEL_CONTENIDO).read_bytes()
                if hashlib.sha256(contenido).hexdigest() != registro.sha256:
                    raise PresetError(f"el snapshot de {ruta} no coincide con su SHA registrado: no se restaura")
                actual = _leer_acotado(ruta) if ruta.is_file() else None
                if actual is None or hashlib.sha256(actual).hexdigest() != registro.sha256:
                    ruta.parent.mkdir(parents=True, exist_ok=True)
                    _escribir_atomico(ruta, contenido)
                if hashlib.sha256(_leer_acotado(ruta)).hexdigest() != registro.sha256:
                    raise PresetError(f"el preset {ruta} no quedó con el SHA del snapshot")
            else:
                if ruta.exists():
                    ruta.unlink()
                if ruta.exists():
                    raise PresetError(f"el preset {ruta} tenía que quedar ABSENT y sigue existiendo")
        except OSError as exc:
            raise PresetError(f"no se pudo restaurar {ruta}: {exc}") from exc
        try:
            rmtree_link_aware(registro.directorio)
        except OSError as exc:
            # El preset ya está probado; un registro que no se pudo borrar se
            # reaplica (idempotente) en la próxima reconciliación.
            logger.warning(
                "preset restaurado pero su registro no se pudo borrar (%s): %s",
                registro.directorio,
                exc,
                extra={"operation_type": "dyndolod_preset_registro_sin_borrar"},
            )
        logger.info(
            "preset restaurado y verificado: %s",
            ruta,
            extra={"operation_type": "dyndolod_preset_restaurado"},
        )

    def reconciliar(self) -> list[pathlib.Path]:
        """Restaura TODO residuo pendiente. Un fallo cualquiera es :class:`PresetError`."""
        restaurados: list[pathlib.Path] = []
        for registro in self.pendientes():
            self.restaurar(registro)
            restaurados.append(registro.ruta)
        return restaurados
