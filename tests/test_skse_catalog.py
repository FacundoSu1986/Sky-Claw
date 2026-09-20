"""Contratos del catálogo puro de releases SKSE (``sky_claw/local/tools/skse_catalog.py``).

La tabla se verificó contra fuentes upstream el 2026-09-20 (ver el docstring
del módulo): skse.silverlock.org, ``skse64_whatsnew.txt`` y
``skse64_common/skse_version.h`` de ianpatt/skse64.
"""

from __future__ import annotations

import dataclasses

import pytest

from sky_claw.local import tools_installer
from sky_claw.local.discovery.scanner import skse_dll_game_version, skyrim_version_matches
from sky_claw.local.tools.skse_catalog import SKSE_RELEASES, SkseRelease, SkseSource, resolve_skse_release


class TestResolucionExacta:
    """Un runtime conocido resuelve al descriptor correcto, campo por campo."""

    def test_resuelve_release_1597(self) -> None:
        """1.5.97 → SKSE 2.0.20 desde silverlock, con su 7z directo."""
        release = resolve_skse_release("1.5.97")

        assert release is not None
        assert release.game_version == "1.5.97"
        assert release.skse_version == "2.0.20"
        assert release.dll_name == "skse64_1_5_97.dll"
        assert release.source is SkseSource.SILVERLOCK
        assert release.artifact_name == "skse64_2_00_20.7z"

    def test_resuelve_release_161170(self) -> None:
        """1.6.1170 → SKSE 2.2.8 (recomendado para ese runtime) desde Nexus."""
        release = resolve_skse_release("1.6.1170")

        assert release is not None
        assert release.skse_version == "2.2.8"
        assert release.dll_name == "skse64_1_6_1170.dll"
        assert release.source is SkseSource.NEXUS
        assert release.artifact_name is None  # 2.2.8 no está en silverlock; sin URL verificada

    def test_resuelve_release_1799(self) -> None:
        """1.7.99 → SKSE 2.3.0 desde Nexus, sin artifact directo verificado."""
        release = resolve_skse_release("1.7.99")

        assert release is not None
        assert release.skse_version == "2.3.0"
        assert release.dll_name == "skse64_1_7_99.dll"
        assert release.source is SkseSource.NEXUS
        assert release.artifact_name is None

    def test_resuelve_release_17104(self) -> None:
        """1.7.104 → SKSE 2.3.1 desde Nexus (build que upstream declara vigente)."""
        release = resolve_skse_release("1.7.104")

        assert release is not None
        assert release.skse_version == "2.3.1"
        assert release.dll_name == "skse64_1_7_104.dll"
        assert release.source is SkseSource.NEXUS
        assert release.artifact_name is None

    def test_resuelve_release_1932_legendary(self) -> None:
        """LE también es clave del modelo viejo: el catálogo lo cubre o PR-2 perdería LE."""
        release = resolve_skse_release("1.9.32")

        assert release is not None
        assert release.skse_version == "1.7.3"
        assert release.dll_name == "skse_1_9_32.dll"
        assert release.source is SkseSource.SILVERLOCK
        assert release.artifact_name == "skse_1_07_03.7z"


class TestEntradaAdmisible:
    """El PE reporta 3 ó 4 segmentos numéricos; cualquier otra forma se rechaza."""

    @pytest.mark.parametrize(
        "runtime_pe,game_version",
        [
            ("1.5.97.0", "1.5.97"),
            ("1.6.1170.0", "1.6.1170"),
            ("1.7.99.0", "1.7.99"),  # formato real observado del PE de 1.7.99
            ("1.7.104.0", "1.7.104"),
            ("1.9.32.0", "1.9.32"),
        ],
    )
    def test_runtime_con_cuarto_segmento_de_pe_matchea_catalogo(self, runtime_pe: str, game_version: str) -> None:
        """``"1.7.104.0"`` (cuarto segmento del PE) y ``"1.7.104"`` son el mismo runtime."""
        release = resolve_skse_release(runtime_pe)

        assert release is not None
        assert release.game_version == game_version
        assert release is resolve_skse_release(game_version)

    @pytest.mark.parametrize("entrada", ["", "1.7", "1.6.117", "abc", "1.7.x", "1.7.104.0.5", "1.6.1170 "])
    def test_entrada_parcial_o_malformada_no_resuelve(self, entrada: str) -> None:
        """Ni parcial, ni no-numérica, ni de cinco segmentos, ni con espacios: ``None``."""
        assert resolve_skse_release(entrada) is None

    @pytest.mark.parametrize("entrada", ["1.7.104.beta", "1.6.1170.alfa", "1.5.97.x"])
    def test_sufijo_no_numerico_del_cuarto_segmento_no_resuelve(self, entrada: str) -> None:
        """Hallazgo de revisión: la comparación por prefijo dejaba pasar un cuarto

        segmento arbitrario (``"1.7.104.rc1"`` matcheaba ``1.7.104``) porque sólo
        lee los tres primeros. La versión del PE nunca tiene sufijos, así que
        cualquier forma no numérica se rechaza ANTES de comparar.
        """
        assert resolve_skse_release(entrada) is None


class TestSinFallback:
    """SKSE está pinneado al runtime: un runtime desconocido nunca recibe otro build."""

    def test_runtime_desconocido_no_tiene_fallback(self) -> None:
        """El runtime inmediatamente posterior al último conocido devuelve ``None``."""
        assert resolve_skse_release("1.7.105") is None

    def test_un_runtime_mas_nuevo_desconocido_no_hereda_el_release_anterior(self) -> None:
        """Ningún runtime posterior al último conocido hereda su SKSE."""
        assert resolve_skse_release("1.7.105") is None
        assert resolve_skse_release("1.7.100") is None

    def test_runtime_historico_sin_entrada_no_cae_al_release_actual(self) -> None:
        """1.6.640 fue un runtime AE real (SKSE 2.2.1) pero NO está en el catálogo:

        no puede recibir el build de 1.6.1170. El catálogo sólo modela los
        releases que Sky-Claw conoce y distribuye; el resto es ausencia explícita.
        """
        assert resolve_skse_release("1.6.640") is None

    def test_resolver_no_elige_el_mas_nuevo(self) -> None:
        """Resolver el runtime más viejo devuelve el build más viejo: no hay «latest»."""
        release = resolve_skse_release("1.5.97")

        assert release is not None
        assert release.skse_version == "2.0.20"


class TestInvariantesEstructurales:
    """Propiedades de la tabla misma, no de un runtime puntual."""

    def test_el_catalogo_esta_congelado_por_igualdad_literal(self) -> None:
        """Toda la tabla, enumerada. Un release nuevo o mutado rompe este ancla:

        la decisión pasa por acá (con su evidencia upstream) en vez de colarse
        tapada por los tests de propiedad.
        """
        assert [(r.game_version, r.skse_version, r.dll_name, r.source, r.artifact_name) for r in SKSE_RELEASES] == [
            ("1.5.97", "2.0.20", "skse64_1_5_97.dll", SkseSource.SILVERLOCK, "skse64_2_00_20.7z"),
            ("1.6.1170", "2.2.8", "skse64_1_6_1170.dll", SkseSource.NEXUS, None),
            ("1.7.99", "2.3.0", "skse64_1_7_99.dll", SkseSource.NEXUS, None),
            ("1.7.104", "2.3.1", "skse64_1_7_104.dll", SkseSource.NEXUS, None),
            ("1.9.32", "1.7.3", "skse_1_9_32.dll", SkseSource.SILVERLOCK, "skse_1_07_03.7z"),
        ]

    def test_el_catalogo_no_tiene_game_versions_duplicadas(self) -> None:
        """Ningún runtime puede tener dos entradas."""
        versions = [r.game_version for r in SKSE_RELEASES]
        assert len(versions) == len(set(versions)), f"game_version duplicadas: {versions}"

    def test_game_versions_del_catalogo_son_canonicas_de_tres_segmentos(self) -> None:
        """Precondición de ``resolve_skse_release``: si una fila tuviera dos

        segmentos, el matcheo por prefijo la convertiría en imán de runtimes
        ajenos (``"1.7"`` absorbería ``1.7.99`` y ``1.7.104``). La exactitud
        depende de que toda clave sea ``major.minor.patch``.
        """
        for release in SKSE_RELEASES:
            partes = release.game_version.split(".")
            assert len(partes) == 3, f"{release.game_version!r} no es canónica de tres segmentos"
            assert all(p.isdigit() for p in partes), f"{release.game_version!r} tiene segmentos no numéricos"

    def test_un_runtime_no_matchea_dos_releases(self) -> None:
        """Cierre de la clase «prefijo accidental»: ningún par de filas es

        compatible entre sí bajo la regla de comparación, así que ningún
        runtime puede resolver a dos descriptores distintos.
        """
        for a in SKSE_RELEASES:
            for b in SKSE_RELEASES:
                if a is b:
                    continue
                assert not skyrim_version_matches(a.game_version, b.game_version), (
                    f"{a.game_version} y {b.game_version} se solapan"
                )


class TestDescriptorCoherente:
    """Invariantes por descriptor: DLL, fuente, artifact e inmutabilidad."""

    def test_el_dll_del_descriptor_codifica_su_runtime(self) -> None:
        """El DLL codifica exactamente el runtime del descriptor.

        Usa el decodificador del scanner (LA regla vigente): si una fila dijera
        ``game_version="1.7.104"`` con ``dll_name="skse64_1_7_99.dll"``, este
        test la delata.
        """
        for release in SKSE_RELEASES:
            assert skse_dll_game_version(release.dll_name) == release.game_version, (
                f"{release.dll_name} no codifica el runtime {release.game_version}"
            )

    def test_los_dll_son_reconocibles_como_runtime_skse(self) -> None:
        """Todo ``dll_name`` matchea el patrón que ``find_skse_installation`` busca

        (``skse*.dll``) y no es un no-runtime conocido (steam loader).
        """
        for release in SKSE_RELEASES:
            assert release.dll_name.startswith("skse"), f"{release.dll_name} no matchea el glob skse*.dll"
            assert release.dll_name.endswith(".dll")
            assert "steam_loader" not in release.dll_name

    def test_cada_release_declara_su_fuente(self) -> None:
        """Ningún descriptor puede nacer sin ``source`` explícita."""
        for release in SKSE_RELEASES:
            assert isinstance(release.source, SkseSource), f"{release.game_version} no declara una fuente válida"

    def test_artifact_presente_exactamente_cuando_hay_descarga_directa(self) -> None:
        """Estado de verificación actual: SILVERLOCK tiene 7z directo verificado;

        NEXUS todavía no (su adquisición es deuda de PR-2 — actualizar el ancla
        de igualdad literal el día que se verifique).
        """
        for release in SKSE_RELEASES:
            if release.source is SkseSource.SILVERLOCK:
                assert release.artifact_name is not None, f"{release.game_version}: silverlock sin artifact"
            else:
                assert release.artifact_name is None, f"{release.game_version}: artifact sin URL verificada"

    def test_los_descriptores_son_inmutables(self) -> None:
        """Mutar un descriptor levanta ``FrozenInstanceError``."""
        release = resolve_skse_release("1.7.104")
        assert release is not None

        with pytest.raises(dataclasses.FrozenInstanceError):
            release.skse_version = "2.3.0"  # la mutación ES lo que se prueba

    def test_el_catalogo_es_una_tupla_y_resolve_devuelve_el_descriptor_compartido(self) -> None:
        """La tabla es una tupla (sin «agregar» en runtime) y resolver devuelve el

        MISMO objeto siempre: la identidad compartida es lo que vuelve a la
        inmutabilidad del descriptor una propiedad del sistema, no decoración.
        """
        assert isinstance(SKSE_RELEASES, tuple)
        assert resolve_skse_release("1.7.104") is resolve_skse_release("1.7.104.0")

    def test_resolver_no_conoce_la_edicion(self) -> None:
        """El descriptor no mezcla edición con runtime: ningún campo es una edición."""
        for field in dataclasses.fields(SkseRelease):
            assert field.name not in {"edition", "edicion"}, f"campo de edición colado: {field.name}"


class TestCoherenciaConElModeloVigente:
    """Ancla transitoria contra ``SKSE_CONFIG`` (tools_installer).

    Mientras convivan los dos modelos, ambos deben atribuir el mismo runtime al
    mismo DLL (la identidad que el scanner usa para decidir si una instalación
    en disco sirve): es la clase de defecto «se arregló un hermano y no el
    otro», acá entre modelo viejo y nuevo. El PIN de build puede diferir a
    propósito — hoy 1.6.1170 resuelve a 2.2.8 (recomendado) mientras el
    payload de adquisición legacy sigue siendo ``skse64_2_02_06.7z`` — porque
    ambos son builds válidos del mismo runtime; unificar la adquisición es del
    PR-2. Muere cuando ``ensure_skse`` consuma el catálogo y ``SKSE_CONFIG``
    deje de ser la fuente.
    """

    def test_todo_runtime_cubierto_por_skse_config_resuelve_al_mismo_dll(self) -> None:
        """Todo runtime del modelo viejo resuelve en el catálogo al MISMO DLL."""
        for ed_key, cfg in tools_installer.SKSE_CONFIG.items():
            # La versión que el modelo viejo targetea se deriva del DLL que
            # instala, con la MISMA regla que usa ensure_skse hoy.
            dll_vigente = cfg["dll"]
            assert dll_vigente is not None, f"{ed_key} sin dll en SKSE_CONFIG"
            runtime_vigente = skse_dll_game_version(dll_vigente)

            release = resolve_skse_release(runtime_vigente)

            assert release is not None, f"el runtime de {ed_key} ({runtime_vigente}) no está en el catálogo"
            assert release.dll_name == dll_vigente, (
                f"{ed_key}: el catálogo dice {release.dll_name}, SKSE_CONFIG {dll_vigente}"
            )
