"""PR-2: la compatibilidad de SKSE se decide por el RUNTIME exacto, no por la edición.

Continuación directa de #603 (``sky_claw/local/tools/skse_catalog.py``): `ensure_skse`
resuelve `detected_version → SkseRelease` y deja de seleccionar payload por
`edition → SKSE_CONFIG["AE"|"SE"|"LE"]`. Este archivo ancla las cuatro propiedades que
el PR existe para establecer:

* la IDEMPOTENCIA reconoce una instalación compatible por la misma detección del
  scanner (loader + DLL del runtime real), no por el único DLL del payload legacy;
* un runtime DESCONOCIDO corta cerrado (cero HITL, cero egress, cero escrituras);
* un release de NEXUS (1.6.1170, 1.7.99, 1.7.104) reconoce su build y corta ANTES de
  cualquier frontera: la adquisición Nexus es de PR-3;
* la adquisición directa (silverlock) se elige por IDENTIDAD del release, nunca por
  edición, y sólo si hay exactamente una coincidencia.

Los tests de comportamiento existentes que protegen #490/#491 (presencia sin
verificar, TOCTOU, dirty upgrades, integridad) siguen en
``tests/test_tools_installer.py::TestEnsureSkse``.
"""

from __future__ import annotations

import pathlib
import tempfile
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from sky_claw.app.security.hitl import Decision, HITLGuard
from sky_claw.app.security.network_gateway import EgressPolicy, NetworkGateway
from sky_claw.app.security.path_validator import PathValidator
from sky_claw.local import tools_installer
from sky_claw.local.discovery.environment import SkyrimEdition
from sky_claw.local.tools.skse_catalog import SKSE_RELEASES, SkseRelease, SkseSource
from sky_claw.local.tools_installer import (
    InstallVerification,
    ToolInstallError,
    ToolsInstaller,
    _adquisicion_para,
)

#: Releases del catálogo por fuente, para parametrizar por PROPIEDAD y no por caso:
#: una fila nueva del catálogo entra sola en estos tests (el `ids` la nombra).
_RELEASES_SILVERLOCK = [r for r in SKSE_RELEASES if r.source is SkseSource.SILVERLOCK]
_RELEASES_NEXUS = [r for r in SKSE_RELEASES if r.source is SkseSource.NEXUS]


def _id(release: SkseRelease) -> str:
    return f"{release.game_version}-skse{release.skse_version}"


def _loader_de(release: SkseRelease) -> str:
    """Loader que le corresponde por FAMILIA de DLL (skse64_* vs skse_*)."""
    return "skse64_loader.exe" if release.dll_name.startswith("skse64_") else "skse_loader.exe"


def _edicion_de(release: SkseRelease) -> SkyrimEdition:
    """Edición del runtime del release, para el stub de detección del PE."""
    if not release.dll_name.startswith("skse64_"):
        return SkyrimEdition.LE
    return SkyrimEdition.SE if release.game_version == "1.5.97" else SkyrimEdition.AE


@pytest.fixture
def installer(tmp_path: pathlib.Path) -> ToolsInstaller:
    """Espejo del fixture de ``test_tools_installer.py`` (lock mockeado, gateway real)."""
    lock_manager = MagicMock()
    lock_manager.acquire_lock = AsyncMock(return_value=MagicMock(resource_id="tools-install:test"))
    lock_manager.release_lock = AsyncMock(return_value=True)
    lock_manager.renew_lock = AsyncMock(return_value=True)
    return ToolsInstaller(
        hitl=HITLGuard(notify_fn=None, timeout=5),
        gateway=NetworkGateway(EgressPolicy(block_private_ips=False)),
        path_validator=PathValidator(roots=[tmp_path, pathlib.Path(tempfile.gettempdir()) / "sky_claw"]),
        lock_manager=lock_manager,
        install_ttl=60.0,
    )


def _skyrim_limpio(tmp_path: pathlib.Path) -> pathlib.Path:
    """Raíz de juego con ejecutable y sin SKSE."""
    install_dir = tmp_path / "skyrim"
    install_dir.mkdir()
    (install_dir / "SkyrimSE.exe").write_bytes(b"MZ")
    return install_dir


def _espias_de_frontera(installer: ToolsInstaller) -> tuple[AsyncMock, AsyncMock]:
    """HITL que aprueba y gateway que explota: si el flujo cruza, el test lo ve."""
    hitl = AsyncMock(return_value=Decision.APPROVED)
    egress = AsyncMock(side_effect=AssertionError("no puede haber egress sin adquisición habilitada"))
    installer._hitl.request_approval = hitl  # type: ignore[method-assign]
    installer._gateway = MagicMock()
    installer._gateway.request = egress
    return hitl, egress


class TestCompatibilidadPorRuntime:
    """El runtime exacto manda: idempotencia, unknown fail-closed y frontera Nexus."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("release", SKSE_RELEASES, ids=_id)
    async def test_instalacion_compatible_se_reconoce_por_runtime(
        self,
        installer: ToolsInstaller,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        release: SkseRelease,
    ) -> None:
        """Caso B/C/D: loader + DLL del runtime real → VERIFIED sin HITL, red ni escrituras.

        Se enumeran TODAS las filas del catálogo: la reconocida es la instalación que
        el scanner ya considera utilizable (`find_skse_installation`), sin importar si
        su release tiene adquisición directa (Nexus incluido) — el build concreto no
        cambia el desenlace porque el nombre del DLL codifica el runtime.
        """
        install_dir = _skyrim_limpio(tmp_path)
        (install_dir / _loader_de(release)).write_bytes(b"MZ")
        (install_dir / release.dll_name).write_bytes(b"MZ")
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: _edicion_de(release))
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: release.game_version)

        hitl, egress = _espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        res = await installer.ensure_skse(install_dir, session)

        assert res.already_existed is True
        assert res.tool_name == "SKSE"
        assert res.exe_path == install_dir / _loader_de(release)
        assert res.verification is InstallVerification.VERIFIED
        hitl.assert_not_awaited(), "una instalación compatible no pide aprobación"
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes, "cero mutaciones del directorio del juego"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("release", _RELEASES_NEXUS, ids=_id)
    async def test_nexus_sin_api_key_falla_cerrado_antes_de_fronteras(
        self,
        installer: ToolsInstaller,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        release: SkseRelease,
    ) -> None:
        """Sin API key de Nexus: Sky-Claw corta ANTES del HITL y del egress, con mensaje accionable.

        Con API key configurada estos mismos pasos descargan del mod 30379 (`SKSE_PR-3`
        cubre el camino feliz en `TestAdquisicionNexusSkse`). Sin ella no hay adquisición
        posible: el único camino lícito es el mensaje manual —y aun ahí no se ofrece el
        payload viejo (2.2.6), ni se pide HITL, ni se escribe.
        """
        install_dir = _skyrim_limpio(tmp_path)
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: _edicion_de(release))
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: release.game_version)

        hitl, egress = _espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError) as exc_info:
            await installer.ensure_skse(install_dir, session)

        mensaje = str(exc_info.value)
        assert release.game_version in mensaje, "el mensaje nombra el runtime"
        assert release.skse_version in mensaje, "el mensaje nombra el build que el catálogo pide"
        assert "Nexus" in mensaje, "dice por qué no se puede adquirir todavía"
        assert "/beta/" not in mensaje, "no ofrece la URL legacy de otro build como fallback"
        assert mensaje.count("https://skse.silverlock.org/") == 0, (
            "el build de Nexus no vive en silverlock: mandar ahí sería un enlace engañoso"
        )
        assert mensaje.count("https://www.nexusmods.com/skyrimspecialedition/mods/30379") == 1, (
            "el mensaje tiene que mandar a donde está el build (Nexus Mods 30379)"
        )
        hitl.assert_not_awaited(), "no se pide aprobación para una adquisición que no existe"
        egress.assert_not_awaited(), "cero egress antes de saber que la adquisición existe"
        assert set(install_dir.iterdir()) == antes, "cero mutaciones del directorio del juego"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("runtime", ["1.7.105", "1.6.640"])
    async def test_runtime_desconocido_falla_cerrado(
        self,
        installer: ToolsInstaller,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        runtime: str,
    ) -> None:
        """Caso G: un runtime fuera del catálogo no cae a AE ni a "el más nuevo".

        1.7.105 es el siguiente build AE conocido upstream y 1.6.640 un pin común: los
        dos clasifican como edición AE, y es exactamente esa tentación —elegir por
        edición— la que el catálogo prohíbe. El mensaje dice que Sky-Claw no tiene un
        release para ese runtime y manda al sitio oficial, sin sugerir otro build.
        """
        install_dir = _skyrim_limpio(tmp_path)
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: runtime)

        hitl, egress = _espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match=runtime) as exc_info:
            await installer.ensure_skse(install_dir, session)

        assert "no tiene un release" in str(exc_info.value)
        assert str(exc_info.value).count("https://skse.silverlock.org/") == 1
        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes, "cero mutaciones del directorio del juego"

    @pytest.mark.asyncio
    async def test_skyrim_vr_con_edicion_explicita_corta_antes_de_catalogo_hitl_y_egress(
        self,
        installer: ToolsInstaller,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """La familia REAL del ejecutable manda sobre el hint: `edition=SE` no vuelve SE a un VR.

        `SkyrimVR.exe` con una versión PE que *parezca* soportada (1.5.97) no puede
        resolver catálogo, pedir HITL ni bajar el SKSE64 de Steam al directorio del VR:
        la clasificación real da UNKNOWN y el veto de producto corta antes de todo.
        """
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()
        (install_dir / "SkyrimVR.exe").write_bytes(b"MZ")
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.UNKNOWN)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "1.5.97")

        hitl, egress = _espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match="no es compatible"):
            await installer.ensure_skse(install_dir, session, edition=SkyrimEdition.SE)

        hitl.assert_not_awaited(), "el veto corta antes del HITL"
        egress.assert_not_awaited(), "el veto corta antes del egress"
        assert set(install_dir.iterdir()) == antes, "cero mutaciones del directorio del juego"

    @pytest.mark.asyncio
    async def test_edicion_explicita_equivocada_no_reinstala_sobre_instalacion_correcta(
        self,
        installer: ToolsInstaller,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """La idempotencia usa la familia REAL del PE, no el hint del caller.

        `edition=LE` sobre un directorio SE con el SKSE correcto ya instalado
        (`skse64_loader.exe` + `skse64_1_5_97.dll`): sin este contrato, la búsqueda de
        idempotencia miraría `skse_loader.exe` (familia LE), no lo encontraría y
        reinstalaría encima de una instalación que ya funciona.
        """
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()
        (install_dir / "SkyrimSE.exe").write_bytes(b"MZ")
        (install_dir / "skse64_loader.exe").write_bytes(b"MZ")
        (install_dir / "skse64_1_5_97.dll").write_bytes(b"MZ")
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.SE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "1.5.97")

        hitl, egress = _espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        res = await installer.ensure_skse(install_dir, session, edition=SkyrimEdition.LE)

        assert res.already_existed is True
        assert res.verification is InstallVerification.VERIFIED
        assert res.exe_path == install_dir / "skse64_loader.exe"
        hitl.assert_not_awaited(), "una instalación reconocida no pide aprobación"
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes, "cero mutaciones del directorio del juego"

    @pytest.mark.asyncio
    async def test_runtime_desconocido_con_instalacion_presente_igual_corta(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El catálogo resuelve ANTES de mirar el disco: sin release, hasta un par que parece completo corta.

        En disco hay `skse64_loader.exe` + `skse64_1_7_105.dll` (un build que upstream
        quizá publique, pero que Sky-Claw no conoce). El orden del flujo es runtime →
        release → instalación: sin release no hay compatibilidad que declarar, y
        devolver `VERIFIED` por presencia sería afirmar lo que este PR prohíbe. Es el
        ancla que impide mover la idempotencia por encima del catálogo.
        """
        install_dir = _skyrim_limpio(tmp_path)
        (install_dir / "skse64_loader.exe").write_bytes(b"MZ")
        (install_dir / "skse64_1_7_105.dll").write_bytes(b"MZ")
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "1.7.105")

        hitl, egress = _espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match="no tiene un release"):
            await installer.ensure_skse(install_dir, session)

        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes, "cero mutaciones del directorio del juego"

    @pytest.mark.asyncio
    async def test_dll_de_otro_runtime_no_se_reporta_instalado_nexus(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Presencia no absuelve: el DLL del par tiene que corresponder al RUNTIME detectado.

        Skyrim 1.7.104 con `skse64_1_5_97.dll` y el loader: el loader está, hay un DLL
        de runtime, pero no es el de este build — SKSE no cargaría. Sin `game_version`
        en `find_skse_installation`, esto se reportaba `VERIFIED`; el desenlace correcto
        es seguir al catálogo, que manda 2.3.1 por Nexus y corta antes de las fronteras.
        """
        install_dir = _skyrim_limpio(tmp_path)
        (install_dir / "skse64_loader.exe").write_bytes(b"MZ")
        (install_dir / "skse64_1_5_97.dll").write_bytes(b"MZ")
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "1.7.104")

        hitl, egress = _espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match="2.3.1") as exc_info:
            await installer.ensure_skse(install_dir, session)

        assert "Nexus" in str(exc_info.value)
        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes, "cero mutaciones del directorio del juego"

    @pytest.mark.asyncio
    async def test_dll_de_otro_runtime_no_se_reporta_instalado_silverlock(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El gemelo con adquisición directa: mismo caso, el flujo sigue y REINSTALA.

        Skyrim 1.5.97 con `skse64_1_6_1170.dll` en disco: hay loader y hay DLL de
        runtime, pero el build no corresponde. El flujo no puede devolver `VERIFIED`
        (la copia deja el DLL del runtime correcto y el cleanup borra el huérfano).
        """
        install_dir = _skyrim_limpio(tmp_path)
        (install_dir / "skse64_loader.exe").write_bytes(b"MZ")
        (install_dir / "skse64_1_6_1170.dll").write_bytes(b"MZ")

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.SE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "1.5.97")

        session = MagicMock(spec=aiohttp.ClientSession)
        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]
        installer._download_skse_archive = AsyncMock(return_value=None)  # type: ignore[method-assign]
        installer._extract = MagicMock(return_value=None)  # type: ignore[method-assign]
        installer._find_skse_root = MagicMock(return_value=tmp_path / "root")  # type: ignore[method-assign]
        installer._copy_skse_files = AsyncMock(return_value=None)  # type: ignore[method-assign]

        res = await installer.ensure_skse(install_dir, session)

        assert res.already_existed is False, "un SKSE de otro runtime no es una instalación compatible"
        assert res.version == "2.0.20"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("release", _RELEASES_SILVERLOCK, ids=_id)
    async def test_adquisicion_directa_instala_el_release_del_catalogo(
        self,
        installer: ToolsInstaller,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        release: SkseRelease,
    ) -> None:
        """Caso J: los runtimes con payload directo siguen autoinstalándose.

        El payload que se descarga es el de IDENTIDAD del release (mismo dll y mismo
        archive que el catálogo), la versión reportada sale del catálogo y el flujo
        llega hasta la copia. Sin red real: descarga/extracción/raíz mockeadas.
        """
        install_dir = _skyrim_limpio(tmp_path)

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: _edicion_de(release))
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: release.game_version)

        session = MagicMock(spec=aiohttp.ClientSession)
        hitl = AsyncMock(return_value=Decision.APPROVED)
        installer._hitl.request_approval = hitl  # type: ignore[method-assign]
        descarga = AsyncMock(return_value=None)
        installer._download_skse_archive = descarga  # type: ignore[method-assign]
        installer._extract = MagicMock(return_value=None)  # type: ignore[method-assign]
        installer._find_skse_root = MagicMock(return_value=tmp_path / "root")  # type: ignore[method-assign]
        installer._copy_skse_files = AsyncMock(return_value=None)  # type: ignore[method-assign]

        res = await installer.ensure_skse(install_dir, session)

        assert res.already_existed is False
        assert res.version == release.skse_version, "la versión sale del catálogo"
        assert res.exe_path == install_dir / _loader_de(release)
        assert res.verification is InstallVerification.VERIFIED
        hitl.assert_awaited_once()
        cfg_descargado = descarga.call_args.args[1]
        assert cfg_descargado["dll"] == release.dll_name, "el payload bajado es el del release resuelto"
        assert cfg_descargado["url"].rsplit("/", 1)[-1] == release.artifact_name

    def test_todo_test_de_aborto_ancla_sus_fronteras(self) -> None:
        """Ancla enumerativa: los tests de aborto de este archivo no pueden perder sus fronteras.

        Misma regla que el ancla de `test_tools_installer.py::TestEnsureSkse`, acotada a
        ESTA clase: si un test de aborto usa `_espias_de_frontera`, tiene que afirmar
        que ni el HITL ni el egress se cruzaron y que el directorio quedó igual. Un
        test nuevo sin esas tres marcas rompe acá, no en un revisor.
        """
        import ast  # noqa: PLC0415 — solo lo usa esta ancla
        import inspect  # noqa: PLC0415 — solo para excluirse a sí misma sin hardcodear el nombre

        fuente = pathlib.Path(__file__).read_text(encoding="utf-8")
        arbol = ast.parse(fuente)
        clase = next(
            nodo
            for nodo in ast.walk(arbol)
            if isinstance(nodo, ast.ClassDef) and nodo.name == "TestCompatibilidadPorRuntime"
        )

        marco = inspect.currentframe()
        assert marco is not None
        esta_ancla = marco.f_code.co_name

        incumplen: dict[str, list[str]] = {}
        revisados: list[str] = []
        for metodo in clase.body:
            if not isinstance(metodo, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            if not metodo.name.startswith("test_") or metodo.name == esta_ancla:
                continue
            cuerpo = ast.get_source_segment(fuente, metodo) or ""
            if "_espias_de_frontera" not in cuerpo or "pytest.raises" not in cuerpo:
                continue
            revisados.append(metodo.name)
            faltan = [
                marca for marca in ("hitl.assert_not_awaited()", "egress.assert_not_awaited()") if marca not in cuerpo
            ]
            if "iterdir()) == antes" not in cuerpo:
                faltan.append("comparación del directorio contra su estado inicial")
            if faltan:
                incumplen[metodo.name] = faltan

        assert revisados, "el ancla dejó de encontrar tests de aborto: revisá los nombres de los helpers"
        assert not incumplen, "tests de aborto sin anclar todas sus fronteras: " + "; ".join(
            f"{nombre} (falta {', '.join(marcas)})" for nombre, marcas in incumplen.items()
        )


class TestAdquisicionDirectaPorIdentidad:
    """`SKSE_CONFIG` es metadata de adquisición silverlock: se elige por identidad, nunca por edición."""

    def test_silverlock_resuelve_por_identidad_exacta(self) -> None:
        """Cada release con payload directo encuentra SU fila (dll + artifact)."""
        for release in _RELEASES_SILVERLOCK:
            cfg = _adquisicion_para(release)
            assert isinstance(cfg, dict)
            assert cfg["dll"] == release.dll_name
            assert cfg["url"].rsplit("/", 1)[-1] == release.artifact_name
            assert cfg["url"].startswith("https://skse.silverlock.org/beta/")

    def test_nexus_resuelve_a_la_spec_del_canal_sin_payload_legacy(self) -> None:
        """Ningún release Nexus cae al overlay silverlock (incluido el 2.2.6).

        Vuelven a la única spec del canal: mod 30379, familia `SKSE64 Steam`. Cualquier
        desvío hacia la tabla legacy (o su URL) es la mutación que este test existe
        para matar.
        """
        for release in _RELEASES_NEXUS:
            adq = _adquisicion_para(release)
            assert isinstance(adq, tools_installer._SkseNexusAcquisition), (
                "Nexus debe devolver la spec del canal, jamás una fila de SKSE_CONFIG"
            )
            assert adq.nexus_id == 30379
            assert adq.display_name == "Skyrim Script Extender (SKSE64) Steam"
            assert adq.loader_name == "skse64_loader.exe"

    def test_nexus_con_familia_extrana_falla_cerrado(self) -> None:
        """Si el catálogo declarara un release Nexus fuera de la familia SKSE64, corta.

        El canal único no cubre un LE hipotético publicado en Nexus; la política é
        resultado real y no hace fallback ni adivina.
        """
        hipotetico = SkseRelease(
            game_version="1.9.32",
            skse_version="9.9.9",
            dll_name="skse_1_9_32.dll",
            source=SkseSource.NEXUS,
            artifact_name=None,
        )

        with pytest.raises(ToolInstallError, match="sólo cubre la familia SKSE64"):
            _adquisicion_para(hipotetico)

    def test_dll_inexistente_en_la_tabla_falla_cerrado(self) -> None:
        """Un release cuyo DLL no está en SKSE_CONFIG no se adivina por edición."""
        huerfano = SkseRelease(
            game_version="1.5.97",
            skse_version="9.9.9",
            dll_name="skse64_no_existe.dll",
            source=SkseSource.SILVERLOCK,
            artifact_name="skse64_2_00_20.7z",
        )

        with pytest.raises(ToolInstallError, match="adquisición directa única"):
            _adquisicion_para(huerfano)

    def test_silverlock_sin_artifact_name_falla_cerrado(self) -> None:
        """SILVERLOCK exige `artifact_name`: la identidad es DLL + archive, nunca sólo DLL.

        Sin este corte, un release con `artifact_name=None` desactivaba la comparación
        del archive y podía matchear por nombre de DLL un payload viejo de la misma
        familia — exactamente el 2.2.6 de 1.6.1170 que este PR no debe reintroducir.
        """
        sin_artifact = SkseRelease(
            game_version="1.6.1170",
            skse_version="2.2.8",
            dll_name="skse64_1_6_1170.dll",
            source=SkseSource.SILVERLOCK,
            artifact_name=None,
        )

        with pytest.raises(ToolInstallError, match="artifact_name"):
            _adquisicion_para(sin_artifact)

    def test_artifact_incoherente_falla_cerrado(self) -> None:
        """Mismo DLL pero archive distinto: la identidad no coincide y no se adivina."""
        incoherente = SkseRelease(
            game_version="1.5.97",
            skse_version="2.0.20",
            dll_name="skse64_1_5_97.dll",
            source=SkseSource.SILVERLOCK,
            artifact_name="skse64_otro_build.7z",
        )

        with pytest.raises(ToolInstallError, match="adquisición directa única"):
            _adquisicion_para(incoherente)

    def test_dos_payloads_coherentes_falla_cerrado(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Si la tabla declara DOS payloads para la misma identidad, no se elige el primero.

        Ancla contra el "primer match gana": una fila duplicada (o un copy-paste con
        otra clave) tiene que romper, no instalar silenciosamente uno de los dos.
        """
        monkeypatch.setitem(tools_installer.SKSE_CONFIG, "SE-DUPLICADO", dict(tools_installer.SKSE_CONFIG["SE"]))
        release = next(r for r in _RELEASES_SILVERLOCK if r.game_version == "1.5.97")

        with pytest.raises(ToolInstallError, match="2 payload"):
            _adquisicion_para(release)

    def test_ensure_skse_no_elige_compatibilidad_desde_skse_config(self) -> None:
        """Ancla de mutación: `ensure_skse` no puede leer `SKSE_CONFIG` para decidir.

        El defecto que PR-2 cerró es exactamente `SKSE_CONFIG[edicion]` como fuente de
        compatibilidad. La adquisición —que sí vive en `SKSE_CONFIG` para los releases
        silverlock— se resuelve en `_adquisicion_para(release)`, por identidad, y sólo
        DESPUÉS de que el catálogo eligió el build. Si `ensure_skse` vuelve a mirar la
        tabla por su cuenta, la puerta lateral edition-first está de vuelta y este test
        la nombra.
        """
        import ast  # noqa: PLC0415 — solo lo usa esta ancla

        fuente = pathlib.Path(tools_installer.__file__).read_text(encoding="utf-8")
        metodo = next(
            nodo
            for nodo in ast.walk(ast.parse(fuente))
            if isinstance(nodo, ast.AsyncFunctionDef) and nodo.name == "ensure_skse"
        )
        usos = [nodo.lineno for nodo in ast.walk(metodo) if isinstance(nodo, ast.Name) and nodo.id == "SKSE_CONFIG"]

        assert not usos, (
            "ensure_skse volvió a leer SKSE_CONFIG directamente: la adquisición tiene que pasar por "
            f"_adquisicion_para (líneas {usos})"
        )


_MD5_OK = "0" * 32


class _DownloaderMock:
    """NexusDownloader mockeable: registra el ORDEN de las llamadas públicas.

    Nada de esto toca la red ni el staging real del downloader: los métodos son
    ``AsyncMock`` con efectos locales, y ``download`` devuelve el archive que se le
    decidió (un `.7z` de juguete que el flujo limpia con ``unlink(missing_ok=True)``).
    """

    def __init__(self) -> None:
        self.eventos: list[str] = []
        self.files: list[dict] = []
        self.info = None
        self.archive: pathlib.Path | None = None
        self.list_files = AsyncMock(side_effect=self._list_files)
        self.get_file_info = AsyncMock(side_effect=self._get_file_info)
        self.download = AsyncMock(side_effect=self._download)

    async def _list_files(self, *_args: object, **_kwargs: object) -> list[dict]:
        self.eventos.append("list_files")
        return self.files

    async def _get_file_info(self, *_args: object, **_kwargs: object):
        self.eventos.append("get_file_info")
        return self.info

    async def _download(self, *_args: object, **_kwargs: object):
        self.eventos.append("download")
        return self.archive


def _info_nexus(release: SkseRelease, *, md5: str | None = _MD5_OK, size_bytes: int = 930 * 1024, nombre: str = "f.7z"):
    from sky_claw.app.scraper.nexus_downloader import FileInfo

    return FileInfo(
        nexus_id=30379,
        file_id=795992,
        file_name=nombre,
        size_bytes=size_bytes,
        md5=md5 or "",
        download_url="",
    )


def _entrada_nexus(
    release: SkseRelease,
    *,
    primary: bool = False,
    deleted: bool = False,
    nombre: str = "Skyrim Script Extender (SKSE64) Steam",
) -> dict:
    return {
        "file_id": 790000,
        "name": nombre,
        "mod_version": release.skse_version,
        "category_name": "DELETED" if deleted else "MAIN",
        "is_primary": primary,
    }


@pytest.fixture
def installer_con_nexus(tmp_path: pathlib.Path) -> tuple[ToolsInstaller, _DownloaderMock]:
    """Installer con la `nexus_downloader_factory` inyectada y un downloader mockeado.

    Es el hermano del fixture `installer` de más arriba; la diferencia es sólo el
    constructor y su factory lazy (la misma firma nueva del PR-3).
    """
    lock_manager = MagicMock()
    lock_manager.acquire_lock = AsyncMock(return_value=MagicMock(resource_id="tools-install:test"))
    lock_manager.release_lock = AsyncMock(return_value=True)
    lock_manager.renew_lock = AsyncMock(return_value=True)

    downloader = _DownloaderMock()
    return ToolsInstaller(
        hitl=HITLGuard(notify_fn=None, timeout=5),
        gateway=NetworkGateway(EgressPolicy(block_private_ips=False)),
        path_validator=PathValidator(roots=[tmp_path, pathlib.Path(tempfile.gettempdir()) / "sky_claw"]),
        lock_manager=lock_manager,
        install_ttl=60.0,
        nexus_downloader_factory=lambda: downloader,
    ), downloader


class TestAdquisicionNexusSkse:
    """Flujo Nexus de PR-3: orden, identidad del archivo y gates de integridad."""

    # -- Camino feliz ---------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize("release", _RELEASES_NEXUS, ids=_id)
    async def test_nexus_descarga_instala_y_reporta_version_del_catalogo(
        self,
        installer_con_nexus: tuple[ToolsInstaller, _DownloaderMock],
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        release: SkseRelease,
    ) -> None:
        """1.6.1170/1.7.99/1.7.104 → 2.2.8/2.3.0/2.3.1 por Nexus: happy path completo.

        Toda la cadena es real salvo el egress del downloader (que es lo único que se
        mockea). El archive se limpia siempre y el resultado reporta la versión del
        catálogo, no el nombre del archivo subido.
        """
        installer, downloader = installer_con_nexus
        install_dir = _skyrim_limpio(tmp_path)

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: _edicion_de(release))
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: release.game_version)

        downloader.files = [_entrada_nexus(release, primary=True)]
        downloader.info = _info_nexus(release, nombre=f"SKSE-{release.skse_version}-steam.7z")
        archive = tmp_path / "staging-dl" / f"SKSE-{release.skse_version}.7z"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"7z-magic")
        downloader.archive = archive

        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]

        def _extract(_archive: pathlib.Path, destino: pathlib.Path) -> None:
            destino.mkdir(parents=True, exist_ok=True)
            (destino / "skse64_loader.exe").write_bytes(b"MZ")
            (destino / release.dll_name).write_bytes(b"MZ")

        installer._extract = _extract  # type: ignore[method-assign]

        session = MagicMock(spec=aiohttp.ClientSession)
        res = await installer.ensure_skse(install_dir, session)

        assert res.already_existed is False
        assert res.tool_name == "SKSE"
        assert res.exe_path == install_dir / "skse64_loader.exe"
        assert res.version == release.skse_version, "la versión sale del catálogo, no del archivo"
        assert res.verification is InstallVerification.VERIFIED
        assert (install_dir / "skse64_loader.exe").exists()
        assert (install_dir / release.dll_name).exists(), "el payload del release se copió al directorio del juego"
        assert not archive.exists(), "el archive del downloader se limpia siempre"

    @pytest.mark.asyncio
    async def test_orden_operaciones_nexus(
        self,
        installer_con_nexus: tuple[ToolsInstaller, _DownloaderMock],
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Orden ASSERTED: approval → list_files → get_file_info → download → extract.

        Este orden es parte del contrato: el HITL ANTES del egress, y el egress antes
        de escribir el juego. Cualquier reorganización que descargue antes de la
        aprobación, o que escriba el juego antes de validar, cambia esta lista y el
        test lo nombra entera.
        """
        installer, downloader = installer_con_nexus
        install_dir = _skyrim_limpio(tmp_path)

        release = next(r for r in _RELEASES_NEXUS if r.game_version == "1.7.104")
        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: release.game_version)

        downloader.files = [_entrada_nexus(release, primary=True)]
        downloader.info = _info_nexus(release, nombre="s.7z")
        archivo = tmp_path / "staging-dl" / "s.7z"
        archivo.parent.mkdir(parents=True, exist_ok=True)
        archivo.write_bytes(b"7z")
        downloader.archive = archivo

        eventos: list[str] = downloader.eventos

        async def _aprobacion(**_kwargs):
            eventos.append("approval")
            return Decision.APPROVED

        installer._hitl.request_approval = AsyncMock(side_effect=_aprobacion)  # type: ignore[method-assign]

        def _extract(_a: pathlib.Path, destino: pathlib.Path) -> None:
            eventos.append("extract")
            destino.mkdir(parents=True, exist_ok=True)
            (destino / "skse64_loader.exe").write_bytes(b"M")
            (destino / release.dll_name).write_bytes(b"M")

        installer._extract = _extract  # type: ignore[method-assign]

        async def _copy(*_args: object, **_kwargs: object) -> None:
            eventos.append("copy")

        async def _cleanup(*_args: object, **_kwargs: object) -> None:
            eventos.append("cleanup")

        installer._copy_skse_files = _copy  # type: ignore[method-assign]
        installer._cleanup_orphaned_skse_dlls = _cleanup  # type: ignore[method-assign]

        session = MagicMock(spec=aiohttp.ClientSession)
        await installer.ensure_skse(install_dir, session)

        assert eventos == [
            "approval",
            "list_files",
            "get_file_info",
            "download",
            "extract",
            "copy",
            "cleanup",
        ]

    # -- Selección del archivo -----------------------------------------

    def test_seleccion_nexus_unica(self) -> None:
        """Un solo candidato: directo, sin gates extra."""
        from sky_claw.local.tools_installer import _seleccionar_archivo_nexus

        release = next(r for r in _RELEASES_NEXUS if r.game_version == "1.7.104")
        entrada = _entrada_nexus(release, primary=True)

        elegido = _seleccionar_archivo_nexus([entrada], release)
        assert elegido is entrada

    def test_seleccion_nexus_duplicado_con_un_primario_gana_al_primario(self) -> None:
        """Re-subidas del mismo build: sólo `is_primary` desempata, no timestamp."""
        from sky_claw.local.tools_installer import _seleccionar_archivo_nexus

        release = next(r for r in _RELEASES_NEXUS if r.game_version == "1.7.104")
        viejo = dict(_entrada_nexus(release, primary=False), file_id=1)
        primario = dict(_entrada_nexus(release, primary=True), file_id=2)

        elegido = _seleccionar_archivo_nexus([viejo, primario], release)
        assert elegido["file_id"] == 2

    def test_seleccion_nexus_duplicado_sin_primario_falla_cerrado(self) -> None:
        """Duplicados sin señal inequívoca de primario: fail-closed."""
        from sky_claw.local.tools_installer import _seleccionar_archivo_nexus

        release = next(r for r in _RELEASES_NEXUS if r.game_version == "1.7.104")
        a = _entrada_nexus(release, primary=False)
        b = _entrada_nexus(release, primary=False)

        with pytest.raises(ToolInstallError, match="archivo ÚNICO"):
            _seleccionar_archivo_nexus([a, b], release)

    def test_seleccion_nexus_no_matchea_otra_version_del_mismo_canal(self) -> None:
        """El 2.2.6 sigue disponible en Nexus pero no es match del 2.2.8 del catálogo."""
        from sky_claw.local.tools_installer import _seleccionar_archivo_nexus

        release = next(r for r in _RELEASES_NEXUS if r.game_version == "1.6.1170")
        viejo = {
            "file_id": 470991,
            "name": "Skyrim Script Extender (SKSE64) Steam",
            "mod_version": "2.2.6",
            "category_name": "OLD_VERSION",
            "is_primary": False,
        }

        with pytest.raises(ToolInstallError, match="archivo ÚNICO"):
            _seleccionar_archivo_nexus([viejo], release)

    def test_seleccion_nexus_no_matchea_gog_ni_deleted(self) -> None:
        """El GOG (otra familia explícita) y los DELETED nunca se eligen."""
        from sky_claw.local.tools_installer import _seleccionar_archivo_nexus

        release = next(r for r in _RELEASES_NEXUS if r.game_version == "1.7.104")
        files = [
            _entrada_nexus(release, primary=True, nombre="Skyrim Script Extender (SKSE64) GOG"),
            _entrada_nexus(release, primary=True, deleted=True),
        ]

        with pytest.raises(ToolInstallError, match="archivo ÚNICO"):
            _seleccionar_archivo_nexus(files, release)

    # -- Gates de integridad antes del download ---------------------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("md5", "size_bytes", "nombre", "motivo"),
        [
            (None, 100, "f.7z", "sin md5"),
            ("", 100, "f.7z", "md5 vacío"),
            ("zz-not-hex-zz", 100, "f.7z", "md5 no hexadecimal"),
            (_MD5_OK, 0, "f.7z", "tamaño desconocido"),
            (_MD5_OK, tools_installer._SKSE_MAX_ARCHIVE_BYTES + 1, "f.7z", "supera el cap"),
            (_MD5_OK, 100, "f.zip", "formato no soportado por el extractor"),
        ],
    )
    async def test_nexus_integridad_corta_antes_de_download(
        self,
        installer_con_nexus: tuple[ToolsInstaller, _DownloaderMock],
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        md5: str | None,
        size_bytes: int,
        nombre: str,
        motivo: str,
    ) -> None:
        """El gate es PRE-download: sin ello, los límites silverlock se habrían perdido
        en el camino Nexus (el downloader general admite hasta 4 GiB)."""
        installer, downloader = installer_con_nexus
        install_dir = _skyrim_limpio(tmp_path)
        antes = set(install_dir.iterdir())

        release = next(r for r in _RELEASES_NEXUS if r.game_version == "1.7.104")
        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: release.game_version)

        downloader.files = [_entrada_nexus(release, primary=True)]
        downloader.info = _info_nexus(release, md5=md5, size_bytes=size_bytes, nombre=nombre)

        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]

        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError):
            await installer.ensure_skse(install_dir, session)

        downloader.download.assert_not_awaited(), f"el gate de integridad corta antes: {motivo}"
        assert set(install_dir.iterdir()) == antes

    # -- TOCTOU y cleanup -------------------------------------------------

    @pytest.mark.asyncio
    async def test_nexus_toctou_aborta_si_el_runtime_cambia_durante_download(
        self,
        installer_con_nexus: tuple[ToolsInstaller, _DownloaderMock],
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """El segundo gate vuelve a leer el PE y lo compara contra release.game_version.

        Si el runtime cambió durante la ventana de la descarga, se corta — el archive
        Nexus ya descargado se descarta en `finally`, la copia nunca corre y el
        directorio del juego queda intacto.
        """
        installer, downloader = installer_con_nexus
        install_dir = _skyrim_limpio(tmp_path)
        antes = set(install_dir.iterdir())

        release = next(r for r in _RELEASES_NEXUS if r.game_version == "1.7.99")
        siguiente = next(r for r in _RELEASES_NEXUS if r.game_version == "1.7.104")

        version_viva = {"v": release.game_version}
        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: version_viva["v"])

        downloader.files = [_entrada_nexus(release, primary=True)]
        downloader.info = _info_nexus(release, nombre="s.7z")
        archivo = tmp_path / "staging-dl" / "s.7z"
        archivo.parent.mkdir(parents=True, exist_ok=True)
        archivo.write_bytes(b"7z")

        async def _download_y_cambia(_info: object, _session: object) -> pathlib.Path:
            downloader.eventos.append("download")
            version_viva["v"] = siguiente.game_version
            return archivo

        downloader.download = AsyncMock(side_effect=_download_y_cambia)

        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]
        installer._find_skse_root = MagicMock(return_value=tmp_path / "r")  # type: ignore[method-assign]
        installer._copy_skse_files = AsyncMock()  # type: ignore[method-assign]
        installer._cleanup_orphaned_skse_dlls = AsyncMock()  # type: ignore[method-assign]
        installer._extract = MagicMock(side_effect=lambda _a, d: d.mkdir(parents=True, exist_ok=True))  # type: ignore[method-assign]

        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match="1.7.104"):
            await installer.ensure_skse(install_dir, session)

        installer._copy_skse_files.assert_not_awaited()
        installer._cleanup_orphaned_skse_dlls.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes
        assert not archivo.exists(), "el archive descargado se limpia aunque el TOCTOU aborte"

    @pytest.mark.asyncio
    async def test_nexus_cleanup_del_archive_si_la_extraccion_falla(
        self,
        installer_con_nexus: tuple[ToolsInstaller, _DownloaderMock],
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Extracción corrupta: el archive no queda pseudo-cacheado para el próximo intento."""
        installer, downloader = installer_con_nexus
        install_dir = _skyrim_limpio(tmp_path)

        release = next(r for r in _RELEASES_NEXUS if r.game_version == "1.7.104")
        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: release.game_version)

        downloader.files = [_entrada_nexus(release, primary=True)]
        downloader.info = _info_nexus(release, nombre="s.7z")
        archivo = tmp_path / "staging-dl" / "s.7z"
        archivo.parent.mkdir(parents=True, exist_ok=True)
        archivo.write_bytes(b"7z")
        downloader.archive = archivo

        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]

        def _falla(_a: pathlib.Path, _d: pathlib.Path) -> None:
            raise RuntimeError("7z corrupto")

        installer._extract = _falla  # type: ignore[method-assign]

        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(RuntimeError, match="corrupto"):
            await installer.ensure_skse(install_dir, session)

        assert not archivo.exists(), "la extracción fallida también limpia el archive"

    # -- Wiring ------------------------------------------------------------

    def test_app_context_inyecta_la_factory_del_downloader_nexus(self) -> None:
        """Ancla del wiring productivo: el `ToolsInstaller` de `AppContext` debe nacer
        con `nexus_downloader_factory=` para que el camino Nexus exista post-boot.

        Esta prueba no arranca `AppContext` (rompería con symlinks/locks reales); lo que
        ancla es la JUNTA — un constructor que no la pase rompe el autoinstall completo.
        """
        import ast  # noqa: PLC0415 — solo lo usa esta ancla

        fuente = (pathlib.Path(__file__).parent.parent / "sky_claw" / "app_context.py").read_text(encoding="utf-8")
        arbol = ast.parse(fuente)
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call):
                callee = getattr(nodo.func, "id", None) or getattr(nodo.func, "attr", "")
                if callee == "ToolsInstaller":
                    kwargs = {k.arg for k in nodo.keywords if isinstance(k, ast.keyword)}
                    assert "nexus_downloader_factory" in kwargs, (
                        "AppContext construye ToolsInstaller sin nexus_downloader_factory: "
                        "el autoinstall Nexus quedaría siempre cortado aunque el PR-3 esté verde."
                    )
                    return
        raise AssertionError("no encontré la construcción de `ToolsInstaller` en app_context.py")
