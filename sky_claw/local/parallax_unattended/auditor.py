"""Post-check de artefactos: lo que el informe dice que escribió, ¿está en disco y sirve?

Qué verifica, y por qué cada cosa
---------------------------------
El informe declara rutas relativas de salida. Este módulo las vuelve a abrir **desde disco** y
comprueba cuatro cosas que el generador no puede certificar sobre sí mismo:

1. **La ruta está dentro de la raíz de salida y no atraviesa enlaces.** Una ruta absoluta, con
   ``..``, con unidad, con nombres reservados de Windows o que apunte a un symlink/junction se
   rechaza: en una corrida desatendida, escribir o leer fuera del mod es el daño irreversible.
2. **El archivo existe.** Un informe que declara ``PUBLICADA`` sobre un archivo ausente es la
   forma más barata de falso verde.
3. **La cabecera es coherente con el tamaño.** Se recalcula la cadena de mips completa de
   BC4 y se exige que coincida **byte a byte** con el archivo. Detecta truncados, que es el
   modo en que falla una escritura interrumpida.
4. **La altura no es plana ni colapsó al comprimir.** Se decodifican bloques BC4 muestreados
   con paso coprimo (sin aliasing espacial en grillas periódicas) y se mide la desviación
   estándar del campo de altura contra ``SD_MINIMA`` y, cuando el informe trae ``amplitud``
   (#797, ADR 0014), contra la desviación previa a cuantizar.

Por qué BC4 y por qué ese umbral
--------------------------------
El generador escribe ``_p.dds`` en BC4 UNORM (``BC4U``, ``ATI1`` o ``DX10`` con formato 80).
En el triaje del issue #792 (documentado en ADR 0014, corregido en #794 y #797), los mapas
planos medidos en el lote real cayeron en 1,1–3,5 sobre 255 del lado inerte frente a 20,2–47,0
del lado sano. ``8/255`` cae en el medio de ese hueco —más de 2× por encima del inerte más
alto y menos de la mitad del sano más bajo— y se complementa con la comparación contra
``amplitud.desviacion`` cuando el informe la declara.

Un formato que no sea BC4 UNORM **no se aprueba**: se registra como no verificado. En una
tubería desatendida, "no lo pude medir" y "está bien" no pueden ser el mismo estado.

Límites declarados
------------------
La sd se estima sobre hasta 4096 bloques muestreados con paso coprimo sobre el total de
bloques del nivel base: evita el aliasing de subgrillas potencias de dos sobre texturas con
juntas periódicas (ladrillo, baldosas) y alcanza para separar plano de con-relieve sin
decodificar los 262.144 bloques de una 2048².
"""

from __future__ import annotations

import enum
import math
import os
import pathlib
import stat
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from sky_claw.app.security.links import (
    link_kind_and_identity_or_raise,
    reject_unclassified_reparse_point,
)

from .veredicto import (
    EFECTOS_CONOCIDOS,
    EFECTOS_QUE_DEJAN_ARCHIVO,
    ESTADOS_CONOCIDOS,
    VERSIONES_DEL_LOTE_CONOCIDAS,
)

#: Desviación estándar mínima del campo de altura, sobre 255. Ver el docstring del módulo.
SD_MINIMA: Final[float] = 8.0 / 255.0

#: Fracción mínima de ``amplitud.desviacion`` que el BC4 en disco debe conservar (ADR 0014).
_FACTOR_MINIMO_DE_AMPLITUD: Final[float] = 0.5

#: Bloques BC4 que se decodifican como máximo por archivo (de 262.144 en una 2048²).
MAXIMO_DE_BLOQUES_MUESTREADOS: Final[int] = 4096

#: Paso primo (constante multiplicativa de Knuth) para muestrear sin aliasing en grillas 2^n.
_PRIMO_MUESTREO: Final[int] = 2654435761

#: Cabecera clásica de DDS: 4 de firma + 124 de ``DDS_HEADER``.
BYTES_DE_CABECERA: Final[int] = 128

#: Cabecera extra de DX10, cuando el ``fourcc`` es ``DX10``.
BYTES_DE_CABECERA_DX10: Final[int] = 20

LADO_MINIMO: Final[int] = 4
LADO_MAXIMO: Final[int] = 8192

#: Tope de lectura por artefacto. Una textura de 8192² con la cadena completa de BC4 ronda los
#: 33 MB, así que 256 MB no rechaza nada legítimo y evita que un archivo corrupto o ajeno al
#: pipeline se lleve la memoria del runner.
MAXIMO_DE_BYTES: Final[int] = 256 * 1024 * 1024

_FIRMA: Final[bytes] = b"DDS "
_TAMANO_DE_CABECERA_DDS: Final[int] = 124
_TAMANO_DEL_PIXELFORMAT: Final[int] = 32
_BYTES_POR_BLOQUE_BC4: Final[int] = 8
_FOURCC_BC4_DIRECTO: Final[frozenset[str]] = frozenset({"BC4U", "ATI1"})
_DXGI_FORMAT_BC4_UNORM: Final[int] = 80
_DIMENSION_TEXTURA_2D: Final[int] = 3
_DX10_MISC_CUBEMAP: Final[int] = 0x4
_DDSCAPS2_CUBEMAP_O_VOLUMEN: Final[int] = 0x200 | 0x200000
_DDSD_ALTO_Y_ANCHO: Final[int] = 0x2 | 0x4
_DDSD_PROFUNDIDAD: Final[int] = 0x800000
_DDPF_FOURCC: Final[int] = 0x4
_CARACTERES_PROHIBIDOS_RUTA: Final[frozenset[str]] = frozenset('<>:"|?*\\')
_NOMBRES_RESERVADOS_WIN: Final[frozenset[str]] = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "conin$",
        "conout$",
        *(f"com{i}" for i in range(10)),
        *(f"lpt{i}" for i in range(10)),
    }
)


class CodigoDeHallazgo(enum.StrEnum):
    """Qué está mal en un artefacto. Vocabulario cerrado."""

    INFORME_INVALIDO = "informe_invalido"
    SIN_ARTEFACTOS_AUDITABLES = "sin_artefactos_auditables"
    RUTA_FUERA_DE_LA_SALIDA = "ruta_fuera_de_la_salida"
    NO_EXISTE = "no_existe"
    NO_LEGIBLE = "no_legible"
    TAMANO_EXCESIVO = "tamano_excesivo"
    NO_ES_DDS = "no_es_dds"
    DIMENSIONES_INVALIDAS = "dimensiones_invalidas"
    TAMANO_INCOHERENTE = "tamano_incoherente"
    ALTURA_PLANA = "altura_plana"


#: Hallazgos donde el propio informe no cumple el contrato (degradan a ``INCONSISTENTE``).
HALLAZGOS_DE_INCONSISTENCIA: Final[frozenset[CodigoDeHallazgo]] = frozenset({CodigoDeHallazgo.INFORME_INVALIDO})

#: Hallazgos que piden revisión humana porque los bytes no distinguen defecto de superficie lisa.
HALLAZGOS_DE_REVISION: Final[frozenset[CodigoDeHallazgo]] = frozenset({CodigoDeHallazgo.ALTURA_PLANA})

#: Hallazgos donde el artefacto está ausente, corrupto o fuera de contrato (falla dura).
HALLAZGOS_DE_FALLA: Final[frozenset[CodigoDeHallazgo]] = frozenset(
    {
        CodigoDeHallazgo.SIN_ARTEFACTOS_AUDITABLES,
        CodigoDeHallazgo.RUTA_FUERA_DE_LA_SALIDA,
        CodigoDeHallazgo.NO_EXISTE,
        CodigoDeHallazgo.NO_LEGIBLE,
        CodigoDeHallazgo.TAMANO_EXCESIVO,
        CodigoDeHallazgo.NO_ES_DDS,
        CodigoDeHallazgo.DIMENSIONES_INVALIDAS,
        CodigoDeHallazgo.TAMANO_INCOHERENTE,
    }
)


class _DdsInvalidoError(Exception):
    """El archivo no tiene una cabecera DDS que este módulo sepa leer."""


class _RutaRechazadaError(Exception):
    """La ruta declarada no se puede aceptar sin riesgo de escribir fuera de la salida."""


@dataclass(frozen=True, slots=True)
class _ObjetivoDeAuditoria:
    ruta: str
    desviacion_declarada: float | None = None


@dataclass(frozen=True, slots=True)
class CabeceraDDS:
    ancho: int
    alto: int
    mips: int
    fourcc: str
    profundidad: int
    dxgi_format: int | None = None
    es_2d_simple: bool = True

    @property
    def bytes_de_cabecera(self) -> int:
        return BYTES_DE_CABECERA + (BYTES_DE_CABECERA_DX10 if self.fourcc == "DX10" else 0)


@dataclass(frozen=True, slots=True)
class Hallazgo:
    """Un artefacto que no se puede aprobar, con el dato que lo sostiene."""

    ruta: str
    codigo: CodigoDeHallazgo
    detalle: str


@dataclass(frozen=True, slots=True)
class InformeDeAuditoria:
    """Resultado de releer la salida. ``ok`` exige que NADA haya quedado sin verificar."""

    verificados: tuple[str, ...]
    hallazgos: tuple[Hallazgo, ...]
    no_verificados: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.hallazgos and not self.no_verificados


def _leer_cabecera(datos: bytes) -> CabeceraDDS:
    if len(datos) < BYTES_DE_CABECERA:
        raise _DdsInvalidoError(f"el archivo tiene {len(datos)} bytes, menos que una cabecera DDS")
    if datos[:4] != _FIRMA:
        raise _DdsInvalidoError(f"la firma es {datos[:4]!r}, no {_FIRMA!r}")
    tamano_de_cabecera, banderas, alto, ancho = struct.unpack("<4I", datos[4:20])
    if tamano_de_cabecera != _TAMANO_DE_CABECERA_DDS:
        raise _DdsInvalidoError(f"dwSize = {tamano_de_cabecera}, se esperaba 124")
    if (banderas & _DDSD_ALTO_Y_ANCHO) != _DDSD_ALTO_Y_ANCHO:
        raise _DdsInvalidoError("DDS_HEADER.dwFlags no declara alto y ancho")
    profundidad, mips = struct.unpack("<II", datos[24:32])
    tamano_pf, banderas_pf = struct.unpack("<II", datos[76:84])
    if tamano_pf != _TAMANO_DEL_PIXELFORMAT:
        raise _DdsInvalidoError("ddspf.dwSize != 32")
    if not (banderas_pf & _DDPF_FOURCC):
        raise _DdsInvalidoError("ddspf.dwFlags no declara DDPF_FOURCC")
    fourcc = datos[84:88].decode("latin-1")
    caps2 = struct.unpack("<I", datos[112:116])[0]
    es_2d_simple = not bool(caps2 & _DDSCAPS2_CUBEMAP_O_VOLUMEN or banderas & _DDSD_PROFUNDIDAD)
    dxgi_format: int | None = None
    if fourcc == "DX10":
        if len(datos) < BYTES_DE_CABECERA + BYTES_DE_CABECERA_DX10:
            raise _DdsInvalidoError("el archivo declara DX10 y no trae la extensión de 20 bytes")
        dxgi_format, dimension, misc_flag, array_size, _ = struct.unpack(
            "<5I", datos[BYTES_DE_CABECERA : BYTES_DE_CABECERA + BYTES_DE_CABECERA_DX10]
        )
        es_2d_simple = (
            es_2d_simple
            and dimension == _DIMENSION_TEXTURA_2D
            and array_size == 1
            and not bool(misc_flag & _DX10_MISC_CUBEMAP)
        )
    return CabeceraDDS(
        ancho=ancho,
        alto=alto,
        mips=mips,
        fourcc=fourcc,
        profundidad=1 if profundidad == 0 and not (banderas & _DDSD_PROFUNDIDAD) else profundidad,
        dxgi_format=dxgi_format,
        es_2d_simple=es_2d_simple,
    )


def _es_bc4_unorm(cabecera: CabeceraDDS) -> bool:
    """``BC4U``, ``ATI1`` o ``DX10`` con ``DXGI_FORMAT_BC4_UNORM`` (80), igual que ``export.py``."""

    if cabecera.fourcc in _FOURCC_BC4_DIRECTO:
        return True
    return cabecera.fourcc == "DX10" and cabecera.dxgi_format == _DXGI_FORMAT_BC4_UNORM


def _dimensiones_plausibles(cabecera: CabeceraDDS) -> bool:
    """Rango de lados, textura 2D simple y cantidad de mips. **No** se exige potencia de dos.

    El redondeo por bloques de 4×4 de BC4 (``max(1, (lado + 3) // 4)``) está definido para
    cualquier dimensión en ``[LADO_MINIMO, LADO_MAXIMO]``, así que el auditor valida la
    coherencia estructural de los bytes sin rechazar texturas que no sean potencia de dos.
    """

    if not cabecera.es_2d_simple or cabecera.profundidad != 1:
        return False
    lados = (cabecera.ancho, cabecera.alto)
    if any(lado < LADO_MINIMO or lado > LADO_MAXIMO for lado in lados):
        return False
    return 1 <= cabecera.mips <= max(lados).bit_length()


def _bytes_de_bc4(ancho: int, alto: int, mips: int) -> int:
    """Tamaño exacto del payload BC4 con esa cadena de mips, incluidos los niveles chicos."""

    total = 0
    for nivel in range(mips):
        bloques_ancho = max(1, ((ancho >> nivel) + 3) // 4)
        bloques_alto = max(1, ((alto >> nivel) + 3) // 4)
        total += bloques_ancho * bloques_alto * _BYTES_POR_BLOQUE_BC4
    return total


def _valores_del_bloque(bloque: bytes) -> tuple[int, ...]:
    """Decodifica un bloque BC4 (8 bytes) a sus 16 valores de altura, según la especificación."""

    extremo_alto, extremo_bajo = bloque[0], bloque[1]
    if extremo_alto > extremo_bajo:
        tabla = [extremo_alto, extremo_bajo]
        tabla += [((8 - i) * extremo_alto + (i - 1) * extremo_bajo) // 7 for i in range(2, 8)]
    else:
        tabla = [extremo_alto, extremo_bajo]
        tabla += [((6 - i) * extremo_alto + (i - 1) * extremo_bajo) // 5 for i in range(2, 6)]
        tabla += [0, 255]
    indices = int.from_bytes(bloque[2:8], "little")
    return tuple(tabla[(indices >> (3 * posicion)) & 0x7] for posicion in range(16))


def _sd_bc4(datos: bytes, cabecera: CabeceraDDS) -> float:
    """Desviación estándar del campo de altura, sobre bloques muestreados. En escala 0..1."""

    bloques_ancho = max(1, (cabecera.ancho + 3) // 4)
    bloques_alto = max(1, (cabecera.alto + 3) // 4)
    total_de_bloques = bloques_ancho * bloques_alto
    muestras = min(total_de_bloques, MAXIMO_DE_BLOQUES_MUESTREADOS)
    if total_de_bloques <= MAXIMO_DE_BLOQUES_MUESTREADOS:
        paso = 1
    else:
        paso = _PRIMO_MUESTREO
        while math.gcd(paso, total_de_bloques) != 1:
            paso += 2

    base = cabecera.bytes_de_cabecera
    suma = 0.0
    suma_de_cuadrados = 0.0
    cantidad = 0
    for k in range(muestras):
        indice = (k * paso) % total_de_bloques
        desde = base + indice * _BYTES_POR_BLOQUE_BC4
        bloque = datos[desde : desde + _BYTES_POR_BLOQUE_BC4]
        if len(bloque) < _BYTES_POR_BLOQUE_BC4:
            raise _DdsInvalidoError(f"bloque truncado en el desplazamiento {desde}")
        for valor in _valores_del_bloque(bloque):
            suma += valor
            suma_de_cuadrados += valor * valor
            cantidad += 1

    if cantidad == 0:
        raise _DdsInvalidoError("no se decodificó ningún bloque")
    media = suma / cantidad
    varianza = (suma_de_cuadrados / cantidad) - (media * media)
    return math.sqrt(max(0.0, varianza)) / 255.0


def _validar_sintaxis_relativa(relativa: str) -> list[str]:
    if "\x00" in relativa:
        raise _RutaRechazadaError(f"{relativa!r} contiene un byte nulo")
    if any(ord(c) < 0x20 or ord(c) > 0x7E for c in relativa):
        raise _RutaRechazadaError(f"{relativa!r} contiene caracteres fuera de ASCII imprimible")
    pura = pathlib.PureWindowsPath(relativa)
    if pura.drive or pura.root or pura.is_absolute() or relativa.startswith("/"):
        raise _RutaRechazadaError(f"{relativa!r} es absoluta o trae unidad")
    if ".." in pura.parts:
        raise _RutaRechazadaError(f"{relativa!r} intenta salir con '..'")
    if ":" in relativa:
        raise _RutaRechazadaError(f"{relativa!r} trae ':' (unidad o flujo alternativo)")
    if any(c in _CARACTERES_PROHIBIDOS_RUTA for c in relativa):
        raise _RutaRechazadaError(f"{relativa!r} contiene caracteres o separadores no admitidos")
    partes = relativa.split("/")
    if not partes or any(parte in ("", ".", "..") for parte in partes):
        raise _RutaRechazadaError(f"{relativa!r} tiene segmentos vacíos o relativos")
    for parte in partes:
        if parte.endswith((".", " ")):
            raise _RutaRechazadaError(f"{relativa!r} tiene un segmento terminado en punto o espacio")
        raiz_nombre = parte.partition(".")[0].casefold()
        if raiz_nombre in _NOMBRES_RESERVADOS_WIN:
            raise _RutaRechazadaError(f"{relativa!r} usa un nombre de dispositivo reservado de Windows")
    return partes


def _verificar_sin_enlaces(ruta: pathlib.Path) -> None:
    tipo, identidad = link_kind_and_identity_or_raise(ruta)
    if tipo is not None:
        raise _RutaRechazadaError(f"{ruta} es un enlace ({tipo})")
    if identidad is not None:
        try:
            reject_unclassified_reparse_point(ruta, identidad)
        except OSError as exc:
            raise _RutaRechazadaError(str(exc)) from exc


def _resolver(raiz: pathlib.Path, relativa: str) -> pathlib.Path:
    """Resuelve una ruta declarada exigiendo contención física y léxica dentro de ``raiz``."""

    partes = _validar_sintaxis_relativa(relativa)
    _verificar_sin_enlaces(raiz)
    actual = raiz
    for parte in partes:
        actual = actual / parte
        _verificar_sin_enlaces(actual)
    destino = actual.resolve()
    if not destino.is_relative_to(raiz):
        raise _RutaRechazadaError(f"{destino} queda fuera de {raiz}")
    return destino


def _desviacion_de_entrada(entrada: Mapping[str, object], posicion: int) -> float | None:
    amplitud = entrada.get("amplitud")
    if amplitud is None:
        return None
    if not isinstance(amplitud, Mapping):
        raise _RutaRechazadaError(f"archivos[{posicion}].amplitud no es un objeto")
    desviacion = amplitud.get("desviacion")
    if (
        isinstance(desviacion, bool)
        or not isinstance(desviacion, (int, float))
        or not math.isfinite(desviacion)
        or not (0.0 <= float(desviacion) <= 1.0)
    ):
        raise _RutaRechazadaError(f"archivos[{posicion}].amplitud.desviacion = {desviacion!r} es inválida")
    return float(desviacion)


def _objetivos_declarados(informe: Mapping[str, object]) -> tuple[_ObjetivoDeAuditoria, ...]:
    """Rutas que el informe dice haber dejado en la salida. No se confía en ``conteos``.

    Una entrada con forma inválida **corta** la auditoría en vez de saltearse: saltearla dejaría
    el resto auditado y el lote aprobado con un artefacto que nadie miró.
    """

    if not isinstance(informe, Mapping):
        raise _RutaRechazadaError("el informe debe ser un objeto")
    version = informe.get("version_del_lote")
    if not isinstance(version, str) or version not in VERSIONES_DEL_LOTE_CONOCIDAS:
        raise _RutaRechazadaError(f"version_del_lote = {version!r} no está en {sorted(VERSIONES_DEL_LOTE_CONOCIDAS)}")
    crudo = informe.get("archivos")
    if not isinstance(crudo, list):
        raise _RutaRechazadaError("falta 'archivos' o no es una lista")

    vistas: set[str] = set()
    objetivos: list[_ObjetivoDeAuditoria] = []
    for posicion, entrada in enumerate(crudo):
        if not isinstance(entrada, Mapping):
            raise _RutaRechazadaError(f"archivos[{posicion}] no es un objeto")
        estado = entrada.get("estado")
        if not isinstance(estado, str) or estado not in ESTADOS_CONOCIDOS:
            raise _RutaRechazadaError(f"archivos[{posicion}].estado = {estado!r} es inválido")
        efecto = entrada.get("efecto")
        if efecto is not None and (not isinstance(efecto, str) or efecto not in EFECTOS_CONOCIDOS):
            raise _RutaRechazadaError(f"archivos[{posicion}].efecto = {efecto!r} es inválido")
        ruta = entrada.get("ruta_de_salida")
        if not isinstance(ruta, str) or not ruta:
            raise _RutaRechazadaError(f"archivos[{posicion}] no trae ruta_de_salida válida")
        clave = ruta.casefold()
        if clave in vistas:
            raise _RutaRechazadaError(f"archivos[{posicion}].ruta_de_salida = {ruta!r} está duplicada")
        vistas.add(clave)
        desviacion = _desviacion_de_entrada(entrada, posicion)

        if estado == "ESCRITA":
            if efecto not in EFECTOS_QUE_DEJAN_ARCHIVO:
                raise _RutaRechazadaError(f"archivos[{posicion}] declara ESCRITA con efecto {efecto!r}")
            objetivos.append(_ObjetivoDeAuditoria(ruta=ruta, desviacion_declarada=desviacion))
        elif estado in ("EN_REVISION", "NO_PROCESADA"):
            if efecto is not None:
                raise _RutaRechazadaError(f"archivos[{posicion}] declara {estado} con efecto {efecto!r}")
        elif estado == "NO_ESCRITA" and efecto in EFECTOS_QUE_DEJAN_ARCHIVO:
            raise _RutaRechazadaError(f"archivos[{posicion}] declara NO_ESCRITA con efecto {efecto!r}")

    if "otras_rutas" in informe and informe.get("otras_rutas") is not None:
        otras = informe.get("otras_rutas")
        if not isinstance(otras, list):
            raise _RutaRechazadaError("'otras_rutas' debe ser una lista")
        for posicion, entrada in enumerate(otras):
            if not isinstance(entrada, Mapping):
                raise _RutaRechazadaError(f"otras_rutas[{posicion}] no es un objeto")
            ruta = entrada.get("ruta")
            if not isinstance(ruta, str) or not ruta:
                raise _RutaRechazadaError(f"otras_rutas[{posicion}] no trae ruta válida")
            clave = ruta.casefold()
            if clave in vistas:
                raise _RutaRechazadaError(f"otras_rutas[{posicion}].ruta = {ruta!r} está duplicada")
            vistas.add(clave)
            efecto = entrada.get("efecto")
            if not isinstance(efecto, str) or efecto not in EFECTOS_CONOCIDOS or efecto in ("PUBLICADA", "REEMPLAZADA"):
                raise _RutaRechazadaError(f"otras_rutas[{posicion}].efecto = {efecto!r} es inválido")
            if efecto in EFECTOS_QUE_DEJAN_ARCHIVO:
                objetivos.append(_ObjetivoDeAuditoria(ruta=ruta))

    return tuple(objetivos)


def _leer_bytes_acotados(destino: pathlib.Path) -> tuple[bytes | None, Hallazgo | None, str]:
    """Abre *destino* una sola vez, verifica modo y tope sobre el descriptor, y lee acotado."""

    with destino.open("rb") as archivo:
        identidad = os.fstat(archivo.fileno())
        if not stat.S_ISREG(identidad.st_mode):
            return None, None, "no es un archivo regular"
        if identidad.st_size > MAXIMO_DE_BYTES:
            return (
                None,
                Hallazgo("", CodigoDeHallazgo.TAMANO_EXCESIVO, f"{identidad.st_size} bytes, tope {MAXIMO_DE_BYTES}"),
                "",
            )
        datos = archivo.read(MAXIMO_DE_BYTES + 1)
    if len(datos) > MAXIMO_DE_BYTES:
        return (
            None,
            Hallazgo("", CodigoDeHallazgo.TAMANO_EXCESIVO, f"{len(datos)} bytes, tope {MAXIMO_DE_BYTES}"),
            "",
        )
    return datos, None, ""


def _auditar_una(objetivo: _ObjetivoDeAuditoria, destino: pathlib.Path) -> tuple[Hallazgo | None, bool]:
    """Devuelve (hallazgo o None, verificado). Un formato no medible NO es verificado."""

    ruta = objetivo.ruta
    try:
        datos, exceso, error_modo = _leer_bytes_acotados(destino)
    except OSError as exc:
        return Hallazgo(ruta, CodigoDeHallazgo.NO_LEGIBLE, f"no se pudo leer: {exc}"), False
    if error_modo:
        return Hallazgo(ruta, CodigoDeHallazgo.NO_LEGIBLE, error_modo), False
    if exceso is not None:
        return Hallazgo(ruta, exceso.codigo, exceso.detalle), False
    assert datos is not None

    try:
        cabecera = _leer_cabecera(datos)
    except _DdsInvalidoError as exc:
        return Hallazgo(ruta, CodigoDeHallazgo.NO_ES_DDS, str(exc)), False
    if not _dimensiones_plausibles(cabecera):
        return (
            Hallazgo(
                ruta,
                CodigoDeHallazgo.DIMENSIONES_INVALIDAS,
                f"{cabecera.ancho}x{cabecera.alto}, {cabecera.mips} mips",
            ),
            False,
        )
    if not _es_bc4_unorm(cabecera):
        return None, False
    esperado = _bytes_de_bc4(cabecera.ancho, cabecera.alto, cabecera.mips)
    real = len(datos) - cabecera.bytes_de_cabecera
    if esperado != real:
        return (
            Hallazgo(
                ruta,
                CodigoDeHallazgo.TAMANO_INCOHERENTE,
                f"{cabecera.ancho}x{cabecera.alto} con {cabecera.mips} mips pide {esperado} bytes y hay {real}",
            ),
            False,
        )
    sd = _sd_bc4(datos, cabecera)
    if sd < SD_MINIMA:
        return (
            Hallazgo(
                ruta,
                CodigoDeHallazgo.ALTURA_PLANA,
                f"sd = {sd * 255:.2f}/255, por debajo de {SD_MINIMA * 255:.0f}/255",
            ),
            False,
        )
    if objetivo.desviacion_declarada is not None and (
        objetivo.desviacion_declarada < SD_MINIMA or sd < objetivo.desviacion_declarada * _FACTOR_MINIMO_DE_AMPLITUD
    ):
        return (
            Hallazgo(
                ruta,
                CodigoDeHallazgo.ALTURA_PLANA,
                f"sd = {sd * 255:.2f}/255 cayó frente a la amplitud declarada ({objetivo.desviacion_declarada * 255:.2f}/255)",
            ),
            False,
        )
    return None, True


def auditar(informe: Mapping[str, object], raiz_de_salida: pathlib.Path) -> InformeDeAuditoria:
    """Relee cada artefacto declarado y devuelve lo que no se puede aprobar. Nunca lanza."""

    try:
        objetivos = _objetivos_declarados(informe)
    except _RutaRechazadaError as exc:
        return InformeDeAuditoria(
            verificados=(),
            hallazgos=(Hallazgo("", CodigoDeHallazgo.INFORME_INVALIDO, str(exc)),),
            no_verificados=(),
        )

    if not objetivos:
        # Cero artefactos declarados NO es "todo bien": es una auditoría que no verificó nada, y
        # un `ok` acá sería exactamente el falso verde que esta capa existe para cerrar.
        return InformeDeAuditoria(
            verificados=(),
            hallazgos=(
                Hallazgo(
                    "",
                    CodigoDeHallazgo.SIN_ARTEFACTOS_AUDITABLES,
                    "el informe no declara ningún artefacto escrito que se pueda releer",
                ),
            ),
            no_verificados=(),
        )

    try:
        _verificar_sin_enlaces(raiz_de_salida)
        raiz = raiz_de_salida.resolve()
    except _RutaRechazadaError as exc:
        return InformeDeAuditoria(
            verificados=(),
            hallazgos=tuple(
                Hallazgo(obj.ruta, CodigoDeHallazgo.RUTA_FUERA_DE_LA_SALIDA, str(exc)) for obj in objetivos
            ),
            no_verificados=(),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return InformeDeAuditoria(
            verificados=(),
            hallazgos=tuple(
                Hallazgo(obj.ruta, CodigoDeHallazgo.NO_LEGIBLE, f"no se pudo resolver la raíz de salida: {exc}")
                for obj in objetivos
            ),
            no_verificados=(),
        )
    verificados: list[str] = []
    hallazgos: list[Hallazgo] = []
    no_verificados: list[str] = []

    for objetivo in objetivos:
        ruta = objetivo.ruta
        try:
            destino = _resolver(raiz, ruta)
        except _RutaRechazadaError as exc:
            hallazgos.append(Hallazgo(ruta, CodigoDeHallazgo.RUTA_FUERA_DE_LA_SALIDA, str(exc)))
            continue
        except (OSError, RuntimeError, ValueError) as exc:
            hallazgos.append(Hallazgo(ruta, CodigoDeHallazgo.NO_LEGIBLE, f"no se pudo resolver la ruta: {exc}"))
            continue
        try:
            existe = destino.is_file()
        except (OSError, RuntimeError, ValueError) as exc:
            hallazgos.append(Hallazgo(ruta, CodigoDeHallazgo.NO_LEGIBLE, f"no se pudo inspeccionar: {exc}"))
            continue
        if not existe:
            hallazgos.append(Hallazgo(ruta, CodigoDeHallazgo.NO_EXISTE, f"el informe la declara y no está en {raiz}"))
            continue
        hallazgo, verificado = _auditar_una(objetivo, destino)
        if hallazgo is not None:
            hallazgos.append(hallazgo)
        if verificado:
            verificados.append(ruta)
        elif hallazgo is None:
            no_verificados.append(ruta)

    return InformeDeAuditoria(
        verificados=tuple(verificados),
        hallazgos=tuple(hallazgos),
        no_verificados=tuple(no_verificados),
    )


def codigos(hallazgos: Sequence[Hallazgo]) -> tuple[CodigoDeHallazgo, ...]:
    """Los códigos presentes, sin repetir y en el orden en que aparecieron. Para imprimir."""

    vistos: list[CodigoDeHallazgo] = []
    for hallazgo in hallazgos:
        if hallazgo.codigo not in vistos:
            vistos.append(hallazgo.codigo)
    return tuple(vistos)
