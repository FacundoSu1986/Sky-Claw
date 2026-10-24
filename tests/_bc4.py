"""Decodificador y codificador BC4 UNORM INGENUO para tests (no es código de producto).

Sirve para el smoke real del ``texconv`` fijado: Pillow solo decodifica el nivel base de un DDS, y para comprobar
que los mips que devolvió ``texconv`` siguen la cadena propia hay que decodificar TODOS los niveles. La
disposición del bloque es la pública de Microsoft ("Block Compression", BC4): dos extremos de 8 bits y 16 índices
de 3 bits, en orden de filas, empaquetados de menos a más significativo. Con ``r0 > r1`` hay 8 valores (6 interpolados);
si no, 6 valores (4 interpolados) más 0 y 255.

``tests/test_bc4_helper.py`` valida el decodificador contra Pillow (otro decodificador, independiente) en el nivel base.
"""

from __future__ import annotations

import numpy as np

TAMANO_DE_BLOQUE = 8


def paleta(r0: int, r1: int) -> list[float]:
    """Los 8 valores que representan los índices 0..7 de un bloque BC4 UNORM (escala 0..255, en float)."""
    if r0 > r1:
        return [float(r0), float(r1), *((((8 - i) * r0 + (i - 1) * r1) / 7.0) for i in range(2, 8))]
    return [float(r0), float(r1), *((((6 - i) * r0 + (i - 1) * r1) / 5.0) for i in range(2, 6)), 0.0, 255.0]


def decodificar_nivel(datos: bytes, ancho: int, alto: int) -> np.ndarray:
    """Decodifica un nivel BC4 UNORM de ``ancho`` x ``alto`` (recorta el relleno del último bloque)."""
    columnas, filas = max(1, (ancho + 3) // 4), max(1, (alto + 3) // 4)
    if len(datos) != columnas * filas * TAMANO_DE_BLOQUE:
        raise ValueError(f"se esperaban {columnas * filas * TAMANO_DE_BLOQUE} bytes y llegaron {len(datos)}")
    salida = np.zeros((filas * 4, columnas * 4), dtype=np.float64)
    for bloque in range(columnas * filas):
        crudo = datos[bloque * TAMANO_DE_BLOQUE : (bloque + 1) * TAMANO_DE_BLOQUE]
        valores = paleta(crudo[0], crudo[1])
        indices = int.from_bytes(crudo[2:], "little")
        fila0, columna0 = (bloque // columnas) * 4, (bloque % columnas) * 4
        for pixel in range(16):
            salida[fila0 + pixel // 4, columna0 + pixel % 4] = valores[(indices >> (3 * pixel)) & 7]
    return salida[:alto, :ancho]


def codificar_nivel_ingenuo(nivel: np.ndarray) -> bytes:
    """Codifica un nivel uint8 con el método más simple: extremos = mínimo y máximo del bloque, índice más cercano.

    El relleno de los bloques parciales replica el borde. NO es un buen codificador; sirve para tener un BC4 de
    referencia independiente del ``texconv`` y para acotar el error que cabe esperar de uno razonable.
    """
    alto, ancho = nivel.shape
    columnas, filas = max(1, (ancho + 3) // 4), max(1, (alto + 3) // 4)
    relleno = np.pad(nivel.astype(np.int64), ((0, filas * 4 - alto), (0, columnas * 4 - ancho)), mode="edge")
    bloques = bytearray()
    for fila in range(filas):
        for columna in range(columnas):
            bloque = relleno[fila * 4 : fila * 4 + 4, columna * 4 : columna * 4 + 4]
            bajo, alto_ = int(bloque.min()), int(bloque.max())
            r0, r1 = (alto_, bajo) if alto_ > bajo else (alto_, alto_)
            valores = np.array(paleta(r0, r1))
            indices = 0
            for pixel, valor in enumerate(bloque.reshape(16)):
                indices |= int(np.abs(valores - valor).argmin()) << (3 * pixel)
            bloques += bytes([r0, r1]) + indices.to_bytes(6, "little")
    return bytes(bloques)


def partir_niveles(carga: bytes, ancho: int, alto: int, niveles: int) -> list[bytes]:
    """Parte la carga de un DDS BC4 en sus niveles (el nivel ``i`` mide ``max(1, lado >> i)`` por lado)."""
    partes, inicio = [], 0
    for i in range(niveles):
        columnas, filas = max(1, (max(1, ancho >> i) + 3) // 4), max(1, (max(1, alto >> i) + 3) // 4)
        fin = inicio + columnas * filas * TAMANO_DE_BLOQUE
        partes.append(carga[inicio:fin])
        inicio = fin
    if inicio != len(carga):
        raise ValueError(f"la carga mide {len(carga)} bytes y los {niveles} niveles suman {inicio}")
    return partes


# ---------------------------------------------------------------------------
# Oráculo de mips: ¿esta cadena sigue la cadena propia (promedio de área) o usó otro filtro?
# ---------------------------------------------------------------------------


def campo_sintetico(lado_alto: int, lado_ancho: int, semilla: int = 11) -> np.ndarray:
    """Altura sintética con relieve a varias escalas (ondas suaves más ruido suavizado), en [0, 1]."""
    rng = np.random.default_rng(semilla)
    y, x = np.mgrid[0:lado_alto, 0:lado_ancho] / np.array([[[lado_alto]], [[lado_ancho]]])
    ruido = rng.random((lado_alto, lado_ancho))
    nucleo = np.ones(5) / 5
    for eje in (0, 1):  # promedio móvil circular de 5: conserva la longitud aun en lados menores que el núcleo
        ruido = np.apply_along_axis(lambda v: np.convolve(np.pad(v, 2, mode="wrap"), nucleo, mode="valid"), eje, ruido)
    return np.clip(0.5 + 0.18 * np.sin(6 * np.pi * x) * np.cos(4 * np.pi * y) + 0.25 * (ruido - 0.5), 0.0, 1.0)


def limites_de_tolerancia(esperado: np.ndarray) -> tuple[float, float]:
    """(error medio, error máximo) admisibles para un nivel: el doble del error de un codificador ingenuo más un margen.

    PROVISIONAL: calibrado contra un codificador ingenuo porque no hay un ``texconv`` real en CI. Si un codificador
    bueno quedara por encima, el dato manda. ``tests/test_bc4_helper.py`` comprueba que el oráculo SIGUE distinguiendo
    los filtros equivocados, para que aflojar este número no pase inadvertido.
    """
    alto, ancho = esperado.shape
    ingenuo = np.abs(decodificar_nivel(codificar_nivel_ingenuo(esperado), ancho, alto) - esperado.astype(np.float64))
    return 2.0 * float(ingenuo.mean()) + 1.0, 2.0 * float(ingenuo.max()) + 3.0


def dentro_de_tolerancia(obtenido: np.ndarray, esperado: np.ndarray) -> bool:
    error = np.abs(obtenido.astype(np.float64) - esperado.astype(np.float64))
    limite_medio, limite_maximo = limites_de_tolerancia(esperado)
    return bool(error.mean() <= limite_medio and error.max() <= limite_maximo)


def _reducir(actual: np.ndarray) -> np.ndarray:
    if actual.shape[0] > 1:
        actual = (actual[0::2, :] + actual[1::2, :]) / 2.0
    if actual.shape[1] > 1:
        actual = (actual[:, 0::2] + actual[:, 1::2]) / 2.0
    return actual


def cadena_con_filtro_point(base: np.ndarray) -> list[np.ndarray]:
    """Mips por submuestreo (filtro POINT): descarta información en vez de promediarla."""
    niveles = max(base.shape).bit_length()
    return [base[:: 2**k, :: 2**k] for k in range(niveles)]


def cadena_con_curva_srgb(base: np.ndarray) -> list[np.ndarray]:
    """Mips promediados en espacio lineal tratando el byte como sRGB: el error clásico de filtrar una altura."""
    a_lineal = lambda c: np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)  # noqa: E731
    a_srgb = lambda lin: np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * np.power(np.maximum(lin, 0), 1 / 2.4) - 0.055)  # noqa: E731
    actual = a_lineal(base / 255.0)
    niveles = [base]
    for _ in range(max(base.shape).bit_length() - 1):
        actual = _reducir(actual)
        niveles.append(np.rint(a_srgb(actual) * 255.0).astype(np.uint8))
    return niveles


def cadena_box_con_redondeo_a_8_bits(base: np.ndarray) -> list[np.ndarray]:
    """Mips por promedio de área REDONDEANDO a 8 bits en cada nivel: lo que daría ``texconv`` si regenerara la cadena."""
    actual = base.astype(np.float64)
    niveles = [base]
    for _ in range(max(base.shape).bit_length() - 1):
        actual = np.rint(_reducir(actual))
        niveles.append(actual.astype(np.uint8))
    return niveles
