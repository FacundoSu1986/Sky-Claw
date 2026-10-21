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
