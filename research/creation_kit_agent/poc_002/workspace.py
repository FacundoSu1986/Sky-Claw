"""
Gestor de Workspace con contención estricta e independiente del sistema operativo host.
Resuelve P0-1 (preservar job_id validado) y P0-5 (análisis cross-platform de Windows/POSIX paths).
"""
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Tuple

from exceptions import PathContainmentError, WorkspaceViolationError


def compute_sha256(file_path: Path) -> str:
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def resolve_under(root: Path, relative_str: str) -> Path:
    """
    Resuelve una ruta bajo 'root' garantizando contención estricta multiplataforma.
    Rechaza:
    - Cadenas vacías o no str.
    - Secuencias de traversal '..' en cualquier formato ('../', '..\\').
    - Rutas absolutas POSIX ('/etc/passwd') y Windows ('C:\\Windows').
    - Rutas con drive relativo ('C:foo.esp').
    - Rutas UNC ('\\\\server\\share' o '//server/share').
    - Escape efectivo tras canonicalización (resolve).
    """
    if not isinstance(relative_str, str) or not relative_str.strip():
        raise PathContainmentError("Ruta relativa vacía o inválida.")

    normalized_separators = relative_str.replace("\\", "/")
    parts = normalized_separators.split("/")
    if any(part == ".." for part in parts):
        raise PathContainmentError(f"Secuencia de traversal '..' detectada en: {relative_str}")

    win_path = PureWindowsPath(relative_str)
    posix_path = PurePosixPath(relative_str)

    if win_path.is_absolute() or posix_path.is_absolute():
        raise PathContainmentError(f"Ruta absoluta detectada: {relative_str}")

    if win_path.drive:
        raise PathContainmentError(f"Ruta con especificación de Windows drive detectada: {relative_str}")

    if relative_str.startswith(("//", "\\\\")):
        raise PathContainmentError(f"Ruta de red UNC detectada: {relative_str}")

    root_resolved = root.resolve()
    target_resolved = (root_resolved / Path(relative_str)).resolve()

    try:
        target_resolved.relative_to(root_resolved)
    except ValueError as e:
        raise PathContainmentError(f"Ruta escapa de la raíz autorizada {root_resolved}: {target_resolved}") from e

    return target_resolved


class JobWorkspace:
    def __init__(self, base_dir: Path, job_id: str):
        self.root = resolve_under(base_dir / "jobs", job_id)
        self.job_id = job_id

        self.originals_dir = self.root / "originals"
        self.candidates_dir = self.root / "candidates"
        self.temp_dir = self.root / "temp"
        self.reports_dir = self.root / "reports"
        self.receipts_dir = self.root / "receipts"
        self.logs_dir = self.root / "logs"

        self._setup_layout()

    def _setup_layout(self):
        for directory in [
            self.originals_dir,
            self.candidates_dir,
            self.temp_dir,
            self.reports_dir,
            self.receipts_dir,
            self.logs_dir,
        ]:
            directory.mkdir(parents=True, exist_ok=True)

    def provision_original(self, filename: str, content: bytes) -> Tuple[Path, str]:
        dest = resolve_under(self.originals_dir, filename)
        if dest.exists():
            raise WorkspaceViolationError(f"El original {filename} ya existe. Es inmutable.")
        dest.write_bytes(content)
        try:
            os.chmod(dest, 0o444)
        except OSError:
            pass
        sha = compute_sha256(dest)
        return dest, sha

    def prepare_candidate_from_original(self, filename: str) -> Tuple[Path, str]:
        orig = resolve_under(self.originals_dir, filename)
        if not orig.exists():
            raise FileNotFoundError(f"Original no encontrado: {orig}")

        cand = resolve_under(self.candidates_dir, filename)
        data = orig.read_bytes()
        cand.write_bytes(data)
        try:
            os.chmod(cand, 0o644)
        except OSError:
            pass
        return cand, compute_sha256(cand)

    def atomic_candidate_write(self, filename: str, data: bytes) -> Tuple[Path, str]:
        candidate_file = resolve_under(self.candidates_dir, filename)
        temp_file = resolve_under(self.temp_dir, f"{filename}.tmp_{os.getpid()}")

        with open(temp_file, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_file, candidate_file)
        final_sha = compute_sha256(candidate_file)
        return candidate_file, final_sha

    def get_candidate_path(self, filename: str) -> Path:
        return resolve_under(self.candidates_dir, filename)

    def get_original_path(self, filename: str) -> Path:
        return resolve_under(self.originals_dir, filename)
