"""Prueba fail-closed de que un worker observa el overlay del perfil MO2."""

from __future__ import annotations

import hashlib
import os
import pathlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from sky_claw.app.security.path_validator import PathViolationError, assert_safe_component
from sky_claw.local.mo2.plugin_sources import OFFICIAL_MASTERS

#: Dominio del digest del estado del perfil.
#:
#: `v2` (#633, 2026-09-24) **no reutiliza** `v1`: `plugins.txt` dejó de hashearse
#: byte a byte y pasó a hashearse por su estado semántico, así que la misma tupla
#: (perfil, archivos, canario) puede producir dos digests cuyo significado depende
#: de la implementación. Dos contratos distintos no comparten dominio.
_PROFILE_FINGERPRINT_DOMAIN = b"skyclaw-vfs-profile-v2"

#: Archivos de estado del perfil que se hashean **byte a byte**: no hay evidencia
#: de que MO2 los reserialice de forma semánticamente equivalente al lanzar una
#: aplicación (#633), así que su contrato sigue siendo "cualquier byte distinto
#: es un estado distinto".
_RAW_PROFILE_STATE_FILES = ("modlist.txt", "loadorder.txt", "settings.ini", "settings.txt")

#: `plugins.txt` se hashea por su **estado semántico**, no por sus bytes: MO2 lo
#: reescribe al lanzar (`CreationGamePlugins::writePluginList`, MO2 2.5.2 →
#: master) y esa reserialización cambia BOM, header, EOL y hasta qué líneas
#: existen sin cambiar qué plugins carga el motor. Contrato en
#: :func:`_canonical_plugins_state`.
_PLUGINS_STATE_FILE = "plugins.txt"

#: Extensiones que el motor acepta como plugin (SSE/AE: `CreationGamePlugins`).
_PLUGIN_EXTENSIONS = (b".esp", b".esm", b".esl")

#: BOM UTF-8 que algunos escritores (launcher del juego, herramientas de terceros)
#: anteponen al header. Para el lector de MO2 no es un comentario, pero tampoco
#: un plugin: es representación.
_UTF8_BOM = b"\xef\xbb\xbf"

#: Bytes prohibidos dentro del nombre de un plugin: separadores de ruta, el
#: marcador de habilitado y los metacaracteres de Windows. Los nombres son
#: nombres de archivo de Windows, no texto ASCII: todo byte imprimible de la
#: página de código (incluido el espacio interior) es admisible (ver
#: :func:`_validated_plugin_name`).
_PLUGIN_NAME_FORBIDDEN_BYTES = frozenset(b'*"<>:|?/\\')

#: Listado de contenido de Creation Club del juego: vive en el *game root* (al
#: lado del ejecutable) y `GameSkyrimSE::CCPlugins()` lo lee para armar
#: `primaryPlugins()`. Ausente o ilegible = "este juego no declara CC"; el
#: digest no se rompe, sólo cambia si el archivo aparece/desaparece/cambia.
_CC_PLUGINS_FILENAME = "Skyrim.ccc"

#: Masters base que el motor carga siempre, se listen o no en `plugins.txt`: se
#: fuerzan `ACTIVE` en el lector de MO2 (`CreationGamePlugins::readPluginList`)
#: y su escritor los omite por completo (`primaryPlugins()`), así que su línea no
#: aporta estado. Se reutiliza la política ya congelada de `plugin_sources` (un
#: solo hecho, una sola fuente) y el ancla de tests de este módulo vuelve a
#: congelar el conjunto efectivo. `primaryPlugins()` **no** es sólo esto:
#: `GameSkyrimSE::primaryPlugins()` agrega `CCPlugins()` (::data:`_CC_PLUGINS_FILENAME`);
#: el conjunto efectivo se arma en :func:`_always_active_plugins` con el game
#: root que cada lado ya recibe.
_ALWAYS_ACTIVE_PLUGINS = frozenset(name.lower() for name in OFFICIAL_MASTERS)

_IGNORED_ROOT_FILES = frozenset({"meta.ini"})


class VfsAttestationError(RuntimeError):
    """No se pudo probar una vista USVFS coherente con el perfil pedido."""


def _validated_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value.lower()):
        raise VfsAttestationError(f"{field} debe ser un SHA-256 hexadecimal")
    return value.lower()


@dataclass(frozen=True, slots=True)
class VfsAttestationChallenge:
    """Canary físico esperado dentro de la vista virtual del worker."""

    profile: str
    source_mod: str
    relative_path: pathlib.PurePosixPath
    sha256: str
    profile_fingerprint: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> VfsAttestationChallenge:
        profile = raw.get("profile")
        source_mod = raw.get("source_mod")
        relative_text = raw.get("relative_path")
        sha256 = raw.get("sha256")
        fingerprint = raw.get("profile_fingerprint")
        if not isinstance(profile, str) or not isinstance(source_mod, str):
            raise VfsAttestationError("profile y source_mod deben ser strings")
        if not isinstance(relative_text, str) or not relative_text:
            raise VfsAttestationError("relative_path debe ser un string no vacío")
        relative = pathlib.PurePosixPath(relative_text)
        if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
            raise VfsAttestationError("relative_path debe quedar dentro de Data")
        return cls(
            profile=_validated_profile(profile),
            source_mod=_validated_profile(source_mod),
            relative_path=relative,
            sha256=_validated_sha256(sha256, field="sha256"),
            profile_fingerprint=_validated_sha256(fingerprint, field="profile_fingerprint"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "profile": self.profile,
            "source_mod": self.source_mod,
            "relative_path": self.relative_path.as_posix(),
            "sha256": self.sha256,
            "profile_fingerprint": self.profile_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class VfsAttestationProof:
    """Evidencia observada por el worker antes de ejecutar una herramienta."""

    profile: str
    source_mod: str
    relative_path: pathlib.PurePosixPath
    visible_sha256: str
    profile_fingerprint: str

    def to_dict(self) -> dict[str, str]:
        return {
            "profile": self.profile,
            "source_mod": self.source_mod,
            "relative_path": self.relative_path.as_posix(),
            "visible_sha256": self.visible_sha256,
            "profile_fingerprint": self.profile_fingerprint,
        }


def _validated_profile(profile: str) -> str:
    try:
        return assert_safe_component(profile, field="profile")
    except PathViolationError as exc:
        raise VfsAttestationError(str(exc)) from exc


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise VfsAttestationError(f"no se pudo leer {path}: {exc}") from exc
    return digest.hexdigest()


def _enabled_mods(modlist_path: pathlib.Path) -> tuple[str, ...]:
    try:
        lines = modlist_path.read_text(encoding="utf-8-sig", errors="strict").splitlines()
    except (OSError, UnicodeError) as exc:
        raise VfsAttestationError(f"no se pudo leer el modlist del perfil: {exc}") from exc
    enabled: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(("#", "-", "*")):
            continue
        if not line.startswith("+") or not line[1:].strip():
            raise VfsAttestationError(f"línea inválida en modlist.txt: {line!r}")
        enabled.append(line[1:].strip())
    return tuple(enabled)


def _iter_mod_files(mod_root: pathlib.Path) -> Iterator[tuple[pathlib.Path, pathlib.Path]]:
    try:
        for current, dirs, files in os.walk(mod_root):
            dirs[:] = sorted(name for name in dirs if not name.endswith(".mohidden"))
            current_path = pathlib.Path(current)
            for name in sorted(files):
                source = current_path / name
                relative = source.relative_to(mod_root)
                if len(relative.parts) == 1 and name.casefold() in _IGNORED_ROOT_FILES:
                    continue
                if name.endswith(".mohidden") or source.is_symlink() or not source.is_file():
                    continue
                yield relative, source
    except OSError as exc:
        raise VfsAttestationError(f"no se pudo enumerar el mod {mod_root.name!r}: {exc}") from exc


def _iter_plugins_lines(data: bytes) -> Iterator[bytes]:
    """Separa líneas aceptando LF, CRLF y CR.

    `QFile::readLine` + `trimmed()` cubre LF y CRLF (ambos producen el mismo
    nombre); CR suelto se trata también como separador para que la
    canonicalización sea determinista en cualquier estilo de fin de línea.
    """
    return iter(data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n"))


def _case_insensitive_key(raw: bytes) -> str:
    """Identidad case-insensitive de un nombre de plugin (bytes sin codepage declarada).

    ``plugins.txt`` se escribe con encoding ``System`` (la página de código de la
    máquina), así que los nombres pueden traer bytes > 0x7E legítimos
    (``EspadaÉlfica.esp``) y no hay una decodificación UTF-8 que valga. `latin-1`
    es una biyección sobre bytes —nunca falla, nunca pierde información y es
    determinista para cualquier entrada— y su bloque de letras pares coincide con
    el de las páginas de código en juego:

    * **cp1252 (y latin-1)**: exacto. ``0xC0-0xDE``/``0xE0-0xFE`` son las mismas
      letras acentuadas y ``0x80-0x9F`` no tiene pares de caja en ninguna de las
      dos, así que el pareo coincide con el del sistema.
    * **cp1251 (cirílico)**: exacto en el bloque alfabético (``0xC0-0xDF`` ↔
      ``0xE0-0xFF``); ``Ё``/``ё`` (``0xA8``/``0xB8``) quedan como bytes distintos,
      igual que en un cotejo byte a byte — es la dirección conservadora: dos
      grafías que Windows uniría se tratan como estados distintos, nunca al
      revés.
    * **otras páginas de código** (p. ej. cp1250, cp932): determinista y estable,
      pero **no** se afirma equivalencia con la tabla de mayúsculas de Windows.
      La garantía declarada es la de los casos probados, no una promesa general.

    Se usa ``lower()`` y **no** ``casefold()``: `casefold` colapsa pares que
    Windows no unifica (``Straße.esp`` vs ``Strasse.esp``) y fabricaría un
    "duplicado" que no existe. Esta clave es también la **identidad** que entra
    al payload canónico, para que el digest no dependa de la grafía.
    """
    return raw.decode("latin-1").lower()


def _validated_plugin_name(raw: bytes) -> bytes:
    """Nombre de plugin admisible o ``VfsAttestationError`` (fail-closed).

    El contrato es el de un nombre de archivo de Windows (lo que el motor
    compara): bytes imprimibles de cualquier página de código —el espacio
    interior incluido, como en ``Unofficial Skyrim Special Edition Patch.esp``—
    sin caracteres de control, sin metacaracteres de Windows y con extensión de
    plugin. Un nombre que no se puede interpretar de forma determinista (UTF-16
    ⇒ NULs, caracteres de control, metacaracteres, sin extensión) no se ignora
    en silencio: se rechaza.
    """
    if not raw:
        raise VfsAttestationError("plugins.txt contiene una entrada sin nombre de plugin")
    for byte in raw:
        if byte < 0x20 or byte in _PLUGIN_NAME_FORBIDDEN_BYTES:
            raise VfsAttestationError(f"plugins.txt contiene un nombre de plugin inválido: {raw!r}")
    # `bytes.lower()` sólo toca letras ASCII, que es exactamente lo que hace
    # falta para la extensión (siempre ASCII) sin asumir la codepage del resto.
    if not raw.lower().endswith(_PLUGIN_EXTENSIONS):
        raise VfsAttestationError(
            f"plugins.txt contiene un nombre que no es de plugin (.esp/.esm/.esl): {raw.decode('latin-1')!r}"
        )
    return raw


def _iter_cc_plugin_keys(game_data_dir: pathlib.Path) -> Iterator[str]:
    """Claves case-insensitive de los plugins de Creation Club del juego.

    ``GameSkyrimSE::CCPlugins()`` lee ``Skyrim.ccc`` del *game root* (el padre
    del ``Data``) con ``forEachLineInFile`` y deduplica por minúsculas. Ese
    conjunto entra a ``primaryPlugins()``, así que sus líneas en ``plugins.txt``
    tampoco aportan estado. Un archivo ausente o ilegible no es un error: es un
    juego sin contenido CC declarado.
    """
    try:
        data = (game_data_dir.parent / _CC_PLUGINS_FILENAME).read_bytes()
    except OSError:
        return
    seen: set[str] = set()
    for raw_line in _iter_plugins_lines(data):
        line = raw_line.strip(b" \t")
        if not line:
            continue
        key = _case_insensitive_key(line)
        if key not in seen:
            seen.add(key)
            yield key


def _always_active_plugins(game_data_dir: pathlib.Path) -> frozenset[str]:
    """Conjunto efectivo de ``primaryPlugins()`` para el game root de ``Data``.

    Masters base (política congelada en :data:`_ALWAYS_ACTIVE_PLUGINS`) más el
    contenido de Creation Club declarado por el juego
    (:func:`_iter_cc_plugin_keys`). **No** se usa un prefijo ``cc``: un mod
    llamado ``ccAlgo.esp`` que el juego no declara sigue siendo estado del
    perfil.
    """
    return _ALWAYS_ACTIVE_PLUGINS | frozenset(_iter_cc_plugin_keys(game_data_dir))


def _canonical_plugins_state(data: bytes | None, *, always_active: frozenset[str]) -> bytes:
    """Estado semántico de ``plugins.txt`` en el dialecto Creation (SSE/AE).

    Contrato (evidencia: ``CreationGamePlugins::readPluginList`` y
    ``writePluginList`` + ``GameSkyrimSE::primaryPlugins``, MO2 2.5.2 → master;
    rig real de #586C en #633):

    * comentarios (``#``), líneas vacías y espacios alrededor del nombre no
      aportan estado — el lector los descarta o los reduce con `trimmed()`;
    * ``*nombre`` = plugin habilitado. ``nombre`` sin estrella = listado pero
      deshabilitado, y para el lector de MO2 "deshabilitado" y "ausente" son el
      **mismo** estado (ambos caminos llaman `setState(INACTIVE)`), así que las
      líneas sin estrella son inertes: una reserialización puede agregarlas o
      quitarlas;
    * los plugins de :data:`always_active` —``primaryPlugins()``: masters base
      más el contenido de Creation Club que declara el juego— se fuerzan
      `ACTIVE` y el escritor de MO2 no los emite, así que su línea —con o sin
      estrella— tampoco aporta estado;
    * BOM, header y estilo de fin de línea son representación.

    Lo que **sí** es estado: qué plugins no primarios están habilitados y en qué
    orden aparecen sus líneas. La identidad de cada uno es su **clave
    case-insensitive** (:func:`_case_insensitive_key`) y no la grafía del
    archivo: en un filesystem case-insensitive dos grafías del mismo nombre son
    el mismo plugin, así que hashearlas distinto volvería a atar el digest a una
    reescritura sin significado. Un nombre repetido —bajo esa misma clave— es
    ambiguo y el formato no lo admite: falla cerrado. La forma devuelta no es un
    archivo: es un payload canónico con esas claves separadas por ``NUL``; los
    nombres validados nunca contienen ``NUL``.
    """
    if data is None:
        # Ausente, vacío y "solo header" son el mismo estado para el lector de
        # MO2 (`pluginsTxtExists = false` ⇒ todo INACTIVE): sin plugins
        # habilitados no oficiales.
        return b""
    # BOM UTF-8: el lector de MO2 no lo reconoce como comentario en la primera
    # línea, pero tampoco lo transforma en un plugin real (un nombre con BOM no
    # matchea ningún archivo), así que no cambia el estado. Se descarta acá para
    # que un archivo con BOM no falle por "nombre inválido".
    if data.startswith(_UTF8_BOM):
        data = data[len(_UTF8_BOM) :]
    enabled: list[bytes] = []
    seen: set[str] = set()
    for raw_line in _iter_plugins_lines(data):
        line = raw_line.strip(b" \t")
        if not line or line.startswith(b"#"):
            continue
        starred = line.startswith(b"*")
        name_bytes = (line[1:] if starred else line).strip(b" \t")
        name = _validated_plugin_name(name_bytes)
        key = _case_insensitive_key(name)
        if key in seen:
            raise VfsAttestationError(f"plugins.txt repite el plugin {name.decode('latin-1')!r}")
        seen.add(key)
        if starred and key not in always_active:
            # Se guarda la IDENTIDAD case-insensitive, no la grafía del archivo:
            # en un filesystem case-insensitive `*RigCanary.esp` y
            # `*rigcanary.esp` son el MISMO plugin, y el digest no puede depender
            # de cómo lo escribió el último que tocó el perfil. `latin-1` es
            # biyectiva con los bytes, así que la clave vuelve a bytes sin
            # pérdida y sigue sin contener ``NUL`` (los rechaza el validador).
            enabled.append(key.encode("latin-1"))
    return b"\x00".join(enabled)


def _state_section(label: str, payload: bytes) -> bytes:
    """Sección con largo explícito: el contenido no puede reencuadrar el digest.

    El esquema v1 concatenaba ``\\0file\\0<nombre>`` con los bytes crudos, así
    que el contenido de un archivo podía imitar el delimitador de otra sección y
    hacer colisionar dos estados distintos (p. ej. `modlist.txt` vacío +
    `loadorder.txt` con el literal ``\\0file\\0loadorder.txt`` contra el reparto
    inverso). Con el largo por delante, cada (label, payload) produce una
    secuencia de bytes distinta y el digest es inyectivo.
    """
    return b"\x00state\x00" + label.encode("ascii") + b"\x00" + len(payload).to_bytes(8, "big") + payload


def _read_profile_file(path: pathlib.Path) -> bytes | None:
    """Bytes del archivo, ``None`` si no existe; otro ``OSError`` falla cerrado."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise VfsAttestationError(f"no se pudo fingerprintar {path}: {exc}") from exc


def _raw_file_payload(path: pathlib.Path) -> bytes:
    """Payload de un archivo hasheado en crudo: distingue "falta" de "vacío"."""
    data = _read_profile_file(path)
    return b"\x00" if data is None else b"\x01" + data


def _profile_fingerprint(
    *,
    profile_dir: pathlib.Path,
    profile: str,
    source_mod: str,
    relative_path: pathlib.PurePosixPath,
    canary_sha256: str,
    always_active: frozenset[str],
) -> str:
    digest = hashlib.sha256()
    digest.update(_PROFILE_FINGERPRINT_DOMAIN)
    digest.update(_state_section("profile", profile.encode("utf-8")))
    digest.update(
        _state_section(
            f"file:{_PLUGINS_STATE_FILE}",
            _canonical_plugins_state(
                _read_profile_file(profile_dir / _PLUGINS_STATE_FILE),
                always_active=always_active,
            ),
        )
    )
    for filename in _RAW_PROFILE_STATE_FILES:
        digest.update(_state_section(f"file:{filename}", _raw_file_payload(profile_dir / filename)))
    digest.update(
        _state_section(
            "canary",
            source_mod.encode("utf-8")
            + b"\x00"
            + relative_path.as_posix().encode("utf-8")
            + b"\x00"
            + canary_sha256.encode("ascii"),
        )
    )
    return digest.hexdigest()


def build_attestation_challenge(
    *,
    mo2_root: pathlib.Path | None = None,
    data_root: pathlib.Path | None = None,
    mods_dir: pathlib.Path | None = None,
    profile: str,
    physical_data_dir: pathlib.Path,
) -> VfsAttestationChallenge:
    """Elige un archivo efectivo de mod que el ``Data`` físico no contiene.

    El conjunto de plugins que el motor carga siempre (``primaryPlugins()``) se
    deriva del game root de ``physical_data_dir``; el worker deriva el mismo
    conjunto del game root de su ``virtual_data_dir``. En producción ambos son
    el mismo ``<juego>/Data`` (USVFS expone la misma ruta que lee el preview).
    """
    profile_name = _validated_profile(profile)
    root = data_root or mo2_root
    if root is None:
        raise VfsAttestationError("se requiere data_root o mo2_root")
    data_resolved = root.resolve()
    mods_resolved = mods_dir.resolve() if mods_dir is not None else (data_resolved / "mods")
    data = physical_data_dir.resolve()
    always_active = _always_active_plugins(data)
    profile_dir = data_resolved / "profiles" / profile_name
    enabled = _enabled_mods(profile_dir / "modlist.txt")
    if not enabled:
        raise VfsAttestationError("el perfil no tiene mods habilitados para construir un canary elegible")

    # modlist.txt crece de menor a mayor prioridad en el contrato vigente del
    # proyecto. Al bajar desde el final, descartamos archivos reemplazados por
    # overwrite o por un mod de prioridad mayor.
    higher_roots: list[pathlib.Path] = [data_resolved / "overwrite"]
    for mod_name in reversed(enabled):
        safe_mod = _validated_profile(mod_name)
        mod_root = mods_resolved / safe_mod
        if not mod_root.is_dir():
            raise VfsAttestationError(f"el mod habilitado {mod_name!r} no existe en {mods_resolved}")
        for relative, source in _iter_mod_files(mod_root):
            if (data / relative).exists():
                continue
            if any((higher / relative).exists() for higher in higher_roots if higher.is_dir()):
                continue
            sha256 = _sha256_file(source)
            relative_posix = pathlib.PurePosixPath(*relative.parts)
            fingerprint = _profile_fingerprint(
                profile_dir=profile_dir,
                profile=profile_name,
                source_mod=mod_name,
                relative_path=relative_posix,
                canary_sha256=sha256,
                always_active=always_active,
            )
            return VfsAttestationChallenge(
                profile=profile_name,
                source_mod=mod_name,
                relative_path=relative_posix,
                sha256=sha256,
                profile_fingerprint=fingerprint,
            )
        higher_roots.append(mod_root)

    raise VfsAttestationError(
        "no existe un canary elegible: todo archivo de mod también está en Data físico o queda sobrescrito"
    )


def verify_vfs_attestation(
    *,
    challenge: VfsAttestationChallenge,
    mo2_root: pathlib.Path | None = None,
    data_root: pathlib.Path | None = None,
    mods_dir: pathlib.Path | None = None,
    profile: str,
    virtual_data_dir: pathlib.Path,
) -> VfsAttestationProof:
    """Verifica fingerprint y visibilidad/hash antes de cualquier mutación.

    El conjunto ``primaryPlugins()`` se deriva del game root del
    ``virtual_data_dir`` (la ruta ``<juego>/Data`` que ve el proceso atestado
    bajo USVFS); es el mismo game root con el que el preview construyó el
    challenge en todos los call sites de producción.
    """
    profile_name = _validated_profile(profile)
    if profile_name != challenge.profile:
        raise VfsAttestationError(f"perfil incorrecto: worker={profile_name!r}, challenge={challenge.profile!r}")
    root = data_root or mo2_root
    if root is None:
        raise VfsAttestationError("se requiere data_root o mo2_root")
    data_resolved = root.resolve()
    mods_resolved = mods_dir.resolve() if mods_dir is not None else (data_resolved / "mods")
    relative = pathlib.Path(*challenge.relative_path.parts)
    source = mods_resolved / _validated_profile(challenge.source_mod) / relative
    current_source_sha = _sha256_file(source)
    if current_source_sha != challenge.sha256:
        raise VfsAttestationError("el canary cambió después del preview")
    current_fingerprint = _profile_fingerprint(
        profile_dir=data_resolved / "profiles" / profile_name,
        profile=profile_name,
        source_mod=challenge.source_mod,
        relative_path=challenge.relative_path,
        canary_sha256=current_source_sha,
        always_active=_always_active_plugins(virtual_data_dir.resolve()),
    )
    if current_fingerprint != challenge.profile_fingerprint:
        raise VfsAttestationError("fingerprint del perfil cambió después del preview")

    visible = virtual_data_dir.resolve() / relative
    if not visible.is_file():
        raise VfsAttestationError(f"canary no visible bajo USVFS: {challenge.relative_path.as_posix()}")
    visible_sha = _sha256_file(visible)
    if visible_sha != challenge.sha256:
        raise VfsAttestationError("el canary visible no coincide con el hash esperado")
    return VfsAttestationProof(
        profile=profile_name,
        source_mod=challenge.source_mod,
        relative_path=challenge.relative_path,
        visible_sha256=visible_sha,
        profile_fingerprint=current_fingerprint,
    )
