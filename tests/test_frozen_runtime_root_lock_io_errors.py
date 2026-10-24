"""Tests focales de inyección de OSError dentro de _write_metadata_crash_safe.

Cubre el contrato de error de P4-S2: un fallo de I/O (write/flush/fsync/truncate)
se traduce a ``FrozenRuntimeLockError`` cuyo ``__cause__`` es la MISMA instancia
de ``OSError`` inyectada —identidad, no sólo clase— preservando ``errno``.

Anclas que estos tests sostienen (hallazgos P2-1/P2-2/P2-3 del PR #757):

* **Descriptor real.** El file object siempre envuelve un archivo abierto, así
  que ``fileno()`` devuelve un descriptor de archivo y no un entero ficticio.
  Con ``fileno() -> 1`` el primer ``os.fsync`` dependía de qué fuera fd 1: bajo
  ``pytest -s`` sobre una consola Windows ``os.fsync(1)`` levanta
  ``OSError(EBADF)``, la excepción se traducía igual y el caso ``truncate``
  pasaba SIN ejecutar ``truncate``. Por eso acá se afirma el orden exacto de
  las llamadas y no sólo el tipo de la excepción.
* **Segunda invocación.** El helper hace ``flush``/``fsync`` otra vez después
  de truncar. Fallar siempre la PRIMERA deja esa rama sin cubrir: mover o
  borrar esas dos llamadas deja la suite en verde. Los casos
  ``segundo-flush-tras-truncar`` y ``segundo-fsync-tras-truncar`` fallan la
  segunda y exigen que el error se traduzca.
* **Identidad de la causa.** ``isinstance(__cause__, OSError)`` lo satisface una
  implementación que descarte la excepción capturada y encadene una nueva. Se
  exige ``__cause__ is error`` y se conserva ``errno``.

``os.fsync`` se parchea con ``monkeypatch`` (alcance de test, restauración
garantizada incluso si el cuerpo falla) en lugar de reasignar el global a mano,
y el último test del archivo verifica que no quede residuo para los siguientes.
"""

from __future__ import annotations

import errno
import json
import os
from collections.abc import Iterator
from typing import IO, Any

import pytest

from sky_claw.local.frozen_runtime.errors import FrozenRuntimeLockError
from sky_claw.local.frozen_runtime.root_lock import _write_metadata_crash_safe

# Identidad de os.fsync al importar el módulo. Se usa para detectar residuos de
# parcheo que contaminarían los tests posteriores de la suite.
_FSYNC_ORIGINAL = os.fsync

# Tamaño previo del archivo. Mayor que la serialización del payload de prueba,
# para forzar la rama que trunca (``old_size > len(serialized)``).
_TAMANO_PREVIO = 4096

_PAYLOAD: dict[str, Any] = {
    "schema_version": 1,
    "canonical_root": "/tmp/test-root",
    "session_id": "00000000-0000-0000-0000-000000000001",
    "pid": 1234,
    "process_create_time": 1000.0,
    "acquired_at": 2000.0,
    "phase": "HELD",
}


class _ArchivoInstrumentado:
    """Envoltorio de un archivo real que cuenta llamadas e inyecta un OSError.

    Delega TODO en el archivo real, ``fileno()`` incluido: el descriptor que ve
    ``_write_metadata_crash_safe`` es el del archivo abierto, así que el
    ``os.fsync`` intermedio se comporta igual que en producción.
    """

    def __init__(
        self,
        real: IO[bytes],
        *,
        punto_de_fallo: str | None = None,
        en_llamada: int = 1,
        error: OSError | None = None,
    ) -> None:
        self._real = real
        self._punto = punto_de_fallo
        self._en_llamada = en_llamada
        self._error = error
        self.llamadas: dict[str, int] = {"write": 0, "flush": 0, "truncate": 0}
        self.orden: list[str] = []

    def _registrar(self, nombre: str) -> None:
        self.llamadas[nombre] += 1
        self.orden.append(nombre)
        if self._punto == nombre and self.llamadas[nombre] == self._en_llamada:
            if self._error is None:
                raise AssertionError("un fallo inyectado exige una instancia de OSError")
            raise self._error

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._real.seek(offset, whence)

    def tell(self) -> int:
        return self._real.tell()

    def write(self, data: bytes) -> int:
        self._registrar("write")
        return self._real.write(data)

    def flush(self) -> None:
        self._registrar("flush")
        self._real.flush()

    def fileno(self) -> int:
        return self._real.fileno()

    def truncate(self, size: int | None = None) -> int:
        self._registrar("truncate")
        return self._real.truncate(size)


def _parchear_fsync(
    monkeypatch: pytest.MonkeyPatch,
    *,
    falla_en: int | None = None,
    error: OSError | None = None,
) -> list[int]:
    """Parchea ``os.fsync`` con un contador que puede fallar en la N-ésima llamada.

    Devuelve la lista de descriptores observados (se llena durante la llamada).
    El parche lo retira ``monkeypatch`` al terminar el test, incluso si el test
    falla, así que no contamina a los siguientes.
    """
    descriptores: list[int] = []
    real_fsync = os.fsync

    def fsync_contador(fd: int) -> None:
        descriptores.append(fd)
        if falla_en is not None and len(descriptores) == falla_en:
            if error is None:
                raise AssertionError("un fallo inyectado exige una instancia de OSError")
            raise error
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync_contador)
    return descriptores


@pytest.fixture
def archivo_paddeado(tmp_path: Any) -> Iterator[IO[bytes]]:
    """Archivo real pre-rellenado para que el payload nuevo sea más corto."""
    ruta = tmp_path / "metadata.lock"
    ruta.write_bytes(b" " * _TAMANO_PREVIO)
    with open(ruta, "r+b") as archivo:
        yield archivo


def test_el_descriptor_es_el_del_archivo_real_y_es_durable(archivo_paddeado: IO[bytes]) -> None:
    """P2-1: el descriptor sale del archivo, no de un entero ficticio.

    ``os.fsync`` sobre ese descriptor tiene que funcionar en cualquier entorno:
    el fallo original era que fd 1 podía ser una consola y no ser fsync-able.
    """
    proxy = _ArchivoInstrumentado(archivo_paddeado)

    assert proxy.fileno() == archivo_paddeado.fileno()
    os.fsync(proxy.fileno())  # no debe levantar: es un archivo, no la consola


@pytest.mark.parametrize(
    ("punto", "en_llamada", "orden_esperado", "fsync_esperados"),
    [
        pytest.param("write", 1, ["write"], 0, id="write"),
        pytest.param("flush", 1, ["write", "flush"], 0, id="primer-flush"),
        pytest.param("fsync", 1, ["write", "flush"], 1, id="primer-fsync"),
        pytest.param("truncate", 1, ["write", "flush", "truncate"], 1, id="truncate"),
        pytest.param(
            "flush",
            2,
            ["write", "flush", "truncate", "flush"],
            1,
            id="segundo-flush-tras-truncar",
        ),
        pytest.param(
            "fsync",
            2,
            ["write", "flush", "truncate", "flush"],
            2,
            id="segundo-fsync-tras-truncar",
        ),
    ],
)
def test_oserror_se_traduce_preservando_la_instancia_original(
    archivo_paddeado: IO[bytes],
    monkeypatch: pytest.MonkeyPatch,
    punto: str,
    en_llamada: int,
    orden_esperado: list[str],
    fsync_esperados: int,
) -> None:
    """Un OSError en cualquier punto de I/O se traduce y conserva su identidad."""
    error = OSError(errno.ENOSPC, "disco lleno simulado")
    proxy = _ArchivoInstrumentado(
        archivo_paddeado,
        punto_de_fallo=punto,
        en_llamada=en_llamada,
        error=error,
    )
    descriptores = _parchear_fsync(
        monkeypatch,
        falla_en=en_llamada if punto == "fsync" else None,
        error=error,
    )

    with pytest.raises(FrozenRuntimeLockError) as exc_info:
        _write_metadata_crash_safe(proxy, _PAYLOAD)

    # P2-3: identidad, no sólo clase. Un `except OSError: ... from OSError(str(exc))`
    # construiría una instancia nueva y perdería errno y el contexto original.
    assert exc_info.value.__cause__ is error
    assert exc_info.value.__cause__.errno == errno.ENOSPC
    assert "no se pudo persistir la metadata del root lock" in str(exc_info.value)

    # P2-1/P2-2: evidencia POSITIVA de qué se ejecutó realmente y en qué orden.
    assert proxy.orden == orden_esperado, f"orden observado: {proxy.orden}"
    assert len(descriptores) == fsync_esperados
    assert all(fd == archivo_paddeado.fileno() for fd in descriptores)


def test_exito_trunca_al_tamano_exacto_y_es_durable(
    archivo_paddeado: IO[bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Camino feliz: escribe, trunca al tamaño exacto y fsyncea dos veces."""
    proxy = _ArchivoInstrumentado(archivo_paddeado)
    descriptores = _parchear_fsync(monkeypatch)

    _write_metadata_crash_safe(proxy, _PAYLOAD)

    assert proxy.orden == ["write", "flush", "truncate", "flush"]
    assert len(descriptores) == 2

    archivo_paddeado.seek(0)
    contenido = archivo_paddeado.read()

    serializado = json.dumps(_PAYLOAD, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert len(contenido) == len(serializado), "el truncado debe dejar el tamaño exacto"
    assert json.loads(contenido) == _PAYLOAD


def test_el_parche_se_retira_aunque_el_cuerpo_falle() -> None:
    """Aislamiento: la restauración no depende de que el test termine bien."""
    original = os.fsync

    with (
        pytest.raises(RuntimeError, match="fallo deliberado"),
        pytest.MonkeyPatch.context() as monkeypatch,
    ):
        _parchear_fsync(monkeypatch, falla_en=1, error=OSError(errno.EIO, "boom"))
        assert os.fsync is not original
        raise RuntimeError("fallo deliberado")

    assert os.fsync is original


def test_no_queda_residuo_de_parche_para_los_tests_siguientes() -> None:
    """Aislamiento: ningún test anterior dejó ``os.fsync`` reasignado.

    Va último a propósito: con la reasignación global manual del test original
    este assert detecta la contaminación que sobrevive al archivo.
    """
    assert os.fsync is _FSYNC_ORIGINAL, "un test anterior dejó os.fsync parcheado"
