"""P5-A — oráculo de Effective Runtime de solo lectura (ADR 0012 §13/§29.4, Q12).

Cierra —o acota— el blocker ``P5_EFFECTIVE_RUNTIME_ORACLE``: *cómo se demuestra
qué ruta ejecutan realmente MO2/SKSE*. Hasta este slice el consumidor existía
(``journal.reconciliar_journal(observador_effective=...)`` recibe una
``ObservacionEffective``) pero **el productor no**: el callback lo tenía que
inventar cada llamador, y el brief prohíbe explícitamente aceptar "una cadena de
ruta declarada por el llamador" como prueba.

La propiedad que estos tests congelan:

    EFFECTIVE_RUNTIME_IS_OBSERVED, NEVER_DECLARED

con su corolario fail-closed:

    UNKNOWN ⇒ NOT authorizes_activation        (y MATCH sin COMPATIBLE tampoco)

El oráculo distingue cinco evidencias que el brief exige separar y que el código
anterior mezclaba en un solo ``str | None``:

- ruta **persistida**   → ``config_skyrim_path`` (TOML de Sky-Claw) y
  ``[Settings] gamePath`` de MO2 (``@ByteArray`` de Qt, que ``path_resolver.py``
  documenta explícitamente que **no** decodifica);
- ruta **resuelta/normalizada** → la misma evidencia, normalizada sin seguir
  reparse points;
- ruta **retenida en memoria** → imagen de un ``SkyrimSE.exe`` /
  ``ModOrganizer.exe`` vivo (la única prueba de qué se está ejecutando *ahora*);
- **ejecutable seleccionado** → ``[customExecutables]`` de MO2
  (``N\\binary``, ``N\\workingDirectory``), que puede contradecir a ``gamePath``;
- **identidad física y build** → ``RuntimeIdentity`` observada del root + gate
  ``RUNTIME_COMPATIBILITY`` (loader y DLL de SKSE contra el build del ejecutable).

**Lo que este slice NO hace** (declarado, no insinuado):

- No añade ninguna operación mutante al bridge de MO2 ni lo consulta: el plugin
  sólo acepta ``health``/``launch_worker``/``cancel`` (ADR 0007) y no expone el
  game path. El ancla ``test_el_paquete_no_contiene_operaciones_mutantes``
  congela esa ausencia por AST.
- No implementa el store de ``RuntimeCloneRecord``: sin él no hay autoridad
  durable del *Desired Clone*, así que en producción el veredicto es ``UNKNOWN``
  por construcción (``test_sin_autoridad_durable_del_clone_nunca_hay_match``).
  Eso es un hallazgo de FASE D, no un defecto del oráculo.
"""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess

import pytest

from sky_claw.local.frozen_runtime.effective_runtime import (
    CompatibilityResult,
    DesiredRuntime,
    EffectiveRuntimeVerdict,
    EvidenceSource,
    LiveProcessEvidence,
    LiveProcessKind,
    LiveProcessSnapshot,
    ObservationState,
    ReasonCode,
    RuntimeCompatibility,
    RuntimeProbes,
    assess_effective_runtime,
    assess_runtime_compatibility,
    decode_qt_byte_array,
    decode_qt_scalar,
    leer_ejecutables_de_mo2,
    leer_game_path_de_mo2,
)
from sky_claw.local.runtime_vault.models import RuntimeIdentity

PAQUETE = pathlib.Path("sky_claw/local/frozen_runtime/effective_runtime")


# ===========================================================================
# Helpers de fixture
# ===========================================================================


def _byte_array(ruta: str) -> str:
    """Serializa *ruta* como la escribe MO2: ``@ByteArray`` con ``\\\\`` por barra.

    Verificado contra un ``modorganizer.ini`` real de esta máquina, no inferido:
    ``gamePath=@ByteArray(G:\\\\Modding\\\\Skyrim_Runtime_1.6.1170)``.
    """
    return "@ByteArray(" + ruta.replace("\\", "\\\\") + ")"


def _ini_mo2(
    destino: pathlib.Path,
    *,
    game_path: str | None,
    ejecutables: tuple[tuple[str, str], ...] = (),
    seccion_game_path: str = "General",
) -> pathlib.Path:
    """Escribe un ``modorganizer.ini`` con la MISMA forma que uno real de MO2 2.5.2.

    ``gamePath`` va en ``[General]`` (junto a ``gameName``/``selected_profile``/
    ``game_edition``), no en ``[Settings]``: eso es lo que hace un MO2 real, y
    asumir lo contrario fue el primer defecto de este slice — los fixtures y el
    lector compartían la misma suposición equivocada, así que la suite quedaba
    verde sobre un ini que MO2 nunca escribe. Lo detectó la sonda contra el rig
    real, no el test.

    ``seccion_game_path`` existe para poder afirmar el caso negativo (clave
    homónima en otra sección ⇒ ``ABSENT`` diagnosticable).
    """
    lineas = [
        "[General]",
        "gameName=Skyrim Special Edition",
        "selected_profile=@ByteArray(Default)",
    ]
    if game_path is not None and seccion_game_path == "General":
        lineas.append(f"gamePath={_byte_array(game_path)}")
    lineas.extend(["game_edition=Steam", "version=2.5.2", ""])
    lineas.append("[PluginPersistance]")
    lineas.append("Python%20Proxy\\tryInit=false")
    lineas.append("")
    lineas.append("[Settings]")
    lineas.append("profile_local_inis=true")
    if game_path is not None and seccion_game_path == "Settings":
        lineas.append(f"gamePath={_byte_array(game_path)}")
    lineas.append("base_directory=" + str(destino.parent).replace("\\", "/"))
    lineas.append("")
    lineas.append("[Geometry]")
    lineas.append("MainWindow_state=@ByteArray(\\x1\\xd9\\x0\\x0\\xff)")
    if ejecutables:
        lineas.append("")
        lineas.append("[customExecutables]")
        lineas.append(f"size={len(ejecutables)}")
        for indice, (titulo, binario) in enumerate(ejecutables, start=1):
            lineas.append(f"{indice}\\arguments=")
            lineas.append(f"{indice}\\binary={binario}")
            lineas.append(f"{indice}\\title={titulo}")
            lineas.append(f"{indice}\\workingDirectory={binario.rsplit('/', 1)[0]}")
    ruta = destino / "modorganizer.ini"
    destino.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(("\r\n".join(lineas) + "\r\n").encode("utf-8"))
    return ruta


def _root_juego(destino: pathlib.Path, *, exe: str = "SkyrimSE.exe") -> pathlib.Path:
    """Crea un root que *parece* un runtime (archivos vacíos, sin PE válido)."""
    destino.mkdir(parents=True, exist_ok=True)
    (destino / exe).write_bytes(b"MZ")
    return destino


def _probes(
    *,
    version: str = "1.6.1170",
    compatibilidad: RuntimeCompatibility = RuntimeCompatibility.COMPATIBLE,
    procesos: LiveProcessSnapshot | None = None,
    motivo_compat: str = "fake: compatible",
) -> RuntimeProbes:
    """Probes deterministas: el oráculo no debe fabricar identidad propia."""

    def identidad(_root: pathlib.Path) -> RuntimeIdentity:
        return RuntimeIdentity(game_key="skyrimse", game_version=version)

    def compatibilidad_fn(_root: pathlib.Path) -> CompatibilityResult:
        return CompatibilityResult(
            state=compatibilidad,
            reason=motivo_compat,
            game_version=version,
            skse_loader=None,
            skse_dll=None,
        )

    def procesos_fn() -> LiveProcessSnapshot:
        return procesos if procesos is not None else LiveProcessSnapshot(processes=(), indeterminate=False)

    return RuntimeProbes(identity=identidad, compatibility=compatibilidad_fn, processes=procesos_fn)


def _vivo(
    kind: LiveProcessKind,
    *,
    image_path: pathlib.Path,
    pid: int = 4242,
    started_at_ns: int = 1,
) -> LiveProcessEvidence:
    return LiveProcessEvidence(kind=kind, pid=pid, image_path=str(image_path), started_at_ns=started_at_ns)


# ===========================================================================
# Decodificador Qt — la "prueba focal de lectura" que faltaba
# ===========================================================================


class TestDecodificadorQt:
    """``gamePath`` de MO2 es ``@ByteArray``: sin decodificarlo no hay evidencia."""

    def test_decodifica_la_ruta_real_de_esta_maquina(self) -> None:
        """Forma verificada contra un ``modorganizer.ini`` real (no inventada)."""
        crudo = "@ByteArray(G:\\\\Modding\\\\Skyrim_Runtime_1.6.1170)"
        assert decode_qt_byte_array(crudo) == b"G:\\Modding\\Skyrim_Runtime_1.6.1170"
        assert decode_qt_scalar(crudo) == "G:\\Modding\\Skyrim_Runtime_1.6.1170"

    def test_decodifica_escapes_hex_de_longitud_variable(self) -> None:
        """Qt consume hasta DOS dígitos hex: ``\\x1\\xd9\\x0`` no es ``\\x1d\\x90``.

        El caso ``\\x1234`` congela la cota por el lado que sí discrimina: sin
        cota, el acumulador pasa de 255 y ``bytearray.append`` explota. La
        primera versión de este test sólo usaba ``\\x1\\xd9\\x0\\x0\\xff``, que el
        mutante "sin cota" pasaba igual — un test que no mata al mutante no
        gobierna la propiedad.
        """
        assert decode_qt_byte_array("@ByteArray(\\x1\\xd9\\x0\\x0\\xff)") == b"\x01\xd9\x00\x00\xff"
        assert decode_qt_byte_array("@ByteArray(\\x1234)") == b"\x12" + b"34"

    def test_una_barra_escapada_no_se_come_el_resto(self) -> None:
        """``\\\\n`` es barra + ene, no salto de línea (trampa del path de Windows)."""
        assert decode_qt_byte_array("@ByteArray(C:\\\\nueva\\\\carpeta)") == b"C:\\nueva\\carpeta"

    def test_texto_plano_no_se_interpreta(self) -> None:
        """``base_directory`` es plano: decodificarlo lo corrompería."""
        assert decode_qt_byte_array("G:/Modding/MO2/SkyrimSE") is None
        assert decode_qt_scalar("G:/Modding/MO2/SkyrimSE") == "G:/Modding/MO2/SkyrimSE"

    def test_byte_array_sin_cerrar_es_none_y_no_basura(self) -> None:
        """Un valor truncado no se adivina: se declara ilegible."""
        assert decode_qt_byte_array("@ByteArray(G:/Modding") is None


class TestLecturaDelIniDeMo2:
    """Lectura read-only de la instancia: ausencia ≠ corrupción."""

    def test_lee_el_game_path_persistido(self, tmp_path: pathlib.Path) -> None:
        instancia = tmp_path / "instancia"
        _ini_mo2(instancia, game_path="G:\\Modding\\Runtime_A")
        evidencia = leer_game_path_de_mo2(instancia / "modorganizer.ini")
        assert evidencia.state is ObservationState.OBSERVED
        assert evidencia.path == "G:\\Modding\\Runtime_A"

    def test_el_game_path_vive_en_general_y_no_en_settings(self, tmp_path: pathlib.Path) -> None:
        """Ancla del defecto real: MO2 2.5.2 escribe ``gamePath`` en ``[General]``.

        El primer lector de este slice buscaba en ``[Settings]`` y devolvía
        ``ABSENT`` sobre una instancia perfectamente configurada. La sonda contra
        el rig real lo detectó; este test lo congela.
        """
        instancia = tmp_path / "instancia"
        _ini_mo2(instancia, game_path="G:\\Modding\\Runtime_A")
        texto = (instancia / "modorganizer.ini").read_text(encoding="utf-8")
        antes_de_settings = texto.split("[Settings]")[0]
        assert "gamePath=@ByteArray(" in antes_de_settings

        evidencia = leer_game_path_de_mo2(instancia / "modorganizer.ini")
        assert evidencia.state is ObservationState.OBSERVED

    def test_gamepath_en_otra_seccion_es_absent_y_diagnosticable(self, tmp_path: pathlib.Path) -> None:
        """Una clave homónima fuera de ``[General]`` no se lee: falla cerrado."""
        instancia = tmp_path / "instancia"
        _ini_mo2(instancia, game_path="G:\\Modding\\Runtime_A", seccion_game_path="Settings")
        evidencia = leer_game_path_de_mo2(instancia / "modorganizer.ini")
        assert evidencia.state is ObservationState.ABSENT
        assert evidencia.path is None
        assert "Settings" in evidencia.detail
        assert "General" in evidencia.detail

    def test_ini_ausente_es_ausencia_no_error(self, tmp_path: pathlib.Path) -> None:
        evidencia = leer_game_path_de_mo2(tmp_path / "no-existe.ini")
        assert evidencia.state is ObservationState.ABSENT
        assert evidencia.path is None

    def test_ini_sin_gamepath_es_ausencia(self, tmp_path: pathlib.Path) -> None:
        instancia = tmp_path / "instancia"
        _ini_mo2(instancia, game_path=None)
        evidencia = leer_game_path_de_mo2(instancia / "modorganizer.ini")
        assert evidencia.state is ObservationState.ABSENT
        assert evidencia.path is None

    def test_gamepath_ilegible_es_unreadable_y_no_ausencia(self, tmp_path: pathlib.Path) -> None:
        """``@ByteArray`` truncado: no se puede afirmar ni la ruta ni que no exista."""
        instancia = tmp_path / "instancia"
        instancia.mkdir(parents=True)
        (instancia / "modorganizer.ini").write_bytes(
            b"[General]\r\ngameName=Skyrim Special Edition\r\ngamePath=@ByteArray(G:/Modding\r\n"
        )
        evidencia = leer_game_path_de_mo2(instancia / "modorganizer.ini")
        assert evidencia.state is ObservationState.UNREADABLE
        assert evidencia.path is None

    def test_lee_los_ejecutables_seleccionados(self, tmp_path: pathlib.Path) -> None:
        """El ejecutable de ``[customExecutables]`` puede contradecir a ``gamePath``."""
        instancia = tmp_path / "instancia"
        _ini_mo2(
            instancia,
            game_path="G:\\Modding\\Runtime_A",
            ejecutables=(
                ("SKSE", "G:/Modding/Runtime_B/skse64_loader.exe"),
                ("Skyrim Special Edition", "G:/Modding/Runtime_B/SkyrimSE.exe"),
            ),
        )
        ejecutables = leer_ejecutables_de_mo2(instancia / "modorganizer.ini")
        assert [e.title for e in ejecutables] == ["SKSE", "Skyrim Special Edition"]
        assert [e.binary for e in ejecutables] == [
            "G:/Modding/Runtime_B/skse64_loader.exe",
            "G:/Modding/Runtime_B/SkyrimSE.exe",
        ]


# ===========================================================================
# FASE C — los diez escenarios exigidos
# ===========================================================================


class TestEscenariosFaseC:
    """Los diez escenarios del brief, sin tocar instalaciones reales."""

    def test_1_mo2_apuntando_al_clone_correcto_da_match(self, tmp_path: pathlib.Path) -> None:
        """Escenario 1: todas las superficies coinciden con el Desired Clone."""
        clone = _root_juego(tmp_path / "clone")
        instancia = tmp_path / "instancia"
        _ini_mo2(
            instancia,
            game_path=str(clone),
            ejecutables=(("SKSE", f"{clone.as_posix()}/skse64_loader.exe"),),
        )
        resultado = assess_effective_runtime(
            mo2_instance_dir=instancia,
            desired=DesiredRuntime(clone_id="clone-a", clone_root=clone, generation_id="gen-a"),
            probes=_probes(),
        )
        assert resultado.verdict is EffectiveRuntimeVerdict.MATCH
        assert ReasonCode.EFFECTIVE_MATCHES_DESIRED_CLONE in resultado.reasons
        assert resultado.authorizes_activation is True
        assert resultado.effective_path is not None
        assert os.path.normcase(resultado.effective_path) == os.path.normcase(str(clone))

    def test_2_mo2_apuntando_a_steam_es_mismatch(self, tmp_path: pathlib.Path) -> None:
        """Escenario 2: la Managed Source nunca es sustituto del Runtime Clone."""
        steam = _root_juego(tmp_path / "steam")
        clone = _root_juego(tmp_path / "clone")
        instancia = tmp_path / "instancia"
        _ini_mo2(instancia, game_path=str(steam))
        resultado = assess_effective_runtime(
            mo2_instance_dir=instancia,
            desired=DesiredRuntime(clone_id="clone-a", clone_root=clone),
            managed_source_root=steam,
            probes=_probes(),
        )
        assert resultado.verdict is EffectiveRuntimeVerdict.MISMATCH
        assert ReasonCode.EFFECTIVE_IS_MANAGED_SOURCE in resultado.reasons
        assert resultado.authorizes_activation is False

    def test_3_mo2_apuntando_a_otro_clone_es_mismatch(self, tmp_path: pathlib.Path) -> None:
        """Escenario 3: un Clone distinto del Desired no autoriza activación."""
        otro = _root_juego(tmp_path / "clone-otro")
        deseado = _root_juego(tmp_path / "clone-deseado")
        instancia = tmp_path / "instancia"
        _ini_mo2(instancia, game_path=str(otro))
        resultado = assess_effective_runtime(
            mo2_instance_dir=instancia,
            desired=DesiredRuntime(clone_id="clone-deseado", clone_root=deseado),
            probes=_probes(),
        )
        assert resultado.verdict is EffectiveRuntimeVerdict.MISMATCH
        assert ReasonCode.EFFECTIVE_IS_NOT_DESIRED_CLONE in resultado.reasons
        assert resultado.authorizes_activation is False

    def test_4_skse_incompatible_con_el_ejecutable(self, tmp_path: pathlib.Path) -> None:
        """Escenario 4: loader + DLL presentes pero de OTRO build ⇒ INCOMPATIBLE."""
        raiz = _root_juego(tmp_path / "runtime")
        (raiz / "skse64_loader.exe").write_bytes(b"MZ")
        (raiz / "skse64_1_5_97.dll").write_bytes(b"MZ")

        resultado = assess_runtime_compatibility(
            raiz, identity=lambda _root: RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170")
        )
        assert resultado.state is RuntimeCompatibility.INCOMPATIBLE
        assert "1.5.97" in resultado.reason

    def test_4b_skse_compatible_da_compatible(self, tmp_path: pathlib.Path) -> None:
        """Hermano positivo del escenario 4: sin él, INCOMPATIBLE sería un literal."""
        raiz = _root_juego(tmp_path / "runtime")
        (raiz / "skse64_loader.exe").write_bytes(b"MZ")
        (raiz / "skse64_1_6_1170.dll").write_bytes(b"MZ")

        resultado = assess_runtime_compatibility(
            raiz, identity=lambda _root: RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")
        )
        assert resultado.state is RuntimeCompatibility.COMPATIBLE

    def test_4c_sin_skse_es_unknown_y_no_compatible(self, tmp_path: pathlib.Path) -> None:
        """Ausencia de SKSE no prueba incompatibilidad, pero jamás es COMPATIBLE."""
        raiz = _root_juego(tmp_path / "runtime")
        resultado = assess_runtime_compatibility(
            raiz, identity=lambda _root: RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170")
        )
        assert resultado.state is RuntimeCompatibility.UNKNOWN
        assert resultado.state is not RuntimeCompatibility.COMPATIBLE

    def test_4e_sin_version_del_ejecutable_no_hay_compatibilidad(self, tmp_path: pathlib.Path) -> None:
        """SKSE perfecto en disco + build inafirmable ⇒ UNKNOWN, no COMPATIBLE.

        ``find_skse_installation`` con ``game_version=""`` degrada a "loader +
        algún DLL": llamarlo así devolvería COMPATIBLE sin haber verificado el
        build, que es exactamente el ``UNKNOWN != COMPATIBLE`` del ADR.
        """
        raiz = _root_juego(tmp_path / "runtime")
        (raiz / "skse64_loader.exe").write_bytes(b"MZ")
        (raiz / "skse64_1_6_1170.dll").write_bytes(b"MZ")

        resultado = assess_runtime_compatibility(
            raiz, identity=lambda _root: RuntimeIdentity(game_key="skyrimse", game_version="")
        )
        assert resultado.state is RuntimeCompatibility.UNKNOWN
        assert resultado.state is not RuntimeCompatibility.COMPATIBLE

    def test_4d_compatibilidad_incompatible_no_autoriza_aunque_el_path_coincida(self, tmp_path: pathlib.Path) -> None:
        """El path correcto con SKSE roto NO es MATCH: el gate manda."""
        clone = _root_juego(tmp_path / "clone")
        instancia = tmp_path / "instancia"
        _ini_mo2(instancia, game_path=str(clone))
        resultado = assess_effective_runtime(
            mo2_instance_dir=instancia,
            desired=DesiredRuntime(clone_id="clone-a", clone_root=clone),
            probes=_probes(compatibilidad=RuntimeCompatibility.INCOMPATIBLE),
        )
        assert resultado.verdict is not EffectiveRuntimeVerdict.MATCH
        assert ReasonCode.SKSE_INCOMPATIBLE in resultado.reasons
        assert resultado.authorizes_activation is False

    def test_5_ruta_inexistente_no_autoriza(self, tmp_path: pathlib.Path) -> None:
        """Escenario 5: el Desired apunta a un root que no existe ⇒ UNKNOWN."""
        fantasma = tmp_path / "no-existe"
        instancia = tmp_path / "instancia"
        _ini_mo2(instancia, game_path=str(fantasma))
        resultado = assess_effective_runtime(
            mo2_instance_dir=instancia,
            desired=DesiredRuntime(clone_id="clone-a", clone_root=fantasma),
            probes=_probes(),
        )
        assert resultado.verdict is EffectiveRuntimeVerdict.UNKNOWN
        assert ReasonCode.EFFECTIVE_PATH_MISSING in resultado.reasons
        assert resultado.authorizes_activation is False

    def test_6_junction_no_es_una_ruta_aceptable(self, tmp_path: pathlib.Path) -> None:
        """Escenario 6: un reparse point no es el Runtime Clone, ni apuntando bien."""
        real = _root_juego(tmp_path / "real")
        enlace = tmp_path / "enlace"
        creado = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(enlace), str(real)],
            capture_output=True,
            check=False,
        )
        if creado.returncode != 0 or not enlace.exists():
            pytest.skip("no se pudo crear junction (sin privilegio o FS sin soporte)")

        instancia = tmp_path / "instancia"
        _ini_mo2(instancia, game_path=str(enlace))
        resultado = assess_effective_runtime(
            mo2_instance_dir=instancia,
            desired=DesiredRuntime(clone_id="clone-a", clone_root=enlace),
            probes=_probes(),
        )
        assert resultado.verdict is EffectiveRuntimeVerdict.MISMATCH
        assert ReasonCode.REPARSE_NOT_ALLOWED in resultado.reasons
        assert resultado.authorizes_activation is False

    def test_7_la_ruta_en_memoria_gana_sobre_la_persistida(self, tmp_path: pathlib.Path) -> None:
        """Escenario 7: el ini dice A, pero el juego vivo corre desde B ⇒ B manda."""
        a = _root_juego(tmp_path / "runtime-a")
        b = _root_juego(tmp_path / "runtime-b")
        instancia = tmp_path / "instancia"
        _ini_mo2(instancia, game_path=str(a))

        resultado = assess_effective_runtime(
            mo2_instance_dir=instancia,
            desired=DesiredRuntime(clone_id="clone-a", clone_root=a),
            probes=_probes(
                procesos=LiveProcessSnapshot(
                    processes=(_vivo(LiveProcessKind.GAME, image_path=b / "SkyrimSE.exe"),),
                    indeterminate=False,
                )
            ),
        )
        assert resultado.effective_source is EvidenceSource.LIVE_GAME_PROCESS
        assert resultado.effective_path is not None
        assert os.path.normcase(resultado.effective_path) == os.path.normcase(str(b))
        assert ReasonCode.PERSISTED_DIVERGES_FROM_LIVE in resultado.reasons
        assert resultado.verdict is EffectiveRuntimeVerdict.MISMATCH

    def test_8_mo2_abierto_con_ini_mas_nuevo_es_indeterminado(self, tmp_path: pathlib.Path) -> None:
        """Escenario 8: MO2 vivo anterior al ini puede retener otro gamePath."""
        a = _root_juego(tmp_path / "runtime-a")
        instancia = tmp_path / "instancia"
        ini = _ini_mo2(instancia, game_path=str(a))

        mtime_ns = ini.stat().st_mtime_ns
        resultado = assess_effective_runtime(
            mo2_instance_dir=instancia,
            desired=DesiredRuntime(clone_id="clone-a", clone_root=a),
            probes=_probes(
                procesos=LiveProcessSnapshot(
                    processes=(
                        _vivo(
                            LiveProcessKind.MO2,
                            image_path=instancia / "ModOrganizer.exe",
                            started_at_ns=mtime_ns - 60 * 10**9,
                        ),
                    ),
                    indeterminate=False,
                )
            ),
        )
        assert resultado.verdict is EffectiveRuntimeVerdict.UNKNOWN
        assert ReasonCode.MO2_IN_MEMORY_MAY_DIVERGE in resultado.reasons
        assert resultado.authorizes_activation is False

    def test_8b_mo2_abierto_despues_del_ini_no_invalida_la_evidencia(self, tmp_path: pathlib.Path) -> None:
        """Hermano de 8: MO2 arrancado DESPUÉS del ini leyó ese valor."""
        a = _root_juego(tmp_path / "runtime-a")
        instancia = tmp_path / "instancia"
        ini = _ini_mo2(instancia, game_path=str(a))

        resultado = assess_effective_runtime(
            mo2_instance_dir=instancia,
            desired=DesiredRuntime(clone_id="clone-a", clone_root=a),
            probes=_probes(
                procesos=LiveProcessSnapshot(
                    processes=(
                        _vivo(
                            LiveProcessKind.MO2,
                            image_path=instancia / "ModOrganizer.exe",
                            started_at_ns=ini.stat().st_mtime_ns + 60 * 10**9,
                        ),
                    ),
                    indeterminate=False,
                )
            ),
        )
        assert resultado.verdict is EffectiveRuntimeVerdict.MATCH
        assert ReasonCode.MO2_IN_MEMORY_MAY_DIVERGE not in resultado.reasons

    def test_9_una_actualizacion_de_steam_no_cambia_el_veredicto(self, tmp_path: pathlib.Path) -> None:
        """Escenario 9: Steam cambia su árbol; el Clone no se toca."""
        clone = _root_juego(tmp_path / "clone")
        steam_viejo = _root_juego(tmp_path / "steam-viejo")
        steam_nuevo = _root_juego(tmp_path / "steam-nuevo")
        (steam_nuevo / "SkyrimSE.exe").write_bytes(b"MZ ACTUALIZADO")

        instancia = tmp_path / "instancia"
        _ini_mo2(instancia, game_path=str(clone))
        deseado = DesiredRuntime(clone_id="clone-a", clone_root=clone)

        antes = assess_effective_runtime(
            mo2_instance_dir=instancia, desired=deseado, managed_source_root=steam_viejo, probes=_probes()
        )
        despues = assess_effective_runtime(
            mo2_instance_dir=instancia, desired=deseado, managed_source_root=steam_nuevo, probes=_probes()
        )
        assert antes.verdict is despues.verdict is EffectiveRuntimeVerdict.MATCH
        assert antes.effective_path == despues.effective_path
        assert antes.reasons == despues.reasons

    def test_7b_las_tools_de_skyclaw_apuntando_a_otro_arbol_es_mismatch(self, tmp_path: pathlib.Path) -> None:
        """Escenario 7, la otra mitad: la superficie de Sky-Claw también cuenta.

        Es el hallazgo del rig real de desarrollo: ``skyrim_path`` persistido
        apuntaba a la instalación de Steam mientras el ``gamePath`` de MO2
        apuntaba a un runtime externo. El oráculo observaba ambos valores y no
        decía nada. Con las tools de Sky-Claw resolviendo a otro árbol, LOOT,
        xEdit, DynDOLOD y el grass escriben en el runtime saliente mientras el
        juego corre desde el otro (§29.4).
        """
        clone = _root_juego(tmp_path / "clone")
        steam = _root_juego(tmp_path / "steam")
        instancia = tmp_path / "instancia"
        _ini_mo2(instancia, game_path=str(clone))
        resultado = assess_effective_runtime(
            mo2_instance_dir=instancia,
            desired=DesiredRuntime(clone_id="clone-a", clone_root=clone),
            managed_source_root=steam,
            config_skyrim_path=str(steam),
            probes=_probes(),
        )
        assert resultado.verdict is EffectiveRuntimeVerdict.MISMATCH
        assert ReasonCode.CONFIG_DIVERGES_FROM_EFFECTIVE in resultado.reasons
        assert resultado.authorizes_activation is False

    def test_7c_config_alineada_con_el_efectivo_no_es_mismatch(self, tmp_path: pathlib.Path) -> None:
        """Hermano positivo de 7b: sin él, MISMATCH sería un literal."""
        clone = _root_juego(tmp_path / "clone")
        instancia = tmp_path / "instancia"
        _ini_mo2(instancia, game_path=str(clone))
        resultado = assess_effective_runtime(
            mo2_instance_dir=instancia,
            desired=DesiredRuntime(clone_id="clone-a", clone_root=clone),
            config_skyrim_path=str(clone),
            environ={"SKYRIM_PATH": str(clone)},
            probes=_probes(),
        )
        assert resultado.verdict is EffectiveRuntimeVerdict.MATCH
        assert ReasonCode.CONFIG_DIVERGES_FROM_EFFECTIVE not in resultado.reasons

    def test_10_evidencia_insuficiente_es_unknown_y_no_autoriza(self, tmp_path: pathlib.Path) -> None:
        """Escenario 10: sin ninguna superficie observable, UNKNOWN."""
        resultado = assess_effective_runtime(
            mo2_instance_dir=tmp_path / "instancia-sin-ini",
            desired=None,
            probes=_probes(),
        )
        assert resultado.verdict is EffectiveRuntimeVerdict.UNKNOWN
        assert ReasonCode.NO_EFFECTIVE_EVIDENCE in resultado.reasons
        assert resultado.authorizes_activation is False
        assert resultado.effective_path is None

    def test_10b_proceso_ilegible_degrada_a_unknown(self, tmp_path: pathlib.Path) -> None:
        """Un probe que no puede leer la tabla de procesos no se lee como "no hay"."""
        clone = _root_juego(tmp_path / "clone")
        instancia = tmp_path / "instancia"
        _ini_mo2(instancia, game_path=str(clone))
        resultado = assess_effective_runtime(
            mo2_instance_dir=instancia,
            desired=DesiredRuntime(clone_id="clone-a", clone_root=clone),
            probes=_probes(procesos=LiveProcessSnapshot(processes=(), indeterminate=True, detail="AccessDenied")),
        )
        assert resultado.verdict is EffectiveRuntimeVerdict.UNKNOWN
        assert resultado.authorizes_activation is False


# ===========================================================================
# Contratos de FASE C y anclas estructurales
# ===========================================================================


class TestContratosYAnclas:
    """``UNKNOWN`` nunca autoriza, y la ausencia de mutación se congela por AST."""

    def test_unknown_nunca_autoriza_activacion(self) -> None:
        """El invariante del brief, afirmado sobre el TIPO y no sobre un caso."""
        for veredicto in EffectiveRuntimeVerdict:
            assessment = _assessment_minimo(veredicto)
            esperado = veredicto is EffectiveRuntimeVerdict.MATCH
            assert assessment.authorizes_activation is esperado, veredicto

    def test_match_sin_compatibilidad_no_autoriza(self) -> None:
        """MATCH exige además ``RUNTIME_COMPATIBILITY != UNKNOWN``."""
        for estado in RuntimeCompatibility:
            assessment = _assessment_minimo(EffectiveRuntimeVerdict.MATCH, compatibility=estado)
            assert assessment.authorizes_activation is (estado is RuntimeCompatibility.COMPATIBLE), estado

    def test_sin_autoridad_durable_del_clone_nunca_hay_match(self, tmp_path: pathlib.Path) -> None:
        """Hallazgo FASE D: sin store de ``RuntimeCloneRecord`` no hay Desired Clone.

        ``desired_runtime_from_storage`` sólo puede leer ``active.json`` v1
        (``desired_active_generation``). Un ``clone_root`` durable **no existe**,
        así que el oráculo no puede probar igualdad contra un Desired y debe
        devolver UNKNOWN. Convertir esa ausencia en MATCH sería inventar la
        autoridad que falta.
        """
        from sky_claw.local.frozen_runtime.effective_runtime import desired_runtime_from_storage

        root = tmp_path / "frozen"
        root.mkdir()
        deseado = desired_runtime_from_storage(root)
        assert deseado.clone_root is None

        clone = _root_juego(tmp_path / "clone")
        instancia = tmp_path / "instancia"
        _ini_mo2(instancia, game_path=str(clone))
        resultado = assess_effective_runtime(mo2_instance_dir=instancia, desired=deseado, probes=_probes())
        assert resultado.verdict is EffectiveRuntimeVerdict.UNKNOWN
        assert ReasonCode.NO_DESIRED_CLONE in resultado.reasons

    def test_la_enumeracion_de_fuentes_es_exhaustiva_y_no_vacia(self) -> None:
        """Ancla enumerativa: una fuente nueva rompe este test hasta cablearse."""
        assert set(EvidenceSource) == {
            EvidenceSource.CONFIG_SKYRIM_PATH,
            EvidenceSource.ENV_SKYRIM_PATH,
            EvidenceSource.MO2_GAME_PATH,
            EvidenceSource.MO2_EXECUTABLE,
            EvidenceSource.LIVE_GAME_PROCESS,
            EvidenceSource.LIVE_MO2_PROCESS,
        }
        assert len(EvidenceSource) > 0, "un parametrize sobre un enum vacío no genera casos"

    def test_no_existe_una_fuente_que_lea_la_managed_source(self) -> None:
        """SFR-05/SFR-19: el árbol de Steam no es una fuente de ruta efectiva."""
        nombres = {fuente.name for fuente in EvidenceSource}
        assert not any("MANAGED" in nombre or "STEAM" in nombre for nombre in nombres)

    def test_el_paquete_no_contiene_operaciones_mutantes(self) -> None:
        """El oráculo es de SOLO lectura: se congela por AST, no por comentario."""
        prohibidas_attr = {
            "write_text",
            "write_bytes",
            "mkdir",
            "unlink",
            "rmdir",
            "touch",
            "chmod",
            "rename",
            "replace",
            "symlink_to",
            "hardlink_to",
            "rmtree",
            "remove",
            "makedirs",
            "copy",
            "copy2",
            "copytree",
            "move",
        }
        archivos = sorted(PAQUETE.glob("*.py"))
        assert archivos, f"no se encontró el paquete {PAQUETE} (¿cwd equivocado?)"

        ofensas: list[str] = []
        for archivo in archivos:
            arbol = ast.parse(archivo.read_text(encoding="utf-8"))
            for nodo in ast.walk(arbol):
                if isinstance(nodo, ast.Call):
                    if isinstance(nodo.func, ast.Name) and nodo.func.id == "open":
                        ofensas.append(f"{archivo}:{nodo.lineno}: open()")
                    if isinstance(nodo.func, ast.Attribute) and nodo.func.attr in prohibidas_attr:
                        ofensas.append(f"{archivo}:{nodo.lineno}: .{nodo.func.attr}()")
        assert ofensas == [], f"el oráculo debe ser read-only; mutaciones encontradas: {ofensas}"


def _assessment_minimo(
    verdict: EffectiveRuntimeVerdict,
    *,
    compatibility: RuntimeCompatibility = RuntimeCompatibility.COMPATIBLE,
):
    """Construye un assessment con el veredicto pedido, para el contrato del tipo."""
    from sky_claw.local.frozen_runtime.effective_runtime import EffectiveRuntimeAssessment

    return EffectiveRuntimeAssessment(
        verdict=verdict,
        reasons=(ReasonCode.EFFECTIVE_MATCHES_DESIRED_CLONE,),
        effective_path=None,
        effective_source=None,
        desired_clone_id=None,
        desired_clone_root=None,
        observations=(),
        runtime_identity=None,
        compatibility=compatibility,
        compatibility_reason="test",
        observed_at_ns=1,
    )
