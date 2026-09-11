"""Propiedad, admisión y coordinación del `external_work_root` (P0 de ADR 0011).

**Qué entrega este módulo y qué NO.** Entrega la CAPACIDAD completa del work
root externo —configurar, validar, vincular durablemente a una instancia lógica,
demostrar propiedad, coordinar cross-process, recordar cuál root está activo y
rechazar colisiones, bindings ajenos y estados ambiguos— y **nada más**. La
ACTIVACIÓN es PR-2: mientras tanto el runner sigue emitiendo el `-o:` productivo
de siempre (`output_targets.dyndolod_output_target`) y este módulo no aparece en
su argv. La propiedad está enunciada como invariante verificable, no como
promesa:

```text
P0 CAPABILITY != PR-2 ACTIVATION
```

y la ancla `tests/test_dyndolod_workspace.py::test_p0_no_cambia_el_output_productivo_del_runner`
rompe si un cambio de acá se filtra al `-o:` real.

**Por qué es un módulo y no lógica repartida.** El lifecycle del root cruza
cuatro preguntas que hoy nadie contesta junto: dónde vive (admisión), quién es
el dueño (binding), qué pasa ante colisión (máquina de estados) y cómo se
serializa entre procesos (coordinación). Repartirlas era garantizar el defecto
dominante del repo —arreglar un camino y dejar el gemelo—, porque cada
superficie que resolviera "¿puedo usar este root?" por su cuenta contestaría
distinto. Acá se contesta una vez.

**Lo que NO es.** No es un segundo `Config` (la preferencia vive en
`Config._data["external_work_root"]` y se persiste con el merge-on-save de
siempre), ni un segundo `PathValidator` (la contención la sigue haciendo
`PathValidator.validate` con `strict_symlink` y las primitivas de
`app/security/links.py`), ni un segundo gestor de locks (la exclusión
cross-process la da el `DistributedLockManager` que ya serializa los rituales).
Lo único que se construye nuevo es lo que no existía: la identidad de Windows
Known Folders (`app/security/known_folders.py`), el binding y su máquina de
estados, y el registro durable de root activo.

**Orden de adquisición** (§21 del contrato de P0; no hay ciclos):

```text
dyndolod-workspace            coordinación de propiedad (DB durable, sólo arranque)
        ↓
dyndolod-ownership            lease LARGA de ownership del snapshot vivo (P2.0)
        ↓
dyndolod-pipeline             coordinación del ritual   (DB durable)
        ↓
snapshot-transaction-lock     lock transaccional + snapshots del servicio
        ↓
journal                      transacción de operaciones
        ↓
directory-rollback            move-aside de los destinos
        ↓
handoff/recovery              reconciliación y restauración
```

Ese orden vive como DATO en :data:`ORDEN_DE_ADQUISICION`, no sólo como prosa,
para que un test lo pueda enumerar: una reordenación silenciosa es un deadlock
esperando a que dos caminos la tomen al revés. Se libera en orden inverso, y
**nunca** se suelta la coordinación mientras un proceso hijo, un rollback o una
recuperación siguen mutando.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import dataclasses
import enum
import hashlib
import json
import logging
import os
import pathlib
import sys
import tempfile
import time
import uuid
from typing import TYPE_CHECKING, Any, Final

from sky_claw.app.db.locks import (
    DistributedLockManager,
    LockAcquisitionError,
    LockLeaseLostError,
    SnapshotTransactionLock,
)
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.app.security import known_folders
from sky_claw.app.security.links import is_link
from sky_claw.app.security.path_validator import PathValidator, PathViolationError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

logger = logging.getLogger(__name__)

#: Nombre del archivo contractual de propiedad. Vive en la RAÍZ del work root,
#: fuera de `DynDOLOD/TexGen` y `DynDOLOD/DynDOLOD`, porque esos dos subárboles
#: serán targets de move-aside/born-empty en PR-2: metadata dentro de un
#: directorio que se renombra entero es metadata que se pierde en el primer
#: rollback.
ARCHIVO_DE_BINDING: Final[str] = ".sky-claw-binding.json"

#: Versión del schema. Congelada: extender exige versión nueva Y su ADR, nunca
#: un campo suelto (ADR 0011 §2.3).
SCHEMA_VERSION: Final[int] = 1

#: Claves EXACTAS del documento. Se usan para el veredicto de igualdad, no como
#: "mínimo requerido": cualquier clave de más es schema desconocido ⇒ caso E.
CLAVES_DE_BINDING: Final[frozenset[str]] = frozenset({"schema_version", "binding_id", "resource_binding"})

#: Claves EXACTAS de la evidencia. `config_path` NO pertenece al binding: puede
#: aparecer en logs como procedencia, nunca como metadata contractual.
CLAVES_DE_RESOURCE_BINDING: Final[frozenset[str]] = frozenset({"game_path", "mo2_instance_data_root", "mo2_mods_path"})


class Veredicto(enum.Enum):
    """Qué se puede hacer con un root en un estado dado."""

    INICIALIZAR = "initialize"
    UTILIZAR = "use"
    RECHAZAR = "reject"


class EstadoDelRoot(enum.Enum):
    """Máquina de estados normativa del root (ADR 0011 §2.4).

    Los OCHO casos son cerrados y su veredicto vive en
    :data:`VEREDICTO_POR_ESTADO`. El ancla de igualdad literal en
    ``tests/test_dyndolod_workspace.py`` rompe si se agrega un estado sin
    veredicto o se cambia uno: la tabla del ADR y el código no pueden divergir
    en silencio.
    """

    A_AUSENTE = "A"
    B_VACIO_SIN_BINDING = "B"
    C_BINDING_COMPATIBLE = "C"
    D_NO_VACIO_SIN_BINDING = "D"
    E_SCHEMA_DESCONOCIDO = "E"
    F_BINDING_AJENO = "F"
    G_RECURSOS_CAMBIARON = "G"
    H_OTRO_ROOT_ACTIVO = "H"


VEREDICTO_POR_ESTADO: Final[dict[EstadoDelRoot, Veredicto]] = {
    EstadoDelRoot.A_AUSENTE: Veredicto.INICIALIZAR,
    EstadoDelRoot.B_VACIO_SIN_BINDING: Veredicto.INICIALIZAR,
    EstadoDelRoot.C_BINDING_COMPATIBLE: Veredicto.UTILIZAR,
    EstadoDelRoot.D_NO_VACIO_SIN_BINDING: Veredicto.RECHAZAR,
    EstadoDelRoot.E_SCHEMA_DESCONOCIDO: Veredicto.RECHAZAR,
    EstadoDelRoot.F_BINDING_AJENO: Veredicto.RECHAZAR,
    EstadoDelRoot.G_RECURSOS_CAMBIARON: Veredicto.RECHAZAR,
    EstadoDelRoot.H_OTRO_ROOT_ACTIVO: Veredicto.RECHAZAR,
}


class MotivoDeRechazo(enum.Enum):
    """Taxonomía de rechazo, para que el operador sepa qué hacer.

    Distinguirlos no es cosmético: "no configurado" es un estado normal que no
    tumba nada, "ocupado" se reintenta, "transición requerida" pide una acción
    deliberada del usuario, y "corrupto"/"ajeno" exigen inspección manual. Un
    único `WorkspaceError` genérico obligaría a leer el texto del mensaje para
    decidir, que es como se degradan los fail-closed a warnings.
    """

    NO_CONFIGURADO = "not_configured"
    INVALIDO = "invalid"
    AJENO = "foreign"
    TRANSICION_REQUERIDA = "transition_required"
    OCUPADO = "busy"
    CORRUPTO = "corrupt"


_MOTIVO_POR_ESTADO: Final[dict[EstadoDelRoot, MotivoDeRechazo]] = {
    EstadoDelRoot.D_NO_VACIO_SIN_BINDING: MotivoDeRechazo.AJENO,
    EstadoDelRoot.E_SCHEMA_DESCONOCIDO: MotivoDeRechazo.CORRUPTO,
    EstadoDelRoot.F_BINDING_AJENO: MotivoDeRechazo.AJENO,
    EstadoDelRoot.G_RECURSOS_CAMBIARON: MotivoDeRechazo.TRANSICION_REQUERIDA,
    EstadoDelRoot.H_OTRO_ROOT_ACTIVO: MotivoDeRechazo.TRANSICION_REQUERIDA,
}

_ETAPA: Final[str] = "dyndolod_workspace"


class WorkspaceRechazadoError(Exception):
    """Rechazo fail-closed del workspace, con todo lo que el operador necesita.

    Lleva etapa, root candidato, estado A–H (cuando aplica), motivo, identidad
    de recursos y acción requerida. Deliberadamente NO lleva un stack de 200
    líneas: una condición de admisión esperable (root no configurado, root
    ajeno) es un diagnóstico, no un incidente — el caller la loguea como una
    línea legible y sigue el camino de "no configurado" o aborta, según el caso.
    Tampoco lleva secretos: los tres paths de evidencia son rutas de instalación
    del usuario, y la clave de recursos es un hash.
    """

    def __init__(
        self,
        *,
        motivo: MotivoDeRechazo,
        razon: str,
        accion_requerida: str,
        root: pathlib.PurePath | None = None,
        estado: EstadoDelRoot | None = None,
        clave_de_recursos: str | None = None,
    ) -> None:
        self.etapa = _ETAPA
        self.motivo = motivo
        self.razon = razon
        self.accion_requerida = accion_requerida
        self.root = root
        self.estado = estado
        self.clave_de_recursos = clave_de_recursos
        detalle = f"[{_ETAPA}] {motivo.value}"
        if estado is not None:
            detalle += f" (caso {estado.value})"
        if root is not None:
            detalle += f" root={root}"
        super().__init__(f"{detalle}: {razon}. Acción requerida: {accion_requerida}")


# ---------------------------------------------------------------------------
# Identidad: `binding_id` (propiedad) ≠ `resource_binding` (evidencia)
# ---------------------------------------------------------------------------


def _canonicalizar(path: pathlib.PurePath) -> pathlib.PurePath:
    """``resolve()`` no estricto, hermano de ``path_resolver._canonicalizar``.

    Misma semántica y a propósito: la evidencia del binding se compara contra la
    resolución que hace el resolver en el arranque, así que si las dos
    canonicalizaciones divergieran el caso C se volvería F sin que nadie tocara
    un path. El ancla
    ``test_la_canonicalizacion_coincide_con_la_del_resolver`` las mantiene
    pegadas.

    Una ruta PURA de otro flavour (p. ej. ``PureWindowsPath`` construida en un
    test que corre sobre POSIX) no se puede resolver contra este filesystem: se
    devuelve tal cual. No es una degradación silenciosa — en producción el
    candidato SIEMPRE es un `pathlib.Path` del sistema vivo.
    """
    if isinstance(path, pathlib.Path):
        return path.resolve(strict=False)
    return path


@dataclasses.dataclass(frozen=True, slots=True)
class ResourceBinding:
    """Evidencia canonicalizada de los recursos de la instancia lógica.

    **Es evidencia, no identidad.** La tupla de paths se COMPARA contra lo que
    el binding registró; no se deriva de ella ningún identificador durable
    (reubicar la instancia rompería el vínculo, y hostname/usuario/SID
    contaminarían la identidad con datos de máquina). La identidad concreta de
    propiedad es :func:`nuevo_binding_id`.
    """

    game_path: str
    mo2_instance_data_root: str
    mo2_mods_path: str

    @classmethod
    def desde_paths(
        cls,
        *,
        game_path: pathlib.PurePath | str,
        mo2_instance_data_root: pathlib.PurePath | str,
        mo2_mods_path: pathlib.PurePath | str,
    ) -> ResourceBinding:
        """Canonicaliza los tres paths con la primitiva del resolver."""
        return cls(
            game_path=str(_canonicalizar(pathlib.Path(game_path))),
            mo2_instance_data_root=str(_canonicalizar(pathlib.Path(mo2_instance_data_root))),
            mo2_mods_path=str(_canonicalizar(pathlib.Path(mo2_mods_path))),
        )

    def clave(self) -> str:
        """Clave estable de la instancia lógica para el registro durable.

        Es un hash de la evidencia canonicalizada, no un identificador de
        propiedad: sirve para preguntar "¿esta instancia lógica ya tiene un root
        activo?" (caso H) sin escribir los paths del usuario en un índice
        compartido. Dos configuraciones que resuelven los mismos recursos dan la
        misma clave — que es exactamente la definición de "misma instancia
        lógica" del ADR.
        """
        crudo = "\u0000".join(
            os.path.normcase(parte) for parte in (self.game_path, self.mo2_instance_data_root, self.mo2_mods_path)
        )
        return hashlib.sha256(crudo.encode("utf-8")).hexdigest()

    def como_documento(self) -> dict[str, str]:
        return {
            "game_path": self.game_path,
            "mo2_instance_data_root": self.mo2_instance_data_root,
            "mo2_mods_path": self.mo2_mods_path,
        }


def nuevo_binding_id() -> str:
    """UUID de propiedad, generado UNA vez al inicializar un work root.

    Nunca se deriva por hash de paths, hostname, usuario, SID ni nombre del
    archivo de config: eso volvería la propiedad función de la máquina y del
    layout, y un `config.toml` copiado a otra PC "heredaría" la propiedad de un
    root que no le pertenece.
    """
    return str(uuid.uuid4())


@dataclasses.dataclass(frozen=True, slots=True)
class BindingDocument:
    """El contenido del `.sky-claw-binding.json`, ya validado contra el schema v1."""

    schema_version: int
    binding_id: str
    resource_binding: ResourceBinding

    def como_documento(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "binding_id": self.binding_id,
            "resource_binding": self.resource_binding.como_documento(),
        }


# ---------------------------------------------------------------------------
# Lectura y publicación del binding
# ---------------------------------------------------------------------------


def _rechazo_de_schema(root: pathlib.Path, razon: str) -> WorkspaceRechazadoError:
    return WorkspaceRechazadoError(
        motivo=MotivoDeRechazo.CORRUPTO,
        estado=EstadoDelRoot.E_SCHEMA_DESCONOCIDO,
        root=root,
        razon=razon,
        accion_requerida=(
            f"inspeccionar {root / ARCHIVO_DE_BINDING} a mano; Sky-Claw no reescribe metadata que no puede interpretar"
        ),
    )


def _parsear_binding(root: pathlib.Path, crudo: str) -> BindingDocument:
    """Valida el schema v1 por IGUALDAD de claves, no por presencia.

    La política de campos extra del ADR es explícita: cualquier clave fuera del
    schema —en la raíz o dentro de `resource_binding`— es schema desconocido y
    va al caso E. Se valida por igualdad exacta justamente para que una
    extensión tolerante no se pueda escribir por accidente: un writer futuro que
    agregue `created_at` rompe la lectura en vez de crear una variante silenciosa
    del formato.
    """
    try:
        datos = json.loads(crudo)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _rechazo_de_schema(root, f"metadata de propiedad ilegible ({exc})") from exc

    if not isinstance(datos, dict):
        raise _rechazo_de_schema(root, "el documento raíz no es un objeto JSON")

    claves = set(datos)
    if claves != CLAVES_DE_BINDING:
        sobran = sorted(claves - CLAVES_DE_BINDING)
        faltan = sorted(CLAVES_DE_BINDING - claves)
        raise _rechazo_de_schema(
            root,
            f"schema v{SCHEMA_VERSION} desconocido (claves de más: {sobran}; faltantes: {faltan})",
        )

    version = datos["schema_version"]
    # `bool` es subclase de `int` y `True == 1`: sin el guard, un
    # `"schema_version": true` pasaría como versión 1.
    if not isinstance(version, int) or isinstance(version, bool) or version != SCHEMA_VERSION:
        raise _rechazo_de_schema(root, f"schema_version no soportada: {version!r}")

    binding_id = datos["binding_id"]
    if not isinstance(binding_id, str) or not binding_id.strip():
        raise _rechazo_de_schema(root, "binding_id ausente o no textual")

    evidencia = datos["resource_binding"]
    if not isinstance(evidencia, dict):
        raise _rechazo_de_schema(root, "resource_binding no es un objeto")
    if set(evidencia) != CLAVES_DE_RESOURCE_BINDING:
        sobran = sorted(set(evidencia) - CLAVES_DE_RESOURCE_BINDING)
        faltan = sorted(CLAVES_DE_RESOURCE_BINDING - set(evidencia))
        raise _rechazo_de_schema(root, f"resource_binding fuera de schema (de más: {sobran}; faltantes: {faltan})")
    if not all(isinstance(valor, str) and valor for valor in evidencia.values()):
        raise _rechazo_de_schema(root, "la evidencia de resource_binding no es textual")

    return BindingDocument(
        schema_version=version,
        binding_id=binding_id,
        resource_binding=ResourceBinding(
            game_path=evidencia["game_path"],
            mo2_instance_data_root=evidencia["mo2_instance_data_root"],
            mo2_mods_path=evidencia["mo2_mods_path"],
        ),
    )


def leer_binding(root: pathlib.Path) -> BindingDocument | None:
    """Documento de propiedad del root, o ``None`` si no hay archivo.

    Raises:
        WorkspaceRechazadoError: metadata corrupta o schema desconocido (caso E).
            Un fallo de LECTURA del archivo existente también es caso E: no se
            puede afirmar propiedad sobre lo que no se pudo leer.
    """
    archivo = root / ARCHIVO_DE_BINDING
    try:
        crudo = archivo.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _rechazo_de_schema(root, f"no se pudo leer la metadata de propiedad ({exc})") from exc
    return _parsear_binding(root, crudo)


def _crear_binding_exclusivo(root: pathlib.Path, documento: BindingDocument) -> bool:
    """Publica el binding con creación EXCLUSIVA. ``True`` si este proceso ganó.

    **Por qué no `os.replace`.** El patrón temporal + `os.replace` que usan
    `Config.save()` y los serializadores de `local_config.py` da atomicidad de
    ESCRITURA, no single-winner: si dos procesos inicializan el mismo root vacío,
    los dos escriben su temporal y el segundo `replace` sustituye el binding ya
    publicado por el primero — dos dueños, uno de ellos convencido de serlo. El
    ADR lo prohíbe con nombre y apellido.

    `os.open` con `O_CREAT | O_EXCL` es la primitiva con la propiedad
    demostrable: la creación y el chequeo de existencia son un solo paso del
    kernel, y el perdedor recibe `FileExistsError` en vez de pisar. Vale para
    POSIX y para Windows, y no depende de que los dos procesos compartan
    memoria, event loop ni intérprete (que es lo que un `asyncio.Lock` o un
    `threading.Lock` asumirían sin decirlo).
    """
    serializado = (json.dumps(documento.como_documento(), indent=2, sort_keys=True) + "\n").encode("utf-8")
    banderas = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        descriptor = os.open(root / ARCHIVO_DE_BINDING, banderas, 0o600)
    except FileExistsError:
        return False
    try:
        # `os.write` de UN solo buffer, sin la capa bufferizada de `fdopen`: el
        # archivo ya es visible para el perdedor desde el instante del `O_EXCL`,
        # así que cuantos menos flushes parciales haya, más chica es la ventana
        # en la que alguien puede leer un documento incompleto. La ventana no se
        # cierra del todo acá — la cierra la espera acotada de
        # :func:`_releer_binding_del_ganador`.
        with memoryview(serializado) as vista:
            escrito = 0
            while escrito < len(vista):
                escrito += os.write(descriptor, vista[escrito:])
        os.fsync(descriptor)
        os.close(descriptor)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        # Un binding a medio escribir es caso E para todo el mundo, incluido
        # este proceso en su próximo arranque: se retira el archivo que este
        # proceso acaba de crear (nadie más lo pudo haber creado: ganamos el
        # O_EXCL) y se propaga el error.
        with contextlib.suppress(OSError):
            (root / ARCHIVO_DE_BINDING).unlink()
        raise
    return True


#: Ventana máxima que el perdedor del `O_EXCL` espera a que el ganador termine
#: de escribir. Sólo se usa en ESE camino (ver :func:`_releer_binding_del_ganador`).
_ESPERA_MAXIMA_DEL_GANADOR_SEGUNDOS: Final[float] = 5.0
_INTERVALO_DE_RELECTURA_SEGUNDOS: Final[float] = 0.02


def _releer_binding_del_ganador(root: pathlib.Path) -> BindingDocument | None:
    """Relee el binding recién publicado por OTRO proceso, esperando lo justo.

    **Por qué esta espera existe y por qué no relaja el fail-closed.** `O_EXCL`
    reclama el NOMBRE de forma atómica, pero el contenido se escribe después: el
    perdedor puede abrir el archivo en el instante en que existe y todavía tiene
    cero bytes. Sin esta espera, la carrera que el ADR exige ganar limpiamente
    terminaba en caso E — un fail-closed correcto para metadata corrupta,
    aplicado a la única situación donde el archivo no está corrupto sino recién
    nacido. Lo encontró el test de dos procesos reales, que es exactamente para
    lo que está.

    La espera es quirúrgica y no toca el contrato general:

    * sólo corre acá, en el camino donde ya SABEMOS que hay un publicador
      concurrente (perdimos su `O_EXCL` hace microsegundos); `leer_binding` —el
      camino de arranque, sin publicador concurrente conocido— sigue fallando
      cerrado de inmediato ante un archivo vacío;
    * sólo espera mientras el archivo esté VACÍO. Un documento con bytes que no
      parsea o que se aparta del schema es caso E al instante: eso no es una
      escritura a medias, es metadata que no se puede interpretar;
    * está acotada. Agotado el plazo, caso E con la razón explícita — nunca un
      bucle indefinido esperando a un proceso que quizás murió a mitad de
      escribir.
    """
    # `time.sleep` y no `asyncio.sleep` porque esta función es SÍNCRONA y sólo se
    # alcanza desde un worker thread (`resolver_workspace` la cruza con
    # `asyncio.to_thread`) o desde un caller sync. Dormir acá no bloquea el event
    # loop; hacerlo en el loop sí lo haría, y por eso el ancla de AST exige el
    # `to_thread` en el único camino async que llega hasta acá.
    limite = time.monotonic() + _ESPERA_MAXIMA_DEL_GANADOR_SEGUNDOS
    while True:
        try:
            crudo = (root / ARCHIVO_DE_BINDING).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _rechazo_de_schema(root, f"no se pudo leer la metadata de propiedad ({exc})") from exc
        if crudo.strip():
            return _parsear_binding(root, crudo)
        if time.monotonic() >= limite:
            raise _rechazo_de_schema(
                root,
                "otro proceso reclamó el binding y no terminó de escribirlo "
                f"en {_ESPERA_MAXIMA_DEL_GANADOR_SEGUNDOS:.0f}s",
            )
        time.sleep(_INTERVALO_DE_RELECTURA_SEGUNDOS)


def publicar_binding(root: pathlib.Path, recursos: ResourceBinding) -> tuple[BindingDocument, bool]:
    """Inicializa la propiedad del root. Devuelve ``(documento, ganó_la_carrera)``.

    Contrato single-winner NO reemplazante: exactamente un proceso publica; el
    perdedor **no** reemplaza el archivo del ganador — lo relee, valida el
    `resource_binding` publicado y continúa sólo si es compatible. Si no lo es,
    fail-closed: dos instancias lógicas distintas no comparten work root.

    Raises:
        WorkspaceRechazadoError: el binding publicado por el ganador pertenece a
            otros recursos (motivo ``AJENO``), quedó corrupto (caso E), o el root
            configurado no se pudo inicializar en disco (motivo ``INVALIDO``).
    """
    documento = BindingDocument(
        schema_version=SCHEMA_VERSION,
        binding_id=nuevo_binding_id(),
        resource_binding=recursos,
    )
    # Un root de sólo lectura, en una unidad desconectada o sin espacio levanta
    # `OSError` acá. Dejarlo propagar crudo lo convertía en "falla inesperada" en
    # el boundary del arranque: un stack de 200 líneas en vez de la única frase
    # que el operador necesita —qué root, qué pasó y qué hacer—, y el veredicto
    # final es el mismo (no se usa ese root). El resto del módulo contesta
    # SIEMPRE con `WorkspaceRechazadoError`; esta era la última grieta por la que
    # se escapaba otra cosa.
    try:
        root.mkdir(parents=True, exist_ok=True)
        gano = _crear_binding_exclusivo(root, documento)
    except OSError as exc:
        raise WorkspaceRechazadoError(
            motivo=MotivoDeRechazo.INVALIDO,
            root=root,
            clave_de_recursos=recursos.clave(),
            razon=f"no se pudo inicializar el external_work_root en disco ({exc})",
            accion_requerida=(
                "verificar que la ruta exista o se pueda crear, que el volumen esté "
                "montado y con espacio, y que el usuario tenga permiso de escritura"
            ),
        ) from exc

    if gano:
        logger.info(
            "external_work_root inicializado: binding publicado.",
            extra={"pipeline_stage": _ETAPA, "root": str(root), "binding_id": documento.binding_id},
        )
        return documento, True

    publicado = _releer_binding_del_ganador(root)
    if publicado is None:  # pragma: no cover - carrera con un borrado externo
        raise _rechazo_de_schema(root, "el binding desapareció entre la creación y la relectura")
    if publicado.resource_binding != recursos:
        raise WorkspaceRechazadoError(
            motivo=MotivoDeRechazo.AJENO,
            estado=EstadoDelRoot.F_BINDING_AJENO,
            root=root,
            clave_de_recursos=recursos.clave(),
            razon="otro proceso publicó primero un binding para OTROS recursos",
            accion_requerida="elegir un external_work_root exclusivo de esta instancia",
        )
    logger.info(
        "external_work_root ya inicializado por otro proceso: se reutiliza su binding.",
        extra={"pipeline_stage": _ETAPA, "root": str(root), "binding_id": publicado.binding_id},
    )
    return publicado, False


def reescribir_binding_propio(root: pathlib.Path, documento: BindingDocument) -> None:
    """Actualiza el binding PROPIO con escritura atómica (temporal + ``os.replace``).

    Acá `os.replace` sí es contractualmente válido: reemplazar el binding propio
    por su dueño es la operación pretendida, y el patrón temporal + replace en el
    MISMO directorio es el que ya usan `Config.save()` y `local_config.py`. Lo
    que nunca se hace es reescribir un binding AJENO: se verifica el
    `binding_id` publicado antes de tocar nada.

    Raises:
        WorkspaceRechazadoError: el archivo en disco pertenece a otro binding.
    """
    publicado = leer_binding(root)
    if publicado is not None and publicado.binding_id != documento.binding_id:
        raise WorkspaceRechazadoError(
            motivo=MotivoDeRechazo.AJENO,
            root=root,
            razon=(f"el binding en disco ({publicado.binding_id}) no es el propio ({documento.binding_id})"),
            accion_requerida="no se reescribe un binding ajeno; inspeccionar el root a mano",
        )
    _escribir_json_atomico(root / ARCHIVO_DE_BINDING, documento.como_documento())


def _escribir_json_atomico(destino: pathlib.Path, contenido: dict[str, Any]) -> None:
    """Temporal + ``os.replace`` en el MISMO directorio que el destino.

    Mismo patrón que `Config.save()`: un fallo a mitad de la serialización no
    puede dejar el archivo existente truncado a 0 bytes, y el `replace` es
    atómico para cualquier lector concurrente.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporal = tempfile.mkstemp(dir=str(destino.parent), prefix=f".{destino.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as archivo:
            archivo.write(json.dumps(contenido, indent=2, sort_keys=True) + "\n")
            archivo.flush()
            os.fsync(archivo.fileno())
        os.replace(temporal, destino)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporal)
        raise


# ---------------------------------------------------------------------------
# Admisión de rutas (ADR 0011 §2.7)
# ---------------------------------------------------------------------------


#: Prefijo de ruta extendida de Windows (``\\\\?\\``) y su variante UNC. `resolve()`
#: puede devolverlos —rutas largas, ciertos volúmenes— y la API de Known Folders
#: NO los usa, así que la misma carpeta llega deletreada de dos formas.
_PREFIJO_EXTENDIDO: Final[str] = "\\\\?\\"
_PREFIJO_EXTENDIDO_UNC: Final[str] = "\\\\?\\UNC\\"


def _ancla_sin_prefijo_extendido(ancla: str) -> str:
    """``\\\\?\\C:\\`` → ``C:\\`` y ``\\\\?\\UNC\\srv\\share\\`` → ``\\\\srv\\share\\``.

    El prefijo es una anotación PARA EL KERNEL (saltear el parseo de rutas y el
    límite MAX_PATH), no parte de la identidad del directorio: ``\\\\?\\C:\\x`` y
    ``C:\\x`` son el MISMO path. Sólo se normaliza el ancla porque es el único
    componente donde el prefijo aparece.
    """
    if ancla.upper().startswith(_PREFIJO_EXTENDIDO_UNC):
        return "\\\\" + ancla[len(_PREFIJO_EXTENDIDO_UNC) :]
    if ancla.startswith(_PREFIJO_EXTENDIDO):
        return ancla[len(_PREFIJO_EXTENDIDO) :]
    return ancla


def _partes_normalizadas(path: pathlib.PurePath) -> tuple[str, ...]:
    """Partes comparables de una ruta, insensibles a mayúsculas y al prefijo extendido.

    Se comparan PARTES, no substrings del string completo: eso es lo que hace
    que `E:\\Mis Documentos de Trabajo` no sea `Documents` y que
    `steamapps_viejos` no sea `steamapps`. El `casefold` es deliberadamente
    conservador — en un filesystem case-sensitive rechaza también el vecino que
    sólo difiere en mayúsculas, que es fail-closed y no fail-open, y el producto
    corre sobre Windows, donde esos dos nombres son el MISMO directorio.

    El prefijo extendido se normaliza por la razón CONTRARIA, y por eso no es
    cosmético: acá el deletreo de más produce un fail-OPEN. `resolve()` puede
    devolver ``\\\\?\\C:\\Users\\x\\Documents\\...`` mientras la API de Known Folders
    devuelve ``C:\\Users\\x\\Documents``; con las anclas distintas, el solapamiento
    NO se detecta y se admite un root que había que rechazar. La comparación
    tiene que ver el mismo directorio escrito de las dos maneras.
    """
    partes = path.parts
    if partes:
        partes = (_ancla_sin_prefijo_extendido(partes[0]), *partes[1:])
    return tuple(parte.casefold() for parte in partes)


def _solapan(uno: pathlib.PurePath, otro: pathlib.PurePath) -> bool:
    """``True`` si alguno contiene al otro, o si son el mismo directorio.

    Las DOS direcciones importan y la que se olvida es siempre la segunda: un
    candidato dentro del juego es obvio, pero un candidato que CONTIENE al juego
    convertiría media unidad en sandbox administrado.
    """
    a = _partes_normalizadas(uno)
    b = _partes_normalizadas(otro)
    corto, largo = (a, b) if len(a) <= len(b) else (b, a)
    return largo[: len(corto)] == corto


@dataclasses.dataclass(frozen=True, slots=True)
class RaicesProhibidas:
    """Raíces con las que el `external_work_root` no puede solapar en NINGUNA dirección.

    Cada entrada es ``(etiqueta, ruta)``; la etiqueta es lo que el operador lee
    en el rechazo, así que nombra el rol (`mo2_mods`, `texgen_install`) y no la
    ruta.

    ``indeterminadas`` lleva las prohibiciones que este sistema **no pudo
    resolver** (hoy: Known Folders en un Windows donde la API no contestó). Van
    en el conjunto y no en un log porque un conjunto de prohibiciones incompleto
    admite exactamente los roots que debía rechazar, sin que nada falle: la única
    forma de que esa ausencia tenga consecuencia es que viaje junto al dato y la
    admisión la mire.
    """

    entradas: tuple[tuple[str, pathlib.PurePath], ...]
    indeterminadas: tuple[str, ...] = ()

    @classmethod
    def desde_entorno(
        cls,
        *,
        game: pathlib.PurePath | None,
        mo2_install: pathlib.PurePath | None,
        mo2_instance_data_root: pathlib.PurePath | None,
        mo2_mods_path: pathlib.PurePath | None,
        dyndolod_exe: pathlib.PurePath | None,
        texgen_exe: pathlib.PurePath | None,
        temp_dir: pathlib.PurePath | None = None,
        known_folders_prohibidos: Sequence[tuple[str, pathlib.PurePath]] | None = None,
    ) -> RaicesProhibidas:
        """Arma el conjunto desde lo que el resolver ya sabe de esta instancia.

        `temp_dir=None` usa ``tempfile.gettempdir()`` — el TEMP real, que el ADR
        prohíbe por descartable. `known_folders_prohibidos=None` consulta la API
        de Known Folders (`Documents`, `Desktop`, `Downloads` por identificador,
        con su ruta efectiva vigente); pasar una secuencia explícita es para
        tests y para callers que ya la resolvieron.

        Cuando se consulta la API, lo que ella **no** pudo contestar en Windows
        viaja como `indeterminadas` y hace que :func:`admitir_root` falle
        cerrado. Pasar la secuencia explícita afirma que el caller ya resolvió el
        conjunto, así que no hay indeterminadas que arrastrar.
        """
        entradas: list[tuple[str, pathlib.PurePath]] = []

        def _agregar(etiqueta: str, ruta: pathlib.PurePath | None) -> None:
            if ruta is not None:
                entradas.append((etiqueta, _canonicalizar(ruta)))

        _agregar("game", game)
        if game is not None:
            _agregar("game_data", pathlib.Path(game) / "Data")
            _agregar("steamapps", _biblioteca_steam_de(game))
        _agregar("mo2_install", mo2_install)
        _agregar("mo2_instance_data_root", mo2_instance_data_root)
        if mo2_instance_data_root is not None:
            _agregar("mo2_profiles", pathlib.Path(mo2_instance_data_root) / "profiles")
            _agregar("mo2_overwrite", pathlib.Path(mo2_instance_data_root) / "overwrite")
        _agregar("mo2_mods", mo2_mods_path)
        if dyndolod_exe is not None:
            _agregar("dyndolod_install", pathlib.Path(dyndolod_exe).parent)
        if texgen_exe is not None:
            _agregar("texgen_install", pathlib.Path(texgen_exe).parent)
        _agregar("temp", temp_dir if temp_dir is not None else pathlib.Path(tempfile.gettempdir()))

        if known_folders_prohibidos is None:
            inspeccion = known_folders.inspeccionar_known_folders_prohibidas()
            carpetas: tuple[tuple[str, pathlib.PurePath], ...] = inspeccion.rutas
            indeterminadas = inspeccion.indeterminadas
        else:
            carpetas = tuple(known_folders_prohibidos)
            indeterminadas = ()
        for nombre, ruta in carpetas:
            entradas.append((nombre, ruta))

        return cls(entradas=tuple(entradas), indeterminadas=indeterminadas)

    def solapadas_con(self, candidato: pathlib.PurePath) -> tuple[str, ...]:
        """TODAS las etiquetas que solapan, no la primera.

        Enumerar importa: `mo2/mods` solapa a la vez con `mo2_instance_data_root`
        y con `mo2_mods`, y un mensaje que nombrara sólo una haría que el
        operador moviera el root a un lugar que sigue prohibido por la otra.
        """
        return tuple(etiqueta for etiqueta, ruta in self.entradas if _solapan(candidato, ruta))


def _biblioteca_steam_de(game: pathlib.PurePath) -> pathlib.PurePath | None:
    """Ancestro del juego cuyo nombre de componente es exactamente ``steamapps``.

    Igualdad de COMPONENTE, no búsqueda de substring: `E:\\steamapps_viejos` no
    es una biblioteca de Steam y `D:\\SteamLibrary\\steamapps\\common\\...` sí lo
    es. El ADR prohíbe la búsqueda de substrings para establecer identidad; esto
    es otra cosa — se recorre la cadena de ancestros del juego CONFIGURADO.
    """
    for ancestro in pathlib.PurePath(game).parents:
        if ancestro.name.casefold() == "steamapps":
            return ancestro
    return None


#: ``GetDriveType`` de Windows. Sólo se nombran los dos valores que deciden algo
#: acá; el resto (``DRIVE_FIXED``, ``DRIVE_REMOVABLE``, ``DRIVE_CDROM``…) no
#: necesita nombre porque no cambia el veredicto.
_DRIVE_UNKNOWN: Final[int] = 0
_DRIVE_REMOTE: Final[int] = 4


def _hay_unidades_mapeadas() -> bool:
    """¿Existe siquiera el concepto de unidad mapeada en esta plataforma?

    Seam aparte del que consulta el tipo, por la misma razón que en
    `known_folders`: sin separarlos, "acá no hay unidades" y "no pude preguntar"
    colapsan en el mismo ``None``, y el fail-closed sobre el segundo volvería
    imposible configurar un root en Linux.
    """
    return sys.platform == "win32"


def _tipo_de_unidad(raiz: str) -> int | None:
    """``GetDriveTypeW`` para *raiz* (``"Z:\\"``), o ``None`` fuera de Windows.

    Único punto que toca el sistema operativo; los tests lo sustituyen para
    ejercer el contrato tipo-de-unidad → veredicto sin correr sobre Windows ni
    montar un share SMB real.
    """
    if sys.platform != "win32":
        return None
    try:
        return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(raiz)))
    except (AttributeError, OSError) as exc:  # pragma: no cover - depende de la plataforma
        logger.debug("GetDriveTypeW(%s) no disponible: %s", raiz, exc)
        return None


def _unidad_de_red(canonico: pathlib.PurePath) -> str | None:
    """Razón por la que *canonico* NO vive en un volumen local, o ``None``.

    **Por qué el chequeo sintáctico de UNC no alcanza.** ``\\\\servidor\\share``
    se rechaza mirando el string, pero el MISMO share montado como ``Z:\\`` es
    sintácticamente indistinguible de un disco local — y es la forma en que la
    gente lo usa de verdad. El ADR prohíbe "UNC/red", no "rutas que empiezan con
    dos backslashes": la propiedad es la del volumen, así que se le pregunta al
    sistema por el tipo de unidad en vez de adivinarlo del texto.

    Fail-closed sobre lo indeterminado: en Windows, un ``DRIVE_UNKNOWN`` o una
    API que no se pudo llamar significan "no sé si esto es local", y admitir un
    root cuyo volumen no se pudo clasificar es exactamente el falso negativo que
    este chequeo existe para no tener. Fuera de Windows no hay unidades mapeadas
    y no se inventa un veredicto.
    """
    if not _hay_unidades_mapeadas():
        return None
    # Hermano del fix del prefijo extendido en `_partes_normalizadas`: `resolve()`
    # puede devolver `\\?\C:\...` en rutas largas, y entonces el ancla es `\\?\C:\`,
    # que `GetDriveTypeW` NO reconoce como raíz de volumen (devuelve UNKNOWN/NO_ROOT).
    # Sin normalizar, un root local largo y legítimo se clasificaría como
    # indeterminado y se rechazaría (falso positivo). El prefijo es una anotación
    # para el kernel, no parte de la identidad del volumen: se quita antes de
    # preguntarle al sistema, igual que en la comparación de solapamiento.
    ancla_de_volumen = _ancla_sin_prefijo_extendido(pathlib.PureWindowsPath(canonico).anchor)
    if not ancla_de_volumen:  # pragma: no cover - `admitir_root` ya exigió absoluta
        return None
    tipo = _tipo_de_unidad(ancla_de_volumen)
    if tipo == _DRIVE_REMOTE:
        return f"la unidad {ancla_de_volumen} es un recurso de red mapeado"
    if tipo is None or tipo == _DRIVE_UNKNOWN:
        return f"no se pudo determinar si la unidad {ancla_de_volumen} es local"
    return None


def admitir_root(candidato: pathlib.PurePath | str, *, prohibidas: RaicesProhibidas) -> pathlib.PurePath:
    """Valida el candidato como `external_work_root` y devuelve su forma canónica.

    Propiedades exigidas (ADR 0011 §2.7), todas fail-closed:

    * absoluta, no UNC/red —ni sintáctica ni por unidad mapeada—, no raíz de
      volumen;
    * el conjunto de raíces prohibidas tiene que estar COMPLETO: una prohibición
      que no se pudo resolver (`prohibidas.indeterminadas`) rechaza, no se
      ignora — pero DESPUÉS del solapamiento, para que un root que ya se puede
      probar prohibido se rechace nombrando con qué solapa y no con qué no se
      pudo comparar;
    * sin solapamiento en NINGUNA dirección con juego, Data, biblioteca Steam,
      instalación y datos de MO2, profiles, mods, overwrite, instalaciones de
      DynDOLOD/TexGen, TEMP y las Known Folders prohibidas;
    * puede vivir en otro volumen local — "externo al juego" no significa "en el
      mismo disco".

    Raises:
        WorkspaceRechazadoError: motivo ``INVALIDO`` con la razón concreta.
    """
    crudo = str(candidato).strip()
    if not crudo:
        raise WorkspaceRechazadoError(
            motivo=MotivoDeRechazo.INVALIDO,
            razon="external_work_root vacío",
            accion_requerida="configurar external_work_root con una ruta absoluta local",
        )

    # UNC / red: se mira el string crudo porque el prefijo es sintáctico y la
    # detección no puede depender del flavour del intérprete que corre el test.
    if crudo.startswith("\\\\") or crudo.startswith("//"):
        raise WorkspaceRechazadoError(
            motivo=MotivoDeRechazo.INVALIDO,
            root=pathlib.PurePath(crudo),
            razon="las rutas UNC/de red no están soportadas en esta entrega",
            accion_requerida="elegir un directorio en un volumen LOCAL",
        )

    ruta = candidato if isinstance(candidato, pathlib.PurePath) else pathlib.Path(crudo)
    if not ruta.is_absolute():
        raise WorkspaceRechazadoError(
            motivo=MotivoDeRechazo.INVALIDO,
            root=ruta,
            razon="external_work_root debe ser una ruta absoluta",
            accion_requerida="configurar la ruta completa, no una relativa al cwd",
        )

    canonico = _canonicalizar(ruta)
    if len(canonico.parts) <= 1:
        raise WorkspaceRechazadoError(
            motivo=MotivoDeRechazo.INVALIDO,
            root=canonico,
            razon="la raíz de un volumen no puede ser el external_work_root",
            accion_requerida="elegir un subdirectorio dedicado dentro del volumen",
        )

    de_red = _unidad_de_red(canonico)
    if de_red is not None:
        raise WorkspaceRechazadoError(
            motivo=MotivoDeRechazo.INVALIDO,
            root=canonico,
            razon=f"las rutas UNC/de red no están soportadas en esta entrega: {de_red}",
            accion_requerida="elegir un directorio en un volumen LOCAL (no una unidad mapeada a un share)",
        )

    # La evidencia que SÍ tenemos manda sobre la que falta. Los dos rechazos son
    # fail-closed y el veredicto final es el mismo —no se usa este root—, pero el
    # ORDEN decide qué lee el operador: con las prohibiciones al revés, un root
    # que demostrablemente ES `Documents` se rechazaba diciendo "no se pudo
    # resolver Desktop", que no nombra el problema real ni la acción que lo
    # arregla. Primero se nombra lo que se puede probar; recién si no hay
    # solapamiento demostrado pesa lo que no se pudo mirar.
    solapadas = prohibidas.solapadas_con(canonico)
    if solapadas:
        raise WorkspaceRechazadoError(
            motivo=MotivoDeRechazo.INVALIDO,
            root=canonico,
            razon=("el external_work_root solapa (en alguna dirección) con: " + ", ".join(solapadas)),
            accion_requerida=(
                "elegir un directorio dedicado, externo al juego, a MO2, a las "
                "instalaciones de herramientas, a TEMP y a las carpetas de usuario"
            ),
        )

    # Sin solapamiento demostrado, un conjunto de prohibiciones INCOMPLETO sigue
    # siendo un rechazo: la ausencia de evidencia no es evidencia de ausencia —
    # este root podría SER la carpeta que no se pudo resolver, y el chequeo de
    # arriba, con esa entrada faltante, diría que no.
    if prohibidas.indeterminadas:
        raise WorkspaceRechazadoError(
            motivo=MotivoDeRechazo.INVALIDO,
            root=canonico,
            razon=(
                "no se pudo resolver la ubicación de "
                + ", ".join(prohibidas.indeterminadas)
                + "; el conjunto de raíces prohibidas está incompleto"
            ),
            accion_requerida=(
                "reintentar; si persiste, verificar que el shell de Windows responde "
                "(las carpetas de usuario se resuelven por la API de Known Folders)"
            ),
        )
    return canonico


def validar_destino_administrado(root: pathlib.Path, destino: pathlib.PurePath) -> pathlib.Path:
    """Exige que *destino* sea un descendiente REAL de *root*, sin componentes enlazados.

    La propiedad del ADR es: *ninguna mutación administrada puede resolver fuera
    del `external_work_root` admitido mediante un componente redirigido*. Se
    verifica en dos pasos que se cubren mutuamente:

    1. **Ningún componente enlazado** entre el root y el destino
       (`links.is_link`, que mira el enlace y no su destino: un junction, un
       symlink de directorio y un enlace ROTO cuentan los tres). Se rechaza
       incluso cuando el enlace apunta hacia adentro: `DirectoryRollback` mueve
       directorios con `rename`, y "renombrar el directorio" y "renombrar el
       enlace" son operaciones distintas con resultados distintos.
    2. **Contención tras resolver**, con `PathValidator.validate(...,
       strict_symlink=True)` — la misma primitiva que ya protege el resto del
       árbol, no un subsistema nuevo.

    Raises:
        WorkspaceRechazadoError: motivo ``INVALIDO``.
    """
    root_canonico = root.resolve(strict=False)
    destino_path = pathlib.Path(destino)
    try:
        relativo = destino_path.relative_to(root_canonico)
    except ValueError:
        try:
            relativo = destino_path.resolve(strict=False).relative_to(root_canonico)
        except ValueError as exc:
            raise WorkspaceRechazadoError(
                motivo=MotivoDeRechazo.INVALIDO,
                root=root_canonico,
                razon=f"el destino {destino} no cuelga del external_work_root admitido",
                accion_requerida="usar únicamente destinos derivados del root administrado",
            ) from exc

    if not relativo.parts:
        # `destino == root` da `PurePath(".")`, con `parts` vacío: el bucle de
        # abajo no corría y la función devolvía el propio root como "destino
        # administrado válido". Un caller de PR-2 podría entonces mover o borrar
        # la carpeta que contiene el binding junto con todos los subroots.
        raise WorkspaceRechazadoError(
            motivo=MotivoDeRechazo.INVALIDO,
            root=root_canonico,
            razon="el propio external_work_root no es un destino administrado",
            accion_requerida="usar un subdirectorio derivado, nunca la raíz que contiene el binding",
        )

    parcial = root_canonico
    for parte in relativo.parts:
        parcial = parcial / parte
        if is_link(parcial):
            raise WorkspaceRechazadoError(
                motivo=MotivoDeRechazo.INVALIDO,
                root=root_canonico,
                razon=(
                    f"el componente {parcial} es un enlace (symlink/junction/reparse) "
                    "y una mutación administrada no puede atravesarlo"
                ),
                accion_requerida=(
                    "reemplazar el componente enlazado por un directorio real dentro del external_work_root"
                ),
            )

    try:
        return PathValidator(roots=[root_canonico]).validate(destino_path, strict_symlink=True)
    except PathViolationError as exc:
        raise WorkspaceRechazadoError(
            motivo=MotivoDeRechazo.INVALIDO,
            root=root_canonico,
            razon=f"el destino {destino} escapa del external_work_root admitido ({exc})",
            accion_requerida="usar únicamente destinos derivados del root administrado",
        ) from exc


# ---------------------------------------------------------------------------
# Registro durable de root activo (P0.2 — habilita el caso H)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class TransicionPendiente:
    """Intención de cambio de root, registrada ANTES de cambiar nada."""

    desde: str | None
    hacia: str
    motivo: str


@dataclasses.dataclass(frozen=True, slots=True)
class EntradaDeRegistro:
    """Lo que la instalación recuerda de una instancia lógica."""

    clave: str
    root: str
    binding_id: str
    transicion_pendiente: TransicionPendiente | None = None


class RegistroDeRootActivo:
    """Estado durable ``resource_binding → external_work_root activo``.

    **Por qué existe y por qué no es el JSON del binding.** El binding describe
    SU root; no puede saber que la misma instancia lógica tiene otro root activo
    en otra parte de la instalación. Esa pregunta —el caso H— es instalacional,
    y el ADR la asigna al estado durable de coordinación de etapa 9. Sin este
    registro, el caso H no es detectable y degradarlo a C sería aceptar dos
    staging simultáneos sobre los mismos mods.

    **Por qué no es otra base SQLite.** El árbol ya tiene la exclusión
    cross-process (`DistributedLockManager`) y la usa para serializar los
    rituales; lo que faltaba era el DATO, no otro mecanismo de exclusión. Este
    registro es un archivo JSON pequeño en el estado durable por usuario, escrito
    atómicamente (temporal + `os.replace`, el patrón de `Config.save()`) y
    mutado siempre bajo la coordinación cross-process — el lock aporta la
    exclusión, el archivo aporta la memoria.

    **Dónde vive.** En ``SystemPaths.runtime_state_dir()``: por usuario, estable
    entre arranques e independiente del `cwd`, del worktree, del propio
    `external_work_root` y de TEMP. Un registro bajo el work root no podría
    responder "¿qué OTRO root está activo?", que es su única razón de ser.
    """

    def __init__(self, path: pathlib.Path) -> None:
        self._path = path

    @property
    def path(self) -> pathlib.Path:
        return self._path

    def leer(self) -> dict[str, EntradaDeRegistro]:
        """Todo el registro. Fail-closed si existe y no se puede interpretar."""
        try:
            crudo = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise WorkspaceRechazadoError(
                motivo=MotivoDeRechazo.CORRUPTO,
                razon=f"no se pudo leer el registro de roots activos ({exc})",
                accion_requerida=f"inspeccionar {self._path} a mano",
            ) from exc
        return self._parsear(crudo)

    def _parsear(self, crudo: str) -> dict[str, EntradaDeRegistro]:
        try:
            datos = json.loads(crudo)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise WorkspaceRechazadoError(
                motivo=MotivoDeRechazo.CORRUPTO,
                razon=f"registro de roots activos ilegible ({exc})",
                accion_requerida=f"inspeccionar {self._path} a mano",
            ) from exc
        version = datos.get("schema_version") if isinstance(datos, dict) else None
        # `bool` es subclase de `int` y `True == 1`: sin el guard explícito, un
        # `"schema_version": true` pasaba como v1. El parser del binding ya lo
        # evitaba y este NO — el hermano sin arreglar, encontrado en review.
        if (
            not isinstance(datos, dict)
            or not isinstance(version, int)
            or isinstance(version, bool)
            or version != SCHEMA_VERSION
        ):
            raise WorkspaceRechazadoError(
                motivo=MotivoDeRechazo.CORRUPTO,
                razon="registro de roots activos con schema desconocido",
                accion_requerida=f"inspeccionar {self._path} a mano",
            )
        entradas = datos.get("instancias")
        if not isinstance(entradas, dict):
            raise WorkspaceRechazadoError(
                motivo=MotivoDeRechazo.CORRUPTO,
                razon="el registro de roots activos no tiene el mapa de instancias",
                accion_requerida=f"inspeccionar {self._path} a mano",
            )
        resultado: dict[str, EntradaDeRegistro] = {}
        for clave, valor in entradas.items():
            if not isinstance(valor, dict) or not isinstance(valor.get("root"), str):
                raise WorkspaceRechazadoError(
                    motivo=MotivoDeRechazo.CORRUPTO,
                    razon=f"entrada de registro inválida para {clave}",
                    accion_requerida=f"inspeccionar {self._path} a mano",
                )
            # La forma anidada se valida EXACTO, no con defaults tolerantes: un
            # marcador sin `desde` hacía que `resolver_workspace` tomara
            # `entrada.root` como root viejo, se saltara `_validar_transicion` y
            # pudiera limpiar el marcador al registrar el activo — o sea, olvidar
            # el root viejo, que es justo lo que §25 prohíbe. Un `desde` no
            # textual terminaba en un `TypeError` crudo de `pathlib.Path`.
            pendiente_crudo = valor.get("transicion_pendiente")
            pendiente: TransicionPendiente | None = None
            if pendiente_crudo is not None:
                if (
                    not isinstance(pendiente_crudo, dict)
                    or set(pendiente_crudo) != {"desde", "hacia", "motivo"}
                    or not isinstance(pendiente_crudo["hacia"], str)
                    or not isinstance(pendiente_crudo["motivo"], str)
                    or not isinstance(pendiente_crudo["desde"], (str, type(None)))
                ):
                    raise WorkspaceRechazadoError(
                        motivo=MotivoDeRechazo.CORRUPTO,
                        razon=f"transición pendiente malformada para {clave}",
                        accion_requerida=f"inspeccionar {self._path} a mano",
                    )
                pendiente = TransicionPendiente(
                    desde=pendiente_crudo["desde"],
                    hacia=pendiente_crudo["hacia"],
                    motivo=pendiente_crudo["motivo"],
                )
            resultado[clave] = EntradaDeRegistro(
                clave=clave,
                root=valor["root"],
                binding_id=str(valor.get("binding_id", "")),
                transicion_pendiente=pendiente,
            )
        return resultado

    def entrada(self, clave: str) -> EntradaDeRegistro | None:
        return self.leer().get(clave)

    def buscar_por_binding_id(self, binding_id: str) -> EntradaDeRegistro | None:
        """La entrada que este instalación asocia a ese `binding_id`, si la hay.

        Es lo que distingue el caso G del F: un binding cuya evidencia ya no
        coincide es NUESTRO (caso G, transición explícita) sólo si la instalación
        recuerda haberlo activado; si nadie lo recuerda, pertenece a otros
        recursos (caso F).
        """
        for entrada in self.leer().values():
            if entrada.binding_id and entrada.binding_id == binding_id:
                return entrada
        return None

    def _guardar(self, entradas: dict[str, EntradaDeRegistro]) -> None:
        documento: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "instancias": {
                clave: {
                    "root": entrada.root,
                    "binding_id": entrada.binding_id,
                    **(
                        {
                            "transicion_pendiente": {
                                "desde": entrada.transicion_pendiente.desde,
                                "hacia": entrada.transicion_pendiente.hacia,
                                "motivo": entrada.transicion_pendiente.motivo,
                            }
                        }
                        if entrada.transicion_pendiente is not None
                        else {}
                    ),
                }
                for clave, entrada in entradas.items()
            },
        }
        _escribir_json_atomico(self._path, documento)

    def registrar_activa(self, *, clave: str, root: pathlib.PurePath | str, binding_id: str) -> EntradaDeRegistro:
        """Recuerda qué root está activo para esa instancia lógica.

        Limpia la transición pendiente: llegar a activo ES el final de la
        transición, y dejar el marcador vivo haría que el próximo arranque
        creyera que quedó a medias.
        """
        entradas = self.leer()
        entrada = EntradaDeRegistro(clave=clave, root=str(root), binding_id=binding_id, transicion_pendiente=None)
        entradas[clave] = entrada
        self._guardar(entradas)
        return entrada

    def registrar_transicion(self, *, clave: str, hacia: pathlib.PurePath | str, motivo: str) -> EntradaDeRegistro:
        """Anota la INTENCIÓN de cambiar de root, antes de tocar nada.

        Es lo que hace que una interrupción en cualquier frontera sea
        recuperable: el próximo arranque encuentra el marcador, sabe cuál era el
        root viejo y se niega a "olvidarlo" aunque el TOML ya apunte al nuevo.
        """
        entradas = self.leer()
        actual = entradas.get(clave)
        entrada = EntradaDeRegistro(
            clave=clave,
            # El root activo YA apunta al nuevo, pero el marcador conserva el
            # viejo: las dos mitades viajan en la MISMA escritura atómica. Si
            # sólo se repuntara, una muerte dura acá borraría del mapa el root
            # que todavía puede tener backups; si sólo se marcara, el arranque
            # siguiente vería el root nuevo como caso H contra el viejo y la
            # transición no podría avanzar nunca.
            root=str(hacia),
            binding_id=actual.binding_id if actual is not None else "",
            transicion_pendiente=TransicionPendiente(
                # Si YA hay una transición pendiente, su `desde` es el ÚNICO
                # registro durable del root original y se conserva. Pisarlo con
                # `actual.root` —que en ese punto ya apunta al root nuevo— borra
                # la referencia al original: un segundo crash antes de
                # `registrar_activa` haría que el arranque siguiente ni siquiera
                # inspeccione la raíz que puede tener los backups. Lo encontró el
                # revisor adversarial siguiendo la recuperación repetida.
                desde=(
                    actual.transicion_pendiente.desde
                    if actual is not None and actual.transicion_pendiente is not None
                    else (actual.root if actual is not None else None)
                ),
                hacia=str(hacia),
                motivo=motivo,
            ),
        )
        entradas[clave] = entrada
        self._guardar(entradas)
        return entrada


# ---------------------------------------------------------------------------
# Máquina de estados
# ---------------------------------------------------------------------------


def _root_esta_vacio(root: pathlib.Path) -> bool:
    """Vacío = sin entradas propias más allá del archivo de binding.

    El propio archivo de metadata no puede disparar el caso D: es nuestro, y un
    root que sólo lo contiene es exactamente un root inicializado.
    """
    try:
        return all(hijo.name == ARCHIVO_DE_BINDING for hijo in root.iterdir())
    except OSError as exc:
        raise WorkspaceRechazadoError(
            motivo=MotivoDeRechazo.CORRUPTO,
            root=root,
            razon=f"no se pudo inspeccionar el external_work_root ({exc})",
            accion_requerida="verificar permisos y disponibilidad del volumen",
        ) from exc


def evaluar_estado(
    root: pathlib.Path,
    *,
    recursos: ResourceBinding,
    registro: RegistroDeRootActivo,
) -> EstadoDelRoot:
    """Clasifica el root en UNO de los ocho casos del ADR 0011 §2.4.

    C, F y H comparten mecanismo —la comparación exacta del `resource_binding`
    registrado contra la resolución canonicalizada del arranque—; H añade la
    consulta al registro durable. No hay tercera vía ni degradación: si algo no
    se puede afirmar, el estado resultante rechaza.
    """
    if not root.exists():
        return EstadoDelRoot.A_AUSENTE

    try:
        documento = leer_binding(root)
    except WorkspaceRechazadoError as exc:
        if exc.estado is not EstadoDelRoot.E_SCHEMA_DESCONOCIDO:
            raise
        # `evaluar_estado` CLASIFICA; quien levanta es `exigir_veredicto`. Sin
        # esta conversión el caso E sería el único de los ocho que no se puede
        # pedir por su nombre, y el test paramétrico que los enumera tendría que
        # tratarlo aparte — que es exactamente cómo se pierde un caso de vista.
        return EstadoDelRoot.E_SCHEMA_DESCONOCIDO
    if documento is None:
        return EstadoDelRoot.B_VACIO_SIN_BINDING if _root_esta_vacio(root) else EstadoDelRoot.D_NO_VACIO_SIN_BINDING

    if documento.resource_binding != recursos:
        propio = registro.buscar_por_binding_id(documento.binding_id)
        return EstadoDelRoot.G_RECURSOS_CAMBIARON if propio is not None else EstadoDelRoot.F_BINDING_AJENO

    entrada = registro.entrada(recursos.clave())
    if entrada is not None and not _solapan(pathlib.Path(entrada.root), root.resolve(strict=False)):
        return EstadoDelRoot.H_OTRO_ROOT_ACTIVO
    if entrada is not None and pathlib.Path(entrada.root) != root.resolve(strict=False):
        # Solapan pero no son el mismo directorio (uno cuelga del otro): eso es
        # otro root activo de la misma instancia igual, no una coincidencia.
        return EstadoDelRoot.H_OTRO_ROOT_ACTIVO
    return EstadoDelRoot.C_BINDING_COMPATIBLE


def exigir_veredicto(
    root: pathlib.Path,
    *,
    recursos: ResourceBinding,
    registro: RegistroDeRootActivo,
) -> EstadoDelRoot:
    """Evalúa el estado y levanta si el veredicto es RECHAZAR.

    Devuelve el estado cuando el veredicto permite continuar (A/B = inicializar,
    C = utilizar), para que el caller sepa cuál de los dos caminos tomar sin
    volver a clasificar.
    """
    estado = evaluar_estado(root, recursos=recursos, registro=registro)
    if VEREDICTO_POR_ESTADO[estado] is not Veredicto.RECHAZAR:
        return estado

    if estado is EstadoDelRoot.E_SCHEMA_DESCONOCIDO:
        # Se re-lee para propagar el rechazo ORIGINAL, con la queja exacta del
        # parser (qué clave sobra, cuál falta, qué versión trae). Un mensaje
        # genérico "metadata inválida" obligaría al operador a abrir el JSON
        # para descubrir lo que el código ya sabía.
        leer_binding(root)
        # NO es código muerto (lo señaló el revisor adversarial, y vale aclararlo
        # acá en vez de en el PR): entre el `evaluar_estado` de arriba y esta
        # relectura hay un TOCTOU real — otro proceso pudo arreglar o borrar la
        # metadata en el ínterin, y entonces `leer_binding` ya no levanta. El
        # veredicto de ESTE arranque sigue siendo E, así que se rechaza igual:
        # cambiar de opinión a mitad de la función sería decidir sobre un disco
        # que ya no es el que se clasificó.
        raise _rechazo_de_schema(root, "metadata de propiedad no interpretable")

    entrada = registro.entrada(recursos.clave())
    razones = {
        EstadoDelRoot.D_NO_VACIO_SIN_BINDING: (
            "el directorio no está vacío y no tiene binding: Sky-Claw nunca adopta contenido preexistente"
        ),
        EstadoDelRoot.F_BINDING_AJENO: "el binding del root pertenece a otros recursos",
        EstadoDelRoot.G_RECURSOS_CAMBIARON: (
            "el binding es propio pero su evidencia ya no coincide con la resolución actual"
        ),
        EstadoDelRoot.H_OTRO_ROOT_ACTIVO: ("esta instancia lógica ya tiene otro external_work_root activo"),
    }
    acciones = {
        EstadoDelRoot.D_NO_VACIO_SIN_BINDING: ("elegir un directorio vacío o mover a mano el contenido existente"),
        EstadoDelRoot.F_BINDING_AJENO: ("elegir un external_work_root exclusivo de esta instancia"),
        EstadoDelRoot.G_RECURSOS_CAMBIARON: ("rebind explícito: P0 no reasocia un binding automáticamente"),
        EstadoDelRoot.H_OTRO_ROOT_ACTIVO: (
            "transición explícita desde " + (entrada.root if entrada is not None else "el root activo registrado")
        ),
    }
    raise WorkspaceRechazadoError(
        motivo=_MOTIVO_POR_ESTADO[estado],
        estado=estado,
        root=root,
        clave_de_recursos=recursos.clave(),
        razon=razones[estado],
        accion_requerida=acciones[estado],
    )


# ---------------------------------------------------------------------------
# P0.2 — coordinación durable de etapa 9
# ---------------------------------------------------------------------------

#: Orden FIJO de adquisición de la familia de etapa 9 (§21 del contrato de P0).
#: Se declara como dato, no como prosa, para que el test lo pueda enumerar: una
#: reordenación silenciosa es un deadlock esperando a que dos caminos la tomen
#: al revés.
#:
#: ``dyndolod-ownership`` es la lease LARGA de P2.0: se adquiere DENTRO del
#: boundary del lock corto (`dyndolod-workspace`) y se conserva toda la vida
#: del `WorkspaceResuelto` — se libera en el shutdown, nunca adentro de un lock
#: posterior. El ritual (`dyndolod-pipeline`) se toma después, ya con el
#: ownership en mano, en el mismo orden tanto en la transición del resolver
#: como en una corrida futura de etapa 9: no hay ciclo.
ORDEN_DE_ADQUISICION: Final[tuple[str, ...]] = (
    "dyndolod-workspace",
    "dyndolod-ownership",
    "dyndolod-pipeline",
    "snapshot-transaction-lock",
    "journal",
    "directory-rollback",
    "handoff/recovery",
)

#: Subdirectorio del estado durable donde vive lo de etapa 9.
_SUBDIR_DE_ETAPA9: Final[str] = "dyndolod"
_ARCHIVO_DE_LOCKS: Final[str] = "stage9_locks.db"
_ARCHIVO_DE_ROOTS_ACTIVOS: Final[str] = "active_roots.json"


def ruta_de_estado_de_etapa9(base: pathlib.Path | None = None) -> pathlib.Path:
    """Directorio de estado durable de etapa 9.

    Por usuario, estable entre arranques e independiente del `cwd`, del
    worktree, del `external_work_root` y de TEMP. **No** se deriva de ninguna
    variable de entorno: una segunda fuente de selección reabre el split-brain
    que la capa de resolución ya cerró para MODS/PROFILE (#552/#555), y además
    haría que el lugar donde vive la memoria de "qué root está activo" pudiera
    cambiar entre arranques sin que nadie lo note.

    `base` existe para tests y para callers que ya tienen un directorio de
    estado propio; en producción se omite.
    """
    from sky_claw.config import SystemPaths

    raiz = base if base is not None else SystemPaths.runtime_state_dir()
    return raiz / _SUBDIR_DE_ETAPA9


class Stage9Coordination:
    """Coordinación cross-process de la etapa 9, sobre estado durable.

    **Qué reutiliza y qué agrega.** Reutiliza el `DistributedLockManager` que ya
    serializa los rituales del árbol —mismo esquema SQLite, mismos leases con
    TTL, misma renovación por heartbeat vía `SnapshotTransactionLock`— y
    conserva el identificador del ritual, ``dyndolod-pipeline``. Lo único que
    cambia es DÓNDE vive el archivo: `.skyclaw_backups/locks.db` es relativo al
    `cwd`, así que dos instancias lanzadas desde directorios distintos abren dos
    bases distintas y no se excluyen. Un lock que no serializa es peor que no
    tener lock, porque promete exclusión.

    No se migran los otros rituales a esta base: mover `journal.db`, los
    snapshots y los locks de LOOT/Pandora/BodySlide es un cambio de blast radius
    mucho mayor que P0, y el contrato de P0 lo excluye explícitamente. Lo que se
    mueve es la coordinación de etapa 9, que es la que este trabajo tiene que
    hacer demostrable.

    **Dos recursos, no uno.** El ritual mantiene ``dyndolod-pipeline``; la
    resolución de propiedad del workspace usa ``dyndolod-workspace``. Son
    distintos a propósito: la resolución corre en el ARRANQUE, y hacerla esperar
    al lock del ritual colgaría el arranque de Sky-Claw detrás de una generación
    de LODs de 30+ minutos que está corriendo en otra instancia.
    """

    RECURSO_DEL_RITUAL: Final[str] = "dyndolod-pipeline"
    RECURSO_DEL_WORKSPACE: Final[str] = "dyndolod-workspace"
    #: Familia del recurso de ownership VIVO (P2.0). El resource_id concreto se
    #: deriva por instancia lógica: ``dyndolod-ownership-<clave>``, donde
    #: ``clave`` es `ResourceBinding.clave()`. NO es un recurso global — dos
    #: instancias lógicas distintas no se serializan entre sí — y NO lleva el
    #: pathname del root: dos roots para la misma instancia compiten por el
    #: MISMO recurso, que es exactamente la propiedad que P2.0 necesita.
    RECURSO_DEL_OWNERSHIP_VIVO: Final[str] = "dyndolod-ownership"

    @classmethod
    def resource_id_de_ownership(cls, clave: str) -> str:
        """Resource_id de la lease de vida para la instancia lógica *clave*."""
        return f"{cls.RECURSO_DEL_OWNERSHIP_VIVO}-{clave}"

    #: TTL corto para la resolución de propiedad: es una operación de arranque de
    #: milisegundos, no una generación de LODs. Corto NO significa frágil: el
    #: heartbeat de `SnapshotTransactionLock` lo renueva mientras el bloque siga
    #: adentro, así que una resolución lenta (filesystem de red, antivirus) no
    #: pierde la exclusión — y si la lease se pierde igual, la salida levanta en
    #: vez de reportar un éxito que no se puede sostener.
    TTL_DEL_WORKSPACE: Final[float] = 30.0

    #: TTL NOMINAL de la lease de ownership vivo (P2.0). No es un plazo de vida:
    #: el heartbeat la renueva cada ``TTL / RENEW_DIVISOR`` mientras el proceso
    #: viva, así que una lease legítima vive indefinidamente. El TTL sólo fija la
    #: ventana de detección de un holder muerto: si un proceso desaparece sin
    #: cleanup, nadie renueva y su lease expira en este plazo, y recién entonces
    #: otra instancia puede reclamar la propiedad.
    TTL_DEL_OWNERSHIP_VIVO: Final[float] = 60.0
    RENEW_DIVISOR_DEL_OWNERSHIP_VIVO: Final[float] = 3.0

    def __init__(
        self,
        *,
        lock_manager: DistributedLockManager,
        snapshot_manager: FileSnapshotManager,
    ) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._inicializado = False
        self._guardia = asyncio.Lock()

    async def manager_del_ritual(self) -> DistributedLockManager:
        """El lock manager de etapa 9, **garantizado abierto**.

        Es `async` a propósito, y es el ÚNICO acceso: un `DistributedLockManager`
        sin `initialize()` no consulta nada, levanta `LockError` en el primer uso
        — y ese primer uso es el guard de reconciliación del arranque, cuyo
        boundary best-effort se come la excepción. El resultado observable era
        que el recovery de U-08 quedaba desactivado en silencio para TODOS los
        productores (DynDOLOD, Pandora, BodySlide, sandbox), en cada arranque con
        `external_work_root` sin configurar — o sea, en el default.

        Que la apertura sea perezosa era correcto (los composition roots
        síncronos no pueden `await`); lo que estaba mal era prometer esa pereza y
        después entregar el manager crudo por una property síncrona, que no tiene
        dónde cumplirla. Con el acceso async no hay forma de sostener el
        manager sin haberlo abierto: la promesa la cumple el mecanismo, no el
        recuerdo del caller.
        """
        await self.initialize()
        return self._lock_manager

    async def initialize(self) -> None:
        """Abre la DB de coordinación. Idempotente y perezosa.

        Se inicializa sola en el primer uso para que los composition roots
        SÍNCRONOS (`SupervisorAgent.__init__`) puedan construir la coordinación
        sin un `await`: si la inicialización fuera obligación del caller, el
        primer camino que se olvidara de llamarla correría sin exclusión y sin
        error visible — el defecto hermano en su forma más silenciosa.
        """
        async with self._guardia:
            if self._inicializado:
                return
            await self._lock_manager.initialize()
            self._inicializado = True

    async def close(self) -> None:
        async with self._guardia:
            if not self._inicializado:
                return
            await self._lock_manager.close()
            self._inicializado = False

    @contextlib.asynccontextmanager
    async def sostener_ritual(
        self,
        *,
        agent_id: str,
        ttl: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[SnapshotTransactionLock]:
        """Sostiene el ritual de etapa 9 mientras dure el bloque.

        Se apoya en `SnapshotTransactionLock` —el mismo que ya usa el servicio—
        con ``target_files=[]``: el rollback de DynDOLOD es el move-aside de
        `DirectoryRollback` (los `Output/` pesan GBs), no el snapshot manager.
        De ahí salen gratis el heartbeat de renovación, `lease_lost` y
        `assert_owned`, que es exactamente la "semántica real de leases" que el
        contrato pide conservar en vez de reinventar.

        La liberación va en el ``finally`` del context manager: no se suelta la
        coordinación mientras el cuerpo sigue corriendo —proceso hijo vivo,
        rollback restaurando, recovery mutando— ni siquiera por el camino de
        excepción o cancelación.
        """
        await self.initialize()
        sostenido = SnapshotTransactionLock(
            lock_manager=self._lock_manager,
            snapshot_manager=self._snapshot_manager,
            resource_id=self.RECURSO_DEL_RITUAL,
            agent_id=agent_id,
            target_files=[],
            ttl=ttl,
            metadata=metadata,
        )
        async with sostenido:
            yield sostenido

    @contextlib.asynccontextmanager
    async def sostener_workspace(self, *, agent_id: str) -> AsyncIterator[SnapshotTransactionLock]:
        """Serializa la resolución de propiedad del workspace entre procesos.

        Cubre el check + publish del binding y la mutación del registro de root
        activo como una unidad: sin esto, dos arranques simultáneos podrían leer
        ambos "no hay root activo" y registrar cada uno el suyo.

        **Por qué usa el MISMO `SnapshotTransactionLock` que el ritual y no un
        `acquire_lock`/`release_lock` crudo.** La primera versión sí era cruda, y
        el revisor adversarial marcó lo que eso implicaba: un TTL fijo sin
        heartbeat: si la resolución tardaba más que el TTL —filesystem de red,
        antivirus escaneando el root, un árbol viejo enorme— la lease expiraba en
        silencio y otro proceso podía entrar, justo en la ventana donde se
        publica el binding y se reescribe el registro de root activo. La
        exclusión que P0.2 promete dejaba de ser demostrable sin que nada
        fallara. Reusando el mismo primitivo que `sostener_ritual` llegan gratis
        la renovación por heartbeat y `assert_owned`, que es lo que el contrato
        pide: exclusión demostrable, no exclusión con fecha de vencimiento.

        Yields el lock sostenido para que el caller pueda llamar `assert_owned()`
        justo antes de cada escritura crítica y estrechar a ~0 la ventana entre
        una lease perdida y su detección.

        Raises:
            WorkspaceRechazadoError: motivo ``OCUPADO`` si otro proceso está
                resolviendo el workspace ahora mismo. Es un rechazo honesto y
                reintentable, no una espera indefinida en el arranque.
            LockLeaseLostError: la lease se perdió durante la resolución. Es
                fail-closed: no se afirma una propiedad que ya no se puede
                garantizar.
        """
        await self.initialize()
        sostenido = SnapshotTransactionLock(
            lock_manager=self._lock_manager,
            snapshot_manager=self._snapshot_manager,
            resource_id=self.RECURSO_DEL_WORKSPACE,
            agent_id=agent_id,
            target_files=[],
            ttl=self.TTL_DEL_WORKSPACE,
        )
        try:
            await sostenido.__aenter__()
        except LockAcquisitionError as exc:
            raise WorkspaceRechazadoError(
                motivo=MotivoDeRechazo.OCUPADO,
                razon="otro proceso está resolviendo la propiedad del external_work_root",
                accion_requerida="reintentar; si persiste, verificar que no quedó una instancia colgada",
            ) from exc
        try:
            yield sostenido
        except BaseException as exc:
            await sostenido.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            await sostenido.__aexit__(None, None, None)

    async def adquirir_ownership_vivo(
        self,
        *,
        recursos: ResourceBinding,
        ttl: float | None = None,
        renew_divisor: float | None = None,
        auto_renew: bool = True,
    ) -> OwnershipDeWorkspaceVivo:
        """Adquiere la lease LARGA de ownership vivo de la instancia lógica.

        **Qué es y qué NO es.** Es la mitad de la unicidad que P0.2 dejó fuera
        de alcance a propósito: la exclusividad del `WorkspaceResuelto` VIVO, no
        sólo la del registro durable. Mientras el handle que devuelve siga vivo
        (con su heartbeat renovando), NINGÚN otro proceso puede resolver ni
        activar otro root para el MISMO `resource_binding` — ni siquiera el
        mismo root (§23): es exclusividad de proceso, no de pathname.

        **Por qué reusa `SnapshotTransactionLock` y no otra cosa.** Ya aporta
        exactamente la semántica de leases que el contrato pide: heartbeat de
        renovación, `lease_lost`, `assert_owned()` (con el token `acquired_at`
        que detecta la readquisición por otro dueño) y liberación en
        `__aexit__`. `target_files=[]`: el "rollback" del workspace es la
        transición del registro, no snapshots de archivos.

        **Resource_id por instancia lógica, owner por ADQUISICIÓN.** El recurso
        es ``dyndolod-ownership-<clave>`` (dos roots del mismo
        `resource_binding` compiten; dos bindings distintos no). El `agent_id`
        es un UUID fresco por adquisición: `renew_lock`/`release_lock` matchean
        por ``resource_id + agent_id``, así que ningún holder puede renovar ni
        liberar la lease de OTRO — ni otro proceso (agents distintos), ni un
        handle viejo de la MISMA coordinación que readquirió tras expirar (el
        handle viejo liberaría la lease nueva si el agent fuera compartido; con
        agent por adquisición no matchea). El token `acquired_at` de
        `assert_owned` cierra la readquisición por otro proceso.

        **Boundary.** Se llama DENTRO de `sostener_workspace` (ver
        `resolver_workspace`): la adquisición y la activación forman una unidad
        frente a otros resolvers, y no existe la ventana "resolví → solté →
        adquirí" que P2.0 cierra.

        Raises:
            WorkspaceRechazadoError: motivo ``OCUPADO`` si otra instancia
                conserva el ownership vivo de esta instancia lógica. Es
                fail-fast y reintentable: el rechazo ocurre ANTES de tocar el
                registro durable.
        """
        await self.initialize()
        agent_id = f"dyndolod-owner-{uuid.uuid4().hex}"
        sostenido = SnapshotTransactionLock(
            lock_manager=self._lock_manager,
            snapshot_manager=self._snapshot_manager,
            resource_id=self.resource_id_de_ownership(recursos.clave()),
            agent_id=agent_id,
            target_files=[],
            ttl=ttl if ttl is not None else self.TTL_DEL_OWNERSHIP_VIVO,
            auto_renew=auto_renew,
            renew_divisor=renew_divisor if renew_divisor is not None else self.RENEW_DIVISOR_DEL_OWNERSHIP_VIVO,
        )
        try:
            await sostenido.__aenter__()
        except LockAcquisitionError as exc:
            raise WorkspaceRechazadoError(
                motivo=MotivoDeRechazo.OCUPADO,
                clave_de_recursos=recursos.clave(),
                razon=("otro proceso conserva el ownership vivo del external_work_root de esta instancia lógica"),
                accion_requerida=(
                    "reintentar; si persiste, verificar que no quedó otra instancia "
                    "de Sky-Claw viva sobre esta instalación"
                ),
            ) from exc
        return OwnershipDeWorkspaceVivo(sostenido, self.resource_id_de_ownership(recursos.clave()), agent_id)


class OwnershipDeWorkspaceVivo:
    """Handle de la lease LARGA de ownership del workspace vivo (P2.0).

    **Por qué existe y quién lo conserva.** El `WorkspaceResuelto` del arranque
    vive toda la vida del proceso (`AppContext`), y su derecho a usarse para
    mutar —la exclusividad frente a otros procesos— tiene que vivir lo mismo.
    Este handle envuelve la lease de :meth:`Stage9Coordination.adquirir_ownership_vivo`
    y es lo que un consumidor futuro (P2.1) fenceará antes de cada mutación.

    **Los dos conceptos que NO se mezclan.** El registro durable
    (`RegistroDeRootActivo`) responde "cuál root es el activo"; esta lease
    responde "qué proceso posee ahora el derecho vivo a usarlo". La lease no
    agrega NADA al binding ni al registro — es una fila de ``resource_locks``
    con TTL y heartbeat, que muere sola por expiración si el proceso muere.

    **`liberar` es tolerante a lease perdida y es idempotente.** En el shutdown
    no interesa si la lease ya se perdió: no hay nada que proteger, y que el
    cleanup falle por eso convertiría un cierre normal en una excepción. El
    fence es `assert_owned`, que SÍ falla cerrado.
    """

    def __init__(self, lock: SnapshotTransactionLock, resource_id: str, agent_id: str) -> None:
        self._lock = lock
        self._resource_id = resource_id
        self._agent_id = agent_id
        self._liberado = False

    @property
    def resource_id(self) -> str:
        return self._resource_id

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def lease_lost(self) -> bool:
        return self._lock.lease_lost

    async def assert_owned(self, *, verify_db: bool = True) -> None:
        """Fence: levanta `LockLeaseLostError` si esta lease ya no es la vigente.

        Se llama antes de cada mutación futura (P2.1) y ya dentro del resolver,
        antes de cada escritura del registro durable. La pérdida NO se esconde
        en un log: queda representada como excepción fail-closed.
        """
        await self._lock.assert_owned(verify_db=verify_db)

    async def liberar(self) -> None:
        """Suelta la lease (shutdown limpio). Idempotente; tolera lease perdida."""
        if self._liberado:
            return
        self._liberado = True
        try:
            await self._lock.__aexit__(None, None, None)
        except LockLeaseLostError:
            # No es un incidente de etapa 9: es el cierre de un contexto cuyo
            # ownership ya se perdió (otro proceso lo reclamó). La lease expira
            # sola; el aviso lleva el resource_id para correlacionar, no el
            # pipeline_stage de una corrida.
            logger.warning(
                "Ownership vivo ya perdido al liberar (%s); la lease expira sola.",
                self._resource_id,
                extra={"resource_id": self._resource_id},
            )


def construir_coordinacion_de_etapa9(
    *,
    base: pathlib.Path | None = None,
    lifecycle: Any = None,
) -> Stage9Coordination:
    """Arma la coordinación sobre el estado durable por usuario.

    `lifecycle` es el `DatabaseLifecycleManager` opcional (M-01.1): con él, la
    conexión sale del lifecycle (WAL recovery + pragmas hardenizadas + shutdown
    coordinado), igual que journal y el resto de los lock managers del árbol.
    """
    directorio = ruta_de_estado_de_etapa9(base)
    directorio.mkdir(parents=True, exist_ok=True)
    return Stage9Coordination(
        lock_manager=DistributedLockManager(db_path=directorio / _ARCHIVO_DE_LOCKS, lifecycle=lifecycle),
        snapshot_manager=FileSnapshotManager(snapshot_dir=directorio / "snapshots"),
    )


def registro_de_roots_activos(base: pathlib.Path | None = None) -> RegistroDeRootActivo:
    """Registro durable de root activo, junto a la DB de coordinación."""
    return RegistroDeRootActivo(ruta_de_estado_de_etapa9(base) / _ARCHIVO_DE_ROOTS_ACTIVOS)


# ---------------------------------------------------------------------------
# Transición de root y resolución del workspace (boundary = arranque)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class InspeccionDeRootViejo:
    """Lo que se pudo averiguar del root que se está por dejar de usar."""

    inspeccionable: bool
    backups: tuple[pathlib.Path, ...]
    detalle: str = ""


def inspeccionar_root_para_transicion(root: pathlib.Path) -> InspeccionDeRootViejo:
    """¿El root viejo está quiescente como para dejarlo de usar?

    Busca residuo de move-aside (``<dir>.rollback-<nonce>``) con la MISMA regex
    que el reconciliador (`rollback_reconciler.SUFIJO_MOVE_ASIDE`), no con una
    propia: dos definiciones de "esto es un backup nuestro" se endurecen por
    separado y la que se olvida deja de ver backups reales.

    Un root que ya no existe es quiescente: no hay nada que perder. Un root que
    no se puede recorrer NO lo es — "no pude mirar" nunca se reporta como "está
    limpio".
    """
    from sky_claw.local.tools.rollback_reconciler import SUFIJO_MOVE_ASIDE

    if not root.exists():
        return InspeccionDeRootViejo(inspeccionable=True, backups=(), detalle="el root ya no existe")

    encontrados: list[pathlib.Path] = []
    try:
        for actual, directorios, archivos in os.walk(root, onerror=_relanzar_error_de_walk):
            for nombre in (*directorios, *archivos):
                if SUFIJO_MOVE_ASIDE.search(nombre):
                    encontrados.append(pathlib.Path(actual) / nombre)
    except OSError as exc:
        return InspeccionDeRootViejo(
            inspeccionable=False,
            backups=tuple(encontrados),
            detalle=f"no se pudo recorrer el root viejo ({exc})",
        )
    return InspeccionDeRootViejo(inspeccionable=True, backups=tuple(encontrados))


def _relanzar_error_de_walk(exc: OSError) -> None:
    """`os.walk` se traga los errores por default: acá NO.

    Un `PermissionError` a mitad del recorrido significa "no pude inspeccionar",
    y el default silencioso lo convertiría en "no encontré backups" — un
    fail-open exactamente donde el contrato exige fail-closed.
    """
    raise exc


@dataclasses.dataclass(frozen=True, slots=True)
class WorkspaceResuelto:
    """Snapshot INMUTABLE del workspace resuelto en ESTE arranque.

    Es frozen a propósito: el contrato de ADR 0011 §2.5 es "persistir ahora,
    aplicar en el próximo arranque". Un objeto mutable invitaría a que alguien
    lo "actualizara" cuando cambia el TOML, y eso mutaría en caliente el root
    bajo `AppContext`, las raíces del `PathValidator`, el runner cacheado, el
    reconciliador y las transacciones activas — precisamente lo que el ADR
    rechaza por diseño.

    **P2.0 — `ownership`.** Desde P2.0 el snapshot que vaya a usarse para mutar
    conserva la lease LARGA de ownership vivo (`OwnershipDeWorkspaceVivo`) que
    lo hace exclusivo frente a otros procesos. El handle viaja CON el snapshot:
    así no hay forma de tener un snapshot sin su fence, y el shutdown del
    contexto que lo conserva (`AppContext`) libera la lease. `None` es un
    estado construido a mano (tests, callers internos) que NO autoriza a mutar:
    `assert_owned()` lo rechaza fail-closed.
    """

    root: pathlib.Path
    binding: BindingDocument
    estado: EstadoDelRoot
    recien_inicializado: bool
    ownership: OwnershipDeWorkspaceVivo | None = None

    async def assert_owned(self) -> None:
        """Fence P2.0: snapshot sin ownership vivo != derecho a mutar.

        Un `WorkspaceResuelto` que perdió su lease (expiró, otro proceso la
        reclamó) NO puede autorizar ninguna mutación posterior: el registro
        puede haber transicionado a otro root mientras este snapshot sigue
        vivo. Levanta `LockLeaseLostError`, el mismo error del lock manager —
        no se crea una excepción paralela para renombrarla.
        """
        if self.ownership is None:
            raise LockLeaseLostError(
                f"El workspace {self.root} no conserva ownership vivo: "
                "sin lease no hay derecho a mutar su external_work_root"
            )
        await self.ownership.assert_owned()


_AGENTE_DEL_RESOLVER: Final[str] = "dyndolod-workspace-resolver"


@contextlib.asynccontextmanager
async def _transicion_validada(
    viejo: pathlib.Path,
    *,
    nuevo: pathlib.Path,
    recursos: ResourceBinding,
    coordinacion: Stage9Coordination,
    inspector: Callable[[pathlib.Path], InspeccionDeRootViejo],
    sonda_de_transaccion_pendiente: Callable[[], Awaitable[bool]] | None,
    ownership: OwnershipDeWorkspaceVivo,
) -> AsyncIterator[SnapshotTransactionLock]:
    """Fail-closed de la transición (§25), con el ritual SOSTENIDO mientras dura.

    **Por qué es un context manager y no una función que valida y vuelve.** La
    primera versión miraba `get_lock_info` y devolvía: entre esa foto y el
    `registrar_transicion` del caller corría la inspección del root viejo, que
    recorre GB de generaciones y no tiene cota de tiempo. Una etapa 9 que
    arrancaba en otro proceso dentro de esa ventana encontraba el registro
    repuntado a la raíz nueva con su corrida en vuelo sobre la vieja —
    exactamente el TOCTOU que el guard decía cerrar. Ahora el ritual se ADQUIERE
    y se sostiene: la validación y la activación son una unidad, y el ritual
    concurrente no puede empezar en el medio porque el lock está tomado.

    El orden es el congelado en :data:`ORDEN_DE_ADQUISICION` —``dyndolod-workspace``
    y ``dyndolod-ownership`` (ambos ya sostenidos por el caller) y recién
    después ``dyndolod-pipeline``—, así que no hay ciclo con el servicio, que
    toma el ritual sin sostener el workspace.

    Yields el lock del ritual para que el caller pueda ``assert_owned()`` justo
    antes de la escritura durable. La lease de ownership vivo también se
    fencea acá, con las otras dos: es la que excluye a OTRA INSTANCIA, y una
    transición escrita bajo una lease perdida repuntaría el registro a un root
    que nadie tiene derecho de activar.

    Se niega a activar otra raíz —y sobre todo a OLVIDAR la vieja— mientras el
    root viejo tenga backups, una transacción PENDING, un ritual vivo o un
    estado que no se pueda inspeccionar. Ninguno de esos casos se degrada a
    warning: perder de vista un root con la única copia recuperable de una
    generación es exactamente la clase de daño silencioso que el ADR ataja.

    Deliberadamente NO escribe nada en el registro cuando rechaza: el root viejo
    sigue siendo el activo registrado, así que el próximo arranque vuelve a
    encontrarlo. "Actualizar el registro para que parezca que la transición
    terminó" está prohibido por escrito.
    """

    def _rechazar(razon: str, *, motivo: MotivoDeRechazo) -> WorkspaceRechazadoError:
        return WorkspaceRechazadoError(
            motivo=motivo,
            root=nuevo,
            clave_de_recursos=recursos.clave(),
            razon=razon,
            accion_requerida=(
                f"resolver el estado pendiente de {viejo} antes de activar {nuevo}; "
                "Sky-Claw no abandona una raíz de trabajo con estado sin resolver"
            ),
        )

    manager = await coordinacion.manager_del_ritual()
    # Fast-path sólo para el DIAGNÓSTICO y para no pagar el backoff cuando ya se
    # ve un dueño: la exclusión real la da la adquisición de abajo, no esta foto.
    info = await manager.get_lock_info(Stage9Coordination.RECURSO_DEL_RITUAL)
    if info is not None and not info.is_expired:
        raise _rechazar(
            f"hay un ritual de etapa 9 en curso ({info.agent_id}); no se cambia de raíz en vuelo",
            motivo=MotivoDeRechazo.OCUPADO,
        )

    async with contextlib.AsyncExitStack() as pila:
        try:
            ritual = await pila.enter_async_context(
                coordinacion.sostener_ritual(
                    agent_id=_AGENTE_DEL_RESOLVER,
                    ttl=Stage9Coordination.TTL_DEL_WORKSPACE,
                    metadata={"motivo": "transicion-de-external-work-root", "hacia": str(nuevo)},
                )
            )
        except LockAcquisitionError as exc:
            dueño = await manager.get_lock_info(Stage9Coordination.RECURSO_DEL_RITUAL)
            raise _rechazar(
                "hay un ritual de etapa 9 en curso "
                f"({dueño.agent_id if dueño is not None else 'otro proceso'}); "
                "no se cambia de raíz en vuelo",
                motivo=MotivoDeRechazo.OCUPADO,
            ) from exc

        # `os.walk` sobre el root viejo puede recorrer GB de generaciones: fuera del loop.
        inspeccion = await asyncio.to_thread(inspector, viejo)
        if not inspeccion.inspeccionable:
            raise _rechazar(
                f"no se puede inspeccionar el root viejo {viejo}: {inspeccion.detalle}",
                motivo=MotivoDeRechazo.TRANSICION_REQUERIDA,
            )
        if inspeccion.backups:
            raise _rechazar(
                f"el root viejo {viejo} conserva {len(inspeccion.backups)} backup(s) de move-aside sin reconciliar",
                motivo=MotivoDeRechazo.TRANSICION_REQUERIDA,
            )

        if sonda_de_transaccion_pendiente is not None:
            try:
                pendiente = await sonda_de_transaccion_pendiente()
            except Exception as exc:  # noqa: BLE001 - boundary: no poder mirar es no poder afirmar
                raise _rechazar(
                    f"no se pudo verificar si hay transacciones pendientes del root viejo ({exc})",
                    motivo=MotivoDeRechazo.TRANSICION_REQUERIDA,
                ) from exc
            if pendiente:
                raise _rechazar(
                    f"el root viejo {viejo} tiene una transacción PENDING sin resolver",
                    motivo=MotivoDeRechazo.TRANSICION_REQUERIDA,
                )

        yield ritual


async def resolver_workspace(
    *,
    preferencia: str | None,
    recursos: ResourceBinding,
    prohibidas: RaicesProhibidas,
    registro: RegistroDeRootActivo,
    coordinacion: Stage9Coordination,
    agent_id: str = _AGENTE_DEL_RESOLVER,
    inspector: Callable[[pathlib.Path], InspeccionDeRootViejo] = inspeccionar_root_para_transicion,
    sonda_de_transaccion_pendiente: Callable[[], Awaitable[bool]] | None = None,
    ttl_del_ownership_vivo: float | None = None,
    renew_divisor_del_ownership_vivo: float | None = None,
) -> WorkspaceResuelto | None:
    """Resuelve la propiedad del `external_work_root` para ESTE arranque.

    Es el único punto que compone la capacidad completa de P0::

        preferencia → admisión → coordinación → transición → estado A–H
                    → binding → registro de root activo → ownership vivo

    **P2.0 — el boundary de ownership.** La lease larga
    (``dyndolod-ownership-<clave>``) se adquiere DENTRO de ``sostener_workspace``,
    antes de tocar el registro: no existe la ventana "resolví root A → solté
    todos los locks → ... → adquirí la lease" en la que otro proceso pudiera
    activar root B para esta instancia. Si otra instancia conserva el ownership
    vivo, la resolución rechaza ``OCUPADO`` ANTES de escribir nada — fail-fast y
    reintentable. La lease viaja en el `WorkspaceResuelto.ownership` y su
    liberación es responsabilidad del contexto que lo conserva (`AppContext`),
    no de esta función: es una lease de VIDA, no de resolución.

    **Qué NO hace.** No conecta el root al `-o:` (P2.1), no escribe bajo el
    `external_work_root`, no degrada ninguna validación P0 (backups, PENDING,
    old-root, A–H, binding): tras readquirir ownership —incluso tras la muerte
    dura de un holder— la transición se revalida completa.

    Returns:
        El workspace resuelto, o ``None`` cuando la preferencia está ausente:
        **DynDOLOD administrado NO CONFIGURADO**. Eso NO es un error — el resto
        de Sky-Claw arranca y funciona igual, y no hay fallback silencioso a
        ninguna raíz derivada. Tampoco se adquiere ninguna lease de ownership:
        un usuario sin esta capacidad no paga locks.

    Raises:
        WorkspaceRechazadoError: cualquier otro camino que no se pueda demostrar
            seguro (ruta inadmisible, root ajeno o no vacío, metadata corrupta,
            transición insegura, coordinación ocupada, ownership vivo en manos
            de otro proceso).
        LockLeaseLostError: la lease de ownership se perdió a mitad de la
            resolución: no se registra nada bajo una exclusión que ya no existe.
    """
    if preferencia is not None and not isinstance(preferencia, str):
        # Un TOML editado a mano puede traer `external_work_root = 3`. Sin esta
        # guarda el `.strip()` de abajo levantaba `AttributeError` — una traza
        # que no nombra ni el campo ni la acción, y que el boundary del arranque
        # reporta como "falla inesperada" en vez de como lo que es: config
        # inválida, con un rechazo que el operador puede leer y corregir.
        raise WorkspaceRechazadoError(
            motivo=MotivoDeRechazo.INVALIDO,
            razon=f"external_work_root debe ser una cadena y es {type(preferencia).__name__}",
            accion_requerida="configurar external_work_root con una ruta absoluta local entre comillas",
        )
    if preferencia is None or not preferencia.strip():
        logger.info(
            "DynDOLOD administrado NO CONFIGURADO: external_work_root ausente. "
            "El resto de Sky-Claw funciona normalmente.",
            extra={"pipeline_stage": _ETAPA, "motivo": MotivoDeRechazo.NO_CONFIGURADO.value},
        )
        return None

    # TODO el I/O de disco de esta función sale del event loop (§2.1 de
    # `.github/coding_conventions.md`: disco vía `asyncio.to_thread`). Las
    # funciones de abajo son SÍNCRONAS a propósito —así se testean y así las usa
    # cualquier caller no-async— y este orquestador es el único punto que las
    # cruza al mundo async, así que el offload vive acá, una vez por llamada.
    # `tests/test_dyndolod_workspace.py::test_ninguna_funcion_async_hace_io_de_disco_en_el_loop`
    # enumera esta familia por AST: una llamada bloqueante nueva sin `to_thread`
    # rompe el ancla antes de llegar a producción.
    admitido = await asyncio.to_thread(admitir_root, preferencia, prohibidas=prohibidas)
    root = pathlib.Path(admitido)
    clave = recursos.clave()

    # La liberación del ownership envuelve TODO el `async with`: si el
    # `__aexit__` del lock corto levanta (lease del workspace perdida entre el
    # último fence y la salida del contexto), la excepción nace FUERA de
    # cualquier try anidado y esta liberación es la única que la cubre. Una
    # liberación anidada adentro del bloque dejaría la lease huérfana hasta su
    # TTL — revisión Codex/Copilot, finding P1.
    ownership: OwnershipDeWorkspaceVivo | None = None
    try:
        async with coordinacion.sostener_workspace(agent_id=agent_id) as sostenido:
            # P2.0 — lease de ownership vivo, DENTRO del boundary del lock corto.
            # Se adquiere ANTES de mirar el registro y de tocar el disco: si otra
            # instancia conserva el ownership, el rechazo es OCUPADO y no se hace
            # ningún trabajo (ni se escribe nada) por esta resolución.
            ownership = await coordinacion.adquirir_ownership_vivo(
                recursos=recursos,
                ttl=ttl_del_ownership_vivo,
                renew_divisor=renew_divisor_del_ownership_vivo,
            )
            entrada = await asyncio.to_thread(registro.entrada, clave)
            if entrada is not None:
                pendiente = entrada.transicion_pendiente
                # El root a dejar es el `desde` del marcador cuando una transición
                # quedó a medias (el registro ya repuntó), y el activo registrado
                # cuando la preferencia acaba de cambiar. Mirar sólo `entrada.root`
                # haría que una transición interrumpida se diera por terminada.
                viejo = pathlib.Path(pendiente.desde) if pendiente and pendiente.desde else pathlib.Path(entrada.root)
                if viejo != root:
                    async with _transicion_validada(
                        viejo,
                        nuevo=root,
                        recursos=recursos,
                        coordinacion=coordinacion,
                        inspector=inspector,
                        sonda_de_transaccion_pendiente=sonda_de_transaccion_pendiente,
                        ownership=ownership,
                    ) as ritual:
                        # Recién con la transición VALIDADA —y con el ritual todavía
                        # SOSTENIDO— se registra la intención y se repunta el activo:
                        # en una sola escritura atómica, y siempre antes de publicar
                        # o usar el root nuevo.
                        # Antes de CADA escritura crítica se reconfirma la propiedad de
                        # las TRES leases contra la DB: entre la adquisición y este punto
                        # corrió I/O de disco de duración no acotada (inspección del root
                        # viejo), así que "las tenía cuando entré" no es evidencia de
                        # "las tengo ahora". Si alguna se perdió, levanta y no se
                        # escribe nada. Las tres protegen cosas distintas: el workspace
                        # excluye a otro resolver, el ownership excluye a otra instancia
                        # viva, el ritual excluye a una etapa 9 arrancando sobre el root
                        # viejo.
                        await sostenido.assert_owned()
                        await ownership.assert_owned()
                        await ritual.assert_owned()
                        await asyncio.to_thread(
                            registro.registrar_transicion, clave=clave, hacia=root, motivo="cambio de preferencia"
                        )
                        logger.info(
                            "Transición de external_work_root validada y registrada.",
                            extra={"pipeline_stage": _ETAPA, "desde": str(viejo), "hacia": str(root)},
                        )

            estado = await asyncio.to_thread(exigir_veredicto, root, recursos=recursos, registro=registro)
            if estado is EstadoDelRoot.C_BINDING_COMPATIBLE:
                # Se estrecha en una variable aparte para que el tipo DECLARADO de
                # `documento` sea `BindingDocument` y no `BindingDocument | None`:
                # con el offload a `to_thread` la primera asignación pasó a ser la
                # opcional, y el strict del módulo (que es opt-in, no heredado)
                # marcaba los dos usos de abajo.
                leido = await asyncio.to_thread(leer_binding, root)
                if leido is None:  # pragma: no cover - el veredicto C lo garantiza
                    raise _rechazo_de_schema(root, "el binding desapareció tras el veredicto")
                documento, recien_inicializado = leido, False
            else:
                documento, recien_inicializado = await asyncio.to_thread(publicar_binding, root, recursos)

            await sostenido.assert_owned()
            await ownership.assert_owned()
            await asyncio.to_thread(registro.registrar_activa, clave=clave, root=root, binding_id=documento.binding_id)
            return WorkspaceResuelto(
                root=root,
                binding=documento,
                estado=estado,
                recien_inicializado=recien_inicializado,
                ownership=ownership,
            )
    except BaseException:
        # §21: cualquier camino que falle DESPUÉS de adquirir la lease de
        # ownership (rechazo de transición, excepción, cancelación, o el propio
        # `__aexit__` del lock corto levantando) la libera. Una lease huérfana
        # hasta su TTL por un error recuperable sería un bloqueo de minutos
        # para la próxima instancia.
        if ownership is not None:
            await ownership.liberar()
        raise
