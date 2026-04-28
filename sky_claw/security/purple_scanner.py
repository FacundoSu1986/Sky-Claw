"""
Sky-Claw PurpleScanner v5.5 (Abril 2026)
Enterprise-Grade AST Security Analyzer con Taint Tracking.
Diseñado para detectar inyección de código sin usar palabras clave literales.
Extensión Anti-Malware: Soporte para .bat, .ps1, .ini
"""

import ast
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Sinks peligrosos (Donde los datos sucios NO deben llegar)
DANGEROUS_SINKS = {
    "exec",
    "eval",
    "system",
    "popen",
    "subprocess.run",
    "subprocess.call",
    "subprocess.Popen",
    "compile",
    "os.system",
    "os.popen",
    "posix.system",
    "builtins.exec",
}

# Funciones que pueden indicar ofuscación
OBFUSCATION_INDICATORS = {"getattr", "setattr", "chr", "ord", "base64.b64decode"}

# Extensiones de archivo soportadas para escaneo de texto
TEXT_SCAN_EXTENSIONS = {".bat", ".ps1", ".ini"}

# Patrones regex compilados para detección de payloads maliciosos
MALICIOUS_PAYLOAD_PATTERNS = [
    # Ofuscación PowerShell
    (
        re.compile(r"powershell\s+-enc", re.IGNORECASE),
        "Ofuscación PowerShell detectada: comando -enc",
    ),
    (
        re.compile(r"-WindowStyle\s+Hidden", re.IGNORECASE),
        "Ejecución oculta PowerShell detectada: -WindowStyle Hidden",
    ),
    # Ransomware
    (
        re.compile(r"vssadmin\s+delete\s+shadows", re.IGNORECASE),
        "Comando ransomware detectado: eliminación de shadow copies",
    ),
    # Secuestro de DLL
    (
        re.compile(r"LoadLibrary", re.IGNORECASE),
        "Posible secuestro de DLL: LoadLibrary detectado",
    ),
    (
        re.compile(r"rundll32\s+", re.IGNORECASE),
        "Invocación rundll32 detectada: posible secuestro de DLL",
    ),
]


class PurpleScanner(ast.NodeVisitor):
    def __init__(self, filename: str = "<unknown>"):
        self.filename = filename
        self.findings: list[dict[str, Any]] = []
        self.tainted_vars: set[str] = set()
        self.const_map: dict[str, str] = {}  # Seguimiento simple de constantes string
        self._current_scope: str = "global"

    def report(
        self,
        message: str,
        node: ast.AST,
        severity: str = "HIGH",
        confidence: float = 0.9,
    ):
        self.findings.append(
            {
                "message": message,
                "line": node.lineno,
                "col": node.col_offset,
                "severity": severity,
                "confidence": confidence,
                "file": self.filename,
            }
        )

    def _resolve_const(self, node: ast.AST) -> str | None:
        """Intenta resolver el valor de un nodo a un string constante."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self._resolve_const(node.left)
            right = self._resolve_const(node.right)
            if left is not None and right is not None:
                return left + right
        elif isinstance(node, ast.Name) and node.id in self.const_map:
            return self.const_map[node.id]
        return None

    def visit_Assign(self, node: ast.Assign):  # noqa: N802
        """Rastrea asignaciones de constantes y variables 'sucias' (tainted)."""
        value_const = self._resolve_const(node.value)

        # Si el valor es una constante, registrarla
        for target in node.targets:
            if isinstance(target, ast.Name):
                if value_const is not None:
                    self.const_map[target.id] = value_const

                # Taint tracking simplificado: Si la fuente es sospechosa, la variable es 'sucia'
                if self._is_tainted_source(node.value):
                    self.tainted_vars.add(target.id)
                elif target.id in self.tainted_vars:
                    # Si era sucia pero se le asigna algo limpio, se limpia (opcional)
                    self.tainted_vars.remove(target.id)

        self.generic_visit(node)

    def _is_tainted_source(self, node: ast.AST) -> bool:
        """Determina si un nodo es una fuente de datos no confiables."""
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                return node.func.id in ("input", "open", "__import__")
            if isinstance(node.func, ast.Attribute):
                return node.func.attr == "read"
        return False

    def visit_Call(self, node: ast.Call):  # noqa: N802
        """Inspecciona llamadas a funciones buscando sinks peligrosos y ofuscación."""
        func_name = self._get_func_name(node.func)

        # 1. Detección de Sinks Directos e Indirectos
        if func_name in DANGEROUS_SINKS:
            self.report(
                f"Llamada a sink peligroso detectada: {func_name}",
                node,
                severity="CRITICAL",
            )

        # 2. Detección de ofuscación vía getattr(__builtins__, 'exec')
        if func_name == "getattr" and len(node.args) >= 2:
            attr_value = self._resolve_const(node.args[1])
            if attr_value in DANGEROUS_SINKS:
                self.report(
                    f"Ofuscación por getattr detectada ('{attr_value}')",
                    node,
                    severity="CRITICAL",
                    confidence=0.95,
                )

        # 3. Taint Tracking: ¿Se está pasando variable sucia a un sink?
        for arg in node.args:
            if (
                isinstance(arg, ast.Name)
                and arg.id in self.tainted_vars
                and (func_name in DANGEROUS_SINKS or func_name in OBFUSCATION_INDICATORS)
            ):
                self.report(
                    f"Variable sucia '{arg.id}' fluyendo hacia sink '{func_name}'",
                    node,
                    severity="HIGH",
                )

        # 4. Ofuscación por chr() o base64 en llamadas
        if func_name in OBFUSCATION_INDICATORS:
            self.report(
                f"Indicador de ofuscación detectado: {func_name}",
                node,
                severity="MEDIUM",
                confidence=0.7,
            )

        self.generic_visit(node)

    def _get_func_name(self, node: ast.AST) -> str:
        """Extrae el nombre completo de una función (ej. 'os.system')."""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            base = self._get_func_name(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return ""

    def visit_Import(self, node: ast.Import):  # noqa: N802
        for name in node.names:
            if name.name in ("os", "subprocess", "builtin", "importlib"):
                self.report(
                    f"Importación de módulo sensible: {name.name}",
                    node,
                    severity="LOW",
                    confidence=0.5,
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):  # noqa: N802
        if node.module in ("os", "subprocess", "importlib"):
            self.report(
                f"Importación parcial de módulo sensible: {node.module}",
                node,
                severity="LOW",
            )
        self.generic_visit(node)


def _read_file_with_fallback(filepath: Path) -> str:
    """Read *filepath* with UTF-8 first, then latin-1 as fallback.

    ``errors="replace"`` is preferred over ``errors="ignore"`` to preserve
    line structure — dropped bytes can shift regex matches and cause the
    payload scanner to miss malicious patterns in non-UTF-8 scripts.
    """
    try:
        return filepath.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        logger.debug("UTF-8 decode failed for %s, falling back to latin-1", filepath)
        return filepath.read_text(encoding="latin-1", errors="replace")


def _scan_text_payloads(content: str, filename: str) -> list[dict[str, Any]]:
    """
    Escanea archivos de texto (.bat, .ps1, .ini) buscando payloads maliciosos.

    Args:
        content: Contenido del archivo
        filename: Nombre del archivo

    Returns:
        Lista de hallazgos detectados
    """
    findings: list[dict[str, Any]] = []

    # Escanear contra patrones maliciosos
    for line_num, line in enumerate(content.splitlines(), start=1):
        for pattern, description in MALICIOUS_PAYLOAD_PATTERNS:
            if pattern.search(line):
                logger.critical("AMENAZA DETECTADA en %s:%d - %s", filename, line_num, description)
                findings.append(
                    {
                        "message": description,
                        "line": line_num,
                        "severity": "CRITICAL",
                        "confidence": 0.95,
                        "file": filename,
                        "matched_line": line.strip()[:200],  # Truncar líneas largas
                    }
                )

    return findings


def run_scan(code: str, filename: str = "main.py") -> list[dict[str, Any]]:
    """
    Punto de entrada principal para el escaneo de seguridad.

    Soporta:
    - Archivos .py: escaneo AST completo
    - Archivos .bat, .ps1, .ini: escaneo de payloads maliciosos
    """
    filepath = Path(filename)
    extension = filepath.suffix.lower()

    # Routing según extensión
    if extension in TEXT_SCAN_EXTENSIONS:
        return _scan_text_payloads(code, filename)

    # Escaneo AST para archivos Python
    try:
        tree = ast.parse(code)
        scanner = PurpleScanner(filename)
        scanner.visit(tree)
        return scanner.findings
    except SyntaxError as e:
        logger.error(f"Error de sintaxis en {filename}: {e}")
        return [
            {
                "message": "Error de sintaxis (Posible código cifrado o malformado)",
                "line": e.lineno or 0,
                "severity": "CRITICAL",
                "confidence": 1.0,
                "file": filename,
            }
        ]
    except Exception as e:
        logger.error(f"Error inesperado en PurpleScanner: {e}")
        return []


def scan_file(filepath: Path) -> list[dict[str, Any]]:
    """
    Escanea un archivo del sistema de archivos.

    Args:
        filepath: Ruta al archivo a escanear

    Returns:
        Lista de hallazgos detectados
    """
    extension = filepath.suffix.lower()

    if extension in TEXT_SCAN_EXTENSIONS or extension == ".py":
        try:
            code = _read_file_with_fallback(filepath)
            if extension in TEXT_SCAN_EXTENSIONS:
                return _scan_text_payloads(code, str(filepath))
            return run_scan(code, str(filepath))
        except OSError as e:
            logger.error(f"Error leyendo archivo {filepath}: {e}")
            return [
                {
                    "message": f"Error de lectura: {e}",
                    "line": 0,
                    "severity": "HIGH",
                    "confidence": 0.8,
                    "file": str(filepath),
                }
            ]

    # Extensión no soportada
    logger.debug(f"Extensión no soportada para escaneo: {extension}")
    return []
