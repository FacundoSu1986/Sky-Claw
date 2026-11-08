"""#852: los gates de ``read_enabled_mods`` NO pueden dar veredicto según la grafía del mod.

``+x`` y ``+X`` son el MISMO mod para MO2/NTFS (identidad ``lower()``, nunca ``casefold()``,
#838/#849). Con un perfil ``(\"A\", \"x\", \"B\", \"X\")`` los gates comparaban de forma
exacta: ``count`` no veía el duplicado, ``index`` elegía una entrada por grafía y dos
consumidores del mismo perfil discrepaban sobre si era resoluble. Con un perfil VÁLIDO
``(\"A\", \"x\", \"B\")`` el nombre pedido como ``\"X\"`` se rechazaba como "no habilitado".

Regla del mecanismo, no recordatorio de proceso: **toda comparación de un nombre de mod
contra la tupla de ``enabled`` pasa por ``ganadores._coincidencias_de_mod``**, y **toda
lectura del modlist falla cerrado ante un perfil ambiguo en la PUERTA
(``read_enabled_mods``)**. El ancla de abajo enumera por AST los consumidores y los
mecanismos; no muestrea un caso por hermano.

Censo (``main`` 501c22e1), además de los que nombra la issue:
``validators/etapas_previas_lods.py`` y ``tools/wrye_bash_service.py``. Clasificación
corregida tras trazar el flujo: ``native_parallax/diagnostico_perfil.cargar_perfil``
YA falla cerrado, porque delega en ``indexar_ganadores`` → ``_exigir_enabled``.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import pathlib

import pytest

from sky_claw.local.mo2 import brokered_dyndolod, ganadores, vfs_attestation
from sky_claw.local.mo2.brokered_dyndolod import BrokeredDynDOLODSpawnStrategy
from sky_claw.local.mo2.mod_effectivity import ModEffectivityError, verificar_artifact_efectivo
from sky_claw.local.mo2.vfs import _clave_de_nombre_de_mod
from sky_claw.local.mo2.vfs_attestation import (
    VfsAttestationError,
    build_attestation_challenge,
    build_attestation_challenge_for_source,
    read_enabled_mods,
)
from sky_claw.local.native_parallax.diagnostico_perfil import PerfilError, cargar_perfil
from sky_claw.local.tools.artifact_digest import digest_arbol
from sky_claw.local.tools.output_targets import bashed_patch_en_overlay
from sky_claw.local.tools.texgen_handoff import (
    HandoffDriftError,
    TexGenHandoffApproval,
    TexGenHandoffRequest,
)
from sky_claw.local.tools.wrye_bash_service import WryeBashPipelineService
from sky_claw.local.validators.etapas_previas_lods import verificar_etapas_previas_de_lods

_PERFIL = "Default"
_RELATIVO = "textures/canary.dds"
_CANARIO = b"BYTES_DEL_CANARY"
_FISICO = b"BYTES_FISICOS_DISTINTOS"

#: prioridad CRECIENTE, tal como la devuelve ``read_enabled_mods``.
_AMBIGUO = ("A", "x", "B", "X")
_VALIDO = ("A", "x", "B")

_RAIZ_DEL_REPO = pathlib.Path(__file__).resolve().parent.parent


class Instancia:
    """Instancia MO2 mínima sobre disco real: perfil, mods, Data físico y vista virtual."""

    def __init__(self, base: pathlib.Path) -> None:
        self.base = base
        self.data_root = base / "MO2"
        self.mods = self.data_root / "mods"
        self.mods.mkdir(parents=True)
        self.perfil_dir = self.data_root / "profiles" / _PERFIL
        self.perfil_dir.mkdir(parents=True)
        (self.perfil_dir / "plugins.txt").write_text("", encoding="utf-8")
        self.fisico = base / "game" / "Data"
        self.fisico.mkdir(parents=True)
        self.virtual = base / "game" / "DataVista"
        self.virtual.mkdir(parents=True)

    @property
    def modlist(self) -> pathlib.Path:
        return self.perfil_dir / "modlist.txt"

    def escribir_modlist(self, *habilitados_en_orden_de_archivo: str) -> None:
        """MO2 escribe la MAYOR prioridad primero; ``read_enabled_mods`` lo invierte."""
        self.modlist.write_text(
            "\n".join(f"+{nombre}" for nombre in habilitados_en_orden_de_archivo) + "\n",
            encoding="utf-8",
        )

    def carpeta(self, nombre: str) -> pathlib.Path:
        ruta = self.mods / nombre
        ruta.mkdir(parents=True, exist_ok=True)
        return ruta

    def canary(self, nombre: str, contenido: bytes = _CANARIO) -> pathlib.Path:
        ruta = self.carpeta(nombre) / _RELATIVO
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(contenido)
        return ruta

    def fisico_con(self, contenido: bytes = _FISICO) -> None:
        ruta = self.fisico / _RELATIVO
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(contenido)

    def poblar(self, nombres: tuple[str, ...]) -> None:
        for nombre in nombres:
            self.carpeta(nombre)

    def canary_en_ambas_grafias(self) -> None:
        """NTFS: ``x`` y ``X`` son la MISMA carpeta. POSIX: se crean las dos."""
        self.canary("x")
        self.canary("X")


@pytest.fixture
def inst(tmp_path: pathlib.Path) -> Instancia:
    return Instancia(tmp_path)


def _elegir(inst: Instancia, seleccion: str, *, source_mod: str = "x", permitir_diferencial: bool = False):
    comunes = {
        "data_root": inst.data_root,
        "mods_dir": inst.mods,
        "profile": _PERFIL,
        "physical_data_dir": inst.fisico,
        "permitir_diferencial": permitir_diferencial,
    }
    if seleccion == "libre":
        return build_attestation_challenge(**comunes)
    return build_attestation_challenge_for_source(source_mod=source_mod, **comunes)


# ---------------------------------------------------------------------------
# 1. La PUERTA: el lector canónico no entrega un perfil ambiguo a nadie
# ---------------------------------------------------------------------------


def test_read_enabled_mods_rechaza_el_mismo_mod_en_dos_grafias(inst: Instancia) -> None:
    """``+x`` y ``+X`` son DOS entradas del MISMO mod: la prioridad no se puede afirmar."""
    inst.escribir_modlist("X", "B", "x", "A")

    with pytest.raises(VfsAttestationError, match="repite el mod"):
        read_enabled_mods(inst.modlist)


def test_read_enabled_mods_no_colapsa_lo_que_ntfs_no_unifica(inst: Instancia) -> None:
    """``casefold()`` uniría ``Straße``/``Strasse``, que NTFS NO unifica: prohibido."""
    inst.escribir_modlist("Straße", "Strasse")

    assert read_enabled_mods(inst.modlist) == ("Strasse", "Straße")


def test_read_enabled_mods_conserva_el_orden_y_la_grafia_del_archivo(inst: Instancia) -> None:
    """No-regresión: el fix no reescribe nombres ni altera la normalización de prioridad."""
    inst.escribir_modlist("Alta", "Media", "Baja")

    assert read_enabled_mods(inst.modlist) == ("Baja", "Media", "Alta")


# ---------------------------------------------------------------------------
# 2. mod_effectivity: la función PÚBLICA, con la tupla inyectada tal como llega
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("grafia", ["x", "X"], ids=["minuscula", "mayuscula"])
def test_mod_effectivity_rechaza_el_perfil_ambiguo_con_cualquier_grafia(inst: Instancia, grafia: str) -> None:
    inst.escribir_modlist("X", "B", "x", "A")
    inst.poblar(_AMBIGUO)
    artifact = inst.canary("x", contenido=b"ARTEFACTO")
    inst.canary("X", contenido=b"ARTEFACTO")

    with pytest.raises(ModEffectivityError, match="repite el mod"):
        verificar_artifact_efectivo(
            artifact_root=artifact.parent,
            mod_name=grafia,
            mods_dir=inst.mods,
            data_root=inst.data_root,
            enabled=_AMBIGUO,
        )


@pytest.mark.parametrize("grafia", ["x", "X"], ids=["minuscula", "mayuscula"])
def test_mod_effectivity_acepta_un_perfil_valido_aunque_el_argumento_difiera_en_mayusculas(
    inst: Instancia, grafia: str
) -> None:
    inst.escribir_modlist("B", "x", "A")
    inst.poblar(_VALIDO)
    artifact = inst.canary("x", contenido=b"ARTEFACTO")
    inst.canary("X", contenido=b"ARTEFACTO")

    veredicto = verificar_artifact_efectivo(
        artifact_root=artifact.parent,
        mod_name=grafia,
        mods_dir=inst.mods,
        data_root=inst.data_root,
        enabled=_VALIDO,
    )

    assert veredicto == 1


def test_mod_effectivity_sigue_rechazando_un_mod_realmente_no_habilitado(inst: Instancia) -> None:
    """No-regresión: identidad canónica no significa admitir cualquier nombre."""
    inst.escribir_modlist("B", "x", "A")
    inst.poblar(_VALIDO)
    artifact = inst.canary("x", contenido=b"ARTEFACTO")

    with pytest.raises(ModEffectivityError, match="no está habilitado"):
        verificar_artifact_efectivo(
            artifact_root=artifact.parent,
            mod_name="Z",
            mods_dir=inst.mods,
            data_root=inst.data_root,
            enabled=_VALIDO,
        )


# ---------------------------------------------------------------------------
# 3. _construir_challenge: selección restringida / libre × canary ausente / diferencial
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seleccion", ["source_mod", "libre"], ids=["source_mod", "libre"])
@pytest.mark.parametrize("modalidad", ["ausente", "diferencial"], ids=["ausente", "diferencial"])
def test_construir_challenge_rechaza_el_perfil_ambiguo_en_todas_las_modalidades(
    inst: Instancia, seleccion: str, modalidad: str
) -> None:
    inst.escribir_modlist("X", "B", "x", "A")
    inst.poblar(_AMBIGUO)
    inst.canary_en_ambas_grafias()
    if modalidad == "diferencial":
        inst.fisico_con()

    with pytest.raises(VfsAttestationError, match="repite el mod"):
        _elegir(inst, seleccion, permitir_diferencial=True)


def test_construir_challenge_falla_cerrado_ante_un_enabled_ambiguo_inyectado(
    inst: Instancia, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La comprobación vive en el GATE, no sólo en el lector: un ``enabled`` ajeno no la bypasea."""
    inst.escribir_modlist("B", "x", "A")
    inst.poblar(_AMBIGUO)
    inst.canary_en_ambas_grafias()
    monkeypatch.setattr(vfs_attestation, "read_enabled_mods", lambda _ruta: _AMBIGUO)

    with pytest.raises(VfsAttestationError, match="repite el mod"):
        _elegir(inst, "source_mod")


@pytest.mark.parametrize("grafia", ["x", "X"], ids=["minuscula", "mayuscula"])
def test_construir_challenge_resuelve_el_indice_por_identidad_no_por_grafia(inst: Instancia, grafia: str) -> None:
    """Mismo perfil, mismo artifact: pedir ``X`` no puede cambiar el canary ni bloquear."""
    inst.escribir_modlist("B", "x", "A")
    inst.poblar(_VALIDO)
    inst.canary_en_ambas_grafias()

    desafio = _elegir(inst, "source_mod", source_mod=grafia)

    assert desafio.relative_path.as_posix() == _RELATIVO
    assert desafio.sha256 == hashlib.sha256(_CANARIO).hexdigest()


# ---------------------------------------------------------------------------
# 4. Los DOS gates de brokered_dyndolod
# ---------------------------------------------------------------------------


class _BrokerSinSesion:
    async def submit(self, *_args: object, **_kwargs: object) -> object:
        raise RuntimeError("probe no disponible en este test")

    async def open_session(self, *_args: object, **_kwargs: object) -> object:
        raise RuntimeError("sesión no disponible en este test")


def _estrategia(inst: Instancia) -> BrokeredDynDOLODSpawnStrategy:
    return BrokeredDynDOLODSpawnStrategy(
        broker=_BrokerSinSesion(),
        instance_id="portable-main",
        profile=_PERFIL,
        data_root=inst.data_root,
        mods_dir=inst.mods,
        install_root=inst.data_root,
        physical_data_dir=inst.fisico,
        virtual_data_dir=inst.virtual,
        output_roots={},
    )


def _request(inst: Instancia, *, mod_name: str, artifact_root: pathlib.Path) -> TexGenHandoffRequest:
    return TexGenHandoffRequest(
        mod_name=mod_name,
        staging=artifact_root,
        data_dir=inst.virtual,
        expected_profile=_PERFIL,
    )


def _approval(inst: Instancia, *, mod_name: str, artifact_root: pathlib.Path) -> TexGenHandoffApproval:
    return TexGenHandoffApproval(
        mod_name=mod_name,
        artifact_root=artifact_root,
        artifact=digest_arbol(artifact_root),
        data_dir=inst.virtual,
        profile=_PERFIL,
    )


async def test_gate_de_handoff_bloquea_el_perfil_ambiguo(inst: Instancia) -> None:
    """Gate 2 (``verify_texgen_handoff``): B3 tiene que nombrar la ambigüedad."""
    inst.escribir_modlist("X", "B", "x", "A")
    inst.poblar(_AMBIGUO)
    artifact = inst.canary("x")
    inst.canary("X")

    resultado = await _estrategia(inst).verify_texgen_handoff(
        _request(inst, mod_name="x", artifact_root=artifact.parent)
    )

    assert not resultado.verified
    assert "repite el mod" in resultado.reason


async def test_gate_de_handoff_no_rechaza_por_grafia_un_perfil_valido(inst: Instancia) -> None:
    """Gate 2: pedir ``X`` sobre ``+x`` NO es «mod no habilitado» (sin pending_action)."""
    inst.escribir_modlist("B", "x", "A")
    inst.poblar(_VALIDO)
    artifact = inst.canary("X")

    resultado = await _estrategia(inst).verify_texgen_handoff(
        _request(inst, mod_name="X", artifact_root=artifact.parent)
    )

    assert resultado.pending_action != "profile_enablement"
    assert "NO está habilitado" not in resultado.reason


async def test_revalidacion_del_spawn_bloquea_el_perfil_ambiguo(inst: Instancia) -> None:
    """Gate 1 (``_revalidar_aprobacion``): el boundary del spawn falla cerrado."""
    inst.escribir_modlist("X", "B", "x", "A")
    inst.poblar(_AMBIGUO)
    artifact = inst.canary("x")
    inst.canary("X")

    with pytest.raises(HandoffDriftError, match="repite el mod"):
        await _estrategia(inst)._revalidar_aprobacion(_approval(inst, mod_name="x", artifact_root=artifact.parent))


async def test_revalidacion_del_spawn_no_rechaza_por_grafia_un_perfil_valido(inst: Instancia) -> None:
    """Gate 1: el enablement pasa y el corte ocurre DESPUÉS, por otro motivo ligado al token."""
    inst.escribir_modlist("B", "x", "A")
    inst.poblar(_VALIDO)
    artifact = inst.canary("X")

    with pytest.raises(HandoffDriftError) as capturado:
        await _estrategia(inst)._revalidar_aprobacion(_approval(inst, mod_name="X", artifact_root=artifact.parent))

    assert "habilitado exactamente una vez" not in str(capturado.value)


async def test_gate_de_handoff_falla_cerrado_con_enabled_ambiguo_inyectado(
    inst: Instancia, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate 2: su PROPIA comprobación de ambigüedad, sin depender del lector."""
    inst.escribir_modlist("B", "x", "A")
    inst.poblar(_AMBIGUO)
    artifact = inst.canary("x")
    inst.canary("X")
    monkeypatch.setattr(brokered_dyndolod, "read_enabled_mods", lambda _ruta: _AMBIGUO)

    resultado = await _estrategia(inst).verify_texgen_handoff(
        _request(inst, mod_name="x", artifact_root=artifact.parent)
    )

    assert not resultado.verified
    assert "repite el mod" in resultado.reason


async def test_revalidacion_del_spawn_falla_cerrado_con_enabled_ambiguo_inyectado(
    inst: Instancia, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate 1: su PROPIA comprobación de ambigüedad, sin depender del lector."""
    inst.escribir_modlist("B", "x", "A")
    inst.poblar(_AMBIGUO)
    artifact = inst.canary("x")
    inst.canary("X")
    monkeypatch.setattr(brokered_dyndolod, "read_enabled_mods", lambda _ruta: _AMBIGUO)

    with pytest.raises(HandoffDriftError, match="repite el mod"):
        await _estrategia(inst)._revalidar_aprobacion(_approval(inst, mod_name="x", artifact_root=artifact.parent))


# ---------------------------------------------------------------------------
# 5. Clasificación corregida: diagnostico_perfil YA cumple el contrato
# ---------------------------------------------------------------------------


def test_cargar_perfil_ya_falla_cerrado_ante_duplicados_de_distinta_grafia(inst: Instancia) -> None:
    """Traza: cargar_perfil → indexar_ganadores → _exigir_enabled → GanadoresError → PerfilError.

    Este módulo NO se modifica: ya delega la unicidad en la primitiva canónica.
    """
    inst.escribir_modlist("X", "x")

    with pytest.raises(PerfilError, match="repite el mod"):
        cargar_perfil(mods_dir=inst.mods, data_root=inst.data_root, perfil=_PERFIL, mod="x")


def test_cargar_perfil_resuelve_el_nombre_por_identidad(inst: Instancia) -> None:
    """No-regresión: elige la grafía que está en el modlist, no la del argumento."""
    inst.escribir_modlist("x")
    inst.canary("x")

    resuelto = cargar_perfil(mods_dir=inst.mods, data_root=inst.data_root, perfil=_PERFIL, mod="X")

    assert resuelto.mod == "x"
    assert resuelto.enabled == ("x",)


# ---------------------------------------------------------------------------
# 6. Los DOS consumidores que la issue no censó
# ---------------------------------------------------------------------------


class _ResolverMO2:
    def __init__(self, raiz: pathlib.Path) -> None:
        self._raiz = raiz

    def get_mo2_instance_data_root(self) -> pathlib.Path:
        return self._raiz


def test_wrye_bash_degrada_la_cobertura_ante_un_perfil_ambiguo(inst: Instancia) -> None:
    """``_mods_habilitados`` devuelve ``None`` ⇒ ``bashed_patch_en_overlay`` NO afirma cobertura completa.

    Sin la comprobación en la PUERTA (``read_enabled_mods``) el servicio entregaba
    ``(\"x\", \"X\")`` y el resolver declaraba ``cobertura_completa=True`` sobre un perfil
    ambiguo. No se toca ``output_targets.py:290``: el arreglo vive en el lector.
    """
    inst.escribir_modlist("X", "x")
    servicio = WryeBashPipelineService(path_resolver=_ResolverMO2(inst.data_root))

    habilitados = servicio._mods_habilitados(_PERFIL)

    assert habilitados is None
    resultado = bashed_patch_en_overlay(
        game=inst.base / "game",
        mo2=inst.data_root,
        mods_dir=inst.mods,
        mods_habilitados=habilitados,
    )
    assert resultado is not None
    assert resultado.cobertura_completa is False


def test_bashed_patch_en_overlay_no_decide_por_grafia(inst: Instancia) -> None:
    """Clasificación: este consumidor ENUMERA capas; no compara un nombre pedido."""
    comunes = {"game": inst.base / "game", "mo2": inst.data_root, "mods_dir": inst.mods}
    con_minuscula = bashed_patch_en_overlay(**comunes, mods_habilitados=_VALIDO)
    con_mayuscula = bashed_patch_en_overlay(**comunes, mods_habilitados=("A", "X", "B"))

    assert con_minuscula is not None and con_mayuscula is not None
    assert (con_minuscula.ruta, con_minuscula.capa, con_minuscula.cobertura_completa) == (
        con_mayuscula.ruta,
        con_mayuscula.capa,
        con_mayuscula.cobertura_completa,
    )


def test_validador_de_etapas_previas_degrada_a_no_enumerable_ante_ambiguedad(inst: Instancia) -> None:
    """El validador LOD es best-effort: un perfil ambiguo no puede hacerle afirmar la etapa 8."""
    inst.escribir_modlist("X", "x")
    grass = inst.carpeta("x") / "Grass"
    grass.mkdir(parents=True, exist_ok=True)
    (grass / "Tamriel.cgid").write_bytes(b"cgid")

    evidencia = verificar_etapas_previas_de_lods(
        plugins_txt=None,
        modlist_txt=inst.modlist,
        mods_dir=inst.mods,
        overwrite_dir=None,
    )

    assert any("etapa 8" in faltante for faltante in evidencia.faltantes)


# ---------------------------------------------------------------------------
# 7. Anclas por ENUMERACIÓN (AST): consumidores y mecanismos de identidad
# ---------------------------------------------------------------------------

#: TODOS los USOS de producción de ``read_enabled_mods``, derivados por AST.
#: Se cuentan referencias, no sólo llamadas: ``brokered_dyndolod`` pasa la función
#: como objeto a ``asyncio.to_thread`` (dos sitios) en vez de invocarla.
CENSO_DE_LECTORES = {
    "sky_claw/local/assets/asset_scanner.py": 1,
    "sky_claw/local/mo2/brokered_dyndolod.py": 2,
    "sky_claw/local/mo2/vfs_attestation.py": 1,
    "sky_claw/local/native_parallax/diagnostico_perfil.py": 1,
    "sky_claw/local/tools/wrye_bash_service.py": 1,
    "sky_claw/local/validators/etapas_previas_lods.py": 1,
}

#: Cómo contesta cada consumidor "¿dónde está este mod?". Incluye los INDIRECTOS
#: (reciben la tupla sin llamar al lector). Un consumidor nuevo rompe el ancla.
CLASIFICACION_DE_CONSUMIDORES = {
    "sky_claw/local/assets/asset_scanner.py": "enumera_capas",
    "sky_claw/local/mo2/brokered_dyndolod.py": "resuelve_indice",
    "sky_claw/local/mo2/ganadores.py": "exige_unico",
    "sky_claw/local/mo2/mod_effectivity.py": "resuelve_indice",
    "sky_claw/local/mo2/vfs_attestation.py": "lee_y_resuelve_indice",
    "sky_claw/local/native_parallax/diagnostico_perfil.py": "delega_a_indexar_ganadores",
    "sky_claw/local/tools/output_targets.py": "enumera_capas",
    "sky_claw/local/tools/wrye_bash_service.py": "delega_a_bashed_patch_en_overlay",
    "sky_claw/local/validators/etapas_previas_lods.py": "enumera_capas",
}

MECANISMO_POR_CLASE = {
    "resuelve_indice": "_coincidencias_de_mod",
    "lee_y_resuelve_indice": "_coincidencias_de_mod",
    "exige_unico": "_exigir_sin_repetidos",
    "delega_a_indexar_ganadores": "indexar_ganadores",
    "delega_a_bashed_patch_en_overlay": "bashed_patch_en_overlay",
    "enumera_capas": None,
}

#: ÚNICAS comparaciones crudas que quedan en ``sky_claw/``: NO consumen el modlist.
COMPARACIONES_CRUDAS_AJENAS = {
    "sky_claw/app_context.py": 1,  # ``mods`` = candidatos de configuración
    "sky_claw/local/validators/missing_masters.py": 1,  # ``key in enabled`` = plugins.txt
}

_SECUENCIAS = {"enabled", "habilitados", "mods_habilitados", "raw_enabled", "mods"}


@functools.lru_cache(maxsize=1)
def _arboles() -> tuple[tuple[str, ast.Module], ...]:
    raiz = _RAIZ_DEL_REPO / "sky_claw"
    return tuple(
        (ruta.relative_to(_RAIZ_DEL_REPO).as_posix(), ast.parse(ruta.read_bytes(), filename=str(ruta)))
        for ruta in sorted(raiz.rglob("*.py"))
    )


def _llamadas(arbol: ast.Module, nombre: str) -> int:
    return sum(
        1
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        and (
            (isinstance(nodo.func, ast.Name) and nodo.func.id == nombre)
            or (isinstance(nodo.func, ast.Attribute) and nodo.func.attr == nombre)
        )
    )


def _referencias(arbol: ast.Module, nombre: str) -> int:
    """Usos en contexto de lectura (incluye pasar la función por referencia)."""
    return sum(
        1
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Name) and isinstance(nodo.ctx, ast.Load) and nodo.id == nombre
    )


def _comparaciones_crudas(arbol: ast.Module) -> int:
    total = 0
    for nodo in ast.walk(arbol):
        if (
            isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Attribute)
            and nodo.func.attr in {"index", "count"}
            and isinstance(nodo.func.value, ast.Name)
            and nodo.func.value.id in _SECUENCIAS
        ):
            total += 1
        if isinstance(nodo, ast.Compare) and any(isinstance(op, (ast.In, ast.NotIn)) for op in nodo.ops):
            if any(isinstance(c, ast.Name) and c.id in _SECUENCIAS for c in nodo.comparators):
                total += 1
            if isinstance(nodo.left, ast.Name) and nodo.left.id in _SECUENCIAS:
                total += 1
    return total


def test_el_censo_de_lectores_de_read_enabled_mods_esta_completo() -> None:
    """AST sobre TODO ``sky_claw/``: un lector nuevo rompe el ancla."""
    censo = {modulo: n for modulo, arbol in _arboles() if (n := _referencias(arbol, "read_enabled_mods"))}
    assert censo == CENSO_DE_LECTORES


def test_todos_los_consumidores_estan_clasificados_y_usan_el_mecanismo_declarado() -> None:
    """La clasificación no es un párrafo: cada módulo ejerce el mecanismo que declara."""
    arboles = dict(_arboles())
    assert set(CENSO_DE_LECTORES) <= set(CLASIFICACION_DE_CONSUMIDORES)
    for modulo, clase in CLASIFICACION_DE_CONSUMIDORES.items():
        assert modulo in arboles, f"{modulo} desapareció: actualizá el censo"
        arbol = arboles[modulo]
        mecanismo = MECANISMO_POR_CLASE[clase]
        if mecanismo is None:
            for candidato in MECANISMO_POR_CLASE.values():
                if candidato is not None:
                    assert _llamadas(arbol, candidato) == 0, f"{modulo} resolvió identidad por su cuenta"
        else:
            assert _llamadas(arbol, mecanismo) >= 1, f"{modulo} no usa {mecanismo}"


def test_ningun_consumidor_del_modlist_resuelve_identidad_por_grafia() -> None:
    """AST sobre TODO ``sky_claw/``: ``count``/``index``/``in`` sobre ``enabled`` están prohibidos."""
    crudas = {modulo: n for modulo, arbol in _arboles() if (n := _comparaciones_crudas(arbol))}
    assert crudas == COMPARACIONES_CRUDAS_AJENAS


def test_los_resolutores_de_identidad_estan_enumerados() -> None:
    """El conjunto de módulos que usan la primitiva canónica está congelado por igualdad literal."""
    usuarios = {
        modulo
        for modulo, arbol in _arboles()
        if _llamadas(arbol, "_coincidencias_de_mod") or _llamadas(arbol, "_exigir_sin_repetidos")
    }
    assert usuarios == {
        "sky_claw/local/mo2/ganadores.py",
        "sky_claw/local/mo2/mod_effectivity.py",
        "sky_claw/local/mo2/vfs_attestation.py",
        "sky_claw/local/mo2/brokered_dyndolod.py",
    }


def test_la_identidad_del_mod_es_lower_y_nunca_casefold() -> None:
    arboles = dict(_arboles())
    for modulo, nombre in (
        ("sky_claw/local/mo2/ganadores.py", "_clave_de_mod"),
        ("sky_claw/local/mo2/vfs.py", "_clave_de_nombre_de_mod"),
    ):
        funcion = next(
            (n for n in ast.walk(arboles[modulo]) if isinstance(n, ast.FunctionDef) and n.name == nombre),
            None,
        )
        assert funcion is not None, f"falta la primitiva de identidad {nombre} en {modulo}"
        # Sólo el cuerpo EJECUTABLE: el docstring nombra ``casefold()`` para prohibirlo
        # y no debe contarse como una llamada.
        sentencias = [
            n
            for n in funcion.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str))
        ]
        cuerpo = ast.unparse(ast.Module(body=sentencias, type_ignores=[]))
        assert ".lower()" in cuerpo, f"{nombre} dejó de usar lower()"
        assert "casefold(" not in cuerpo, f"{nombre} usa casefold(): colapsa pares que NTFS distingue"

    assert _clave_de_nombre_de_mod("X") == _clave_de_nombre_de_mod("x")
    assert _clave_de_nombre_de_mod("Straße") != _clave_de_nombre_de_mod("Strasse")


def test_la_primitiva_canonica_devuelve_todas_las_coincidencias() -> None:
    helper = getattr(ganadores, "_coincidencias_de_mod", None)
    assert helper is not None, "falta _coincidencias_de_mod: la resolución de índice no está unificada"

    assert helper(_AMBIGUO, "x") == [1, 3]
    assert helper(_AMBIGUO, "X") == [1, 3]
    assert helper(_VALIDO, "X") == [1]
    assert helper(_VALIDO, "Z") == []
