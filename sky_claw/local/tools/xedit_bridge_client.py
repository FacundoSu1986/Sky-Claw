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
from pathlib import Path
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


class BridgeError(RuntimeError):
    """Falla del bridge o del protocolo. Nunca es un veredicto sobre el plugin auditado."""


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
        (Python 3.11 del sistema, fuera del sandbox de Sky-Claw), args y env. Leerlo de ahí evita una
        segunda variable de entorno —una segunda fuente de verdad— y que ese intérprete tenga que
        saltarse el ``PathValidator``. Se adopta COMPLETO (el cwd declarado es la propia raíz): tomar
        solo una parte lanzaría el bridge distinto de como se validó. Se exige que el TOML describa
        ESTA instalación.
        """
        import tomllib

        toml = raiz / "codex-mcp.example.toml"
        try:
            servidor = tomllib.loads(toml.read_text(encoding="utf-8"))["mcp_servers"]["xedit_bridge_local"]
            comando, args = Path(servidor["command"]), [Path(a) for a in servidor["args"]]
            entorno = servidor.get("env", {})
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise BridgeError(f"no se pudo leer el intérprete del xEdit Bridge desde {toml}: {exc}") from exc
        if not isinstance(entorno, dict) or not all(
            isinstance(clave, str) and isinstance(valor, str) for clave, valor in entorno.items()
        ):
            raise BridgeError(f"el env de {toml} no es un mapa de texto a texto")
        if len(args) < 4 or args[3] != raiz / "run_mcp.py":
            raise BridgeError(f"{toml} describe otra instalación del bridge, no {raiz}")
        if (
            not comando.is_absolute()
            or not comando.name.lower().startswith("python")
            or comando.suffix.lower() != ".exe"
        ):
            raise BridgeError(f"el comando del xEdit Bridge no es un intérprete Python absoluto: {comando}")
        instalacion = cls(python=comando, raiz=raiz, entorno=dict(entorno))
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
        respuestas[respuesta.get("id")] = respuesta
    if codigo != 0:
        raise BridgeError(f"el xEdit Bridge terminó con código {codigo}: {stderr.strip()[-300:]}")
    inicio, llamada = respuestas.get(1), respuestas.get(2)
    if inicio is None or llamada is None:
        raise BridgeError("el xEdit Bridge no respondió a initialize y tools/call")
    for respuesta in (inicio, llamada):
        if "error" in respuesta:
            raise BridgeError(f"error JSON-RPC del xEdit Bridge: {respuesta['error']}")
    info = (inicio.get("result") or {}).get("serverInfo") or {}
    if info.get("name") != NOMBRE_SERVIDOR:
        raise BridgeError(f"el servidor no es el xEdit Bridge: {info.get('name')!r}")
    version = str(info.get("version"))
    if not VERSION_MINIMA <= _version(version) < VERSION_TOPE:
        raise BridgeError(f"versión del xEdit Bridge no soportada: {version} (se requiere >=1.1.0 y <2)")
    resultado = llamada.get("result") or {}
    contenido = resultado.get("content") or []
    es_error = resultado.get("isError")
    if not isinstance(es_error, bool) or len(contenido) != 1 or contenido[0].get("type") != "text":
        raise BridgeError("respuesta de xedit_check con forma inesperada")
    try:
        payload = json.loads(contenido[0]["text"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise BridgeError("el resultado de xedit_check no es JSON") from exc
    if not isinstance(payload, dict):
        raise BridgeError("el resultado de xedit_check no es un objeto")
    # Contrato del bridge (mcp_server._text_result): isError == not ok. Un veredicto negativo (chequeo
    # incompleto, plugin no admitido) viaja con isError=true y SE LEE; la incoherencia es un protocolo roto.
    if es_error is (payload.get("ok") is True):
        raise BridgeError("respuesta de xedit_check incoherente: isError no coincide con ok")
    return RespuestaAuditoria(payload=payload, version=version)


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
    "PATRON_PLUGIN",
    "BridgeError",
    "InstalacionBridge",
    "RespuestaAuditoria",
    "admitir_plugin",
    "auditar_plugin",
    "validar_plugin",
]
