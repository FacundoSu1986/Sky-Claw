"""Módulo de creación y verificación de Runtime Clones independientes (RV-3).

Permite crear una copia física real, jugable e independiente de un Golden Master
verificado, garantizando:
- Autoridad de fuente: solo un GoldenMasterVerificationResult en estado VERIFIED
  o un GoldenMasterDescriptor inmutable con rol 'reference_only' autorizan la clonación.
- Fail-closed: ante destino preexistente (DestinationExistsError), rutas anidadas,
  árboles no inspeccionables o cualquier discrepancia criptográfica.
- Pre-copy y post-copy Golden Master re-verification.
- Creación de staging temporal hermano en el mismo volumen y publicación atómica
  vía os.replace solo tras superar la verificación completa de staging.
- Prohibición estricta de hardlinks, symlinks, junctions y reparse points.
- Independencia física absoluta: (dest.st_dev, dest.st_ino) != (src.st_dev, src.st_ino).
- Rollback determinista que elimina únicamente el directorio de staging propio.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import shutil
import stat
import uuid
from collections.abc import Sequence

from sky_claw.local.runtime_vault.golden import verify_critical_files
from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.models import (
    CriticalFileExpectation,
    DestinationExistsError,
    FileIdentity,
    GoldenMasterDescriptor,
    GoldenMasterVerificationResult,
    InventoryError,
    InventoryLinkError,
    RuntimeCloneDescriptor,
    RuntimeCloneError,
    RuntimeCloneResult,
    TreeVerificationResult,
    VerificationState,
)
from sky_claw.local.runtime_vault.verification import tree_digest_from_files


def verify_physical_independence(
    source_root: pathlib.Path,
    dest_root: pathlib.Path,
    files: Sequence[FileIdentity],
) -> bool:
    """Verifica que todos los archivos en dest_root sean físicamente independientes del origen.

    Comprueba que para cada archivo registrado en el inventario:
    (dest.st_dev, dest.st_ino) != (src.st_dev, src.st_ino).
    Si se detecta cualquier coincidencia de device e inode (hardlink), retorna False.
    """
    src_abs = pathlib.Path(os.path.abspath(os.fspath(source_root)))
    dest_abs = pathlib.Path(os.path.abspath(os.fspath(dest_root)))

    for f in files:
        src_path = src_abs / f.rel_path
        dest_path = dest_abs / f.rel_path

        try:
            src_stat = src_path.stat()
            dest_stat = dest_path.stat()
        except OSError:
            return False

        # En el mismo volumen NTFS/POSIX, dos archivos comparten st_dev.
        # Si además comparten st_ino, son la misma entrada física (hardlink).
        if src_stat.st_dev == dest_stat.st_dev and src_stat.st_ino == dest_stat.st_ino:
            return False

    return True


def _cleanup_staging(staging_path: pathlib.Path) -> None:
    """Elimina de forma segura y determinista el directorio de staging temporal.

    Recorrido postorden iterativo que no atraviesa symlinks ni junctions y maneja
    archivos con atributo read-only en Windows.
    """
    if not staging_path.exists():
        return

    def _eliminar_entrada(ruta: pathlib.Path) -> None:
        try:
            if ruta.is_dir() and not ruta.is_symlink():
                ruta.rmdir()
            else:
                ruta.unlink()
        except OSError:
            with contextlib.suppress(OSError):
                os.chmod(ruta, stat.S_IWRITE | stat.S_IREAD)
                if ruta.is_dir() and not ruta.is_symlink():
                    ruta.rmdir()
                else:
                    ruta.unlink()

    pendientes: list[tuple[pathlib.Path, bool]] = [(staging_path, False)]
    while pendientes:
        actual, procesado = pendientes.pop()
        if procesado:
            _eliminar_entrada(actual)
            continue

        try:
            if actual.is_symlink() or not actual.is_dir():
                _eliminar_entrada(actual)
                continue

            with os.scandir(actual) as entradas:
                hijos = [pathlib.Path(e.path) for e in entradas]
            pendientes.append((actual, True))
            pendientes.extend((h, False) for h in reversed(hijos))
        except OSError:
            _eliminar_entrada(actual)


def create_runtime_clone(
    golden_source: GoldenMasterVerificationResult | GoldenMasterDescriptor,
    destination: pathlib.Path | str,
    *,
    critical_expectations: Sequence[CriticalFileExpectation] = (),
) -> RuntimeCloneResult:
    """Crea y verifica una copia física independiente del Golden Master en destination.

    Garantiza fail-closed, staging atómico, validación previa y posterior del Golden Master,
    verificación criptográfica y auditoría de independencia física.
    """
    critical_expectations = tuple(critical_expectations)

    # 1. Validación de autoridad de fuente
    if isinstance(golden_source, GoldenMasterVerificationResult):
        if golden_source.state is not VerificationState.VERIFIED or golden_source.descriptor is None:
            raise RuntimeCloneError(
                f"Autoridad de fuente inválida: GoldenMasterVerificationResult no verificado "
                f"(estado: {golden_source.state.value})"
            )
        golden_desc = golden_source.descriptor
    elif isinstance(golden_source, GoldenMasterDescriptor):
        golden_desc = golden_source
    else:
        raise RuntimeCloneError(
            f"Tipo de fuente Golden Master no autorizado: {type(golden_source).__name__}. "
            f"Se requiere GoldenMasterVerificationResult VERIFIED o GoldenMasterDescriptor."
        )

    if golden_desc.role != "reference_only":
        raise RuntimeCloneError(f"Rol de Golden Master inválido: '{golden_desc.role}'. Se requiere 'reference_only'.")

    # 2. Validación de rutas y seguridad anti-traversal / anidamiento
    try:
        src_root = pathlib.Path(os.path.abspath(os.fspath(golden_desc.location)))
    except Exception as exc:
        raise RuntimeCloneError(f"Ruta de origen Golden Master inválida: {exc}") from exc

    try:
        dest_root = pathlib.Path(os.path.abspath(os.fspath(destination)))
    except Exception as exc:
        raise RuntimeCloneError(f"Ruta de destino inválida: {exc}") from exc

    if not src_root.exists():
        raise RuntimeCloneError(f"El directorio origen Golden Master no existe: '{src_root}'")
    if not src_root.is_dir():
        raise RuntimeCloneError(f"El origen Golden Master no es un directorio: '{src_root}'")

    # Validar igualdad o anidamientos cruzados
    if src_root == dest_root:
        raise RuntimeCloneError(f"Origen y destino son idénticos: '{src_root}'")

    if dest_root.is_relative_to(src_root):
        raise RuntimeCloneError(f"El destino '{dest_root}' está anidado dentro del origen '{src_root}'")

    if src_root.is_relative_to(dest_root):
        raise RuntimeCloneError(f"El origen '{src_root}' está anidado dentro del destino '{dest_root}'")

    # Fail-closed ante destino preexistente
    if dest_root.exists():
        raise DestinationExistsError(
            f"El destino '{dest_root}' ya existe. Fail-closed: no se sobrescribe ni se elimina."
        )

    # 3. Pre-copy Golden revalidation (detecta tampering o corrupción previa a la copia)
    try:
        src_files = inventory_tree(src_root)
    except (InventoryError, InventoryLinkError) as exc:
        raise RuntimeCloneError(f"Pre-copy revalidation falló al inventariar Golden Master: {exc}") from exc

    src_digest = tree_digest_from_files(src_files)
    if src_digest != golden_desc.tree_digest:
        raise RuntimeCloneError(
            f"Pre-copy revalidation falló para Golden Master en '{src_root}': "
            f"digest observado '{src_digest.digest}' != esperado '{golden_desc.tree_digest.digest}'"
        )

    src_crit = verify_critical_files(src_files, critical_expectations)
    if not all(c.state is VerificationState.VERIFIED for c in src_crit):
        raise RuntimeCloneError("Pre-copy revalidation falló para archivos críticos en Golden Master")

    # 4. Creación de directorio staging hermano en el mismo volumen
    dest_parent = dest_root.parent
    dest_parent.mkdir(parents=True, exist_ok=True)
    staging_name = f".{dest_root.name}.skyclaw-staging-{uuid.uuid4().hex[:12]}"
    staging_dir = dest_parent / staging_name

    try:
        staging_dir.mkdir(parents=True, exist_ok=False)
    except Exception as exc:
        raise RuntimeCloneError(f"No se pudo crear el directorio de staging '{staging_dir}': {exc}") from exc

    try:
        # 5. Copia física real (shutil.copy2)
        for f in src_files:
            rel = pathlib.Path(f.rel_path)
            src_file_path = src_root / rel
            dest_file_path = staging_dir / rel

            dest_file_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file_path, dest_file_path)

        # 6. Verificación de Staging
        staging_files = inventory_tree(staging_dir)
        staging_digest = tree_digest_from_files(staging_files)

        if staging_digest != golden_desc.tree_digest:
            raise RuntimeCloneError(
                f"Verificación de TreeDigest en staging falló: "
                f"observado '{staging_digest.digest}' != esperado '{golden_desc.tree_digest.digest}'"
            )

        staging_crit = verify_critical_files(staging_files, critical_expectations)
        if not all(c.state is VerificationState.VERIFIED for c in staging_crit):
            raise RuntimeCloneError("Verificación de archivos críticos en staging falló")

        staging_indep = verify_physical_independence(src_root, staging_dir, src_files)
        if not staging_indep:
            raise RuntimeCloneError(
                "Verificación de independencia física falló: se detectaron hardlinks entre origen y staging"
            )

        # 7. Atomic Publish vía os.replace
        if dest_root.exists():
            raise DestinationExistsError(
                f"Destino '{dest_root}' apareció concurrentemente antes del publish. Abortando."
            )

        os.replace(os.fspath(staging_dir), os.fspath(dest_root))

    except Exception as exc:
        _cleanup_staging(staging_dir)
        if isinstance(exc, (RuntimeCloneError, DestinationExistsError, InventoryError)):
            raise
        raise RuntimeCloneError(f"Fallo durante la clonación o verificación en staging: {exc}") from exc

    # 8. Post-publish Destination Verification
    try:
        dest_files = inventory_tree(dest_root)
        dest_digest = tree_digest_from_files(dest_files)

        if dest_digest != golden_desc.tree_digest:
            raise RuntimeCloneError("Verificación de TreeDigest post-publish en destino falló")

        dest_crit = verify_critical_files(dest_files, critical_expectations)
        if not all(c.state is VerificationState.VERIFIED for c in dest_crit):
            raise RuntimeCloneError("Verificación de archivos críticos post-publish en destino falló")

        dest_indep = verify_physical_independence(src_root, dest_root, src_files)
        if not dest_indep:
            raise RuntimeCloneError("Independencia física post-publish en destino falló")

        # 9. Post-copy Golden Master Re-verification
        post_golden_files = inventory_tree(src_root)
        post_golden_digest = tree_digest_from_files(post_golden_files)
        if post_golden_digest != golden_desc.tree_digest:
            raise RuntimeCloneError(
                "El Golden Master original fue modificado o degradado durante la operación de clonación"
            )

    except Exception as exc:
        _cleanup_staging(dest_root)
        if isinstance(exc, (RuntimeCloneError, DestinationExistsError, InventoryError)):
            raise
        raise RuntimeCloneError(f"Fallo en la verificación post-publish: {exc}") from exc

    # 10. Construcción del resultado final y descriptor inmutable
    clone_desc = RuntimeCloneDescriptor(
        location=dest_root,
        runtime_identity=golden_desc.runtime_identity,
        tree_digest=dest_digest,
        role="runtime_clone",
        source_golden=src_root,
    )

    tree_res = TreeVerificationResult(
        state=VerificationState.VERIFIED,
        message="",
        expected=golden_desc.tree_digest,
        observed=dest_digest,
    )

    return RuntimeCloneResult(
        state=VerificationState.VERIFIED,
        message="",
        source_golden=golden_desc,
        tree_result=tree_res,
        critical_results=dest_crit,
        physical_independence_verified=True,
        descriptor=clone_desc,
    )


__all__ = [
    "create_runtime_clone",
    "verify_physical_independence",
]
