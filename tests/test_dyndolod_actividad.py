"""Actividad del log de la etapa 9: ¿sigue escribiendo la herramienta?

La etapa 9 dura de minutos a decenas de minutos y es ASISTIDA: el operador (a veces
remoto) tiene que cerrar la ventana de cada herramienta. Hasta acá el único pulso era
un ``heartbeat`` que sólo iba al log local, así que un operador que seguía la corrida
por Telegram no tenía forma de saber si la herramienta trabajaba, esperaba una acción
o estaba colgada.

Este módulo no INFIERE fases ni toca el veredicto: reporta hechos observables del
archivo de log (tamaño, mtime, última línea) y decide cuándo vale la pena avisar.

* La lógica es pura y con reloj inyectado: se prueba sin dormir.
* Un aviso de estancamiento se emite UNA vez por episodio (no cada sondeo), y una
  reanudación lo cierra. Si no, un log callado durante una hora serían decenas de
  mensajes.
* Leer la cola del log nunca lanza: un ``sharing violation`` mientras la herramienta
  escribe es lo esperable en Windows, y el observador no puede ser el que rompa la corrida.
"""

from __future__ import annotations

import os
import pathlib
import threading

import pytest

from sky_claw.local.tools import dyndolod_actividad as mod
from sky_claw.local.tools.dyndolod_actividad import (
    AvisoDeActividad,
    ConfiguracionDeObservacion,
    MuestraDeLog,
    RastreadorDeActividad,
    TipoDeAviso,
    tomar_muestra,
)

#: Umbrales distintos entre sí para que ningún test dependa de que dos coincidan.
_INFINITO = 1e9


def _config(*, progreso: float = _INFINITO, estancamiento: float = _INFINITO) -> ConfiguracionDeObservacion:
    """Un solo mecanismo activo por vez salvo que el test pida los dos (aísla lo que se prueba)."""
    return ConfiguracionDeObservacion(
        intervalo_de_sondeo_s=1.0,
        intervalo_de_progreso_s=progreso,
        umbral_de_estancamiento_s=estancamiento,
        max_bytes_de_cola=4096,
    )


def _muestra(
    tamano: int = 1000, mtime: float = 50.0, nombre: str = "DynDOLOD_SSE_log.txt", linea: str = "x"
) -> MuestraDeLog:
    return MuestraDeLog(nombre=nombre, tamano=tamano, mtime=mtime, ultima_linea=linea)


class _Reloj:
    """Aplica una serie de observaciones y junta los avisos con el instante en que salieron."""

    def __init__(self, rastreador: RastreadorDeActividad) -> None:
        self.rastreador = rastreador
        self.avisos: list[tuple[float, AvisoDeActividad]] = []

    def tick(self, muestra: MuestraDeLog | None, ahora: float) -> AvisoDeActividad | None:
        aviso = self.rastreador.observar(muestra, ahora=ahora)
        if aviso is not None:
            self.avisos.append((ahora, aviso))
        return aviso

    def tipos(self) -> list[tuple[float, TipoDeAviso]]:
        return [(t, a.tipo) for t, a in self.avisos]


# ---------------------------------------------------------------------------
# Rastreador: la decisión de cuándo avisar (pura, reloj inyectado)
# ---------------------------------------------------------------------------


class TestProgreso:
    def test_un_log_que_crece_avisa_progreso_cada_intervalo_y_no_antes(self) -> None:
        reloj = _Reloj(RastreadorDeActividad(_config(progreso=100.0), inicio=0.0))

        for t in range(1, 251):
            reloj.tick(_muestra(tamano=1000 + t, mtime=float(t), linea=f"linea {t}"), float(t))

        assert reloj.tipos() == [(100.0, TipoDeAviso.PROGRESO), (200.0, TipoDeAviso.PROGRESO)]
        primero = reloj.avisos[0][1]
        assert primero.segundos_en_curso == 100.0
        assert primero.segundos_sin_actividad == 0.0
        assert primero.tamano_del_log == 1100
        assert primero.ultima_linea == "linea 100"
        assert primero.log == "DynDOLOD_SSE_log.txt"

    def test_el_progreso_declara_cuanto_hace_que_el_log_no_cambia(self) -> None:
        reloj = _Reloj(RastreadorDeActividad(_config(progreso=100.0), inicio=0.0))
        reloj.tick(_muestra(), 1.0)  # única actividad

        for t in range(2, 101):
            reloj.tick(_muestra(), float(t))

        [(instante, aviso)] = reloj.avisos
        assert (instante, aviso.tipo) == (100.0, TipoDeAviso.PROGRESO)
        assert aviso.segundos_sin_actividad == 99.0, "un log quieto menos que el umbral se informa, no se calla"

    def test_sin_log_de_esta_corrida_el_progreso_no_inventa_un_archivo(self) -> None:
        reloj = _Reloj(RastreadorDeActividad(_config(progreso=100.0), inicio=0.0))

        for t in range(1, 101):
            reloj.tick(None, float(t))

        [(_, aviso)] = reloj.avisos
        assert (aviso.log, aviso.tamano_del_log, aviso.ultima_linea) == ("", 0, "")


class TestEstancamiento:
    def test_un_log_que_no_cambia_avisa_al_llegar_al_umbral_y_una_sola_vez(self) -> None:
        reloj = _Reloj(RastreadorDeActividad(_config(estancamiento=300.0), inicio=0.0))
        reloj.tick(_muestra(), 10.0)  # única actividad: firma nueva en t=10

        for t in range(11, 900):
            reloj.tick(_muestra(), float(t))

        [(instante, aviso)] = reloj.avisos
        assert (instante, aviso.tipo) == (310.0, TipoDeAviso.ESTANCADO), "300 s después de la última actividad"
        assert aviso.segundos_sin_actividad == 300.0
        assert aviso.ultima_linea == "x"

    def test_una_reanudacion_cierra_el_episodio_y_dice_cuanto_duro(self) -> None:
        reloj = _Reloj(RastreadorDeActividad(_config(estancamiento=300.0), inicio=0.0))
        reloj.tick(_muestra(), 10.0)
        for t in range(11, 311):
            reloj.tick(_muestra(), float(t))

        reanudado = reloj.tick(_muestra(tamano=2000, mtime=400.0), 400.0)

        assert reanudado is not None
        assert reanudado.tipo is TipoDeAviso.REANUDADO
        assert reanudado.segundos_sin_actividad == 390.0, "cuánto estuvo sin actividad (de t=10 a t=400)"
        assert reanudado.tamano_del_log == 2000

    def test_dos_episodios_de_estancamiento_avisan_dos_veces(self) -> None:
        reloj = _Reloj(RastreadorDeActividad(_config(estancamiento=300.0), inicio=0.0))
        t = 0.0
        for episodio in range(2):
            t += 1.0
            quieta = _muestra(tamano=1000 + episodio, mtime=t)  # firma nueva: actividad
            reloj.tick(quieta, t)
            for _ in range(350):  # 350 s sin cambios: supera el umbral de 300
                t += 1.0
                reloj.tick(quieta, t)
        t += 1.0
        reloj.tick(_muestra(tamano=9999, mtime=t), t)  # y recién ahí vuelve a escribir

        assert [tipo for _, tipo in reloj.tipos()] == [
            TipoDeAviso.ESTANCADO,
            TipoDeAviso.REANUDADO,
            TipoDeAviso.ESTANCADO,
            TipoDeAviso.REANUDADO,
        ]

    def test_un_cambio_de_mtime_sin_cambio_de_tamano_cuenta_como_actividad(self) -> None:
        reloj = _Reloj(RastreadorDeActividad(_config(estancamiento=300.0), inicio=0.0))
        reloj.tick(_muestra(tamano=1000, mtime=1.0), 1.0)

        # Cada 200 s el binario "toca" el log sin cambiar su tamaño: nunca llega a 300 s de silencio.
        for t in range(2, 1000):
            mtime = float((t // 200) * 200 + 1)
            reloj.tick(_muestra(tamano=1000, mtime=mtime), float(t))

        assert reloj.avisos == []

    def test_crecer_sin_cambiar_el_mtime_cuenta_como_actividad(self) -> None:
        """Con granularidad gruesa de mtime (FAT: 2 s) dos sondeos pueden ver el mismo mtime con otro tamaño."""
        reloj = _Reloj(RastreadorDeActividad(_config(estancamiento=300.0), inicio=0.0))

        for t in range(1, 1000):
            reloj.tick(_muestra(tamano=1000 + t, mtime=7.0), float(t))

        assert reloj.avisos == []

    def test_empezar_a_escribir_otro_log_cuenta_como_actividad(self) -> None:
        """TexGen termina y DynDOLOD empieza su propio log: la firma incluye el NOMBRE.

        Tamaño y mtime son idénticos a propósito: sólo el nombre distingue a un log del otro.
        """
        reloj = _Reloj(RastreadorDeActividad(_config(estancamiento=300.0), inicio=0.0))
        reloj.tick(_muestra(nombre="TexGen_SSE_log.txt", tamano=500, mtime=1.0), 1.0)
        for t in range(2, 301):
            reloj.tick(_muestra(nombre="TexGen_SSE_log.txt", tamano=500, mtime=1.0), float(t))
        assert reloj.avisos == [], "299 s de silencio todavía no es un estancamiento"

        # t=301 sería el estancamiento de TexGen: justo antes aparece el log de DynDOLOD.
        reloj.tick(_muestra(nombre="DynDOLOD_SSE_log.txt", tamano=500, mtime=1.0), 301.0)
        reloj.tick(_muestra(nombre="DynDOLOD_SSE_log.txt", tamano=500, mtime=1.0), 302.0)

        assert reloj.avisos == []

    def test_sin_log_de_esta_corrida_el_silencio_se_mide_desde_el_inicio(self) -> None:
        reloj = _Reloj(RastreadorDeActividad(_config(estancamiento=300.0), inicio=1000.0))

        for t in range(1, 300):
            reloj.tick(None, 1000.0 + t)
        assert reloj.avisos == []
        aviso = reloj.tick(None, 1300.0)

        assert aviso is not None
        assert aviso.tipo is TipoDeAviso.ESTANCADO
        assert aviso.log == "", "todavía no hay un log de esta corrida que nombrar"
        assert (aviso.ultima_linea, aviso.tamano_del_log) == ("", 0)
        assert aviso.segundos_sin_actividad == 300.0
        assert aviso.segundos_en_curso == 300.0


class TestInteraccionEntreAvisos:
    def test_si_coinciden_estancamiento_y_progreso_sale_el_estancamiento(self) -> None:
        reloj = _Reloj(RastreadorDeActividad(_config(progreso=300.0, estancamiento=300.0), inicio=0.0))
        reloj.tick(_muestra(), 0.0)

        aviso = reloj.tick(_muestra(), 300.0)

        assert aviso is not None
        assert aviso.tipo is TipoDeAviso.ESTANCADO

    def test_una_vez_estancado_no_hay_mas_avisos_de_progreso(self) -> None:
        reloj = _Reloj(RastreadorDeActividad(_config(progreso=100.0, estancamiento=300.0), inicio=0.0))
        reloj.tick(_muestra(), 0.0)

        for t in range(1, 3000):
            reloj.tick(_muestra(), float(t))

        tipos = [tipo for _, tipo in reloj.tipos()]
        assert tipos == [TipoDeAviso.PROGRESO, TipoDeAviso.PROGRESO, TipoDeAviso.ESTANCADO], (
            "dos progresos mientras el silencio era menor al umbral, el estancamiento, y después nada"
        )

    def test_tras_reanudar_el_progreso_se_cuenta_desde_la_reanudacion(self) -> None:
        reloj = _Reloj(RastreadorDeActividad(_config(progreso=100.0, estancamiento=300.0), inicio=0.0))
        reloj.tick(_muestra(), 0.0)
        for t in range(1, 301):
            reloj.tick(_muestra(), float(t))  # PROGRESO en 100 y 200, ESTANCADO en 300
        reloj.tick(_muestra(tamano=2000, mtime=400.0), 400.0)  # REANUDADO

        # Sin reiniciar la cuenta, el progreso "atrasado" saldría pegado a la reanudación: dos mensajes seguidos.
        siguiente = reloj.tick(_muestra(tamano=2001, mtime=401.0), 401.0)

        assert siguiente is None


class TestConfiguracionDeObservacion:
    @pytest.mark.parametrize(
        "campo",
        ["intervalo_de_sondeo_s", "intervalo_de_progreso_s", "umbral_de_estancamiento_s", "max_bytes_de_cola"],
    )
    @pytest.mark.parametrize("valor", [0, -1])
    def test_un_valor_no_positivo_se_rechaza_nombrando_el_campo(self, campo: str, valor: float) -> None:
        with pytest.raises(ValueError, match=campo):
            ConfiguracionDeObservacion(**{campo: valor})

    def test_los_defaults_estan_muy_por_encima_del_mayor_silencio_medido(self) -> None:
        """Una corrida real commiteada tuvo, como mucho, 65 s entre marcas de tiempo consecutivas del log.

        El criterio es de margen (10x), NO una calibración: es UNA corrida. Un falso estancamiento en una corrida
        sana le enseña al operador a ignorar el aviso, así que el default se equivoca hacia avisar tarde.
        """
        config = ConfiguracionDeObservacion()

        assert config.umbral_de_estancamiento_s >= 10 * 65
        assert config.intervalo_de_sondeo_s <= config.umbral_de_estancamiento_s / 10
        assert config.intervalo_de_progreso_s < config.umbral_de_estancamiento_s


# ---------------------------------------------------------------------------
# tomar_muestra: leer el log sin romper nada
# ---------------------------------------------------------------------------


class TestTomarMuestra:
    def test_toma_tamano_mtime_nombre_y_ultima_linea(self, tmp_path: pathlib.Path) -> None:
        log = tmp_path / "DynDOLOD_SSE_log.txt"
        contenido = b"[00:01] arranque\n[00:02] Generating LOD for Tamriel\n"
        log.write_bytes(contenido)

        muestra = tomar_muestra(log, 4096)

        assert muestra == MuestraDeLog(
            nombre="DynDOLOD_SSE_log.txt",
            tamano=len(contenido),
            mtime=log.stat().st_mtime,
            ultima_linea="[00:02] Generating LOD for Tamriel",
        )

    def test_ignora_las_lineas_vacias_del_final(self, tmp_path: pathlib.Path) -> None:
        log = tmp_path / "TexGen_SSE_log.txt"
        log.write_bytes(b"[00:01] a\n[00:47] TexGen completed successfully\n\n   \r\n\n")

        muestra = tomar_muestra(log, 4096)

        assert muestra is not None
        assert muestra.ultima_linea == "[00:47] TexGen completed successfully"

    def test_un_log_mas_grande_que_la_cola_conserva_la_ultima_linea_real(self, tmp_path: pathlib.Path) -> None:
        log = tmp_path / "DynDOLOD_SSE_log.txt"
        relleno = b"".join(f"[00:00] linea {i:06d}\n".encode() for i in range(50000))
        final = b"[05:39] Saving Occlusion.esp\n"
        log.write_bytes(relleno + final)

        muestra = tomar_muestra(log, 1024)

        assert muestra is not None
        assert muestra.ultima_linea == "[05:39] Saving Occlusion.esp"
        assert muestra.tamano == len(relleno) + len(final), "el tamaño es el del archivo, no el de la cola leída"

    def test_una_linea_enorme_se_recorta(self, tmp_path: pathlib.Path) -> None:
        log = tmp_path / "DynDOLOD_SSE_log.txt"
        log.write_bytes(b"[00:01] " + b"x" * 5000 + b"\n")

        muestra = tomar_muestra(log, 8192)

        assert muestra is not None
        assert len(muestra.ultima_linea) == mod.MAX_CARACTERES_DE_LINEA
        assert muestra.ultima_linea.endswith("…")

    def test_bytes_que_ningun_codec_acepta_no_lanzan(self, tmp_path: pathlib.Path) -> None:
        log = tmp_path / "DynDOLOD_SSE_log.txt"
        log.write_bytes(b"[00:01] \xff\xfe\x81 raro \x8d\n")

        muestra = tomar_muestra(log, 4096)

        assert muestra is not None
        assert "raro" in muestra.ultima_linea

    def test_un_log_vacio_es_una_muestra_valida_sin_linea(self, tmp_path: pathlib.Path) -> None:
        log = tmp_path / "DynDOLOD_SSE_log.txt"
        log.write_bytes(b"")

        muestra = tomar_muestra(log, 4096)

        assert muestra is not None
        assert (muestra.tamano, muestra.ultima_linea) == (0, "")

    def test_no_hay_muestra_si_no_es_un_archivo_regular(self, tmp_path: pathlib.Path) -> None:
        directorio = tmp_path / "dir_log.txt"
        directorio.mkdir()

        assert tomar_muestra(tmp_path / "ausente_log.txt", 4096) is None
        assert tomar_muestra(directorio, 4096) is None

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="sin FIFOs en esta plataforma")
    def test_un_archivo_especial_no_se_abre(self, tmp_path: pathlib.Path) -> None:
        """Abrir un FIFO para lectura bloquea hasta que alguien escriba: el hilo del observador quedaría colgado.

        Corre en un hilo con plazo para que, si el código regresara, el test FALLE en vez de colgar la suite.
        """
        fifo = tmp_path / "DynDOLOD_SSE_log.txt"
        os.mkfifo(fifo)
        resultado: list[MuestraDeLog | None] = []
        hilo = threading.Thread(target=lambda: resultado.append(tomar_muestra(fifo, 4096)), daemon=True)

        hilo.start()
        hilo.join(timeout=5)
        colgado = hilo.is_alive()
        if colgado:
            # Destraba el open() bloqueado (abriendo el otro extremo) para no dejar un hilo huérfano.
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            hilo.join(timeout=5)

        assert not colgado, "tomar_muestra se bloqueó abriendo un archivo especial"
        assert resultado == [None]

    def test_no_hay_muestra_si_es_un_enlace_simbolico(self, tmp_path: pathlib.Path) -> None:
        secreto = tmp_path / "secreto.txt"
        secreto.write_bytes(b"no debe leerse\n")
        enlace = tmp_path / "DynDOLOD_SSE_log.txt"
        try:
            enlace.symlink_to(secreto)
        except (OSError, NotImplementedError):
            pytest.skip("la plataforma no permite crear enlaces simbólicos (sin privilegio): caso no ejercitable acá")

        assert tomar_muestra(enlace, 4096) is None

    def test_un_error_del_sistema_de_archivos_no_lanza(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = tmp_path / "DynDOLOD_SSE_log.txt"
        log.write_bytes(b"x\n")

        def _sharing_violation(*_args: object, **_kwargs: object) -> object:
            raise PermissionError("sharing violation")

        monkeypatch.setattr(pathlib.Path, "open", _sharing_violation)

        assert tomar_muestra(log, 4096) is None


def test_el_aviso_es_inmutable() -> None:
    aviso = AvisoDeActividad(
        tipo=TipoDeAviso.PROGRESO,
        log="x",
        segundos_en_curso=1.0,
        segundos_sin_actividad=0.0,
        tamano_del_log=1,
        ultima_linea="",
    )

    with pytest.raises(AttributeError):
        aviso.log = "otro"  # type: ignore[misc]


def test_los_tipos_de_aviso_son_los_del_contrato_del_evento() -> None:
    """El valor de cada tipo viaja en el payload del evento y lo lee el notificador: no se renombra a la ligera."""
    assert {t.name: t.value for t in TipoDeAviso} == {
        "PROGRESO": "progress",
        "ESTANCADO": "stalled",
        "REANUDADO": "resumed",
    }
