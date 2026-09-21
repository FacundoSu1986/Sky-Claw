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
    _adquisicion_directa_para,
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
    async def test_nexus_pendiente_falla_cerrado_antes_de_fronteras(
        self,
        installer: ToolsInstaller,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        release: SkseRelease,
    ) -> None:
        """Caso A/E/F: reconoce el release del catálogo y NO descarga un build viejo.

        1.6.1170 es el caso más filoso: `SKSE_CONFIG["AE"]` todavía tiene la URL de
        2.2.6, y el catálogo manda 2.2.8 por Nexus. La mutación "NEXUS → cae a la URL
        de 2.2.6" tiene que morir acá: el mensaje nombra el pin del catálogo, no
        ofrece ningún `/beta/`, y el gateway no se toca. También cubre que el HITL no
        se pide antes de saber que la adquisición existe.
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
    """`SKSE_CONFIG` es metadata de adquisición: se elige por identidad, nunca por edición."""

    def test_silverlock_resuelve_por_identidad_exacta(self) -> None:
        """Cada release con payload directo encuentra SU fila (dll + artifact)."""
        for release in _RELEASES_SILVERLOCK:
            cfg = _adquisicion_directa_para(release)
            assert cfg["dll"] == release.dll_name
            assert cfg["url"].rsplit("/", 1)[-1] == release.artifact_name
            assert cfg["url"].startswith("https://skse.silverlock.org/beta/")

    def test_nexus_corta_con_el_pin_del_catalogo(self) -> None:
        """Ningún release Nexus resuelve a un payload legacy (2.2.6 incluido)."""
        for release in _RELEASES_NEXUS:
            with pytest.raises(ToolInstallError) as exc_info:
                _adquisicion_directa_para(release)
            mensaje = str(exc_info.value)
            assert release.game_version in mensaje
            assert release.skse_version in mensaje
            assert "/beta/" not in mensaje

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
            _adquisicion_directa_para(huerfano)

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
            _adquisicion_directa_para(sin_artifact)

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
            _adquisicion_directa_para(incoherente)

    def test_dos_payloads_coherentes_falla_cerrado(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Si la tabla declara DOS payloads para la misma identidad, no se elige el primero.

        Ancla contra el "primer match gana": una fila duplicada (o un copy-paste con
        otra clave) tiene que romper, no instalar silenciosamente uno de los dos.
        """
        monkeypatch.setitem(tools_installer.SKSE_CONFIG, "SE-DUPLICADO", dict(tools_installer.SKSE_CONFIG["SE"]))
        release = next(r for r in _RELEASES_SILVERLOCK if r.game_version == "1.5.97")

        with pytest.raises(ToolInstallError, match="2 payload"):
            _adquisicion_directa_para(release)

    def test_ensure_skse_no_elige_compatibilidad_desde_skse_config(self) -> None:
        """Ancla de mutación: `ensure_skse` no puede leer `SKSE_CONFIG` para decidir.

        El defecto que PR-2 cierra es exactamente `SKSE_CONFIG[edicion]` como fuente de
        compatibilidad. La adquisición —que sí vive en `SKSE_CONFIG`— se resuelve en
        `_adquisicion_directa_para(release)`, por identidad, y sólo DESPUÉS de que el
        catálogo eligió el build. Si `ensure_skse` vuelve a mirar la tabla por su
        cuenta, la puerta lateral edition-first está de vuelta y este test la nombra.
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
            f"_adquisicion_directa_para (líneas {usos})"
        )
