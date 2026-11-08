"""Identidad del Job del worker (P2): el verificador, con una API Win32 FALSA e inyectable.

Lo que se prueba acá es la LÓGICA de decisión: qué se acepta y, sobre todo, que todo lo demás falla
CERRADO. La semántica real del sistema operativo (``IsProcessInJob`` contra el handle correcto, la ACL
del Job, jobs anidados) se prueba con procesos reales en ``test_vfs_job_identity_windows.py``; un doble
no reemplaza esa validación nativa.

Propiedades del mecanismo que estos tests fijan:

* el nombre del Job es inadivinable y de formato cerrado (no se le pasa al SO un string arbitrario);
* la pertenencia se pregunta contra el handle del Job ABIERTO por nombre, nunca contra ``NULL``;
* cualquier falla —abrir, consultar, límites, descendientes— es un ``JobIdentityError``, jamás una prueba;
* el handle se cierra SIEMPRE: ``KILL_ON_JOB_CLOSE`` solo dispara cuando se cierra el ÚLTIMO handle, así
  que un worker que conservara el suyo impediría que el cierre del Job matara el árbol.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Sequence

import psutil
import pytest

from sky_claw.local.mo2 import vfs_job_identity as identidad
from sky_claw.local.mo2.vfs_job_identity import (
    JobIdentityError,
    PruebaDeJob,
    generar_nombre_de_job,
    terminar_descendientes_vivos,
    validar_nombre_de_job,
    verificar_identidad_del_job,
)

KILL_ON_JOB_CLOSE = 0x2000
BREAKAWAY_OK = 0x0800
SILENT_BREAKAWAY_OK = 0x1000
NOMBRE = "Local\\SkyClawVfs-" + "0123456789abcdef" * 2
HANDLE = 4242


class _ApiFalsa:
    """API Win32 en memoria con registro de llamadas y fallas inyectables."""

    def __init__(
        self,
        *,
        pertenece: bool = True,
        banderas: int = KILL_ON_JOB_CLOSE,
        totales: int = 2,
        activos: int = 2,
        descendientes: Sequence[int] = (),
        fuera_del_job: Sequence[int] = (),
        murieron: Sequence[int] = (),
        no_se_dejan_matar: Sequence[int] = (),
        error_al_terminar: Sequence[int] = (),
        mueren_aunque_falle: bool = False,
        falla_en: str | None = None,
        excepcion: Exception | None = None,
        tambien_falla_en: str | None = None,
    ) -> None:
        self.pertenece = pertenece
        self.banderas = banderas
        self.totales = totales
        self.activos = activos
        self._vivos = list(descendientes)
        self.fuera_del_job = set(fuera_del_job)
        self.murieron = set(murieron)
        self.no_se_dejan_matar = set(no_se_dejan_matar)
        # PIDs para los que ``terminar_proceso`` LANZA (p. ej. ACCESS_DENIED); los demás se terminan con normalidad.
        self.error_al_terminar = set(error_al_terminar)
        # Si es True, el proceso con error igual deja de estar vivo (murió mientras se esperaba su muerte).
        self.mueren_aunque_falle = mueren_aunque_falle
        self.falla_en = falla_en
        self.tambien_falla_en = (
            tambien_falla_en  # un SEGUNDO punto de falla: fallos que ocurren juntos (verificar + cerrar)
        )
        self.excepcion = excepcion if excepcion is not None else JobIdentityError(f"falla inyectada en {falla_en}")
        self.abiertos: list[int] = []
        self.cerrados: list[int] = []
        self.consultas_de_pertenencia: list[int] = []
        self.terminados: list[int] = []

    def _fallar(self, donde: str) -> None:
        if donde in (self.falla_en, self.tambien_falla_en):
            raise self.excepcion

    def abrir_job(self, nombre: str) -> int:
        self._fallar("abrir_job")
        assert nombre == NOMBRE
        self.abiertos.append(HANDLE)
        return HANDLE

    def proceso_actual_en_job(self, job: int) -> bool:
        self._fallar("proceso_actual_en_job")
        self.consultas_de_pertenencia.append(job)
        return self.pertenece

    def banderas_de_limite(self, job: int) -> int:
        self._fallar("banderas_de_limite")
        return self.banderas

    def contabilidad(self, job: int) -> tuple[int, int]:
        self._fallar("contabilidad")
        return self.totales, self.activos

    def descendientes_vivos(self) -> Sequence[int]:
        self._fallar("descendientes_vivos")
        return tuple(self._vivos)

    def proceso_en_job(self, pid: int, job: int) -> bool | None:
        self._fallar("proceso_en_job")
        self.consultas_de_pertenencia.append(job)
        if pid in self.murieron:
            return None
        return pid not in self.fuera_del_job

    def terminar_proceso(self, pid: int) -> None:
        self._fallar("terminar_proceso")
        if pid in self.error_al_terminar:
            if self.mueren_aunque_falle:
                self._vivos = [vivo for vivo in self._vivos if vivo != pid]
            raise self.excepcion
        self.terminados.append(pid)
        if pid not in self.no_se_dejan_matar:
            self._vivos = [vivo for vivo in self._vivos if vivo != pid]

    def cerrar(self, handle: int) -> None:
        self.cerrados.append(handle)  # se registra el INTENTO: un cierre que falla igual se intentó
        self._fallar("cerrar")


# ---------------------------------------------------------------------------
# 1. El nombre: formato cerrado e inadivinable
# ---------------------------------------------------------------------------


def test_un_nombre_bien_formado_se_acepta_tal_cual() -> None:
    # Act / Assert
    assert validar_nombre_de_job(NOMBRE) == NOMBRE


@pytest.mark.parametrize(
    "malo",
    [
        "",
        "Global\\SkyClawVfs-" + "0" * 32,  # otro espacio de nombres: visible entre sesiones
        "SkyClawVfs-" + "0" * 32,  # sin prefijo de espacio de nombres
        "Local\\SkyClawVfs-" + "0" * 31,  # corto
        "Local\\SkyClawVfs-" + "0" * 33,  # largo
        "Local\\SkyClawVfs-" + "A" * 32,  # mayúsculas: no es lo que genera el broker
        "Local\\SkyClawVfs-" + "g" * 32,  # no hexadecimal
        "Local\\SkyClawVfs-" + "0" * 32 + "\n",  # `$` de una regex lo aceptaría
        "Local\\SkyClawVfs-" + "0" * 32 + "\\..\\x",  # intento de salirse del espacio de nombres
        "Local\\SkyClawVfs-" + "0" * 32 + "\x00",
        " Local\\SkyClawVfs-" + "0" * 32,
        None,
        42,
        b"Local\\SkyClawVfs-" + b"0" * 32,
    ],
)
def test_un_nombre_mal_formado_se_rechaza_antes_de_llegar_al_sistema_operativo(malo: object) -> None:
    # Act / Assert
    with pytest.raises(JobIdentityError):
        validar_nombre_de_job(malo)


def test_los_nombres_generados_son_validos_y_no_se_repiten() -> None:
    # Act
    nombres = {generar_nombre_de_job() for _ in range(2000)}
    # Assert: 128 bits de azar → sin colisiones, y todos pasan el mismo validador que usan bridge y worker.
    assert len(nombres) == 2000
    assert all(validar_nombre_de_job(nombre) == nombre for nombre in nombres)


# ---------------------------------------------------------------------------
# 2. Qué se acepta
# ---------------------------------------------------------------------------


def test_un_worker_dentro_del_job_correcto_con_limites_seguros_se_acepta() -> None:
    # Arrange
    api = _ApiFalsa(totales=2, activos=2)
    # Act
    prueba = verificar_identidad_del_job(NOMBRE, api=api)
    # Assert
    assert isinstance(prueba, PruebaDeJob)
    assert prueba.nombre == NOMBRE
    assert (prueba.procesos_totales, prueba.procesos_activos) == (2, 2)


def test_la_pertenencia_se_pregunta_contra_el_handle_abierto_nunca_contra_null() -> None:
    # Arrange
    api = _ApiFalsa(descendientes=(77,))
    # Act
    verificar_identidad_del_job(NOMBRE, api=api)
    # Assert: IsProcessInJob(p, NULL) responde TRUE para CUALQUIER job; no prueba identidad (#856 §4.1).
    assert api.consultas_de_pertenencia == [HANDLE, HANDLE]
    assert 0 not in api.consultas_de_pertenencia


def test_un_descendiente_vivo_dentro_del_job_no_es_problema() -> None:
    # Arrange
    api = _ApiFalsa(descendientes=(11, 12))
    # Act / Assert
    assert verificar_identidad_del_job(NOMBRE, api=api).nombre == NOMBRE


def test_un_descendiente_que_murio_durante_el_barrido_no_es_problema() -> None:
    # Arrange: el proceso 11 terminó entre la enumeración y la consulta (``None``).
    api = _ApiFalsa(descendientes=(11,), murieron=(11,))
    # Act / Assert
    assert verificar_identidad_del_job(NOMBRE, api=api).nombre == NOMBRE


# ---------------------------------------------------------------------------
# 3. Todo lo demás falla CERRADO
# ---------------------------------------------------------------------------


def test_si_el_job_no_existe_o_no_se_puede_abrir_falla_cerrado() -> None:
    # Arrange: un bridge viejo (Job anónimo) o un nombre equivocado → OpenJobObjectW falla.
    api = _ApiFalsa(falla_en="abrir_job")
    # Act / Assert
    with pytest.raises(JobIdentityError):
        verificar_identidad_del_job(NOMBRE, api=api)
    assert api.abiertos == [] and api.cerrados == []


def test_un_worker_fuera_del_job_falla_cerrado() -> None:
    # Arrange: el hijo Python nació ANTES del assign del bridge → no pertenece al Job.
    api = _ApiFalsa(pertenece=False)
    # Act / Assert
    with pytest.raises(JobIdentityError, match="no pertenece"):
        verificar_identidad_del_job(NOMBRE, api=api)


@pytest.mark.parametrize(
    "donde",
    ["abrir_job", "proceso_actual_en_job", "banderas_de_limite", "contabilidad", "descendientes_vivos", "cerrar"],
)
@pytest.mark.parametrize("excepcion", [None, OSError("Win32"), RuntimeError("inesperado"), ValueError("raro")])
def test_cualquier_falla_al_consultar_es_un_error_de_identidad_nunca_una_prueba(
    donde: str, excepcion: Exception | None
) -> None:
    # Arrange: `abrir_job` es la primera frontera (su `except Exception` propio) y las otras cuatro, la segunda.
    api = _ApiFalsa(falla_en=donde, excepcion=excepcion)
    # Act / Assert: ni siquiera una excepción inesperada puede salir como éxito ni como otro tipo de error.
    with pytest.raises(JobIdentityError):
        verificar_identidad_del_job(NOMBRE, api=api)


@pytest.mark.parametrize("excepcion", [None, OSError("Win32"), RuntimeError("inesperado"), ValueError("raro")])
def test_si_no_se_pudo_abrir_el_job_no_hay_handle_que_cerrar_ni_otro_error_que_el_de_identidad(
    excepcion: Exception | None,
) -> None:
    """Revisión: se sospechó de un ``UnboundLocalError`` del ``finally`` si ``abrir_job`` fallaba con otro tipo de error.

    El ``finally`` solo rodea a la comprobación, que se alcanza con el handle ya asignado; si abrir falla se sale antes.
    """
    # Arrange
    api = _ApiFalsa(falla_en="abrir_job", excepcion=excepcion)
    # Act / Assert: exactamente el error de identidad (un `UnboundLocalError` no es subclase y rompería esta aserción).
    with pytest.raises(JobIdentityError):
        verificar_identidad_del_job(NOMBRE, api=api)
    # Nada se abrió ⇒ nada se cierra (cerrar un handle inexistente sería cerrar uno ajeno).
    assert api.abiertos == []
    assert api.cerrados == []


@pytest.mark.parametrize("excepcion", [OSError("kernel32"), AttributeError("OpenJobObjectW"), RuntimeError("raro")])
@pytest.mark.parametrize(
    "consumidor",
    [
        pytest.param(lambda: verificar_identidad_del_job(NOMBRE), id="verificar_identidad_del_job"),
        pytest.param(lambda: terminar_descendientes_vivos(pausa_s=0.0), id="terminar_descendientes_vivos"),
    ],
)
def test_si_la_api_por_defecto_no_se_puede_construir_el_error_es_de_identidad_y_no_uno_crudo(
    monkeypatch: pytest.MonkeyPatch, consumidor: object, excepcion: Exception
) -> None:
    """Revisión: construir ``_ApiWin32`` (``ctypes.WinDLL`` y ``argtypes``) puede fallar con ``OSError``/``AttributeError``.

    ``worker_main`` solo captura ``JobIdentityError``: un error crudo se salía de ahí sin código 72, sin barrer a los
    descendientes y sin cerrar el logging. Los dos consumidores de la API por defecto tienen que cumplir lo mismo.
    """

    # Arrange
    def _no_se_puede_construir() -> object:
        raise excepcion

    monkeypatch.setattr(identidad, "_api_por_defecto", _no_se_puede_construir)
    # Act / Assert: exactamente el error de identidad (un `OSError` crudo no es subclase y rompería esta aserción).
    with pytest.raises(JobIdentityError):
        consumidor()  # type: ignore[operator]


@pytest.mark.parametrize("excepcion", [None, OSError("CloseHandle"), RuntimeError("raro")])
def test_si_la_verificacion_falla_y_el_cierre_tambien_se_conserva_la_causa_real_y_el_cierre_se_registra(
    caplog: pytest.LogCaptureFixture, excepcion: Exception | None
) -> None:
    """Revisión: el error del ``finally`` reemplazaba al de la verificación y el log solo veía «no se pudo cerrar».

    Con dos fallos, el que viaja hacia ``worker_main`` es la CAUSA («no pertenece al Job…»); el fallo de cierre queda
    registrado aparte. Es solo diagnóstico: el resultado sigue siendo fail-closed en ambos casos.
    """
    # Arrange: el worker NO está en el Job y, además, cerrar el handle falla.
    api = _ApiFalsa(pertenece=False, falla_en="cerrar", excepcion=excepcion)
    # Act / Assert
    with (
        caplog.at_level(logging.ERROR, logger=identidad.__name__),
        pytest.raises(JobIdentityError, match="no pertenece"),
    ):
        verificar_identidad_del_job(NOMBRE, api=api)
    assert api.cerrados == [HANDLE], "el cierre se intentó igual"
    assert any("cerrar el handle" in registro.getMessage() for registro in caplog.records)


@pytest.mark.parametrize(
    "donde", ["proceso_actual_en_job", "banderas_de_limite", "contabilidad", "descendientes_vivos"]
)
def test_si_una_excepcion_inesperada_de_la_verificacion_y_el_cierre_fallan_juntos_se_conserva_la_causa(
    caplog: pytest.LogCaptureFixture, donde: str
) -> None:
    """Hermana de la anterior: ``verificar_identidad_del_job`` tiene DOS ramas de fallo (error de identidad y error crudo).

    El mismo arreglo vive en las dos; una sola quedó probada y quitarlo de la otra no rompía nada (mutante sobreviviente).
    """
    # Arrange: la API lanza un error CRUDO al verificar y, además, al cerrar.
    api = _ApiFalsa(falla_en=donde, tambien_falla_en="cerrar", excepcion=RuntimeError("inesperado"))
    # Act / Assert: viaja la causa de la verificación, envuelta; el cierre fallido solo se registra.
    with (
        caplog.at_level(logging.ERROR, logger=identidad.__name__),
        pytest.raises(JobIdentityError, match="falló la verificación"),
    ):
        verificar_identidad_del_job(NOMBRE, api=api)
    assert api.cerrados == [HANDLE], "el cierre se intentó igual"
    assert any("cerrar el handle" in registro.getMessage() for registro in caplog.records)


def test_si_la_verificacion_tiene_exito_pero_el_cierre_falla_no_hay_prueba() -> None:
    # Arrange: control — sin un fallo previo, un handle que no se cierra SÍ invalida la prueba
    # (``KILL_ON_JOB_CLOSE`` solo dispara al cerrarse el último handle).
    api = _ApiFalsa(falla_en="cerrar")
    # Act / Assert
    with pytest.raises(JobIdentityError, match="cerrar"):
        verificar_identidad_del_job(NOMBRE, api=api)


@pytest.mark.parametrize(
    "excepcion", [OSError("psutil"), RuntimeError("raro"), psutil.AccessDenied(pid=1), JobIdentityError("propio")]
)
@pytest.mark.parametrize("donde", ["descendientes_vivos", "terminar_proceso"])
def test_la_limpieza_de_descendientes_tambien_falla_cerrado_con_el_error_de_identidad(
    donde: str, excepcion: Exception
) -> None:
    """Revisión: ``terminar_descendientes_vivos`` llamaba a la API sin envolver; un error crudo se escapaba de ``worker_main``.

    ``psutil`` no garantiza que ``Process().children(...)`` (el ``descendientes_vivos`` real) nunca lance ``psutil.Error``
    (no se reprodujo un fallo real: es un defecto de contrato). La limpieza es el gemelo de la verificación y cumple el
    mismo contrato del módulo: toda falla es un ``JobIdentityError``.
    """
    # Arrange: hay un descendiente vivo, para que la limpieza llegue a llamar a `terminar_proceso`.
    api = _ApiFalsa(descendientes=(11,), falla_en=donde, excepcion=excepcion)
    # Act / Assert
    with pytest.raises(JobIdentityError):
        terminar_descendientes_vivos(api=api, pausa_s=0.0)


def test_un_error_al_preguntar_por_un_descendiente_falla_cerrado() -> None:
    # Arrange
    api = _ApiFalsa(descendientes=(11,), falla_en="proceso_en_job")
    # Act / Assert
    with pytest.raises(JobIdentityError):
        verificar_identidad_del_job(NOMBRE, api=api)


@pytest.mark.parametrize(
    "banderas",
    [0, BREAKAWAY_OK, SILENT_BREAKAWAY_OK, KILL_ON_JOB_CLOSE | BREAKAWAY_OK, KILL_ON_JOB_CLOSE | SILENT_BREAKAWAY_OK],
)
def test_un_job_sin_kill_on_close_o_con_breakaway_no_contiene_y_falla_cerrado(banderas: int) -> None:
    # Arrange: sin KILL_ON_JOB_CLOSE cerrar el Job no mata nada; con BREAKAWAY un descendiente puede salirse.
    api = _ApiFalsa(banderas=banderas)
    # Act / Assert
    with pytest.raises(JobIdentityError):
        verificar_identidad_del_job(NOMBRE, api=api)


def test_un_descendiente_vivo_fuera_del_job_falla_cerrado_y_se_informa_cual() -> None:
    # Arrange: el pid 77 nació antes del assign y sigue vivo fuera del Job.
    api = _ApiFalsa(descendientes=(11, 77), fuera_del_job=(77,))
    # Act / Assert
    with pytest.raises(JobIdentityError, match="77"):
        verificar_identidad_del_job(NOMBRE, api=api)


@pytest.mark.parametrize("nombre", ["Global\\SkyClawVfs-" + "0" * 32, "", None])
def test_un_nombre_invalido_falla_antes_de_tocar_la_api(nombre: object) -> None:
    # Arrange
    api = _ApiFalsa()
    # Act / Assert
    with pytest.raises(JobIdentityError):
        verificar_identidad_del_job(nombre, api=api)
    assert api.abiertos == []


# ---------------------------------------------------------------------------
# 4. El handle se cierra SIEMPRE (KILL_ON_JOB_CLOSE solo dispara con el último handle)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "api",
    [
        _ApiFalsa(),  # éxito
        _ApiFalsa(pertenece=False),
        _ApiFalsa(banderas=0),
        _ApiFalsa(falla_en="proceso_actual_en_job"),
        _ApiFalsa(falla_en="banderas_de_limite"),
        _ApiFalsa(falla_en="contabilidad"),
        _ApiFalsa(falla_en="descendientes_vivos"),
        _ApiFalsa(descendientes=(77,), fuera_del_job=(77,)),
        _ApiFalsa(descendientes=(77,), falla_en="proceso_en_job", excepcion=RuntimeError("x")),
    ],
    ids=lambda a: f"pert={a.pertenece},banderas={a.banderas:#x},falla={a.falla_en},fuera={sorted(a.fuera_del_job)}",
)
def test_el_handle_del_job_se_cierra_en_exito_y_en_cualquier_falla(api: _ApiFalsa) -> None:
    # Act
    with contextlib.suppress(JobIdentityError):
        verificar_identidad_del_job(NOMBRE, api=api)
    # Assert: todo lo abierto quedó cerrado; si el worker conservara el handle, el cierre del Job del bridge no mataría el árbol.
    assert api.abiertos == [HANDLE]
    assert api.cerrados == [HANDLE]


# ---------------------------------------------------------------------------
# 5. Terminación de lo no autorizado
# ---------------------------------------------------------------------------


def test_terminar_descendientes_mata_a_todos_los_vivos_y_devuelve_cuales() -> None:
    # Arrange
    api = _ApiFalsa(descendientes=(11, 12, 13))
    # Act
    terminados = terminar_descendientes_vivos(api=api)
    # Assert
    assert sorted(terminados) == [11, 12, 13]
    assert api.descendientes_vivos() == ()


def test_terminar_descendientes_sin_nada_vivo_no_hace_nada() -> None:
    # Arrange
    api = _ApiFalsa()
    # Act / Assert
    assert terminar_descendientes_vivos(api=api) == ()
    assert api.terminados == []


def test_si_un_descendiente_no_muere_el_limpiador_falla_en_vez_de_afirmar_que_limpio() -> None:
    # Arrange: el proceso 13 ignora la terminación.
    api = _ApiFalsa(descendientes=(11, 13), no_se_dejan_matar=(13,))
    # Act / Assert: no se puede afirmar «sin residuos» cuando queda uno vivo.
    with pytest.raises(JobIdentityError, match="13"):
        terminar_descendientes_vivos(api=api)


def test_un_descendiente_que_ya_murio_al_terminarlo_no_es_un_error() -> None:
    # Arrange: terminar_proceso encuentra al proceso ya muerto (carrera benigna) y no falla.
    api = _ApiFalsa(descendientes=(11,))
    # Act / Assert
    assert terminar_descendientes_vivos(api=api) == (11,)


@pytest.mark.parametrize("excepcion", [JobIdentityError("denegado"), OSError("kernel32"), psutil.AccessDenied(pid=1)])
@pytest.mark.parametrize("fallido", [11, 12, 13], ids=["el_primero", "el_del_medio", "el_ultimo"])
def test_un_descendiente_que_no_se_puede_terminar_no_impide_intentar_con_los_demas(
    fallido: int, excepcion: Exception
) -> None:
    """Auditoría independiente H-03: un error en UN pid abortaba el bucle y dejaba sin intentar a los demás.

    Es la propiedad del mecanismo: la limpieza de lo no autorizado es por-PID. Que un proceso se niegue a morir
    (``ACCESS_DENIED`` sobre uno de mayor integridad, o una espera vencida) no puede salvar a sus hermanos. Se prueba
    con el que falla en cada posición de la lista (el informe describía el primero, pero el defecto era de cualquiera).
    """
    # Arrange
    api = _ApiFalsa(descendientes=(11, 12, 13), error_al_terminar=(fallido,), excepcion=excepcion)
    otros = {11, 12, 13} - {fallido}
    # Act / Assert: la limpieza falla (queda uno vivo) y NOMBRA al que no murió…
    with pytest.raises(JobIdentityError, match=str(fallido)):
        terminar_descendientes_vivos(api=api, pausa_s=0.0)
    # …pero los otros dos SÍ se terminaron: el error de uno no abortó el barrido.
    assert otros <= set(api.terminados)
    assert sorted(api.descendientes_vivos()) == [fallido]


def test_el_error_por_pid_viaja_en_el_mensaje_de_la_limpieza_fallida() -> None:
    # Arrange: el diagnóstico de por qué no murió no se pierde (el log CRITICAL del worker es lo único que queda).
    api = _ApiFalsa(descendientes=(11, 12), error_al_terminar=(11,), excepcion=JobIdentityError("acceso denegado"))
    # Act / Assert
    with pytest.raises(JobIdentityError, match=r"11.*acceso denegado"):
        terminar_descendientes_vivos(api=api, pausa_s=0.0)


def test_un_descendiente_que_falla_al_terminarse_pero_ya_no_esta_vivo_no_cuenta_como_residuo() -> None:
    # Arrange: terminar_proceso lanza (espera vencida) pero el proceso murió mientras tanto: no hay residuo que reportar.
    api = _ApiFalsa(descendientes=(11, 12), error_al_terminar=(11,), mueren_aunque_falle=True)
    # Act / Assert: no se afirma limpieza que no ocurrió, ni se falla por algo que ya no está vivo.
    assert sorted(terminar_descendientes_vivos(api=api, pausa_s=0.0)) == [11, 12]


def test_un_fallo_persistente_en_un_pid_no_impide_que_los_demas_mueran_en_rondas_posteriores() -> None:
    # Arrange: 11 siempre falla; 12 y 13 mueren en la primera ronda, así que el resto de las rondas solo ve a 11.
    api = _ApiFalsa(descendientes=(11, 12, 13), error_al_terminar=(11,))
    # Act
    with pytest.raises(JobIdentityError):
        terminar_descendientes_vivos(api=api, intentos=3, pausa_s=0.0)
    # Assert: 12 y 13 se terminaron UNA sola vez (no se reintenta lo que ya murió); 11 no figura entre los terminados.
    assert api.terminados.count(12) == 1
    assert api.terminados.count(13) == 1
    assert 11 not in api.terminados


# ---------------------------------------------------------------------------
# 6. Sin API de Windows no hay forma de probar identidad: falla cerrado
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="en Windows la API por defecto es la real")
def test_fuera_de_windows_el_verificador_por_defecto_falla_cerrado() -> None:
    # Act / Assert
    with pytest.raises(JobIdentityError, match="Windows"):
        verificar_identidad_del_job(NOMBRE)


def test_el_modulo_no_importa_nada_pesado_de_la_aplicacion() -> None:
    """Se importa muy temprano en el worker congelado: solo stdlib y psutil, nada de ``sky_claw.*``."""
    import ast
    import pathlib

    arbol = ast.parse(pathlib.Path(identidad.__file__).read_text(encoding="utf-8"))
    importados = {
        (nodo.module or "") if isinstance(nodo, ast.ImportFrom) else alias.name
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Import | ast.ImportFrom)
        for alias in (nodo.names if isinstance(nodo, ast.Import) else [None])
    }
    assert not {m for m in importados if m == "sky_claw" or m.startswith("sky_claw.")}
