"""Writer create-once + flush verificado para UNTRUSTED_STAGING.

Vive aparte de `authorized_plan_store` para no ensuciar ese modulo con una
estrategia de escritura que NO es la del plan durable: el candidate manifest es
evidencia no autoritativa y el SD canonico del namespace protegido no le
corresponde.
"""

from __future__ import annotations

import os
import pathlib
import uuid


class StagingCreateOnceWriter:
    """Create-once con publicacion no-reemplazante y flush verificado.

    Semántica:

    1. Escribe en un hermano provisional y lo flushea.
    2. Publica con ``os.link`` —single-winner: falla con
       ``FileExistsError`` si el destino ya existe, sin ventana
       check-then-write— y borra el temporal. Es el MISMO criterio con el que
       el repo fija los bindings one-use (``CREATE_NEW`` / ``O_EXCL``) aplicado
       a un archivo.
    3. Verifica el flush antes de publicar.

    **Por qué no ``os.replace``:** el anchor
    ``test_os_replace_solo_vive_en_los_dos_modulos_autorizados`` congela
    ``os.replace`` a ``{clone.py, trusted_namespace.py}`` con la propiedad "el
    reemplazo de bytes protegidos tiene UN dueño". Publicar el manifest con
    ``os.replace`` agregaría un tercer dueño de esa operación, y ampliar un
    anchor de seguridad por conveniencia de un slice es exactamente el
    antipatrón que el repo prohíbe. ``os.link`` + ``unlink`` da la misma
    single-winner sin tocar esa frontera.

    **Por qué NO ``write_secured_file_create_once_at``:** esa primitiva es la
    correcta para el namespace protegido y comparte este mismo mecanismo
    (``CreateHardLinkW`` + ``DeleteFileW``), pero nace el temporal con el SD
    canónico. Aplicarlo a staging afirmaría una protección que el contrato no
    define para la zona UNTRUSTED y presupondría que el SID propietario existe
    — que es justo lo que todavía no está provisionado para el helper. Aquí se
    replica el mecanismo de publicación, no el SD.
    """

    def write_create_once(self, dest: pathlib.Path, payload: bytes, object_name: str) -> None:
        dest = pathlib.Path(dest)
        if dest.exists():
            raise FileExistsError(f"el destino ya existe (create-once, sin sustitución): {dest}")

        provisional = dest.with_name(f".staging-{uuid.uuid4().hex}.{dest.name}")
        try:
            with open(provisional, "xb") as fh:  # noqa: PTH123 — modo binario explícito
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            # Single-winner sin ventana check-then-write: `os.link` falla si
            # `dest` existe. No hay fallback a reemplazo — un fallback
            # convertiría la política create-once en overwrite.
            os.link(provisional, dest)
        except OSError:
            provisional.unlink(missing_ok=True)
            raise
        provisional.unlink(missing_ok=True)


__all__ = ["StagingCreateOnceWriter"]
