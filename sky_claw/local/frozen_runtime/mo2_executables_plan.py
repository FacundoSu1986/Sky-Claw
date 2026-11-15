"""Taxonomía y planificación de migración para [customExecutables] de MO2 (P7-A5 PR 2).

Proporciona un modelo inmutable y verificable para clasificar cada entrada de
[customExecutables] en cinco estados cerrados y calcular de manera determinista
las mutaciones requeridas hacia el Runtime Clone.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
from enum import StrEnum
from typing import TYPE_CHECKING

from sky_claw.local.frozen_runtime.effective_runtime.mo2_ini import (
    _DIGITOS_HEX,
    _DIGITOS_OCT,
    _ESCAPES_DE_UNA_LETRA,
    decode_qt_byte_array,
)

if TYPE_CHECKING:
    from sky_claw.local.frozen_runtime.effective_runtime.mo2_ini import Mo2Executable

__all__ = [
    "ClasificacionEjecutableMO2",
    "EntradaPlanEjecutableMO2",
    "MutacionCampoEjecutable",
    "PlanMigracionEjecutablesMO2",
    "planificar_migracion_ejecutables",
]


class ClasificacionEjecutableMO2(StrEnum):
    """Clasificación exhaustiva y cerrada de cada entrada de [customExecutables]."""

    #: Binario dentro del runtime saliente cuyo binario homólogo existe en el Clone.
    MIGRABLE_AL_CLONE = "migrable_al_clone"
    #: Herramienta externa fuera del juego y de Steam, existente y preservable.
    EXTERNA_PRESERVABLE = "externa_preservable"
    #: Binario, directorio o argumentos que referencian o dependen de Steam.
    DEPENDIENTE_DE_STEAM = "dependiente_de_steam"
    #: El binario de origen o destino no existe en el sistema de archivos.
    OBSOLETA_O_INEXISTENTE = "obsoleta_o_inexistente"
    #: Entrada ambigua, malformada o con colisión/índice inválido.
    AMBIGUA_O_BLOQUEADA = "ambigua_o_bloqueada"


@dataclasses.dataclass(frozen=True, slots=True)
class MutacionCampoEjecutable:
    """Mutación puntual de un campo de una entrada en [customExecutables]."""

    index: int
    campo: str
    valor_previo: str
    valor_nuevo: str


@dataclasses.dataclass(frozen=True, slots=True)
class EntradaPlanEjecutableMO2:
    """Evaluación inmutable de una entrada individual de [customExecutables]."""

    index: int
    title: str
    clasificacion: ClasificacionEjecutableMO2
    binary_original: str
    binary_nuevo: str | None = None
    working_dir_original: str | None = None
    working_dir_nuevo: str | None = None
    arguments_original: str | None = None
    arguments_nuevo: str | None = None
    motivo: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class PlanMigracionEjecutablesMO2:
    """Plan integral inmutable de migración de [customExecutables]."""

    entradas: tuple[EntradaPlanEjecutableMO2, ...]
    mutaciones: tuple[MutacionCampoEjecutable, ...]
    bloqueado: bool
    motivos_bloqueo: tuple[str, ...]

    def serializar(self) -> list[dict[str, object]]:
        """Serializa las mutaciones para almacenamiento en el registro durable."""
        return [
            {
                "index": m.index,
                "campo": m.campo,
                "valor_previo": m.valor_previo,
                "valor_nuevo": m.valor_nuevo,
            }
            for m in self.mutaciones
        ]


def _es_igual_o_hijo(camino: pathlib.Path, base: pathlib.Path) -> bool:
    """Comprueba si camino es igual o descendiente estricto de base."""
    try:
        camino.relative_to(base)
        return True
    except ValueError:
        return False


def _dentro_de_area_steamapps(camino: pathlib.Path) -> bool:
    """Detecta si la ruta o alguno de sus ancestros se denomina 'steamapps'."""
    return any(comp.name.casefold() == "steamapps" for comp in (camino, *camino.parents))


def _desescapar_qt_argumentos(texto: str) -> tuple[str, bool]:
    """Desescapa una cadena ``arguments`` serializada por Qt ``QSettings::IniFormat``.

    Devuelve ``(texto_decodificado, escape_malformado)``. Si el texto emplea
    escapes Qt (``\\\\`` o ``\\"``), un escape ``\\x`` sin dígitos hexadecimales,
    una barra suelta al final o comillas desequilibradas se marcan como
    malformados para fallar cerrado.
    """
    salida: list[str] = []
    indice = 0
    largo = len(texto)
    vio_escape_qt = False
    malformado = False

    while indice < largo:
        caracter = texto[indice]
        if caracter != "\\":
            salida.append(caracter)
            indice += 1
            continue

        indice += 1
        if indice >= largo:
            if vio_escape_qt:
                malformado = True
            salida.append("\\")
            break

        siguiente = texto[indice]
        indice += 1
        if siguiente in _ESCAPES_DE_UNA_LETRA:
            if siguiente in ('"', "'", "?", "\\"):
                vio_escape_qt = True
            codigo = _ESCAPES_DE_UNA_LETRA[siguiente]
            if codigo == 0:
                malformado = True
            salida.append(chr(codigo))
            continue

        if siguiente == "x":
            if indice >= largo or texto[indice] not in _DIGITOS_HEX:
                if vio_escape_qt:
                    malformado = True
                salida.append("\\x")
                continue
            valor = 0
            while indice < largo and texto[indice] in _DIGITOS_HEX:
                valor = ((valor << 4) + int(texto[indice], 16)) & 0xFFFF
                indice += 1
            if valor == 0:
                malformado = True
            salida.append(chr(valor))
            continue

        if siguiente in _DIGITOS_OCT:
            valor = int(siguiente, 8)
            while indice < largo and texto[indice] in _DIGITOS_OCT:
                valor = ((valor << 3) + int(texto[indice], 8)) & 0xFFFF
                indice += 1
            if valor == 0:
                malformado = True
            salida.append(chr(valor))
            continue

        salida.append("\\")
        salida.append(siguiente)

    decodificado = "".join(salida)
    if "\x00" in decodificado:
        malformado = True
    if vio_escape_qt and decodificado.count('"') % 2 == 1:
        malformado = True
    return decodificado, malformado


def _analizar_variantes_argumentos(texto: str) -> tuple[tuple[str, ...], bool]:
    """Produce representaciones comparables de ``arguments`` y señala escapes inválidos."""
    if not texto:
        return (), False
    if "\x00" in texto:
        return (texto,), True

    recortado = texto.strip()
    if recortado.startswith("@ByteArray("):
        decodificado_bytes = decode_qt_byte_array(texto)
        if decodificado_bytes is None:
            return (texto,), True
        dec_ba = decodificado_bytes.decode("utf-8", "surrogateescape")
        if "\x00" in dec_ba:
            return (dec_ba,), True
        return (dec_ba,), False

    dec_qt, malformado = _desescapar_qt_argumentos(texto)
    variantes = (dec_qt, texto) if dec_qt != texto else (texto,)
    return variantes, malformado


def _tiene_escape_argumento_malformado(texto: str) -> bool:
    """Indica si ``arguments`` presenta un escape Qt ambiguo, truncado o con NUL."""
    _variantes, malformado = _analizar_variantes_argumentos(texto)
    return malformado


def _colapsar_barras_para_busqueda(texto: str, *, preservar_unc: bool) -> str:
    """Normaliza separadores repetidos a ``/`` manteniendo el prefijo UNC si aplica."""
    if preservar_unc and (texto.startswith("\\\\") or texto.startswith("//")):
        cuerpo = texto[2:]
        return "//" + re.sub(r"[\\/]+", "/", cuerpo)
    return re.sub(r"[\\/]+", "/", texto)


def _contiene_referencia_a_ruta(texto: str, ruta_base: pathlib.Path) -> bool:
    """Detecta si un texto contiene referencias a una ruta base o sus descendientes.

    Decodifica escapes de Qt ``QSettings::IniFormat`` (``\\\\``, ``\\"``,
    hex/octal y ``@ByteArray(...)``) y falla cerrado si detecta escapes
    truncados, ambiguos o NUL embebidos.
    """
    if not texto:
        return False
    fspath = os.fspath(ruta_base).rstrip("/\\")
    if not fspath:
        return False

    es_unc = fspath.startswith("\\\\") or fspath.startswith("//")
    fspath_norm = _colapsar_barras_para_busqueda(fspath, preservar_unc=es_unc)
    if not fspath_norm:
        return False

    variantes, malformado = _analizar_variantes_argumentos(texto)
    if malformado:
        return True

    patron = re.compile(
        r"(?:^|(?<=[\s\"'=:,;(]))" + re.escape(fspath_norm) + r"(?:[/\\\"' \t\r\n,;)]|$)",
        flags=re.IGNORECASE,
    )
    for variante in variantes:
        variante_norm = _colapsar_barras_para_busqueda(variante, preservar_unc=es_unc)
        if patron.search(variante_norm):
            return True
    return False


def _argumentos_dependientes_de_steam(argumentos: str, managed_source_root: pathlib.Path | None) -> bool:
    """Comprueba si los argumentos contienen referencias a Steam o Managed Source."""
    if not argumentos or not argumentos.strip():
        return False
    variantes, malformado = _analizar_variantes_argumentos(argumentos)
    if malformado:
        return True
    if any("steamapps" in variante.casefold() for variante in variantes):
        return True
    if managed_source_root is not None:
        return _contiene_referencia_a_ruta(argumentos, managed_source_root)
    return False


def _formatear_ruta_mo2(ruta: pathlib.Path | str) -> str:
    """Formatea la ruta usando barras directas '/' como convención canónica de MO2."""
    ruta_fspath = os.fspath(ruta)
    return ruta_fspath.replace("\\", "/")


def planificar_migracion_ejecutables(
    ejecutables: tuple[Mo2Executable, ...],
    outgoing_runtime_root: pathlib.Path,
    target_clone_root: pathlib.Path,
    managed_source_root: pathlib.Path | None = None,
) -> PlanMigracionEjecutablesMO2:
    """Construye un plan inmutable para la migración de ejecutables de MO2.

    Clasifica exhaustivamente cada ejecutable según su semántica real sin recurrir
    a reemplazos globales de subcadenas. Falla cerrado ante rutas inexistentes,
    ambiguas o dependientes de Steam.
    """
    norm_outgoing = pathlib.Path(os.path.normpath(os.path.abspath(outgoing_runtime_root)))
    norm_clone = pathlib.Path(os.path.normpath(os.path.abspath(target_clone_root)))
    norm_ms = (
        pathlib.Path(os.path.normpath(os.path.abspath(managed_source_root)))
        if managed_source_root is not None
        else None
    )

    entradas: list[EntradaPlanEjecutableMO2] = []
    mutaciones: list[MutacionCampoEjecutable] = []
    motivos_bloqueo: list[str] = []
    indices_vistos: set[int] = set()

    for exe in ejecutables:
        if getattr(exe, "anomalia", None):
            motivo = f"Ejecutable {exe.index} ({exe.title}) contiene anomalías en el INI: {exe.anomalia}"
            entradas.append(
                EntradaPlanEjecutableMO2(
                    index=exe.index,
                    title=exe.title,
                    clasificacion=ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA,
                    binary_original=exe.binary,
                    working_dir_original=exe.working_directory,
                    arguments_original=exe.arguments,
                    motivo=motivo,
                )
            )
            motivos_bloqueo.append(motivo)
            continue

        if exe.index < 1 or exe.index in indices_vistos:
            motivo = f"Índice de ejecutable inválido o duplicado: {exe.index}"
            entradas.append(
                EntradaPlanEjecutableMO2(
                    index=exe.index,
                    title=exe.title,
                    clasificacion=ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA,
                    binary_original=exe.binary,
                    motivo=motivo,
                )
            )
            motivos_bloqueo.append(motivo)
            continue
        indices_vistos.add(exe.index)

        bin_raw = exe.binary.strip().strip('"')
        if not bin_raw:
            motivo = f"Ejecutable {exe.index} ({exe.title}) carece de binario"
            entradas.append(
                EntradaPlanEjecutableMO2(
                    index=exe.index,
                    title=exe.title,
                    clasificacion=ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA,
                    binary_original=exe.binary,
                    motivo=motivo,
                )
            )
            motivos_bloqueo.append(motivo)
            continue

        # Detección de ambigüedad en rutas relativas o con saltos
        if ".." in bin_raw or bin_raw.startswith("."):
            motivo = f"Ejecutable {exe.index} ({exe.title}) usa ruta relativa o ambigua: '{bin_raw}'"
            entradas.append(
                EntradaPlanEjecutableMO2(
                    index=exe.index,
                    title=exe.title,
                    clasificacion=ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA,
                    binary_original=exe.binary,
                    motivo=motivo,
                )
            )
            motivos_bloqueo.append(motivo)
            continue

        bin_path = pathlib.Path(bin_raw)
        if not bin_path.is_absolute():
            # Si no es absoluta y no tiene separadores, podría ser un comando del PATH, pero en MO2 customExecutables es ambiguo
            motivo = f"Ejecutable {exe.index} ({exe.title}) no especifica ruta absoluta: '{bin_raw}'"
            entradas.append(
                EntradaPlanEjecutableMO2(
                    index=exe.index,
                    title=exe.title,
                    clasificacion=ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA,
                    binary_original=exe.binary,
                    motivo=motivo,
                )
            )
            motivos_bloqueo.append(motivo)
            continue

        norm_bin = pathlib.Path(os.path.normpath(os.path.abspath(bin_raw)))

        # 1. Comprobación de Steam (binario/workdir) y escapes malformados en arguments
        en_ms = norm_ms is not None and _es_igual_o_hijo(norm_bin, norm_ms)
        en_steamapps = _dentro_de_area_steamapps(norm_bin)
        wd_en_steam = False
        if exe.working_directory:
            wd_raw = exe.working_directory.strip().strip('"')
            if wd_raw:
                norm_wd = pathlib.Path(os.path.normpath(os.path.abspath(wd_raw)))
                wd_en_steam = (norm_ms is not None and _es_igual_o_hijo(norm_wd, norm_ms)) or _dentro_de_area_steamapps(
                    norm_wd
                )

        if (
            not (en_ms or en_steamapps or wd_en_steam)
            and exe.arguments
            and _tiene_escape_argumento_malformado(exe.arguments)
        ):
            motivo = (
                f"Ejecutable {exe.index} ({exe.title}) contiene escapes malformados o "
                f"ambiguos en arguments: '{exe.arguments}'"
            )
            entradas.append(
                EntradaPlanEjecutableMO2(
                    index=exe.index,
                    title=exe.title,
                    clasificacion=ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA,
                    binary_original=exe.binary,
                    working_dir_original=exe.working_directory,
                    arguments_original=exe.arguments,
                    motivo=motivo,
                )
            )
            motivos_bloqueo.append(motivo)
            continue

        args_en_steam = False
        if exe.arguments:
            args_en_steam = _argumentos_dependientes_de_steam(exe.arguments, norm_ms)

        if en_ms or en_steamapps or wd_en_steam or args_en_steam:
            detalles_steam: list[str] = []
            if en_ms or en_steamapps:
                detalles_steam.append(f"{exe.index}\\binary ({exe.title}): '{exe.binary}'")
            if wd_en_steam:
                detalles_steam.append(f"{exe.index}\\workingDirectory ({exe.title}): '{exe.working_directory}'")
            if args_en_steam:
                detalles_steam.append(f"{exe.index}\\arguments ({exe.title}): '{exe.arguments}'")
            motivo = "; ".join(detalles_steam)
            entradas.append(
                EntradaPlanEjecutableMO2(
                    index=exe.index,
                    title=exe.title,
                    clasificacion=ClasificacionEjecutableMO2.DEPENDIENTE_DE_STEAM,
                    binary_original=exe.binary,
                    working_dir_original=exe.working_directory,
                    arguments_original=exe.arguments,
                    motivo=motivo,
                )
            )
            motivos_bloqueo.append(
                f"entradas en [customExecutables] siguen dependiendo de rutas administradas por Steam: {motivo}"
            )
            continue

        # 2. Comprobación de pertenencia al runtime saliente
        if _es_igual_o_hijo(norm_bin, norm_outgoing):
            rel_bin = norm_bin.relative_to(norm_outgoing)
            target_bin = norm_clone / rel_bin

            # Comprobar existencia física en origen y en destino del clone
            if not norm_bin.exists():
                motivo = f"Binario origen de {exe.index} ({exe.title}) inexistente en disco: '{norm_bin}'"
                entradas.append(
                    EntradaPlanEjecutableMO2(
                        index=exe.index,
                        title=exe.title,
                        clasificacion=ClasificacionEjecutableMO2.OBSOLETA_O_INEXISTENTE,
                        binary_original=exe.binary,
                        working_dir_original=exe.working_directory,
                        arguments_original=exe.arguments,
                        motivo=motivo,
                    )
                )
                motivos_bloqueo.append(motivo)
                continue

            if not target_bin.exists():
                motivo = f"Binario destino homólogo de {exe.index} ({exe.title}) inexistente en Clone: '{target_bin}'"
                entradas.append(
                    EntradaPlanEjecutableMO2(
                        index=exe.index,
                        title=exe.title,
                        clasificacion=ClasificacionEjecutableMO2.OBSOLETA_O_INEXISTENTE,
                        binary_original=exe.binary,
                        working_dir_original=exe.working_directory,
                        arguments_original=exe.arguments,
                        motivo=motivo,
                    )
                )
                motivos_bloqueo.append(motivo)
                continue

            if exe.arguments and _contiene_referencia_a_ruta(exe.arguments, norm_outgoing):
                motivo = (
                    f"Ejecutable migrable {exe.index} ({exe.title}) contiene referencias residuales "
                    f"al runtime anterior en arguments: '{exe.arguments}'"
                )
                entradas.append(
                    EntradaPlanEjecutableMO2(
                        index=exe.index,
                        title=exe.title,
                        clasificacion=ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA,
                        binary_original=exe.binary,
                        working_dir_original=exe.working_directory,
                        arguments_original=exe.arguments,
                        motivo=motivo,
                    )
                )
                motivos_bloqueo.append(motivo)
                continue

            nuevo_wd_str = None
            wd_invalido = False
            if exe.working_directory:
                wd_raw = exe.working_directory.strip().strip('"')
                if wd_raw:
                    norm_wd = pathlib.Path(os.path.normpath(os.path.abspath(wd_raw)))
                    if _es_igual_o_hijo(norm_wd, norm_outgoing):
                        rel_wd = norm_wd.relative_to(norm_outgoing)
                        target_wd = norm_clone / rel_wd
                        if not target_wd.exists():
                            motivo = (
                                f"Directorio de trabajo destino homólogo de {exe.index} ({exe.title}) "
                                f"inexistente en Clone: '{target_wd}'"
                            )
                            entradas.append(
                                EntradaPlanEjecutableMO2(
                                    index=exe.index,
                                    title=exe.title,
                                    clasificacion=ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA,
                                    binary_original=exe.binary,
                                    working_dir_original=exe.working_directory,
                                    arguments_original=exe.arguments,
                                    motivo=motivo,
                                )
                            )
                            motivos_bloqueo.append(motivo)
                            wd_invalido = True
                        elif not target_wd.is_dir():
                            motivo = (
                                f"Destino homólogo de workingDirectory de {exe.index} ({exe.title}) "
                                f"en Clone no es un directorio: '{target_wd}'"
                            )
                            entradas.append(
                                EntradaPlanEjecutableMO2(
                                    index=exe.index,
                                    title=exe.title,
                                    clasificacion=ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA,
                                    binary_original=exe.binary,
                                    working_dir_original=exe.working_directory,
                                    arguments_original=exe.arguments,
                                    motivo=motivo,
                                )
                            )
                            motivos_bloqueo.append(motivo)
                            wd_invalido = True
                        else:
                            nuevo_wd_str = _formatear_ruta_mo2(target_wd)
                    else:
                        nuevo_wd_str = exe.working_directory

            if wd_invalido:
                continue

            # MIGRABLE_AL_CLONE
            nuevo_bin_str = _formatear_ruta_mo2(target_bin)
            mutaciones.append(
                MutacionCampoEjecutable(
                    index=exe.index,
                    campo="binary",
                    valor_previo=exe.binary,
                    valor_nuevo=nuevo_bin_str,
                )
            )

            if nuevo_wd_str is not None and nuevo_wd_str != exe.working_directory:
                mutaciones.append(
                    MutacionCampoEjecutable(
                        index=exe.index,
                        campo="workingDirectory",
                        valor_previo=exe.working_directory or "",
                        valor_nuevo=nuevo_wd_str,
                    )
                )

            entradas.append(
                EntradaPlanEjecutableMO2(
                    index=exe.index,
                    title=exe.title,
                    clasificacion=ClasificacionEjecutableMO2.MIGRABLE_AL_CLONE,
                    binary_original=exe.binary,
                    binary_nuevo=nuevo_bin_str,
                    working_dir_original=exe.working_directory,
                    working_dir_nuevo=nuevo_wd_str,
                    arguments_original=exe.arguments,
                    arguments_nuevo=exe.arguments,
                    motivo="Migrable hacia Runtime Clone con binario verificado",
                )
            )
            continue

        # 3. Externo al runtime y fuera de Steam (herramientas como LOOT, SSEEdit, etc.)
        if exe.working_directory:
            wd_raw = exe.working_directory.strip().strip('"')
            if wd_raw:
                norm_wd = pathlib.Path(os.path.normpath(os.path.abspath(wd_raw)))
                if _es_igual_o_hijo(norm_wd, norm_outgoing):
                    motivo = (
                        f"Herramienta externa {exe.index} ({exe.title}) tiene workingDirectory "
                        f"dentro del runtime saliente: '{exe.working_directory}'"
                    )
                    entradas.append(
                        EntradaPlanEjecutableMO2(
                            index=exe.index,
                            title=exe.title,
                            clasificacion=ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA,
                            binary_original=exe.binary,
                            working_dir_original=exe.working_directory,
                            arguments_original=exe.arguments,
                            motivo=motivo,
                        )
                    )
                    motivos_bloqueo.append(motivo)
                    continue

        if exe.arguments and _contiene_referencia_a_ruta(exe.arguments, norm_outgoing):
            motivo = (
                f"Herramienta externa {exe.index} ({exe.title}) contiene referencias residuales "
                f"al runtime anterior en arguments: '{exe.arguments}'"
            )
            entradas.append(
                EntradaPlanEjecutableMO2(
                    index=exe.index,
                    title=exe.title,
                    clasificacion=ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA,
                    binary_original=exe.binary,
                    working_dir_original=exe.working_directory,
                    arguments_original=exe.arguments,
                    motivo=motivo,
                )
            )
            motivos_bloqueo.append(motivo)
            continue

        entradas.append(
            EntradaPlanEjecutableMO2(
                index=exe.index,
                title=exe.title,
                clasificacion=ClasificacionEjecutableMO2.EXTERNA_PRESERVABLE,
                binary_original=exe.binary,
                working_dir_original=exe.working_directory,
                arguments_original=exe.arguments,
                motivo="Herramienta externa válida y preservable sin mutación",
            )
        )

    bloqueado = len(motivos_bloqueo) > 0
    return PlanMigracionEjecutablesMO2(
        entradas=tuple(entradas),
        mutaciones=tuple(mutaciones),
        bloqueado=bloqueado,
        motivos_bloqueo=tuple(motivos_bloqueo),
    )
