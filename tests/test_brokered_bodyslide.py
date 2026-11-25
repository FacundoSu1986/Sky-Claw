"""PR 2c-2b: BodySlide bajo MO2/USVFS, DORMIDO.

Contrato aprobado (brief §13.6):

- ``tool_id="bodyslide"`` con payload CERRADO; el worker reconstruye el argv con
  ``construir_argv`` (un argv armado nunca cruza el IPC) y exige el destino canónico;
- el worker re-hashea el exe por un handle retenido mientras corre (D7, mitad del worker);
- *solo el fence del broker convierte ``LANZADO`` en ``TERMINAL``*: una excepción que veta el
  rollback es :class:`TerminalidadDesconocidaError` y el runner no restaura;
- dormido: barrera en ``False`` y ningún call site productivo del resolvedor.
"""

from __future__ import annotations

import ast
import hashlib
import pathlib
from typing import Any
from unittest.mock import patch

import pytest

from sky_claw.app.db.rollback_veto import mark_unknown_terminality
from sky_claw.local.mo2.brokered_bodyslide import (
    BrokeredBodySlideLanzador,
    VfsRequiredBodySlideLanzador,
    resolve_bodyslide_lanzador,
)
from sky_claw.local.mo2.vfs_attestation import build_attestation_challenge
from sky_claw.local.mo2.vfs_broker import (
    VfsBridgeLaunchError,
    VfsBridgeLostTeardownError,
    VfsInstanceQuarantinedError,
    VfsJobTimeoutError,
    VfsTeardownError,
)
from sky_claw.local.mo2.vfs_contracts import (
    ALLOWED_VFS_TOOL_IDS,
    BODYSLIDE_EXECUTABLE_NAMES,
    VFS_PROTOCOL_VERSION,
    VfsJob,
    VfsJobResult,
)
from sky_claw.local.mo2.vfs_manifest import VfsWorkerManifest
from sky_claw.local.mo2.vfs_worker import BODYSLIDE_PAYLOAD_KEYS, _bodyslide_handler, _default_handlers
from sky_claw.local.tools import bodyslide_service
from sky_claw.local.tools.bodyslide_headless import construir_argv
from sky_claw.local.tools.bodyslide_lanzador import (
    PedidoDeLanzamiento,
    SalidaDelLanzamiento,
    TerminalidadDesconocidaError,
)
from sky_claw.local.tools.output_targets import bodyslide_output_root, bodyslide_output_target

RAIZ = pathlib.Path(__file__).resolve().parents[1]
EXE_BYTES = b"MZ BodySlide de prueba"


def _sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# Escenario MO2 mínimo (canario visible solo en el Data virtual)
# ---------------------------------------------------------------------------


def _escenario(tmp_path: pathlib.Path) -> dict[str, pathlib.Path]:
    mo2 = tmp_path / "MO2"
    perfil = mo2 / "profiles" / "Default"
    mod = mo2 / "mods" / "CanaryMod"
    juego = tmp_path / "Skyrim"
    data_fisico = juego / "Data"
    for carpeta in (perfil, mod, data_fisico):
        carpeta.mkdir(parents=True)
    (perfil / "modlist.txt").write_text("+CanaryMod\n", encoding="utf-8-sig")
    (perfil / "plugins.txt").write_text("", encoding="utf-8")
    (mod / "canary.txt").write_bytes(b"canary-bodyslide")
    exe = data_fisico / "CalienteTools" / "BodySlide" / "BodySlide x64.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(EXE_BYTES)
    return {"mo2": mo2, "juego": juego, "data": data_fisico, "exe": exe}


def _payload(rutas: dict[str, pathlib.Path], **overrides: Any) -> dict[str, Any]:
    target = bodyslide_output_target(game=rutas["juego"], group="CBBE")
    assert target is not None
    payload: dict[str, Any] = {
        "bodyslide_exe": str(rutas["exe"]),
        "exe_sha256": _sha(EXE_BYTES),
        "group": "CBBE",
        "target": str(target),
        "preset": "Zeroed Sliders",
        "preset_verified": True,
        "build_morphs": True,
        "quiet_verified": True,
        "expected_plugins": [],
    }
    payload.update(overrides)
    return payload


def _manifiesto(
    rutas: dict[str, pathlib.Path],
    payload: dict[str, Any],
    *,
    mutation_targets: tuple[pathlib.Path, ...] | None = None,
) -> VfsWorkerManifest:
    challenge = build_attestation_challenge(mo2_root=rutas["mo2"], profile="Default", physical_data_dir=rutas["data"])
    targets = mutation_targets if mutation_targets is not None else (pathlib.Path(payload["target"]),)
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="bodyslide",
        payload=payload,
        timeout_seconds=60.0,
        expected_fingerprint=challenge.profile_fingerprint,
        mutation_targets=targets,
    )
    return VfsWorkerManifest(
        protocol_version=VFS_PROTOCOL_VERSION,
        job=job,
        challenge=challenge,
        mo2_root=rutas["mo2"],
        virtual_data_dir=rutas["data"],
        descriptor_path=rutas["mo2"] / "descriptor.json",
    )


class _RunCaptureFalso:
    """Doble de ``run_capture``: registra el argv y devuelve una salida."""

    def __init__(self, resultado: tuple[bytes, bytes, int] | BaseException = (b"ok", b"", 0)) -> None:
        self.resultado = resultado
        self.llamadas: list[tuple[list[str], dict[str, Any]]] = []

    async def __call__(self, argv: list[str], **kwargs: Any) -> tuple[bytes, bytes, int]:
        self.llamadas.append((argv, kwargs))
        if isinstance(self.resultado, BaseException):
            raise self.resultado
        return self.resultado


# ---------------------------------------------------------------------------
# Contratos
# ---------------------------------------------------------------------------


def test_bodyslide_esta_en_el_protocolo_y_con_handler() -> None:
    assert "bodyslide" in ALLOWED_VFS_TOOL_IDS
    assert _default_handlers()["bodyslide"] is _bodyslide_handler
    assert frozenset({"bodyslide x64.exe", "bodyslide.exe"}) == BODYSLIDE_EXECUTABLE_NAMES
    assert not any("outfitstudio" in nombre for nombre in BODYSLIDE_EXECUTABLE_NAMES)


def test_el_payload_cerrado_esta_congelado() -> None:
    assert (
        frozenset(
            {
                "bodyslide_exe",
                "exe_sha256",
                "group",
                "target",
                "preset",
                "preset_verified",
                "build_morphs",
                "quiet_verified",
                "expected_plugins",
            }
        )
        == BODYSLIDE_PAYLOAD_KEYS
    )
    assert "argv" not in BODYSLIDE_PAYLOAD_KEYS, "un argv armado nunca cruza el IPC"


# ---------------------------------------------------------------------------
# Worker: validación del payload
# ---------------------------------------------------------------------------


async def test_el_worker_reconstruye_el_argv_del_contrato_y_lanza(tmp_path: pathlib.Path) -> None:
    # Arrange
    rutas = _escenario(tmp_path)
    payload = _payload(rutas)
    falso = _RunCaptureFalso((b"stdout", b"", 0))

    # Act
    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", falso):
        ejecucion = await _bodyslide_handler(_manifiesto(rutas, payload))

    # Assert
    assert ejecucion.success is True
    assert ejecucion.exit_code == 0
    assert ejecucion.stdout == "stdout"
    assert ejecucion.tool_result["exe_sha256"] == _sha(EXE_BYTES)
    ((argv, kwargs),) = falso.llamadas
    raiz = bodyslide_output_root(game=rutas["juego"].resolve())
    assert argv == construir_argv(
        str(rutas["exe"]),
        "CBBE",
        payload["target"],
        preset="Zeroed Sliders",
        preset_verified=True,
        build_morphs=True,
        quiet_verified=True,
        stage_root=str(raiz),
    )
    assert kwargs["require_job"] is True
    assert kwargs["cwd"] == str(rutas["juego"].resolve())


@pytest.mark.parametrize(
    "cambio",
    [
        pytest.param({"argv": ["BodySlide x64.exe", "-gbuild", "x"]}, id="campo-extra-argv"),
        pytest.param({"extra": 1}, id="campo-extra"),
    ],
)
async def test_un_campo_de_mas_se_rechaza(tmp_path: pathlib.Path, cambio: dict[str, Any]) -> None:
    rutas = _escenario(tmp_path)
    falso = _RunCaptureFalso()

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", falso), pytest.raises(ValueError, match="contrato"):
        await _bodyslide_handler(_manifiesto(rutas, _payload(rutas, **cambio)))

    assert falso.llamadas == []


@pytest.mark.parametrize("faltante", sorted(BODYSLIDE_PAYLOAD_KEYS))
async def test_cada_campo_faltante_se_rechaza(tmp_path: pathlib.Path, faltante: str) -> None:
    rutas = _escenario(tmp_path)
    payload = _payload(rutas)
    destino = (pathlib.Path(payload["target"]),)
    del payload[faltante]
    falso = _RunCaptureFalso()

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", falso), pytest.raises(ValueError, match="contrato"):
        await _bodyslide_handler(_manifiesto(rutas, payload, mutation_targets=destino))

    assert falso.llamadas == []


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("preset_verified", "true"),
        ("build_morphs", 1),
        ("quiet_verified", None),
        ("preset", ""),
        ("preset", 3),
        # Review CodeRabbit #1025: un null saltearía el gate de visibilidad (lo trata como lista vacía).
        ("expected_plugins", None),
        ("expected_plugins", "Skyrim.esm"),
        ("expected_plugins", ["Skyrim.esm", ""]),
        ("expected_plugins", ["Skyrim.esm", 3]),
    ],
)
async def test_tipos_invalidos_se_rechazan(tmp_path: pathlib.Path, campo: str, valor: Any) -> None:
    rutas = _escenario(tmp_path)
    falso = _RunCaptureFalso()

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", falso), pytest.raises(ValueError):
        await _bodyslide_handler(_manifiesto(rutas, _payload(rutas, **{campo: valor})))

    assert falso.llamadas == []


async def test_otro_ejecutable_se_rechaza(tmp_path: pathlib.Path) -> None:
    rutas = _escenario(tmp_path)
    outfit = rutas["exe"].with_name("OutfitStudio x64.exe")
    outfit.write_bytes(EXE_BYTES)
    falso = _RunCaptureFalso()

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", falso), pytest.raises(ValueError, match="incompat"):
        await _bodyslide_handler(_manifiesto(rutas, _payload(rutas, bodyslide_exe=str(outfit))))

    assert falso.llamadas == []


async def test_un_target_distinto_del_mutation_target_se_rechaza(tmp_path: pathlib.Path) -> None:
    rutas = _escenario(tmp_path)
    payload = _payload(rutas)
    otro = rutas["juego"] / "BodySlide_Output" / "3BA"
    falso = _RunCaptureFalso()

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", falso), pytest.raises(ValueError, match="mutation"):
        await _bodyslide_handler(_manifiesto(rutas, payload, mutation_targets=(otro,)))

    assert falso.llamadas == []


async def test_un_target_que_no_es_el_canonico_del_grupo_no_lanza(tmp_path: pathlib.Path) -> None:
    """El target declarado coincide con mutation_targets, pero es la carpeta de OTRO grupo."""
    rutas = _escenario(tmp_path)
    otro = bodyslide_output_target(game=rutas["juego"], group="3BA")
    assert otro is not None
    payload = _payload(rutas, target=str(otro))
    falso = _RunCaptureFalso()

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", falso):
        ejecucion = await _bodyslide_handler(_manifiesto(rutas, payload))

    assert ejecucion.success is False
    assert ejecucion.tool_result == {"bodyslide_failure_kind": "args_invalid"}
    assert falso.llamadas == []


async def test_sin_quiet_verificado_el_contrato_no_arma_argv(tmp_path: pathlib.Path) -> None:
    rutas = _escenario(tmp_path)
    falso = _RunCaptureFalso()

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", falso):
        ejecucion = await _bodyslide_handler(_manifiesto(rutas, _payload(rutas, quiet_verified=False)))

    assert ejecucion.tool_result == {"bodyslide_failure_kind": "args_invalid"}
    assert falso.llamadas == []


async def test_un_exe_con_otro_hash_no_se_ejecuta(tmp_path: pathlib.Path) -> None:
    """Reemplazado entre el hash del daemon y el worker: no se ejecuta otro binario con el hash del primero."""
    rutas = _escenario(tmp_path)
    payload = _payload(rutas, exe_sha256=_sha(b"el binario que hasheo el daemon"))
    falso = _RunCaptureFalso()

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", falso):
        ejecucion = await _bodyslide_handler(_manifiesto(rutas, payload))

    assert ejecucion.success is False
    assert ejecucion.tool_result == {"bodyslide_failure_kind": "exe_mismatch"}
    assert ejecucion.outputs == ()
    assert falso.llamadas == []


async def test_el_exe_queda_retenido_mientras_corre(tmp_path: pathlib.Path) -> None:
    rutas = _escenario(tmp_path)
    eventos: list[str] = []

    class _Espia(_RunCaptureFalso):
        async def __call__(self, argv: list[str], **kwargs: Any) -> tuple[bytes, bytes, int]:
            eventos.append("spawn")
            return await super().__call__(argv, **kwargs)

    with (
        patch("sky_claw.local.tools.bodyslide_runner.run_capture", _Espia()),
        patch(
            "sky_claw.local.tools.exe_retenido.retener_exe_verificado",
            _contexto_que_registra(eventos),
        ),
    ):
        await _bodyslide_handler(_manifiesto(rutas, _payload(rutas)))

    # Un solo registro ordenado (review CodeRabbit #1025): el spawn ocurre DENTRO de la retención.
    assert eventos == ["entra", "spawn", "sale"]


def _contexto_que_registra(entradas: list[str]):  # type: ignore[no-untyped-def]
    import contextlib

    @contextlib.contextmanager
    def _ctx(_ruta: pathlib.Path, sha: str):  # type: ignore[no-untyped-def]
        entradas.append("entra")
        try:
            yield sha
        finally:
            entradas.append("sale")

    return _ctx


@pytest.mark.parametrize(
    ("error", "tipo", "outputs_declarados"),
    [
        (TimeoutError(), "timeout", True),
        (OSError("no se pudo crear el proceso"), "execution_error", True),
    ],
)
async def test_los_fallos_del_spawn_son_tipados(
    tmp_path: pathlib.Path, error: BaseException, tipo: str, outputs_declarados: bool
) -> None:
    rutas = _escenario(tmp_path)
    payload = _payload(rutas)

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", _RunCaptureFalso(error)):
        ejecucion = await _bodyslide_handler(_manifiesto(rutas, payload))

    assert ejecucion.success is False
    assert ejecucion.tool_result == {"bodyslide_failure_kind": tipo}
    assert bool(ejecucion.outputs) is outputs_declarados


# ---------------------------------------------------------------------------
# Lanzador brokered: tabla de terminalidad del lado del broker
# ---------------------------------------------------------------------------


class _BrokerFalso:
    def __init__(self, efecto: VfsJobResult | BaseException) -> None:
        self.efecto = efecto
        self.jobs: list[VfsJob] = []

    async def submit(self, job: VfsJob, **_kwargs: Any) -> VfsJobResult:
        self.jobs.append(job)
        if isinstance(self.efecto, BaseException):
            raise self.efecto
        return self.efecto


def _resultado(**overrides: Any) -> VfsJobResult:
    datos: dict[str, Any] = {
        "protocol_version": VFS_PROTOCOL_VERSION,
        "job_id": "job-1",
        "success": True,
        "message": "",
        "exit_code": 0,
        "stdout": "salida",
        "stderr": "",
        "outputs": (),
        "rollback_state": "not_required",
        "attestation": None,
        "tool_result": {"return_code": 0},
    }
    datos.update(overrides)
    return VfsJobResult(**datos)


def _pedido(rutas: dict[str, pathlib.Path]) -> PedidoDeLanzamiento:
    target = bodyslide_output_target(game=rutas["juego"], group="CBBE")
    assert target is not None
    return PedidoDeLanzamiento(
        argv=("x",),
        exe=rutas["exe"],
        cwd=rutas["juego"],
        timeout=60.0,
        group="CBBE",
        target=str(target),
        preset=None,
        preset_verified=False,
        build_morphs=True,
        quiet_verified=True,
    )


def _lanzador(rutas: dict[str, pathlib.Path], broker: _BrokerFalso) -> BrokeredBodySlideLanzador:
    return BrokeredBodySlideLanzador(
        broker=broker,
        instance_id="portable-main",
        profile="Default",
        data_root=rutas["mo2"],
        mods_dir=rutas["mo2"] / "mods",
        install_root=rutas["mo2"],
        virtual_data_dir=rutas["data"],
    )


def _con_veto(exc: BaseException) -> BaseException:
    mark_unknown_terminality(exc)
    return exc


#: Desenlace del broker → qué ve el runner. Congelado por igualdad literal.
TABLA_DEL_BROKER: dict[str, str] = {
    "resultado": "SalidaDelLanzamiento",
    "timeout_con_terminalidad": "TimeoutError",
    "timeout_sin_terminalidad": "TerminalidadDesconocidaError",
    "teardown_fallido": "TerminalidadDesconocidaError",
    "bridge_perdido": "TerminalidadDesconocidaError",
    "instancia_en_cuarentena": "VfsInstanceQuarantinedError",
    "bridge_no_lanzo": "VfsBridgeLaunchError",
    "fallo_tipado_timeout": "TimeoutError",
    "fallo_tipado_exe": "RuntimeError",
    "resultado_sin_codigo": "RuntimeError",
}


def _efecto(desenlace: str) -> VfsJobResult | BaseException:
    return {
        "resultado": _resultado(),
        "timeout_con_terminalidad": VfsJobTimeoutError("plazo vencido; worker_exit confirmado"),
        "timeout_sin_terminalidad": _con_veto(VfsJobTimeoutError("plazo vencido; sin worker_exit")),
        "teardown_fallido": VfsTeardownError("el Job no quedó vacío"),
        "bridge_perdido": VfsBridgeLostTeardownError("el bridge no volvió"),
        "instancia_en_cuarentena": VfsInstanceQuarantinedError("instancia aislada"),
        "bridge_no_lanzo": VfsBridgeLaunchError("startApplication falló"),
        "fallo_tipado_timeout": _resultado(
            success=False, exit_code=None, tool_result={"bodyslide_failure_kind": "timeout"}
        ),
        "fallo_tipado_exe": _resultado(
            success=False, exit_code=None, tool_result={"bodyslide_failure_kind": "exe_mismatch"}
        ),
        "resultado_sin_codigo": _resultado(exit_code=None),
    }[desenlace]


@pytest.mark.parametrize("desenlace", sorted(TABLA_DEL_BROKER))
async def test_la_tabla_del_broker(tmp_path: pathlib.Path, desenlace: str) -> None:
    rutas = _escenario(tmp_path)
    broker = _BrokerFalso(_efecto(desenlace))

    try:
        salida = await _lanzador(rutas, broker).lanzar(_pedido(rutas))
        observado = type(salida).__name__
    except Exception as exc:  # noqa: BLE001 -- la tabla registra el tipo
        observado = type(exc).__name__

    assert observado == TABLA_DEL_BROKER[desenlace]


async def test_el_job_lleva_el_hash_del_exe_y_el_target_como_unico_destino(tmp_path: pathlib.Path) -> None:
    rutas = _escenario(tmp_path)
    broker = _BrokerFalso(_resultado())
    pedido = _pedido(rutas)

    salida = await _lanzador(rutas, broker).lanzar(pedido)

    assert salida == SalidaDelLanzamiento(stdout=b"salida", stderr=b"", return_code=0)
    (job,) = broker.jobs
    assert job.tool_id == "bodyslide"
    assert set(job.payload) == BODYSLIDE_PAYLOAD_KEYS
    assert job.payload["exe_sha256"] == _sha(EXE_BYTES)
    assert job.mutation_targets == (pathlib.Path(pedido.target),)


async def test_un_exe_ajeno_no_llega_al_broker(tmp_path: pathlib.Path) -> None:
    rutas = _escenario(tmp_path)
    broker = _BrokerFalso(_resultado())
    pedido = _pedido(rutas)
    outfit = rutas["exe"].with_name("OutfitStudio x64.exe")
    outfit.write_bytes(EXE_BYTES)

    with pytest.raises(RuntimeError, match="incompatible"):
        await _lanzador(rutas, broker).lanzar(
            PedidoDeLanzamiento(**{**_campos(pedido), "exe": outfit})  # type: ignore[arg-type]
        )

    assert broker.jobs == []


def _campos(pedido: PedidoDeLanzamiento) -> dict[str, object]:
    return {nombre: getattr(pedido, nombre) for nombre in pedido.__dataclass_fields__}


def test_la_terminalidad_desconocida_del_broker_no_es_un_timeout() -> None:
    assert not issubclass(TerminalidadDesconocidaError, TimeoutError)


# ---------------------------------------------------------------------------
# Resolvedor y barrera
# ---------------------------------------------------------------------------


def _resolver(backend: object, **overrides: Any) -> object:
    argumentos: dict[str, Any] = {
        "backend": backend,
        "broker": object(),
        "instance_id": "portable-main",
        "profile": "Default",
        "data_root": pathlib.Path("/mo2"),
        "mods_dir": pathlib.Path("/mo2/mods"),
        "install_root": pathlib.Path("/mo2"),
        "virtual_data_dir": pathlib.Path("/juego/Data"),
    }
    argumentos.update(overrides)
    return resolve_bodyslide_lanzador(**argumentos)


def test_standalone_no_devuelve_lanzador() -> None:
    assert _resolver("standalone") is None
    assert _resolver(" Standalone ") is None


@pytest.mark.parametrize("backend", ["brokered", "BROKERED", "otro", None, 3])
def test_con_la_barrera_baja_todo_pedido_no_standalone_falla_cerrado(backend: object) -> None:
    assert bodyslide_service._CONTENCION_BODYSLIDE_VERIFICADA is False
    assert isinstance(_resolver(backend), VfsRequiredBodySlideLanzador)


@pytest.mark.parametrize(
    "faltante",
    ["broker", "instance_id", "profile", "data_root", "mods_dir", "install_root", "virtual_data_dir"],
)
def test_con_la_barrera_alta_cada_requisito_faltante_falla_cerrado(
    monkeypatch: pytest.MonkeyPatch, faltante: str
) -> None:
    monkeypatch.setattr(bodyslide_service, "_CONTENCION_BODYSLIDE_VERIFICADA", True)
    assert isinstance(_resolver("brokered", **{faltante: None}), VfsRequiredBodySlideLanzador)


def test_con_la_barrera_alta_y_todo_presente_construye_el_lanzador(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bodyslide_service, "_CONTENCION_BODYSLIDE_VERIFICADA", True)
    assert isinstance(_resolver("brokered"), BrokeredBodySlideLanzador)


async def test_el_guard_no_lanza_y_su_error_prueba_terminalidad(tmp_path: pathlib.Path) -> None:
    """No se lanzó nada: error común (no TerminalidadDesconocidaError), así el runner restaura."""
    rutas = _escenario(tmp_path)
    guard = _resolver("brokered")
    assert isinstance(guard, VfsRequiredBodySlideLanzador)

    with pytest.raises(RuntimeError) as info:
        await guard.lanzar(_pedido(rutas))

    assert not isinstance(info.value, TerminalidadDesconocidaError)


def _llamadores(nombre: str) -> set[str]:
    encontrados: set[str] = set()
    for archivo in sorted((RAIZ / "sky_claw").rglob("*.py")):
        relativo = archivo.relative_to(RAIZ).as_posix()
        for nodo in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
            if isinstance(nodo, ast.Call):
                func = nodo.func
                llamado = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if llamado == nombre:
                    encontrados.add(relativo)
    return encontrados


def test_el_backend_brokered_no_tiene_call_site_productivo() -> None:
    """Dormido: nadie fuera de su módulo resuelve ni construye el lanzador brokered (C5)."""
    propio = {"sky_claw/local/mo2/brokered_bodyslide.py"}
    assert _llamadores("resolve_bodyslide_lanzador") == set()
    assert _llamadores("BrokeredBodySlideLanzador") == propio
    assert _llamadores("VfsRequiredBodySlideLanzador") == propio


async def test_el_job_lleva_los_plugins_habilitados_del_perfil(tmp_path: pathlib.Path) -> None:
    """C2: la misma lectura canónica que LOOT y DynDOLOD (``read_enabled_plugins``)."""
    rutas = _escenario(tmp_path)
    (rutas["mo2"] / "profiles" / "Default" / "plugins.txt").write_text("*MiMod.esp\nApagado.esp\n", encoding="utf-8")
    broker = _BrokerFalso(_resultado())

    await _lanzador(rutas, broker).lanzar(_pedido(rutas))

    (job,) = broker.jobs
    plugins = job.payload["expected_plugins"]
    assert isinstance(plugins, list)
    # Identidad canónica en minúscula, igual que el fingerprint del perfil.
    assert "mimod.esp" in plugins
    assert "apagado.esp" not in plugins
    assert "skyrim.esm" in plugins


async def test_sin_plugins_txt_legible_el_job_no_se_arma(tmp_path: pathlib.Path) -> None:
    """Sin la lista completa no hay prueba de visibilidad; error común: no se lanzó nada y el runner restaura."""
    rutas = _escenario(tmp_path)
    plugins_txt = rutas["mo2"] / "profiles" / "Default" / "plugins.txt"
    plugins_txt.unlink()
    plugins_txt.mkdir()  # ilegible como archivo de perfil
    broker = _BrokerFalso(_resultado())

    with pytest.raises(RuntimeError, match="plugins") as info:
        await _lanzador(rutas, broker).lanzar(_pedido(rutas))

    assert not isinstance(info.value, TerminalidadDesconocidaError)
    assert broker.jobs == []
