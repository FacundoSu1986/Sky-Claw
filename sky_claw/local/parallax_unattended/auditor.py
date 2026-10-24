"""Post-check de artefactos: lo que el informe dice que escribió, ¿está en disco y sirve?

Qué verifica, y por qué cada cosa
---------------------------------
El informe declara rutas relativas de salida. Este módulo las vuelve a abrir **desde disco** y
comprueba cuatro cosas que el generador no puede certificar sobre sí mismo:

1. **La ruta está dentro de la raíz de salida.** Una ruta absoluta, con ``..`` o con unidad
   se rechaza: en una corrida desatendida, escribir fuera del mod es el daño irreversible.
2. **El archivo existe.** Un informe que declara ``PUBLICADA`` sobre un archivo ausente es la
   forma más barata de falso verde.
3. **La cabecera es coherente con el tamaño.** Se recalcula la cadena de mips completa de
   BC4 y se exige que coincida **byte a byte** con el archivo. Detecta truncados, que es el
   modo en que falla una escritura interrumpida.
4. **La altura no es plana.** Se decodifican bloques BC4 muestreados y se mide la desviación
   estándar del campo de altura.

Por qué BC4 y por qué ese umbral
--------------------------------
El generador escribe ``_p.dds`` en BC4U (verificado en la salida real: ``fourcc = BC4U``,
1024²–2048², 11–12 mips, y el tamaño del payload coincide byte a byte con la cadena de mips
completa). Medido sobre los ocho artefactos del lote real (issue #792), la distribución de sd
tiene un **hueco**: 1,1 / 1,3 / 3,2 / 3,5 sobre 255 del lado inerte, y 20,2 / 43,2 / 45,3 /
47,0 del lado sano. ``8/255`` cae dentro de ese hueco —más de 2× por encima del inerte más
alto y menos de la mitad del sano más bajo—, así que no hay zona gris que discutir.

Un formato que no sea BC4U **no se aprueba**: se registra como no verificado. En una tubería
desatendida, "no lo pude medir" y "está bien" no pueden ser el mismo estado.

Límites declarados
------------------
La sd se estima sobre bloques muestreados (hasta 4096), no sobre los 262.144 de una textura
de 2048²: alcanza para separar plano de con-relieve por un factor de 6, y no pretende medir
la distribución exacta. La decodificación BC4 de referencia está probada contra bloques
construidos a mano en los tests, no contra el decodificador de DirectX.
"""

from __future__ import annotations

import enum
import math
import pathlib
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from .veredicto import EFECTOS_QUE_DEJAN_ARCHIVO, VERSIONES_DEL_LOTE_CONOCIDAS

#: Desviación estándar mínima del campo de altura, sobre 255. Ver el docstring del módulo.
SD_MINIMA: Final[float] = 8.0 / 255.0

#: Bloques BC4 que se decodifican como máximo por archivo (de 262.144 en una 2048²).
MAXIMO_DE_BLOQUES_MUESTREADOS: Final[int] = 4096

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
_DDSD_PROFUNDIDAD: Final[int] = 0x800000


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


#: Hallazgos que piden revisión humana porque los bytes no distinguen defecto de superficie lisa.
HALLAZGOS_DE_REVISION: Final[frozenset[CodigoDeHallazgo]] = frozenset({CodigoDeHallazgo.ALTURA_PLANA})

#: Hallazgos donde el artefacto está ausente, corrupto o fuera de contrato (falla dura).
HALLAZGOS_DE_FALLA: Final[frozenset[CodigoDeHallazgo]] = frozenset(
    {
        CodigoDeHallazgo.INFORME_INVALIDO,
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
    profundidad, mips = struct.unpack("<II", datos[24:32])
    if struct.unpack("<I", datos[76:80])[0] != _TAMANO_DEL_PIXELFORMAT:
        raise _DdsInvalidoError("ddspf.dwSize != 32")
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
        if dxgi_format == _DXGI_FORMAT_BC4_UNORM:
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
    """Rango de lados, textura 2D simple y cadena de mips. **No** se exige potencia de dos.

    Exigir POT rechazaría salida legítima: ``--escala-x 0.75`` sobre una normal de 2048 produce
    1536, que DDS y BC4 aceptan sin problema. El formato no lo pide, el motor tampoco, y una
    comprobación más estricta que el formato sólo agrega falsos positivos que bloquean una
    corrida desatendida por una razón que no es un defecto.
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
    paso = max(1, math.ceil(math.sqrt(total_de_bloques / MAXIMO_DE_BLOQUES_MUESTREADOS)))

    base = cabecera.bytes_de_cabecera
    suma = 0.0
    suma_de_cuadrados = 0.0
    cantidad = 0
    for fila in range(0, bloques_alto, paso):
        for columna in range(0, bloques_ancho, paso):
            desde = base + (fila * bloques_ancho + columna) * _BYTES_POR_BLOQUE_BC4
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


def _resolver(raiz: pathlib.Path, relativa: str) -> pathlib.Path:
    """Resuelve una ruta declarada exigiendo que quede dentro de la raíz de salida.

    Las tres primeras comprobaciones son **redundantes** con la última: una ruta con ``..``,
    con unidad o absoluta ya quedaría fuera al resolverla. Se mantienen porque dan un mensaje
    que nombra la causa —"intenta salir con '..'"— en vez de un "queda fuera" genérico, y
    porque el costo es nulo. La garantía la da ``is_relative_to``; las otras son diagnóstico.
    """

    if "\x00" in relativa:
        raise _RutaRechazadaError(f"{relativa!r} contiene un byte nulo")
    pura = pathlib.PureWindowsPath(relativa)
    if pura.drive or pura.root or pura.is_absolute():
        raise _RutaRechazadaError(f"{relativa!r} es absoluta o trae unidad")
    if ".." in pura.parts:
        raise _RutaRechazadaError(f"{relativa!r} intenta salir con '..'")
    if ":" in relativa:
        raise _RutaRechazadaError(f"{relativa!r} trae ':' (unidad o flujo alternativo)")
    destino = (raiz / relativa).resolve()
    if not destino.is_relative_to(raiz):
        raise _RutaRechazadaError(f"{destino} queda fuera de {raiz}")
    return destino


def _rutas_declaradas(informe: Mapping[str, object]) -> tuple[str, ...]:
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
    rutas: list[str] = []
    for posicion, entrada in enumerate(crudo):
        if not isinstance(entrada, Mapping):
            raise _RutaRechazadaError(f"archivos[{posicion}] no es un objeto")
        if entrada.get("estado") != "ESCRITA":
            continue
        if entrada.get("efecto") not in EFECTOS_QUE_DEJAN_ARCHIVO:
            continue
        ruta = entrada.get("ruta_de_salida")
        if not isinstance(ruta, str) or not ruta:
            raise _RutaRechazadaError(f"archivos[{posicion}] se declara escrito y no trae ruta_de_salida")
        rutas.append(ruta)
    return tuple(rutas)


def _auditar_una(ruta: str, destino: pathlib.Path) -> tuple[Hallazgo | None, bool]:
    """Devuelve (hallazgo o None, verificado). Un formato no medible NO es verificado.

    La lectura va dentro de ``try``: entre ``is_file()`` y ``read_bytes()`` el archivo puede
    desaparecer o quedar bloqueado por otro proceso (el juego, el antivirus, MO2). Eso no puede
    tumbar la auditoría de los demás artefactos, así que se traduce a hallazgo.
    """

    try:
        tamano = destino.stat().st_size
        if tamano > MAXIMO_DE_BYTES:
            return (
                Hallazgo(ruta, CodigoDeHallazgo.TAMANO_EXCESIVO, f"{tamano} bytes, tope {MAXIMO_DE_BYTES}"),
                False,
            )
        datos = destino.read_bytes()
    except OSError as exc:
        return Hallazgo(ruta, CodigoDeHallazgo.NO_LEGIBLE, f"no se pudo leer: {exc}"), False

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
    return None, True


def auditar(informe: Mapping[str, object], raiz_de_salida: pathlib.Path) -> InformeDeAuditoria:
    """Relee cada artefacto declarado y devuelve lo que no se puede aprobar. Nunca lanza."""

    try:
        rutas = _rutas_declaradas(informe)
    except _RutaRechazadaError as exc:
        return InformeDeAuditoria(
            verificados=(),
            hallazgos=(Hallazgo("", CodigoDeHallazgo.INFORME_INVALIDO, str(exc)),),
            no_verificados=(),
        )

    if not rutas:
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
        raiz = raiz_de_salida.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        return InformeDeAuditoria(
            verificados=(),
            hallazgos=tuple(
                Hallazgo(ruta, CodigoDeHallazgo.NO_LEGIBLE, f"no se pudo resolver la raíz de salida: {exc}")
                for ruta in rutas
            ),
            no_verificados=(),
        )
    verificados: list[str] = []
    hallazgos: list[Hallazgo] = []
    no_verificados: list[str] = []

    for ruta in rutas:
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
        hallazgo, verificado = _auditar_una(ruta, destino)
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
