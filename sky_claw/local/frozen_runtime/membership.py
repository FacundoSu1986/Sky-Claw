"""Evidencia de membership de directorios + observacion sellada coherente (P3).

Cierra ``P3_DIRECTORY_MEMBERSHIP``: el ``TreeDigest`` de P1/P2 sella archivos
pero es ciego a los directorios vacios. Un Candidate que perdiera
``Data/EmptyFolder/`` conservaria el mismo ``TreeDigest``, asi que la membership
se sella aparte y se exige en las TRES evidencias (PRE, Candidate y POST).

Canonicalizacion (unica, determinista):

- relativa al root, nunca absoluta (ni ``/inicio`` ni ``C:\\``);
- separador canonico ``/`` (``\\`` se convierte; ``//`` colapsa);
- sin ``.`` ni ``..`` en ningun componente (``..`` se RECHAZA, no se resuelve:
  canonicalizar no es normalizar rutas arbitrarias del caller);
- orden lexicografico por el relpath canonico, sin duplicados;
- el root NO es una entrada (si lo fuera, ``Data`` y el root homonimo
  colisionarian semanticamente);
- sin timestamps: la identidad es el conjunto, no el momento.

Digest: SHA-256 (la primitive criptografica ya usada por RV-1), con separacion
de dominio explicita para que un digest de membership nunca sea comparable con
un ``TreeDigest`` de archivos por construccion. ``hash()`` de Python (salted,
por proceso) queda PROHIBIDO: la evidencia tiene que sobrevivir a un restart.

Sobre symlink/junction/reparse: falla cerrado via la primitive canonica
:mod:`sky_claw.app.security.links` -- no se sigue, no se ignora, y no se
reimplementa la deteccion en este modulo.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import stat
import time
from dataclasses import dataclass
from typing import Final

from sky_claw.app.security.links import link_kind_and_identity_or_raise, same_file_identity
from sky_claw.local.frozen_runtime.errors import FrozenRuntimeError
from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.models import FileIdentity, InventoryError, RuntimeIdentity, TreeDigest
from sky_claw.local.runtime_vault.runtime_observation import (
    RuntimeObservationError,
    observe_runtime_identity_from_root,
)
from sky_claw.local.runtime_vault.verification import tree_digest_from_files

#: Separador entre campos del digest (mismo criterio que RV-1).
_SEP: Final[bytes] = b"\x00"

#: Etiqueta de dominio: impide que un digest de membership sea interpretable
#: como un ``TreeDigest`` de archivos aunque las primitives coincidan.
_DOMAIN: Final[bytes] = b"frozen-runtime:directory-membership:v1\x00"

#: Caracteres que un componente de relpath NO puede contener, por dos razones
#: distintas que conviene no mezclar:
#:
#: - ``NUL`` (U+0000) y los controles ASCII (U+0001..U+001F) no son un nombre
#:   valido para el filesystem: Python y el SO los rechazan al MATERIALIZAR el
#:   path (``ValueError: embedded null character in path`` en ``os.stat``/
#:   ``os.mkdir``), no al canonicalizarlo.
#: - ``DEL`` (U+007F) si se puede crear, pero no es portable y no aporta
#:   ninguna identidad que un nombre imprimible no de. Se rechaza por la misma
#:   regla que P3 ya aplica a los identificadores (``candidate_id.py``).
#:
#: El predicado se escribe con ``<=`` y ``0x7F`` para cubrir los DOS extremos
#: de un tiron; ``DEL`` queda FUERA de ``< 0x20`` y necesita su propia clausula.
_CONTROL_O_DEL: Final[str] = "".join(chr(c) for c in (*range(0x20), 0x7F))


class SourceObservationError(FrozenRuntimeError):
    """Base de fallos al observar (archivos + membership + identidad) un arbol.

    Existe para que un caller que quiere convertir CUALQUIER fallo de observacion
    en un resultado tipado no tenga que enumerar los hermanos: antes
    ``TreeObservationCoherenceError`` era hermano de ``DirectoryMembershipError``
    (no subclase), asi que un handler que atrapaba sólo el segundo dejaba pasar
    la incoherencia como excepcion en vez de fail-closed (Codex sobre #682).
    """


class DirectoryMembershipError(SourceObservationError):
    """No se pudo capturar una membership de directorios fiable (fail-closed)."""


class TreeObservationCoherenceError(SourceObservationError):
    """Las dos mitades de la observacion no describen el mismo instante.

    La garantia que se pierde al aceptarla: la membership del ``TreeDigest`` y
    la membership propia podrian venir de instantes distintos, y entonces la
    comparacion PRE==Candidate==POST de membership no probaria nada sobre el
    mismo arbol que el digest de archivos.
    """


@dataclass(frozen=True, slots=True)
class DirectoryMembershipEvidence:
    """Conjunto completo de directorios de un arbol, comprometido por digest.

    ``digest`` compromete criptograficamente ``directories`` completo: los dos
    campos van juntos a persistencia y la comparacion es por identidad completa
    (digest + conteo), nunca solo por la lista ni solo por el conteo.
    """

    digest: str
    directory_count: int
    directories: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.digest:
            raise DirectoryMembershipError("la evidencia de membership exige un digest")
        if self.directory_count != len(self.directories):
            raise DirectoryMembershipError(
                f"directory_count {self.directory_count} no coincide con la lista ({len(self.directories)} entradas)"
            )
        if list(self.directories) != sorted(self.directories):
            raise DirectoryMembershipError("la lista de directorios no esta en orden canonico")
        if len(set(self.directories)) != len(self.directories):
            raise DirectoryMembershipError("la lista de directorios tiene duplicados")


def _clasificar_enlace(ruta: pathlib.Path) -> tuple[str | None, os.stat_result | None]:
    """Clasifica un enlace traduciendo el fallo de INSPECCION a la familia tipada.

    ``link_kind_and_identity_or_raise`` solo traduce ``FileNotFoundError``: un
    ``OSError`` transitorio (permiso, sharing, volumen desconectado) salia crudo y
    escapaba de ``crear_candidate``/``verificar_candidate`` en vez de volverse
    veredicto fail-closed. La traduccion vive aca, no en cada caller, para que
    ninguna sonda nueva quede sin traducir (Codex sobre #682).
    """
    try:
        return link_kind_and_identity_or_raise(ruta)
    except OSError as exc:
        raise DirectoryMembershipError(f"no se pudo inspeccionar '{ruta}': {exc}") from exc


def canonicalizar_relpath_de_scope(entrada: object, *, tipo: str) -> str:
    """Lleva un relpath de ambito (archivo o directorio) a SU UNICA forma canonica.

    Es la UNICA primitive de canonicalizacion de relpaths del paquete, y la
    comparten la evidencia de membership y la copia del Candidate. Que sea una sola
    no es cosmetico: si la copia repitiera estas reglas, un patron aceptado por la
    evidencia seria rechazado por la copia (o al reves) y la diferencia seria
    exactamente el hueco por donde pasaria un traversal.

    NORMALIZA lo que es inequivoco (separador ``\\`` a ``/``, ``//`` colapsado,
    ``.`` eliminado) y RECHAZA lo que es un ataque o una ambiguedad (``..``,
    rutas absolutas, letra de unidad/ADS, entradas degeneradas).

    La distincion importa: ``Data\\Meshes`` y ``Data/./Meshes`` describen el
    mismo directorio y deben producir la misma identidad; ``Data/../../etc`` no
    describe un directorio dentro del arbol, y canonicalizarlo "limpiandolo"
    convertiria un intento de traversal en un path legitimo.

    Un relpath ya canonico es relativo y sin `..`, asi que unirlo a una raiz no
    puede salir de ella: sobre Windows, ademas, no puede cambiar de unidad ni
    abrir un ADS.

    RECHAZA, ademas, toda entrada que contenga NUL / controles ASCII / DEL, y
    todo componente terminado en espacio o en punto. Ninguna de las dos cosas
    se "limpia", y la razon es la MISMA que ya obliga a rechazar `..`: el
    canonico es la IDENTIDAD de un elemento del arbol sellado, asi que
    normalizar dos nombres distintos al mismo string no simplifica nada -- hace
    que la evidencia afirme haber visto algo que no es lo que hay.

    El caso Windows de los sufijos merece precision porque NO es "prohibir
    espacios": el espacio INTERNO (`Data/My Folder`) es legitimo y se preserva.
    Lo que el SO recorta es el sufijo, asi que `Foo ` y `Foo.` describen (o
    dejan de describir, segun el API) el mismo nombre que `Foo`, y aceptarlos
    dejaria un componente cuya identidad depende de quien lo materialice.

    El espacio LEADING queda deliberadamente FUERA de este rechazo: Windows lo
    acepta de forma estable y ningun contrato de P3 lo prohibe. La regla de
    ``candidate_id.py`` (`candidate_id == candidate_id.strip()`) no se traslada
    porque un relpath de scope nombra un elemento REAL del arbol, donde el
    espacio inicial es un caracter significativo del nombre.
    """
    if not isinstance(entrada, str) or not entrada:
        raise DirectoryMembershipError(f"una entrada de {tipo} no puede ser vacia")
    # El chequeo va sobre la ENTRADA COMPLETA y ANTES de normalizar separadores:
    # un control embebido no es un problema de componentes, es un caracter que
    # el filesystem no acepta en NINGUNA posicion, y comprobarlo aca cubre de una
    # sola vez al componente inicial, a los intermedios y al ultimo. Ponerlo
    # por-componente multiplicaria la misma regla por cada caller y por cada
    # iteracion del bucle.
    if any(caracter in _CONTROL_O_DEL for caracter in entrada):
        raise DirectoryMembershipError(
            f"entrada de {tipo} con NUL, caracteres de control o DEL: {entrada!r} "
            "(no es un nombre de filesystem valido: se rechaza antes de materializar cualquier path)"
        )
    if entrada.startswith(("/", "\\")):
        raise DirectoryMembershipError(f"una entrada de {tipo} no puede ser absoluta: '{entrada}'")
    if ":" in entrada:
        raise DirectoryMembershipError(f"entrada de {tipo} ambigua (letra de unidad o ADS): '{entrada}'")
    crudo = entrada.replace("\\", "/")
    componentes = crudo.split("/")
    partes: list[str] = []
    for parte in componentes:
        if parte in ("", "."):
            continue
        if parte == ".." or ":" in parte:
            raise DirectoryMembershipError(f"entrada de {tipo} con componente no canonico: '{entrada}'")
        # El sufijo ambiguo de Windows se mira DESPUES del filtro estructural:
        # `Data/./Meshes` normaliza (el componente `.` describe el mismo
        # directorio), pero `Data/Foo.` es un NOMBRE y se rechaza. Un
        # componente que llegara aca terminado en `.` no es el estructural.
        if parte.endswith((" ", ".")):
            raise DirectoryMembershipError(
                f"entrada de {tipo} con componente terminado en espacio o punto: '{entrada}' "
                "(Windows lo recorta, asi que nombraria un elemento ambiguo: se rechaza, no se normaliza)"
            )
        partes.append(parte)
    if not partes:
        raise DirectoryMembershipError(f"entrada de {tipo} degenerada: '{entrada}'")
    # Un ARCHIVO nombra un archivo: `Data/`, `Data//` y `Data/.` describen un
    # directorio. La comprobacion que habia sobre `partes[-1]` era codigo MUERTO
    # (el filtro de arriba ya descarta "" y "."), asi que aceptaba `Data/` como
    # archivo y convertia una entrada de `mkdir` en una de `open` (CodeRabbit
    # sobre #682). La señal hay que leerla en los componentes CRUDOS, antes del
    # filtro.
    if tipo == "archivo" and componentes[-1] in ("", "."):
        raise DirectoryMembershipError(f"entrada de archivo que nombra un directorio: '{entrada}'")
    canonico = "/".join(partes)
    return canonico


def canonicalizar_directorio(entrada: str) -> str:
    """Canonicaliza una entrada de directorio (delega en la primitive unica)."""
    return canonicalizar_relpath_de_scope(entrada, tipo="directorio")


def canonicalizar_archivo(entrada: str) -> str:
    """Canonicaliza un `rel_path` de archivo (delega en la primitive unica).

    Un archivo tiene que NOMBRAR un archivo: `Data/` describe un directorio, y
    aceptarlo convertiria un archivo en una entrada de `mkdir`. El rechazo vive en
    la primitive (sobre los componentes CRUDOS) para que valga para todo caller;
    aca no queda ninguna comprobacion post-normalizacion porque `canonico` ya no
    puede terminar en `/`.
    """
    return canonicalizar_relpath_de_scope(entrada, tipo="archivo")


def construir_evidencia_membership(directorios: tuple[str, ...]) -> DirectoryMembershipEvidence:
    """Sella un conjunto de directorios ya en forma canonica."""
    canonicos = tuple(canonicalizar_directorio(d) for d in directorios)
    if len(set(canonicos)) != len(canonicos):
        raise DirectoryMembershipError("membership con directorios duplicados tras canonicalizar")
    ordenados = tuple(sorted(canonicos))
    acumulador = hashlib.sha256()
    acumulador.update(_DOMAIN)
    for directorio in ordenados:
        acumulador.update(directorio.encode("utf-8", "surrogatepass"))
        acumulador.update(_SEP)
    return DirectoryMembershipEvidence(
        digest=acumulador.hexdigest(),
        directory_count=len(ordenados),
        directories=ordenados,
    )


def capturar_membership_directorios(root: pathlib.Path) -> DirectoryMembershipEvidence:
    """Captura fail-closed del conjunto completo de directorios bajo *root*.

    Un symlink/junction/reparse dentro del scope NO se sigue ni se ignora
    (fail-closed): seguirlo copiaria fuera de la Managed Source, ignorarlo
    haria que una membership identica describiera dos arboles distintos. La
    deteccion es la primitive canonica del repo, no una reimplementacion.
    """
    raiz = pathlib.Path(root)
    tipo_raiz, identidad_raiz = _clasificar_enlace(raiz)
    if identidad_raiz is None:
        raise DirectoryMembershipError(f"la raiz '{raiz}' no existe: no hay membership que afirmar")
    if tipo_raiz is not None:
        raise DirectoryMembershipError(f"la raiz '{raiz}' es un enlace ({tipo_raiz}): no se sigue ni se ignora")
    if not stat.S_ISDIR(identidad_raiz.st_mode):
        raise DirectoryMembershipError(f"la raiz '{raiz}' no es un directorio")

    capturados: set[str] = set()
    pendientes: list[pathlib.Path] = [raiz]
    while pendientes:
        actual = pendientes.pop()
        tipo, identidad = _clasificar_enlace(actual)
        if identidad is None:
            raise DirectoryMembershipError(f"'{actual}' desaparecio durante el recorrido de membership")
        if tipo is not None:
            raise DirectoryMembershipError(
                f"enlace inesperado dentro del scope: '{actual}' ({tipo}) -- no se sigue ni se ignora"
            )
        if stat.S_ISREG(identidad.st_mode):
            # Archivo regular: no es entrada de membership, pero SI se valido
            # arriba (si fuera un enlace ya habriamos salido por `tipo`).
            continue
        if not stat.S_ISDIR(identidad.st_mode):
            raise DirectoryMembershipError(
                f"'{actual}' no es archivo regular ni directorio dentro del scope de membership"
            )
        try:
            with os.scandir(actual) as entradas:
                hijos = [pathlib.Path(entrada.path) for entrada in entradas]
        except OSError as exc:
            raise DirectoryMembershipError(f"no se pudo recorrer '{actual}': {exc}") from exc
        # Revalidar DESPUES de abrir: un directorio reemplazado por un enlace
        # entre su lstat y el scandir no debe meterse en la membership. Misma
        # defensa que aplica `inventory_tree`.
        tipo_despues, identidad_despues = _clasificar_enlace(actual)
        if identidad_despues is None:
            raise DirectoryMembershipError(f"'{actual}' desaparecio mientras se abria")
        if tipo_despues is not None:
            raise DirectoryMembershipError(
                f"'{actual}' se convertio en un enlace ({tipo_despues}) durante el recorrido"
            )
        if not same_file_identity(identidad, identidad_despues):
            raise DirectoryMembershipError(f"'{actual}' cambio mientras se abria")
        if actual != raiz:
            capturados.add(canonicalizar_directorio(actual.relative_to(raiz).as_posix()))
        pendientes.extend(hijos)
    return construir_evidencia_membership(tuple(sorted(capturados)))


@dataclass(frozen=True, slots=True)
class SealedTreeObservation:
    """Archivos + membership + identidad, capturados como UNA sola observacion.

    Es la unidad de autoridad que P3 compara en las tres posiciones (PRE,
    Candidate, POST). Existe para que ``TreeDigest`` y ``DirectoryMembership``
    nunca queden desincronizados: se emiten juntos o no se emiten.
    """

    root: pathlib.Path
    files: tuple[FileIdentity, ...]
    tree_digest: TreeDigest
    directory_membership: DirectoryMembershipEvidence
    runtime_identity: RuntimeIdentity
    observed_at_ns: int


def _observar_identidad(root: pathlib.Path, game_key: str) -> RuntimeIdentity:
    """Observa la identidad de runtime exigiendo siempre un ``game_key``.

    ``observe_runtime_identity_from_root`` exige el game_key (no es opcional):
    dejarlo en ``None`` para "no restringir" abriria una puerta a observar una
    identidad de otro juego dentro de la misma raiz. La ambiguedad se resuelve
    fail-closed acá, no en el contrato de la primitive.
    """
    try:
        observacion = observe_runtime_identity_from_root(root, expected_game_key=game_key)
    except RuntimeObservationError as exc:
        raise DirectoryMembershipError(f"no se pudo observar la identidad de runtime en '{root}': {exc}") from exc
    return observacion.runtime_identity


def exigir_coherencia_archivos_membership(
    files: tuple[FileIdentity, ...],
    membership: DirectoryMembershipEvidence,
) -> None:
    """Todo directorio ancestro de un archivo debe estar en la membership.

    Es el test de consistencia que convierte "coherente" en una propiedad
    verificable y no en una intencion: si el inventario de archivos y la
    membership provinieran de instantes distintos, algum archivo quedaria
    colgando de un directorio ausente de la membership y esto falla.
    """
    conocidos = set(membership.directories)
    for archivo in files:
        partes = pathlib.PurePosixPath(archivo.rel_path).parts[:-1]
        for indice in range(1, len(partes) + 1):
            ancestro = "/".join(partes[:indice])
            if ancestro not in conocidos:
                raise TreeObservationCoherenceError(
                    f"el archivo '{archivo.rel_path}' cuelga de '{ancestro}', ausente de la membership "
                    "de directorios: el inventario de archivos y la membership no son coherentes"
                )


def observar_arbol_sellado(
    root: pathlib.Path,
    *,
    game_key: str,
    observed_at_ns: int | None = None,
) -> SealedTreeObservation:
    """Sella archivos, membership e identidad en UNA observacion coherente.

    **Garantia concreta** que se obtiene, y por que el orden importa: la
    membership se captura PRE y POST y el inventario de archivos en medio. Si la
    membership de directorios cambio durante la ventana, las dos capturas
    difieren y la observacion entera se descarta -- nunca se emite un digest de
    archivos de un instante con la membership de otro. La identidad de runtime
    se observa igual en ambos extremos por la misma razon. Solo despues se
    exige la coherencia estructural archivos<->membership.

    Esto NO promete una garantia TOCTOU fuerte frente a un atacante con acceso
    al arbol (RV-1 tampoco la promete); acota la ventana a "el arbol estaba quieto
    durante la captura", que es exactamente lo que hace falta para que la
    comparacion PRE==Candidate==POST signifique algo.
    """
    raiz = pathlib.Path(root)
    ts = observed_at_ns if observed_at_ns is not None else time.time_ns()

    membership_pre = capturar_membership_directorios(raiz)
    identidad_pre = _observar_identidad(raiz, game_key)
    try:
        files = inventory_tree(raiz)
    except InventoryError as exc:
        raise DirectoryMembershipError(f"no se pudo inventariar '{raiz}': {exc}") from exc
    identidad_post = _observar_identidad(raiz, game_key)
    membership_post = capturar_membership_directorios(raiz)

    if membership_pre != membership_post:
        raise TreeObservationCoherenceError(
            "la membership de directorios cambio durante la observacion del arbol "
            f"({membership_pre.digest} -> {membership_post.digest}): no se emite evidencia parcial"
        )
    if identidad_pre != identidad_post:
        raise TreeObservationCoherenceError(
            "la identidad de runtime cambio durante la observacion del arbol "
            f"({identidad_pre.game_version} -> {identidad_post.game_version})"
        )
    exigir_coherencia_archivos_membership(files, membership_post)

    return SealedTreeObservation(
        root=raiz,
        files=files,
        tree_digest=tree_digest_from_files(files),
        directory_membership=membership_post,
        runtime_identity=identidad_post,
        observed_at_ns=ts,
    )
