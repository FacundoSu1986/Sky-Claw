"""Guía al operador de la etapa 9 asistida (TexGen + DynDOLOD): checklist previo, paso en vivo y cortes en criollo.

**Por qué existe (#997).** R0 brokered 2 dio PASS pero necesitó ocho intentos: casi todos los cortes fueron
precondiciones que nadie le avisó al operador, o un orden de pasos que no tenía forma de saber, y el producto los
reportó DESPUÉS de fallar y en jerga (``ERROR_UIA``, ``PROCESO_TERMINO``, «outside all sandbox roots»). Este módulo es
la UNA fuente de:

* el **catálogo de precondiciones** (``CATALOGO``): cada una con su texto en criollo y su arreglo en una línea;
* los **chequeos** de solo lectura que la evalúan (``chequear_*``), sin red ni escrituras;
* las **fases** del modo asistido (``FaseAsistida``) con el texto que ve el operador en cada momento;
* la **explicación** en criollo de cada corte del protocolo de readiness (``explicar_fallo``).

**Quién lo consume.** ``DynDOLODPipelineService.verificar_checklist`` arma el checklist y ``execute`` lo hace cumplir
antes de tocar nada; la GUI (``GenerateLodsStrategy``) y el agente LLM (``GestorDeTrabajosDeLods``) lo consultan antes
de preguntar o lanzar. La GUI y el agente muestran el MISMO ``ChecklistDeEtapa9.to_dict()``.

**Política (decisión del propietario, FASE 1 de #997).** Un ❌ (``Severidad.BLOQUEA``) es solo para fallas SEGURAS o
de SEGURIDAD —la herramienta va a fallar igual, o es salir del sandbox— y por eso ninguna ficha admite override. Lo
incierto es ⚠️ (``Severidad.AVISO``) y nunca bloquea. ``Estado.NO_VERIFICABLE`` tampoco bloquea: cada ❌ refleja un
corte fail-closed que sigue existiendo aguas abajo (runner, ``PathValidator``, worker), así que no poder verificarlo
acá no abre nada; solo se pierde el aviso temprano.

Las anclas que enumeran todo esto: ``tests/test_dyndolod_guia_catalogo.py`` (igualdad literal) y
``tests/test_dyndolod_guia_superficies.py`` (toda superficie lo consulta).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import ntpath
import os
import pathlib
import string
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from sky_claw.local.mo2.vfs_attestation import VfsAttestationError, read_enabled_mods
from sky_claw.local.tools.dyndolod_presets import PresetError, valor_de_clave
from sky_claw.local.tools.dyndolod_uia_contrato import CONTRATOS_DE_UI, ContratoDeUIHerramienta
from sky_claw.local.tools.dyndolod_uia_gate import ResolucionProtocoloReadiness
from sky_claw.local.tools.dyndolod_uia_preflight import RazonPreflight

logger = logging.getLogger(__name__)


# =============================================================================
# Catálogo de precondiciones
# =============================================================================


class IdPrecondicion(StrEnum):
    """Las precondiciones del checklist. Congeladas en ``tests/test_dyndolod_guia_catalogo.py``."""

    DYNDOLOD_ENCONTRADO = "dyndolod_encontrado"
    TEXGEN_ENCONTRADO = "texgen_encontrado"
    HERRAMIENTAS_EN_CARPETA_PERMITIDA = "herramientas_en_carpeta_permitida"
    HERRAMIENTAS_FUERA_DEL_JUEGO = "herramientas_fuera_del_juego"
    SALIDA_VIEJA_DESACTIVADA = "salida_vieja_desactivada"
    ETAPAS_PREVIAS = "etapas_previas"
    OUTPUT_DEL_PRESET = "output_del_preset"
    DLL_NG_Y_MASTERS = "dll_ng_y_masters"


class Severidad(StrEnum):
    """❌ (frena la corrida) o ⚠️ (se avisa y se sigue)."""

    BLOQUEA = "bloquea"
    AVISO = "aviso"


_RANGO_DE_SEVERIDAD = {Severidad.AVISO: 0, Severidad.BLOQUEA: 1}


class Estado(StrEnum):
    """Qué dio el chequeo. ``NO_VERIFICABLE`` = no se pudo mirar (nunca bloquea)."""

    OK = "ok"
    FALLA = "falla"
    NO_VERIFICABLE = "no_verificable"


@dataclass(frozen=True, slots=True)
class FichaDePrecondicion:
    """El texto del operador para una precondición. ``{campos}`` se completan con los datos del chequeo."""

    id: IdPrecondicion
    titulo: str
    arreglo: str
    severidad_maxima: Severidad
    admite_override: bool = False


def _ficha(
    ficha_id: IdPrecondicion, titulo: str, arreglo: str, severidad: Severidad
) -> tuple[IdPrecondicion, FichaDePrecondicion]:
    return ficha_id, FichaDePrecondicion(id=ficha_id, titulo=titulo, arreglo=arreglo, severidad_maxima=severidad)


CATALOGO: Mapping[IdPrecondicion, FichaDePrecondicion] = MappingProxyType(
    dict(
        (
            _ficha(
                IdPrecondicion.DYNDOLOD_ENCONTRADO,
                "No encuentro DynDOLOD.",
                "Abrí Ajustes y elegí dónde está DynDOLODx64.exe.",
                Severidad.BLOQUEA,
            ),
            _ficha(
                IdPrecondicion.TEXGEN_ENCONTRADO,
                "No encuentro TexGen.",
                "Abrí Ajustes y elegí TexGenx64.exe (está en la misma carpeta que DynDOLOD).",
                Severidad.BLOQUEA,
            ),
            _ficha(
                IdPrecondicion.HERRAMIENTAS_EN_CARPETA_PERMITIDA,
                "DynDOLOD está en una carpeta que Sky-Claw no puede usar.",
                "Movelo (o instalalo) en una de estas carpetas: {raices}. Después tocá «Revisar de nuevo».",
                Severidad.BLOQUEA,
            ),
            _ficha(
                IdPrecondicion.HERRAMIENTAS_FUERA_DEL_JUEGO,
                "DynDOLOD o TexGen están adentro de la carpeta del juego, y ahí no arrancan.",
                "Mové la carpeta de DynDOLOD afuera del juego (por ejemplo a C:\\Modding\\DynDOLOD) y actualizá la ruta.",
                Severidad.BLOQUEA,
            ),
            _ficha(
                IdPrecondicion.SALIDA_VIEJA_DESACTIVADA,
                "Está activa la salida vieja de DynDOLOD ({plugins}).",
                "En MO2, destildá esos plugins (o el mod «DynDOLOD Output») y tocá «Revisar de nuevo».",
                Severidad.BLOQUEA,
            ),
            _ficha(
                IdPrecondicion.ETAPAS_PREVIAS,
                "Faltan pasos anteriores: {etapas}.",
                "Elegí «Asistido» (te va a pedir que confirmes) o completá esos pasos primero.",
                Severidad.AVISO,
            ),
            _ficha(
                IdPrecondicion.OUTPUT_DEL_PRESET,
                "{herramienta} va a abrir con otra carpeta de salida guardada.",
                "Cuando se abra {herramienta}, cambiá el campo Output a {esperado} antes de aprobar.",
                Severidad.AVISO,
            ),
            _ficha(
                IdPrecondicion.DLL_NG_Y_MASTERS,
                "Tenés DynDOLOD DLL NG: si los masters no están limpios, DynDOLOD frena a mitad de camino.",
                "Limpialos con «Limpiar Archivos» (QuickAutoClean) o desactivá DynDOLOD DLL NG en MO2.",
                Severidad.AVISO,
            ),
        )
    )
)


class _Datos(dict[str, str]):
    """Un campo que el chequeo no completó se ve como «…» en vez de explotar el cartel."""

    def __missing__(self, clave: str) -> str:
        return "…"


def _rellenar(plantilla: str, datos: Mapping[str, str]) -> str:
    return string.Formatter().vformat(plantilla, (), _Datos(datos))


@dataclass(frozen=True, slots=True)
class ResultadoDePrecondicion:
    """Lo que dio UN chequeo. ``severidad`` es la efectiva y nunca supera la de su ficha."""

    id: IdPrecondicion
    estado: Estado
    severidad: Severidad
    detalle_tecnico: str = ""
    datos: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        maxima = CATALOGO[self.id].severidad_maxima
        if _RANGO_DE_SEVERIDAD[self.severidad] > _RANGO_DE_SEVERIDAD[maxima]:
            raise ValueError(f"{self.id}: severidad {self.severidad} supera la de su ficha ({maxima})")

    @property
    def ficha(self) -> FichaDePrecondicion:
        return CATALOGO[self.id]

    @property
    def titulo(self) -> str:
        return _rellenar(self.ficha.titulo, dict(self.datos))

    @property
    def arreglo(self) -> str:
        return _rellenar(self.ficha.arreglo, dict(self.datos))

    @property
    def bloquea(self) -> bool:
        return self.estado is Estado.FALLA and self.severidad is Severidad.BLOQUEA

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id.value,
            "estado": self.estado.value,
            "ok": self.estado is Estado.OK,
            "severidad": self.severidad.value,
            "bloquea": self.bloquea,
            "titulo": self.titulo,
            "arreglo": self.arreglo,
            "detalle_tecnico": self.detalle_tecnico,
        }


def resultado(
    ficha_id: IdPrecondicion,
    estado: Estado,
    *,
    severidad: Severidad | None = None,
    detalle: str = "",
    **datos: str,
) -> ResultadoDePrecondicion:
    """Atajo: la severidad por defecto es la máxima de la ficha."""
    return ResultadoDePrecondicion(
        id=ficha_id,
        estado=estado,
        severidad=severidad if severidad is not None else CATALOGO[ficha_id].severidad_maxima,
        detalle_tecnico=detalle,
        datos=tuple(sorted(datos.items())),
    )


@dataclass(frozen=True, slots=True)
class ChecklistDeEtapa9:
    """El checklist completo. ``listo`` = ningún ❌; los ⚠️ viajan igual para que el operador los lea."""

    resultados: tuple[ResultadoDePrecondicion, ...] = field(default=())

    def __post_init__(self) -> None:
        ids = [r.id for r in self.resultados]
        if len(ids) != len(set(ids)):
            raise ValueError(f"precondición repetida en el checklist: {ids}")

    def bloqueantes(self) -> tuple[ResultadoDePrecondicion, ...]:
        return tuple(r for r in self.resultados if r.bloquea)

    def avisos(self) -> tuple[ResultadoDePrecondicion, ...]:
        return tuple(r for r in self.resultados if r.estado is Estado.FALLA and not r.bloquea)

    @property
    def listo(self) -> bool:
        return not self.bloqueantes()

    def mensaje_tecnico(self) -> str:
        """El ``message`` técnico del contrato de tools (las fallas con su evidencia)."""
        partes = [f"{r.id.value}: {r.detalle_tecnico or r.titulo}" for r in (*self.bloqueantes(), *self.avisos())]
        return "checklist de la etapa 9: " + ("; ".join(partes) if partes else "sin fallas")

    def guia(self) -> dict[str, str]:
        """Criollo primero: el primer ❌ (o ⚠️) con su arreglo; la evidencia va al desplegable."""
        primero = next(iter(self.bloqueantes() or self.avisos()), None)
        if primero is None:
            return {"titulo": "Todo listo para arrancar.", "arreglo": "", "detalle_tecnico": self.mensaje_tecnico()}
        resto = len(self.bloqueantes()) - 1 if primero.bloquea else 0
        sufijo = f" (y {resto} cosa{'s' if resto > 1 else ''} más en la lista)" if resto > 0 else ""
        return {
            "titulo": primero.titulo + sufijo,
            "arreglo": primero.arreglo,
            "detalle_tecnico": self.mensaje_tecnico(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"listo": self.listo, "items": [r.to_dict() for r in self.resultados]}


async def checklist_temprano(servicio: object, *, run_texgen: bool) -> ChecklistDeEtapa9 | None:
    """La consulta TEMPRANA de un lanzador (GUI o agente), antes de preguntar o lanzar: es UX, no la autoridad.

    La autoridad es ``DynDOLODPipelineService.execute``, que corre el checklist antes de cualquier efecto. Por eso,
    si el servicio no ofrece el checklist (otra implementación del puerto) o la consulta falla, esto devuelve ``None``
    y el lanzador sigue como antes: no se inventa un bloqueo ni un OK. La cancelación se propaga.
    """
    verificar = getattr(servicio, "verificar_checklist", None)
    if not callable(verificar):
        return None
    try:
        obtenido = verificar(run_texgen=run_texgen)
        if inspect.isawaitable(obtenido):
            obtenido = await obtenido
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 -- consulta temprana best-effort: la autoridad sigue siendo execute()
        logger.warning("no se pudo consultar el checklist de la etapa 9 antes de lanzar", exc_info=True)
        return None
    return obtenido if isinstance(obtenido, ChecklistDeEtapa9) else None


def texto_de_corte_temprano(checklist: ChecklistDeEtapa9) -> str:
    """Una línea para el cartel cuando un lanzador corta antes de preguntar: criollo primero, técnico al final."""
    explicada = checklist.guia()
    return f"{explicada['titulo']} {explicada['arreglo']} (detalle técnico: {explicada['detalle_tecnico']})"


# =============================================================================
# Chequeos (solo lectura)
# =============================================================================

#: Plugins que DynDOLOD genera. Activos en el ``plugins.txt`` antes de regenerar ⇒ el worker brokered rechaza el job
#: (abierto de #993) y DynDOLOD recomienda desactivarlos siempre. ``DynDOLOD.esm`` es de DynDOLOD 3 (NO_VERIFICADO en
#: rig; incluirlo solo puede sumar un aviso, nunca esconder uno).
PLUGINS_DE_SALIDA_DE_DYNDOLOD: tuple[str, ...] = ("DynDOLOD.esm", "DynDOLOD.esp", "Occlusion.esp")

#: Dónde instala DynDOLOD DLL NG su DLL, relativo a una capa de Data.
RUTA_DEL_DLL_NG: tuple[str, ...] = ("SKSE", "Plugins", "DynDOLOD.dll")


def _es_ruta(valor: object) -> bool:
    """Un doble (``MagicMock``) no es una ruta: el chequeo es «no verificable», nunca falla ni OK."""
    return isinstance(valor, pathlib.PurePath)


def _clave_windows(ruta: pathlib.PurePath | str) -> str:
    """Forma comparable de una ruta Windows: separadores, ``..`` y mayúsculas normalizados (``ntpath``).

    ``ntpath`` y no ``os.path``: estas rutas son Windows aunque los tests corran en POSIX, y ``os.path`` solo es
    ``ntpath`` en Windows (el mismo criterio que el post-check de la salida de DynDOLOD).
    """
    return ntpath.normcase(ntpath.normpath(str(ruta))).rstrip("\\")


def _esta_adentro(hija: pathlib.PurePath, padre: pathlib.PurePath) -> bool:
    clave_hija = _clave_windows(hija)
    clave_padre = _clave_windows(padre)
    return clave_hija == clave_padre or clave_hija.startswith(clave_padre + "\\")


def chequear_ejecutable(
    ficha_id: IdPrecondicion, *, nombre: str, crudo: object, validado: object
) -> ResultadoDePrecondicion:
    """¿Está configurado y existe? ``crudo`` es el valor tal cual; ``validado`` el que aceptó el sandbox."""
    if crudo is None and validado is None:
        return resultado(ficha_id, Estado.FALLA, detalle=f"{nombre} no está configurado")
    ruta = validado if validado is not None else crudo
    if not isinstance(ruta, pathlib.PurePath):
        return resultado(ficha_id, Estado.NO_VERIFICABLE, detalle=f"{nombre}: el resolver no devolvió una ruta")
    try:
        existe = pathlib.Path(ruta).is_file()
    except OSError as exc:
        return resultado(ficha_id, Estado.NO_VERIFICABLE, detalle=f"{nombre}: no se pudo mirar {ruta}: {exc}")
    if not existe:
        return resultado(ficha_id, Estado.FALLA, detalle=f"{nombre}: no existe el archivo {ruta}")
    return resultado(ficha_id, Estado.OK, detalle=str(ruta))


def chequear_carpeta_permitida(
    *, ejecutables: Sequence[tuple[str, object, object]], raices: Sequence[pathlib.PurePath]
) -> ResultadoDePrecondicion:
    """#4: un ejecutable configurado que el ``PathValidator`` rechazó (está fuera de todas las raíces del sandbox)."""
    ficha_id = IdPrecondicion.HERRAMIENTAS_EN_CARPETA_PERMITIDA
    configurados = [(nombre, crudo, validado) for nombre, crudo, validado in ejecutables if crudo is not None]
    if not configurados:
        return resultado(ficha_id, Estado.NO_VERIFICABLE, detalle="no hay ejecutables configurados")
    if any(not _es_ruta(crudo) for _nombre, crudo, _validado in configurados):
        return resultado(ficha_id, Estado.NO_VERIFICABLE, detalle="el resolver no devolvió rutas")
    rechazados = [f"{nombre}={crudo}" for nombre, crudo, validado in configurados if validado is None]
    if not rechazados:
        return resultado(ficha_id, Estado.OK)
    lista = "; ".join(str(raiz) for raiz in raices) or "(sin raíces configuradas)"
    return resultado(
        ficha_id,
        Estado.FALLA,
        detalle=f"fuera de todas las raíces del sandbox: {', '.join(rechazados)}; raíces permitidas: {lista}",
        raices=lista,
    )


def chequear_fuera_del_juego(*, ejecutables: Sequence[tuple[str, object]], juego: object) -> ResultadoDePrecondicion:
    """#3: TexGen/DynDOLOD rechazan correr instalados adentro de la carpeta del juego."""
    ficha_id = IdPrecondicion.HERRAMIENTAS_FUERA_DEL_JUEGO
    if not isinstance(juego, pathlib.PurePath):
        return resultado(ficha_id, Estado.NO_VERIFICABLE, detalle="la carpeta del juego no está configurada")
    rutas = [(nombre, ruta) for nombre, ruta in ejecutables if isinstance(ruta, pathlib.PurePath)]
    if not rutas:
        return resultado(ficha_id, Estado.NO_VERIFICABLE, detalle="no hay ejecutables para comparar")
    adentro = [f"{nombre}={ruta}" for nombre, ruta in rutas if _esta_adentro(ruta, juego)]
    if not adentro:
        return resultado(ficha_id, Estado.OK)
    return resultado(ficha_id, Estado.FALLA, detalle=f"adentro de {juego}: {', '.join(adentro)}")


def chequear_salida_vieja(*, plugins_txt: object, brokered: bool) -> ResultadoDePrecondicion:
    """Salida previa de DynDOLOD activa: ❌ bajo brokered (el worker rechaza el job), ⚠️ en standalone."""
    ficha_id = IdPrecondicion.SALIDA_VIEJA_DESACTIVADA
    if not isinstance(plugins_txt, pathlib.PurePath):
        return resultado(ficha_id, Estado.NO_VERIFICABLE, detalle="no hay plugins.txt del perfil activo")
    try:
        lineas = pathlib.Path(plugins_txt).read_text(encoding="utf-8-sig", errors="strict").splitlines()
    except (OSError, UnicodeError) as exc:
        return resultado(ficha_id, Estado.NO_VERIFICABLE, detalle=f"no se pudo leer {plugins_txt}: {exc}")
    buscados = {nombre.lower(): nombre for nombre in PLUGINS_DE_SALIDA_DE_DYNDOLOD}
    activos = [linea[1:].strip() for linea in lineas if linea.startswith("*")]
    encontrados = [plugin for plugin in activos if plugin.lower() in buscados]
    if not encontrados:
        return resultado(ficha_id, Estado.OK)
    return resultado(
        ficha_id,
        Estado.FALLA,
        severidad=Severidad.BLOQUEA if brokered else Severidad.AVISO,
        detalle=f"activos en {plugins_txt}: {', '.join(encontrados)}",
        plugins=" / ".join(encontrados),
    )


def chequear_etapas_previas(faltantes: Sequence[str] | None) -> ResultadoDePrecondicion:
    """#9: solo se MUESTRA. La hacen cumplir los guards de etapas previas existentes (D-3 y el del desatendido)."""
    ficha_id = IdPrecondicion.ETAPAS_PREVIAS
    if faltantes is None:
        return resultado(ficha_id, Estado.NO_VERIFICABLE, detalle="no se pudieron verificar las etapas 6–8")
    if not faltantes:
        return resultado(ficha_id, Estado.OK)
    return resultado(ficha_id, Estado.FALLA, detalle="; ".join(faltantes), etapas="; ".join(faltantes))


@dataclass(frozen=True, slots=True)
class LecturaDeOutputDePreset:
    """El ``OutputPath`` que la herramienta tiene guardado en su preset y el ``-o:`` que Sky-Claw le va a pasar."""

    herramienta: str
    guardado: str | None
    esperado: str


def leer_output_de_preset(
    tool: str, exe: pathlib.Path, *, esperado: pathlib.PurePath
) -> LecturaDeOutputDePreset | None:
    """Lee el ``OutputPath`` del preset del build MEDIDO; ``None`` si el build no tiene contrato o no se puede leer.

    La ruta del preset y su sección salen del contrato de UI del par ``(tool, sha256)`` (``CONTRATOS_DE_UI``): un
    build sin contrato no tiene ruta demostrada y no se adivina. Bloqueante (hash + lectura): llamar desde un hilo.
    """
    try:
        sha = _sha256_de_archivo(exe)
    except OSError:
        return None
    contrato: ContratoDeUIHerramienta | None = CONTRATOS_DE_UI.get((tool, sha))
    if contrato is None:
        return None
    ruta = exe.parent.joinpath(*contrato.preset_relativo)
    try:
        with ruta.open("rb") as archivo:
            contenido = archivo.read(4 * 1024 * 1024)
        guardado = valor_de_clave(contenido, seccion=contrato.seccion_preset, clave="OutputPath")
    except (OSError, PresetError):
        return None
    return LecturaDeOutputDePreset(herramienta=tool, guardado=guardado, esperado=str(esperado))


def _sha256_de_archivo(ruta: pathlib.Path) -> str:
    resumen = hashlib.sha256()
    with ruta.open("rb") as archivo:
        for bloque in iter(lambda: archivo.read(1024 * 1024), b""):
            resumen.update(bloque)
    return resumen.hexdigest()


def chequear_output_de_presets(lecturas: Sequence[LecturaDeOutputDePreset]) -> ResultadoDePrecondicion:
    """#5: el ``OutputPath`` guardado no es el ``-o:``. ⚠️: en asistido lo corrige el operador y el gate UIA lo exige."""
    ficha_id = IdPrecondicion.OUTPUT_DEL_PRESET
    if not lecturas:
        return resultado(ficha_id, Estado.NO_VERIFICABLE, detalle="ningún preset medible (build sin contrato)")
    distintas = [
        lectura
        for lectura in lecturas
        if lectura.guardado is not None and _clave_windows(lectura.guardado) != _clave_windows(lectura.esperado)
    ]
    if not distintas:
        return resultado(ficha_id, Estado.OK)
    primera = distintas[0]
    return resultado(
        ficha_id,
        Estado.FALLA,
        detalle="; ".join(f"{d.herramienta}: preset={d.guardado!r} -o:={d.esperado!r}" for d in distintas),
        herramienta=primera.herramienta,
        esperado=primera.esperado,
    )


def carpetas_donde_buscar_el_dll_ng(
    *,
    data_del_juego: pathlib.Path | None,
    overwrite: pathlib.Path | None,
    mods_dir: pathlib.Path | None,
    modlist_txt: pathlib.Path | None,
) -> tuple[pathlib.Path, ...] | None:
    """Las capas de Data que ve la corrida: el ``Data`` del juego, ``overwrite`` y los mods HABILITADOS.

    Enumera capas: no resuelve identidad de mods (censo de ``read_enabled_mods``, clase «enumera_capas»). ``None`` si
    no hay ninguna capa que mirar o el ``modlist.txt`` es ilegible: «no lo pude leer» nunca es «no está».
    """
    capas = [ruta for ruta in (data_del_juego, overwrite) if isinstance(ruta, pathlib.Path)]
    if isinstance(mods_dir, pathlib.Path) and isinstance(modlist_txt, pathlib.Path):
        try:
            habilitados_en_orden = read_enabled_mods(modlist_txt)
        except (OSError, VfsAttestationError):
            return None
        capas.extend(mods_dir / nombre for nombre in habilitados_en_orden)
    return tuple(capas) or None


def _contiene_sin_mayusculas(carpeta: pathlib.Path, partes: Sequence[str]) -> pathlib.Path | None:
    actual = carpeta
    for parte in partes:
        try:
            with os.scandir(actual) as entradas:
                siguiente = next((e.name for e in entradas if e.name.lower() == parte.lower()), None)
        except OSError:
            return None
        if siguiente is None:
            return None
        actual = actual / siguiente
    return actual if actual.is_file() else None


def chequear_dll_ng(*, carpetas: Sequence[pathlib.Path] | None) -> ResultadoDePrecondicion:
    """#10: con DLL NG, unos masters sin limpiar frenan a DynDOLOD («Deleted large references found»).

    Que los masters estén sucios no se puede probar desde acá (no hay registro de que QuickAutoClean haya corrido),
    así que es ⚠️ cuando el DLL está presente.
    """
    ficha_id = IdPrecondicion.DLL_NG_Y_MASTERS
    if not carpetas:
        return resultado(ficha_id, Estado.NO_VERIFICABLE, detalle="no hay capas de Data para mirar")
    for carpeta in carpetas:
        encontrado = _contiene_sin_mayusculas(carpeta, RUTA_DEL_DLL_NG)
        if encontrado is not None:
            return resultado(ficha_id, Estado.FALLA, detalle=f"DynDOLOD DLL NG presente: {encontrado}")
    return resultado(ficha_id, Estado.OK)


# =============================================================================
# Fases del modo asistido (indicador de paso en vivo)
# =============================================================================


class FaseAsistida(StrEnum):
    """Qué espera Sky-Claw en cada momento del protocolo de readiness asistido."""

    ABRIENDO_VENTANA = "abriendo_ventana"
    ESPERANDO_TU_APROBACION = "esperando_tu_aprobacion"
    VERIFICANDO_OUTPUT = "verificando_output"
    PODES_APRETAR_START = "podes_apretar_start"
    HERRAMIENTA_TERMINO = "herramienta_termino"
    CORTADA = "cortada"


@dataclass(frozen=True, slots=True)
class BotonesDeHerramienta:
    """Los botones REALES de cada herramienta, tal como los midió el rig (contrato de UI)."""

    previo: str | None
    inicio: str
    salida: str


def _botones_del_contrato() -> Mapping[str, BotonesDeHerramienta]:
    """Una sola fuente: los nombres salen de ``CONTRATOS_DE_UI``, no de un literal de la guía."""
    botones: dict[str, BotonesDeHerramienta] = {}
    for contrato in CONTRATOS_DE_UI.values():
        botones.setdefault(
            contrato.tool,
            BotonesDeHerramienta(
                previo=contrato.accion_previa.selector.nombre if contrato.accion_previa is not None else None,
                inicio=contrato.accion_inicio.selector.nombre or "Start",
                salida=contrato.accion_terminal.selector.nombre or "Exit",
            ),
        )
    return MappingProxyType(botones)


BOTONES_POR_HERRAMIENTA: Mapping[str, BotonesDeHerramienta] = _botones_del_contrato()
_BOTONES_GENERICOS = BotonesDeHerramienta(previo=None, inicio="Start", salida="Exit")

TEXTOS_DE_FASE: Mapping[FaseAsistida, str] = MappingProxyType(
    {
        FaseAsistida.ABRIENDO_VENTANA: (
            "Paso 1 de 4 · Se está abriendo {herramienta}: esperá a que aparezca la ventana.{aviso_stitched}"
        ),
        FaseAsistida.ESPERANDO_TU_APROBACION: (
            "Paso 2 de 4 · En {herramienta}: {paso_previo}poné en Output exactamente {esperado}, elegí el preset y "
            "después tocá «Aprobar» en Sky-Claw. Todavía NO toques «{boton_inicio}»."
        ),
        FaseAsistida.VERIFICANDO_OUTPUT: (
            "Paso 3 de 4 · Sky-Claw está revisando la carpeta de salida de {herramienta}. "
            "Todavía NO toques «{boton_inicio}»."
        ),
        FaseAsistida.PODES_APRETAR_START: (
            "Paso 4 de 4 · ¡Listo! Ya podés apretar «{boton_inicio}» en {herramienta}. Cuando termine de generar, "
            "cerrala con «{boton_salida}»; antes de eso, no la cierres."
        ),
        FaseAsistida.HERRAMIENTA_TERMINO: (
            "{herramienta} terminó. Sky-Claw está revisando y guardando lo que generó: esperá."
        ),
        FaseAsistida.CORTADA: "Se cortó {herramienta}. Mirá el aviso para saber qué hacer.",
    }
)

#: Fases en las que tocar el botón de arranque o cerrar la herramienta corta la corrida (#6).
FASES_DE_NO_TOCAR = frozenset({FaseAsistida.ESPERANDO_TU_APROBACION, FaseAsistida.VERIFICANDO_OUTPUT})


Corte = ResolucionProtocoloReadiness | RazonPreflight


@dataclass(frozen=True, slots=True)
class CambioDeFase:
    """Aviso del runner: entró a una fase (o se cortó, con el código TIPADO del corte)."""

    tool_name: str
    fase: FaseAsistida
    esperado: str | None = None
    corte: Corte | None = None

    def texto(self) -> str:
        botones = BOTONES_POR_HERRAMIENTA.get(self.tool_name, _BOTONES_GENERICOS)
        datos = {
            "herramienta": self.tool_name,
            "esperado": self.esperado or "la ruta que te muestra Sky-Claw",
            "boton_inicio": botones.inicio,
            "boton_salida": botones.salida,
            "paso_previo": f"tocá «{botones.previo}», " if botones.previo else "",
            "aviso_stitched": (
                " Si sale un cartel que habla de «stitched», apretá «Ignore»." if self.tool_name == "TexGen" else ""
            ),
        }
        return _rellenar(TEXTOS_DE_FASE[self.fase], datos)


#: Observador de fases del runner: best-effort, jamás decide el resultado de la corrida.
AvisoDeFase = Callable[[CambioDeFase], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class FaseEnCurso:
    """La fase vigente de UNA corrida y desde cuándo (``time.time()``)."""

    cambio: CambioDeFase
    desde: float = field(default_factory=time.time)

    def hechos(self, *, ahora: float) -> dict[str, Any]:
        return {
            "herramienta": self.cambio.tool_name,
            "fase": self.cambio.fase.value,
            "texto": self.cambio.texto(),
            "hace_segundos": max(0, int(ahora - self.desde)),
        }


# =============================================================================
# Explicaciones de los cortes (criollo primero; el técnico va al desplegable)
# =============================================================================


@dataclass(frozen=True, slots=True)
class Explicacion:
    titulo: str
    arreglo: str


def _explicacion(titulo: str, arreglo: str) -> Explicacion:
    return Explicacion(titulo=titulo, arreglo=arreglo)


EXPLICACION_POR_RESOLUCION: Mapping[ResolucionProtocoloReadiness, Explicacion] = MappingProxyType(
    {
        ResolucionProtocoloReadiness.DENEGADA: _explicacion(
            "Dijiste que no en la ventana de aprobación de Sky-Claw.",
            "Si fue sin querer, volvé a correr y tocá «Aprobar» cuando tengas todo configurado.",
        ),
        ResolucionProtocoloReadiness.TIMEOUT: _explicacion(
            "Pasó demasiado tiempo sin que aprobaras en Sky-Claw.",
            "Volvé a correr y, apenas termines de configurar {herramienta}, tocá «Aprobar».",
        ),
        ResolucionProtocoloReadiness.CANAL_NO_DISPONIBLE: _explicacion(
            "Sky-Claw no pudo mostrarte la ventana de aprobación.",
            "Dejá abierta la pestaña de Sky-Claw durante toda la corrida y volvé a correr.",
        ),
        ResolucionProtocoloReadiness.PROCESO_TERMINO: _explicacion(
            "Se cerró {herramienta} antes de que aprobaras.",
            "No cierres la herramienta: configurala, tocá «Aprobar» en Sky-Claw y esperá el aviso para «{boton_inicio}».",
        ),
    }
)

_UIA_SIN_LECTURA = _explicacion(
    "Sky-Claw no pudo leer la ventana de {herramienta}.",
    "Dejá la ventana visible (sin minimizar ni tapar) y volvé a correr.",
)
_UIA_INTERNO = _explicacion(
    "Sky-Claw no pudo revisar la configuración de {herramienta}.",
    "Esto no lo arreglás vos: guardá el detalle técnico y avisá para que lo revisen.",
)
_UIA_PROCESO = _explicacion(
    "Sky-Claw perdió de vista a {herramienta}.",
    "Cerrá cualquier otra copia abierta de TexGen o DynDOLOD y volvé a correr.",
)
_UIA_VENTANA = _explicacion(
    "No encontré la ventana de {herramienta}.",
    "Esperá a que abra del todo; si hay otra ventana igual abierta, cerrala y volvé a correr.",
)
_UIA_TAPADA = _explicacion(
    "Había algo tapando la ventana de {herramienta} (por ejemplo un cartel).",
    "Si el cartel habla de «stitched», apretá «Ignore»; si es otro, leelo y cerralo. Después volvé a correr.",
)

EXPLICACION_POR_RAZON_UIA: Mapping[RazonPreflight, Explicacion] = MappingProxyType(
    {
        RazonPreflight.OUTPUT_DIFIERE: _explicacion(
            "La carpeta de salida (Output) de {herramienta} no quedó como Sky-Claw necesita.",
            "Volvé a correr y, antes de aprobar, poné en Output exactamente la ruta que te muestra Sky-Claw.",
        ),
        RazonPreflight.TOOL_DESCONOCIDA: _UIA_INTERNO,
        RazonPreflight.SELECTOR_SIN_CRITERIOS: _UIA_INTERNO,
        RazonPreflight.ESPERADO_NO_CANONICALIZABLE: _UIA_INTERNO,
        RazonPreflight.EJECUTABLE_NO_CANONICALIZABLE: _UIA_INTERNO,
        RazonPreflight.OBSERVADO_NO_CANONICALIZABLE: _UIA_INTERNO,
        RazonPreflight.UIA_NO_DISPONIBLE: _UIA_SIN_LECTURA,
        RazonPreflight.ERROR_UIA: _UIA_SIN_LECTURA,
        RazonPreflight.ENUMERACION_INCOMPLETA: _UIA_SIN_LECTURA,
        RazonPreflight.VALOR_NO_LEIBLE: _UIA_SIN_LECTURA,
        RazonPreflight.PROCESO_NO_ENCONTRADO: _UIA_PROCESO,
        RazonPreflight.PROCESO_AMBIGUO: _UIA_PROCESO,
        RazonPreflight.PID_NO_COINCIDE: _UIA_PROCESO,
        RazonPreflight.IDENTIDAD_NO_DEMOSTRABLE: _UIA_PROCESO,
        RazonPreflight.IDENTIDAD_CAMBIO_DURANTE_LA_OBSERVACION: _UIA_PROCESO,
        RazonPreflight.PID_NO_OBSERVABLE: _UIA_PROCESO,
        RazonPreflight.VENTANA_NO_ENCONTRADA: _UIA_VENTANA,
        RazonPreflight.VENTANA_AMBIGUA: _UIA_VENTANA,
        RazonPreflight.CONTROL_NO_ENCONTRADO: _UIA_TAPADA,
        RazonPreflight.CONTROL_AMBIGUO: _UIA_TAPADA,
        RazonPreflight.CONTROL_FUERA_DEL_PROCESO: _UIA_TAPADA,
    }
)

#: #6 del rig: el corte llegó mientras Sky-Claw verificaba (se tocó el botón de arranque o se cerró la herramienta).
_CERRADA_ANTES_DE_TIEMPO = _explicacion(
    "Se cerró (o cambió) la ventana de {herramienta} antes de tiempo.",
    "Volvé a correr y no toques «{boton_inicio}» ni cierres la ventana hasta que Sky-Claw diga «ya podés apretar».",
)
_CORTES_DE_VENTANA_PERDIDA: frozenset[Corte] = frozenset(
    {
        ResolucionProtocoloReadiness.PROCESO_TERMINO,
        RazonPreflight.ERROR_UIA,
        RazonPreflight.UIA_NO_DISPONIBLE,
        RazonPreflight.PROCESO_NO_ENCONTRADO,
        RazonPreflight.VENTANA_NO_ENCONTRADA,
        RazonPreflight.VALOR_NO_LEIBLE,
        RazonPreflight.OUTPUT_DIFIERE,
    }
)

_GENERICA = _explicacion(
    "La etapa 9 no terminó bien.",
    "Mirá el detalle técnico para saber qué pasó; si no lo entendés, guardalo y avisá para que lo revisen.",
)
_PREFLIGHT_EN_ROJO = _explicacion(
    "Sky-Claw frenó antes de arrancar: encontró un problema en tu instalación.",
    "Abrí el detalle técnico para ver qué es y arreglalo antes de volver a correr.",
)
_PASO_PENDIENTE = _explicacion(
    "TexGen terminó, pero falta un paso tuyo antes de seguir con DynDOLOD.",
    "Hacé lo que indica el detalle y después tocá «Continuar DynDOLOD».",
)


def explicar_fallo(
    *,
    mensaje_tecnico: str,
    fase: FaseAsistida | None,
    herramienta: str | None,
    corte: Corte | None,
    razon: str | None = None,
    paso_pendiente: bool = False,
) -> dict[str, str]:
    """La ``guia`` de un resultado fallido: ``{titulo, arreglo, detalle_tecnico}``; el técnico nunca se pierde."""
    if paso_pendiente:
        explicacion = _PASO_PENDIENTE
    elif corte is not None and fase in FASES_DE_NO_TOCAR and corte in _CORTES_DE_VENTANA_PERDIDA:
        explicacion = _CERRADA_ANTES_DE_TIEMPO
    elif isinstance(corte, ResolucionProtocoloReadiness) and corte in EXPLICACION_POR_RESOLUCION:
        explicacion = EXPLICACION_POR_RESOLUCION[corte]
    elif isinstance(corte, RazonPreflight) and corte in EXPLICACION_POR_RAZON_UIA:
        explicacion = EXPLICACION_POR_RAZON_UIA[corte]
    elif razon == "PreflightBlocked":
        explicacion = _PREFLIGHT_EN_ROJO
    else:
        explicacion = _GENERICA
    nombre = herramienta or "la herramienta"
    botones = BOTONES_POR_HERRAMIENTA.get(herramienta or "", _BOTONES_GENERICOS)
    datos = {"herramienta": nombre, "boton_inicio": botones.inicio}
    return {
        "titulo": _rellenar(explicacion.titulo, datos),
        "arreglo": _rellenar(explicacion.arreglo, datos),
        "detalle_tecnico": mensaje_tecnico,
    }
