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

    **Lo que este writer NO afirma** (y `trusted_namespace` sí):

    * No usa ``CreateFileW`` con SD canónico, ni verifica el contenido por
      handle Win32, ni comprueba el retorno de ``FlushFileBuffers``.
    * El flush es ``os.fsync`` sobre un file handle respaldado por el SO, con
      su retorno comprobado. Es "content flush solicitado y comprobado por el
      handle del SO", **no** "``FlushFileBuffers == TRUE`` verificado".
    * No hay re-verificación por handle ni re-lectura byte a byte *dentro* del
      writer: la re-lectura la hace el llamador (`publish_candidate_manifest`),
      que es quien sabe qué comparación hacer.

    Mecanismo de publicación **compartido** con el namespace: single-winner
    tipo ``CREATE_NEW``/``O_EXCL`` vía link duro. Garantía de flush
    **distinta y más débil** que la Win32 handle-bound.
    """

    def write_create_once(self, dest: pathlib.Path, payload: bytes, object_name: str) -> None:
        """Intenta la publicación create-once. Levanta ``FileExistsError`` si perdió.

        **El árbitro es la operación atómica, NO un ``dest.exists()`` previo.**
        Un fast-path ``if dest.exists(): raise`` es check-then-act: abre una
        ventana entre el check y el write en la que un adversario crea el
        destino. `os.link` no tiene esa ventana — falla con ``FileExistsError``
        en la syscall de publicación si el destino ya existía o apareció.

        El contrato hacia arriba es **no** traducir el error: el llamador tiene
        que poder reconciliar un replay idéntico con un conflicto de contenido
        (ver `publish_candidate_manifest`). Traducir `FileExistsError` a otro
        tipo acá volvería esa reconciliación imposible.
        """

        dest = pathlib.Path(dest)
        provisional = dest.with_name(f".staging-{uuid.uuid4().hex}.{dest.name}")
        try:
            with open(provisional, "xb") as fh:  # noqa: PTH123 — modo binario explícito
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            # Single-winner sin ventana check-then-write. No hay fallback a
            # reemplazo: un fallback convertiría create-once en overwrite.
            os.link(provisional, dest)
        except OSError:
            provisional.unlink(missing_ok=True)
            raise
        provisional.unlink(missing_ok=True)


__all__ = ["StagingCreateOnceWriter"]
