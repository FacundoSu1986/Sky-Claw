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
import hashlib
import inspect
import os
import pathlib
import subprocess
import sys
from collections.abc import AsyncIterator
from typing import Any

import pytest

from sky_claw.app.db.locks import LockLeaseLostError, SnapshotTransactionLock
from sky_claw.local.native_parallax import output_writer
from sky_claw.local.native_parallax.output import (
    NOMBRE_DEL_MOD_DE_SALIDA,
    AccionDeSalida,
    EncoderUsado,
    Manifiesto,
    NivelDeReproducibilidad,
    OrigenDeEntrada,
    Procedencia,
    SalidaDeseada,
    clave_de_cache,
    clave_de_ruta,
    leer_manifiesto,
    parametros_canonicos,
    serializar_manifiesto,
)
from sky_claw.local.native_parallax.output_writer import (
    CoordinacionDeSalida,
    EfectoDeSalida,
    FuenteDeSalida,
    ResultadoDeAplicacion,
    SalidaNoEscribibleError,
    SalidaOcupadaError,
    SesionDeSalida,
    abrir_sesion_de_salida,
    construir_coordinacion_de_salida,
)

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

    resultado = await escenario.lote(lote)

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.OMITIDA, RUTA_C: EfectoDeSalida.OMITIDA}
    assert {r: escenario.disco(r).stat().st_ino for r in (RUTA_A, RUTA_C)} == antes
    assert (escenario.control() / "manifest.json").read_bytes() == manifiesto_antes


async def test_un_cambio_de_procedencia_reemplaza_el_archivo_y_actualiza_el_manifiesto(escenario: Escenario) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-viejo", 1.5)})

    resultado = await escenario.lote({RUTA_A: escenario.fuente(b"DDS-nuevo", 9.5)})

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.REEMPLAZADA}
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

    with pytest.raises(SalidaNoEscribibleError, match="enlace"):
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


async def test_una_fuente_que_no_existe_o_es_un_enlace_se_rechaza_antes_de_tocar_nada(
    escenario: Escenario, tmp_path: pathlib.Path
) -> None:
    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        f = escenario.fuente(b"DDS-A")
        plan = await sesion.planificar([SalidaDeseada(RUTA_A, clave_de_cache(f.procedencia))])
        f.archivo.unlink()

        with pytest.raises(SalidaNoEscribibleError, match="fuente"):
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
    escenario: Escenario, interponer: Any
) -> None:
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


async def test_una_fuente_que_cambia_mientras_se_copia_no_se_publica(escenario: Escenario, interponer: Any) -> None:
    fuente = escenario.fuente(b"DDS-A")
    interponer("tras_pendiente", lambda: fuente.archivo.write_bytes(b"OTRO-CONTENIDO"))

    resultado = await escenario.lote({RUTA_A: fuente})

    assert _efectos(resultado) == {RUTA_A: EfectoDeSalida.FALLIDA}
    assert escenario.archivos_bajo_textures() == set()
    assert escenario.manifiesto_en_disco().entradas == ()
    assert "cambió" in resultado.rutas[0].detalle


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
            with pytest.raises(SalidaOcupadaError):
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
    assert recurso(raiz).startswith("native-parallax-output-")


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

    with pytest.raises(SalidaNoEscribibleError, match="ilegible"):
        async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion):
            pytest.fail("no debería entrar")

    assert _instantanea(escenario.raiz) == antes


@pytest.mark.parametrize("nombre", ["manifest.json", "pending.json"])
async def test_un_archivo_de_control_que_es_un_directorio_se_rechaza(escenario: Escenario, nombre: str) -> None:
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    if nombre == "manifest.json":
        (escenario.control() / nombre).unlink()
    (escenario.control() / nombre).mkdir()

    with pytest.raises(SalidaNoEscribibleError, match="regular"):
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


async def test_lo_que_un_tercero_dejo_en_la_papelera_nunca_se_borra_ni_se_devuelve(escenario: Escenario) -> None:
    """Una huella en la papelera solo es nuestra si es la de la entrada vigente: lo demás se queda donde está."""
    await escenario.lote({RUTA_A: escenario.fuente(b"DDS-A")})
    antes = escenario.manifiesto_en_disco()
    (escenario.control() / "pending.json").write_bytes(
        serializar_manifiesto(Manifiesto(entradas=()))  # intención: retirar A
    )
    escenario.disco(RUTA_A).unlink()  # y A ya no está
    papelera = escenario.control() / "trash"
    papelera.mkdir()
    apartado = papelera / output_writer._id_de_ruta(RUTA_A)
    apartado.write_bytes(b"DE-OTRO-QUE-NO-ES-A")

    async with abrir_sesion_de_salida(escenario.raiz, coordinacion=escenario.coordinacion) as sesion:
        assert sesion.manifiesto.entradas == ()

    assert apartado.read_bytes() == b"DE-OTRO-QUE-NO-ES-A"
    assert not escenario.disco(RUTA_A).exists()
    assert antes.entradas[0].ruta == RUTA_A


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

    assert mutadores == {
        "_apartar",
        "_asegurar_directorio_de_control",
        "_borrar_pendiente",
        "_construir",
        "_copiar_a_etapa",
        "_escribir_atomico",
        "_limpiar_control",
        "_mover_sin_reemplazar",
        "_podar_directorios_vacios",
        "_preparar_padre",
        "_recuperar",
        "_reemplazar",
        "_restaurar_desde_papelera",
        "_retirar",
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


async def test_cada_unidad_que_escribe_va_precedida_de_una_comprobacion_del_lock(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sobre una corrida con las tres acciones: antes de CADA unidad que escribe, ``assert_owned`` acaba de pasar."""
    lote = await _armar_el_estado_previo(escenario)
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
    assert len(escritoras) >= 7  # pendiente, 3 acciones, manifiesto, limpieza y borrado del pendiente
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
    # Los que nombra el código van por el asyncio.to_thread(_punto_de_corte, "x") además de la llamada directa:
    nombrados |= {
        c.args[1].value
        for c in ast.walk(_arbol())
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Attribute)
        and c.func.attr == "to_thread"
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
