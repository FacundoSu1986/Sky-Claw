"""
Gestor de Snapshots de archivos para rollback.

Este módulo proporciona funcionalidad de copy-on-write para crear
respaldos de archivos antes de modificaciones, permitiendo restauración.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import pathlib
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sky_claw.db.journal import JournalSnapshotError

logger = logging.getLogger(__name__)


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass(frozen=True, slots=True)
class SnapshotInfo:
    """Información sobre un snapshot de archivo."""

    snapshot_id: str
    original_path: str
    snapshot_path: str
    checksum: str
    size_bytes: int
    created_at: datetime
    metadata: dict[str, Any] | None = None


@dataclass
class SnapshotStats:
    """Estadísticas del gestor de snapshots."""

    total_snapshots: int
    total_size_bytes: int
    oldest_snapshot: datetime | None
    newest_snapshot: datetime | None
    snapshots_by_extension: dict[str, int]


@dataclass
class CleanupResult:
    """Resultado de limpieza de snapshots."""

    deleted_count: int
    freed_bytes: int
    errors: list[str] = field(default_factory=list)


# =============================================================================
# FILE SNAPSHOT MANAGER
# =============================================================================


class FileSnapshotManager:
    """
    Gestiona snapshots de archivos para rollback.

    Utiliza copy-on-write para crear respaldos eficientes de archivos
    antes de modificaciones. Los snapshots se almacenan en un directorio
    dedicado con estructura organizada por fecha.

    Attributes:
        snapshot_dir: Directorio base para snapshots.
        max_size_bytes: Tamaño máximo total de snapshots.

    Usage:
        manager = FileSnapshotManager(
            snapshot_dir=Path("/snapshots"),
            max_size_mb=1024
        )

        # Crear snapshot antes de modificar
        snapshot = await manager.create_snapshot(Path("/data/file.txt"))

        # Restaurar si es necesario
        await manager.restore_snapshot(snapshot.snapshot_path, Path("/data/file.txt"))
    """

    def __init__(
        self,
        snapshot_dir: pathlib.Path,
        max_size_mb: int = 1024,  # 1GB por defecto
    ) -> None:
        """
        Inicializa el gestor de snapshots.

        Args:
            snapshot_dir: Directorio donde almacenar los snapshots.
            max_size_mb: Tamaño máximo en MB para todos los snapshots.
        """
        self._snapshot_dir = snapshot_dir
        self._max_size_bytes = max_size_mb * 1024 * 1024
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Crea el directorio de snapshots si no existe."""
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Snapshot manager initialized",
            extra={
                "snapshot_dir": str(self._snapshot_dir),
                "max_size_mb": self._max_size_bytes // (1024 * 1024),
            },
        )

    # =========================================================================
    # SNAPSHOT CREATION
    # =========================================================================

    async def create_snapshot(
        self,
        file_path: pathlib.Path,
        metadata: dict[str, Any] | None = None,
    ) -> SnapshotInfo:
        """
        Crea una copia de respaldo del archivo.

        Args:
            file_path: Path al archivo original.
            metadata: Metadatos adicionales para el snapshot.

        Returns:
            SnapshotInfo con información del snapshot creado.

        Raises:
            JournalSnapshotError: Si el archivo no existe o falla la copia.
        """
        if not file_path.exists():
            raise JournalSnapshotError(f"Cannot create snapshot: file does not exist: {file_path}")

        if not file_path.is_file():
            raise JournalSnapshotError(f"Cannot create snapshot: path is not a file: {file_path}")

        async with self._lock:
            try:
                # Generar ID único para el snapshot
                timestamp = datetime.now(UTC)
                snapshot_id = self._generate_snapshot_id(file_path, timestamp)

                # Crear estructura de directorios por fecha
                date_dir = self._snapshot_dir / timestamp.strftime("%Y-%m-%d")
                date_dir.mkdir(parents=True, exist_ok=True)

                # Nombre del archivo snapshot
                snapshot_name = f"{snapshot_id}_{file_path.name}"
                snapshot_path = date_dir / snapshot_name

                # Calcular checksum antes de copiar
                checksum = await self._calculate_checksum(file_path)
                file_size = file_path.stat().st_size

                # Copiar archivo (copy-on-write cuando es posible)
                await asyncio.to_thread(shutil.copy2, file_path, snapshot_path)

                # SSP-001: Persistir checksum completo en sidecar .meta.json
                meta_path = snapshot_path.with_suffix(snapshot_path.suffix + ".meta.json")
                await asyncio.to_thread(
                    meta_path.write_text,
                    json.dumps({"checksum": checksum}, ensure_ascii=False),
                    "utf-8",
                )

                # Verificar tamaño límite
                await self._enforce_size_limit()

                snapshot_info = SnapshotInfo(
                    snapshot_id=snapshot_id,
                    original_path=str(file_path),
                    snapshot_path=str(snapshot_path),
                    checksum=checksum,
                    size_bytes=file_size,
                    created_at=timestamp,
                    metadata=metadata,
                )

                logger.info(
                    "Snapshot created",
                    extra={
                        "snapshot_id": snapshot_id,
                        "original_path": str(file_path),
                        "snapshot_path": str(snapshot_path),
                        "size_bytes": file_size,
                        "checksum": checksum[:16] + "...",  # Truncated for logs
                    },
                )

                return snapshot_info

            except OSError as e:
                raise JournalSnapshotError(f"Failed to create snapshot for {file_path}: {e}") from e

    async def create_snapshot_sync(
        self,
        file_path: pathlib.Path,
        metadata: dict[str, Any] | None = None,
    ) -> SnapshotInfo:
        """
        Versión síncrona de create_snapshot para uso en contextos no async.

        Args:
            file_path: Path al archivo original.
            metadata: Metadatos adicionales.

        Returns:
            SnapshotInfo con información del snapshot creado.
        """
        if not file_path.exists():
            raise JournalSnapshotError(f"Cannot create snapshot: file does not exist: {file_path}")

        try:
            timestamp = datetime.now(UTC)
            snapshot_id = self._generate_snapshot_id(file_path, timestamp)

            date_dir = self._snapshot_dir / timestamp.strftime("%Y-%m-%d")
            date_dir.mkdir(parents=True, exist_ok=True)

            snapshot_name = f"{snapshot_id}_{file_path.name}"
            snapshot_path = date_dir / snapshot_name

            checksum = self._calculate_checksum_sync(file_path)
            file_size = file_path.stat().st_size

            shutil.copy2(file_path, snapshot_path)

            # SSP-001: Persistir checksum completo en sidecar .meta.json
            meta_path = snapshot_path.with_suffix(snapshot_path.suffix + ".meta.json")
            meta_path.write_text(
                json.dumps({"checksum": checksum}, ensure_ascii=False),
                encoding="utf-8",
            )

            return SnapshotInfo(
                snapshot_id=snapshot_id,
                original_path=str(file_path),
                snapshot_path=str(snapshot_path),
                checksum=checksum,
                size_bytes=file_size,
                created_at=timestamp,
                metadata=metadata,
            )

        except OSError as e:
            raise JournalSnapshotError(f"Failed to create snapshot for {file_path}: {e}") from e

    # =========================================================================
    # SNAPSHOT RESTORATION
    # =========================================================================

    async def restore_snapshot(
        self,
        snapshot_path: str | pathlib.Path,
        target: pathlib.Path,
        verify_checksum: bool = True,
    ) -> bool:
        """
        Restaura un archivo desde su snapshot.

        Args:
            snapshot_path: Path al archivo de snapshot.
            target: Path destino donde restaurar.
            verify_checksum: Si True, verifica el checksum después de restaurar.

        Returns:
            True si la restauración fue exitosa.

        Raises:
            JournalSnapshotError: Si el snapshot no existe o la verificación falla.
        """
        snapshot_file = pathlib.Path(snapshot_path)

        if not snapshot_file.exists():
            raise JournalSnapshotError(f"Snapshot file does not exist: {snapshot_path}")

        async with self._lock:
            try:
                # Crear directorio padre si no existe
                target.parent.mkdir(parents=True, exist_ok=True)

                # Si el target existe, crear backup temporal
                backup_path: pathlib.Path | None = None
                if target.exists():
                    backup_path = target.with_suffix(target.suffix + ".restore_backup")
                    await asyncio.to_thread(shutil.copy2, target, backup_path)

                # Restaurar desde snapshot
                await asyncio.to_thread(shutil.copy2, snapshot_file, target)

                # Verificar checksum si se solicita
                if verify_checksum:
                    # SSP-001: Leer checksum desde sidecar .meta.json (fuente fiable)
                    expected_checksum = self._extract_checksum_from_meta(snapshot_file)
                    if expected_checksum:
                        actual_checksum = await self._calculate_checksum(target)
                        if actual_checksum != expected_checksum:
                            # Restaurar backup si falla verificación
                            if backup_path and backup_path.exists():
                                await asyncio.to_thread(shutil.copy2, backup_path, target)
                                await asyncio.to_thread(backup_path.unlink)
                            raise JournalSnapshotError(
                                f"Checksum verification failed for {target}. "
                                f"Expected: {expected_checksum[:16]}..., "
                                f"Got: {actual_checksum[:16]}..."
                            )

                # Eliminar backup temporal si existe
                if backup_path and backup_path.exists():
                    backup_path.unlink()

                logger.info(
                    "Snapshot restored",
                    extra={
                        "snapshot_path": str(snapshot_path),
                        "target_path": str(target),
                        "verified": verify_checksum,
                    },
                )

                return True

            except OSError as e:
                raise JournalSnapshotError(f"Failed to restore snapshot {snapshot_path} to {target}: {e}") from e

    # =========================================================================
    # SNAPSHOT CLEANUP
    # =========================================================================

    async def cleanup_old_snapshots(
        self,
        days_old: int = 30,
        dry_run: bool = False,
    ) -> CleanupResult:
        """
        Elimina snapshots antiguos para liberar espacio.

        Args:
            days_old: Edad mínima en días para eliminar.
            dry_run: Si True, solo simula sin eliminar.

        Returns:
            CleanupResult con detalles de la operación.
        """
        cutoff_time = time.time() - (days_old * 24 * 60 * 60)

        deleted_count = 0
        freed_bytes = 0
        errors: list[str] = []

        async with self._lock:
            try:
                for date_dir in self._snapshot_dir.iterdir():
                    if not date_dir.is_dir():
                        continue

                    # Verificar si el directorio es antiguo
                    try:
                        dir_time = time.strptime(date_dir.name, "%Y-%m-%d")
                        dir_timestamp = time.mktime(dir_time)

                        if dir_timestamp < cutoff_time:
                            # Eliminar todo el directorio
                            dir_size = sum(f.stat().st_size for f in date_dir.rglob("*") if f.is_file())
                            file_count = sum(1 for f in date_dir.rglob("*") if f.is_file())

                            if not dry_run:
                                await asyncio.to_thread(shutil.rmtree, date_dir)

                            deleted_count += file_count
                            freed_bytes += dir_size

                            logger.info(
                                "Cleaned up old snapshot directory",
                                extra={
                                    "directory": str(date_dir),
                                    "files_deleted": file_count,
                                    "bytes_freed": dir_size,
                                    "dry_run": dry_run,
                                },
                            )

                    except ValueError:
                        # Directorio con nombre inválido, ignorar
                        continue

            except OSError as e:
                errors.append(f"Error during cleanup: {e}")
                logger.error("Snapshot cleanup error", extra={"error": str(e)})

        return CleanupResult(deleted_count=deleted_count, freed_bytes=freed_bytes, errors=errors)

    async def cleanup_by_pattern(
        self,
        pattern: str,
        dry_run: bool = False,
    ) -> CleanupResult:
        """
        Elimina snapshots que coinciden con un patrón.

        Args:
            pattern: Patrón glob para buscar snapshots.
            dry_run: Si True, solo simula sin eliminar.

        Returns:
            CleanupResult con detalles de la operación.
        """
        deleted_count = 0
        freed_bytes = 0
        errors: list[str] = []

        async with self._lock:
            try:
                for snapshot_file in self._snapshot_dir.rglob(pattern):
                    if not snapshot_file.is_file():
                        continue

                    file_size = (await asyncio.to_thread(snapshot_file.stat)).st_size

                    if not dry_run:
                        await asyncio.to_thread(snapshot_file.unlink)

                    deleted_count += 1
                    freed_bytes += file_size

            except OSError as e:
                errors.append(f"Error cleaning pattern {pattern}: {e}")

        return CleanupResult(deleted_count=deleted_count, freed_bytes=freed_bytes, errors=errors)

    # =========================================================================
    # SIZE MANAGEMENT
    # =========================================================================

    async def _enforce_size_limit(self) -> None:
        """
        Elimina snapshots más antiguos si se excede el límite de tamaño.

        Este método se llama automáticamente después de crear un snapshot.
        """
        current_size = await self._calculate_total_size()

        if current_size <= self._max_size_bytes:
            return

        logger.warning(
            "Snapshot size limit exceeded, cleaning up old snapshots",
            extra={
                "current_size_mb": current_size // (1024 * 1024),
                "max_size_mb": self._max_size_bytes // (1024 * 1024),
            },
        )

        # Ordenar directorios por fecha (más antiguo primero)
        date_dirs: list[tuple[pathlib.Path, float]] = []
        for date_dir in self._snapshot_dir.iterdir():
            if date_dir.is_dir():
                try:
                    # SSP-003: Usar UTC consistentemente (no time.mktime que asume timezone local)
                    dir_dt = datetime.strptime(date_dir.name, "%Y-%m-%d").replace(tzinfo=UTC)
                    dir_timestamp = dir_dt.timestamp()
                    date_dirs.append((date_dir, dir_timestamp))
                except ValueError:
                    continue

        date_dirs.sort(key=lambda x: x[1])

        # Eliminar directorios más antiguos hasta estar bajo el límite
        for date_dir, _ in date_dirs:
            if current_size <= self._max_size_bytes * 0.9:  # 90% del límite
                break

            # SSP-002: Delegar cálculo de tamaño y eliminación a thread pool
            def _calc_dir_size_and_remove(d: pathlib.Path) -> int:
                sz = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                shutil.rmtree(d)
                return sz

            dir_size = await asyncio.to_thread(_calc_dir_size_and_remove, date_dir)
            current_size -= dir_size

            logger.info(
                "Removed old snapshot directory to enforce size limit",
                extra={
                    "directory": str(date_dir),
                    "freed_mb": dir_size // (1024 * 1024),
                },
            )

    async def _calculate_total_size(self) -> int:
        """Calcula el tamaño total de todos los snapshots."""

        # SSP-002: Delegar recorrido de filesystem a thread pool
        def _walk_size(directory: pathlib.Path) -> int:
            return sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())

        return await asyncio.to_thread(_walk_size, self._snapshot_dir)

    # =========================================================================
    # STATISTICS
    # =========================================================================

    async def get_stats(self) -> SnapshotStats:
        """
        Obtiene estadísticas del gestor de snapshots.

        Returns:
            SnapshotStats con información actual.
        """
        total_snapshots = 0
        total_size = 0
        oldest: datetime | None = None
        newest: datetime | None = None
        by_extension: dict[str, int] = {}

        # SSP-002: Delegar recorrido de filesystem a thread pool
        def _collect_stats(
            directory: pathlib.Path,
        ) -> tuple[int, int, datetime | None, datetime | None, dict[str, int]]:
            ts = 0
            count = 0
            old: datetime | None = None
            new: datetime | None = None
            ext_map: dict[str, int] = {}
            for fp in directory.rglob("*"):
                if not fp.is_file():
                    continue
                count += 1
                sz = fp.stat().st_size
                ts += sz
                try:
                    fd = datetime.strptime(fp.parent.name, "%Y-%m-%d")
                    if old is None or fd < old:
                        old = fd
                    if new is None or fd > new:
                        new = fd
                except ValueError:
                    pass
                ext = fp.suffix.lower()
                ext_map[ext] = ext_map.get(ext, 0) + 1
            return count, ts, old, new, ext_map

        total_snapshots, total_size, oldest, newest, by_extension = await asyncio.to_thread(
            _collect_stats, self._snapshot_dir
        )

        return SnapshotStats(
            total_snapshots=total_snapshots,
            total_size_bytes=total_size,
            oldest_snapshot=oldest,
            newest_snapshot=newest,
            snapshots_by_extension=by_extension,
        )

    # =========================================================================
    # UTILITY METHODS
    # =========================================================================

    def _generate_snapshot_id(self, file_path: pathlib.Path, timestamp: datetime) -> str:
        """Genera un ID único para el snapshot."""
        # Usar hash del path + timestamp para unicidad
        unique_string = f"{file_path}:{timestamp.isoformat()}:{time.time_ns()}"
        return hashlib.sha256(unique_string.encode()).hexdigest()[:16]

    async def _calculate_checksum(self, file_path: pathlib.Path) -> str:
        """
        Calcula el checksum SHA256 de un archivo.

        Args:
            file_path: Path al archivo.

        Returns:
            Checksum SHA256 en formato hexadecimal.
        """
        sha256_hash = hashlib.sha256()

        def _read_file() -> None:
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    sha256_hash.update(chunk)

        await asyncio.to_thread(_read_file)

        return sha256_hash.hexdigest()

    def _calculate_checksum_sync(self, file_path: pathlib.Path) -> str:
        """Versión síncrona del cálculo de checksum."""
        sha256_hash = hashlib.sha256()

        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256_hash.update(chunk)

        return sha256_hash.hexdigest()

    def _extract_checksum_from_path(self, snapshot_path: pathlib.Path) -> str | None:
        """
        Extrae el checksum del nombre del archivo snapshot.

        El formato esperado es: {checksum}_{original_name}
        """
        name = snapshot_path.name
        parts = name.split("_", 1)

        if len(parts) >= 1 and len(parts[0]) == 64:
            # Checksum SHA256 completo
            return parts[0]
        elif len(parts) >= 1 and len(parts[0]) == 16:
            # Checksum truncado (no se puede verificar)
            return None

        return None

    def _extract_checksum_from_meta(self, snapshot_path: pathlib.Path) -> str | None:
        """
        SSP-001: Lee el checksum SHA256 completo desde el sidecar .meta.json.

        Args:
            snapshot_path: Path al archivo de snapshot.

        Returns:
            Checksum SHA256 de 64 caracteres hex, o None si el meta no existe/malformado.
        """
        meta_path = snapshot_path.with_suffix(snapshot_path.suffix + ".meta.json")
        if not meta_path.exists():
            return None
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            checksum = data.get("checksum", "")
            if self.validate_checksum_format(checksum):
                return checksum
        except (OSError, ValueError, json.JSONDecodeError):
            logger.warning("Failed to read snapshot meta: %s", meta_path)
        return None

    def validate_checksum_format(self, checksum: str) -> bool:
        """
        Valida que un string sea un checksum SHA256 válido.

        Args:
            checksum: String a validar.

        Returns:
            True si es un SHA256 válido (64 caracteres hex).
        """
        if len(checksum) != 64:
            return False

        try:
            int(checksum, 16)
            return True
        except ValueError:
            return False

    # =========================================================================
    # CONTEXT MANAGER
    # =========================================================================

    async def __aenter__(self) -> FileSnapshotManager:
        """Context manager entry."""
        await self.initialize()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Context manager exit."""
        pass  # No cleanup needed
