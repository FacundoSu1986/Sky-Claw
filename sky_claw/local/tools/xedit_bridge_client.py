"""Cliente del xEdit Bridge (auditoría nativa de SSEEdit, ADR 0018).

El bridge es un proceso aparte, con su propio laboratorio y gate de integridad. Sky-Claw no lo
importa: habla con él como cualquier cliente MCP, por stdio, en **una sesión por llamada**
(``initialize`` → ``notifications/initialized`` → ``tools/call``, stdin cerrado, el servidor
termina solo). La admisión de un plugin real NO es una herramienta MCP del bridge: es su comando
``run_admit.py``, que Sky-Claw ejecuta solo después de una aprobación humana.

Todo falla cerrado: un servidor que no es el bridge, una versión fuera de rango, una respuesta que
no es JSON-RPC, un error RPC, un exit code distinto de 0 o un timeout levantan :class:`BridgeError`,
que nunca es un veredicto sobre el plugin.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

from sky_claw.local.tools._process import run_capture

PROTOCOLO_MCP = "2025-06-18"
NOMBRE_SERVIDOR = "xedit-bridge"
#: Rango de versiones del bridge que este cliente entiende: >= 1.1.0 (admisión de plugins reales) y < 2.
VERSION_MINIMA = (1, 1, 0)
VERSION_TOPE = (2, 0, 0)
#: Margen sobre el timeout del motor: hashes de integridad, carga de masters y cierre del bridge.
MARGEN_TIMEOUT_S = 60.0
TIMEOUT_ADMISION_S = 900.0
TIMEOUT_MINIMO, TIMEOUT_MAXIMO = 5, 120
#: Nombre de plugin sin ruta. Única fuente: lo reusan los modelos de parámetros de las dos
#: superficies (``schemas.py`` y ``tool_strategies``); sintaxis válida para ``re`` y para Pydantic.
PATRON_PLUGIN = r"^[^\\/:*?\"<>|\x00-\x1f]+\.(?:[eE][sS][mMpPlL])$"
_PLUGIN = re.compile(PATRON_PLUGIN)
#: Largo máximo del nombre tal como lo ve el operador (con sus escapes): tiene que entrar ENTERO en
#: el pedido de aprobación de las dos superficies (el gate de la GUI acota su detalle a 800).
LARGO_MAXIMO_NOMBRE_VISIBLE = 400
_INTERPRETE_WINDOWS = re.compile(r"python(?:\d+(?:\.\d+)*)?w?\.exe", re.IGNORECASE)
_INTERPRETE_POSIX = re.compile(r"python(?:\d+(?:\.\d+)*)?")
#: ``Default_Ignorable_Code_Point`` de Unicode (DerivedCoreProperties.txt): no se muestran, aunque Python
#: da por imprimibles a varios (marcas Mn como el COMBINING GRAPHEME JOINER, letras de relleno Lo).
_IGNORABLES_POR_DEFECTO = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)


def _es_ignorable(c: str) -> bool:
    punto = ord(c)
    return any(desde <= punto <= hasta for desde, hasta in _IGNORABLES_POR_DEFECTO)


class BridgeError(RuntimeError):
    """Falla del bridge o del protocolo. Nunca es un veredicto sobre el plugin auditado."""


def _es_interprete_python(comando: PurePath, *, windows: bool) -> bool:
    """``comando`` es un intérprete Python absoluto: ``python*.exe`` en Windows, ``python*`` en POSIX."""
    patron = _INTERPRETE_WINDOWS if windows else _INTERPRETE_POSIX
    return comando.is_absolute() and patron.fullmatch(comando.name) is not None


def nombre_visible(plugin: str) -> str:
    """``plugin`` tal como tiene que verlo el operador al aprobar: completo y sin ambigüedad.

    ``PATRON_PLUGIN`` acepta caracteres que no se ven o engañan a la vista: formato Unicode (un
    override de dirección como U+202E reordena lo que se muestra), espacios de ancho cero, NBSP,
    controles C1 y los ignorables por defecto que Python da por imprimibles (U+034F). Todo carácter
    no imprimible o ignorable se muestra escapado (``\\u202e``); como el patrón
    prohíbe la barra invertida, un escape nunca se confunde con texto del nombre. El nombre que se
    ejecuta NO cambia. Uno que no entra entero en el pedido se rechaza: no se aprueba a ciegas.
    """
    visible = "".join(
        c
        if c.isprintable() and not _es_ignorable(c)
        else (f"\\u{ord(c):04x}" if ord(c) <= 0xFFFF else f"\\U{ord(c):08x}")
        for c in plugin
    )
    if len(visible) > LARGO_MAXIMO_NOMBRE_VISIBLE:
        raise ValueError(
            f"el nombre del plugin no se puede mostrar entero para aprobarlo ({len(visible)} caracteres visibles)"
        )
    return visible


@dataclass(frozen=True)
class InstalacionBridge:
    """Instalación del bridge: su intérprete, su carpeta ``BridgeTrabajo`` y el env que declara.

    ``entorno`` son las variables que el bridge pide para su proceso (sus ``TEMP``/``APPDATA``
    dentro del laboratorio); se SUMAN al entorno heredado al lanzarlo.
    """

    python: Path
    raiz: Path
    entorno: dict[str, str] = field(default_factory=dict)

    @property
    def config(self) -> Path:
        return self.raiz / "config.json"

    @classmethod
    def desde_raiz(cls, raiz: Path) -> InstalacionBridge:
        """Instalación a partir de su carpeta ``BridgeTrabajo``, lanzada como el propio bridge declara.

        El instalador del bridge escribe en ``codex-mcp.example.toml`` el arranque exacto: comando
        (Python 3.11 del sistema, fuera del sandbox de Sky-Claw), args, cwd y env. Leerlo de ahí evita
        una segunda variable de entorno —una segunda fuente de verdad— y que ese intérprete tenga que
        saltarse el ``PathValidator``. Se adopta COMPLETO: los args declarados tienen que ser
        EXACTAMENTE los que Sky-Claw ejecuta (:meth:`comando`) y el cwd, si se declara, la propia raíz.
        Tomar solo una parte lanzaría el bridge distinto de como se validó.
        """
        import tomllib

        toml = raiz / "codex-mcp.example.toml"
        try:
            servidor = tomllib.loads(toml.read_text(encoding="utf-8"))["mcp_servers"]["xedit_bridge_local"]
            comando, args = Path(servidor["command"]), servidor["args"]
            cwd, entorno = servidor.get("cwd"), servidor.get("env", {})
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise BridgeError(f"no se pudo leer el intérprete del xEdit Bridge desde {toml}: {exc}") from exc
        if not isinstance(entorno, dict) or not all(
            isinstance(clave, str) and isinstance(valor, str) for clave, valor in entorno.items()
        ):
            raise BridgeError(f"el env de {toml} no es un mapa de texto a texto")
        if not _es_interprete_python(comando, windows=os.name == "nt"):
            raise BridgeError(f"el comando del xEdit Bridge no es un intérprete Python absoluto: {comando}")
        instalacion = cls(python=comando, raiz=raiz, entorno=dict(entorno))
        esperados = [Path(a) for a in instalacion.comando("run_mcp.py")[1:]]
        if (
            not isinstance(args, list)
            or not all(isinstance(a, str) for a in args)
            or [Path(a) for a in args] != esperados
        ):
            raise BridgeError(
                f"{toml} describe otra instalación del bridge o un arranque distinto del que se ejecuta "
                f"(se esperaba {[str(a) for a in esperados]})"
            )
        if cwd is not None and (not isinstance(cwd, str) or Path(cwd) != raiz):
            raise BridgeError(f"el cwd de {toml} no es la raíz de esta instalación ({raiz}): {cwd!r}")
        instalacion.validar()
        return instalacion

    def validar(self) -> None:
        if not self.python.is_file():
            raise BridgeError(f"Python del xEdit Bridge no encontrado: {self.python}")
        for nombre in ("run_mcp.py", "run_admit.py", "config.json"):
            if not (self.raiz / nombre).is_file():
                raise BridgeError(f"Instalación del xEdit Bridge incompleta: falta {nombre} en {self.raiz}")

    def comando(self, script: str, *args: str) -> list[str]:
        # -I -S -B: el mismo arranque aislado y sin bytecode que documenta el bridge.
        return [str(self.python), "-I", "-S", "-B", str(self.raiz / script), str(self.config), *args]


@dataclass(frozen=True)
class RespuestaAuditoria:
    """Respuesta cruda del bridge a ``xedit_check`` y la versión que declaró el servidor."""

    payload: dict[str, Any]
    version: str


def validar_plugin(nombre: str) -> str:
    """Nombre de plugin sin ruta (``.esm``/``.esp``/``.esl``); cualquier otra cosa se rechaza."""
    if (
        not isinstance(nombre, str)
        or not _PLUGIN.fullmatch(nombre)
        or Path(nombre).name != nombre
        or nombre.strip() != nombre
    ):
        raise ValueError(f"nombre de plugin inválido: {nombre!r}")
    return nombre


def _validar_componente(tipo: str, valor: str) -> str:
    if not valor or valor in (".", "..") or any(c in valor for c in "\\/:") or valor.strip() != valor:
        raise ValueError(f"{tipo} de MO2 inválido: {valor!r}")
    return valor


def _version(texto: Any) -> tuple[int, int, int]:
    partes = str(texto).split(".")
    if len(partes) != 3 or not all(p.isdigit() for p in partes):
        raise BridgeError(f"versión del xEdit Bridge ilegible: {texto!r}")
    return int(partes[0]), int(partes[1]), int(partes[2])


def _decodificar(salida: bytes) -> str:
    # run_mcp.py fija UTF-8; run_admit.py imprime con la codificación de consola (cp1252) cuando es un pipe.
    try:
        return salida.decode("utf-8")
    except UnicodeDecodeError:
        return salida.decode("cp1252", errors="replace")


async def _ejecutar(
    instalacion: InstalacionBridge, comando: list[str], *, timeout: float, entrada: bytes | None = None
) -> tuple[str, str, int]:
    entorno = {**os.environ, **instalacion.entorno} if instalacion.entorno else None
    try:
        stdout, stderr, codigo = await run_capture(
            comando, timeout=timeout, cwd=str(instalacion.raiz), input_bytes=entrada, env=entorno
        )
    except TimeoutError as exc:
        raise BridgeError(
            f"el xEdit Bridge superó el tiempo límite ({timeout:.0f} s); el proceso fue terminado"
        ) from exc
    except OSError as exc:  # ejecutable ausente, acceso denegado, estado indeterminado de run_capture
        raise BridgeError(f"no se pudo lanzar el xEdit Bridge: {exc}") from exc
    return _decodificar(stdout), _decodificar(stderr), codigo


async def auditar_plugin(
    instalacion: InstalacionBridge,
    plugin: str,
    *,
    timeout: int,
    margen_s: float = MARGEN_TIMEOUT_S,
) -> RespuestaAuditoria:
    """Ejecuta ``xedit_check(plugin, profile="admitted")`` en una sesión MCP propia."""
    plugin = validar_plugin(plugin)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not TIMEOUT_MINIMO <= timeout <= TIMEOUT_MAXIMO:
        raise ValueError(f"timeout fuera de rango ({TIMEOUT_MINIMO}-{TIMEOUT_MAXIMO} s): {timeout!r}")
    instalacion.validar()
    mensajes = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOLO_MCP,
                "capabilities": {},
                "clientInfo": {"name": "sky-claw", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "xedit_check",
                "arguments": {"plugin": plugin, "profile": "admitted", "max_errors": 500, "timeout": timeout},
            },
        },
    ]
    entrada = "".join(json.dumps(m) + "\n" for m in mensajes).encode("utf-8")
    stdout, stderr, codigo = await _ejecutar(
        instalacion,
        instalacion.comando("run_mcp.py"),
        timeout=timeout + margen_s,
        entrada=entrada,
    )
    respuestas: dict[Any, dict[str, Any]] = {}
    for linea in stdout.splitlines():
        if not linea.strip():
            continue
        try:
            respuesta = json.loads(linea)
        except json.JSONDecodeError as exc:
            raise BridgeError("el xEdit Bridge devolvió una línea que no es JSON-RPC") from exc
        if not isinstance(respuesta, dict) or respuesta.get("jsonrpc") != "2.0":
            raise BridgeError("el xEdit Bridge devolvió una respuesta JSON-RPC inválida")
        ident = respuesta.get("id")
        # JSON-RPC solo admite texto, número o null. Una lista u objeto no es clave de dict (TypeError), y un
        # bool se confundiría con un número (True == 1, el id de ``initialize``).
        if isinstance(ident, bool) or not (ident is None or isinstance(ident, (int, str))):
            raise BridgeError(f"el xEdit Bridge devolvió un id JSON-RPC inválido ({type(ident).__name__})")
        respuestas[ident] = respuesta
    if codigo != 0:
        raise BridgeError(f"el xEdit Bridge terminó con código {codigo}: {stderr.strip()[-300:]}")
    inicio, llamada = respuestas.get(1), respuestas.get(2)
    if inicio is None or llamada is None:
        raise BridgeError("el xEdit Bridge no respondió a initialize y tools/call")
    for respuesta in (inicio, llamada):
        if "error" in respuesta:
            raise BridgeError(f"error JSON-RPC del xEdit Bridge: {respuesta['error']}")
    # Toda forma inesperada es BridgeError: un AttributeError/TypeError atravesaría el servicio, que solo
    # convierte BridgeError y ValueError en un resultado success=False.
    info = _objeto(_objeto(inicio.get("result")).get("serverInfo"))
    if info.get("name") != NOMBRE_SERVIDOR:
        raise BridgeError(f"el servidor no es el xEdit Bridge: {info.get('name')!r}")
    version = str(info.get("version"))
    if not VERSION_MINIMA <= _version(version) < VERSION_TOPE:
        raise BridgeError(f"versión del xEdit Bridge no soportada: {version} (se requiere >=1.1.0 y <2)")
    resultado = llamada.get("result")
    if not isinstance(resultado, dict):
        raise BridgeError("respuesta de xedit_check con forma inesperada")
    contenido, es_error = resultado.get("content"), resultado.get("isError")
    if (
        not isinstance(es_error, bool)
        or not isinstance(contenido, list)
        or len(contenido) != 1
        or not isinstance(contenido[0], dict)
        or contenido[0].get("type") != "text"
        or not isinstance(contenido[0].get("text"), str)
    ):
        raise BridgeError("respuesta de xedit_check con forma inesperada")
    try:
        payload = json.loads(contenido[0]["text"])
    except json.JSONDecodeError as exc:
        raise BridgeError("el resultado de xedit_check no es JSON") from exc
    if not isinstance(payload, dict):
        raise BridgeError("el resultado de xedit_check no es un objeto")
    ok = payload.get("ok")
    if not isinstance(ok, bool):
        raise BridgeError("el resultado de xedit_check no trae un ok booleano: no hay veredicto")
    # Contrato del bridge (mcp_server._text_result): isError == not ok. Un veredicto negativo (chequeo
    # incompleto, plugin no admitido) viaja con isError=true y SE LEE; la incoherencia es un protocolo roto.
    if es_error is ok:
        raise BridgeError("respuesta de xedit_check incoherente: isError no coincide con ok")
    return RespuestaAuditoria(payload=payload, version=version)


def _objeto(valor: Any) -> dict[str, Any]:
    """Un objeto JSON del protocolo; cualquier otra forma cuenta como vacío (y falla en la validación)."""
    return valor if isinstance(valor, dict) else {}


async def admitir_plugin(
    instalacion: InstalacionBridge,
    mo2: Path,
    perfil: str,
    plugin: str,
    *,
    timeout_s: float = TIMEOUT_ADMISION_S,
) -> dict[str, Any]:
    """Ejecuta ``run_admit.py`` (copia el plugin y sus masters al laboratorio del bridge; MO2 no se toca).

    Devuelve el JSON del bridge tal cual: ``ok=False`` es un rechazo de dominio (p. ej. un master
    faltante), no una excepción. Solo debe llamarse después de una aprobación humana.
    """
    plugin = validar_plugin(plugin)
    perfil = _validar_componente("perfil", perfil)
    instalacion.validar()
    stdout, stderr, codigo = await _ejecutar(
        instalacion,
        instalacion.comando("run_admit.py", "--mo2", str(mo2), "--profile", perfil, "--plugin", plugin),
        timeout=timeout_s,
    )
    lineas = [linea for linea in stdout.splitlines() if linea.strip()]
    try:
        datos = json.loads(lineas[-1]) if lineas else None
    except json.JSONDecodeError:
        datos = None
    if not isinstance(datos, dict) or not isinstance(datos.get("ok"), bool):
        raise BridgeError(f"run_admit.py no devolvió JSON (código {codigo}): {(stdout + stderr).strip()[-300:]}")
    if datos["ok"] and codigo != 0:
        raise BridgeError(f"run_admit.py informó ok con código {codigo}")
    return datos


__all__ = [
    "LARGO_MAXIMO_NOMBRE_VISIBLE",
    "PATRON_PLUGIN",
    "BridgeError",
    "InstalacionBridge",
    "RespuestaAuditoria",
    "admitir_plugin",
    "auditar_plugin",
    "nombre_visible",
    "validar_plugin",
]
