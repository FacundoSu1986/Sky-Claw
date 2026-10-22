"""NP-O0b2: el escritor de la salida gestionada, sobre un disco real (``tmp_path``).

Cada test monta un mod de salida real, corre una o más sesiones y mira el disco. Lo que se prueba:

- **Propiedad**: solo se toca lo que el manifiesto prueba nuestro e intacto (``PRESERVE_EXISTING``).
- **Exclusión**: un lock cross-process; una segunda sesión sobre la misma raíz no entra (ver el test con DOS PROCESOS).
- **Verificar lo que se aparta**: un archivo nuestro se aparta a una papelera y recién ahí se verifica su huella.
- **Cortes**: el proceso puede morir en cualquier momento y la sesión siguiente deja todo consistente (ver más abajo).
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import hashlib
import inspect
import logging
import os
import pathlib
import re
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from typing import Any

import pytest

from sky_claw.app.db.locks import LockLeaseLostError, SnapshotTransactionLock
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.app.security.links import ContencionFisicaVioladaError
from sky_claw.local.native_parallax import output_writer
from sky_claw.local.native_parallax.output import (
    NOMBRE_DEL_MOD_DE_SALIDA,
    AccionDeSalida,
    AccionPlanificada,
    EncoderUsado,
    Manifiesto,
    MotivoDeSalida,
    NivelDeReproducibilidad,
    Observacion,
    OrigenDeEntrada,
    Procedencia,
    SalidaDeseada,
    TipoObservado,
    clave_de_cache,
    clave_de_ruta,
    cota_del_manifiesto_tras_un_corte,
    crear_entrada,
    leer_manifiesto,
    parametros_canonicos,
    serializar_manifiesto,
)
from sky_claw.local.native_parallax.output_writer import (
    CoordinacionDeSalida,
    EfectoDeSalida,
    FuenteDeSalida,
    ResultadoDeAplicacion,
    ResultadoPorRuta,
    SalidaNoEscribibleError,
    SalidaOcupadaError,
    SesionDeSalida,
    abrir_sesion_de_salida,
    construir_coordinacion_de_salida,
)
from tests._symlink_guard import crear_junction

H_X = "c" * 64
RUTA_A = "textures/architecture/whiterun/muro01_p.dds"
RUTA_B = "textures/architecture/whiterun/muro02_p.dds"
RUTA_C = "textures/landscape/roca01_p.dds"


def _procedencia(escala: float = 1.5) -> Procedencia:
    return Procedencia(
        algoritmo_id="normal_fft_periodic_v1",
        parametros=parametros_canonicos({"borde": "periodico", "escala": escala}),
        origenes=(OrigenDeEntrada(rol="normal", sha256=H_X),),
        encoder=EncoderUsado(
            exportador_id="np_e0b_bc4_v1",
            texconv_version="may2026",
            texconv_sha256=H_X,
            argumentos=("-f", "BC4_UNORM"),
        ),
        entorno=(("numpy", "2.4.6"), ("plataforma", "win32")),
        nivel=NivelDeReproducibilidad.DETERMINISTA_MISMO_ENTORNO,
    )


def _sha(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


class Escenario:
    """Un mod de salida en ``tmp_path`` y utilidades para armar fuentes y mirar el disco."""

    def __init__(self, base: pathlib.Path) -> None:
        self.base = base
        self.mods = base / "mods"
        self.mods.mkdir()
        self.raiz = self.mods / NOMBRE_DEL_MOD_DE_SALIDA
        self.trabajo = base / "trabajo"
        self.trabajo.mkdir()
        self._n = 0
        self.coordinacion = construir_coordinacion_de_salida(base=base / "estado")

    def fuente(self, contenido: bytes, escala: float = 1.5) -> FuenteDeSalida:
        self._n += 1
        archivo = self.trabajo / f"fuente{self._n}.dds"
        archivo.write_bytes(contenido)
        return FuenteDeSalida(archivo=archivo, procedencia=_procedencia(escala))

    def disco(self, ruta: str) -> pathlib.Path:
        return self.raiz.joinpath(*ruta.split("/"))

    def leer(self, ruta: str) -> bytes:
        return self.disco(ruta).read_bytes()

    def manifiesto_en_disco(self) -> Manifiesto:
        """El manifiesto comprometido; si nunca se escribió uno, es el vacío (ausente equivale a vacío)."""
        archivo = self.raiz / ".skyclaw-output" / "manifest.json"
        return leer_manifiesto(archivo.read_bytes()) if archivo.exists() else Manifiesto(entradas=())

    def archivos_bajo_textures(self) -> set[str]:
        textures = self.raiz / "textures"
        if not textures.exists():
            return set()
        return {p.relative_to(self.raiz).as_posix() for p in textures.rglob("*") if p.is_file()}

    def control(self) -> pathlib.Path:
        return self.raiz / ".skyclaw-output"

    async def lote(
        self, lote: dict[str, FuenteDeSalida], *, tambien_deseadas: dict[str, str] | None = None
    ) -> ResultadoDeAplicacion:
        """Una sesión completa: planifica lo deseado (``lote``) y aplica con las fuentes que el plan pida."""
        async with abrir_sesion_de_salida(self.raiz, coordinacion=self.coordinacion) as sesion:
            return await aplicar_lote(sesion, lote, tambien_deseadas)

    async def cerrar(self) -> None:
        await self.coordinacion.close()


async def aplicar_lote(
    sesion: SesionDeSalida, lote: dict[str, FuenteDeSalida], tambien_deseadas: dict[str, str] | None = None
) -> ResultadoDeAplicacion:
    deseadas = [SalidaDeseada(ruta, clave_de_cache(f.procedencia)) for ruta, f in lote.items()]
    deseadas += [SalidaDeseada(ruta, clave) for ruta, clave in (tambien_deseadas or {}).items()]
    plan = await sesion.planificar(deseadas)
    pedidas = {
        clave_de_ruta(a.ruta)
        for a in plan.acciones
        if a.accion in (AccionDeSalida.CONSTRUIR, AccionDeSalida.RECONSTRUIR)
    }
    fuentes = {ruta: f for ruta, f in lote.items() if clave_de_ruta(ruta) in pedidas}
    return await sesion.aplicar(plan, fuentes)


@pytest.fixture
async def escenario(tmp_path: pathlib.Path) -> AsyncIterator[Escenario]:
    e = Escenario(tmp_path)
    yield e
    await e.cerrar()


def _efectos(resultado: ResultadoDeAplicacion) -> dict[str, EfectoDeSalida]:
    return {r.ruta: r.efecto for r in resultado.rutas}


def _detalles(resultado: ResultadoDeAplicacion) -> dict[str, str]:
    return {r.ruta: r.detalle for r in resultado.rutas}


def _identidad(archivo: pathlib.Path) -> tuple[int, int]:
    """Inodo y ``mtime_ns``: cambian si el archivo se reescribe, aunque sea con el mismo contenido."""
    datos = archivo.stat()
    return datos.st_ino, datos.st_mtime_ns


def _enlazar_o_saltear(destino: pathlib.Path, enlace: pathlib.Path) -> None:
    try:
        os.symlink(destino, enlace, target_is_directory=destino.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("este sistema no permite crear symlinks")


# ---------------------------------------------------------------------------
# Primer lote: crea el mod, publica y compromete el manifiesto
# ---------------------------------------------------------------------------


async def test_el_primer_lote_crea_el_mod_publica_los_archivos_y_escribe_el_manifiesto(escenario: Escenario) -> None:
    resultado = await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A"), RUTA_C: escenario.fuente(b"DDS-C", 2.5)})

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.PUBLICADA, RUTA_C: EfectoDeSalida.PUBLICADA}
    assert resultado.completo
    assert escenario.leer(RUTA_A) == b"DDS-A"
    assert escenario.leer(RUTA_C) == b"DDS-C"
    manifiesto = escenario.manifiesto_en_disco()
    assert {e.ruta: e.sha256 for e in manifiesto.entradas} == {RUTA_A: _sha(b"DDS-A"), RUTA_C: _sha(b"DDS-C")}
    assert {e.ruta: e.tamano for e in manifiesto.entradas} == {RUTA_A: 5, RUTA_C: 5}
    assert resultado.manifiesto == manifiesto


async def test_tras_un_lote_completo_no_quedan_ni_pendiente_ni_etapas_ni_papelera(escenario: Escenario) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})

    control = escenario.control()
    assert sorted(p.name for p in control.iterdir()) == ["manifest.json"]


async def test_el_mod_de_salida_solo_tiene_texturas_y_la_carpeta_de_control(escenario: Escenario) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})

    assert sorted(p.name for p in escenario.raiz.iterdir()) == [".skyclaw-output", "textures"]


async def test_los_directorios_intermedios_se_crean_y_los_nombres_conservan_la_ortografia_pedida(
    escenario: Escenario,
) -> None:
    ruta = "Textures/Architecture/Muro_P.dds"

    await escenario.lote({ruta: escenario.fuente(b"DDS")})

    assert escenario.manifiesto_en_disco().entradas[0].ruta == ruta
    assert escenario.leer(ruta) == b"DDS"


# ---------------------------------------------------------------------------
# Caché: lo vigente se omite; un cambio reconstruye
# ---------------------------------------------------------------------------


async def test_un_segundo_lote_igual_omite_todo_y_no_toca_los_archivos(escenario: Escenario) -> None:
    lote = {RUTA_A: escenario.fuente(b"DDS-A"), RUTA_C: escenario.fuente(b"DDS-C", 2.5)}
    await escenario.lote(lote)
    antes = {r: escenario.disco(r).stat().st_ino for r in (RUTA_A, RUTA_C)}
    manifiesto_antes = (escenario.control() / "manifest.json").read_bytes()
    identidad_antes = _identidad(escenario.control() / "manifest.json")

    resultado = await escenario.lote(lote)

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.OMITIDA, RUTA_C: EfectoDeSalida.OMITIDA}
    assert {r: escenario.disco(r).stat().st_ino for r in (RUTA_A, RUTA_C)} == antes
    assert (escenario.control() / "manifest.json").read_bytes() == manifiesto_antes
    # Ni siquiera se reescribe con el mismo contenido (un reemplazo atómico cambiaría el inodo).
    assert _identidad(escenario.control() / "manifest.json") == identidad_antes


async def test_un_cambio_de_procedencia_reemplaza_el_archivo_y_actualiza_el_manifiesto(escenario: Escenario) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-viejo", 1.5)})

    resultado = await escenario.lote({RUTA_A: escenario.fuente(b"DDS-nuevo", 9.5)})

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.REEMPLAZADA}
    assert resultado.rutas[0].detalle == ""
    assert escenario.leer(RUTA_A) == b"DDS-nuevo"
    assert escenario.manifiesto_en_disco().entradas[0].sha256 == _sha(b"DDS-nuevo")
    assert not (escenario.control() / "trash").exists() or not list((escenario.control() / "trash").iterdir())


async def test_lo_que_ya_no_se_desea_se_retira_y_se_podan_los_directorios_vacios(escenario: Escenario) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A"), RUTA_C: escenario.fuente(b"DDS-C", 2.5)})

    resultado = await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})

    assert _efectos(resultado)[RUTA_C] is EfectoDeSalida.RETIRADA
    assert escenario.archivos_bajo_textures() == {RUTA_A}
    assert not (escenario.raiz / "textures" / "landscape").exists()
    assert (escenario.raiz / "textures").is_dir()
    assert {e.ruta for e in escenario.manifiesto_en_disco().entradas} == {RUTA_A}


# ---------------------------------------------------------------------------
# PRESERVE_EXISTING: lo ajeno y lo modificado no se tocan
# ---------------------------------------------------------------------------


async def test_un_archivo_ajeno_en_la_ruta_deseada_no_se_pisa_ni_entra_al_manifiesto(escenario: Escenario) -> None:
    escenario.disco(RUTA_A).parent.mkdir(parents=True)
    escenario.disco(RUTA_A).write_bytes(b"DEL-USUARIO")

    resultado = await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.PRESERVADA}
    assert escenario.leer(RUTA_A) == b"DEL-USUARIO"
    assert escenario.manifiesto_en_disco().entradas == ()


async def test_un_archivo_nuestro_modificado_por_un_tercero_no_se_pisa_y_sale_del_manifiesto(
    escenario: Escenario,
) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A", 1.5)})
    escenario.disco(RUTA_A).write_bytes(b"EDITADO-A-MANO")

    resultado = await escenario.lote({RUTA_A: escenario.fuente(b"DDS-nuevo", 9.5)})

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.PRESERVADA}
    assert escenario.leer(RUTA_A) == b"EDITADO-A-MANO"
    assert escenario.manifiesto_en_disco().entradas == ()


async def test_retirar_un_archivo_modificado_por_un_tercero_lo_suelta_sin_borrarlo(escenario: Escenario) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    escenario.disco(RUTA_A).write_bytes(b"EDITADO-A-MANO")

    resultado = await escenario.lote({})

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.SOLTADA}
    assert escenario.leer(RUTA_A) == b"EDITADO-A-MANO"
    assert escenario.manifiesto_en_disco().entradas == ()


async def test_un_archivo_nuestro_borrado_por_un_tercero_se_vuelve_a_construir(escenario: Escenario) -> None:
    lote = {RUTA_A: escenario.fuente(b"DDS-A")}
    await escenario.lote(lote)
    escenario.disco(RUTA_A).unlink()

    resultado = await escenario.lote(lote)

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.PUBLICADA}
    assert escenario.leer(RUTA_A) == b"DDS-A"


async def test_los_archivos_de_terceros_junto_a_los_nuestros_nunca_se_tocan(escenario: Escenario) -> None:
    ajeno = escenario.raiz / "textures" / "otros" / "no_es_nuestro_p.dds"
    ajeno.parent.mkdir(parents=True)
    ajeno.write_bytes(b"AJENO")
    lote = {RUTA_A: escenario.fuente(b"DDS-A")}

    await escenario.lote(lote)
    await escenario.lote({})  # retira todo lo nuestro

    assert ajeno.read_bytes() == b"AJENO"
    assert escenario.archivos_bajo_textures() == {"textures/otros/no_es_nuestro_p.dds"}


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="sin symlinks")
async def test_una_ruta_que_atraviesa_un_enlace_a_otro_lado_no_se_escribe(
    escenario: Escenario, tmp_path: pathlib.Path
) -> None:
    afuera = tmp_path / "afuera"
    afuera.mkdir()
    (escenario.raiz / "textures").mkdir(parents=True)
    try:
        os.symlink(afuera, escenario.raiz / "textures" / "architecture", target_is_directory=True)
    except OSError:
        pytest.skip("este sistema no permite crear symlinks")

    resultado = await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.PRESERVADA}
    assert list(afuera.rglob("*")) == []
    assert escenario.manifiesto_en_disco().entradas == ()


# ---------------------------------------------------------------------------
# La raíz del mod
# ---------------------------------------------------------------------------


async def test_la_raiz_debe_llamarse_como_el_mod_de_salida(escenario: Escenario) -> None:
    otra = escenario.mods / "Otro Mod"

    with pytest.raises(SalidaNoEscribibleError, match="nombre"):
        async with abrir_sesion_de_salida(otra, coordinacion=escenario.coordinacion):
            pytest.fail("no debería entrar")

    assert not otra.exists()


async def test_la_raiz_debe_ser_absoluta_y_su_carpeta_madre_debe_existir(
    escenario: Escenario, tmp_path: pathlib.Path
) -> None:
    with pytest.raises(SalidaNoEscribibleError, match="absoluta"):
        async with abrir_sesion_de_salida(pathlib.Path(NOMBRE_DEL_MOD_DE_SALIDA), coordinacion=escenario.coordinacion):
            pytest.fail("no debería entrar")
    inexistente = tmp_path / "no_existe" / NOMBRE_DEL_MOD_DE_SALIDA
    with pytest.raises(SalidaNoEscribibleError, match="existir"):
        async with abrir_sesion_de_salida(inexistente, coordinacion=escenario.coordinacion):
            pytest.fail("no debería entrar")
    assert not inexistente.parent.exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="sin symlinks")
async def test_una_raiz_que_es_un_enlace_se_rechaza(escenario: Escenario, tmp_path: pathlib.Path) -> None:
    destino = tmp_path / "destino"
    destino.mkdir()
    try:
        os.symlink(destino, escenario.raiz, target_is_directory=True)
    except OSError:
        pytest.skip("este sistema no permite crear symlinks")

    with pytest.raises(SalidaNoEscribibleError, match="es un enlace"):
        async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion):
            pytest.fail("no debería entrar")

    assert list(destino.iterdir()) == []


async def test_una_raiz_que_es_un_archivo_se_rechaza(escenario: Escenario) -> None:
    escenario.raiz.write_bytes(b"no soy un directorio")

    with pytest.raises(SalidaNoEscribibleError, match="directorio"):
        async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion):
            pytest.fail("no debería entrar")


# ---------------------------------------------------------------------------
# Validación del lote antes de tocar nada
# ---------------------------------------------------------------------------


async def test_faltar_una_fuente_pedida_por_el_plan_falla_sin_tocar_el_disco(escenario: Escenario) -> None:
    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        f = escenario.fuente(b"DDS-A")
        plan = await sesion.planificar([SalidaDeseada(RUTA_A, clave_de_cache(f.procedencia))])

        with pytest.raises(SalidaNoEscribibleError, match="fuente"):
            await sesion.aplicar(plan, {})

    assert escenario.archivos_bajo_textures() == set()
    assert sorted(p.name for p in escenario.control().iterdir()) == []


async def test_una_fuente_que_no_fue_pedida_por_el_plan_se_rechaza(escenario: Escenario) -> None:
    lote = {RUTA_A: escenario.fuente(b"DDS-A")}
    await escenario.lote(lote)
    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        plan = await sesion.planificar([SalidaDeseada(RUTA_A, clave_de_cache(lote[RUTA_A].procedencia))])  # OMITIR

        with pytest.raises(SalidaNoEscribibleError, match="fuente"):
            await sesion.aplicar(plan, {RUTA_A: lote[RUTA_A]})


async def test_una_fuente_con_otra_procedencia_que_la_deseada_se_rechaza(escenario: Escenario) -> None:
    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        deseada = escenario.fuente(b"DDS-A", 1.5)
        entregada = escenario.fuente(b"DDS-A", 9.5)
        plan = await sesion.planificar([SalidaDeseada(RUTA_A, clave_de_cache(deseada.procedencia))])

        with pytest.raises(SalidaNoEscribibleError, match="clave"):
            await sesion.aplicar(plan, {RUTA_A: entregada})

    assert escenario.archivos_bajo_textures() == set()


async def test_un_plan_que_no_es_el_ultimo_de_la_sesion_se_rechaza(escenario: Escenario) -> None:
    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        f = escenario.fuente(b"DDS-A")
        plan_viejo = await sesion.planificar([SalidaDeseada(RUTA_A, clave_de_cache(f.procedencia))])
        await sesion.planificar([])

        with pytest.raises(SalidaNoEscribibleError, match="plan"):
            await sesion.aplicar(plan_viejo, {RUTA_A: f})


@pytest.mark.parametrize(
    ("caso", "mensaje"),
    [("no_existe", "no existe"), ("enlace", "no es un archivo regular"), ("directorio", "no es un archivo regular")],
)
async def test_una_fuente_que_no_existe_o_es_un_enlace_se_rechaza_antes_de_tocar_nada(
    escenario: Escenario, tmp_path: pathlib.Path, caso: str, mensaje: str
) -> None:
    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        f = escenario.fuente(b"DDS-A")
        plan = await sesion.planificar([SalidaDeseada(RUTA_A, clave_de_cache(f.procedencia))])
        f.archivo.unlink()
        if caso == "enlace":  # un enlace a un archivo con los mismos bytes: igual se rechaza, no se sigue
            real = tmp_path / "real.dds"
            real.write_bytes(b"DDS-A")
            _enlazar_o_saltear(real, f.archivo)
        elif caso == "directorio":
            f.archivo.mkdir()

        with pytest.raises(SalidaNoEscribibleError, match=f"la fuente .* {mensaje}"):
            await sesion.aplicar(plan, {RUTA_A: f})

    assert escenario.archivos_bajo_textures() == set()


async def test_una_fuente_vacia_se_rechaza(escenario: Escenario) -> None:
    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        f = escenario.fuente(b"")
        plan = await sesion.planificar([SalidaDeseada(RUTA_A, clave_de_cache(f.procedencia))])

        with pytest.raises(SalidaNoEscribibleError, match="vacía"):
            await sesion.aplicar(plan, {RUTA_A: f})


def test_el_maximo_de_la_ruta_completa_es_el_max_path_de_windows_menos_el_terminador() -> None:
    assert output_writer._LARGO_MAXIMO_DE_RUTA_COMPLETA == 259


async def test_una_ruta_cuya_ruta_completa_no_cabe_falla_esa_ruta_sin_publicar_y_no_las_demas(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    limite = len(os.fspath(escenario.disco(RUTA_C)))  # RUTA_C cabe justo; RUTA_A es más larga
    monkeypatch.setattr(output_writer, "_LARGO_MAXIMO_DE_RUTA_COMPLETA", limite)

    resultado = await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A"), RUTA_C: escenario.fuente(b"DDS-C", 2.5)})

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.FALLIDA, RUTA_C: EfectoDeSalida.PUBLICADA}
    assert not resultado.completo
    assert escenario.archivos_bajo_textures() == {RUTA_C}
    assert {e.ruta for e in escenario.manifiesto_en_disco().entradas} == {RUTA_C}
    detalle = next(r.detalle for r in resultado.rutas if r.ruta == RUTA_A)
    assert f"mide {len(os.fspath(escenario.disco(RUTA_A)))}" in detalle
    assert f"máximo es {limite}" in detalle


async def test_una_ruta_completa_exactamente_en_el_maximo_se_publica(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(output_writer, "_LARGO_MAXIMO_DE_RUTA_COMPLETA", len(os.fspath(escenario.disco(RUTA_A))))

    resultado = await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.PUBLICADA}


async def test_un_reemplazo_cuya_ruta_ya_no_cabe_deja_intacto_el_archivo_y_su_entrada(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-viejo", 1.5)})
    monkeypatch.setattr(output_writer, "_LARGO_MAXIMO_DE_RUTA_COMPLETA", 10)

    resultado = await escenario.lote({RUTA_A: escenario.fuente(b"DDS-nuevo", 9.5)})

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.FALLIDA}
    assert escenario.leer(RUTA_A) == b"DDS-viejo"
    assert escenario.manifiesto_en_disco().entradas[0].sha256 == _sha(b"DDS-viejo")


# ---------------------------------------------------------------------------
# El disco cambia entre planificar y aplicar, o en el instante vacío de un reemplazo
# ---------------------------------------------------------------------------


@pytest.fixture
def interponer(monkeypatch: pytest.MonkeyPatch) -> Any:
    """``interponer("tras_apartar", algo)``: corre *algo* la primera vez que el escritor pasa por ese punto."""
    pendientes: dict[str, Any] = {}

    def instalar(nombre: str, accion: Any) -> None:
        pendientes[nombre] = accion

    original = output_writer._punto_de_corte

    def punto(nombre: str) -> None:
        original(nombre)
        accion = pendientes.pop(nombre, None)
        if accion is not None:
            accion()

    monkeypatch.setattr(output_writer, "_punto_de_corte", punto)
    return instalar


async def _planificar_y_aplicar(
    escenario: Escenario, lote: dict[str, FuenteDeSalida], entre: Any = None
) -> ResultadoDeAplicacion:
    """Como ``Escenario.lote`` pero corriendo *entre* después de planificar y antes de aplicar."""
    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        deseadas = [SalidaDeseada(r, clave_de_cache(f.procedencia)) for r, f in lote.items()]
        plan = await sesion.planificar(deseadas)
        if entre is not None:
            entre()
        pedidas = {
            clave_de_ruta(a.ruta)
            for a in plan.acciones
            if a.accion in (AccionDeSalida.CONSTRUIR, AccionDeSalida.RECONSTRUIR)
        }
        return await sesion.aplicar(plan, {r: f for r, f in lote.items() if clave_de_ruta(r) in pedidas})


async def test_un_tercero_que_edita_el_archivo_entre_planificar_y_aplicar_no_es_pisado(escenario: Escenario) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-viejo", 1.5)})

    resultado = await _planificar_y_aplicar(
        escenario,
        {RUTA_A: escenario.fuente(b"DDS-nuevo", 9.5)},
        entre=lambda: escenario.disco(RUTA_A).write_bytes(b"EDITADO-A-MANO"),
    )

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.CONFLICTO}
    assert escenario.leer(RUTA_A) == b"EDITADO-A-MANO"
    assert escenario.manifiesto_en_disco().entradas == ()
    assert sorted(p.name for p in escenario.control().iterdir()) == ["manifest.json"]


async def test_un_tercero_que_borra_el_archivo_entre_planificar_y_aplicar_no_rompe_el_lote(
    escenario: Escenario,
) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-viejo", 1.5), RUTA_C: escenario.fuente(b"DDS-C", 2.5)})

    resultado = await _planificar_y_aplicar(
        escenario,
        {RUTA_A: escenario.fuente(b"DDS-nuevo", 9.5)},  # RUTA_C ya no se desea: se retira
        entre=lambda: (escenario.disco(RUTA_A).unlink(), escenario.disco(RUTA_C).unlink()),
    )

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.CONFLICTO, RUTA_C: EfectoDeSalida.SOLTADA}
    assert escenario.archivos_bajo_textures() == set()
    assert escenario.manifiesto_en_disco().entradas == ()


async def test_un_tercero_que_crea_un_archivo_donde_ibamos_a_construir_no_es_pisado(escenario: Escenario) -> None:
    def crear_ajeno() -> None:
        escenario.disco(RUTA_A).parent.mkdir(parents=True)
        escenario.disco(RUTA_A).write_bytes(b"DEL-USUARIO")

    resultado = await _planificar_y_aplicar(escenario, {RUTA_A: escenario.fuente(b"DDS-A")}, entre=crear_ajeno)

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.CONFLICTO}
    assert escenario.leer(RUTA_A) == b"DEL-USUARIO"
    assert escenario.manifiesto_en_disco().entradas == ()
    assert sorted(p.name for p in escenario.control().iterdir()) == ["manifest.json"] or not any(
        (escenario.control() / "staging").glob("*")
    )


async def test_retirar_un_archivo_que_un_tercero_edito_despues_de_planificar_lo_devuelve_intacto(
    escenario: Escenario,
) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})

    resultado = await _planificar_y_aplicar(
        escenario, {}, entre=lambda: escenario.disco(RUTA_A).write_bytes(b"EDITADO-A-MANO")
    )

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.SOLTADA}
    assert escenario.leer(RUTA_A) == b"EDITADO-A-MANO"
    assert escenario.manifiesto_en_disco().entradas == ()
    assert not (escenario.control() / "trash").exists()


async def test_si_alguien_ocupa_la_ruta_en_el_instante_vacio_de_un_reemplazo_no_se_pisa_y_el_original_se_conserva(
    escenario: Escenario, interponer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=output_writer.logger.name)
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-viejo", 1.5)})
    interponer("tras_verificar", lambda: escenario.disco(RUTA_A).write_bytes(b"LLEGO-OTRO"))

    resultado = await escenario.lote({RUTA_A: escenario.fuente(b"DDS-nuevo", 9.5)})

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.CONFLICTO}
    assert escenario.leer(RUTA_A) == b"LLEGO-OTRO"
    apartados = list((escenario.control() / "trash").iterdir())
    assert [p.read_bytes() for p in apartados] == [b"DDS-viejo"]  # el original (nuestro) no se perdió
    assert escenario.manifiesto_en_disco().entradas == ()
    detalle = resultado.rutas[0].detalle
    assert str(apartados[0]) in detalle
    assert "quedó ocupada" in caplog.text  # lo que queda en la papelera se avisa


async def test_si_el_reemplazo_no_se_puede_publicar_se_devuelve_el_original_y_queda_como_nuestro(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-viejo", 1.5)})
    original = output_writer._mover_sin_reemplazar
    destino_a = escenario.disco(RUTA_A)

    def mover(origen: pathlib.Path, destino: pathlib.Path) -> None:
        if destino == destino_a and origen.parent.name == "staging":  # solo la publicación del reemplazo
            raise PermissionError("sharing violation simulada")
        original(origen, destino)

    monkeypatch.setattr(output_writer, "_mover_sin_reemplazar", mover)

    resultado = await escenario.lote({RUTA_A: escenario.fuente(b"DDS-nuevo", 9.5)})

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.CONFLICTO}
    assert escenario.leer(RUTA_A) == b"DDS-viejo"
    assert escenario.manifiesto_en_disco().entradas[0].sha256 == _sha(b"DDS-viejo")  # sigue siendo nuestro
    assert not (escenario.control() / "trash").exists()
    assert not (escenario.control() / "staging").exists()


async def test_un_error_de_disco_al_publicar_una_ruta_nueva_falla_esa_ruta_y_limpia_la_etapa(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = output_writer._mover_sin_reemplazar
    destino_a = escenario.disco(RUTA_A)

    def mover(origen: pathlib.Path, destino: pathlib.Path) -> None:
        if destino == destino_a:
            raise PermissionError("sharing violation simulada")
        original(origen, destino)

    monkeypatch.setattr(output_writer, "_mover_sin_reemplazar", mover)

    resultado = await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A"), RUTA_C: escenario.fuente(b"DDS-C", 2.5)})

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.FALLIDA, RUTA_C: EfectoDeSalida.PUBLICADA}
    assert escenario.archivos_bajo_textures() == {RUTA_C}
    assert sorted(p.name for p in escenario.control().iterdir()) == ["manifest.json"]


@pytest.mark.parametrize(
    ("contenido", "dice"),
    [(b"OTRO-CONTENIDO", "creció"), (b"DDS-B", "cambió")],
    ids=["mas_grande", "mismo_tamano"],
)
async def test_una_fuente_que_cambia_mientras_se_copia_no_se_publica(
    escenario: Escenario, interponer: Any, contenido: bytes, dice: str
) -> None:
    """Se compara la huella Y el tamaño de lo copiado: un cambio que conserva el tamaño también se detecta, y uno que
    agranda la fuente se corta apenas pasa el tamaño validado."""
    fuente = escenario.fuente(b"DDS-A")
    interponer("tras_pendiente", lambda: fuente.archivo.write_bytes(contenido))

    resultado = await escenario.lote({RUTA_A: fuente})

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.FALLIDA}
    assert escenario.archivos_bajo_textures() == set()
    assert escenario.manifiesto_en_disco().entradas == ()
    assert dice in resultado.rutas[0].detalle


async def test_una_fuente_que_desaparece_antes_de_copiarse_falla_esa_ruta_sin_filtrar_descriptores(
    escenario: Escenario, interponer: Any
) -> None:
    perdida = escenario.fuente(b"DDS-A")
    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion):
        pass  # calienta: la base de locks ya queda abierta y su descriptor no cuenta
    interponer("tras_pendiente", lambda: perdida.archivo.unlink())
    fds_antes = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None

    resultado = await escenario.lote({RUTA_A: perdida, RUTA_C: escenario.fuente(b"DDS-C", 2.5)})

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.FALLIDA, RUTA_C: EfectoDeSalida.PUBLICADA}
    assert escenario.archivos_bajo_textures() == {RUTA_C}
    assert sorted(p.name for p in escenario.control().iterdir()) == ["manifest.json"]
    if fds_antes is not None:
        assert len(os.listdir("/proc/self/fd")) <= fds_antes


async def test_una_sesion_no_admite_dos_operaciones_a_la_vez(escenario: Escenario) -> None:
    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        resultados = await asyncio.gather(sesion.planificar([]), sesion.planificar([]), return_exceptions=True)

        rechazadas = [r for r in resultados if isinstance(r, SalidaNoEscribibleError)]
        assert len(rechazadas) == 1
        assert "ocupada" in str(rechazadas[0])
        await sesion.planificar([])  # y la sesión sigue sirviendo cuando la operación terminó


# ---------------------------------------------------------------------------
# El lock
# ---------------------------------------------------------------------------

_GUION_QUE_SOSTIENE_LA_SESION = """
import asyncio, pathlib, sys
sys.path.insert(0, {repo!r})
from sky_claw.local.native_parallax.output_writer import abrir_sesion_de_salida, construir_coordinacion_de_salida


async def principal(raiz, base, listo, soltar):
    coordinacion = construir_coordinacion_de_salida(base=pathlib.Path(base))
    try:
        async with abrir_sesion_de_salida(pathlib.Path(raiz), coordinacion=coordinacion):
            pathlib.Path(listo).write_text("1")
            while not pathlib.Path(soltar).exists():
                await asyncio.sleep(0.05)
    finally:
        await coordinacion.close()


asyncio.run(principal(*sys.argv[1:5]))
"""


async def test_una_segunda_sesion_en_otro_proceso_no_entra_mientras_la_primera_sostiene_la_raiz(
    escenario: Escenario, tmp_path: pathlib.Path
) -> None:
    """DOS PROCESOS reales: dos corrutinas del mismo intérprete verían exclusión aunque el lock fuera un ``threading.Lock``."""
    repo = str(pathlib.Path(output_writer.__file__).resolve().parents[3])
    guion = tmp_path / "sostener.py"
    guion.write_text(_GUION_QUE_SOSTIENE_LA_SESION.format(repo=repo), encoding="utf-8")
    listo, soltar = tmp_path / "listo", tmp_path / "soltar"
    proceso = subprocess.Popen(  # noqa: S603 - argv fijo, sin shell
        [sys.executable, str(guion), str(escenario.raiz), str(tmp_path / "estado"), str(listo), str(soltar)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(600):  # hasta 60 s para que el otro proceso tome el lock
            if listo.exists() or proceso.poll() is not None:
                break
            await asyncio.sleep(0.1)
        assert listo.exists(), proceso.communicate(timeout=10)[1].decode(errors="replace")

        impaciente = construir_coordinacion_de_salida(
            base=tmp_path / "estado", max_retries=2, backoff_base=0.01, backoff_max=0.02
        )
        try:
            with pytest.raises(SalidaOcupadaError, match="otro proceso"):
                async with abrir_sesion_de_salida(escenario.raiz, coordinacion=impaciente):
                    pytest.fail("no debería entrar mientras otro proceso sostiene la raíz")
        finally:
            await impaciente.close()
    finally:
        soltar.write_text("1")
        _, error = proceso.communicate(timeout=60)
    assert proceso.returncode == 0, error.decode(errors="replace")

    resultado = await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})  # liberado: ahora sí entra
    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.PUBLICADA}


async def test_dos_raices_distintas_no_se_excluyen_entre_si(escenario: Escenario, tmp_path: pathlib.Path) -> None:
    otros_mods = tmp_path / "otros_mods"
    otros_mods.mkdir()
    otra_raiz = otros_mods / NOMBRE_DEL_MOD_DE_SALIDA

    async with (
        abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion),
        abrir_sesion_de_salida(otra_raiz, coordinacion=escenario.coordinacion),
    ):
        pass


def test_el_recurso_del_lock_depende_de_la_raiz_normalizada(tmp_path: pathlib.Path) -> None:
    raiz = tmp_path / "mods" / NOMBRE_DEL_MOD_DE_SALIDA
    con_puntos = tmp_path / "mods" / ".." / "mods" / NOMBRE_DEL_MOD_DE_SALIDA
    otra = tmp_path / "otros" / NOMBRE_DEL_MOD_DE_SALIDA

    recurso = CoordinacionDeSalida.resource_id_de_la_raiz
    assert recurso(raiz) == recurso(con_puntos)
    assert recurso(raiz) != recurso(otra)
    # El formato es estable entre versiones: dos versiones de Sky-Claw sobre la misma raíz compiten por el MISMO recurso.
    assert re.fullmatch(r"native-parallax-output-[0-9a-f]{32}", recurso(raiz))


def test_el_estado_de_la_coordinacion_no_depende_del_cwd(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un lock en una base relativa al ``cwd`` no excluiría a dos instancias lanzadas desde directorios distintos."""
    from sky_claw.config import SystemPaths

    estado = tmp_path / "estado_durable"
    monkeypatch.setattr(SystemPaths, "runtime_state_dir", classmethod(lambda cls: estado))
    uno, otro = tmp_path / "uno", tmp_path / "otro"
    uno.mkdir()
    otro.mkdir()

    monkeypatch.chdir(uno)
    desde_uno = output_writer.ruta_de_estado_de_salida()
    monkeypatch.chdir(otro)
    desde_otro = output_writer.ruta_de_estado_de_salida()

    assert desde_uno == desde_otro == estado / "native_parallax_output"
    assert desde_uno.is_absolute()


async def test_si_se_pierde_el_lock_antes_de_aplicar_no_se_toca_el_disco(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        f = escenario.fuente(b"DDS-A")
        plan = await sesion.planificar([SalidaDeseada(RUTA_A, clave_de_cache(f.procedencia))])

        async def perdida(*args: Any, **kwargs: Any) -> None:
            raise LockLeaseLostError("lease perdida (simulada)")

        monkeypatch.setattr(sesion._lock, "assert_owned", perdida)
        with pytest.raises(LockLeaseLostError):
            await sesion.aplicar(plan, {RUTA_A: f})
        monkeypatch.undo()

    assert escenario.archivos_bajo_textures() == set()
    assert sorted(p.name for p in escenario.control().iterdir()) == []


async def test_si_se_pierde_el_lock_a_mitad_del_lote_no_se_ejecutan_las_acciones_que_faltan(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    lote = {
        RUTA_A: escenario.fuente(b"DDS-A"),
        RUTA_B: escenario.fuente(b"DDS-B", 2.5),
        RUTA_C: escenario.fuente(b"DDS-C", 3.5),
    }
    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        plan = await sesion.planificar([SalidaDeseada(r, clave_de_cache(f.procedencia)) for r, f in lote.items()])
        llamadas = {"n": 0}
        real = sesion._lock.assert_owned

        async def se_pierde_en_la_tercera(*args: Any, **kwargs: Any) -> None:
            llamadas["n"] += 1
            if llamadas["n"] >= 4:  # 1 al entrar a aplicar, 1 antes del pendiente, 1 por acción
                raise LockLeaseLostError("lease perdida (simulada)")
            await real(*args, **kwargs)

        monkeypatch.setattr(sesion._lock, "assert_owned", se_pierde_en_la_tercera)
        with pytest.raises(LockLeaseLostError):
            await sesion.aplicar(plan, lote)
        monkeypatch.undo()

    publicados = escenario.archivos_bajo_textures()
    assert len(publicados) == 1  # la primera acción se ejecutó; las otras dos no
    assert (escenario.control() / "pending.json").exists()  # y el corte quedó para recuperar

    resultado = await escenario.lote(lote)  # la sesión siguiente recupera y completa
    assert resultado.completo
    assert escenario.archivos_bajo_textures() == {RUTA_A, RUTA_B, RUTA_C}


# ---------------------------------------------------------------------------
# Cortes: el proceso puede morir en CUALQUIER punto y la sesión siguiente deja todo consistente
# ---------------------------------------------------------------------------


class CorteSimulado(BaseException):  # noqa: N818 - BaseException a propósito: nada del escritor debe poder tragárselo
    """El proceso murió acá."""


RUTA_D = "textures/otros/ajeno_p.dds"  # un archivo de un tercero que NUNCA se toca


async def _armar_el_estado_previo(e: Escenario) -> dict[str, FuenteDeSalida]:
    """Estado inicial: A y B nuestros y publicados, más un archivo ajeno D. Devuelve el lote que sigue."""
    await e.lote({RUTA_A: e.fuente(b"A-viejo", 1.5), RUTA_B: e.fuente(b"B-viejo", 2.5)})
    ajeno = e.disco(RUTA_D)
    ajeno.parent.mkdir(parents=True, exist_ok=True)
    ajeno.write_bytes(b"AJENO")
    # El lote que sigue: A se reemplaza, B se retira (ya no se desea) y C es nueva.
    return {RUTA_A: e.fuente(b"A-nuevo", 9.5), RUTA_C: e.fuente(b"C-nuevo", 3.5)}


def _contar_o_cortar(monkeypatch: pytest.MonkeyPatch, cortar_en: int | None, vistos: list[str]) -> None:
    def punto(nombre: str) -> None:
        vistos.append(nombre)
        if cortar_en is not None and len(vistos) == cortar_en:
            raise CorteSimulado(nombre)

    monkeypatch.setattr(output_writer, "_punto_de_corte", punto)


def _estado_esperado_final() -> dict[str, bytes]:
    return {RUTA_A: b"A-nuevo", RUTA_C: b"C-nuevo", RUTA_D: b"AJENO"}


def _comprobar_invariantes(e: Escenario, *, nuestros: set[str]) -> None:
    """Lo que debe valer tras CUALQUIER recuperación, haya llegado el lote a donde haya llegado."""
    assert e.leer(RUTA_D) == b"AJENO"  # lo de un tercero no se toca
    entradas = e.manifiesto_en_disco().entradas
    for entrada in entradas:  # cada entrada del manifiesto describe un archivo que existe con esos bytes
        assert _sha(e.leer(entrada.ruta)) == entrada.sha256
    en_manifiesto = {entrada.ruta for entrada in entradas}
    assert en_manifiesto <= nuestros
    assert e.archivos_bajo_textures() - {RUTA_D} == en_manifiesto  # nada nuestro queda huérfano
    assert sorted(p.name for p in e.control().iterdir()) == ["manifest.json"]  # ni pendiente, ni etapas, ni papelera


async def test_el_lote_de_referencia_pasa_por_todos_los_puntos_de_corte_del_lote(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    lote = await _armar_el_estado_previo(escenario)
    vistos: list[str] = []
    _contar_o_cortar(monkeypatch, None, vistos)

    resultado = await escenario.lote(lote)

    assert resultado.completo
    assert {r: escenario.leer(r) for r in (RUTA_A, RUTA_C, RUTA_D)} == _estado_esperado_final()
    assert not escenario.disco(RUTA_B).exists()
    assert set(vistos) == set(output_writer._PUNTOS_DE_CORTE) - {
        "recuperacion_tras_papelera",
        "recuperacion_tras_manifiesto",
    }
    # Y en este orden, uno por paso: el test de cortes corta en CADA uno, así que perder una llamada achicaría el test.
    assert vistos == [
        "tras_pendiente",
        *("tras_etapa", "tras_apartar", "tras_verificar", "tras_publicar"),  # A se reemplaza
        *("tras_apartar", "tras_borrar"),  # B se retira: verificar y borrar son una sola unidad (rehace la huella)
        *("tras_etapa", "tras_publicar"),  # C es nueva
        *("antes_del_compromiso", "tras_compromiso", "tras_limpieza"),
    ]
    _comprobar_invariantes(escenario, nuestros={RUTA_A, RUTA_C})


async def _corridas_de_referencia(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> int:
    (tmp_path / "referencia").mkdir()
    e = Escenario(tmp_path / "referencia")
    try:
        lote = await _armar_el_estado_previo(e)
        vistos: list[str] = []
        _contar_o_cortar(monkeypatch, None, vistos)
        await e.lote(lote)
        return len(vistos)
    finally:
        await e.cerrar()
        monkeypatch.undo()


async def test_un_corte_en_cualquier_punto_del_lote_se_recupera_y_el_lote_converge(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Se corta el proceso en CADA llamada a un punto de corte (no en una muestra) y se mira el estado tras recuperar."""
    total = await _corridas_de_referencia(tmp_path, monkeypatch)
    assert total >= 12  # si el lote deja de pasar por tantos puntos, el test dejó de medir lo que dice

    for k in range(1, total + 1):
        base = tmp_path / f"corte_{k}"
        base.mkdir()
        e = Escenario(base)
        try:
            lote = await _armar_el_estado_previo(e)
            vistos: list[str] = []
            _contar_o_cortar(monkeypatch, k, vistos)
            with pytest.raises(CorteSimulado):
                await e.lote(lote)
            monkeypatch.undo()
            punto = vistos[-1]

            # 1) Recuperar sin aplicar nada más ya deja todo consistente.
            async with abrir_sesion_de_salida(e.raiz, coordinacion=e.coordinacion):
                pass
            _comprobar_invariantes(e, nuestros={RUTA_A, RUTA_B, RUTA_C})

            # 2) Y repetir el lote llega al estado final de la corrida sin cortes.
            resultado = await e.lote(lote)
            assert resultado.completo, (k, punto)
            assert {r: e.leer(r) for r in (RUTA_A, RUTA_C, RUTA_D)} == _estado_esperado_final(), (k, punto)
            assert not e.disco(RUTA_B).exists(), (k, punto)
            _comprobar_invariantes(e, nuestros={RUTA_A, RUTA_C})
        finally:
            monkeypatch.undo()
            await e.cerrar()


async def test_un_segundo_corte_durante_la_recuperacion_tambien_se_recupera(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corte en el lote y OTRO corte en cada punto de la recuperación: la tercera sesión converge igual."""
    total = await _corridas_de_referencia(tmp_path, monkeypatch)
    cortes_de_recuperacion = ("recuperacion_tras_papelera", "recuperacion_tras_manifiesto")
    alguna_vez_se_corto_la_recuperacion = False

    for k in range(1, total + 1):
        for nombre in cortes_de_recuperacion:
            base = tmp_path / f"doble_{k}_{nombre}"
            base.mkdir()
            e = Escenario(base)
            try:
                lote = await _armar_el_estado_previo(e)
                _contar_o_cortar(monkeypatch, k, [])
                with pytest.raises(CorteSimulado):
                    await e.lote(lote)
                monkeypatch.undo()

                original = output_writer._punto_de_corte

                def cortar_recuperacion(punto: str, nombre: str = nombre, original: Any = original) -> None:
                    original(punto)
                    if punto == nombre:
                        raise CorteSimulado(punto)

                monkeypatch.setattr(output_writer, "_punto_de_corte", cortar_recuperacion)
                try:
                    async with abrir_sesion_de_salida(e.raiz, coordinacion=e.coordinacion):
                        pass
                except CorteSimulado:
                    alguna_vez_se_corto_la_recuperacion = True
                monkeypatch.undo()

                resultado = await e.lote(lote)
                assert resultado.completo, (k, nombre)
                assert {r: e.leer(r) for r in (RUTA_A, RUTA_C, RUTA_D)} == _estado_esperado_final(), (k, nombre)
                _comprobar_invariantes(e, nuestros={RUTA_A, RUTA_C})
            finally:
                monkeypatch.undo()
                await e.cerrar()

    assert alguna_vez_se_corto_la_recuperacion  # el test llegó a cortar de verdad una recuperación


# ---------------------------------------------------------------------------
# Archivos de control ilegibles: se falla cerrado sin tocar nada
# ---------------------------------------------------------------------------


#: Cómo nombra el escritor a cada archivo de control en sus mensajes.
_QUE = {"manifest.json": "el manifiesto", "pending.json": "el manifiesto pendiente"}


def _instantanea(raiz: pathlib.Path) -> dict[str, bytes | None]:
    return {p.relative_to(raiz).as_posix(): (p.read_bytes() if p.is_file() else None) for p in sorted(raiz.rglob("*"))}


@pytest.mark.parametrize("nombre", ["manifest.json", "pending.json"])
@pytest.mark.parametrize(
    "contenido",
    [b"", b"no es json", b"{}", b'{"esquema": "otro", "mod": "x", "entradas": []}', b"\xff\xfe\x00"],
    ids=["vacio", "texto", "objeto_vacio", "esquema_ajeno", "binario"],
)
async def test_un_archivo_de_control_ilegible_se_rechaza_sin_tocar_nada(
    escenario: Escenario, nombre: str, contenido: bytes
) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    (escenario.control() / nombre).write_bytes(contenido)
    antes = _instantanea(escenario.raiz)

    with pytest.raises(SalidaNoEscribibleError, match=f"^{_QUE[nombre]} es ilegible"):
        async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion):
            pytest.fail("no debería entrar")

    assert _instantanea(escenario.raiz) == antes


@pytest.mark.parametrize("nombre", ["manifest.json", "pending.json"])
async def test_un_archivo_de_control_que_es_un_directorio_se_rechaza(escenario: Escenario, nombre: str) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    if nombre == "manifest.json":
        (escenario.control() / nombre).unlink()
    (escenario.control() / nombre).mkdir()

    with pytest.raises(SalidaNoEscribibleError, match=f"^{_QUE[nombre]} no es un archivo regular"):
        async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion):
            pytest.fail("no debería entrar")


async def test_un_manifiesto_ilegible_no_se_pisa_al_aplicar_un_lote_nuevo(escenario: Escenario) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    (escenario.control() / "manifest.json").write_bytes(b"basura")

    with pytest.raises(SalidaNoEscribibleError):
        await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})

    assert (escenario.control() / "manifest.json").read_bytes() == b"basura"
    assert escenario.leer(RUTA_A) == b"DDS-A"


async def test_la_recuperacion_borra_los_temporales_y_las_etapas_huerfanas_pero_no_toca_la_papelera(
    escenario: Escenario,
) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    control = escenario.control()
    (control / "staging").mkdir()
    (control / "staging" / "copia").write_bytes(b"x")
    (control / ".manifest.json.abc.tmp").write_bytes(b"x")
    (control / "trash").mkdir()
    (control / "trash" / "desconocido").write_bytes(b"NO ES NUESTRO")

    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion):
        pass

    assert sorted(p.name for p in control.iterdir()) == ["manifest.json", "trash"]
    assert (control / "trash" / "desconocido").read_bytes() == b"NO ES NUESTRO"


def _apartar_a_mano(escenario: Escenario, ruta: str, contenido: bytes) -> pathlib.Path:
    """Deja en la casilla de *ruta* de la papelera un archivo con *contenido* (lo que dejaría un corte tras apartar)."""
    papelera = escenario.control() / "trash"
    papelera.mkdir(exist_ok=True)
    apartado = papelera / output_writer._id_de_ruta(ruta)
    apartado.write_bytes(contenido)
    return apartado


async def test_lo_apartado_que_no_es_nuestro_nunca_se_borra_y_con_la_ruta_vacia_vuelve_a_su_lugar(
    escenario: Escenario, caplog: pytest.LogCaptureFixture
) -> None:
    """Una huella en la papelera solo es nuestra si es la de la entrada vigente; lo demás no se borra nunca.

    Con la ruta vacía, lo apartado vuelve a ella: es lo que el escritor hace sin corte cuando lo que apartó no era lo
    autorizado (``_devolver``). La recuperación es el camino gemelo y cumple la misma propiedad.
    """
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    (escenario.control() / "pending.json").write_bytes(
        serializar_manifiesto(Manifiesto(entradas=()))  # intención: retirar A
    )
    escenario.disco(RUTA_A).unlink()  # y A ya no está en su ruta...
    _apartar_a_mano(escenario, RUTA_A, b"DE-OTRO-QUE-NO-ES-A")  # ...sino en la papelera, con bytes de un tercero

    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        assert sesion.manifiesto.entradas == ()

    assert escenario.leer(RUTA_A) == b"DE-OTRO-QUE-NO-ES-A"
    assert escenario.manifiesto_en_disco().entradas == ()  # vuelve, pero no es nuestro
    assert sorted(p.name for p in escenario.control().iterdir()) == ["manifest.json"]
    assert "No se pudo devolver" not in caplog.text  # se devolvió: no hay nada que avisar


async def test_lo_apartado_que_no_es_nuestro_con_la_ruta_ocupada_se_queda_en_la_papelera(
    escenario: Escenario, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=output_writer.logger.name)
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    (escenario.control() / "pending.json").write_bytes(serializar_manifiesto(Manifiesto(entradas=())))
    escenario.disco(RUTA_A).write_bytes(b"LO-QUE-HAY-AHORA")
    apartado = _apartar_a_mano(escenario, RUTA_A, b"DE-OTRO-QUE-NO-ES-A")

    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        assert sesion.manifiesto.entradas == ()

    assert apartado.read_bytes() == b"DE-OTRO-QUE-NO-ES-A"
    assert escenario.leer(RUTA_A) == b"LO-QUE-HAY-AHORA"
    assert not (escenario.control() / "pending.json").exists()
    assert "1 archivo(s) que no son nuestros" in caplog.text  # lo que queda en la papelera se avisa


@pytest.mark.parametrize("accion", ["reconstruir", "retirar"])
async def test_un_corte_tras_apartar_lo_que_edito_un_tercero_se_lo_devuelve_al_recuperar(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch, accion: str
) -> None:
    """El escritor aparta ANTES de verificar. Si un tercero editó el archivo después de planificar y el proceso muere
    justo después de apartarlo, la recuperación tiene que devolverlo: si no, el archivo del usuario queda encerrado en la
    papelera y su ruta vacía. Se enumeran las dos acciones que apartan (reemplazar y retirar), no una muestra."""
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-viejo", 1.5)})
    lote = {RUTA_A: escenario.fuente(b"DDS-nuevo", 9.5)} if accion == "reconstruir" else {}

    def punto(nombre: str) -> None:
        if nombre == "tras_apartar":
            raise CorteSimulado(nombre)

    monkeypatch.setattr(output_writer, "_punto_de_corte", punto)
    with pytest.raises(CorteSimulado):
        await _planificar_y_aplicar(
            escenario, lote, entre=lambda: escenario.disco(RUTA_A).write_bytes(b"EDITADO-A-MANO")
        )
    monkeypatch.undo()

    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        assert sesion.manifiesto.entradas == ()

    assert escenario.leer(RUTA_A) == b"EDITADO-A-MANO"
    assert escenario.manifiesto_en_disco().entradas == ()
    assert sorted(p.name for p in escenario.control().iterdir()) == ["manifest.json"]


# ---------------------------------------------------------------------------
# Borrar de la papelera: la huella se vuelve a calcular justo antes
# ---------------------------------------------------------------------------


async def test_la_recuperacion_no_descarta_lo_que_cambio_en_la_papelera_despues_de_observarlo(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Entre que la recuperación observa la papelera y borra lo que decidió descartar, el archivo puede cambiar."""
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    (escenario.control() / "pending.json").write_bytes(serializar_manifiesto(Manifiesto(entradas=())))
    escenario.disco(RUTA_A).unlink()
    apartado = _apartar_a_mano(escenario, RUTA_A, b"DDS-A")  # retiro cortado: lo apartado es nuestro
    original = output_writer.reconciliar_tras_corte

    def reconciliar(*args: Any) -> Any:
        recuperacion = original(*args)
        apartado.write_bytes(b"LLEGO-OTRO-A-LA-PAPELERA")  # después de observar, antes de descartar
        return recuperacion

    monkeypatch.setattr(output_writer, "reconciliar_tras_corte", reconciliar)

    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        assert sesion.manifiesto.entradas == ()

    assert apartado.read_bytes() == b"LLEGO-OTRO-A-LA-PAPELERA"
    assert "No se descartó" in caplog.text


async def test_el_reemplazo_no_borra_el_original_si_cambio_en_la_papelera_despues_de_verificarlo(
    escenario: Escenario, interponer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Entre verificar el original apartado y borrarlo se publica el reemplazo: el borrado vuelve a mirar la huella."""
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-viejo", 1.5)})
    apartado = escenario.control() / "trash" / output_writer._id_de_ruta(RUTA_A)
    interponer("tras_publicar", lambda: apartado.write_bytes(b"LLEGO-OTRO-A-LA-PAPELERA"))

    resultado = await escenario.lote({RUTA_A: escenario.fuente(b"DDS-nuevo", 9.5)})

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.REEMPLAZADA}
    assert "cambió después de verificarlo" in resultado.rutas[0].detalle
    assert escenario.leer(RUTA_A) == b"DDS-nuevo"
    assert apartado.read_bytes() == b"LLEGO-OTRO-A-LA-PAPELERA"
    assert "cambió después de verificarlo" in caplog.text


# ---------------------------------------------------------------------------
# Un lote cuyo resultado tras un corte no se podría escribir se rechaza antes de tocar nada
# ---------------------------------------------------------------------------


RUTA_A2 = "textures/architecture/whiterun/muro03_p.dds"
RUTA_B2 = "textures/architecture/whiterun/muro04_p.dds"


async def test_un_lote_cuya_union_con_el_vigente_no_cabe_se_rechaza_sin_tocar_nada(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El vigente y el pendiente caben cada uno por su lado, pero un corte puede dejar la UNIÓN (A y B todavía sin
    retirar, A2 y B2 ya publicadas): si esa unión no se puede serializar, la recuperación fallaría en cada arranque."""
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A", 1.5), RUTA_B: escenario.fuente(b"DDS-B", 2.5)})
    tope = len((escenario.control() / "manifest.json").read_bytes())  # el pendiente pesa exactamente lo mismo
    monkeypatch.setattr(output_writer, "TAMANO_MAXIMO_DEL_MANIFIESTO", tope)
    antes = _instantanea(escenario.raiz)

    with pytest.raises(SalidaNoEscribibleError, match="dividí el lote"):
        await escenario.lote({RUTA_A2: escenario.fuente(b"DDS-C", 3.5), RUTA_B2: escenario.fuente(b"DDS-D", 4.5)})

    assert _instantanea(escenario.raiz) == antes


async def test_el_tope_se_compara_con_la_cota_del_vigente_y_el_pendiente_del_lote(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """En el borde: un tope igual a la cota del (vigente, pendiente) alcanza y uno menos no."""
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A", 1.5), RUTA_B: escenario.fuente(b"DDS-B", 2.5)})
    pendiente = Manifiesto.ordenado(
        [
            crear_entrada(RUTA_A2, _sha(b"DDS-C"), len(b"DDS-C"), _procedencia(3.5)),
            crear_entrada(RUTA_B2, _sha(b"DDS-D"), len(b"DDS-D"), _procedencia(4.5)),
        ]
    )
    cota = cota_del_manifiesto_tras_un_corte(escenario.manifiesto_en_disco(), pendiente)
    lote = {RUTA_A2: escenario.fuente(b"DDS-C", 3.5), RUTA_B2: escenario.fuente(b"DDS-D", 4.5)}

    monkeypatch.setattr(output_writer, "TAMANO_MAXIMO_DEL_MANIFIESTO", cota - 1)
    with pytest.raises(SalidaNoEscribibleError, match=f"hasta {cota} bytes"):
        await escenario.lote(lote)

    monkeypatch.setattr(output_writer, "TAMANO_MAXIMO_DEL_MANIFIESTO", cota)
    resultado = await escenario.lote(lote)

    assert resultado.completo
    assert escenario.manifiesto_en_disco() == pendiente


# ---------------------------------------------------------------------------
# La sesión no se puede fabricar a mano
# ---------------------------------------------------------------------------


def test_la_sesion_solo_se_construye_desde_el_gestor_de_contexto(tmp_path: pathlib.Path) -> None:
    with pytest.raises(TypeError, match="abrir_sesion_de_salida"):
        SesionDeSalida(object(), raiz=tmp_path, lock=None, vigente=None)


# ---------------------------------------------------------------------------
# Anclas: el lock solo excluye si TODOS los mutadores lo toman (se enumeran, no se muestrean)
# ---------------------------------------------------------------------------

_MODULO = pathlib.Path(output_writer.__file__)
_PAQUETE = _MODULO.parent
_MUTADORES_DE_OS = frozenset(
    {
        "rename",
        "link",
        "unlink",
        "replace",
        "rmdir",
        "mkdir",
        "makedirs",
        "open",
        "remove",
        "symlink",
        "truncate",
        "chmod",
        "utime",
        "removedirs",
        "fdopen",
    }
)
_MUTADORES_DE_PATH = frozenset(
    {"unlink", "rmdir", "mkdir", "write_bytes", "write_text", "rename", "replace", "touch", "symlink_to", "hardlink_to"}
)


def _arbol() -> ast.Module:
    return ast.parse(_MODULO.read_text(encoding="utf-8"))


class _Recorrido(ast.NodeVisitor):
    """Para cada nodo ``Name``/``Attribute`` interesante, en qué función (la más interna) aparece."""

    def __init__(self) -> None:
        self.pila: list[str] = []
        self.mutadores: dict[str, set[str]] = {}
        self.nombres: list[tuple[str, str]] = []  # (nombre referenciado, función que lo referencia)

    def _funcion(self, nodo: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.pila.append(nodo.name)
        self.generic_visit(nodo)
        self.pila.pop()

    visit_FunctionDef = _funcion  # noqa: N815 - nombre fijado por ast.NodeVisitor
    visit_AsyncFunctionDef = _funcion  # noqa: N815

    def visit_Attribute(self, nodo: ast.Attribute) -> None:
        dueno = self.pila[-1] if self.pila else "<módulo>"
        if isinstance(nodo.value, ast.Name) and nodo.value.id == "os" and nodo.attr in _MUTADORES_DE_OS:
            self.mutadores.setdefault(dueno, set()).add(f"os.{nodo.attr}")
        elif nodo.attr in _MUTADORES_DE_PATH and not (isinstance(nodo.value, ast.Name) and nodo.value.id == "os"):
            self.mutadores.setdefault(dueno, set()).add(f".{nodo.attr}")
        self.generic_visit(nodo)

    def visit_Name(self, nodo: ast.Name) -> None:
        self.nombres.append((nodo.id, self.pila[-1] if self.pila else "<módulo>"))


def _recorrer() -> _Recorrido:
    recorrido = _Recorrido()
    recorrido.visit(_arbol())
    return recorrido


def test_all_esta_congelado() -> None:
    assert set(output_writer.__all__) == {
        "CoordinacionDeSalida",
        "EfectoDeSalida",
        "FuenteDeSalida",
        "ResultadoDeAplicacion",
        "ResultadoPorRuta",
        "SalidaNoEscribibleError",
        "SalidaOcupadaError",
        "SesionDeSalida",
        "abrir_sesion_de_salida",
        "construir_coordinacion_de_salida",
        "ruta_de_estado_de_salida",
    }


def test_las_funciones_que_mutan_el_disco_estan_enumeradas_y_son_todas_privadas() -> None:
    """Agregar una función que toque el disco sin pasar por acá rompe el ancla: decidí si participa del lock."""
    mutadores = set(_recorrer().mutadores)

    # Las que crean carpetas lo hacen a través de ``_crear_carpeta_contenida`` (ver el ancla de contención), así que
    # ``_apartar``, ``_preparar_padre``, ``_restaurar_desde_papelera`` y ``_asegurar_directorio_de_control`` ya no llaman
    # al sistema de archivos directamente. Lo mismo ``_retirar``, que borra de la papelera a través de
    # ``_borrar_si_es_el_autorizado`` (ver el censo de ``os.unlink``).
    assert mutadores == {
        "_borrar_pendiente",
        "_borrar_si_es_el_autorizado",
        "_construir",
        "_copiar_a_etapa",
        "_crear_carpeta_contenida",
        "_escribir_atomico",
        "_limpiar_control",
        "_mover_sin_reemplazar",
        "_podar_directorios_vacios",
        "_recuperar",
        "_reemplazar",
        # Crea el directorio de estado de los locks (el del usuario), no toca el mod de salida.
        "construir_coordinacion_de_salida",
    }
    publicas = {nombre for nombre in mutadores if not nombre.startswith("_")}
    assert publicas == {"construir_coordinacion_de_salida"}


def test_cada_mutador_solo_se_alcanza_desde_donde_el_lock_esta_tomado() -> None:
    """``_recuperar`` y las cuatro unidades que escriben corren DENTRO de la sesión; nadie más las nombra."""
    quien_nombra: dict[str, set[str]] = {}
    for nombre, funcion in _recorrer().nombres:
        quien_nombra.setdefault(nombre, set()).add(funcion)

    assert quien_nombra["_recuperar"] == {"abrir_sesion_de_salida"}
    assert quien_nombra["_escribir_atomico"] == {"_ejecutar", "_comprometer_sin_disco", "_recuperar"}
    assert quien_nombra["_actuar_en_disco"] == {"_ejecutar"}
    assert quien_nombra["_limpiar_control"] == {"_ejecutar", "_recuperar"}
    assert quien_nombra["_borrar_pendiente"] == {"_ejecutar", "_recuperar"}
    # TODO borrado de la papelera mira la huella en ese mismo instante: los tres (descartar en la recuperación, el
    # original de un reemplazo y lo que se retira), no uno ni dos. El tercero lo encontró la revisión de #782.
    assert quien_nombra["_borrar_si_es_el_autorizado"] == {"_recuperar", "_reemplazar", "_retirar"}
    assert quien_nombra["SesionDeSalida"] >= {"abrir_sesion_de_salida"}
    assert {f for f in quien_nombra["SesionDeSalida"] if f != "<módulo>"} <= {"abrir_sesion_de_salida"}


def test_la_recuperacion_corre_dentro_del_bloque_que_sostiene_el_lock() -> None:
    funcion = next(
        n for n in ast.walk(_arbol()) if isinstance(n, ast.AsyncFunctionDef) and n.name == "abrir_sesion_de_salida"
    )
    bloques = [n for n in ast.walk(funcion) if isinstance(n, ast.AsyncWith)]
    assert len(bloques) == 1
    sostiene = [
        c
        for item in bloques[0].items
        for c in ast.walk(item.context_expr)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "sostener"
    ]
    assert sostiene, "el bloque no llama a sostener()"
    nombres_dentro = {n.id for stmt in bloques[0].body for n in ast.walk(stmt) if isinstance(n, ast.Name)}
    assert {"_recuperar", "SesionDeSalida"} <= nombres_dentro
    afuera = {n.id for n in ast.walk(funcion) if isinstance(n, ast.Name)} - nombres_dentro
    assert "_recuperar" not in afuera


@pytest.mark.parametrize("camino", ["con_disco", "sin_disco"])
async def test_cada_unidad_que_escribe_va_precedida_de_una_comprobacion_del_lock(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch, camino: str
) -> None:
    """En los DOS caminos que escriben (un lote con las tres acciones sobre el disco, y uno que solo cambia el manifiesto):
    antes de CADA unidad que escribe, ``assert_owned`` acaba de pasar. Son hermanos: el lock tiene que estar en los dos."""
    if camino == "con_disco":
        lote = await _armar_el_estado_previo(escenario)
        minimo = 7  # pendiente, 3 acciones, manifiesto, limpieza y borrado del pendiente
    else:
        await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
        escenario.disco(RUTA_A).unlink()  # el lote que sigue la suelta: solo cambia el manifiesto
        lote = {}
        minimo = 1
    traza: list[str] = []
    asercion = SnapshotTransactionLock.assert_owned

    async def asercion_registrada(self: SnapshotTransactionLock, *, verify_db: bool = True) -> None:
        await asercion(self, verify_db=verify_db)
        traza.append("lock")

    monkeypatch.setattr(SnapshotTransactionLock, "assert_owned", asercion_registrada)
    unidades = ("_escribir_atomico", "_actuar_en_disco", "_limpiar_control", "_borrar_pendiente")
    for nombre in unidades:
        original = getattr(output_writer, nombre)

        def registrada(*args: Any, _nombre: str = nombre, _original: Any = original, **kwargs: Any) -> Any:
            traza.append(_nombre)
            return _original(*args, **kwargs)

        monkeypatch.setattr(output_writer, nombre, registrada)

    resultado = await escenario.lote(lote)

    assert resultado.completo
    # La recuperación corre justo después de tomar el lock (adentro del bloque que lo sostiene), antes del primer
    # ``assert_owned``; lo que se mide es el lote que viene después de planificar.
    lote_aplicado = traza[traza.index("lock") :]
    escritoras = [i for i, evento in enumerate(lote_aplicado) if evento in unidades]
    assert len(escritoras) >= minimo
    assert [lote_aplicado[i - 1] for i in escritoras] == ["lock"] * len(escritoras), traza


def test_los_puntos_de_corte_nombrados_en_el_codigo_son_los_congelados() -> None:
    nombrados = {
        c.args[0].value
        for c in ast.walk(_arbol())
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Name)
        and c.func.id == "_punto_de_corte"
        and c.args
        and isinstance(c.args[0], ast.Constant)
    }
    # Los que nombra el código van por el _en_hilo(_punto_de_corte, "x") además de la llamada directa:
    nombrados |= {
        c.args[1].value
        for c in ast.walk(_arbol())
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Name)
        and c.func.id == "_en_hilo"
        and len(c.args) > 1
        and isinstance(c.args[0], ast.Name)
        and c.args[0].id == "_punto_de_corte"
        and isinstance(c.args[1], ast.Constant)
    }

    assert nombrados == set(output_writer._PUNTOS_DE_CORTE)
    assert len(output_writer._PUNTOS_DE_CORTE) == len(set(output_writer._PUNTOS_DE_CORTE))


def test_el_modulo_no_importa_numpy_ni_pillow_ni_research() -> None:
    importados = {
        (a.name if isinstance(n, ast.Import) else (n.module or "")).split(".")[0]
        for n in ast.walk(_arbol())
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in (n.names if isinstance(n, ast.Import) else [ast.alias(name=n.module or "")])
    }

    assert not importados & {"numpy", "PIL", "scipy"}
    assert not any(
        isinstance(n, ast.ImportFrom) and n.module and n.module.endswith("research") for n in ast.walk(_arbol())
    )


def test_solo_el_escritor_del_paquete_usa_el_lock_distribuido() -> None:
    """Un segundo módulo del paquete que tomara su propio lock sobre la salida reabriría el defecto del hermano."""
    quienes = {
        ruta.name
        for ruta in _PAQUETE.rglob("*.py")
        if "DistributedLockManager" in ruta.read_text(encoding="utf-8")
        or "SnapshotTransactionLock" in ruta.read_text(encoding="utf-8")
    }

    assert quienes == {"output_writer.py"}


def test_la_coordinacion_no_expone_el_manager_por_ninguna_via_sincrona() -> None:
    publicos = {n for n in dir(CoordinacionDeSalida) if not n.startswith("_")}

    assert publicos == {"close", "initialize", "resource_id_de_la_raiz", "sostener"}
    assert not any(isinstance(v, property) for v in vars(CoordinacionDeSalida).values())
    assert inspect.iscoroutinefunction(CoordinacionDeSalida.initialize)
    assert inspect.isasyncgenfunction(CoordinacionDeSalida.sostener.__wrapped__)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Contratos que la mutación encontró sin probar (agrupados por propiedad, no por mutante)
# ---------------------------------------------------------------------------


def test_los_efectos_estan_congelados() -> None:
    """Los valores viajan al resultado que verá NP-A0: cambiar uno sin querer rompe a quien lo lea."""
    assert {e.name: e.value for e in EfectoDeSalida} == {
        "PUBLICADA": "publicada",
        "REEMPLAZADA": "reemplazada",
        "RETIRADA": "retirada",
        "OMITIDA": "omitida",
        "PRESERVADA": "preservada",
        "SOLTADA": "soltada",
        "CONFLICTO": "conflicto",
        "FALLIDA": "fallida",
    }


@pytest.mark.parametrize(
    "objeto",
    [
        ResultadoPorRuta(RUTA_A, EfectoDeSalida.PUBLICADA, ""),
        ResultadoDeAplicacion(rutas=(), manifiesto=Manifiesto(entradas=())),
        FuenteDeSalida(archivo=pathlib.Path("fuente.dds"), procedencia=_procedencia()),
        output_writer._Trabajo(
            entrada=crear_entrada(RUTA_A, H_X, 1, _procedencia()), fuente=pathlib.Path("fuente.dds")
        ),
    ],
    ids=["ResultadoPorRuta", "ResultadoDeAplicacion", "FuenteDeSalida", "_Trabajo"],
)
def test_los_valores_del_escritor_son_inmutables_y_sin_diccionario(objeto: Any) -> None:
    campo = dataclasses.fields(objeto)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(objeto, campo, None)
    assert not hasattr(objeto, "__dict__")


def test_una_fuente_valida_sus_campos() -> None:
    with pytest.raises(SalidaNoEscribibleError, match=r"debe ser una ruta \(Path\)"):
        FuenteDeSalida(archivo="fuente.dds", procedencia=_procedencia())  # type: ignore[arg-type]
    with pytest.raises(SalidaNoEscribibleError, match="la procedencia de la fuente"):
        FuenteDeSalida(archivo=pathlib.Path("fuente.dds"), procedencia="p")  # type: ignore[arg-type]


async def test_una_fuente_que_no_es_una_fuente_de_salida_se_rechaza(escenario: Escenario) -> None:
    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        f = escenario.fuente(b"DDS-A")
        plan = await sesion.planificar([SalidaDeseada(RUTA_A, clave_de_cache(f.procedencia))])

        with pytest.raises(SalidaNoEscribibleError, match="debe ser una FuenteDeSalida"):
            await sesion.aplicar(plan, {RUTA_A: f.archivo})  # type: ignore[dict-item]

    assert escenario.archivos_bajo_textures() == set()


@pytest.mark.parametrize("caso", ["desmedida", "no_se_puede_inspeccionar", "no_se_puede_leer"])
async def test_una_fuente_que_no_se_puede_hashear_se_rechaza_antes_de_tocar_nada(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch, caso: str
) -> None:
    f = escenario.fuente(b"DDS-A")
    if caso == "desmedida":
        monkeypatch.setattr(output_writer, "_TAMANO_MAXIMO_DE_ARCHIVO", len(b"DDS-A") - 1)
        mensaje = "pesa más de 4 bytes"
    elif caso == "no_se_puede_inspeccionar":
        real = output_writer.link_kind_and_identity_or_raise_with_retry

        def inspeccionar(ruta: pathlib.Path) -> Any:
            if ruta == f.archivo:
                raise PermissionError("bloqueado (simulado)")
            return real(ruta)

        monkeypatch.setattr(output_writer, "link_kind_and_identity_or_raise_with_retry", inspeccionar)
        mensaje = "no se pudo inspeccionar la fuente"
    else:
        real_huella = output_writer._huella_de_archivo

        def huella(ruta: pathlib.Path) -> str:
            if ruta == f.archivo:
                raise PermissionError("bloqueado (simulado)")
            return real_huella(ruta)

        monkeypatch.setattr(output_writer, "_huella_de_archivo", huella)
        mensaje = "no se pudo leer la fuente"

    with pytest.raises(SalidaNoEscribibleError, match=mensaje):
        await escenario.lote({RUTA_A: f})

    assert escenario.archivos_bajo_textures() == set()


async def test_una_fuente_exactamente_en_el_tamano_maximo_se_publica(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(output_writer, "_TAMANO_MAXIMO_DE_ARCHIVO", len(b"DDS-A"))

    resultado = await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.PUBLICADA}
    assert escenario.manifiesto_en_disco().entradas[0].sha256 == _sha(b"DDS-A")


def test_la_huella_de_un_archivo_desmedido_no_es_la_de_ninguna_entrada(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(output_writer, "_TAMANO_MAXIMO_DE_ARCHIVO", 4)
    justo, de_mas = tmp_path / "justo.dds", tmp_path / "de_mas.dds"
    justo.write_bytes(b"1234")
    de_mas.write_bytes(b"12345")

    assert output_writer._huella_de_archivo(justo) == _sha(b"1234")
    assert output_writer._huella_de_archivo(de_mas) == output_writer._HUELLA_DE_ARCHIVO_DESMEDIDO


def test_observar_una_ruta_no_sigue_enlaces_y_ante_la_duda_dice_otro(
    escenario: Escenario, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raiz = escenario.raiz
    for ruta in (RUTA_A, RUTA_B, RUTA_C):
        escenario.disco(ruta).parent.mkdir(parents=True, exist_ok=True)
    assert output_writer._observar_ruta(raiz, RUTA_A) == Observacion(TipoObservado.AUSENTE, None)
    escenario.disco(RUTA_A).write_bytes(b"DDS-A")
    assert output_writer._observar_ruta(raiz, RUTA_A) == Observacion(TipoObservado.ARCHIVO, _sha(b"DDS-A"))
    escenario.disco(RUTA_B).mkdir()
    assert output_writer._observar_ruta(raiz, RUTA_B) == Observacion(TipoObservado.OTRO, None)
    real = tmp_path / "real.dds"
    real.write_bytes(b"DDS-A")
    _enlazar_o_saltear(real, escenario.disco(RUTA_C))
    assert output_writer._observar_ruta(raiz, RUTA_C) == Observacion(TipoObservado.OTRO, None)  # no se sigue

    def ilegible(ruta: pathlib.Path) -> str:
        raise PermissionError("bloqueado (simulado)")

    monkeypatch.setattr(output_writer, "_huella_de_archivo", ilegible)
    assert output_writer._observar_ruta(raiz, RUTA_A) == Observacion(TipoObservado.OTRO, None)


# --- La intención y el compromiso de un lote con TODAS las acciones (y los fallos previos) ---------------------------

_LARGA = "x" * 60
RUTA_E = "textures/e/editado_p.dds"  # nuestra, la edita un tercero: se preserva
RUTA_F = f"textures/f/{_LARGA}_p.dds"  # se quiere reemplazar pero su ruta ya no cabe: conserva su entrada
RUTA_G = "textures/g/borrado_p.dds"  # nuestra, la borra un tercero y ya no se desea: se suelta
RUTA_H = f"textures/h/{_LARGA}_p.dds"  # nuestra, la borra un tercero, se quiere de nuevo y no cabe: sale
RUTA_L = f"textures/l/{_LARGA}_p.dds"  # nueva y no cabe: nunca entra
RUTA_N = "textures/n/nuevo_p.dds"  # nueva: se publica


async def test_la_intencion_y_el_compromiso_de_un_lote_con_todas_las_acciones(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch, interponer: Any
) -> None:
    """El pendiente es EXACTAMENTE lo que quedaría si todo sale bien, y el manifiesto final es lo que quedó.

    El lote tiene las seis acciones del plan más tres fallos previos (rutas que no caben): un reemplazo que falla
    conserva su entrada y una construcción que falla no deja nada, aunque la ruta ya tuviera una entrada.
    """
    e = escenario
    await e.lote(
        {
            RUTA_A: e.fuente(b"A", 1.5),
            RUTA_B: e.fuente(b"B", 2.5),
            RUTA_C: e.fuente(b"C", 3.5),
            RUTA_E: e.fuente(b"E", 4.5),
            RUTA_F: e.fuente(b"F", 5.5),
            RUTA_G: e.fuente(b"G", 6.5),
            RUTA_H: e.fuente(b"H", 7.5),
        }
    )
    previo = {entrada.ruta: entrada for entrada in e.manifiesto_en_disco().entradas}
    e.disco(RUTA_E).write_bytes(b"EDITADO-A-MANO")
    e.disco(RUTA_G).unlink()
    e.disco(RUTA_H).unlink()
    cortas = (RUTA_A, RUTA_B, RUTA_C, RUTA_E, RUTA_G, RUTA_N)
    monkeypatch.setattr(
        output_writer, "_LARGO_MAXIMO_DE_RUTA_COMPLETA", max(len(os.fspath(e.disco(r))) for r in cortas)
    )
    pendientes: list[Manifiesto] = []
    interponer(
        "tras_pendiente", lambda: pendientes.append(leer_manifiesto((e.control() / "pending.json").read_bytes()))
    )

    resultado = await e.lote(
        {
            RUTA_A: e.fuente(b"A", 1.5),  # omitir
            RUTA_B: e.fuente(b"B2", 8.5),  # reemplazar
            RUTA_E: e.fuente(b"E2", 9.5),  # preservar (la editó un tercero)
            RUTA_F: e.fuente(b"F2", 10.5),  # reemplazar, pero no cabe
            RUTA_H: e.fuente(b"H", 7.5),  # construir (falta en disco), pero no cabe
            RUTA_L: e.fuente(b"L", 12.5),  # construir, pero no cabe
            RUTA_N: e.fuente(b"N", 11.5),  # construir
        }  # C se retira y G se suelta
    )

    assert _efectos(resultado) == {
        RUTA_A: EfectoDeSalida.OMITIDA,
        RUTA_B: EfectoDeSalida.REEMPLAZADA,
        RUTA_C: EfectoDeSalida.RETIRADA,
        RUTA_E: EfectoDeSalida.PRESERVADA,
        RUTA_F: EfectoDeSalida.FALLIDA,
        RUTA_G: EfectoDeSalida.SOLTADA,
        RUTA_H: EfectoDeSalida.FALLIDA,
        RUTA_L: EfectoDeSalida.FALLIDA,
        RUTA_N: EfectoDeSalida.PUBLICADA,
    }
    esperado = Manifiesto.ordenado(
        [
            previo[RUTA_A],
            crear_entrada(RUTA_B, _sha(b"B2"), 2, _procedencia(8.5)),
            previo[RUTA_F],
            crear_entrada(RUTA_N, _sha(b"N"), 1, _procedencia(11.5)),
        ]
    )
    assert pendientes == [esperado]
    assert e.manifiesto_en_disco() == esperado == resultado.manifiesto


async def test_un_lote_sin_disco_con_una_construccion_que_no_cabe_saca_su_entrada(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El mismo fallo previo por el camino que no toca archivos: la entrada de un archivo que ya no está sale."""
    await escenario.lote({RUTA_H: escenario.fuente(b"H", 7.5)})
    escenario.disco(RUTA_H).unlink()
    monkeypatch.setattr(output_writer, "_LARGO_MAXIMO_DE_RUTA_COMPLETA", 10)

    resultado = await escenario.lote({RUTA_H: escenario.fuente(b"H", 7.5)})

    assert _efectos(resultado) == {RUTA_H: EfectoDeSalida.FALLIDA}
    assert escenario.manifiesto_en_disco().entradas == ()
    assert sorted(p.name for p in escenario.control().iterdir()) == ["manifest.json"]


async def test_una_recuperacion_que_no_cambia_nada_no_reescribe_el_manifiesto(escenario: Escenario) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    manifiesto = escenario.control() / "manifest.json"
    (escenario.control() / "pending.json").write_bytes(manifiesto.read_bytes())  # una intención que no cambiaba nada
    antes = _identidad(manifiesto)

    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion):
        pass

    assert _identidad(manifiesto) == antes
    assert not (escenario.control() / "pending.json").exists()


async def test_planificar_sin_el_lock_no_observa_nada(escenario: Escenario, monkeypatch: pytest.MonkeyPatch) -> None:
    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:

        async def perdida(*args: Any, **kwargs: Any) -> None:
            raise LockLeaseLostError("lease perdida (simulada)")

        monkeypatch.setattr(sesion._lock, "assert_owned", perdida)
        with pytest.raises(LockLeaseLostError):
            await sesion.planificar([])
        monkeypatch.undo()


# --- Ninguna rama que falla deja su copia de trabajo, y cada una dice por qué ---------------------------------------


def _fallar_al_mover(monkeypatch: pytest.MonkeyPatch, cuando: Any) -> None:
    original = output_writer._mover_sin_reemplazar

    def mover(origen: pathlib.Path, destino: pathlib.Path) -> None:
        if cuando(origen, destino):
            raise PermissionError("sharing violation simulada")
        original(origen, destino)

    monkeypatch.setattr(output_writer, "_mover_sin_reemplazar", mover)


def _fallar_el_fsync_de_la_etapa(escenario: Escenario, monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.fsync
    etapa = escenario.control() / "staging" / output_writer._id_de_ruta(RUTA_A)

    def fsync(fd: int) -> None:
        if etapa.exists() and os.fstat(fd).st_ino == etapa.stat().st_ino:
            raise OSError(28, "disco lleno (simulado)")
        real(fd)

    monkeypatch.setattr(os, "fsync", fsync)


_RAMAS_QUE_FALLAN = {
    # caso: (hay un archivo previo, efecto, parte del detalle, la entrada previa se conserva)
    "construir_ruta_ocupada": (False, EfectoDeSalida.CONFLICTO, "apareció algo en la ruta", False),
    "construir_error_al_publicar": (False, EfectoDeSalida.FALLIDA, "no se pudo publicar", False),
    "copia_que_falla_a_mitad": (False, EfectoDeSalida.FALLIDA, "no se pudo copiar la fuente", False),
    "fuente_que_cambia": (False, EfectoDeSalida.FALLIDA, "cambió mientras se copiaba", False),
    "reemplazar_desaparecido": (True, EfectoDeSalida.CONFLICTO, "desapareció antes de reemplazarlo", False),
    "reemplazar_tocado_por_un_tercero": (True, EfectoDeSalida.CONFLICTO, "lo modificó un tercero", False),
    "reemplazar_error_al_publicar": (True, EfectoDeSalida.CONFLICTO, "no se pudo publicar el reemplazo", True),
    "reemplazar_fuente_que_desaparece": (True, EfectoDeSalida.FALLIDA, "no se pudo copiar la fuente", True),
}


@pytest.mark.parametrize("caso", sorted(_RAMAS_QUE_FALLAN))
async def test_ninguna_rama_que_falla_deja_su_copia_de_trabajo(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch, interponer: Any, caso: str
) -> None:
    """Se enumeran TODAS las ramas que fallan con una etapa ya copiada: cada una la borra en el acto (un lote con muchos
    fallos no acumula copias hasta el compromiso) y su detalle nombra la causa."""
    previo, efecto, detalle, se_conserva = _RAMAS_QUE_FALLAN[caso]
    e = escenario
    if previo:
        await e.lote({RUTA_A: e.fuente(b"DDS-viejo", 1.5)})
    fuente = e.fuente(b"DDS-nuevo", 9.5)
    etapas: list[list[str]] = []
    interponer("antes_del_compromiso", lambda: etapas.append(sorted(os.listdir(e.control() / "staging"))))
    entre: Any = None
    if caso == "construir_ruta_ocupada":

        def entre() -> None:
            e.disco(RUTA_A).parent.mkdir(parents=True)
            e.disco(RUTA_A).write_bytes(b"DEL-USUARIO")

    elif caso in ("construir_error_al_publicar", "reemplazar_error_al_publicar"):
        _fallar_al_mover(monkeypatch, lambda o, d: d == e.disco(RUTA_A) and o.parent.name == "staging")
    elif caso == "copia_que_falla_a_mitad":
        _fallar_el_fsync_de_la_etapa(e, monkeypatch)
    elif caso == "fuente_que_cambia":
        interponer("tras_pendiente", lambda: fuente.archivo.write_bytes(b"DDS-NUEVA"))  # mismo tamaño
    elif caso == "reemplazar_desaparecido":

        def entre() -> None:
            e.disco(RUTA_A).unlink()

    elif caso == "reemplazar_tocado_por_un_tercero":

        def entre() -> None:
            e.disco(RUTA_A).write_bytes(b"EDITADO-A-MANO")

    elif caso == "reemplazar_fuente_que_desaparece":
        interponer("tras_pendiente", lambda: fuente.archivo.unlink())

    resultado = await _planificar_y_aplicar(e, {RUTA_A: fuente}, entre=entre)

    assert _efectos(resultado) == {RUTA_A: efecto}
    assert detalle in _detalles(resultado)[RUTA_A]
    assert etapas == [[]]
    entradas = e.manifiesto_en_disco().entradas
    assert [x.sha256 for x in entradas] == ([_sha(b"DDS-viejo")] if se_conserva else [])
    if se_conserva:
        assert e.leer(RUTA_A) == b"DDS-viejo"


@pytest.mark.parametrize("accion", ["reconstruir", "retirar"])
async def test_lo_apartado_que_no_se_puede_devolver_porque_la_ruta_se_ocupo_queda_en_la_papelera(
    escenario: Escenario, interponer: Any, accion: str
) -> None:
    """Un tercero editó el archivo (no es lo autorizado) y además ocupó la ruta antes de que se le devolviera: no se pisa
    nada, lo apartado queda en la papelera y el resultado dice dónde. Se enumeran las dos acciones que apartan."""
    e = escenario
    await e.lote({RUTA_A: e.fuente(b"DDS-viejo", 1.5)})
    apartado = e.control() / "trash" / output_writer._id_de_ruta(RUTA_A)

    def tercero() -> None:
        apartado.write_bytes(b"EDITADO-A-MANO")
        e.disco(RUTA_A).write_bytes(b"LLEGO-OTRO")

    interponer("tras_apartar", tercero)

    resultado = await e.lote({RUTA_A: e.fuente(b"DDS-nuevo", 9.5)} if accion == "reconstruir" else {})

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.CONFLICTO}
    assert f"quedó en {apartado}" in _detalles(resultado)[RUTA_A]
    assert e.leer(RUTA_A) == b"LLEGO-OTRO"
    assert apartado.read_bytes() == b"EDITADO-A-MANO"
    assert e.manifiesto_en_disco().entradas == ()


@pytest.mark.parametrize("accion", ["reconstruir", "retirar"])
async def test_un_error_al_apartar_falla_la_ruta_y_conserva_el_archivo_y_su_entrada(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch, interponer: Any, accion: str
) -> None:
    e = escenario
    await e.lote({RUTA_A: e.fuente(b"DDS-viejo", 1.5)})

    def apartar(*args: Any) -> pathlib.Path:
        raise PermissionError("el juego tiene el archivo abierto (simulado)")

    monkeypatch.setattr(output_writer, "_apartar", apartar)
    etapas: list[list[str]] = []
    interponer(
        "antes_del_compromiso",
        lambda: etapas.append(
            sorted(os.listdir(e.control() / "staging")) if (e.control() / "staging").exists() else []
        ),
    )

    resultado = await e.lote({RUTA_A: e.fuente(b"DDS-nuevo", 9.5)} if accion == "reconstruir" else {})

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.FALLIDA}
    assert "no se pudo apartar" in _detalles(resultado)[RUTA_A]
    assert e.leer(RUTA_A) == b"DDS-viejo"
    assert [x.sha256 for x in e.manifiesto_en_disco().entradas] == [_sha(b"DDS-viejo")]
    assert etapas == [[]]


@pytest.mark.parametrize(
    ("accion", "detalle"),
    [("retirar", "ya no estaba"), ("retirar_editado", "lo modificó un tercero: no se borró")],
)
async def test_el_detalle_de_un_retiro_que_no_borra_dice_por_que(
    escenario: Escenario, accion: str, detalle: str
) -> None:
    e = escenario
    await e.lote({RUTA_A: e.fuente(b"DDS-A")})

    def entre() -> None:
        if accion == "retirar":
            e.disco(RUTA_A).unlink()
        else:
            e.disco(RUTA_A).write_bytes(b"EDITADO-A-MANO")

    resultado = await _planificar_y_aplicar(e, {}, entre=entre)

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.SOLTADA}
    assert _detalles(resultado)[RUTA_A] == detalle


async def test_retirar_lo_ultimo_no_poda_ni_textures_ni_la_raiz(escenario: Escenario) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})

    await escenario.lote({})

    assert escenario.archivos_bajo_textures() == set()
    assert (escenario.raiz / "textures").is_dir()
    assert list((escenario.raiz / "textures").iterdir()) == []


# --- Archivos de control: todo lo que no se puede leer detiene la sesión, y el mensaje dice cuál ---------------------


@pytest.mark.parametrize("nombre", ["manifest.json", "pending.json"])
@pytest.mark.parametrize("caso", ["desmedido", "no_se_puede_inspeccionar", "no_se_puede_leer"])
async def test_un_archivo_de_control_que_no_se_puede_leer_detiene_la_sesion(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch, nombre: str, caso: str
) -> None:
    control = escenario.control()
    control.mkdir(parents=True)
    vacio = serializar_manifiesto(Manifiesto(entradas=()))
    con_una = serializar_manifiesto(Manifiesto(entradas=(crear_entrada(RUTA_A, H_X, 1, _procedencia()),)))
    (control / "manifest.json").write_bytes(vacio if nombre == "pending.json" else con_una)
    if nombre == "pending.json":
        (control / "pending.json").write_bytes(con_una)
    antes = _instantanea(escenario.raiz)
    que = _QUE[nombre]
    if caso == "desmedido":
        monkeypatch.setattr(output_writer, "TAMANO_MAXIMO_DEL_MANIFIESTO", len(vacio))  # el vacío cabe, el otro no
        mensaje = f"^{que} supera {len(vacio)} bytes"
    elif caso == "no_se_puede_inspeccionar":
        real = output_writer.link_kind_and_identity_or_raise_with_retry

        def inspeccionar(ruta: pathlib.Path) -> Any:
            if ruta == control / nombre:
                raise PermissionError("bloqueado (simulado)")
            return real(ruta)

        monkeypatch.setattr(output_writer, "link_kind_and_identity_or_raise_with_retry", inspeccionar)
        mensaje = f"^no se pudo inspeccionar {que}:"
    else:
        real_leer = pathlib.Path.read_bytes

        def leer(ruta: pathlib.Path) -> bytes:
            if ruta == control / nombre:
                raise PermissionError("bloqueado (simulado)")
            return real_leer(ruta)

        monkeypatch.setattr(pathlib.Path, "read_bytes", leer)
        mensaje = f"^no se pudo leer {que}:"

    with pytest.raises(SalidaNoEscribibleError, match=mensaje):
        async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion):
            pytest.fail("no debería entrar")

    monkeypatch.undo()
    assert _instantanea(escenario.raiz) == antes


async def test_una_carpeta_de_control_que_es_un_enlace_hacia_afuera_se_rechaza(
    escenario: Escenario, tmp_path: pathlib.Path
) -> None:
    afuera = tmp_path / "afuera_del_mod"
    afuera.mkdir()
    (afuera / ".algo.tmp").write_bytes(b"NO ES NUESTRO")
    escenario.raiz.mkdir()
    _enlazar_o_saltear(afuera, escenario.raiz / ".skyclaw-output")

    with pytest.raises(ContencionFisicaVioladaError):
        async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion):
            pytest.fail("no debería entrar")

    assert sorted(p.name for p in afuera.iterdir()) == [".algo.tmp"]


async def test_la_limpieza_solo_borra_lo_que_es_suyo_por_construccion(
    escenario: Escenario, tmp_path: pathlib.Path
) -> None:
    """Las copias de trabajo y los temporales ``.<nombre>.<hex>.tmp`` (archivos regulares) son nuestros por construcción;
    nada más lo es: ni un ``.tmp`` sin punto, ni un archivo oculto sin ``.tmp``, ni un enlace, ni una carpeta."""
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    control = escenario.control()
    (control / "staging").mkdir()
    (control / "staging" / "copia").write_bytes(b"x")
    (control / "staging" / "carpeta").mkdir()
    (control / ".manifest.json.abc.tmp").write_bytes(b"x")
    (control / "ajeno.tmp").write_bytes(b"x")
    (control / ".ajeno").write_bytes(b"x")
    afuera = tmp_path / "afuera.dds"
    afuera.write_bytes(b"x")
    _enlazar_o_saltear(afuera, control / ".enlace.tmp")

    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion):
        pass

    assert sorted(p.name for p in control.iterdir()) == [
        ".ajeno",
        ".enlace.tmp",
        "ajeno.tmp",
        "manifest.json",
        "staging",
    ]
    assert sorted(p.name for p in (control / "staging").iterdir()) == ["carpeta"]
    assert (control / ".enlace.tmp").is_symlink()
    assert afuera.read_bytes() == b"x"


async def test_un_enlace_en_la_papelera_no_cuenta_como_apartado(
    escenario: Escenario, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Aunque apunte a un archivo con la huella vigente: no se devuelve (publicaría un enlace) ni se borra."""
    caplog.set_level(logging.WARNING, logger=output_writer.logger.name)
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    (escenario.control() / "pending.json").write_bytes(serializar_manifiesto(Manifiesto(entradas=())))
    escenario.disco(RUTA_A).unlink()
    afuera = tmp_path / "afuera.dds"
    afuera.write_bytes(b"DDS-A")
    (escenario.control() / "trash").mkdir()
    casilla = escenario.control() / "trash" / output_writer._id_de_ruta(RUTA_A)
    _enlazar_o_saltear(afuera, casilla)

    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        assert sesion.manifiesto.entradas == ()

    assert casilla.is_symlink()
    assert afuera.read_bytes() == b"DDS-A"
    assert not escenario.disco(RUTA_A).exists()
    assert "no son nuestros" in caplog.text


async def test_la_recuperacion_descarta_lo_nuestro_apartado_sin_avisar(
    escenario: Escenario, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=output_writer.logger.name)
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    (escenario.control() / "pending.json").write_bytes(serializar_manifiesto(Manifiesto(entradas=())))
    escenario.disco(RUTA_A).unlink()
    _apartar_a_mano(escenario, RUTA_A, b"DDS-A")  # un retiro cortado entre verificar y borrar

    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion):
        pass

    assert sorted(p.name for p in escenario.control().iterdir()) == ["manifest.json"]
    assert caplog.text == ""


async def test_si_la_ruta_se_ocupa_antes_de_devolver_lo_apartado_se_avisa_y_se_conserva(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=output_writer.logger.name)
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    (escenario.control() / "pending.json").write_bytes(serializar_manifiesto(Manifiesto(entradas=())))
    escenario.disco(RUTA_A).unlink()
    apartado = _apartar_a_mano(escenario, RUTA_A, b"DE-OTRO")
    original = output_writer.reconciliar_tras_corte

    def reconciliar(*args: Any) -> Any:
        recuperacion = original(*args)
        escenario.disco(RUTA_A).write_bytes(b"LLEGO-OTRO")  # después de observar la ruta vacía, antes de devolver
        return recuperacion

    monkeypatch.setattr(output_writer, "reconciliar_tras_corte", reconciliar)

    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion):
        pass

    assert escenario.leer(RUTA_A) == b"LLEGO-OTRO"
    assert apartado.read_bytes() == b"DE-OTRO"
    assert "No se pudo devolver" in caplog.text


# --- Contención en los dos sitios que crean carpetas para escribir (hermanos) ----------------------------------------


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="sin symlinks")
async def test_una_carpeta_que_se_vuelve_enlace_despues_de_planificar_no_crea_nada_afuera(
    escenario: Escenario, tmp_path: pathlib.Path
) -> None:
    afuera = tmp_path / "afuera"
    afuera.mkdir()

    def enlazar() -> None:
        (escenario.raiz / "textures").mkdir(parents=True, exist_ok=True)
        _enlazar_o_saltear(afuera, escenario.raiz / "textures" / "architecture")

    resultado = await _planificar_y_aplicar(escenario, {RUTA_A: escenario.fuente(b"DDS-A")}, entre=enlazar)

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.FALLIDA}
    assert list(afuera.rglob("*")) == []  # ni la carpeta que faltaba se creó afuera


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="sin symlinks")
async def test_al_devolver_una_carpeta_que_se_vuelve_enlace_no_crea_nada_afuera(
    escenario: Escenario, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    afuera = tmp_path / "afuera"
    afuera.mkdir()
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    (escenario.control() / "pending.json").write_bytes(serializar_manifiesto(Manifiesto(entradas=())))
    escenario.disco(RUTA_A).unlink()
    escenario.disco(RUTA_A).parent.rmdir()
    escenario.disco(RUTA_A).parent.parent.rmdir()  # textures/architecture
    apartado = _apartar_a_mano(escenario, RUTA_A, b"DE-OTRO")
    original = output_writer.reconciliar_tras_corte

    def reconciliar(*args: Any) -> Any:
        recuperacion = original(*args)
        _enlazar_o_saltear(afuera, escenario.raiz / "textures" / "architecture")
        return recuperacion

    monkeypatch.setattr(output_writer, "reconciliar_tras_corte", reconciliar)

    with pytest.raises(ContencionFisicaVioladaError):
        async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion):
            pytest.fail("no debería entrar")

    assert list(afuera.rglob("*")) == []
    assert apartado.read_bytes() == b"DE-OTRO"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="sin symlinks")
@pytest.mark.parametrize("sitio", ["publicar", "devolver"])
async def test_si_la_carpeta_recien_creada_se_vuelve_enlace_no_se_escribe_afuera(
    escenario: Escenario, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, sitio: str
) -> None:
    """Entre crear la carpeta madre y usarla, alguien la cambia por un enlace hacia afuera: la segunda comprobación lo
    ataja. Se enumeran los dos sitios que crean carpetas para escribir en el mod (publicar y devolver), no uno."""
    afuera = tmp_path / "afuera"
    afuera.mkdir()
    objetivo = escenario.disco(RUTA_A).parent
    if sitio == "devolver":
        await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
        (escenario.control() / "pending.json").write_bytes(serializar_manifiesto(Manifiesto(entradas=())))
        escenario.disco(RUTA_A).unlink()
        objetivo.rmdir()
        apartado = _apartar_a_mano(escenario, RUTA_A, b"DE-OTRO")
    real = os.makedirs

    def makedirs(nombre: Any, *args: Any, **kwargs: Any) -> None:
        real(nombre, *args, **kwargs)
        if pathlib.Path(nombre) == objetivo and not objetivo.is_symlink():
            os.rmdir(objetivo)
            os.symlink(afuera, objetivo, target_is_directory=True)

    monkeypatch.setattr(os, "makedirs", makedirs)

    if sitio == "publicar":
        resultado = await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
        assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.FALLIDA}
    else:
        with pytest.raises(ContencionFisicaVioladaError):
            async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion):
                pytest.fail("no debería entrar")
        assert apartado.read_bytes() == b"DE-OTRO"

    assert list(afuera.rglob("*")) == []


# --- La raíz y la coordinación ---------------------------------------------------------------------------------------


async def test_la_raiz_debe_ser_una_ruta_y_poder_inspeccionarse(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(SalidaNoEscribibleError, match=r"la raíz debe ser una ruta \(Path\)"):
        async with abrir_sesion_de_salida(str(escenario.raiz), coordinacion=escenario.coordinacion):  # type: ignore[arg-type]
            pytest.fail("no debería entrar")
    real = output_writer.link_kind_and_identity_or_raise_with_retry

    def inspeccionar(ruta: pathlib.Path) -> Any:
        if ruta == escenario.raiz:
            raise PermissionError("bloqueado (simulado)")
        return real(ruta)

    monkeypatch.setattr(output_writer, "link_kind_and_identity_or_raise_with_retry", inspeccionar)
    with pytest.raises(SalidaNoEscribibleError, match="no se pudo inspeccionar la raíz"):
        async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion):
            pytest.fail("no debería entrar")
    assert not escenario.raiz.exists()


class _ManagerQueCuenta:
    """Un ``DistributedLockManager`` de mentira que solo cuenta aperturas y cierres."""

    def __init__(self) -> None:
        self.aperturas = 0
        self.cierres = 0

    async def initialize(self) -> None:
        self.aperturas += 1

    async def close(self) -> None:
        self.cierres += 1


async def test_la_coordinacion_abre_una_vez_cierra_solo_lo_abierto_y_puede_reabrir(tmp_path: pathlib.Path) -> None:
    manager = _ManagerQueCuenta()
    coordinacion = CoordinacionDeSalida(
        lock_manager=manager,  # type: ignore[arg-type]
        snapshot_manager=FileSnapshotManager(snapshot_dir=tmp_path / "snapshots"),
    )

    await coordinacion.close()
    assert (manager.aperturas, manager.cierres) == (0, 0)
    await coordinacion.initialize()
    await coordinacion.initialize()
    assert (manager.aperturas, manager.cierres) == (1, 0)
    await coordinacion.close()
    await coordinacion.close()
    assert (manager.aperturas, manager.cierres) == (1, 1)
    await coordinacion.initialize()
    assert (manager.aperturas, manager.cierres) == (2, 1)


# --- Defensas de la ejecución (el plan las garantiza; si no, no se toca nada) ---------------------------------------


@pytest.mark.parametrize(
    ("accion", "con_trabajo", "con_vigente", "mensaje"),
    [
        (AccionDeSalida.CONSTRUIR, False, False, "no trae la huella que la autoriza"),
        (AccionDeSalida.RETIRAR, False, False, "no trae la huella que la autoriza"),
        (AccionDeSalida.RECONSTRUIR, False, True, "no tiene su trabajo"),
    ],
)
def test_una_accion_incoherente_no_toca_el_disco(
    escenario: Escenario, accion: AccionDeSalida, con_trabajo: bool, con_vigente: bool, mensaje: str
) -> None:
    escenario.disco(RUTA_A).parent.mkdir(parents=True)
    escenario.disco(RUTA_A).write_bytes(b"DDS-A")
    vigente = crear_entrada(RUTA_A, _sha(b"DDS-A"), 5, _procedencia()) if con_vigente else None
    autorizado = _sha(b"DDS-A") if accion is not AccionDeSalida.CONSTRUIR else None
    motivo = {
        AccionDeSalida.CONSTRUIR: MotivoDeSalida.NUEVA,
        AccionDeSalida.RETIRAR: MotivoDeSalida.HUERFANO_PROPIO,
        AccionDeSalida.RECONSTRUIR: MotivoDeSalida.CLAVE_CAMBIO,
    }[accion]
    plan = AccionPlanificada(RUTA_A, accion, motivo, autorizado)
    antes = _instantanea(escenario.raiz)

    with pytest.raises(SalidaNoEscribibleError, match=mensaje):
        output_writer._actuar_en_disco(escenario.raiz, plan, None, vigente)

    assert _instantanea(escenario.raiz) == antes


@pytest.mark.skipif(os.name == "nt", reason="los bits de permiso POSIX no aplican en Windows")
async def test_lo_publicado_y_el_manifiesto_tienen_los_permisos_de_siempre(escenario: Escenario) -> None:
    mascara = os.umask(0)
    os.umask(mascara)

    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})

    esperado = 0o644 & ~mascara
    assert escenario.disco(RUTA_A).stat().st_mode & 0o777 == esperado
    assert (escenario.control() / "manifest.json").stat().st_mode & 0o777 == esperado


async def test_una_escritura_atomica_que_falla_levanta_y_no_deja_temporales(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destino = tmp_path / "manifest.json"
    destino.write_bytes(b"VIEJO")

    def reemplazar(*args: Any) -> None:
        raise PermissionError("bloqueado (simulado)")

    monkeypatch.setattr(os, "replace", reemplazar)

    with pytest.raises(PermissionError):
        output_writer._escribir_atomico(destino, b"NUEVO")

    monkeypatch.undo()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["manifest.json"]
    assert destino.read_bytes() == b"VIEJO"


async def test_si_se_pierde_el_lock_antes_de_un_compromiso_sin_disco_no_se_escribe_el_manifiesto(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El camino que solo cambia el manifiesto vuelve a comprobar el lock justo antes de escribirlo (la traza no lo puede
    distinguir: el chequeo al entrar a ``aplicar`` ya dejó «lock» antes de esa escritura)."""
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    escenario.disco(RUTA_A).unlink()  # el lote que sigue la suelta: solo cambia el manifiesto
    manifiesto = escenario.control() / "manifest.json"
    antes = manifiesto.read_bytes()
    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        plan = await sesion.planificar([])
        llamadas = {"n": 0}
        real = sesion._lock.assert_owned

        async def se_pierde_en_la_segunda(*args: Any, **kwargs: Any) -> None:
            llamadas["n"] += 1
            if llamadas["n"] >= 2:  # 1 al entrar a aplicar, 1 antes de escribir el manifiesto
                raise LockLeaseLostError("lease perdida (simulada)")
            await real(*args, **kwargs)

        monkeypatch.setattr(sesion._lock, "assert_owned", se_pierde_en_la_segunda)
        with pytest.raises(LockLeaseLostError):
            await sesion.aplicar(plan, {})
        monkeypatch.undo()

    assert manifiesto.read_bytes() == antes


# ---------------------------------------------------------------------------
# Revisión de #782: las carpetas de control también están contenidas
# ---------------------------------------------------------------------------


def _enlazar_carpeta(tipo: str, destino: pathlib.Path, enlace: pathlib.Path) -> None:
    """Crea *enlace* → *destino* como symlink o como junction (que ``is_symlink()`` no ve y solo existe en Windows)."""
    if tipo == "symlink":
        _enlazar_o_saltear(destino, enlace)
        return
    if os.name != "nt":
        pytest.skip("los junctions solo existen en Windows")
    # Sin skip en Windows: crear un junction no pide privilegios, así que un fallo es el helper roto, no el entorno.
    if (motivo := crear_junction(enlace, destino)) is not None:
        pytest.fail(f"no se pudo crear el junction que este test existe para ejercer: {motivo}")


@pytest.mark.parametrize("tipo", ["symlink", "junction"])
@pytest.mark.parametrize("carpeta", ["staging", "trash"])
async def test_una_carpeta_de_control_interna_que_es_un_enlace_hacia_afuera_se_rechaza_al_abrir(
    escenario: Escenario, tmp_path: pathlib.Path, carpeta: str, tipo: str
) -> None:
    """Las dos carpetas donde el escritor copia y aparta: si ya son un enlace (o un junction), copiar, apartar o limpiar
    ahí lo haría AFUERA del mod. Se rechazan al abrir la sesión, sin tocar nada de afuera."""
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    afuera = tmp_path / "afuera_del_mod"
    afuera.mkdir()
    (afuera / "copia").write_bytes(b"NO ES NUESTRO")
    _enlazar_carpeta(tipo, afuera, escenario.control() / carpeta)

    with pytest.raises(ContencionFisicaVioladaError):
        async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion):
            pytest.fail("no debería entrar")

    assert sorted(p.name for p in afuera.iterdir()) == ["copia"]
    assert (afuera / "copia").read_bytes() == b"NO ES NUESTRO"


@pytest.mark.parametrize("carpeta", ["staging", "trash"])
async def test_una_carpeta_de_control_interna_que_se_vuelve_enlace_durante_la_sesion_no_recibe_nada(
    escenario: Escenario, tmp_path: pathlib.Path, carpeta: str
) -> None:
    """Entre planificar y aplicar, la carpeta de copias (o la papelera) se vuelve un enlace hacia afuera: un reemplazo
    copia a la etapa y aparta el original, así que pasa por las dos. La ruta falla, el original sigue en su lugar y su
    entrada en el manifiesto, y afuera no aparece nada."""
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    afuera = tmp_path / "afuera_del_mod"
    afuera.mkdir()

    resultado = await _planificar_y_aplicar(
        escenario,
        {RUTA_A: escenario.fuente(b"DDS-A2", escala=2.0)},
        entre=lambda: _enlazar_o_saltear(afuera, escenario.control() / carpeta),
    )

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.FALLIDA}
    assert list(afuera.iterdir()) == []
    assert escenario.leer(RUTA_A) == b"DDS-A"
    assert [e.sha256 for e in escenario.manifiesto_en_disco().entradas] == [_sha(b"DDS-A")]


def test_toda_carpeta_se_crea_con_la_contencion_validada_antes_y_despues() -> None:
    """Toda carpeta que el escritor crea dentro del mod, de salida o de control, pasa por UN helper que valida la
    contención física antes y después de crearla. Quedan afuera, con su motivo: la raíz misma (la validó
    ``_validar_raiz`` y su carpeta madre puede vivir legítimamente bajo un junction, como MO2 en otro disco) y el estado
    de los locks, que vive en el directorio del usuario y no en el mod."""
    crean = {f for f, usos in _recorrer().mutadores.items() if usos & {"os.makedirs", "os.mkdir", ".mkdir"}}
    assert crean == {"_crear_carpeta_contenida", "_recuperar", "construir_coordinacion_de_salida"}

    recuperar = next(n for n in ast.walk(_arbol()) if isinstance(n, ast.FunctionDef) and n.name == "_recuperar")
    creaciones = [
        c
        for c in ast.walk(recuperar)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr in {"mkdir", "makedirs"}
    ]
    assert [ast.unparse(c) for c in creaciones] == ["os.mkdir(raiz)"]  # solo la raíz

    helper = next(
        n for n in ast.walk(_arbol()) if isinstance(n, ast.FunctionDef) and n.name == "_crear_carpeta_contenida"
    )
    llamadas = [
        ast.unparse(s.value.func) for s in helper.body if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
    ]
    assert llamadas == ["exigir_contencion_fisica", "os.makedirs", "exigir_contencion_fisica"]


# ---------------------------------------------------------------------------
# Revisión de #782: todo borrado de la papelera rehace la huella, y no poder borrar no aborta el lote
# ---------------------------------------------------------------------------


def test_solo_el_helper_que_rehace_la_huella_borra_de_la_papelera() -> None:
    """El censo de quién llama a ``os.unlink`` está congelado. De la papelera borra únicamente
    ``_borrar_si_es_el_autorizado``; el resto borra lo propio por construcción (temporales, copias de trabajo, el
    pendiente y el nombre viejo de un movimiento sin reemplazo en POSIX)."""
    borran = {f for f, usos in _recorrer().mutadores.items() if "os.unlink" in usos}
    assert borran == {
        "_borrar_pendiente",
        "_borrar_si_es_el_autorizado",
        "_construir",
        "_copiar_a_etapa",
        "_escribir_atomico",
        "_limpiar_control",
        "_mover_sin_reemplazar",
        "_reemplazar",
    }


def _que_el_primer_borrado_en_la_papelera_falle(monkeypatch: pytest.MonkeyPatch, control: pathlib.Path) -> list[str]:
    """El primer ``os.unlink`` de algo en la papelera falla como con un antivirus o un archivo de solo lectura en Windows;
    los siguientes funcionan. Devuelve la lista de lo que no se pudo borrar."""
    real = os.unlink
    fallados: list[str] = []

    def unlink(ruta: Any, *args: Any, **kwargs: Any) -> None:
        if pathlib.Path(ruta).parent == control / "trash" and not fallados:
            fallados.append(str(ruta))
            raise PermissionError(13, "lo tiene abierto otro proceso (simulado)", str(ruta))
        real(ruta, *args, **kwargs)

    monkeypatch.setattr(output_writer.os, "unlink", unlink)
    return fallados


async def test_si_no_se_puede_borrar_lo_que_se_retira_vuelve_a_su_ruta_y_el_lote_sigue(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A"), RUTA_C: escenario.fuente(b"DDS-C")})
    fallados = _que_el_primer_borrado_en_la_papelera_falle(monkeypatch, escenario.control())

    resultado = await escenario.lote({RUTA_C: escenario.fuente(b"DDS-C")})  # A se retira

    assert fallados
    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.FALLIDA, RUTA_C: EfectoDeSalida.OMITIDA}
    assert "no se pudo borrar" in _detalles(resultado)[RUTA_A]
    assert escenario.leer(RUTA_A) == b"DDS-A"  # volvió a su ruta y sigue siendo nuestro
    assert {e.ruta for e in escenario.manifiesto_en_disco().entradas} == {RUTA_A, RUTA_C}
    assert sorted(p.name for p in escenario.control().iterdir()) == ["manifest.json"]


async def test_si_no_se_puede_borrar_el_original_de_un_reemplazo_el_reemplazo_queda_y_se_dice(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    fallados = _que_el_primer_borrado_en_la_papelera_falle(monkeypatch, escenario.control())

    resultado = await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A2", escala=2.0)})

    assert fallados
    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.REEMPLAZADA}
    assert "no se pudo borrar el original" in _detalles(resultado)[RUTA_A]
    assert escenario.leer(RUTA_A) == b"DDS-A2"
    assert [e.sha256 for e in escenario.manifiesto_en_disco().entradas] == [_sha(b"DDS-A2")]
    apartados = list((escenario.control() / "trash").iterdir())
    assert [p.read_bytes() for p in apartados] == [b"DDS-A"]  # el original no se perdió: quedó en la papelera


async def test_si_la_recuperacion_no_puede_descartar_avisa_y_la_sesion_abre_igual(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Con un archivo de solo lectura en Windows el borrado falla SIEMPRE: si la recuperación levantara, la sesión no
    volvería a abrir nunca. Avisa, lo deja en la papelera (es nuestro y está verificado) y sigue."""
    caplog.set_level(logging.WARNING, logger=output_writer.logger.name)
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    (escenario.control() / "pending.json").write_bytes(serializar_manifiesto(Manifiesto(entradas=())))
    escenario.disco(RUTA_A).unlink()
    apartado = _apartar_a_mano(escenario, RUTA_A, b"DDS-A")  # un retiro cortado entre apartar y borrar
    fallados = _que_el_primer_borrado_en_la_papelera_falle(monkeypatch, escenario.control())

    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        assert sesion.manifiesto.entradas == ()

    assert fallados
    assert apartado.read_bytes() == b"DDS-A"
    assert not (escenario.control() / "pending.json").exists()
    assert "No se pudo descartar" in caplog.text


# ---------------------------------------------------------------------------
# Revisión de #782: la copia a la etapa no pasa del tamaño validado
# ---------------------------------------------------------------------------


class _LecturaContada:
    """Envuelve un archivo abierto y cuenta los bytes que se leen (un ``BufferedReader`` no deja reemplazar ``read``)."""

    def __init__(self, archivo: Any) -> None:
        self._archivo = archivo
        self.total = 0

    def __enter__(self) -> _LecturaContada:
        return self

    def __exit__(self, *excepcion: object) -> None:
        self._archivo.close()

    def read(self, n: int = -1) -> bytes:
        datos: bytes = self._archivo.read(n)
        self.total += len(datos)
        return datos


async def test_una_fuente_que_crece_despues_de_validarse_se_corta_sin_copiar_de_mas(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si la fuente crece después de validarla (otro proceso la sigue escribiendo), la copia corta en cuanto supera el
    tamaño validado; no sigue hasta el final del archivo, que podría llenar el volumen del mod."""
    fuente = escenario.fuente(b"DDS-A")
    validar = output_writer._huella_y_tamano_de_la_fuente
    lecturas: list[_LecturaContada] = []
    abrir = pathlib.Path.open

    def validar_y_crecer(archivo: pathlib.Path) -> tuple[str, int]:
        resultado = validar(archivo)
        with abrir(archivo, "ab") as f:
            f.write(b"X" * 1_000_000)
        monkeypatch.setattr(pathlib.Path, "open", abrir_contando)  # de acá en adelante, solo cuenta la copia
        return resultado

    def abrir_contando(self: pathlib.Path, *args: Any, **kwargs: Any) -> Any:
        archivo = abrir(self, *args, **kwargs)
        if self != fuente.archivo:
            return archivo
        lecturas.append(_LecturaContada(archivo))
        return lecturas[-1]

    monkeypatch.setattr(output_writer, "_huella_y_tamano_de_la_fuente", validar_y_crecer)
    monkeypatch.setattr(output_writer, "_BLOQUE", 4)

    resultado = await escenario.lote({RUTA_A: fuente})

    monkeypatch.undo()
    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.FALLIDA}
    assert "creció" in _detalles(resultado)[RUTA_A]
    assert [lectura.total for lectura in lecturas] == [8]  # dos bloques de 4: el segundo ya pasa los 5 validados
    assert escenario.archivos_bajo_textures() == set()
    assert sorted(p.name for p in escenario.control().iterdir()) == ["manifest.json"]


# ---------------------------------------------------------------------------
# Revisión de #782: toda ortografía de la misma carpeta física compite por un solo lock
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tipo", ["symlink", "junction"])
async def test_toda_ortografia_de_la_misma_carpeta_fisica_compite_por_un_solo_lock(
    escenario: Escenario, tmp_path: pathlib.Path, tipo: str
) -> None:
    """Un ancestro enlazado (la carpeta de mods de MO2 en otro disco por un junction es común) da otra ortografía de la
    MISMA carpeta: tiene que competir por el mismo recurso, o dos procesos la mutarían a la vez."""
    alias = tmp_path / "alias_de_mods"
    _enlazar_carpeta(tipo, escenario.mods, alias)
    por_el_alias = alias / NOMBRE_DEL_MOD_DE_SALIDA

    recurso = CoordinacionDeSalida.resource_id_de_la_raiz
    assert recurso(por_el_alias) == recurso(escenario.raiz)

    impaciente = construir_coordinacion_de_salida(
        base=tmp_path / "estado", max_retries=2, backoff_base=0.01, backoff_max=0.02
    )
    try:
        async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion):
            with pytest.raises(SalidaOcupadaError):
                async with abrir_sesion_de_salida(por_el_alias, coordinacion=impaciente):
                    pytest.fail("no debería entrar por el alias mientras la otra sesión sostiene la carpeta")
    finally:
        await impaciente.close()


# ---------------------------------------------------------------------------
# Revisión de #782: la lease dura lo que dura el hilo que escribe
# ---------------------------------------------------------------------------


def test_todo_lo_que_corre_en_un_hilo_pasa_por_en_hilo() -> None:
    """``asyncio.to_thread`` aparece solo dentro de ``_en_hilo``: una llamada directa nueva podría abandonar su hilo al
    cancelarse y dejar que la sesión suelte el lock con el hilo todavía escribiendo."""
    duenos = {
        funcion.name
        for funcion in ast.walk(_arbol())
        if isinstance(funcion, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(isinstance(n, ast.Attribute) and n.attr == "to_thread" for n in ast.walk(funcion))
    }
    assert duenos == {"_en_hilo"}


async def test_cancelar_con_un_hilo_escribiendo_no_suelta_el_lock_hasta_que_el_hilo_termina(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """``asyncio.to_thread`` no frena el hilo al cancelarse: si la sesión soltara el lock en ese momento, otro proceso
    entraría a recuperar mientras el hilo todavía mueve archivos. La lease tiene que durar lo que dura el hilo."""
    adentro, seguir = threading.Event(), threading.Event()
    actuar = output_writer._actuar_en_disco

    def actuar_lento(*args: Any) -> Any:
        adentro.set()
        seguir.wait(timeout=30)
        return actuar(*args)

    monkeypatch.setattr(output_writer, "_actuar_en_disco", actuar_lento)
    tarea = asyncio.create_task(escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")}))
    impaciente = construir_coordinacion_de_salida(
        base=tmp_path / "estado", max_retries=2, backoff_base=0.01, backoff_max=0.02
    )
    try:
        for _ in range(500):
            if adentro.is_set():
                break
            await asyncio.sleep(0.01)
        assert adentro.is_set()
        tarea.cancel()
        await asyncio.sleep(0.1)

        try:
            with pytest.raises(SalidaOcupadaError):
                async with abrir_sesion_de_salida(escenario.raiz, coordinacion=impaciente):
                    pytest.fail("entró mientras el hilo del lote cancelado seguía escribiendo")
            assert not tarea.done()
        finally:
            seguir.set()
        with pytest.raises(asyncio.CancelledError):
            await tarea

        # Con el hilo terminado, la lease se soltó y la sesión siguiente recupera el lote cortado.
        async with abrir_sesion_de_salida(escenario.raiz, coordinacion=impaciente) as sesion:
            assert {e.ruta for e in sesion.manifiesto.entradas} <= {RUTA_A}
        for entrada in escenario.manifiesto_en_disco().entradas:  # lo que el manifiesto dice nuestro existe así
            assert _sha(escenario.leer(entrada.ruta)) == entrada.sha256
        assert not (escenario.control() / "pending.json").exists()
    finally:
        seguir.set()
        await impaciente.close()


# ---------------------------------------------------------------------------
# Revisión de #782: un lote que quedó a medias inutiliza la sesión hasta reabrirla
# ---------------------------------------------------------------------------


async def test_un_lote_que_falla_despues_de_la_intencion_exige_reabrir_la_sesion(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si el lote falla después de escribir el pendiente (acá, al comprometer el manifiesto), el disco quedó a medias:
    reintentar el plan viejo en la misma sesión compararía contra huellas que ya no valen y soltaría lo recién publicado.
    La sesión no acepta nada más, y reabrirla recupera."""
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    nueva = escenario.fuente(b"DDS-A2", escala=2.0)
    deseadas = [SalidaDeseada(RUTA_A, clave_de_cache(nueva.procedencia))]
    escribir = output_writer._escribir_atomico

    def falla_al_comprometer(destino: pathlib.Path, datos: bytes) -> None:
        if destino.name == "manifest.json":
            raise OSError(28, "no queda espacio (simulado)")
        escribir(destino, datos)

    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        plan = await sesion.planificar(deseadas)
        monkeypatch.setattr(output_writer, "_escribir_atomico", falla_al_comprometer)
        with pytest.raises(OSError, match="no queda espacio"):
            await sesion.aplicar(plan, {RUTA_A: nueva})
        monkeypatch.undo()

        with pytest.raises(SalidaNoEscribibleError, match="a medias"):
            await sesion.aplicar(plan, {RUTA_A: nueva})
        with pytest.raises(SalidaNoEscribibleError, match="a medias"):
            await sesion.planificar(deseadas)

    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:  # recupera
        assert [e.sha256 for e in sesion.manifiesto.entradas] == [_sha(b"DDS-A2")]
    assert escenario.leer(RUTA_A) == b"DDS-A2"
    assert sorted(p.name for p in escenario.control().iterdir()) == ["manifest.json"]


async def test_un_lote_rechazado_antes_de_tocar_el_disco_no_inutiliza_la_sesion(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El rechazo por la cota del manifiesto no tocó nada: la sesión sigue sirviendo. El plan, en cambio, se aplica una
    sola vez: para volver a intentarlo hay que planificar de nuevo."""
    fuentes = {RUTA_A: escenario.fuente(b"DDS-A"), RUTA_C: escenario.fuente(b"DDS-C")}
    deseadas = [SalidaDeseada(r, clave_de_cache(f.procedencia)) for r, f in fuentes.items()]

    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        plan = await sesion.planificar(deseadas)
        monkeypatch.setattr(output_writer, "TAMANO_MAXIMO_DEL_MANIFIESTO", 10)
        with pytest.raises(SalidaNoEscribibleError, match="dividí el lote"):
            await sesion.aplicar(plan, fuentes)
        monkeypatch.undo()

        with pytest.raises(SalidaNoEscribibleError, match="no es el último"):
            await sesion.aplicar(plan, fuentes)
        resultado = await sesion.aplicar(await sesion.planificar(deseadas), fuentes)

    assert resultado.completo
    assert {e.ruta for e in escenario.manifiesto_en_disco().entradas} == {RUTA_A, RUTA_C}
