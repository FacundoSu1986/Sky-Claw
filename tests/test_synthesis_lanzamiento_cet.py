"""PR-3a (ADR 0019 §2.4): decisión de lanzamiento de patchers con apphost CETCompat.

Bajo usvfs 0.5.6.1 un apphost marcado CET-compatible cae con 0xC0000005. La decisión del
propietario es: lanzar directo lo que no tiene CET, lanzar vía ``dotnet.exe <nombre>.dll`` el
apphost framework-dependent con CET cuya app embebida es exactamente ``<nombre>.dll``, y
rechazar todo lo demás (fail-closed). Fase 1 además compila sin CET, no incremental, y verifica
el apphost producido.

Los binarios se sintetizan en ``tmp_path`` con ``tests/_pe_sintetico.py``: el repo no versiona
ejecutables (``tests/test_clean_room_invariant.py``).
"""

from __future__ import annotations

import pathlib

import pytest

from sky_claw.local.tools import synthesis_prebuild as sp
from sky_claw.local.tools.synthesis_prebuild import EstadoCet, ModoLanzamiento, Motivo
from tests._pe_sintetico import SIN as _SIN
from tests._pe_sintetico import patcher_sintetico as _patcher
from tests._pe_sintetico import pe_sintetico as _pe_sintetico

_APP = (b"MiPatcher.dll",)

# Caso -> (constructor del patcher, (modo, motivo) esperado). Enumera TODOS los caminos de
# lanzamiento: un Motivo nuevo sin caso acá rompe `test_cada_motivo_tiene_su_caso`.
_CASOS: dict[str, tuple[dict[str, object], tuple[ModoLanzamiento, Motivo]]] = {
    "sin_entrada_cet": ({"pe": _pe_sintetico(apps=_APP)}, (ModoLanzamiento.DIRECTO, Motivo.SIN_CET)),
    "cet_apagado": ({"pe": _pe_sintetico(cet=0, apps=_APP)}, (ModoLanzamiento.DIRECTO, Motivo.SIN_CET)),
    "otros_bits_sin_cet": ({"pe": _pe_sintetico(cet=0x2, apps=_APP)}, (ModoLanzamiento.DIRECTO, Motivo.SIN_CET)),
    "sin_cet_nativo_sin_dll": (
        {"pe": _pe_sintetico(cet=0, offsets_bundle=()), "dll": False, "runtimeconfig": _SIN},
        (ModoLanzamiento.DIRECTO, Motivo.SIN_CET),
    ),
    "cet_framework_dependent": ({}, (ModoLanzamiento.VIA_DOTNET, Motivo.CET_VIA_DOTNET)),
    "cet_frameworks_plural": (
        {"runtimeconfig": {"runtimeOptions": {"frameworks": [{"name": "Microsoft.NETCore.App", "version": "8.0.0"}]}}},
        (ModoLanzamiento.VIA_DOTNET, Motivo.CET_VIA_DOTNET),
    ),
    "no_es_pe": ({"pe": b"#!/bin/sh\necho hola\n"}, (ModoLanzamiento.RECHAZADO, Motivo.NO_ES_PE)),
    "cet_ilegible_tamano_cero": (
        {"pe": _pe_sintetico(cet=1, tam_cet=0, apps=_APP)},
        (ModoLanzamiento.RECHAZADO, Motivo.CET_ILEGIBLE),
    ),
    "cet_entrada_duplicada": (
        {"pe": _pe_sintetico(cet=1, entradas_cet=2, apps=_APP)},
        (ModoLanzamiento.RECHAZADO, Motivo.CET_ILEGIBLE),
    ),
    "cet_sin_marcador_bundle": (
        {"pe": _pe_sintetico(cet=1, offsets_bundle=(), apps=_APP)},
        (ModoLanzamiento.RECHAZADO, Motivo.NO_ES_APPHOST),
    ),
    "cet_dos_marcadores_bundle": (
        {"pe": _pe_sintetico(cet=1, offsets_bundle=(0, 0), apps=_APP)},
        (ModoLanzamiento.RECHAZADO, Motivo.NO_ES_APPHOST),
    ),
    "cet_single_file": (
        {"pe": _pe_sintetico(cet=1, offsets_bundle=(168762,), apps=_APP)},
        (ModoLanzamiento.RECHAZADO, Motivo.BUNDLE_SINGLE_FILE),
    ),
    # NTFS no distingue mayúsculas ASCII: `mipatcher.dll` y `MiPatcher.dll` son el mismo archivo.
    "cet_app_solo_difiere_en_mayusculas": (
        {"pe": _pe_sintetico(cet=1, apps=(b"mipatcher.dll",))},
        (ModoLanzamiento.VIA_DOTNET, Motivo.CET_VIA_DOTNET),
    ),
    # Hermano del caso anterior: la BÚSQUEDA de la región también pliega ASCII, no solo la
    # comparación final (sin esto `MIPATCHER.DLL` ni siquiera era candidata).
    "cet_app_extension_en_mayusculas": (
        {"pe": _pe_sintetico(cet=1, apps=(b"MIPATCHER.DLL",))},
        (ModoLanzamiento.VIA_DOTNET, Motivo.CET_VIA_DOTNET),
    ),
    # Pero el plegado no-ASCII no se adivina: "ß" no es "ss" para NTFS.
    "cet_app_plegado_no_ascii": (
        {"nombre": "Straße", "pe": _pe_sintetico(cet=1, apps=(b"Strasse.dll",))},
        (ModoLanzamiento.RECHAZADO, Motivo.APP_EMBEBIDA_DISTINTA),
    ),
    "cet_app_de_otro_nombre": (
        {"pe": _pe_sintetico(cet=1, apps=(b"Otro.dll",))},
        (ModoLanzamiento.RECHAZADO, Motivo.APP_EMBEBIDA_DISTINTA),
    ),
    "cet_app_en_subcarpeta": (
        {"pe": _pe_sintetico(cet=1, apps=(b"sub\\MiPatcher.dll",))},
        (ModoLanzamiento.RECHAZADO, Motivo.APP_EMBEBIDA_DISTINTA),
    ),
    "cet_app_ausente": ({"pe": _pe_sintetico(cet=1)}, (ModoLanzamiento.RECHAZADO, Motivo.APP_EMBEBIDA_AMBIGUA)),
    "cet_dos_apps": (
        {"pe": _pe_sintetico(cet=1, apps=(b"MiPatcher.dll", b"Otro.dll"))},
        (ModoLanzamiento.RECHAZADO, Motivo.APP_EMBEBIDA_AMBIGUA),
    ),
    "cet_dll_ausente": ({"dll": False}, (ModoLanzamiento.RECHAZADO, Motivo.DLL_AUSENTE)),
    "cet_runtimeconfig_ausente": ({"runtimeconfig": _SIN}, (ModoLanzamiento.RECHAZADO, Motivo.NO_FRAMEWORK_DEPENDENT)),
    "cet_self_contained": (
        {
            "runtimeconfig": {
                "runtimeOptions": {"includedFrameworks": [{"name": "Microsoft.NETCore.App", "version": "10.0.0"}]}
            }
        },
        (ModoLanzamiento.RECHAZADO, Motivo.NO_FRAMEWORK_DEPENDENT),
    ),
    "cet_framework_e_included": (
        {"runtimeconfig": {"runtimeOptions": {"framework": {"name": "x"}, "includedFrameworks": []}}},
        (ModoLanzamiento.RECHAZADO, Motivo.NO_FRAMEWORK_DEPENDENT),
    ),
    "cet_runtimeconfig_sin_framework": (
        {"runtimeconfig": {"runtimeOptions": {"tfm": "net10.0"}}},
        (ModoLanzamiento.RECHAZADO, Motivo.NO_FRAMEWORK_DEPENDENT),
    ),
    "cet_runtimeconfig_framework_no_objeto": (
        {"runtimeconfig": {"runtimeOptions": {"framework": "Microsoft.NETCore.App"}}},
        (ModoLanzamiento.RECHAZADO, Motivo.NO_FRAMEWORK_DEPENDENT),
    ),
    # El host de .NET exige `name` y `version` (strings) en cada referencia de framework.
    "cet_runtimeconfig_frameworks_ref_vacia": (
        {"runtimeconfig": {"runtimeOptions": {"frameworks": [{}]}}},
        (ModoLanzamiento.RECHAZADO, Motivo.NO_FRAMEWORK_DEPENDENT),
    ),
    "cet_runtimeconfig_framework_sin_version": (
        {"runtimeconfig": {"runtimeOptions": {"framework": {"name": "Microsoft.NETCore.App"}}}},
        (ModoLanzamiento.RECHAZADO, Motivo.NO_FRAMEWORK_DEPENDENT),
    ),
    "cet_runtimeconfig_framework_sin_nombre": (
        {"runtimeconfig": {"runtimeOptions": {"framework": {"version": "10.0.0"}}}},
        (ModoLanzamiento.RECHAZADO, Motivo.NO_FRAMEWORK_DEPENDENT),
    ),
    "cet_runtimeconfig_framework_version_no_string": (
        {"runtimeconfig": {"runtimeOptions": {"framework": {"name": "Microsoft.NETCore.App", "version": 10}}}},
        (ModoLanzamiento.RECHAZADO, Motivo.NO_FRAMEWORK_DEPENDENT),
    ),
    "cet_runtimeconfig_frameworks_una_ref_incompleta": (
        {
            "runtimeconfig": {
                "runtimeOptions": {
                    "frameworks": [
                        {"name": "Microsoft.NETCore.App", "version": "8.0.0"},
                        {"name": "Microsoft.WindowsDesktop.App"},
                    ]
                }
            }
        },
        (ModoLanzamiento.RECHAZADO, Motivo.NO_FRAMEWORK_DEPENDENT),
    ),
    # El host procesa AMBAS secciones si coexisten: una válida no tapa a la otra rota.
    "cet_runtimeconfig_framework_valido_y_frameworks_roto": (
        {
            "runtimeconfig": {
                "runtimeOptions": {
                    "framework": {"name": "Microsoft.NETCore.App", "version": "10.0.0"},
                    "frameworks": [{}],
                }
            }
        },
        (ModoLanzamiento.RECHAZADO, Motivo.NO_FRAMEWORK_DEPENDENT),
    ),
    "cet_runtimeconfig_framework_roto_y_frameworks_valido": (
        {
            "runtimeconfig": {
                "runtimeOptions": {
                    "framework": {"name": "Microsoft.NETCore.App"},
                    "frameworks": [{"name": "Microsoft.NETCore.App", "version": "10.0.0"}],
                }
            }
        },
        (ModoLanzamiento.RECHAZADO, Motivo.NO_FRAMEWORK_DEPENDENT),
    ),
    # Una clave PRESENTE con null no equivale a ausente: el host la procesa y la rechaza.
    "cet_runtimeconfig_framework_valido_y_frameworks_null": (
        {
            "runtimeconfig": {
                "runtimeOptions": {
                    "framework": {"name": "Microsoft.NETCore.App", "version": "10.0.0"},
                    "frameworks": None,
                }
            }
        },
        (ModoLanzamiento.RECHAZADO, Motivo.NO_FRAMEWORK_DEPENDENT),
    ),
    "cet_runtimeconfig_framework_null_y_frameworks_valido": (
        {
            "runtimeconfig": {
                "runtimeOptions": {
                    "framework": None,
                    "frameworks": [{"name": "Microsoft.NETCore.App", "version": "10.0.0"}],
                }
            }
        },
        (ModoLanzamiento.RECHAZADO, Motivo.NO_FRAMEWORK_DEPENDENT),
    ),
    "cet_runtimeconfig_ambas_secciones_validas": (
        {
            "runtimeconfig": {
                "runtimeOptions": {
                    "framework": {"name": "Microsoft.NETCore.App", "version": "10.0.0"},
                    "frameworks": [{"name": "Microsoft.WindowsDesktop.App", "version": "10.0.0"}],
                }
            }
        },
        (ModoLanzamiento.VIA_DOTNET, Motivo.CET_VIA_DOTNET),
    ),
    "cet_runtimeconfig_frameworks_vacio": (
        {"runtimeconfig": {"runtimeOptions": {"frameworks": []}}},
        (ModoLanzamiento.RECHAZADO, Motivo.NO_FRAMEWORK_DEPENDENT),
    ),
    "cet_runtimeconfig_no_objeto": (
        {"runtimeconfig": [1, 2]},
        (ModoLanzamiento.RECHAZADO, Motivo.NO_FRAMEWORK_DEPENDENT),
    ),
    "cet_runtimeconfig_json_roto": (
        {"runtimeconfig": "{no es json"},
        (ModoLanzamiento.RECHAZADO, Motivo.NO_FRAMEWORK_DEPENDENT),
    ),
}


@pytest.mark.parametrize("caso", sorted(_CASOS))
def test_tabla_de_decision_de_lanzamiento(tmp_path: pathlib.Path, caso: str) -> None:
    kwargs, esperado = _CASOS[caso]
    exe = _patcher(tmp_path / caso, **kwargs)  # type: ignore[arg-type]
    decision = sp.decidir_lanzamiento(exe)
    assert (decision.modo, decision.motivo) == esperado
    assert decision.mensaje  # todo veredicto explica por qué, también el éxito
    if decision.modo is ModoLanzamiento.VIA_DOTNET:
        assert decision.dll == exe.with_suffix(".dll")
    else:
        assert decision.dll is None


def test_cada_motivo_tiene_su_caso() -> None:
    # Ancla enumerante: un camino de lanzamiento nuevo (Motivo) sin caso en la tabla rompe acá.
    assert {motivo for _, (_, motivo) in _CASOS.values()} == set(Motivo)


def test_cada_motivo_implica_un_unico_modo() -> None:
    assert sp.MODO_POR_MOTIVO == {
        Motivo.SIN_CET: ModoLanzamiento.DIRECTO,
        Motivo.CET_VIA_DOTNET: ModoLanzamiento.VIA_DOTNET,
        Motivo.NO_ES_PE: ModoLanzamiento.RECHAZADO,
        Motivo.CET_ILEGIBLE: ModoLanzamiento.RECHAZADO,
        Motivo.NO_ES_APPHOST: ModoLanzamiento.RECHAZADO,
        Motivo.BUNDLE_SINGLE_FILE: ModoLanzamiento.RECHAZADO,
        Motivo.APP_EMBEBIDA_DISTINTA: ModoLanzamiento.RECHAZADO,
        Motivo.APP_EMBEBIDA_AMBIGUA: ModoLanzamiento.RECHAZADO,
        Motivo.DLL_AUSENTE: ModoLanzamiento.RECHAZADO,
        Motivo.NO_FRAMEWORK_DEPENDENT: ModoLanzamiento.RECHAZADO,
    }


def test_exe_inexistente_se_rechaza(tmp_path: pathlib.Path) -> None:
    decision = sp.decidir_lanzamiento(tmp_path / "NoExiste.exe")
    assert (decision.modo, decision.motivo) == (ModoLanzamiento.RECHAZADO, Motivo.NO_ES_PE)


def test_la_decision_no_modifica_el_binario(tmp_path: pathlib.Path) -> None:
    # Lanzar vía dotnet existe justamente para no tocar el PE de un tercero (pin de hash).
    exe = _patcher(tmp_path)
    antes = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    sp.decidir_lanzamiento(exe)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == antes


@pytest.mark.parametrize(
    ("pe", "estado"),
    [
        (_pe_sintetico(), EstadoCet.AUSENTE),
        (_pe_sintetico(cet=0), EstadoCet.APAGADO),
        (_pe_sintetico(cet=1), EstadoCet.ENCENDIDO),
        (_pe_sintetico(cet=0x3), EstadoCet.ENCENDIDO),
        (_pe_sintetico(cet=1, tam_cet=0), EstadoCet.ILEGIBLE),
        # Directorio de debug con un solo campo en cero: inconsistente, no "sin CET".
        (_pe_sintetico(cet=1, dir_debug=(0, 28)), EstadoCet.ILEGIBLE),
        (_pe_sintetico(cet=1, dir_debug=(0x1000, 0)), EstadoCet.ILEGIBLE),
        (_pe_sintetico(cet=1, dir_debug=(0, 0)), EstadoCet.AUSENTE),
        (b"no es un PE", EstadoCet.NO_ES_PE),
    ],
    ids=[
        "ausente",
        "apagado",
        "encendido",
        "encendido_con_otros_bits",
        "tamano_cero",
        "dir_sin_rva",
        "dir_sin_tamano",
        "dir_vacio",
        "no_pe",
    ],
)
def test_leer_estado_cet(tmp_path: pathlib.Path, pe: bytes, estado: EstadoCet) -> None:
    exe = tmp_path / "x.exe"
    exe.write_bytes(pe)
    assert sp.leer_estado_cet(exe) is estado


# --- Fase 1: compilar sin CET, no incremental, y verificar el apphost producido ---------


def test_argumentos_de_build_del_patcher() -> None:
    # Igualdad literal: quitar `--no-incremental` deja pasar el apphost viejo con CET sin aviso
    # (evidencia de rig, build B), y quitar `-p:CETCompat=false` lo vuelve a encender.
    proyecto = pathlib.Path("C:/cache/fuentes/MiPatcher/MiPatcher.csproj")
    assert sp.argumentos_dotnet_build_patcher(proyecto) == [
        "build",
        str(proyecto),
        "--no-restore",
        "--no-incremental",
        "--disable-build-servers",
        "-c",
        "Release",
        "-p:UseSharedCompilation=false",
        "-p:CETCompat=false",
    ]


@pytest.mark.parametrize("pe", [_pe_sintetico(), _pe_sintetico(cet=0)], ids=["ausente", "apagado"])
def test_verificacion_post_build_acepta_apphost_sin_cet(tmp_path: pathlib.Path, pe: bytes) -> None:
    exe = tmp_path / "x.exe"
    exe.write_bytes(pe)
    sp.verificar_apphost_sin_cet(exe)  # no levanta


@pytest.mark.parametrize(
    ("pe", "estado"),
    [
        (_pe_sintetico(cet=1), EstadoCet.ENCENDIDO),
        (_pe_sintetico(cet=1, tam_cet=0), EstadoCet.ILEGIBLE),
        (b"no es un PE", EstadoCet.NO_ES_PE),
    ],
    ids=["encendido", "ilegible", "no_pe"],
)
def test_verificacion_post_build_falla_cerrado(tmp_path: pathlib.Path, pe: bytes, estado: EstadoCet) -> None:
    exe = tmp_path / "x.exe"
    exe.write_bytes(pe)
    with pytest.raises(sp.CetCompatPersistenteError) as exc:
        sp.verificar_apphost_sin_cet(exe)
    assert exc.value.estado is estado
    assert str(exe) in str(exc.value)


def test_verificacion_post_build_falla_si_no_hay_apphost(tmp_path: pathlib.Path) -> None:
    # El mensaje distingue "no se generó" de "se generó con CET": son diagnósticos distintos.
    with pytest.raises(sp.CetCompatPersistenteError) as exc:
        sp.verificar_apphost_sin_cet(tmp_path / "NoSeGenero.exe")
    assert "no existe" in str(exc.value)
    assert "no quedó sin CETCompat" not in str(exc.value)


def test_argumentos_de_build_con_carpeta_de_salida() -> None:
    # PR-3d: la Fase 1 compila hacia una carpeta propia de la caché; el resto no cambia.
    proyecto = pathlib.Path("C:/cache/fuentes/MiPatcher/MiPatcher.csproj")
    salida = pathlib.Path("C:/cache/salida")
    assert sp.argumentos_dotnet_build_patcher(proyecto, salida=salida) == [
        *sp.argumentos_dotnet_build_patcher(proyecto),
        "-o",
        str(salida),
    ]


def test_argumentos_de_restore_del_patcher() -> None:
    proyecto = pathlib.Path("C:/cache/fuentes/MiPatcher/MiPatcher.csproj")
    assert sp.argumentos_dotnet_restore_patcher(proyecto) == ["restore", str(proyecto), "--disable-build-servers"]
