"""Cableado del sensor de visibilidad de mods en los servicios (U-01).

**Test-ancla de una CLASE de defecto, no de un caso.** El defecto #1 del repo es
el fix que aterriza en un camino y deja intacto al gemelo (13 de 21 follow-ups
auditados). U-01 es exactamente esa forma: el falso verde del run standalone no
vive en un servicio, vive en TODOS los que construyen un preflight. Por eso acá
se **enumera la familia** (patrón ``RITUAL_TOOL_MAP`` de
``tests/test_ritual_dispatch.py``) y un guard de completitud falla si aparece un
servicio nuevo que nadie clasificó — en vez de seis tests escritos a mano que
callan sobre el séptimo.

**La invariante evolucionó (PR-586F):** ya no es "servicio → tiene/no tiene
sensor", sino **"servicio + execution domain → sensor físico requerido / no
aplicable"**. El sensor mide el ``Data`` FÍSICO: donde el backend que ejecuta la
corrida consume el namespace virtual MO2/USVFS, medirlo es un ROJO FALSO — el
incidente real de PR-586E (brokered, mods nunca materializados a disco,
``PreflightBlocked`` antes del spawn). La tabla
:data:`SENSOR_FISICO_POR_DOMINIO` expresa esa relación y los tests de
comportamiento se derivan de ella: proteger la CLASE del defecto, no hacer
pasar CI editando una lista.

Ver ``sky_claw/local/validators/vfs_visibility`` para el porqué del sensor.
"""

from __future__ import annotations

import ast
import pathlib
import re
from typing import Any
from unittest.mock import MagicMock

import pytest

from sky_claw.local.validators.preflight import PreflightStatus

_TOOLS_DIR = pathlib.Path(__file__).resolve().parent.parent / "sky_claw" / "local" / "tools"

#: PR-586F — la invariante de la familia, expresada como
#: **servicio + execution domain → ¿el sensor físico es requerido?**
#:
#: * ``"physical"`` — el backend lee el ``<game>/Data`` físico (standalone
#:   histórico): U-01 aplica y el sensor DEBE estar cableado.
#: * ``"virtual_usvfs"`` — el backend corre bajo la USVFS del broker y lee el
#:   overlay del perfil: medir el Data físico es un rojo falso. La visibilidad
#:   la demuestra el contrato VFS específico del backend (handoff brokered,
#:   efectividad, canary runtime), NUNCA este sensor.
#:
#: Un servicio sin la clave de un dominio NO ejecuta en ese dominio. La
#: representación honesta del "no aplicable" difiere por servicio según su
#: ``omit_unconfigured``: DynDOLOD omite el checkpoint; LOOT lo emite como
#: "no configurado". Ambas son correctas — lo que se prohibe es un rojo por una
#: medición que no significa nada para ese backend.
SENSOR_FISICO_POR_DOMINIO: dict[str, dict[str, bool]] = {
    "loot_service": {"physical": True, "virtual_usvfs": False},
    "dyndolod_service": {"physical": True, "virtual_usvfs": False},
    "synthesis_service": {"physical": True},
    "pandora_service": {"physical": True},
    "wrye_bash_service": {"physical": True},
}

#: Servicios cuyo preflight DEBE cablear el sensor de visibilidad (U-01) en
#: ALGÚN dominio de ejecución (derivado de :data:`SENSOR_FISICO_POR_DOMINIO`).
SERVICIOS_CON_VISIBILIDAD: frozenset[str] = frozenset(
    servicio for servicio, dominios in SENSOR_FISICO_POR_DOMINIO.items() if any(dominios.values())
)

#: Excluidos **con motivo explícito** (la regla de "Hermanos" del template pide
#: nombrarlos, no darlos por descartados de memoria).
SERVICIOS_SIN_VISIBILIDAD: dict[str, str] = {
    "xedit_service": (
        "Su preflight gatea SOLO quick_auto_clean, que limpia los DLC oficiales "
        "(Update/Dawnguard/HearthFires/Dragonborn) que viven en el Data del juego "
        "con o sin VFS. Un ROJO por visibilidad ahí bloquearía una limpieza que "
        "funciona perfecto en standalone: falso positivo, justo lo que el sensor "
        "está diseñado para NO hacer. execute_patch no usa preflight."
    ),
}


def _modulos_con_preflight_vfs() -> set[str]:
    """Servicios que construyen un sensor de VFS — la familia a clasificar."""
    return {
        ruta.stem for ruta in _TOOLS_DIR.glob("*_service.py") if "build_vfs_sensor(" in ruta.read_text(encoding="utf-8")
    }


def test_la_enumeracion_cubre_toda_la_familia() -> None:
    """Guard de completitud: un servicio nuevo con preflight VFS obliga a
    decidir si lleva el sensor, en vez de heredar el falso verde en silencio."""
    clasificados = SERVICIOS_CON_VISIBILIDAD | set(SERVICIOS_SIN_VISIBILIDAD)

    assert _modulos_con_preflight_vfs() == clasificados


def test_ningun_servicio_hardcodea_scan_mods_dir_false() -> None:
    """``scan_mods_dir`` se deriva de si la raíz MO2 está validada (patrón de
    ``loot_service``), nunca se fija en False: hardcodearlo dejaba el scan de
    symlinks de ``mods/`` ciego incluso con una instancia MO2 legítima."""
    culpables = [
        ruta.name
        for ruta in _TOOLS_DIR.glob("*_service.py")
        if re.search(r"scan_mods_dir\s*=\s*False", ruta.read_text(encoding="utf-8"))
    ]

    assert culpables == []


# ---------------------------------------------------------------------------
# Comportamiento: el escenario real de U-01, servicio por servicio
# ---------------------------------------------------------------------------


def _instancia(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """Juego con Data vainilla + MO2 cuyo perfil activa un mod NO materializado.

    Es el escenario exacto de U-01: MO2 en USVFS estándar, Sky-Claw corriendo
    standalone, así que ``MiMod.esp`` existe bajo ``mods/`` pero el tool leería
    un ``Data`` sin él.
    """
    data = tmp_path / "Skyrim" / "Data"
    data.mkdir(parents=True)
    (data / "Skyrim.esm").write_bytes(b"TES4")

    mo2 = tmp_path / "MO2"
    (mo2 / "mods" / "MiMod").mkdir(parents=True)
    (mo2 / "mods" / "MiMod" / "MiMod.esp").write_bytes(b"TES4")
    (mo2 / "overwrite").mkdir(parents=True)
    perfil = mo2 / "profiles" / "Default"
    perfil.mkdir(parents=True)
    (perfil / "plugins.txt").write_bytes(b"\xef\xbb\xbf*Skyrim.esm\r\n*MiMod.esp\r\n")
    return tmp_path / "Skyrim", mo2


def _resolver(*, skyrim: pathlib.Path, mo2: pathlib.Path) -> MagicMock:
    """Resolver con rutas crudas y validadas (mismo shape que test_preflight_wiring)."""
    resolver = MagicMock()
    resolver.get_skyrim_path_raw = MagicMock(return_value=skyrim)
    resolver.get_skyrim_path = MagicMock(return_value=skyrim)
    resolver.get_mo2_path_raw = MagicMock(return_value=mo2)
    resolver.get_mo2_path = MagicMock(return_value=mo2)
    # Raíz de datos para los preflights migrados (portable: install == data).
    resolver.get_mo2_instance_data_root = MagicMock(return_value=mo2)
    resolver.get_mo2_mods_path = MagicMock(return_value=mo2 / "mods")
    resolver.has_explicit_mo2_install_selection = MagicMock(return_value=False)
    resolver.detect_mo2_path = MagicMock(return_value=mo2)
    resolver.get_active_profile = MagicMock(return_value="Default")
    resolver.get_loot_exe = MagicMock(return_value=None)
    resolver.get_pandora_exe = MagicMock(return_value=None)
    return resolver


def _pares_de_dominio(dominio: str, *, sensor_requerido: bool) -> list[tuple[str, str]]:
    """Pares (servicio, dominio) de :data:`SENSOR_FISICO_POR_DOMINIO` que coinciden.

    ``dominio in dominios`` (no ``.get``) distingue "este servicio no ejecuta en
    ese dominio" de "ejecuta ahí y el sensor NO aplica" — el segundo es el caso
    del falso rojo brokered; el primero simplemente no existe.
    """
    return sorted(
        (servicio, dominio)
        for servicio, dominios in SENSOR_FISICO_POR_DOMINIO.items()
        if dominio in dominios and dominios[dominio] is sensor_requerido
    )


class _EstrategiaVirtualDePrueba:
    """Strategy mínima que declara el dominio ``virtual_usvfs`` POR CONTRATO.

    No hereda de ``BrokeredDynDOLODSpawnStrategy``: lo que el servicio reconoce
    es la capability, no la clase concreta (la arquitectura del runner prohíbe
    adivinar el backend por ``isinstance``).
    """

    data_visibility_domain = "virtual_usvfs"

    async def spawn(self, **_kwargs: Any) -> Any:
        raise AssertionError("el preflight no spawnea")

    async def verify_texgen_handoff(self, _request: Any) -> Any:
        raise AssertionError("el preflight no gatea handoffs")


class _WrapperDeStrategy:
    """Decorator/RecordingStrategy: delega el contrato COMPLETO a la strategy real.

    Es el shape del rig de aceptación PR-586E: un wrapper de observación que
    envuelve la strategy real. La capability viaja por el contrato normal (una
    propiedad delegada), no por introspección de ``_inner``/``delegate``.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def data_visibility_domain(self) -> str:
        return self._inner.data_visibility_domain

    async def spawn(self, **kwargs: Any) -> Any:
        return await self._inner.spawn(**kwargs)

    async def verify_texgen_handoff(self, request: Any) -> Any:
        return await self._inner.verify_texgen_handoff(request)


class _EstrategiaIncompleta:
    """Sólo ``spawn``: NO implementa ``data_visibility_domain``.

    Un doble incompleto no puede apagar U-01: lo indeterminado falla cerrado.
    """

    async def spawn(self, **_kwargs: Any) -> Any:
        raise AssertionError("nunca debe llegar al spawn")


def _construir(nombre: str, *, resolver: MagicMock, mo2: pathlib.Path, dominio: str = "physical") -> Any:
    """Instancia el servicio *nombre* con colaboradores mockeados, en *dominio*."""
    comunes: dict[str, Any] = {
        "lock_manager": MagicMock(),
        "snapshot_manager": MagicMock(),
        "path_resolver": resolver,
    }
    if nombre == "loot_service":
        from sky_claw.local.mo2.load_order import LoadOrderPaths
        from sky_claw.local.tools.loot_service import LootSortingService

        # LOOT toma los habilitados de su resolver de load order (prefiere
        # plugins.txt sobre loadorder.txt); apuntarlo al del perfil real.
        load_order = MagicMock()
        load_order.resolve.return_value = LoadOrderPaths(
            files=(mo2 / "profiles" / "Default" / "plugins.txt",), sources=("mo2_profile",)
        )
        if dominio == "virtual_usvfs":
            # SIN loot_runner inyectado: `_ensure_loot_runner` construye el
            # `BrokeredLootRunner` del broker y el sort corre dentro de la USVFS.
            return LootSortingService(**comunes, load_order_resolver=load_order, vfs_broker=MagicMock())
        return LootSortingService(**comunes, loot_runner=MagicMock(), load_order_resolver=load_order)
    if nombre == "dyndolod_service":
        from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

        strategy: Any = None
        if dominio == "virtual_usvfs":
            strategy = _EstrategiaVirtualDePrueba()
        return DynDOLODPipelineService(**comunes, journal=MagicMock(), event_bus=MagicMock(), spawn_strategy=strategy)
    if nombre == "synthesis_service":
        from sky_claw.local.tools.synthesis_service import SynthesisPipelineService

        return SynthesisPipelineService(**comunes, journal=MagicMock(), event_bus=MagicMock())
    if nombre == "pandora_service":
        from sky_claw.local.tools.pandora_service import PandoraPipelineService

        return PandoraPipelineService(**comunes)
    if nombre == "wrye_bash_service":
        from sky_claw.local.tools.wrye_bash_service import WryeBashPipelineService

        return WryeBashPipelineService(**comunes)
    raise AssertionError(f"servicio no contemplado en el factory: {nombre}")


@pytest.mark.parametrize(("servicio", "dominio"), _pares_de_dominio("physical", sensor_requerido=True))
async def test_el_modlist_invisible_pone_el_preflight_rojo(servicio: str, dominio: str, tmp_path: pathlib.Path) -> None:
    """El falso verde de U-01, cerrado en CADA servicio de la familia, en el
    dominio donde el sensor significa algo (backend que lee el Data físico): el
    perfil activa un mod que el Data del juego no tiene → el Ritual no corre."""
    skyrim, mo2 = _instancia(tmp_path)
    svc = _construir(servicio, resolver=_resolver(skyrim=skyrim, mo2=mo2), mo2=mo2, dominio=dominio)

    preflight = svc._ensure_preflight()
    assert preflight is not None, f"{servicio} no construyó preflight con game+MO2 resolubles"
    reporte = await preflight.run()

    visibilidad = next((c for c in reporte.checks if c.name == "vfs_visibility"), None)
    assert visibilidad is not None, f"{servicio} no cableó el sensor de visibilidad"
    assert visibilidad.status is PreflightStatus.RED
    assert any("MiMod.esp" in d for d in visibilidad.details)
    assert reporte.blocks_mutations is True


@pytest.mark.parametrize(("servicio", "dominio"), _pares_de_dominio("physical", sensor_requerido=True))
async def test_el_modlist_visible_no_bloquea(servicio: str, dominio: str, tmp_path: pathlib.Path) -> None:
    """Contracara imprescindible: con los mods materializados el gate no
    estorba. Sin este test, 'siempre rojo' pasaría el test de arriba."""
    skyrim, mo2 = _instancia(tmp_path)
    (skyrim / "Data" / "MiMod.esp").write_bytes(b"TES4")  # materializado
    svc = _construir(servicio, resolver=_resolver(skyrim=skyrim, mo2=mo2), mo2=mo2, dominio=dominio)

    reporte = await svc._ensure_preflight().run()

    visibilidad = next(c for c in reporte.checks if c.name == "vfs_visibility")
    assert visibilidad.status is PreflightStatus.GREEN
    # Un verde "no configurado" pasaría este test sin que el sensor exista:
    # exigir que el verde venga de una MEDICIÓN real (los servicios con
    # omit_unconfigured=False emiten ese checkpoint igual).
    assert "no configurado" not in visibilidad.summary.lower()


# ---------------------------------------------------------------------------
# Modo de lanzamiento: el gate mide el Data FÍSICO (review CodeRabbit #381)
# ---------------------------------------------------------------------------


async def test_loot_con_broker_vfs_no_aplica_el_gate(tmp_path: pathlib.Path) -> None:
    """Con broker y SIN loot_runner inyectado, LOOT corre DENTRO de la USVFS vía
    ``BrokeredLootRunner`` (``_ensure_loot_runner`` construye ese runner porque
    no hay nada inyectado que lo bypasee) y sí ve los mods virtualizados. Medir
    el ``Data`` físico ahí daría un ROJO falso que bloquea un sort correcto —
    el falso positivo que este sensor existe para evitar. El modo de
    lanzamiento es precondición del sensor.

    Deliberadamente SIN ``loot_runner=`` (a diferencia de las demás pruebas de
    este archivo): inyectarlo junto al broker es el escenario del test
    hermano de abajo, donde el runner inyectado gana y el gate debe seguir
    activo — mezclar ambos acá haría que este test no probara lo que dice
    probar (review CodeRabbit, posterior a 134d9e0)."""
    from sky_claw.local.mo2.load_order import LoadOrderPaths
    from sky_claw.local.tools.loot_service import LootSortingService

    skyrim, mo2 = _instancia(tmp_path)  # Data pelado: el escenario que daría rojo
    load_order = MagicMock()
    load_order.resolve.return_value = LoadOrderPaths(
        files=(mo2 / "profiles" / "Default" / "plugins.txt",), sources=("mo2_profile",)
    )
    svc = LootSortingService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        path_resolver=_resolver(skyrim=skyrim, mo2=mo2),
        load_order_resolver=load_order,
        vfs_broker=MagicMock(),  # ← corre dentro de la USVFS
    )

    reporte = await svc._ensure_preflight().run()

    visibilidad = next(c for c in reporte.checks if c.name == "vfs_visibility")
    assert visibilidad.status is PreflightStatus.GREEN
    # No se midió: el checkpoint debe DECIRLO, no fingir un verde verificado.
    assert "no configurado" in visibilidad.summary.lower()
    assert reporte.blocks_mutations is False


async def test_loot_runner_inyectado_bypasea_el_broker_y_mantiene_el_gate(
    tmp_path: pathlib.Path,
) -> None:
    """Hermano del test anterior: ``vfs_broker`` configurado NO prueba que LOOT
    vaya a correr bajo USVFS. ``_ensure_loot_runner`` mira primero
    ``self._loot_runner`` y, si no declara ``for_profile`` (solo
    ``BrokeredLootRunner``/``VfsRequiredLootRunner`` lo hacen), devuelve ESE
    runner tal cual — el broker nunca se toca. Con el ternario viejo
    (``visibility_check`` apagado solo por ``vfs_broker is not None``), este
    caso quedaba con el sensor apagado mientras el sort real seguía leyendo el
    ``Data`` físico: el falso verde de U-01, reabierto por un runner inyectado
    en vez de por la ausencia de broker."""
    from sky_claw.local.mo2.load_order import LoadOrderPaths
    from sky_claw.local.tools.loot_service import LootSortingService

    skyrim, mo2 = _instancia(tmp_path)  # Data pelado: el escenario que daría rojo
    load_order = MagicMock()
    load_order.resolve.return_value = LoadOrderPaths(
        files=(mo2 / "profiles" / "Default" / "plugins.txt",), sources=("mo2_profile",)
    )
    loot_runner = MagicMock()  # sin for_profile: bypasea al broker en _ensure_loot_runner
    svc = LootSortingService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        path_resolver=_resolver(skyrim=skyrim, mo2=mo2),
        loot_runner=loot_runner,
        load_order_resolver=load_order,
        vfs_broker=MagicMock(),
    )

    # Confirma la premisa: el broker configurado no se usa, gana el inyectado.
    assert svc._ensure_loot_runner("Default") is loot_runner

    reporte = await svc._ensure_preflight().run()

    visibilidad = next(c for c in reporte.checks if c.name == "vfs_visibility")
    assert visibilidad.status is PreflightStatus.RED
    assert reporte.blocks_mutations is True


class _RunnerPorPerfil:
    """Stub de un runner VFS-aware (``BrokeredLootRunner``/``VfsRequiredLootRunner``).

    Lo que los distingue de un runner corriente es el factory ``for_profile``
    declarado **en la clase** — por eso acá es un método real y no un
    ``MagicMock``, que fabricaría el atributo sin declararlo en el tipo.

    ``for_profile`` devuelve una instancia **distinta**, igual que el
    ``BrokeredLootRunner`` real (crea un runner ligado a ese perfil). Es lo que
    hace observable si el servicio realmente usó la fábrica: con un stub que
    devolviera ``self``, "se fabricó por perfil" y "se devolvió el inyectado tal
    cual" serían indistinguibles y la aserción pasaría por ambas ramas sin
    probar nada (review CodeRabbit #385).
    """

    def __init__(self) -> None:
        self.perfiles_pedidos: list[str] = []
        self.hijos: dict[str, _RunnerPorPerfil] = {}

    def for_profile(self, profile: str) -> _RunnerPorPerfil:
        self.perfiles_pedidos.append(profile)
        hijo = _RunnerPorPerfil()
        self.hijos[profile] = hijo
        return hijo

    async def sort(self, *, update_masterlist: bool = False) -> Any:
        raise AssertionError("el preflight no debe ejecutar el sort")


async def test_runner_inyectado_por_perfil_no_aplica_el_gate(tmp_path: pathlib.Path) -> None:
    """El CUARTO cuadrante de ``_routes_through_physical_data``: un runner
    inyectado que **sí** declara ``for_profile`` es VFS-aware, así que
    ``_ensure_loot_runner`` lo re-instancia por perfil y el sort corre DENTRO de
    la USVFS — el gate no debe aplicar, igual que sin runner inyectado.

    Sin este test la tabla de verdad queda con un hueco: una simplificación a
    ``self._loot_runner is not None or self._vfs_broker is None`` pasaría los
    otros tres casos y solo rompería éste, encendiendo el gate bajo un
    ``BrokeredLootRunner`` inyectado → ROJO falso que bloquea un sort correcto.
    """
    from sky_claw.local.mo2.load_order import LoadOrderPaths
    from sky_claw.local.tools.loot_service import LootSortingService

    skyrim, mo2 = _instancia(tmp_path)  # Data pelado: el escenario que daría rojo
    load_order = MagicMock()
    load_order.resolve.return_value = LoadOrderPaths(
        files=(mo2 / "profiles" / "Default" / "plugins.txt",), sources=("mo2_profile",)
    )
    runner = _RunnerPorPerfil()
    svc = LootSortingService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        path_resolver=_resolver(skyrim=skyrim, mo2=mo2),
        loot_runner=runner,
        load_order_resolver=load_order,
        vfs_broker=MagicMock(),
    )

    # Confirma la premisa: al declarar for_profile en la CLASE, el servicio usa
    # la fábrica y devuelve el runner LIGADO AL PERFIL — no el inyectado tal
    # cual, que es lo que hace el test hermano de arriba con un runner sin
    # for_profile. Se comprueba por identidad del hijo y por la llamada
    # registrada, sin volver a invocar la fábrica en la aserción (hacerlo la
    # volvería trivialmente cierta).
    elegido = svc._ensure_loot_runner("Default")
    assert runner.perfiles_pedidos == ["Default"]
    assert elegido is runner.hijos["Default"]
    assert elegido is not runner
    # Y se cachea por perfil: la segunda llamada no vuelve a fabricar.
    assert svc._ensure_loot_runner("Default") is elegido
    assert runner.perfiles_pedidos == ["Default"]

    reporte = await svc._ensure_preflight().run()

    visibilidad = next(c for c in reporte.checks if c.name == "vfs_visibility")
    assert visibilidad.status is PreflightStatus.GREEN
    assert "no configurado" in visibilidad.summary.lower()
    assert reporte.blocks_mutations is False


async def test_loot_sin_broker_sigue_aplicando_el_gate(tmp_path: pathlib.Path) -> None:
    """Contracara del anterior: el fix del broker no debe apagar el gate en el
    camino standalone, que es justo el que U-01 protege."""
    skyrim, mo2 = _instancia(tmp_path)
    svc = _construir("loot_service", resolver=_resolver(skyrim=skyrim, mo2=mo2), mo2=mo2)

    reporte = await svc._ensure_preflight().run()

    visibilidad = next(c for c in reporte.checks if c.name == "vfs_visibility")
    assert visibilidad.status is PreflightStatus.RED


# ---------------------------------------------------------------------------
# PR-586F — execution domain: el sensor mide el namespace que el backend REALMENTE
# consumirá, no "hay un broker configurado"
# ---------------------------------------------------------------------------


def _visibilidad_de(reporte: Any) -> Any | None:
    return next((c for c in reporte.checks if c.name == "vfs_visibility"), None)


@pytest.mark.parametrize(("servicio", "dominio"), _pares_de_dominio("virtual_usvfs", sensor_requerido=False))
async def test_en_dominio_virtual_el_sensor_fisico_no_aplica(
    servicio: str, dominio: str, tmp_path: pathlib.Path
) -> None:
    """D3 (familia): con un backend que consume el overlay USVFS, la medición
    física NO APlica — no se afirma un verde medido y no se bloquea.

    Data pelado a propósito: es exactamente el escenario que produjo el ROJO
    FALSO del incidente PR-586E (``HachaTencent.esl``/``EscudoOvalo.esl`` nunca
    materializados a ``G:\\...\\Data``, ``PreflightBlocked`` antes del spawn).
    La representación honesta según lo que cada ``PreflightService`` soporta:
    DynDOLOD (``omit_unconfigured=True``) OMITE el checkpoint; LOOT lo emite
    como "no configurado". Lo prohibido es un rojo por una medición que no
    significa nada para ese backend.
    """
    skyrim, mo2 = _instancia(tmp_path)  # Data PELADO: el escenario del falso rojo
    svc = _construir(servicio, resolver=_resolver(skyrim=skyrim, mo2=mo2), mo2=mo2, dominio=dominio)

    preflight = svc._ensure_preflight()
    assert preflight is not None, f"{servicio} no construyó preflight en dominio {dominio}"
    reporte = await preflight.run()

    visibilidad = _visibilidad_de(reporte)
    if visibilidad is not None:
        assert "no configurado" in visibilidad.summary.lower(), (
            f"{servicio} en dominio virtual emitió un checkpoint que finge una medición"
        )
        assert visibilidad.status is not PreflightStatus.RED
    assert reporte.blocks_mutations is False


async def test_d3_dyndolod_brokered_invisible_fisicamente_no_bloquea(tmp_path: pathlib.Path) -> None:
    """D3 (incidente real): DynDOLOD brokered con los mods del perfil INVISIBLES
    en el Data físico — el preflight NO bloquea por visibilidad física.

    Semántica honesta de DynDOLOD (``omit_unconfigured=True``): el sensor
    **no aplica**, así que el checkpoint ``vfs_visibility`` simplemente NO está
    en el reporte. No se afirma ``vfs_visibility=GREEN``: no se midió.
    """
    skyrim, mo2 = _instancia(tmp_path)  # MiMod.esp NO está en Data físico
    svc = _construir("dyndolod_service", resolver=_resolver(skyrim=skyrim, mo2=mo2), mo2=mo2, dominio="virtual_usvfs")

    reporte = await svc._ensure_preflight().run()

    assert _visibilidad_de(reporte) is None, "el sensor físico no debe existir en el dominio virtual"
    assert reporte.blocks_mutations is False


async def test_d5_standalone_explicito_mantiene_el_sensor_fisico(tmp_path: pathlib.Path) -> None:
    """D5: ``spawn_strategy=None`` no es el único camino standalone. Una strategy
    standalone EXPLÍCITA debe conservar el gate físico (U-01 sigue protegido)."""
    from sky_claw.local.tools.dyndolod_runner import StandaloneDynDOLODSpawnStrategy
    from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

    skyrim, mo2 = _instancia(tmp_path)  # Data pelado → el gate debe disparar
    svc = DynDOLODPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=_resolver(skyrim=skyrim, mo2=mo2),
        event_bus=MagicMock(),
        spawn_strategy=StandaloneDynDOLODSpawnStrategy(),
    )

    reporte = await svc._ensure_preflight().run()

    visibilidad = _visibilidad_de(reporte)
    assert visibilidad is not None, "la strategy standalone explícita debe mantener el sensor cableado"
    assert visibilidad.status is PreflightStatus.RED
    assert reporte.blocks_mutations is True


async def test_d6_wrapper_que_delega_el_dominio_se_reconoce_como_virtual(tmp_path: pathlib.Path) -> None:
    """D6: un wrapper/decorator (el ``RecordingStrategy`` del rig) que DELEGA la
    capability vía contrato normal es reconocido como dominio virtual — el
    servicio jamás desnuda ``_inner``/``delegate`` ni pregunta ``isinstance``."""
    from sky_claw.local.mo2.brokered_dyndolod import BrokeredDynDOLODSpawnStrategy

    inner = _EstrategiaVirtualDePrueba()
    wrapper = _WrapperDeStrategy(inner)
    # Prueba estructural de que la decisión es por CONTRATO: el wrapper no es la
    # clase brokered concreta.
    assert not isinstance(wrapper, BrokeredDynDOLODSpawnStrategy)
    assert wrapper.data_visibility_domain == "virtual_usvfs"

    skyrim, mo2 = _instancia(tmp_path)  # Data pelado: sin el fix, rojo falso
    resolver = _resolver(skyrim=skyrim, mo2=mo2)
    from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

    svc = DynDOLODPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=resolver,
        event_bus=MagicMock(),
        spawn_strategy=wrapper,  # type: ignore[arg-type]
    )

    reporte = await svc._ensure_preflight().run()

    assert _visibilidad_de(reporte) is None, "el dominio virtual debe reconocerse a través del wrapper"
    assert reporte.blocks_mutations is False


def test_d7_strategy_sin_capability_es_fail_closed() -> None:
    """D7: una strategy inyectada que NO declara ``data_visibility_domain`` es
    INDETERMINADA → configuration error fail-closed. Nunca se asume brokered ni
    se apaga U-01: una strategy incompleta no puede silenciar el gate."""
    from sky_claw.local.tools.dyndolod_runner import DataVisibilityDomainError
    from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

    with pytest.raises(DataVisibilityDomainError, match="data_visibility_domain"):
        DynDOLODPipelineService(
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
            journal=MagicMock(),
            path_resolver=MagicMock(),
            event_bus=MagicMock(),
            spawn_strategy=_EstrategiaIncompleta(),  # type: ignore[arg-type]
        )


def test_d7_un_mock_no_fabrica_un_dominio_virtual() -> None:
    """Adversarial: ``MagicMock`` fabrica CUALQUIER atributo, incluida una
    capability aparente. Un valor fabricado no es un dominio conocido →
    fail-closed, jamás un ``falso virtual`` que apague U-01 en silencio."""
    from sky_claw.local.tools.dyndolod_runner import DataVisibilityDomainError
    from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

    with pytest.raises(DataVisibilityDomainError):
        DynDOLODPipelineService(
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
            journal=MagicMock(),
            path_resolver=MagicMock(),
            event_bus=MagicMock(),
            spawn_strategy=MagicMock(),
        )


async def test_preflight_cacheado_conserva_la_policy_de_su_dominio(tmp_path: pathlib.Path) -> None:
    """PREFLIGHT CACHE: el ``_ensure_preflight`` cacheado no puede quedar armado
    para un dominio y usarse con otro. La policy se fija con la configuración
    (el dominio se resuelve en el constructor) y el cache la conserva estable
    entre runs. La premisa —``_spawn_strategy``/``_data_visibility_domain`` son
    inmutables después del ``__init__``— la ancla el test estructural de abajo.
    """
    skyrim, mo2 = _instancia(tmp_path)
    svc = _construir("dyndolod_service", resolver=_resolver(skyrim=skyrim, mo2=mo2), mo2=mo2, dominio="virtual_usvfs")

    primero = svc._ensure_preflight()
    assert primero is not None
    assert svc._ensure_preflight() is primero, "el preflight es una instancia cacheada"

    primer_reporte = await primero.run()
    segundo_reporte = await primero.run()
    assert _visibilidad_de(primer_reporte) is None
    assert _visibilidad_de(segundo_reporte) is None, "la policy cacheada no puede virar entre runs"

    # Contracara en el dominio físico: el cache conserva el sensor ACTIVO.
    svc_fisico = _construir("dyndolod_service", resolver=_resolver(skyrim=skyrim, mo2=mo2), mo2=mo2)
    cacheado = svc_fisico._ensure_preflight()
    assert cacheado is not None and svc_fisico._ensure_preflight() is cacheado
    assert _visibilidad_de(await cacheado.run()) is not None
    assert _visibilidad_de(await cacheado.run()) is not None


def test_el_dominio_y_la_estrategia_se_fijan_en_el_constructor() -> None:
    """Ancla estructural de la premisa del cache: en ``dyndolod_service.py``,
    ``self._spawn_strategy`` y ``self._data_visibility_domain`` se asignan SOLO
    dentro de ``__init__``. Si un refactor pudiera mutarlos después, el
    preflight cacheado congelaría la policy del dominio equivocado — este guard
    obliga a re-evaluar ese diseño en vez de descubrirlo como un falso rojo/verde.
    """
    fuente = (_TOOLS_DIR / "dyndolod_service.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    sitios: list[str] = []
    for fn in ast.walk(arbol):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for sub in ast.walk(fn):
            objetivos: list[ast.expr] = []
            if isinstance(sub, ast.Assign):
                objetivos = list(sub.targets)
            elif isinstance(sub, ast.AnnAssign | ast.AugAssign):
                objetivos = [sub.target]
            for objetivo in objetivos:
                if (
                    isinstance(objetivo, ast.Attribute)
                    and isinstance(objetivo.value, ast.Name)
                    and objetivo.value.id == "self"
                    and objetivo.attr in {"_spawn_strategy", "_data_visibility_domain"}
                ):
                    sitios.append(f"{fn.name}:{objetivo.attr}")

    assert sitios, "el servicio debe fijar _spawn_strategy/_data_visibility_domain en algún sitio"
    assert all(nombre.startswith("__init__") for nombre in sitios), (
        f"estos atributos son la premisa del cache del preflight y sólo deben fijarse en el constructor: {sitios}"
    )


def test_el_servicio_no_pregunta_el_backend_por_isinstance() -> None:
    """Ancla estructural: la capability explícita es la ÚNICA forma de decidir el
    dominio. El servicio jamás discrimina por la clase concreta del backend
    (hermana del ancla del runner en ``test_texgen_handoff_gate``)."""
    fuente = (_TOOLS_DIR / "dyndolod_service.py").read_text(encoding="utf-8")
    assert "isinstance(self._spawn_strategy" not in fuente
    assert "BrokeredDynDOLOD" not in fuente, "el servicio no debe nombrar la clase del backend brokered"
    assert "StandaloneDynDOLOD" not in fuente, "el servicio no debe nombrar la clase del backend standalone"
