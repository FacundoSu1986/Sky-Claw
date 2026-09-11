"""Propiedad del `external_work_root` — P0 de ADR 0011 (binding, admisión, estado).

**Qué se ancla acá y qué NO.** P0 entrega la CAPACIDAD (configurar → validar →
vincular → demostrar propiedad → coordinar → recordar → rechazar); la ACTIVACIÓN
—que el runner pase estos subroots por ``-o:``— es PR-2 y este PR no la hace.
La frontera está anclada explícitamente por
`test_p0_no_cambia_el_output_productivo_del_runner`: si un cambio de P0 hiciera
que el argv real del runner use el root nuevo, ese test rompe.

**Por qué los tests enumeran.** La máquina de estados A–H se lista caso por caso
(`test_maquina_de_estados_enumerada`), no se muestrea: agregar un estado sin su
veredicto rompe el ancla de igualdad literal contra la tabla del ADR. Es el
mismo instrumento que `test_ritual_dispatch.py` usa para el dict de rituales, y
la razón es la de `AGENTS.md`: un caso escrito a mano para el hermano que faltó
no ataja al tercero.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

from sky_claw.app.security import known_folders
from sky_claw.local.tools import dyndolod_workspace as ws
from tests._symlink_guard import crear_junction, junction_guard, symlink_guard

# ---------------------------------------------------------------------------
# Andamiaje: una instancia lógica mínima (game + datos MO2 + mods)
# ---------------------------------------------------------------------------


def _instancia(tmp_path: pathlib.Path, sufijo: str = "") -> ws.ResourceBinding:
    game = tmp_path / f"game{sufijo}"
    datos = tmp_path / f"mo2{sufijo}"
    mods = datos / "mods"
    for d in (game, datos, mods):
        d.mkdir(parents=True, exist_ok=True)
    return ws.ResourceBinding.desde_paths(
        game_path=game,
        mo2_instance_data_root=datos,
        mo2_mods_path=mods,
    )


def _prohibidas(tmp_path: pathlib.Path) -> ws.RaicesProhibidas:
    """Raíces prohibidas derivadas de la instancia de `_instancia`."""
    return ws.RaicesProhibidas.desde_entorno(
        game=tmp_path / "game",
        mo2_install=tmp_path / "mo2_install",
        mo2_instance_data_root=tmp_path / "mo2",
        mo2_mods_path=tmp_path / "mo2" / "mods",
        dyndolod_exe=tmp_path / "tools" / "DynDOLOD" / "DynDOLODx64.exe",
        texgen_exe=tmp_path / "tools" / "TexGen" / "TexGenx64.exe",
        # `tmp_path` de pytest cuelga del TEMP real, así que el TEMP del sistema
        # rechazaría TODO candidato del test. Se inyecta uno de mentira; que el
        # default sea el TEMP real lo ancla
        # `test_el_temp_real_del_sistema_es_raiz_prohibida_por_default`.
        temp_dir=tmp_path / "temp",
        known_folders_prohibidos=(),
    )


def _registro(tmp_path: pathlib.Path) -> ws.RegistroDeRootActivo:
    return ws.RegistroDeRootActivo(tmp_path / "estado" / "dyndolod_active_roots.json")


def _escribir_binding(root: pathlib.Path, documento: dict[str, object]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ws.ARCHIVO_DE_BINDING).write_text(json.dumps(documento), encoding="utf-8")


def _documento_valido(recursos: ws.ResourceBinding, binding_id: str = "11111111-2222-3333-4444-555555555555") -> dict:
    return {
        "schema_version": ws.SCHEMA_VERSION,
        "binding_id": binding_id,
        "resource_binding": {
            "game_path": recursos.game_path,
            "mo2_instance_data_root": recursos.mo2_instance_data_root,
            "mo2_mods_path": recursos.mo2_mods_path,
        },
    }


# ---------------------------------------------------------------------------
# Schema v1 — congelado y cerrado (ADR 0011 §2.3)
# ---------------------------------------------------------------------------


def test_el_schema_v1_es_exactamente_tres_claves_y_tres_de_evidencia() -> None:
    """Igualdad literal contra el schema del ADR: sin timestamps, sin config_path."""
    assert frozenset({"schema_version", "binding_id", "resource_binding"}) == ws.CLAVES_DE_BINDING
    assert frozenset({"game_path", "mo2_instance_data_root", "mo2_mods_path"}) == ws.CLAVES_DE_RESOURCE_BINDING
    assert ws.SCHEMA_VERSION == 1
    assert ws.ARCHIVO_DE_BINDING == ".sky-claw-binding.json"


def test_un_binding_valido_hace_round_trip(tmp_path: pathlib.Path) -> None:
    recursos = _instancia(tmp_path)
    root = tmp_path / "work"
    _escribir_binding(root, _documento_valido(recursos))

    documento = ws.leer_binding(root)

    assert documento is not None
    assert documento.schema_version == 1
    assert documento.binding_id == "11111111-2222-3333-4444-555555555555"
    assert documento.resource_binding == recursos


@pytest.mark.parametrize(
    ("etiqueta", "mutar"),
    [
        ("campo extra en la raíz", lambda d: d.update({"config_path": "C:/x/config.toml"})),
        ("timestamp en la raíz", lambda d: d.update({"created_at": "2026-09-10T00:00:00Z"})),
        ("hostname en la raíz", lambda d: d.update({"hostname": "PC-FACHA"})),
        ("campo extra en resource_binding", lambda d: d["resource_binding"].update({"profile": "Default"})),
        ("falta binding_id", lambda d: d.pop("binding_id")),
        ("falta resource_binding", lambda d: d.pop("resource_binding")),
        ("falta un campo de evidencia", lambda d: d["resource_binding"].pop("mo2_mods_path")),
        ("schema_version desconocida", lambda d: d.update({"schema_version": 2})),
        ("schema_version no entera", lambda d: d.update({"schema_version": "1"})),
        ("evidencia plana en la raíz", lambda d: d.update({"game_path": d["resource_binding"]["game_path"]})),
        ("resource_binding no es objeto", lambda d: d.update({"resource_binding": "C:/x"})),
        ("binding_id no es texto", lambda d: d.update({"binding_id": 7})),
    ],
)
def test_todo_apartamiento_del_schema_v1_es_caso_e(tmp_path: pathlib.Path, etiqueta: str, mutar) -> None:
    """Política de campos extra declarada: fuera del schema ⇒ caso E fail-closed.

    Se enumeran las formas reales de apartarse (extra en raíz, extra anidado,
    faltante, versión desconocida, evidencia plana), no una muestra: la
    tolerancia se cuela por la forma que nadie escribió a mano.
    """
    recursos = _instancia(tmp_path)
    root = tmp_path / "work"
    documento = _documento_valido(recursos)
    mutar(documento)
    _escribir_binding(root, documento)

    with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
        ws.leer_binding(root)

    assert excinfo.value.motivo is ws.MotivoDeRechazo.CORRUPTO, etiqueta
    assert excinfo.value.estado is ws.EstadoDelRoot.E_SCHEMA_DESCONOCIDO


@pytest.mark.parametrize("contenido", ["", "   ", "{no es json", "[]", '"texto"', "null", "123"])
def test_metadata_corrupta_es_caso_e(tmp_path: pathlib.Path, contenido: str) -> None:
    root = tmp_path / "work"
    root.mkdir()
    (root / ws.ARCHIVO_DE_BINDING).write_text(contenido, encoding="utf-8")

    with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
        ws.leer_binding(root)

    assert excinfo.value.motivo is ws.MotivoDeRechazo.CORRUPTO


# ---------------------------------------------------------------------------
# Máquina de estados A–H (ADR 0011 §2.4) — enumerada, no muestreada
# ---------------------------------------------------------------------------


def test_la_tabla_de_veredictos_cubre_los_ocho_estados_y_solo_esos() -> None:
    """Ancla de igualdad literal: un estado nuevo sin veredicto rompe acá."""
    assert set(ws.EstadoDelRoot) == set(ws.VEREDICTO_POR_ESTADO)
    assert [e.value for e in ws.EstadoDelRoot] == ["A", "B", "C", "D", "E", "F", "G", "H"]
    assert ws.VEREDICTO_POR_ESTADO == {
        ws.EstadoDelRoot.A_AUSENTE: ws.Veredicto.INICIALIZAR,
        ws.EstadoDelRoot.B_VACIO_SIN_BINDING: ws.Veredicto.INICIALIZAR,
        ws.EstadoDelRoot.C_BINDING_COMPATIBLE: ws.Veredicto.UTILIZAR,
        ws.EstadoDelRoot.D_NO_VACIO_SIN_BINDING: ws.Veredicto.RECHAZAR,
        ws.EstadoDelRoot.E_SCHEMA_DESCONOCIDO: ws.Veredicto.RECHAZAR,
        ws.EstadoDelRoot.F_BINDING_AJENO: ws.Veredicto.RECHAZAR,
        ws.EstadoDelRoot.G_RECURSOS_CAMBIARON: ws.Veredicto.RECHAZAR,
        ws.EstadoDelRoot.H_OTRO_ROOT_ACTIVO: ws.Veredicto.RECHAZAR,
    }


def _preparar_caso(caso: str, tmp_path: pathlib.Path):
    """Monta el disco + registro para UN caso de la tabla y devuelve sus insumos."""
    recursos = _instancia(tmp_path)
    registro = _registro(tmp_path)
    root = tmp_path / "work"

    if caso == "A":
        pass  # el root simplemente no existe
    elif caso == "B":
        root.mkdir()
    elif caso == "C":
        _escribir_binding(root, _documento_valido(recursos))
    elif caso == "D":
        root.mkdir()
        (root / "generacion_anterior").mkdir()
    elif caso == "E":
        documento = _documento_valido(recursos)
        documento["created_at"] = "2026-09-10T00:00:00Z"
        _escribir_binding(root, documento)
    elif caso == "F":
        otros = _instancia(tmp_path, sufijo="_ajeno")
        _escribir_binding(root, _documento_valido(otros, binding_id="99999999-0000-0000-0000-000000000000"))
    elif caso == "G":
        # Binding PROPIO (el registro durable recuerda este binding_id) cuya
        # evidencia ya no coincide con la resolución actual: MO2 se movió.
        viejos = _instancia(tmp_path, sufijo="_viejo")
        _escribir_binding(root, _documento_valido(viejos, binding_id="abcdefab-0000-0000-0000-000000000001"))
        registro.registrar_activa(
            clave=viejos.clave(),
            root=root,
            binding_id="abcdefab-0000-0000-0000-000000000001",
        )
    elif caso == "H":
        _escribir_binding(root, _documento_valido(recursos))
        registro.registrar_activa(
            clave=recursos.clave(),
            root=tmp_path / "otro_work",
            binding_id="11111111-2222-3333-4444-555555555555",
        )
    else:  # pragma: no cover - defensivo
        raise AssertionError(f"caso desconocido: {caso}")

    return root, recursos, registro


@pytest.mark.parametrize(
    ("caso", "estado_esperado"),
    [
        ("A", ws.EstadoDelRoot.A_AUSENTE),
        ("B", ws.EstadoDelRoot.B_VACIO_SIN_BINDING),
        ("C", ws.EstadoDelRoot.C_BINDING_COMPATIBLE),
        ("D", ws.EstadoDelRoot.D_NO_VACIO_SIN_BINDING),
        ("E", ws.EstadoDelRoot.E_SCHEMA_DESCONOCIDO),
        ("F", ws.EstadoDelRoot.F_BINDING_AJENO),
        ("G", ws.EstadoDelRoot.G_RECURSOS_CAMBIARON),
        ("H", ws.EstadoDelRoot.H_OTRO_ROOT_ACTIVO),
    ],
)
def test_maquina_de_estados_enumerada(tmp_path: pathlib.Path, caso: str, estado_esperado: ws.EstadoDelRoot) -> None:
    """Los OCHO casos del ADR, uno por uno, con su disco real montado."""
    root, recursos, registro = _preparar_caso(caso, tmp_path)

    estado = ws.evaluar_estado(root, recursos=recursos, registro=registro)

    assert estado is estado_esperado


def test_caso_d_nunca_adopta_contenido_existente(tmp_path: pathlib.Path) -> None:
    """Un root con GB de generaciones anteriores no se absorbe: se rechaza."""
    root, recursos, registro = _preparar_caso("D", tmp_path)

    with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
        ws.exigir_veredicto(root, recursos=recursos, registro=registro)

    assert excinfo.value.estado is ws.EstadoDelRoot.D_NO_VACIO_SIN_BINDING
    assert (root / "generacion_anterior").is_dir(), "el rechazo no toca el contenido ajeno"


def test_caso_h_exige_transicion_explicita(tmp_path: pathlib.Path) -> None:
    """Mismo `resource_binding` + otro root activo ⇒ RECHAZAR, nunca degradar a C."""
    root, recursos, registro = _preparar_caso("H", tmp_path)

    with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
        ws.exigir_veredicto(root, recursos=recursos, registro=registro)

    assert excinfo.value.estado is ws.EstadoDelRoot.H_OTRO_ROOT_ACTIVO
    assert excinfo.value.motivo is ws.MotivoDeRechazo.TRANSICION_REQUERIDA
    assert "otro_work" in excinfo.value.accion_requerida


def test_caso_h_no_se_confunde_con_c_cuando_el_root_activo_es_el_mismo(
    tmp_path: pathlib.Path,
) -> None:
    """El registro recordando ESTE root no es colisión: es exactamente C."""
    recursos = _instancia(tmp_path)
    registro = _registro(tmp_path)
    root = tmp_path / "work"
    _escribir_binding(root, _documento_valido(recursos))
    registro.registrar_activa(clave=recursos.clave(), root=root, binding_id="11111111-2222-3333-4444-555555555555")

    assert ws.evaluar_estado(root, recursos=recursos, registro=registro) is ws.EstadoDelRoot.C_BINDING_COMPATIBLE


def test_un_root_con_solo_el_binding_no_cuenta_como_no_vacio(tmp_path: pathlib.Path) -> None:
    """El propio archivo de metadata no puede disparar el caso D."""
    recursos = _instancia(tmp_path)
    root = tmp_path / "work"
    _escribir_binding(root, _documento_valido(recursos))

    assert ws.evaluar_estado(root, recursos=recursos, registro=_registro(tmp_path)) is (
        ws.EstadoDelRoot.C_BINDING_COMPATIBLE
    )


def test_el_error_de_rechazo_nombra_lo_que_el_operador_necesita(tmp_path: pathlib.Path) -> None:
    """Observabilidad (§36): etapa, root, estado, motivo, identidad y acción."""
    root, recursos, registro = _preparar_caso("F", tmp_path)

    with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
        ws.exigir_veredicto(root, recursos=recursos, registro=registro)

    error = excinfo.value
    assert error.etapa == "dyndolod_workspace"
    assert error.root == root
    assert error.estado is ws.EstadoDelRoot.F_BINDING_AJENO
    assert error.motivo is ws.MotivoDeRechazo.AJENO
    assert error.clave_de_recursos == recursos.clave()
    assert error.accion_requerida
    assert error.razon
    # Sin secretos ni volcado: el mensaje es una línea legible.
    assert "\n" not in str(error)


# ---------------------------------------------------------------------------
# Identidad: `binding_id` ≠ `resource_binding` (ADR 0011 §2.2)
# ---------------------------------------------------------------------------


def test_el_binding_id_no_se_deriva_de_los_paths_ni_de_la_maquina(
    tmp_path: pathlib.Path,
) -> None:
    """UUID generado una vez: dos inicializaciones de roots distintos difieren."""
    recursos = _instancia(tmp_path)
    uno = ws.nuevo_binding_id()
    otro = ws.nuevo_binding_id()

    assert uno != otro
    # Y no es función de la evidencia: la misma evidencia produce ids distintos.
    assert ws.nuevo_binding_id() != ws.nuevo_binding_id()
    assert recursos.clave() == _instancia(tmp_path).clave(), "la CLAVE sí es función de la evidencia"


def test_la_clave_de_recursos_es_estable_y_no_expone_los_paths(
    tmp_path: pathlib.Path,
) -> None:
    recursos = _instancia(tmp_path)
    clave = recursos.clave()

    assert clave == recursos.clave()
    assert str(tmp_path) not in clave
    assert _instancia(tmp_path, sufijo="_otro").clave() != clave


# ---------------------------------------------------------------------------
# Admisión de rutas (ADR 0011 §2.7) — fail-closed
# ---------------------------------------------------------------------------


def test_un_root_admisible_pasa(tmp_path: pathlib.Path) -> None:
    candidato = tmp_path / "Sky-Claw Work"
    admitido = ws.admitir_root(candidato, prohibidas=_prohibidas(tmp_path))
    assert admitido == candidato.resolve()


@pytest.mark.parametrize(
    ("etiqueta", "candidato"),
    [
        ("vacío", ""),
        ("relativo", "trabajo/dyndolod"),
        ("relativo con ..", "../trabajo"),
        ("UNC con backslashes", r"\\servidor\recurso\trabajo"),
        ("UNC con barras", "//servidor/recurso/trabajo"),
        ("raíz del volumen", os.path.abspath(os.sep)),
    ],
)
def test_admision_rechaza_rutas_estructuralmente_invalidas(
    tmp_path: pathlib.Path, etiqueta: str, candidato: str
) -> None:
    with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
        ws.admitir_root(candidato, prohibidas=_prohibidas(tmp_path))
    assert excinfo.value.motivo is ws.MotivoDeRechazo.INVALIDO, etiqueta


@pytest.mark.parametrize(
    "etiqueta",
    [
        "game",
        "game_data",
        "mo2_install",
        "mo2_instance_data_root",
        "mo2_mods",
        "mo2_profiles",
        "mo2_overwrite",
        "dyndolod_install",
        "texgen_install",
        "temp",
    ],
)
def test_admision_rechaza_el_solapamiento_en_ambas_direcciones(tmp_path: pathlib.Path, etiqueta: str) -> None:
    """`candidato dentro de X` y `X dentro de candidato` son ambos rechazo.

    La dirección que se olvida es siempre la segunda: un root gigante que
    CONTIENE al juego autorizaría media unidad como sandbox administrado.
    """
    prohibidas = _prohibidas(tmp_path)
    raiz = dict(prohibidas.entradas)[etiqueta]

    dentro = pathlib.Path(raiz) / "trabajo"
    with pytest.raises(ws.WorkspaceRechazadoError) as dentro_exc:
        ws.admitir_root(dentro, prohibidas=prohibidas)
    assert dentro_exc.value.motivo is ws.MotivoDeRechazo.INVALIDO
    assert etiqueta in dentro_exc.value.razon

    contiene = pathlib.Path(raiz).parent
    with pytest.raises(ws.WorkspaceRechazadoError) as contiene_exc:
        ws.admitir_root(contiene, prohibidas=prohibidas)
    assert contiene_exc.value.motivo is ws.MotivoDeRechazo.INVALIDO
    assert etiqueta in contiene_exc.value.razon

    # Y el propio directorio, que es el caso degenerado de las dos direcciones.
    with pytest.raises(ws.WorkspaceRechazadoError):
        ws.admitir_root(raiz, prohibidas=prohibidas)


def test_admision_admite_otro_volumen_local(tmp_path: pathlib.Path) -> None:
    """ "Externo al juego" no significa "en el mismo disco": otro volumen es válido."""
    otro_volumen = tmp_path / "volumen_E" / "Sky-Claw Work"
    admitido = ws.admitir_root(otro_volumen, prohibidas=_prohibidas(tmp_path))
    assert admitido == otro_volumen.resolve()


#: Rutas inertes para las Known Folders que un test NO está ejerciendo. Existen
#: porque el conjunto v1 es CERRADO y la admisión falla cerrado sobre lo que no
#: se pudo resolver: dejar dos carpetas sin contestar hacía que el veredicto
#: dependiera de la PLATAFORMA que corre el test (en Linux no hay Known Folders
#: y no falta nada; en Windows faltan dos y el rechazo cambia de razón). El seam
#: existe justamente para que el contrato se ejerza igual en todos lados.
_RUTAS_INERTES_DE_KNOWN_FOLDERS: dict[str, str] = {
    "Documents": r"Q:\inerte\Documents",
    "Desktop": r"Q:\inerte\Desktop",
    "Downloads": r"Q:\inerte\Downloads",
}


def _resolver_todas_las_known_folders(carpeta: str, ruta_efectiva: str):
    """Resuelve el conjunto CERRADO completo: *carpeta* en su ruta, el resto inerte."""
    rutas = dict(_RUTAS_INERTES_DE_KNOWN_FOLDERS)
    rutas[carpeta] = ruta_efectiva
    por_guid = {known_folders.IDENTIFICADORES[nombre]: ruta for nombre, ruta in rutas.items()}
    return por_guid.get


@pytest.mark.parametrize(
    ("carpeta", "ruta_efectiva"),
    [
        ("Documents", r"C:\Users\facha\Documents"),
        ("Desktop", r"C:\Users\facha\Desktop"),
        ("Downloads", r"C:\Users\facha\Downloads"),
        # Redirigida a OTRO volumen y con otro nombre: el caso que `%USERPROFILE%`
        # y la búsqueda por substring dejan pasar.
        ("Documents", r"D:\Users\facha\Docs"),
        ("Desktop", r"E:\Escritorio Sincronizado"),
    ],
)
def test_admision_rechaza_los_known_folders_por_identificador(
    tmp_path: pathlib.Path, carpeta: str, ruta_efectiva: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cada Known Folder contractual hace rechazar el root, redirigida incluida."""
    monkeypatch.setattr(known_folders, "_resolver_por_api", _resolver_todas_las_known_folders(carpeta, ruta_efectiva))
    prohibidas = ws.RaicesProhibidas.desde_entorno(
        game=tmp_path / "game",
        mo2_install=None,
        mo2_instance_data_root=None,
        mo2_mods_path=None,
        dyndolod_exe=None,
        texgen_exe=None,
        temp_dir=tmp_path / "temp",
    )

    candidato = pathlib.PureWindowsPath(ruta_efectiva) / "Sky-Claw Work"
    with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
        ws.admitir_root(candidato, prohibidas=prohibidas)

    assert excinfo.value.motivo is ws.MotivoDeRechazo.INVALIDO
    assert carpeta in excinfo.value.razon


def test_una_carpeta_con_nombre_parecido_no_es_una_known_folder(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin búsqueda de substrings: `Mis Documentos de Trabajo` no es `Documents`."""
    monkeypatch.setattr(
        known_folders,
        "_resolver_por_api",
        _resolver_todas_las_known_folders("Documents", r"C:\Users\facha\Documents"),
    )
    prohibidas = ws.RaicesProhibidas.desde_entorno(
        game=tmp_path / "game",
        mo2_install=None,
        mo2_instance_data_root=None,
        mo2_mods_path=None,
        dyndolod_exe=None,
        texgen_exe=None,
        temp_dir=tmp_path / "temp",
    )

    admitido = ws.admitir_root(pathlib.PureWindowsPath(r"E:\Mis Documentos de Trabajo"), prohibidas=prohibidas)
    assert admitido == pathlib.PureWindowsPath(r"E:\Mis Documentos de Trabajo")


@pytest.mark.parametrize("hay_known_folders", [True, False])
def test_el_veredicto_de_admision_no_depende_de_la_plataforma(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, hay_known_folders: bool
) -> None:
    """Con el conjunto CERRADO resuelto entero, el veredicto es el mismo en todos lados.

    RED contra un fallo que la suite de Linux no podía ver: los fixtures de
    Known Folders resolvían UNA carpeta y dejaban las otras dos sin contestar.
    En Linux eso no falta nada (no existen); en Windows son dos indeterminadas y
    el rechazo cambiaba de razón. El seam existe para que el contrato se ejerza
    igual en las dos plataformas — un test que sólo pasa en una no ancla nada.
    """
    monkeypatch.setattr(known_folders, "plataforma_resuelve_known_folders", lambda: hay_known_folders)
    monkeypatch.setattr(
        known_folders,
        "_resolver_por_api",
        _resolver_todas_las_known_folders("Documents", r"C:\Users\facha\Documents"),
    )
    prohibidas = ws.RaicesProhibidas.desde_entorno(
        game=tmp_path / "game",
        mo2_install=None,
        mo2_instance_data_root=None,
        mo2_mods_path=None,
        dyndolod_exe=None,
        texgen_exe=None,
        temp_dir=tmp_path / "temp",
    )

    assert prohibidas.indeterminadas == ()
    with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
        ws.admitir_root(pathlib.PureWindowsPath(r"C:\Users\facha\Documents\Sky-Claw Work"), prohibidas=prohibidas)
    assert "Documents" in excinfo.value.razon


def test_el_solapamiento_demostrado_se_nombra_antes_que_la_evidencia_que_falta(
    tmp_path: pathlib.Path,
) -> None:
    """Los dos rechazos son fail-closed; el ORDEN decide qué lee el operador.

    Un root que demostrablemente ES `Documents` tiene que rechazarse diciendo
    con QUÉ solapa, no "no se pudo resolver Desktop": el segundo mensaje no
    nombra el problema real ni la acción que lo arregla, y era lo que salía
    cuando la API resolvía unas carpetas sí y otras no — el caso REAL en Windows,
    no un montaje.
    """
    prohibidas = ws.RaicesProhibidas(
        entradas=(("Documents", pathlib.PureWindowsPath(r"C:\Users\facha\Documents")),),
        indeterminadas=("Desktop", "Downloads"),
    )

    with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
        ws.admitir_root(pathlib.PureWindowsPath(r"C:\Users\facha\Documents\Sky-Claw Work"), prohibidas=prohibidas)

    assert "solapa" in excinfo.value.razon
    assert "Documents" in excinfo.value.razon
    assert "no se pudo resolver" not in excinfo.value.razon

    # Y sin solapamiento demostrado, la evidencia que falta SIGUE rechazando:
    # la precedencia reordena los mensajes, no relaja el fail-closed.
    with pytest.raises(ws.WorkspaceRechazadoError) as sin_solape:
        ws.admitir_root(pathlib.PureWindowsPath(r"E:\Trabajo\Sky-Claw"), prohibidas=prohibidas)
    assert "no se pudo resolver" in sin_solape.value.razon


@pytest.mark.parametrize(
    ("etiqueta", "candidato", "prohibida"),
    [
        (
            "el candidato viene con prefijo (lo que devuelve resolve())",
            r"\\?\C:\Users\facha\Documents\Sky-Claw Work",
            r"C:\Users\facha\Documents",
        ),
        (
            "la prohibida viene con prefijo",
            r"C:\Users\facha\Documents\Sky-Claw Work",
            r"\\?\C:\Users\facha\Documents",
        ),
        (
            "la variante UNC del prefijo",
            r"\\?\UNC\servidor\share\work",
            r"\\servidor\share",
        ),
    ],
)
def test_el_prefijo_extendido_de_windows_no_esconde_un_solapamiento(
    etiqueta: str, candidato: str, prohibida: str
) -> None:
    """``\\\\?\\C:\\x`` y ``C:\\x`` son el MISMO directorio: tienen que solapar.

    Es el único deletreo de más de esta comparación que produce un fail-OPEN, no
    un fail-closed: `Path.resolve()` puede devolver la forma con prefijo —rutas
    largas, ciertos volúmenes— mientras la API de Known Folders devuelve la
    forma plana. Con las anclas distintas (`\\\\?\\C:\\` vs `C:\\`) el solapamiento
    no se detectaba y un root dentro de `Documents` quedaba ADMITIDO.

    Se ejerce con `PureWindowsPath` literales, sin `resolve()`: así el contrato
    se verifica igual en Linux y en Windows, en vez de depender de conseguir un
    path largo real en el runner.
    """
    assert ws._solapan(pathlib.PureWindowsPath(candidato), pathlib.PureWindowsPath(prohibida)), etiqueta


def test_normalizar_el_prefijo_no_hace_solapar_rutas_distintas() -> None:
    """La normalización quita una anotación del kernel, no afloja la comparación."""
    assert not ws._solapan(
        pathlib.PureWindowsPath(r"\\?\C:\Trabajo\Sky-Claw"),
        pathlib.PureWindowsPath(r"C:\Users\facha\Documents"),
    )
    # Y el vecino de nombre parecido sigue sin serlo, con prefijo o sin él.
    assert not ws._solapan(
        pathlib.PureWindowsPath(r"\\?\E:\Mis Documentos de Trabajo"),
        pathlib.PureWindowsPath(r"C:\Users\facha\Documents"),
    )


def test_admitir_rechaza_cuando_la_canonicalizacion_agrega_el_prefijo(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r"""El hilo completo, por donde el agujero existe de verdad.

    El usuario NO escribe el prefijo: escribe `C:\Users\facha\Documents\...`.
    Un `\\?\...` tipeado a mano lo frena antes el chequeo sintáctico de UNC —un
    falso positivo fail-CLOSED, inofensivo—, así que ése no es el escenario. El
    prefijo lo introduce `resolve()` DENTRO de `admitir_root`, y recién ahí la
    comparación contra la Known Folder plana fallaba y admitía el root.

    Se sustituye `_canonicalizar` por lo que Windows puede devolver: es la forma
    honesta de montar el escenario desde Linux, en vez de fingir que el usuario
    tipeó algo que no tipea.
    """
    monkeypatch.setattr(
        known_folders,
        "_resolver_por_api",
        _resolver_todas_las_known_folders("Documents", r"C:\Users\facha\Documents"),
    )
    prefijado = pathlib.PureWindowsPath(r"\\?\C:\Users\facha\Documents\Sky-Claw Work")
    monkeypatch.setattr(ws, "_canonicalizar", lambda ruta: prefijado if "Sky-Claw Work" in str(ruta) else ruta)
    prohibidas = ws.RaicesProhibidas.desde_entorno(
        game=tmp_path / "game",
        mo2_install=None,
        mo2_instance_data_root=None,
        mo2_mods_path=None,
        dyndolod_exe=None,
        texgen_exe=None,
        temp_dir=tmp_path / "temp",
    )

    with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
        ws.admitir_root(pathlib.PureWindowsPath(r"C:\Users\facha\Documents\Sky-Claw Work"), prohibidas=prohibidas)

    assert excinfo.value.motivo is ws.MotivoDeRechazo.INVALIDO
    assert "Documents" in excinfo.value.razon, "el prefijo que agregó resolve() escondió el solapamiento"


def test_steamapps_entra_por_igualdad_de_componente_no_por_substring(
    tmp_path: pathlib.Path,
) -> None:
    """La biblioteca Steam que contiene el juego es raíz prohibida."""
    game = tmp_path / "Steam" / "steamapps" / "common" / "Skyrim Special Edition"
    game.mkdir(parents=True)
    prohibidas = ws.RaicesProhibidas.desde_entorno(
        game=game,
        mo2_install=None,
        mo2_instance_data_root=None,
        mo2_mods_path=None,
        dyndolod_exe=None,
        texgen_exe=None,
        temp_dir=tmp_path / "temp",
        known_folders_prohibidos=(),
    )

    assert "steamapps" in dict(prohibidas.entradas)
    with pytest.raises(ws.WorkspaceRechazadoError):
        ws.admitir_root(tmp_path / "Steam" / "steamapps" / "Trabajo", prohibidas=prohibidas)
    # Un directorio que sólo CONTIENE la palabra no es la biblioteca.
    ws.admitir_root(tmp_path / "steamapps_viejos_backup", prohibidas=prohibidas)


# ---------------------------------------------------------------------------
# Enlaces: ninguna mutación administrada escapa del root (ADR 0011 §2.7)
# ---------------------------------------------------------------------------


def test_un_destino_administrado_normal_se_valida(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    destino = ws.validar_destino_administrado(root, root / "DynDOLOD" / "TexGen")
    assert destino == (root / "DynDOLOD" / "TexGen").resolve()


def test_un_destino_fuera_del_root_se_rechaza(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    with pytest.raises(ws.WorkspaceRechazadoError):
        ws.validar_destino_administrado(root, tmp_path / "otro" / "cosa")


@symlink_guard
def test_un_symlink_que_escapa_del_root_se_rechaza(tmp_path: pathlib.Path) -> None:
    """El componente redirigido es el vector: se mira el ENLACE, no sólo el final."""
    root = tmp_path / "work"
    root.mkdir()
    afuera = tmp_path / "afuera"
    afuera.mkdir()
    (root / "DynDOLOD").symlink_to(afuera, target_is_directory=True)

    with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
        ws.validar_destino_administrado(root, root / "DynDOLOD" / "TexGen")

    assert excinfo.value.motivo is ws.MotivoDeRechazo.INVALIDO
    assert "enlace" in excinfo.value.razon.lower()


@symlink_guard
def test_un_symlink_que_no_escapa_no_se_sigue_igual(tmp_path: pathlib.Path) -> None:
    """Fail-closed: un enlace interno tampoco es un destino administrado válido.

    `DirectoryRollback` mueve directorios con `rename`; un componente enlazado
    hace que "mover el directorio" y "mover el enlace" sean operaciones
    distintas con resultados distintos. No se admite ninguno de los dos casos,
    y el mensaje lo dice — no se degrada a warning.
    """
    root = tmp_path / "work"
    (root / "real").mkdir(parents=True)
    (root / "DynDOLOD").symlink_to(root / "real", target_is_directory=True)

    with pytest.raises(ws.WorkspaceRechazadoError):
        ws.validar_destino_administrado(root, root / "DynDOLOD" / "TexGen")


@symlink_guard
@junction_guard
def test_un_junction_que_escapa_del_root_se_rechaza(tmp_path: pathlib.Path) -> None:
    """El hermano que importa: un JUNCTION, no un symlink.

    `os.path.islink()` devuelve **False** para un junction, así que una defensa
    escrita contra symlinks lo deja pasar entero. Por eso la contención se apoya
    en `links.is_link` —que mira el `st_reparse_tag`— y por eso este test crea un
    junction de verdad con `mklink /J` en vez de un symlink disfrazado. Sólo
    puede correr en Windows, que es la única plataforma donde existe el modo de
    falla.
    """
    root = tmp_path / "work"
    root.mkdir()
    afuera = tmp_path / "afuera"
    afuera.mkdir()
    motivo = crear_junction(root / "DynDOLOD", afuera)
    assert motivo is None, f"el helper de junction falló: {motivo}"

    with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
        ws.validar_destino_administrado(root, root / "DynDOLOD" / "TexGen")

    assert excinfo.value.motivo is ws.MotivoDeRechazo.INVALIDO
    assert "enlace" in excinfo.value.razon.lower()


@symlink_guard
def test_un_root_que_es_un_enlace_se_canonicaliza_antes_de_admitir(
    tmp_path: pathlib.Path,
) -> None:
    """El root se compara ya resuelto: un enlace al juego no evade el solapamiento."""
    prohibidas = _prohibidas(tmp_path)
    (tmp_path / "game").mkdir(exist_ok=True)
    disfraz = tmp_path / "parece_inocente"
    disfraz.symlink_to(tmp_path / "game", target_is_directory=True)

    with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
        ws.admitir_root(disfraz, prohibidas=prohibidas)

    assert "game" in excinfo.value.razon


@pytest.mark.parametrize(
    ("tipo", "esperado"),
    [
        (ws._DRIVE_REMOTE, "recurso de red mapeado"),
        (ws._DRIVE_UNKNOWN, "no se pudo determinar"),
        (None, "no se pudo determinar"),
        (3, None),  # DRIVE_FIXED: un disco local de verdad
    ],
)
def test_el_tipo_de_unidad_decide_si_la_ruta_es_local(
    tipo: int | None, esperado: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Z:\\` mapeado a un share es indistinguible de un disco local MIRANDO EL TEXTO.

    El chequeo sintáctico de UNC (``\\\\servidor\\share``) no lo ve, y es la forma
    en que la gente monta un NAS de verdad. La identidad del volumen la contesta
    el sistema, no la sintaxis — y lo que no se pudo clasificar se rechaza, que
    es el mismo fail-closed que el conjunto incompleto de Known Folders.
    """
    consultadas: list[str] = []

    def _falso(raiz: str) -> int | None:
        consultadas.append(raiz)
        return tipo

    monkeypatch.setattr(ws, "_hay_unidades_mapeadas", lambda: True)
    monkeypatch.setattr(ws, "_tipo_de_unidad", _falso)

    razon = ws._unidad_de_red(pathlib.PureWindowsPath(r"Z:\work\dyndolod"))

    assert consultadas == ["Z:\\"], "se pregunta por el VOLUMEN, no por la ruta completa"
    if esperado is None:
        assert razon is None
    else:
        assert razon is not None and esperado in razon


def test_fuera_de_windows_no_se_inventa_un_veredicto_de_unidad(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin unidades mapeadas no falta evidencia: no existe el concepto.

    Tratar el ``None`` de la plataforma como "indeterminado" haría que el
    fail-closed rechazara TODO root en Linux — fail-closed convertido en
    fail-siempre.
    """
    monkeypatch.setattr(ws, "_hay_unidades_mapeadas", lambda: False)
    monkeypatch.setattr(ws, "_tipo_de_unidad", lambda raiz: pytest.fail("no se consulta el tipo"))

    assert ws._unidad_de_red(pathlib.PureWindowsPath(r"Z:\work")) is None


def test_el_prefijo_extendido_se_quita_antes_de_preguntar_el_tipo_de_unidad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hermano del fix de solapamiento: `GetDriveTypeW` recibe la raíz SIN `\\\\?\\`.

    RED contra el gemelo del defecto que arregló `8dd6f5f`: `resolve()` puede
    devolver `\\\\?\\C:\\...` en rutas largas, y el ancla resultante (`\\\\?\\C:\\`) no
    es una raíz de volumen que `GetDriveTypeW` reconozca — un root local largo y
    legítimo se clasificaría como indeterminado y se rechazaría. Se ejerce con
    `PureWindowsPath` literal, sin `resolve()`, para que valga igual en Linux y
    Windows.
    """
    vistas: list[str] = []

    def _falso(raiz: str) -> int:
        vistas.append(raiz)
        return 3  # DRIVE_FIXED: un disco local de verdad

    monkeypatch.setattr(ws, "_hay_unidades_mapeadas", lambda: True)
    monkeypatch.setattr(ws, "_tipo_de_unidad", _falso)

    razon = ws._unidad_de_red(pathlib.PureWindowsPath(r"\\?\C:\Users\facha\ruta larguísima\Sky-Claw Work"))

    assert vistas == ["C:\\"], "se preguntó por el ancla CON prefijo extendido, que GetDriveTypeW no reconoce"
    assert razon is None, "un volumen fijo local no es una unidad de red"


def test_admitir_rechaza_un_root_en_una_unidad_de_red(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """La admisión integra el veredicto de volumen, no sólo el prefijo UNC."""
    monkeypatch.setattr(ws, "_unidad_de_red", lambda canonico: "la unidad Z:\\ es un recurso de red mapeado")

    with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
        ws.admitir_root(tmp_path / "work", prohibidas=_prohibidas(tmp_path))

    assert excinfo.value.motivo is ws.MotivoDeRechazo.INVALIDO
    assert "red" in excinfo.value.razon
    assert "LOCAL" in excinfo.value.accion_requerida


def test_un_conjunto_de_prohibiciones_incompleto_rechaza_el_root(tmp_path: pathlib.Path) -> None:
    """Fail-closed sobre la EVIDENCIA que falta, no sólo sobre la que hay.

    Si la API de Known Folders no contestó, este root podría SER `Documents` y
    el solapamiento diría que no. Admitir contra un conjunto incompleto es el
    falso negativo exacto que la identidad por identificador existe para evitar,
    reintroducido por la puerta de atrás.
    """
    prohibidas = dataclasses.replace(_prohibidas(tmp_path), indeterminadas=("Documents",))

    with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
        ws.admitir_root(tmp_path / "work", prohibidas=prohibidas)

    assert excinfo.value.motivo is ws.MotivoDeRechazo.INVALIDO
    assert "Documents" in excinfo.value.razon
    assert "incompleto" in excinfo.value.razon


def test_la_api_de_known_folders_alimenta_las_indeterminadas_de_la_admision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El hilo completo: API que no contesta → `indeterminadas` → rechazo.

    Sin este test las dos mitades podían quedar correctas por separado y
    desconectadas — el módulo de identidad reportando la ausencia y la admisión
    sin mirarla nunca.
    """
    from sky_claw.app.security import known_folders as kf

    monkeypatch.setattr(kf, "plataforma_resuelve_known_folders", lambda: True)
    monkeypatch.setattr(kf, "_resolver_por_api", lambda guid: None)

    prohibidas = ws.RaicesProhibidas.desde_entorno(
        game=None,
        mo2_install=None,
        mo2_instance_data_root=None,
        mo2_mods_path=None,
        dyndolod_exe=None,
        texgen_exe=None,
        temp_dir=pathlib.PurePath("/no/existe/temp"),
    )

    assert prohibidas.indeterminadas == ("Documents", "Desktop", "Downloads")


def test_una_secuencia_explicita_de_known_folders_no_arrastra_indeterminadas(
    tmp_path: pathlib.Path,
) -> None:
    """Pasar la secuencia afirma que el caller ya resolvió el conjunto."""
    assert _prohibidas(tmp_path).indeterminadas == ()


# ---------------------------------------------------------------------------
# Single-winner: creación exclusiva NO reemplazante (ADR 0011 §2.3)
# ---------------------------------------------------------------------------


def test_publicar_binding_crea_el_archivo_cuando_no_existe(tmp_path: pathlib.Path) -> None:
    recursos = _instancia(tmp_path)
    root = tmp_path / "work"
    root.mkdir()

    documento, gano = ws.publicar_binding(root, recursos)

    assert gano is True
    assert (root / ws.ARCHIVO_DE_BINDING).exists()
    assert ws.leer_binding(root) == documento


def test_el_perdedor_no_reemplaza_el_binding_del_ganador(tmp_path: pathlib.Path) -> None:
    recursos = _instancia(tmp_path)
    root = tmp_path / "work"
    root.mkdir()

    ganador, _ = ws.publicar_binding(root, recursos)
    perdedor, gano = ws.publicar_binding(root, recursos)

    assert gano is False
    assert perdedor.binding_id == ganador.binding_id, "el perdedor releyó, no reescribió"
    assert ws.leer_binding(root).binding_id == ganador.binding_id


def test_el_perdedor_con_recursos_incompatibles_falla_cerrado(tmp_path: pathlib.Path) -> None:
    recursos = _instancia(tmp_path)
    ajenos = _instancia(tmp_path, sufijo="_ajeno")
    root = tmp_path / "work"
    root.mkdir()
    ws.publicar_binding(root, recursos)

    with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
        ws.publicar_binding(root, ajenos)

    assert excinfo.value.motivo is ws.MotivoDeRechazo.AJENO
    assert ws.leer_binding(root).resource_binding == recursos, "el binding ajeno quedó intacto"


def test_publicar_no_usa_os_replace_para_crear() -> None:
    """Ancla de mecanismo: `os.replace` NO puede ser la primitiva de creación.

    `os.replace` sustituye un archivo publicado concurrentemente por otro
    proceso — exactamente lo que el ADR prohíbe. La creación tiene que usar
    apertura exclusiva (`O_EXCL`). Se ancla por fuente porque ningún test de
    comportamiento distingue "ganó por O_EXCL" de "ganó por suerte".
    """
    import ast

    fuente = pathlib.Path(ws.__file__).read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    creador = next(
        n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef) and n.name == "_crear_binding_exclusivo"
    )
    llamadas = {n.func.attr for n in ast.walk(creador) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "replace" not in llamadas, "la creación inicial no puede reemplazar nada"
    assert "open" in llamadas
    assert "O_EXCL" in fuente


_GUION_SINGLE_WINNER = """\
import json, pathlib, sys, time
sys.path.insert(0, {raiz!r})
from sky_claw.local.tools import dyndolod_workspace as ws

root = pathlib.Path(sys.argv[1])
listo = pathlib.Path(sys.argv[2])
salida = pathlib.Path(sys.argv[3])
recursos = ws.ResourceBinding(
    game_path=sys.argv[4], mo2_instance_data_root=sys.argv[5], mo2_mods_path=sys.argv[6]
)

listo.write_text("listo", encoding="utf-8")
# Barrera de arranque: los dos procesos entran a la sección crítica a la vez.
# Se cuentan los marcadores: `all(...)` sobre una lista de UNO devuelve True, así
# que la versión anterior dejaba pasar al primero sin esperar al segundo — una
# barrera que no ataja es peor que ninguna, porque el test dice que sincronizó.
while len(list(listo.parent.glob("listo_*"))) < 2:
    time.sleep(0.005)
while time.time() < float(sys.argv[7]):
    time.sleep(0.001)

try:
    documento, gano = ws.publicar_binding(root, recursos)
    salida.write_text(
        json.dumps({{"gano": gano, "binding_id": documento.binding_id}}), encoding="utf-8"
    )
except Exception as exc:  # noqa: BLE001 - el guión reporta, el test juzga
    salida.write_text(json.dumps({{"error": type(exc).__name__}}), encoding="utf-8")
"""


def test_single_winner_entre_dos_procesos_reales(tmp_path: pathlib.Path) -> None:
    """T-PR2-22: DOS PROCESOS, no dos coroutines.

    Dos corrutinas del mismo intérprete comparten el GIL y el estado del módulo:
    verían exclusión aunque la primitiva fuera un `threading.Lock`. La propiedad
    que hay que demostrar es cross-process, así que el test lanza dos
    intérpretes de verdad contra el MISMO root vacío, sincronizados por una
    barrera de archivo + un instante de arranque común.
    """
    recursos = _instancia(tmp_path)
    root = tmp_path / "work"
    root.mkdir()
    barrera = tmp_path / "barrera"
    barrera.mkdir()
    raiz_repo = str(pathlib.Path(ws.__file__).resolve().parents[3])

    guion = tmp_path / "publicar.py"
    guion.write_text(_GUION_SINGLE_WINNER.format(raiz=raiz_repo), encoding="utf-8")

    import time

    arranque = time.time() + 1.5
    procesos = []
    salidas = []
    for indice in (1, 2):
        salida = tmp_path / f"salida_{indice}.json"
        salidas.append(salida)
        procesos.append(
            subprocess.Popen(  # noqa: S603 - argv fijo, sin shell
                [
                    sys.executable,
                    str(guion),
                    str(root),
                    str(barrera / f"listo_{indice}"),
                    str(salida),
                    recursos.game_path,
                    recursos.mo2_instance_data_root,
                    recursos.mo2_mods_path,
                    str(arranque),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )

    for proceso in procesos:
        _, err = proceso.communicate(timeout=120)
        assert proceso.returncode == 0, err.decode(errors="replace")

    resultados = [json.loads(s.read_text(encoding="utf-8")) for s in salidas]
    assert all("error" not in r for r in resultados), resultados

    ganadores = [r for r in resultados if r["gano"]]
    perdedores = [r for r in resultados if not r["gano"]]
    assert len(ganadores) == 1, f"exactamente uno publica: {resultados}"
    assert len(perdedores) == 1

    # UN solo archivo, UN solo binding_id, y el perdedor releyó el del ganador.
    archivos = [p for p in root.iterdir() if p.name.startswith(".sky-claw-binding")]
    assert len(archivos) == 1
    publicado = ws.leer_binding(root)
    assert publicado.binding_id == ganadores[0]["binding_id"]
    assert perdedores[0]["binding_id"] == ganadores[0]["binding_id"]
    assert publicado.resource_binding == recursos


@pytest.mark.parametrize(
    ("etiqueta", "romper"),
    [
        ("no se puede crear el directorio", "mkdir"),
        ("no se puede escribir el binding", "open"),
    ],
)
def test_un_root_que_no_se_puede_inicializar_se_rechaza_con_accion(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, etiqueta: str, romper: str
) -> None:
    """Un `OSError` de disco es un RECHAZO legible, no una "falla inesperada".

    Volumen desconectado, root de sólo lectura o sin espacio: el veredicto final
    es el mismo (no se usa ese root), pero el `OSError` crudo llegaba al boundary
    del arranque como incidente con stack, sin nombrar el campo ni la acción. El
    módulo contesta SIEMPRE con `WorkspaceRechazadoError`; ésta era la grieta.
    """
    recursos = _instancia(tmp_path)
    root = tmp_path / "work"

    def _estalla(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    if romper == "mkdir":
        monkeypatch.setattr(pathlib.Path, "mkdir", _estalla)
    else:
        root.mkdir()
        monkeypatch.setattr(ws.os, "open", _estalla)

    with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
        ws.publicar_binding(root, recursos)

    assert excinfo.value.motivo is ws.MotivoDeRechazo.INVALIDO
    assert "no se pudo inicializar" in excinfo.value.razon
    assert "permiso de escritura" in excinfo.value.accion_requerida
    assert isinstance(excinfo.value.__cause__, OSError), "se preserva la causa técnica"


def test_actualizar_un_binding_propio_si_usa_reemplazo_atomico(tmp_path: pathlib.Path) -> None:
    """`os.replace` es legítimo para reescribir el binding PROPIO (temporal + replace)."""
    recursos = _instancia(tmp_path)
    root = tmp_path / "work"
    root.mkdir()
    documento, _ = ws.publicar_binding(root, recursos)

    ws.reescribir_binding_propio(root, documento)

    assert ws.leer_binding(root) == documento
    assert [p.name for p in root.iterdir()] == [ws.ARCHIVO_DE_BINDING], "sin temporales huérfanos"


def test_reescribir_un_binding_ajeno_esta_prohibido(tmp_path: pathlib.Path) -> None:
    recursos = _instancia(tmp_path)
    ajenos = _instancia(tmp_path, sufijo="_ajeno")
    root = tmp_path / "work"
    root.mkdir()
    ws.publicar_binding(root, recursos)

    ajeno = ws.BindingDocument(
        schema_version=ws.SCHEMA_VERSION,
        binding_id="00000000-0000-0000-0000-0000000000ff",
        resource_binding=ajenos,
    )
    with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
        ws.reescribir_binding_propio(root, ajeno)

    assert excinfo.value.motivo is ws.MotivoDeRechazo.AJENO
    assert ws.leer_binding(root).resource_binding == recursos


def test_la_canonicalizacion_coincide_con_la_del_resolver(tmp_path: pathlib.Path) -> None:
    """Ancla de hermanos: dos canonicalizaciones distintas volverían C en F.

    La evidencia del binding se compara contra la resolución que hace
    `path_resolver` en el arranque. Si las dos primitivas divergieran —una
    siguiendo enlaces y la otra no, una colapsando `..` y la otra no— un root
    perfectamente válido pasaría a "binding ajeno" sin que nadie tocara un path.
    """
    from sky_claw.app.core import path_resolver

    candidatos = [
        tmp_path / "trabajo",
        tmp_path / "trabajo" / ".." / "otro",
        tmp_path / "no" / "existe" / "todavia",
    ]
    for candidato in candidatos:
        assert ws._canonicalizar(candidato) == path_resolver._canonicalizar(candidato)


def test_el_temp_real_del_sistema_es_raiz_prohibida_por_default() -> None:
    """Sin `temp_dir` explícito, TEMP entra: el ADR lo prohíbe por descartable."""
    import tempfile as _tempfile

    prohibidas = ws.RaicesProhibidas.desde_entorno(
        game=None,
        mo2_install=None,
        mo2_instance_data_root=None,
        mo2_mods_path=None,
        dyndolod_exe=None,
        texgen_exe=None,
        known_folders_prohibidos=(),
    )

    assert dict(prohibidas.entradas)["temp"] == pathlib.Path(_tempfile.gettempdir()).resolve()
    with pytest.raises(ws.WorkspaceRechazadoError):
        ws.admitir_root(pathlib.Path(_tempfile.gettempdir()) / "sky-claw", prohibidas=prohibidas)


# ---------------------------------------------------------------------------
# P0.2 — estado durable, coordinación cross-process y transición de root
# ---------------------------------------------------------------------------


def test_el_estado_durable_no_depende_del_cwd_ni_del_work_root(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, estado_durable_real: None
) -> None:
    """La ubicación es por usuario y estable: ni cwd, ni worktree, ni TEMP, ni el root.

    Es EL defecto que P0.2 cierra: `.skyclaw_backups/` es relativo, así que dos
    instancias de Sky-Claw lanzadas desde directorios distintos abren dos
    `locks.db` distintos y no se excluyen entre sí. Un lock que no serializa es
    peor que no tener lock: promete exclusión y no la da.
    """
    from sky_claw.config import Config, SystemPaths

    (tmp_path / "otro_cwd").mkdir()
    desde_aca = ws.ruta_de_estado_de_etapa9()
    monkeypatch.chdir(tmp_path / "otro_cwd")
    desde_alla = ws.ruta_de_estado_de_etapa9()

    assert desde_aca == desde_alla
    assert desde_aca.is_absolute()
    assert SystemPaths.runtime_state_dir() == Config.DEFAULT_CONFIG_DIR / "state"
    # Ni TEMP ni el propio work root pueden contenerla.
    import tempfile as _tempfile

    assert not str(desde_aca).startswith(str(pathlib.Path(_tempfile.gettempdir()).resolve()))


def test_el_estado_durable_no_sale_de_una_variable_de_entorno(
    monkeypatch: pytest.MonkeyPatch, estado_durable_real: None
) -> None:
    """Sin env var de staging: una segunda fuente reabre el split-brain de #552."""
    antes = ws.ruta_de_estado_de_etapa9()
    for variable in ("SKY_CLAW_STATE_DIR", "SKYCLAW_STATE_DIR", "SKY_CLAW_EXTERNAL_WORK_ROOT"):
        monkeypatch.setenv(variable, "/algo/que/no/deberia/importar")
    assert ws.ruta_de_estado_de_etapa9() == antes


async def test_la_coordinacion_reusa_el_ritual_y_los_leases_existentes(
    tmp_path: pathlib.Path,
) -> None:
    """Se conserva `dyndolod-pipeline` y la semántica de leases del árbol."""
    coordinacion = ws.construir_coordinacion_de_etapa9(base=tmp_path / "estado")
    try:
        assert coordinacion.RECURSO_DEL_RITUAL == "dyndolod-pipeline"
        async with coordinacion.sostener_ritual(agent_id="test") as sostenido:
            assert sostenido.lock_info is not None
            await sostenido.assert_owned()
            info = await (await coordinacion.manager_del_ritual()).get_lock_info("dyndolod-pipeline")
            assert info is not None and not info.is_expired
        assert await (await coordinacion.manager_del_ritual()).get_lock_info("dyndolod-pipeline") is None
    finally:
        await coordinacion.close()


_GUION_RITUAL = """\
import json, pathlib, sys, time
sys.path.insert(0, {raiz!r})
import asyncio
from sky_claw.local.tools import dyndolod_workspace as ws

estado = pathlib.Path(sys.argv[1])
salida = pathlib.Path(sys.argv[2])
señal = pathlib.Path(sys.argv[3])
rol = sys.argv[4]
# El cwd de cada proceso es DISTINTO a propósito: si la coordinación dependiera
# del cwd (como `.skyclaw_backups/locks.db`), los dos entrarían al ritual.
import os
os.chdir(sys.argv[5])

async def main():
    coordinacion = ws.construir_coordinacion_de_etapa9(base=estado)
    try:
        if rol == "primero":
            async with coordinacion.sostener_ritual(agent_id="proceso-a"):
                señal.write_text("dentro", encoding="utf-8")
                # Se queda adentro hasta que el segundo terminó de intentarlo.
                while not (señal.parent / "segundo_listo").exists():
                    await asyncio.sleep(0.02)
            salida.write_text(json.dumps({{"entro": True}}), encoding="utf-8")
        else:
            while not señal.exists():
                time.sleep(0.02)
            try:
                async with coordinacion.sostener_ritual(agent_id="proceso-b", ttl=2.0):
                    salida.write_text(json.dumps({{"entro": True}}), encoding="utf-8")
            except Exception as exc:
                salida.write_text(
                    json.dumps({{"entro": False, "error": type(exc).__name__}}), encoding="utf-8"
                )
            (señal.parent / "segundo_listo").write_text("ok", encoding="utf-8")
    finally:
        await coordinacion.close()

asyncio.run(main())
"""


def _correr_dos_procesos_de_ritual(tmp_path: pathlib.Path, *, estado: pathlib.Path) -> list[dict]:
    """Lanza dos procesos REALES con cwd distinto contra el mismo ritual."""
    raiz_repo = str(pathlib.Path(ws.__file__).resolve().parents[3])
    guion = tmp_path / "ritual.py"
    guion.write_text(_GUION_RITUAL.format(raiz=raiz_repo), encoding="utf-8")
    señal = tmp_path / "señales" / "primero_dentro"
    señal.parent.mkdir(parents=True, exist_ok=True)

    procesos = []
    salidas = []
    for indice, rol in ((1, "primero"), (2, "segundo")):
        cwd = tmp_path / f"cwd_{indice}"
        cwd.mkdir(exist_ok=True)
        salida = tmp_path / f"ritual_{indice}.json"
        salidas.append(salida)
        procesos.append(
            subprocess.Popen(  # noqa: S603 - argv fijo, sin shell
                [sys.executable, str(guion), str(estado), str(salida), str(señal), rol, str(cwd)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
    for proceso in procesos:
        _, err = proceso.communicate(timeout=180)
        assert proceso.returncode == 0, err.decode(errors="replace")
    return [json.loads(s.read_text(encoding="utf-8")) for s in salidas]


def test_dos_procesos_con_cwd_distinto_no_entran_al_mismo_ritual(
    tmp_path: pathlib.Path,
) -> None:
    """§20: A con cwd=X y B con cwd=Y sobre el MISMO recurso → el segundo no entra.

    Dos coroutines del mismo intérprete no prueban nada acá: comparten el
    módulo, el event loop y la conexión. La propiedad es cross-process, y el
    `cwd` distinto es lo que distingue una coordinación durable de una que
    depende de desde dónde se lanzó el proceso.
    """
    primero, segundo = _correr_dos_procesos_de_ritual(tmp_path, estado=tmp_path / "estado")

    assert primero["entro"] is True
    assert segundo["entro"] is False, "el segundo entró al ritual con el primero adentro"
    assert segundo["error"] == "LockAcquisitionError"


def test_la_coordinacion_no_depende_del_pathname_del_work_root(
    tmp_path: pathlib.Path,
) -> None:
    """Work roots distintos, MISMO ejecutable/recursos → siguen serializados.

    El exe, los INI, los logs, el `Data` y los mods se comparten aunque los work
    roots difieran (ADR 0011 §2.6): una coordinación identificada por el
    pathname del root prometería concurrencia que el filesystem no da. Se ancla
    que el identificador del ritual NO lleva el root adentro.
    """
    coordinacion_a = ws.construir_coordinacion_de_etapa9(base=tmp_path / "estado")
    coordinacion_b = ws.construir_coordinacion_de_etapa9(base=tmp_path / "estado")

    assert coordinacion_a.RECURSO_DEL_RITUAL == coordinacion_b.RECURSO_DEL_RITUAL
    assert "work" not in coordinacion_a.RECURSO_DEL_RITUAL
    # Y la exclusión real, con dos procesos y dos work roots distintos:
    primero, segundo = _correr_dos_procesos_de_ritual(tmp_path, estado=tmp_path / "estado")
    assert (primero["entro"], segundo["entro"]) == (True, False)


async def test_perder_la_lease_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """Lease perdida ⇒ no se afirma exclusividad, y la salida del bloque LEVANTA.

    Fail-closed en las dos mitades: `assert_owned` corta ANTES de la mutación
    siguiente, y la salida limpia del bloque tampoco puede reportar éxito — si
    el bloque terminara en silencio, el caller creería que el ritual corrió con
    exclusividad cuando otro dueño ya se llevó el lock a mitad de camino. Se
    reutiliza la semántica de leases que el árbol ya tiene, no una propia.
    """
    from sky_claw.app.db.locks import LockLeaseLostError

    coordinacion = ws.construir_coordinacion_de_etapa9(base=tmp_path / "estado")
    perdida_vista_desde_el_cuerpo = False
    try:
        with pytest.raises(LockLeaseLostError):
            async with coordinacion.sostener_ritual(agent_id="dueño", ttl=60.0) as sostenido:
                # Otro dueño se lleva el lock por debajo (expiración forzada).
                await (await coordinacion.manager_del_ritual()).force_release("dyndolod-pipeline")
                await (await coordinacion.manager_del_ritual()).acquire_lock("dyndolod-pipeline", "intruso", ttl=60.0)
                with pytest.raises(LockLeaseLostError):
                    await sostenido.assert_owned()
                perdida_vista_desde_el_cuerpo = sostenido.lease_lost

        assert perdida_vista_desde_el_cuerpo is True
        # Y el lock del intruso sigue siendo del intruso: no se lo robamos al salir.
        info = await (await coordinacion.manager_del_ritual()).get_lock_info("dyndolod-pipeline")
        assert info is not None and info.agent_id == "intruso"
    finally:
        await (await coordinacion.manager_del_ritual()).force_release("dyndolod-pipeline")
        await coordinacion.close()


async def test_la_coordinacion_se_libera_recien_al_salir_del_bloque(
    tmp_path: pathlib.Path,
) -> None:
    """Ni una cancelación ni una excepción sueltan la coordinación antes de tiempo.

    El orden importa: mientras el cuerpo sigue corriendo —proceso hijo vivo,
    rollback restaurando, recovery mutando— la coordinación NO se suelta, y se
    suelta SIEMPRE al salir, también por el camino de excepción.
    """
    coordinacion = ws.construir_coordinacion_de_etapa9(base=tmp_path / "estado")
    vistos: list[bool] = []
    try:
        with pytest.raises(RuntimeError):
            async with coordinacion.sostener_ritual(agent_id="dueño"):
                info = await (await coordinacion.manager_del_ritual()).get_lock_info("dyndolod-pipeline")
                vistos.append(info is not None and not info.is_expired)
                raise RuntimeError("el ritual explotó a mitad de una mutación")

        assert vistos == [True]
        assert await (await coordinacion.manager_del_ritual()).get_lock_info("dyndolod-pipeline") is None
    finally:
        await coordinacion.close()


def test_el_orden_de_adquisicion_esta_documentado_y_es_aciclico() -> None:
    """§21: el orden vive escrito en el módulo, no en la cabeza de quien lo escribió."""
    doc = ws.__doc__ or ""
    orden = ws.ORDEN_DE_ADQUISICION
    assert orden == (
        "dyndolod-workspace",
        "dyndolod-ownership",
        "dyndolod-pipeline",
        "snapshot-transaction-lock",
        "journal",
        "directory-rollback",
        "handoff/recovery",
    )
    assert len(set(orden)) == len(orden), "un nombre repetido sería un ciclo"
    for nombre in orden:
        assert nombre in doc, f"{nombre} no está en el orden documentado del módulo"


# ---------------------------------------------------------------------------
# Resolución de workspace, unicidad de root activo y transición
# ---------------------------------------------------------------------------


def _coordinacion(tmp_path: pathlib.Path) -> ws.Stage9Coordination:
    return ws.construir_coordinacion_de_etapa9(base=tmp_path / "estado")


async def test_preferencia_ausente_es_no_configurado_y_no_rompe_nada(
    tmp_path: pathlib.Path,
) -> None:
    """Ausente ⇒ `None` (NO CONFIGURADO), no una excepción que tumbe el arranque."""
    coordinacion = _coordinacion(tmp_path)
    try:
        for vacia in (None, "", "   "):
            assert (
                await ws.resolver_workspace(
                    preferencia=vacia,
                    recursos=_instancia(tmp_path),
                    prohibidas=_prohibidas(tmp_path),
                    registro=_registro(tmp_path),
                    coordinacion=coordinacion,
                )
                is None
            )
    finally:
        await coordinacion.close()


async def test_resolver_inicializa_y_recuerda_el_root_activo(tmp_path: pathlib.Path) -> None:
    recursos = _instancia(tmp_path)
    registro = _registro(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    root = tmp_path / "Sky-Claw Work"
    try:
        resuelto = await ws.resolver_workspace(
            preferencia=str(root),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        assert resuelto is not None
        assert resuelto.root == root.resolve()
        assert resuelto.recien_inicializado is True
        assert resuelto.estado is ws.EstadoDelRoot.A_AUSENTE

        entrada = registro.entrada(recursos.clave())
        assert entrada is not None
        assert entrada.root == str(root.resolve())
        assert entrada.binding_id == resuelto.binding.binding_id
        assert entrada.transicion_pendiente is None, "una transición terminada no deja marcador"

        # P2.0: un "arranque siguiente" en el MISMO proceso exige liberar la
        # lease del primero — la exclusividad del snapshot vivo es de proceso.
        await resuelto.ownership.liberar()
        # Segundo arranque sobre el mismo root: caso C, sin re-inicializar.
        otra_vez = await ws.resolver_workspace(
            preferencia=str(root),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        assert otra_vez is not None
        assert otra_vez.estado is ws.EstadoDelRoot.C_BINDING_COMPATIBLE
        assert otra_vez.recien_inicializado is False
        assert otra_vez.binding.binding_id == resuelto.binding.binding_id
        await otra_vez.ownership.liberar()
    finally:
        await coordinacion.close()


async def test_dos_roots_con_el_mismo_resource_binding_no_quedan_ambos_activos(
    tmp_path: pathlib.Path,
) -> None:
    """La unicidad instalacional es el caso H, y NO se puede probar con los JSON locales.

    Los dos roots tienen un binding perfectamente válido para los mismos
    recursos; mirados de a uno, los dos son caso C. Lo que los distingue es el
    estado durable de la instalación, que es por lo que el ADR le asigna la
    detección a P0.2 y no al archivo del binding.
    """
    recursos = _instancia(tmp_path)
    registro = _registro(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    root_a = tmp_path / "Work A"
    root_b = tmp_path / "Work B"
    try:
        await ws.resolver_workspace(
            preferencia=str(root_a),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        # B se inicializa "a mano" con un binding válido para los MISMOS recursos.
        ws.publicar_binding(root_b, recursos)

        assert ws.evaluar_estado(root_b, recursos=recursos, registro=registro) is ws.EstadoDelRoot.H_OTRO_ROOT_ACTIVO
        # Y el registro sigue nombrando a UNO solo.
        assert registro.entrada(recursos.clave()).root == str(root_a.resolve())
    finally:
        await coordinacion.close()


async def test_la_transicion_se_registra_durablemente_antes_de_activar(
    tmp_path: pathlib.Path,
) -> None:
    """§24: persistir preferencia → reinicio → inspeccionar → validar → activar."""
    recursos = _instancia(tmp_path)
    registro = _registro(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    viejo = tmp_path / "Work Viejo"
    nuevo = tmp_path / "Work Nuevo"
    vistos: list[tuple[str, object]] = []
    try:
        primero = await ws.resolver_workspace(
            preferencia=str(viejo),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        assert primero is not None
        # P2.0: la lease del "arranque anterior" se libera para poder simular el
        # siguiente arranque en el mismo proceso.
        await primero.ownership.liberar()

        def _inspector(root: pathlib.Path) -> ws.InspeccionDeRootViejo:
            # Al inspeccionar, el registro TODAVÍA nombra al viejo: la
            # transición no puede haberse dado por hecha antes de validarla.
            vistos.append((str(root), registro.entrada(recursos.clave()).root))
            return ws.inspeccionar_root_para_transicion(root)

        resuelto = await ws.resolver_workspace(
            preferencia=str(nuevo),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
            inspector=_inspector,
        )

        assert vistos == [(str(viejo.resolve()), str(viejo.resolve()))]
        assert resuelto is not None
        assert resuelto.root == nuevo.resolve()
        entrada = registro.entrada(recursos.clave())
        assert entrada.root == str(nuevo.resolve())
        assert entrada.transicion_pendiente is None
        # El root viejo NO se toca: ni se migra, ni se borra, ni se adopta.
        assert (viejo / ws.ARCHIVO_DE_BINDING).exists()
        await resuelto.ownership.liberar()
    finally:
        await coordinacion.close()


async def test_la_transicion_sostiene_el_ritual_mientras_valida_y_activa(
    tmp_path: pathlib.Path,
) -> None:
    """§25: el guard del ritual es una ADQUISICIÓN, no una foto de `get_lock_info`.

    RED contra el TOCTOU real: entre la foto y el `registrar_transicion` corría
    la inspección del root viejo, que recorre GB de generaciones y no tiene cota
    de tiempo. Una etapa 9 que arrancaba en otro proceso dentro de esa ventana
    encontraba el registro ya repuntado a la raíz nueva, con su corrida en vuelo
    sobre la vieja.

    Se observa desde la sonda de §25, que corre DENTRO de la validación: el lock
    tiene que estar tomado ahí, y suelto cuando la resolución termina.
    """
    recursos = _instancia(tmp_path)
    registro = _registro(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    viejo = tmp_path / "Work Viejo"
    nuevo = tmp_path / "Work Nuevo"
    dueños: list[str | None] = []
    try:
        primero = await ws.resolver_workspace(
            preferencia=str(viejo),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        assert primero is not None
        await primero.ownership.liberar()

        async def _sonda() -> bool:
            manager = await coordinacion.manager_del_ritual()
            info = await manager.get_lock_info(ws.Stage9Coordination.RECURSO_DEL_RITUAL)
            dueños.append(None if info is None or info.is_expired else info.agent_id)
            return False

        resuelto = await ws.resolver_workspace(
            preferencia=str(nuevo),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
            sonda_de_transaccion_pendiente=_sonda,
        )

        assert resuelto is not None and resuelto.root == nuevo.resolve()
        assert dueños == [ws._AGENTE_DEL_RESOLVER], "el ritual NO estaba sostenido durante la validación"

        # Y se suelta: una transición no puede dejar el ritual tomado, o el
        # próximo pipeline de etapa 9 no arrancaría nunca.
        manager = await coordinacion.manager_del_ritual()
        assert await manager.get_lock_info(ws.Stage9Coordination.RECURSO_DEL_RITUAL) is None
        await resuelto.ownership.liberar()
    finally:
        await coordinacion.close()


@pytest.mark.parametrize("preferencia", [3, 3.5, True, pathlib.PurePosixPath("/work"), ["/work"]])
async def test_una_preferencia_que_no_es_cadena_se_rechaza_con_mensaje(
    tmp_path: pathlib.Path, preferencia: object
) -> None:
    """Un TOML editado a mano trae `external_work_root = 3`, y eso no es un crash.

    El `.strip()` sobre un int levantaba `AttributeError`: una traza que no
    nombra ni el campo ni la acción, y que el boundary del arranque reporta como
    "falla inesperada" en vez de como lo que es — config inválida y corregible.
    """
    coordinacion = _coordinacion(tmp_path)
    try:
        with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
            await ws.resolver_workspace(
                preferencia=preferencia,  # type: ignore[arg-type]
                recursos=_instancia(tmp_path),
                prohibidas=_prohibidas(tmp_path),
                registro=_registro(tmp_path),
                coordinacion=coordinacion,
            )
    finally:
        await coordinacion.close()

    assert excinfo.value.motivo is ws.MotivoDeRechazo.INVALIDO
    assert "external_work_root" in excinfo.value.razon
    assert type(preferencia).__name__ in excinfo.value.razon


def _sabotear_con_backups(viejo: pathlib.Path) -> None:
    (viejo / "DynDOLOD").mkdir(parents=True, exist_ok=True)
    (viejo / "DynDOLOD" / "textures.rollback-1757462400000000000").mkdir()


def _inspector_ciego(_root: pathlib.Path) -> ws.InspeccionDeRootViejo:
    """Lo que devuelve el inspector REAL ante un root que no puede recorrer.

    No se usa `chmod(0o000)` para producirlo: el CI de este repo corre como root
    en Linux, donde los bits de permiso no detienen la lectura, así que ese
    montaje probaría el camino feliz creyendo que prueba el otro. El
    comportamiento del inspector ante un `OSError` real se ancla aparte, en
    `test_el_inspector_no_reporta_limpio_lo_que_no_pudo_recorrer`.
    """
    return ws.InspeccionDeRootViejo(inspeccionable=False, backups=(), detalle="permiso denegado al recorrer")


@pytest.mark.parametrize(
    ("etiqueta", "sabotear", "inspector"),
    [
        ("backups de move-aside pendientes", _sabotear_con_backups, None),
        ("el root viejo no se puede inspeccionar", lambda _viejo: None, _inspector_ciego),
    ],
)
async def test_un_root_viejo_no_quiescente_bloquea_la_transicion(
    tmp_path: pathlib.Path, etiqueta: str, sabotear, inspector
) -> None:
    """§25: con backups o sin poder inspeccionar, NO se olvida ni se activa otra."""
    recursos = _instancia(tmp_path)
    registro = _registro(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    viejo = tmp_path / "Work Viejo"
    nuevo = tmp_path / "Work Nuevo"
    extra = {} if inspector is None else {"inspector": inspector}
    try:
        primero = await ws.resolver_workspace(
            preferencia=str(viejo),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        assert primero is not None
        await primero.ownership.liberar()
        sabotear(viejo)

        with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
            await ws.resolver_workspace(
                preferencia=str(nuevo),
                recursos=recursos,
                prohibidas=_prohibidas(tmp_path),
                registro=registro,
                coordinacion=coordinacion,
                **extra,
            )

        assert excinfo.value.motivo is ws.MotivoDeRechazo.TRANSICION_REQUERIDA, etiqueta
        # NO se olvidó el root viejo y NO se activó el nuevo.
        assert registro.entrada(recursos.clave()).root == str(viejo.resolve())
        assert not (nuevo / ws.ARCHIVO_DE_BINDING).exists()
    finally:
        await coordinacion.close()


def test_el_inspector_no_reporta_limpio_lo_que_no_pudo_recorrer(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`os.walk` se traga los errores por default; acá eso sería un fail-OPEN.

    Un `PermissionError` a mitad del recorrido tiene que reportarse como "no
    pude inspeccionar", no como "no encontré backups": la diferencia entre las
    dos respuestas es si la transición avanza abandonando una raíz que quizá
    conserva la única copia recuperable de una generación.
    """
    viejo = tmp_path / "Work Viejo"
    viejo.mkdir()

    def _walk_que_falla(_root, onerror=None, **_kwargs):
        if onerror is not None:
            onerror(PermissionError(13, "Permission denied"))
        return iter(())

    monkeypatch.setattr(ws.os, "walk", _walk_que_falla)

    inspeccion = ws.inspeccionar_root_para_transicion(viejo)

    assert inspeccion.inspeccionable is False
    assert inspeccion.backups == ()
    assert "no se pudo recorrer" in inspeccion.detalle


def test_el_inspector_encuentra_el_residuo_con_la_regex_del_reconciliador(
    tmp_path: pathlib.Path,
) -> None:
    """Misma definición de "backup nuestro" que `rollback_reconciler`, no otra."""
    from sky_claw.local.tools.rollback_reconciler import SUFIJO_MOVE_ASIDE

    viejo = tmp_path / "Work Viejo"
    (viejo / "DynDOLOD").mkdir(parents=True)
    residuo = viejo / "DynDOLOD" / "textures.rollback-1757462400000000000"
    residuo.mkdir()
    # Un nombre PARECIDO que la regex del reconciliador no admite tampoco cuenta acá.
    (viejo / "DynDOLOD" / "textures.rollback-123").mkdir()

    inspeccion = ws.inspeccionar_root_para_transicion(viejo)

    assert inspeccion.inspeccionable is True
    assert inspeccion.backups == (residuo,)
    assert SUFIJO_MOVE_ASIDE.search(residuo.name)


async def test_un_ritual_vivo_bloquea_la_transicion_como_ocupado(
    tmp_path: pathlib.Path,
) -> None:
    """Un ritual en vuelo (aun en otra instancia) no se atropella: OCUPADO."""
    recursos = _instancia(tmp_path)
    registro = _registro(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    viejo = tmp_path / "Work Viejo"
    nuevo = tmp_path / "Work Nuevo"
    try:
        primero = await ws.resolver_workspace(
            preferencia=str(viejo),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        assert primero is not None
        # Se libera la lease de este "arranque" para que el bloqueo de abajo sea
        # el del RITUAL vivo y no el de la exclusividad de proceso (P2.0).
        await primero.ownership.liberar()
        await coordinacion.initialize()
        await (await coordinacion.manager_del_ritual()).acquire_lock("dyndolod-pipeline", "otra-instancia", ttl=120.0)

        with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
            await ws.resolver_workspace(
                preferencia=str(nuevo),
                recursos=recursos,
                prohibidas=_prohibidas(tmp_path),
                registro=registro,
                coordinacion=coordinacion,
            )

        assert excinfo.value.motivo is ws.MotivoDeRechazo.OCUPADO
        assert registro.entrada(recursos.clave()).root == str(viejo.resolve())
    finally:
        await (await coordinacion.manager_del_ritual()).force_release("dyndolod-pipeline")
        await coordinacion.close()


async def test_una_transaccion_pendiente_bloquea_la_transicion(tmp_path: pathlib.Path) -> None:
    """Una TX PENDING del root viejo no se abandona cambiando de raíz."""
    recursos = _instancia(tmp_path)
    registro = _registro(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    viejo = tmp_path / "Work Viejo"
    nuevo = tmp_path / "Work Nuevo"

    async def _hay_pendiente() -> bool:
        return True

    try:
        primero = await ws.resolver_workspace(
            preferencia=str(viejo),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        assert primero is not None
        await primero.ownership.liberar()
        with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
            await ws.resolver_workspace(
                preferencia=str(nuevo),
                recursos=recursos,
                prohibidas=_prohibidas(tmp_path),
                registro=registro,
                coordinacion=coordinacion,
                sonda_de_transaccion_pendiente=_hay_pendiente,
            )
        assert excinfo.value.motivo is ws.MotivoDeRechazo.TRANSICION_REQUERIDA
        assert registro.entrada(recursos.clave()).root == str(viejo.resolve())
    finally:
        await coordinacion.close()


async def test_una_transicion_interrumpida_se_revalida_en_el_proximo_arranque(
    tmp_path: pathlib.Path,
) -> None:
    """Interrupción entre "registrar" y "activar": el marcador sobrevive y se revalida.

    El proceso murió después de repuntar el registro al root nuevo y antes de
    confirmar la activación. El próximo arranque no puede dar la transición por
    terminada: el marcador nombra el root viejo, así que se lo vuelve a
    inspeccionar — y si mientras tanto le aparecieron backups, se bloquea.
    """
    recursos = _instancia(tmp_path)
    registro = _registro(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    viejo = tmp_path / "Work Viejo"
    nuevo = tmp_path / "Work Nuevo"
    try:
        primero = await ws.resolver_workspace(
            preferencia=str(viejo),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        assert primero is not None
        await primero.ownership.liberar()
        # Muerte dura justo después del registro durable de la transición.
        registro.registrar_transicion(clave=recursos.clave(), hacia=nuevo, motivo="preferencia cambiada")
        assert registro.entrada(recursos.clave()).transicion_pendiente is not None

        # Y en el ínterin el root viejo quedó con residuo de move-aside.
        (viejo / "DynDOLOD").mkdir(parents=True, exist_ok=True)
        (viejo / "DynDOLOD" / "textures.rollback-1757462400000000000").mkdir()

        with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
            await ws.resolver_workspace(
                preferencia=str(nuevo),
                recursos=recursos,
                prohibidas=_prohibidas(tmp_path),
                registro=registro,
                coordinacion=coordinacion,
            )
        assert excinfo.value.motivo is ws.MotivoDeRechazo.TRANSICION_REQUERIDA
        assert "Work Viejo" in excinfo.value.razon
        # El marcador sigue vivo: la transición NO se dio por terminada.
        assert registro.entrada(recursos.clave()).transicion_pendiente is not None
    finally:
        await coordinacion.close()


async def test_edicion_manual_del_toml_reconcilia_contra_el_estado_durable(
    tmp_path: pathlib.Path,
) -> None:
    """§26: el TOML NO es autoridad de propiedad; se reconcilia contra lo durable.

    El usuario edita `external_work_root` a mano y reinicia. El sistema tiene
    que reconciliar preferencia + binding local + registro durable + recovery
    pendiente, y fallar cerrado si no puede demostrar una transición segura.
    """
    from sky_claw.config import Config
    from sky_claw.local.local_config import persistir_campo

    recursos = _instancia(tmp_path)
    registro = _registro(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    config_path = tmp_path / "config.toml"
    viejo = tmp_path / "Work Viejo"
    nuevo = tmp_path / "Work Nuevo"
    try:
        persistir_campo(config_path, "external_work_root", str(viejo))
        primero = await ws.resolver_workspace(
            preferencia=Config(config_path).external_work_root,
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        assert primero is not None
        await primero.ownership.liberar()

        # Edición MANUAL del TOML: se reescribe el archivo como lo haría el
        # usuario con un editor de texto. NO se usa `persistir_campo` a propósito
        # — el punto del caso es que la preferencia cambió por FUERA de toda la
        # maquinaria de workspace.
        #
        # Se escribe la línea TOML directamente en vez de un `str.replace` del
        # path crudo sobre el archivo: en Windows el serializador escapa las
        # barras invertidas (`"D:\\a\\Work Viejo"`), así que el `replace` del
        # path sin escapar no encontraba NADA — el montaje era un no-op, la
        # preferencia seguía apuntando al root viejo y el test verificaba "el
        # mismo root otra vez" creyendo que verificaba una transición. Verde en
        # POSIX, rojo en Windows, y sin probar nada en ninguno de los dos.
        # `json.dumps` produce el mismo escapado de barras y comillas que exige
        # una basic string de TOML, en cualquier plataforma.
        config_path.write_text(f"external_work_root = {json.dumps(str(nuevo))}\n", encoding="utf-8")
        # El montaje tiene que haber mutado algo de verdad. Esta aserción es la
        # que convierte "el test no prueba nada" en un fallo visible.
        assert Config(config_path).external_work_root == str(nuevo)

        # Y el root viejo quedó con residuo de move-aside sin reconciliar.
        (viejo / "DynDOLOD").mkdir(parents=True, exist_ok=True)
        (viejo / "DynDOLOD" / "textures.rollback-1757462400000000000").mkdir()

        with pytest.raises(ws.WorkspaceRechazadoError):
            await ws.resolver_workspace(
                preferencia=Config(config_path).external_work_root,
                recursos=recursos,
                prohibidas=_prohibidas(tmp_path),
                registro=registro,
                coordinacion=coordinacion,
            )
        assert registro.entrada(recursos.clave()).root == str(viejo.resolve()), (
            "el TOML editado a mano hizo olvidar el root viejo"
        )
    finally:
        await coordinacion.close()


async def test_cambiar_la_preferencia_no_hace_hot_reload(tmp_path: pathlib.Path) -> None:
    """§27: lo resuelto en ESTE arranque no muta cuando cambia la preferencia.

    `WorkspaceResuelto` es un snapshot inmutable del arranque. Cambiar el TOML
    en caliente no puede mover el root debajo de un `AppContext`, un
    `PathValidator`, un runner cacheado, el reconciliador ni una TX activa: el
    boundary es el próximo arranque, y eso se ve acá en que el objeto ya
    resuelto sigue nombrando el root de antes.
    """
    import dataclasses as _dc

    from sky_claw.local.local_config import persistir_campo

    recursos = _instancia(tmp_path)
    registro = _registro(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    config_path = tmp_path / "config.toml"
    viejo = tmp_path / "Work Viejo"
    try:
        persistir_campo(config_path, "external_work_root", str(viejo))
        resuelto = await ws.resolver_workspace(
            preferencia=str(viejo),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        assert resuelto is not None

        persistir_campo(config_path, "external_work_root", str(tmp_path / "Work Nuevo"))

        assert resuelto.root == viejo.resolve()
        assert _dc.is_dataclass(resuelto) and resuelto.__dataclass_params__.frozen
        with pytest.raises(_dc.FrozenInstanceError):
            resuelto.root = tmp_path / "Work Nuevo"  # type: ignore[misc]
        await resuelto.ownership.liberar()
    finally:
        await coordinacion.close()


# ---------------------------------------------------------------------------
# Frontera P0 / PR-2: la capacidad existe, la activación NO
# ---------------------------------------------------------------------------


def test_p0_no_cambia_el_output_productivo_del_runner(tmp_path: pathlib.Path) -> None:
    """`P0 CAPABILITY != PR-2 ACTIVATION`, verificado sobre el argv REAL.

    Si un cambio de P0 hiciera que el `-o:` del runner apunte al work root
    externo, sería SCOPE VIOLATION: PR-2 reabre el gate de lanzamiento T5 al
    tocar esos subroots, y este PR no lo reabre. Se mide el argv que el runner
    construye, no un comentario que diga que no cambió.
    """
    from sky_claw.local.tools.dyndolod_runner import DynDOLODConfig, DynDOLODRunner
    from sky_claw.local.tools.output_targets import (
        DYNDOLOD_OUTPUT_ROOT,
        SKY_CLAW_MANAGED_DIR,
        dyndolod_output_target,
    )

    game = tmp_path / "Skyrim Special Edition"
    (game / "Data").mkdir(parents=True)
    exe = tmp_path / "tools" / "DynDOLOD" / "DynDOLODx64.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")

    # La derivación productiva sigue colgando del juego, no del work root.
    esperado = game.resolve() / SKY_CLAW_MANAGED_DIR / DYNDOLOD_OUTPUT_ROOT
    assert dyndolod_output_target(game=game) == esperado

    config = DynDOLODConfig(dyndolod_exe=exe, game_path=game, mo2_path=None, mo2_mods_path=None)
    assert config.output_root == esperado

    runner = DynDOLODRunner(config)
    argv = runner._build_xedit_args(None)
    salidas = [arg for arg in argv if arg.startswith("-o:")]
    assert len(salidas) == 1
    assert str(esperado) in salidas[0]
    assert "Sky-Claw Work" not in salidas[0]


def test_ni_output_targets_ni_el_runner_conocen_el_workspace() -> None:
    """Ancla de importación: la capacidad de P0 no puede filtrarse al `-o:`.

    El test de arriba mide el argv de HOY; éste cierra la clase. Mientras
    `output_targets` y el runner no importen el módulo de workspace, no hay
    forma de que la derivación de salida empiece a depender de él sin romper
    acá primero.
    """
    import ast

    for modulo in ("output_targets", "dyndolod_runner"):
        ruta = pathlib.Path(ws.__file__).with_name(f"{modulo}.py")
        arbol = ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))
        importados = set()
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Import):
                importados.update(alias.name for alias in nodo.names)
            elif isinstance(nodo, ast.ImportFrom) and nodo.module:
                importados.add(nodo.module)
        assert not any("dyndolod_workspace" in nombre for nombre in importados), modulo


def test_p0_no_toca_el_root_legacy(tmp_path: pathlib.Path) -> None:
    """El árbol legacy `<game>/Sky-Claw/DynDOLOD` queda intacto: ni migrar ni adoptar.

    P0 no lo migra, no lo borra, no lo mueve, no lo adopta como staging nuevo y
    no crea `DirectoryRollback` sobre él. Lo único que P0 hace con un root viejo
    es LEERLO para negarse a abandonarlo con backups pendientes.
    """
    from sky_claw.local.tools.output_targets import dyndolod_output_target

    game = tmp_path / "game"
    legacy = dyndolod_output_target(game=game)
    (legacy / "textures").mkdir(parents=True)
    (legacy / "textures" / "lod.dds").write_bytes(b"generacion anterior")
    antes = sorted(p.relative_to(legacy).as_posix() for p in legacy.rglob("*"))

    inspeccion = ws.inspeccionar_root_para_transicion(legacy)

    assert inspeccion.inspeccionable is True
    assert inspeccion.backups == ()
    despues = sorted(p.relative_to(legacy).as_posix() for p in legacy.rglob("*"))
    assert despues == antes
    assert (legacy / "textures" / "lod.dds").read_bytes() == b"generacion anterior"


#: Sitios de PRODUCCIÓN que construyen `DynDOLODPipelineService`. Igualdad
#: literal, como `RITUAL_TOOL_MAP`: un constructor nuevo rompe el ancla hasta
#: que se decida si participa de la coordinación de etapa 9. Sin esto, el
#: default `stage9_coordination=None` sería exactamente el defecto dominante de
#: este repo —un camino coordinado y su gemelo no— en su forma más silenciosa,
#: porque no falla nada: simplemente no hay exclusión.
CONSTRUCTORES_DEL_SERVICIO_DYNDOLOD: frozenset[str] = frozenset(
    {
        "sky_claw/app/orchestrator/orchestration_composition.py",
        "sky_claw/app/orchestrator/preview/chain_preview_service.py",
    }
)


def test_censo_de_constructores_del_servicio_dyndolod() -> None:
    """Todo constructor de producción pasa `stage9_coordination=`, sin excepciones."""
    import ast

    raiz = pathlib.Path(ws.__file__).resolve().parents[3]
    paquete = raiz / "sky_claw"

    def _nombre_construido(func: ast.expr) -> str | None:
        """Nombre de lo que se construye, sea ``Servicio(...)`` o ``modulo.Servicio(...)``.

        Mirar sólo `ast.Name` dejaba al censo ciego a la forma calificada, que es
        la que produce un `import sky_claw.local.tools.dyndolod_service as ds`.
        Un censo con un punto ciego no es un censo: da verde con el constructor
        sin coordinar delante.
        """
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return None

    encontrados: dict[str, bool] = {}
    for archivo in paquete.rglob("*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call) and _nombre_construido(nodo.func) == "DynDOLODPipelineService":
                clave = archivo.relative_to(raiz).as_posix()
                pasa = any(kw.arg == "stage9_coordination" for kw in nodo.keywords)
                encontrados[clave] = encontrados.get(clave, True) and pasa

    assert set(encontrados) == CONSTRUCTORES_DEL_SERVICIO_DYNDOLOD
    sin_coordinacion = sorted(k for k, v in encontrados.items() if not v)
    assert not sin_coordinacion, f"construyen el servicio sin coordinar: {sin_coordinacion}"


def _funcion_con_el_veto_de_leases() -> tuple[object, str]:
    """La función de `dyndolod_service` que arma el veto de rollback."""
    import ast as _ast

    from sky_claw.local.tools import dyndolod_service

    fuente = pathlib.Path(dyndolod_service.__file__).read_text(encoding="utf-8")
    arbol = _ast.parse(fuente)
    for nodo in _ast.walk(arbol):
        if isinstance(nodo, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and any(
            isinstance(hijo, _ast.FunctionDef) and hijo.name == "_conserva_las_leases" for hijo in _ast.walk(nodo)
        ):
            return nodo, fuente
    raise AssertionError("no se encontró la función que define `_conserva_las_leases`")


#: Las leases que sostienen una corrida de etapa 9. Igualdad literal: una tercera
#: que se sume al `AsyncExitStack` rompe el ancla hasta que se decida si participa
#: del veto y de los fences. Es el defecto dominante del repo en su forma exacta —
#: un lock cableado y su hermano no—, y acá los dos hermanos viven en la MISMA
#: función, que es donde el repo ya lo cometió (#373).
LEASES_DE_LA_CORRIDA_DE_ETAPA9: frozenset[str] = frozenset({"tx_lock", "ritual_de_etapa9"})


def test_todas_las_leases_de_la_corrida_participan_del_veto_y_de_los_fences() -> None:
    """El veto de rollback y `assert_owned` miran TODAS las leases, no una.

    La coordinación de etapa 9 sostiene una lease REAL —con heartbeat y
    `lease_lost`— y es la única que excluye a una instancia lanzada desde otro
    `cwd`. Descartarla dejaba el veto de `DirectoryRollback` mirando sólo el lock
    del `locks.db` relativo al cwd: con la lease cross-process perdida, el
    rollback restauraba encima de la salida del nuevo dueño. El fence de
    provenance tenía el mismo agujero.
    """
    import ast as _ast

    funcion, _ = _funcion_con_el_veto_de_leases()

    con_lease_lost = {
        nodo.value.id
        for nodo in _ast.walk(funcion)
        if isinstance(nodo, _ast.Attribute) and nodo.attr == "lease_lost" and isinstance(nodo.value, _ast.Name)
    }
    con_assert_owned = {
        nodo.func.value.id
        for nodo in _ast.walk(funcion)
        if isinstance(nodo, _ast.Call)
        and isinstance(nodo.func, _ast.Attribute)
        and nodo.func.attr == "assert_owned"
        and isinstance(nodo.func.value, _ast.Name)
    }

    assert con_lease_lost == LEASES_DE_LA_CORRIDA_DE_ETAPA9, (
        f"el veto de rollback no mira todas las leases: {sorted(con_lease_lost)}"
    )
    assert con_assert_owned == LEASES_DE_LA_CORRIDA_DE_ETAPA9, (
        f"los fences de ownership no miran todas las leases: {sorted(con_assert_owned)}"
    )


def test_el_directory_rollback_no_veta_con_una_sola_lease() -> None:
    """Ancla de forma: el veto se pasa como el predicado que enumera, no inline.

    Un `lambda: not tx_lock.lease_lost` vuelve a ser fácil de escribir y vuelve a
    dejar la mitad del mecanismo afuera; el predicado con nombre es el que el
    ancla de arriba puede verificar.
    """
    import ast as _ast

    funcion, _ = _funcion_con_el_veto_de_leases()
    for nodo in _ast.walk(funcion):
        if not (
            isinstance(nodo, _ast.Call) and isinstance(nodo.func, _ast.Name) and nodo.func.id == "DirectoryRollback"
        ):
            continue
        veto = next((kw.value for kw in nodo.keywords if kw.arg == "should_rollback"), None)
        assert isinstance(veto, _ast.Name) and veto.id == "_conserva_las_leases", (
            f"DirectoryRollback recibe un veto que no enumera las leases: {_ast.unparse(nodo)}"
        )


async def test_la_sonda_de_transaccion_pendiente_ve_una_tx_sin_cerrar(tmp_path: pathlib.Path) -> None:
    """§25: la sonda que producción cablea responde sobre el journal REAL.

    El parámetro existía desde el principio y producción nunca lo llenaba, así
    que el fail-closed de "transacción PENDING sin resolver" sólo vivía en los
    tests. Acá se ejerce la sonda que `AppContext` construye, contra un journal
    de verdad.
    """
    from sky_claw.app.db.journal import OperationJournal
    from sky_claw.app_context import AppContext

    journal = OperationJournal(db_path=tmp_path / "journal.db")
    await journal.open()
    try:
        sonda = AppContext._sonda_de_transaccion_pendiente(journal)
        assert await sonda() is False

        tx_id = await journal.begin_transaction(description="corrida interrumpida", agent_id="test")
        assert await sonda() is True, "una TX PENDING viva tiene que bloquear la transición"

        await journal.commit_transaction(tx_id)
        assert await sonda() is False, "una TX cerrada no puede seguir bloqueando"
    finally:
        await journal.close()


def test_el_arranque_cablea_la_sonda_y_resuelve_despues_del_journal() -> None:
    """Ancla por AST del orden y del cableado que la sonda REQUIERE.

    Dos cosas que no se pueden verificar por separado: la sonda necesita el
    journal ABIERTO, así que resolver el workspace antes de `journal.open()`
    volvía imposible cablearla — el orden y el cableado son la misma propiedad.
    """
    import ast as _ast

    import sky_claw.app_context as app_context_mod

    fuente = pathlib.Path(app_context_mod.__file__).read_text(encoding="utf-8")
    arbol = _ast.parse(fuente)
    inner = next(n for n in _ast.walk(arbol) if isinstance(n, _ast.AsyncFunctionDef) and n.name == "_start_full_inner")

    posiciones: dict[str, int] = {}
    for nodo in _ast.walk(inner):
        if isinstance(nodo, _ast.Call):
            if (
                isinstance(nodo.func, _ast.Attribute)
                and nodo.func.attr == "open"
                and isinstance(nodo.func.value, _ast.Name)
                and nodo.func.value.id == "journal"
            ):
                posiciones.setdefault("journal.open", nodo.lineno)
            if isinstance(nodo.func, _ast.Name) and nodo.func.id == "reconcile_orphan_rollback_backups":
                posiciones.setdefault("reconcile_orphan_rollback_backups", nodo.lineno)
            if isinstance(nodo.func, _ast.Attribute) and nodo.func.attr == "_resolver_workspace_de_dyndolod":
                posiciones.setdefault("resolver_workspace", nodo.lineno)
                assert any(kw.arg == "journal" for kw in nodo.keywords), (
                    "la resolución del workspace no recibe el journal: la sonda de §25 no se puede cablear"
                )

    assert set(posiciones) == {"journal.open", "reconcile_orphan_rollback_backups", "resolver_workspace"}
    assert posiciones["journal.open"] < posiciones["resolver_workspace"], (
        "el workspace se resuelve antes de que el journal esté abierto: la sonda de §25 quedaría en None"
    )
    assert posiciones["reconcile_orphan_rollback_backups"] < posiciones["resolver_workspace"], (
        "la transición se validaría contra backups que el barrido del mismo arranque estaba por reconciliar"
    )

    resolver = next(
        n
        for n in _ast.walk(arbol)
        if isinstance(n, _ast.AsyncFunctionDef) and n.name == "_resolver_workspace_de_dyndolod"
    )
    llamada = next(
        n
        for n in _ast.walk(resolver)
        if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name) and n.func.id == "resolver_workspace"
    )
    assert any(kw.arg == "sonda_de_transaccion_pendiente" for kw in llamada.keywords), (
        "el fail-closed de §25 sigue siendo test-only: producción no pasa la sonda"
    )


class _CoordinacionEspia:
    """Stand-in que sólo registra si le pidieron cerrar."""

    def __init__(self) -> None:
        self.cierres = 0

    async def close(self) -> None:
        self.cierres += 1


@pytest.mark.parametrize(
    ("propia", "cierres_esperados"),
    [(True, 1), (False, 0)],
)
async def test_el_supervisor_cierra_la_coordinacion_propia_y_nunca_la_ajena(
    propia: bool, cierres_esperados: int
) -> None:
    """Quién construyó, cierra — y las dos mitades importan.

    La de respaldo (la arma el composition root cuando nadie inyecta) no tiene
    dueño: su conexión SQLite sobre la DB durable sobrevivía al supervisor. La
    INYECTADA sí lo tiene —`AppContext` la registra en su cleanup— y cerrarla acá
    le sacaría la coordinación de abajo al resto del proceso. Un fix que sólo
    mirara el primer caso rompería el segundo: es el hermano exacto.

    Se ejerce el método sin construir un supervisor: el grafo completo no aporta
    nada a esta regla y haría el test imposible de correr sin I/O real.
    """
    from sky_claw.app.orchestrator.supervisor import SupervisorAgent

    supervisor = SupervisorAgent.__new__(SupervisorAgent)
    espia = _CoordinacionEspia()
    supervisor._stage9_coordination = espia
    supervisor._owns_stage9_coordination = propia

    await supervisor._cerrar_coordinacion_propia()

    assert espia.cierres == cierres_esperados


def test_el_apagado_del_supervisor_cierra_la_coordinacion() -> None:
    """Ancla de cableado: el método existe Y el apagado lo llama.

    Sin esta mitad, el método de arriba podía quedar correcto y muerto — la
    forma más silenciosa del defecto, porque el test de comportamiento sigue en
    verde mientras la conexión queda viva en producción.
    """
    import ast as _ast

    from sky_claw.app.orchestrator import supervisor as modulo

    arbol = _ast.parse(pathlib.Path(modulo.__file__).read_text(encoding="utf-8"))
    start = next(n for n in _ast.walk(arbol) if isinstance(n, _ast.AsyncFunctionDef) and n.name == "start")
    finales = [n for n in _ast.walk(start) if isinstance(n, _ast.Try) for n in n.finalbody]
    llamadas = {
        n.func.attr
        for bloque in finales
        for n in _ast.walk(bloque)
        if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)
    }
    assert "_cerrar_coordinacion_propia" in llamadas, (
        "el apagado del supervisor no cierra la coordinación de etapa 9 que construyó"
    )
    # Y la propiedad se decide por el parámetro, no por inspeccionar el objeto.
    init = next(n for n in _ast.walk(arbol) if isinstance(n, _ast.FunctionDef) and n.name == "__init__")
    fuente_init = _ast.unparse(init)
    assert "self._owns_stage9_coordination = stage9_coordination is None" in fuente_init


async def test_el_reconciliador_rutea_el_ritual_de_etapa9_a_la_base_durable(
    tmp_path: pathlib.Path,
) -> None:
    """El guard de recovery mira la MISMA base donde el servicio toma el ritual.

    Es el hermano del cableado del servicio: si el reconciliador siguiera
    mirando el `locks.db` relativo al cwd, vería LIBRE un ritual en vuelo en
    otra instancia y restauraría un backup que su productor todavía usa.
    """
    from sky_claw.app.db.locks import DistributedLockManager
    from sky_claw.local.tools.rollback_reconciler import _manager_del_ritual

    por_defecto = DistributedLockManager(db_path=tmp_path / "cwd_locks.db")
    coordinacion = ws.construir_coordinacion_de_etapa9(base=tmp_path / "estado")
    try:
        assert await _manager_del_ritual("dyndolod-pipeline", por_defecto, coordinacion) is (
            await coordinacion.manager_del_ritual()
        )
        # Los otros rituales NO se migran incidentalmente.
        for otro in ("behavior-graphs", "bodyslide-meshes", "load-order"):
            assert await _manager_del_ritual(otro, por_defecto, coordinacion) is por_defecto
        # Y sin coordinación cableada, todo conserva el comportamiento previo.
        assert await _manager_del_ritual("dyndolod-pipeline", por_defecto, None) is por_defecto
    finally:
        await coordinacion.close()


async def test_el_manager_que_entrega_la_coordinacion_ya_esta_abierto(
    tmp_path: pathlib.Path,
) -> None:
    """Ruteado NO alcanza: el manager tiene que poder CONSULTARSE.

    RED contra el defecto real: la coordinación abre su DB de forma perezosa y
    entregaba el manager crudo por una property síncrona. El único camino que la
    abría era `sostener_workspace`, que NO corre cuando `external_work_root`
    está sin configurar — el default. El guard de reconciliación del arranque
    recibía entonces un manager cerrado, cuyo `LockError` abortaba el barrido
    ENTERO dentro de un boundary best-effort que lo volvía invisible.

    Se ejerce sobre el manager recién entregado, sin `initialize()` del caller:
    esa es exactamente la precondición que el mecanismo tiene que cumplir solo.
    """
    coordinacion = ws.construir_coordinacion_de_etapa9(base=tmp_path / "estado")
    try:
        manager = await coordinacion.manager_del_ritual()
        assert await manager.get_lock_info(ws.Stage9Coordination.RECURSO_DEL_RITUAL) is None
    finally:
        await coordinacion.close()


def test_la_coordinacion_no_expone_el_manager_por_una_property_sincrona() -> None:
    """Ancla de forma: no hay acceso que pueda entregar el manager sin abrirlo.

    Enunciado como propiedad del mecanismo y no como recordatorio: mientras
    exista un accesor SÍNCRONO, algún camino nuevo lo va a usar y volverá a
    correr sin exclusión. El accesor async no tiene esa falla posible porque no
    puede devolver sin haber esperado la apertura.
    """
    accesor = inspect.getattr_static(ws.Stage9Coordination, "manager_del_ritual")
    assert inspect.iscoroutinefunction(accesor), "el accesor del manager dejó de ser async"
    assert not isinstance(inspect.getattr_static(ws.Stage9Coordination, "lock_manager", None), property), (
        "volvió una property síncrona que entrega el manager sin abrirlo"
    )


async def test_el_barrido_de_arranque_corre_con_una_coordinacion_recien_construida(
    tmp_path: pathlib.Path,
) -> None:
    """Reproducción end-to-end del defecto: el recovery de U-08 se desactivaba.

    Escenario del default productivo: `external_work_root` sin configurar, así
    que nadie resolvió el workspace y la coordinación nunca se abrió. El barrido
    tiene que reconciliar igual — y no sólo el productor de DynDOLOD: el `for`
    de productores abortaba en el primero, dejando a Pandora, BodySlide y el
    clon de sandbox sin barrer en el mismo arranque.
    """
    from sky_claw.app.db.locks import DistributedLockManager
    from sky_claw.local.tools.rollback_reconciler import (
        SUFIJO_MOVE_ASIDE,
        ProductorDeMoveAside,
        reconcile_orphan_rollback_backups,
    )

    def _sembrar_backup(destino: pathlib.Path, nonce: str) -> None:
        backup = destino.with_name(f"{destino.name}.rollback-{nonce}")
        # El nombre se valida contra la MISMA regex que usa el reconciliador: un
        # backup sembrado que no matchea haría pasar el test sin barrer nada.
        assert SUFIJO_MOVE_ASIDE.search(backup.name), backup.name
        backup.mkdir(parents=True)
        (backup / "marcador.txt").write_text("generación previa", encoding="utf-8")

    destino = tmp_path / "mods" / "DynDOLOD Output"
    _sembrar_backup(destino, "170000000000")
    otro_destino = tmp_path / "mods" / "Pandora Output"
    _sembrar_backup(otro_destino, "170000000001")

    por_defecto = DistributedLockManager(db_path=tmp_path / "cwd_locks.db")
    await por_defecto.initialize()
    coordinacion = ws.construir_coordinacion_de_etapa9(base=tmp_path / "estado")
    try:
        resultado = await reconcile_orphan_rollback_backups(
            productores=[
                ProductorDeMoveAside(
                    nombre="dyndolod",
                    lock_resource_id=ws.Stage9Coordination.RECURSO_DEL_RITUAL,
                    destinos=(destino,),
                ),
                ProductorDeMoveAside(
                    nombre="pandora",
                    lock_resource_id="behavior-graphs",
                    destinos=(otro_destino,),
                ),
            ],
            sandbox_root=None,
            lock_manager=por_defecto,
            coordinacion_etapa9=coordinacion,
        )
    finally:
        await coordinacion.close()
        await por_defecto.close()

    assert destino in resultado.restaurados, "el productor coordinado no se reconcilió"
    assert otro_destino in resultado.restaurados, "el hermano quedó sin barrer al abortar el loop"
    assert (destino / "marcador.txt").read_text(encoding="utf-8") == "generación previa"


def test_el_health_cli_arranca_sin_external_work_root(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Consumidor real de `Config`: la ausencia del campo no lo rompe.

    `__main__._parse_args` construye un `Config` de verdad para derivar defaults
    de la CLI (entre ellos el chat id de Telegram). Es el consumidor que el plan
    de P0 pide comprobar, y comprueba algo concreto: que el campo nuevo entre por
    `_load_defaults()` y no por un `getattr` disperso que el primer caller en
    olvidarlo convertiría en `AttributeError` durante el arranque.
    """
    from sky_claw import __main__ as cli
    from sky_claw.config import Config

    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\nmo2_root = "D:/MO2"\n', encoding="utf-8")
    monkeypatch.setattr(Config, "DEFAULT_CONFIG_FILE", config_path)
    monkeypatch.setattr(Config, "DEFAULT_CONFIG_DIR", tmp_path)

    assert "external_work_root" not in config_path.read_text(encoding="utf-8")

    args = cli._parse_args(["--mode", "vfs-health", "--skyrim-path", str(tmp_path / "game")])

    assert args.mode == "vfs-health"
    assert Config(config_path).external_work_root == ""


# ---------------------------------------------------------------------------
# El event loop no se bloquea (§2.1 de `.github/coding_conventions.md`)
# ---------------------------------------------------------------------------

#: Helpers SÍNCRONOS del módulo que hacen I/O de disco (leer, escribir, recorrer,
#: `fsync`, `resolve`) o duermen. Cualquier llamada a uno de estos desde una
#: función `async` del módulo tiene que ir por `asyncio.to_thread`.
#:
#: Se enumera en vez de muestrear a propósito: el revisor adversarial encontró
#: DOS de estas ocho en el PR original, y arreglar sólo las dos nombradas es
#: exactamente el defecto dominante de este repo. El ancla falla con la novena.
HELPERS_BLOQUEANTES: frozenset[str] = frozenset(
    {
        "admitir_root",
        "leer_binding",
        "publicar_binding",
        "reescribir_binding_propio",
        "evaluar_estado",
        "exigir_veredicto",
        "validar_destino_administrado",
        "inspeccionar_root_para_transicion",
        "entrada",
        "leer",
        "buscar_por_binding_id",
        "registrar_activa",
        "registrar_transicion",
    }
)


def test_ninguna_funcion_async_hace_io_de_disco_en_el_loop() -> None:
    """Ancla de clase: I/O de disco desde `async` sólo vía `asyncio.to_thread`.

    `.github/coding_conventions.md` §2.1 lo dice sin matices —"No bloquear el
    event loop: I/O bloqueante (subprocesos, disco...) vía `asyncio.to_thread`"
    y "Prohibido `time.sleep()` en código async"— pero ninguna herramienta del
    repo lo verifica: ni ruff ni mypy ven esta clase. Por eso el PR original la
    violó en ocho lugares con todo en verde, y por eso la regla necesita este
    gate y no otro párrafo.
    """
    import ast

    ruta = pathlib.Path(ws.__file__)
    arbol = ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))

    ofensores: list[str] = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.AsyncFunctionDef):
            continue
        # Las llamadas que YA van envueltas: `asyncio.to_thread(helper, ...)`
        # pasa el helper como ARGUMENTO, no como `func`, así que basta mirar
        # quién está en posición de llamada.
        for sub in ast.walk(nodo):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            nombre = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
            if nombre in HELPERS_BLOQUEANTES:
                ofensores.append(f"{nodo.name} → {nombre}()")
            if (
                nombre == "sleep"
                and isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "time"
            ):
                ofensores.append(f"{nodo.name} → time.sleep()")

    assert not ofensores, (
        f"I/O de disco (o time.sleep) en el event loop; envolver con asyncio.to_thread: {sorted(ofensores)}"
    )


def test_el_detector_de_bloqueo_reconoce_las_dos_formas() -> None:
    """El ancla de arriba no sirve si su detector es ciego: se prueba con casos.

    Sin esto, un detector roto reportaría cero ofensores para siempre y la
    regla volvería a ser prosa — que es el modo de falla que el propio
    `AGENTS.md` describe.
    """
    import ast

    def _ofensores(fuente: str) -> list[str]:
        arbol = ast.parse(fuente)
        hallados: list[str] = []
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.AsyncFunctionDef):
                continue
            for sub in ast.walk(nodo):
                if not isinstance(sub, ast.Call):
                    continue
                func = sub.func
                nombre = (
                    func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
                )
                if nombre in HELPERS_BLOQUEANTES:
                    hallados.append(nombre)
                if (
                    nombre == "sleep"
                    and isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "time"
                ):
                    hallados.append("time.sleep")
        return hallados

    desnudo = "async def f():\n    leer_binding(root)\n"
    envuelto = "async def f():\n    await asyncio.to_thread(leer_binding, root)\n"
    durmiendo = "import time\nasync def f():\n    time.sleep(1)\n"
    dormido_bien = "import asyncio\nasync def f():\n    await asyncio.sleep(1)\n"
    sincrono = "def f():\n    leer_binding(root)\n"

    assert _ofensores(desnudo) == ["leer_binding"]
    assert _ofensores(envuelto) == []
    assert _ofensores(durmiendo) == ["time.sleep"]
    assert _ofensores(dormido_bien) == []
    assert _ofensores(sincrono) == [], "una función sync puede bloquear: no es su problema"


async def test_resolver_workspace_no_congela_el_event_loop(tmp_path: pathlib.Path) -> None:
    """La prueba de comportamiento del ancla anterior: el loop sigue latiendo.

    Un inspector deliberadamente lento (el caso real: recorrer GB de
    generaciones viejas) corre mientras un ticker cuenta en el mismo loop. Si la
    inspección ocurriera en el loop, el ticker no avanzaría ni una vez.
    """
    import asyncio

    recursos = _instancia(tmp_path)
    registro = _registro(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    viejo = tmp_path / "Work Viejo"
    nuevo = tmp_path / "Work Nuevo"

    def _inspector_lento(root: pathlib.Path) -> ws.InspeccionDeRootViejo:
        import time as _time

        _time.sleep(0.4)
        return ws.inspeccionar_root_para_transicion(root)

    latidos = 0

    async def _ticker() -> None:
        nonlocal latidos
        while True:
            await asyncio.sleep(0.01)
            latidos += 1

    try:
        primero = await ws.resolver_workspace(
            preferencia=str(viejo),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        assert primero is not None
        await primero.ownership.liberar()

        tarea = asyncio.create_task(_ticker())
        try:
            resuelto = await ws.resolver_workspace(
                preferencia=str(nuevo),
                recursos=recursos,
                prohibidas=_prohibidas(tmp_path),
                registro=registro,
                coordinacion=coordinacion,
                inspector=_inspector_lento,
            )
        finally:
            tarea.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tarea

        assert resuelto is not None
        await resuelto.ownership.liberar()

        # Con el inspector de 0.4 s fuera del loop, el ticker de 10 ms tuvo que
        # latir muchas veces. Si corriera EN el loop, quedaría cerca de cero.
        assert latidos >= 10, f"el event loop quedó bloqueado durante la resolución ({latidos} latidos)"
    finally:
        await coordinacion.close()


async def test_perder_la_lease_del_workspace_aborta_antes_de_escribir(
    tmp_path: pathlib.Path,
) -> None:
    """Fail-closed de la coordinación de arranque: sin lease no se registra nada.

    La versión original tomaba el lock con `acquire_lock` crudo y TTL fijo, sin
    heartbeat: si la resolución tardaba más que el TTL, la lease expiraba en
    silencio y otro proceso podía entrar justo mientras se publica el binding y
    se reescribe el registro. Ahora se reconfirma la propiedad ANTES de cada
    escritura crítica, así que perderla aborta en vez de escribir con una
    exclusión que ya no existe.
    """
    from sky_claw.app.db.locks import LockLeaseLostError

    recursos = _instancia(tmp_path)
    registro = _registro(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    root = tmp_path / "Sky-Claw Work"

    try:
        await coordinacion.initialize()

        # Otro dueño se lleva el lock del workspace ANTES de que arranque la
        # resolución: es la forma determinista de reproducir "la lease ya no es
        # tuya", sin competir con el heartbeat ni depender de timings.
        await (await coordinacion.manager_del_ritual()).acquire_lock(
            coordinacion.RECURSO_DEL_WORKSPACE, "intruso", ttl=120.0
        )

        with pytest.raises((LockLeaseLostError, ws.WorkspaceRechazadoError)):
            await ws.resolver_workspace(
                preferencia=str(root),
                recursos=recursos,
                prohibidas=_prohibidas(tmp_path),
                registro=registro,
                coordinacion=coordinacion,
            )

        # Y NO quedó registro de root activo escrito bajo una lease ajena.
        assert registro.entrada(recursos.clave()) is None
    finally:
        await (await coordinacion.manager_del_ritual()).force_release(coordinacion.RECURSO_DEL_WORKSPACE)
        await coordinacion.close()


def test_la_lease_de_ownership_es_por_instancia_logica_no_por_root(tmp_path: pathlib.Path) -> None:
    """§17/§18: X+rootA y X+rootB compiten por el MISMO recurso; X e Y no.

    El resource_id de la lease larga se deriva de `ResourceBinding.clave()`
    (la identidad durable de la instancia lógica), nunca del pathname del
    root ni de un recurso global único: dos roots para la misma instancia
    tienen que excluirse y dos instancias no deben serializarse entre sí.
    """
    recursos = _instancia(tmp_path)
    misma = _instancia(tmp_path)
    otra = _instancia(tmp_path, sufijo="_otra")

    rid = ws.Stage9Coordination.resource_id_de_ownership(recursos.clave())
    assert ws.Stage9Coordination.RECURSO_DEL_OWNERSHIP_VIVO == "dyndolod-ownership"
    assert rid.startswith("dyndolod-ownership-")
    assert rid == ws.Stage9Coordination.resource_id_de_ownership(misma.clave())
    assert rid != ws.Stage9Coordination.resource_id_de_ownership(otra.clave())


async def test_cada_adquisicion_tiene_una_identidad_de_owner_distinta(
    tmp_path: pathlib.Path,
) -> None:
    """§6: owner por ADQUISICIÓN, no por coordinación ni por proceso.

    `release_lock`/`renew_lock` matchean por ``resource_id + agent_id``. Si el
    agent fuera compartido entre adquisiciones de la misma coordinación, un
    handle VIEJO que se limpie después de una readquisición borraría la lease
    NUEVA (el SQL no mira `acquired_at`). Agent fresco por adquisición cierra
    la clase; el token `acquired_at` de `assert_owned` cierra la readquisición
    entre procesos. Revisión Codex/Copilot, finding P2.
    """
    from sky_claw.app.db.locks import LockLeaseLostError

    recursos = _instancia(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    try:
        viejo = await coordinacion.adquirir_ownership_vivo(recursos=recursos, ttl=0.3, auto_renew=False)
        await asyncio.sleep(0.4)  # expiró sin renovación

        nuevo = await coordinacion.adquirir_ownership_vivo(recursos=recursos)
        assert viejo.agent_id != nuevo.agent_id

        # El handle viejo NO puede liberar la lease nueva: agent distinto.
        await viejo.liberar()
        await nuevo.assert_owned()
        # Y el snapshot viejo queda fenced.
        with pytest.raises(LockLeaseLostError):
            await viejo.assert_owned()
        await nuevo.liberar()
    finally:
        await coordinacion.close()


async def test_el_snapshot_sin_ownership_vivo_no_puede_mutar(tmp_path: pathlib.Path) -> None:
    """§16: snapshot viejo + ownership perdido => `assert_owned()` fail-closed.

    No alcanza con probar que B no entra: el `WorkspaceResuelto` que A conserva
    tiene que quedar FENCED cuando su lease deja de ser suya. La pérdida es REAL
    (otro dueño reclama el recurso por debajo), no un flag simulado.
    """
    from sky_claw.app.db.locks import LockLeaseLostError

    recursos = _instancia(tmp_path)
    registro = _registro(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    root = tmp_path / "Sky-Claw Work"
    rid = ws.Stage9Coordination.resource_id_de_ownership(recursos.clave())
    try:
        resuelto = await ws.resolver_workspace(
            preferencia=str(root),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        assert resuelto is not None and resuelto.ownership is not None
        await resuelto.assert_owned()  # lease válida => OK

        manager = await coordinacion.manager_del_ritual()
        await manager.force_release(rid)
        await manager.acquire_lock(rid, "intruso", ttl=120.0)

        with pytest.raises(LockLeaseLostError):
            await resuelto.assert_owned()
    finally:
        await (await coordinacion.manager_del_ritual()).force_release(rid)
        await coordinacion.close()


async def test_el_segundo_resolver_del_mismo_binding_rechaza_ocupado(
    tmp_path: pathlib.Path,
) -> None:
    """§17: mismo `resource_binding`, otro root => OCUPADO mientras el primero vive."""
    recursos = _instancia(tmp_path)
    registro = _registro(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    root_a = tmp_path / "Work A"
    root_b = tmp_path / "Work B"
    try:
        primero = await ws.resolver_workspace(
            preferencia=str(root_a),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        assert primero is not None

        with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
            await ws.resolver_workspace(
                preferencia=str(root_b),
                recursos=recursos,
                prohibidas=_prohibidas(tmp_path),
                registro=registro,
                coordinacion=coordinacion,
            )
        assert excinfo.value.motivo is ws.MotivoDeRechazo.OCUPADO
        assert registro.entrada(recursos.clave()).root == str(root_a.resolve())

        # Al liberar el primero, el segundo puede avanzar (transición validada).
        await primero.ownership.liberar()
        segundo = await ws.resolver_workspace(
            preferencia=str(root_b),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        assert segundo is not None and segundo.root == root_b.resolve()
        assert registro.entrada(recursos.clave()).root == str(root_b.resolve())
        await segundo.ownership.liberar()
    finally:
        await coordinacion.close()


async def test_dos_bindings_distintos_sostienen_ownership_simultaneo(
    tmp_path: pathlib.Path,
) -> None:
    """§18: X e Y coexisten; no hay un lock de vida global entre instalaciones."""
    recursos_x = _instancia(tmp_path)
    recursos_y = _instancia(tmp_path, sufijo="_otra")
    registro = _registro(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    try:
        x = await ws.resolver_workspace(
            preferencia=str(tmp_path / "Work X"),
            recursos=recursos_x,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        y = await ws.resolver_workspace(
            preferencia=str(tmp_path / "Work Y"),
            recursos=recursos_y,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        assert x is not None and y is not None
        assert x.ownership.resource_id != y.ownership.resource_id

        await x.assert_owned()
        await y.assert_owned()
        manager = await coordinacion.manager_del_ritual()
        info_x = await manager.get_lock_info(x.ownership.resource_id)
        info_y = await manager.get_lock_info(y.ownership.resource_id)
        assert info_x is not None and not info_x.is_expired
        assert info_y is not None and not info_y.is_expired

        await x.ownership.liberar()
        await y.ownership.liberar()
    finally:
        await coordinacion.close()


async def test_el_heartbeat_mantiene_viva_la_lease_de_ownership(
    tmp_path: pathlib.Path,
) -> None:
    """§20: lease viva > TTL nominal porque el heartbeat renueva."""
    recursos = _instancia(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    try:
        ownership = await coordinacion.adquirir_ownership_vivo(recursos=recursos, ttl=0.3)
        await asyncio.sleep(0.6)  # el doble del TTL nominal
        await ownership.assert_owned()
        await ownership.liberar()
    finally:
        await coordinacion.close()


async def test_un_holder_muerto_no_bloquea_la_readquisicion(tmp_path: pathlib.Path) -> None:
    """§20: sin renovación (holder muerto), otro proceso readquiere tras expirar.

    Dos COORDINACIONES distintas = dos procesos. El holder deja de renovar
    (`auto_renew=False` es la muerte dura sin cleanup), su lease expira, el
    nuevo adquiere. Además la identidad de owner por proceso cierra la clase
    "A libera la lease de B": el holder muerto no puede tocar la lease ajena.
    """
    from sky_claw.app.db.locks import LockLeaseLostError

    recursos = _instancia(tmp_path)
    coordinacion_a = _coordinacion(tmp_path)
    coordinacion_b = _coordinacion(tmp_path)
    try:
        muerto = await coordinacion_a.adquirir_ownership_vivo(recursos=recursos, ttl=0.4, auto_renew=False)
        await asyncio.sleep(0.7)  # expiró: nadie renovó

        vivo = await coordinacion_b.adquirir_ownership_vivo(recursos=recursos)
        await vivo.assert_owned()

        # El snapshot del holder muerto queda fenced.
        with pytest.raises(LockLeaseLostError):
            await muerto.assert_owned()

        # Y su `liberar` no puede liberar la lease del nuevo dueño.
        await muerto.liberar()
        await vivo.assert_owned()
        await vivo.liberar()
    finally:
        await coordinacion_a.close()
        await coordinacion_b.close()


async def test_el_rechazo_post_ownership_libera_la_lease(tmp_path: pathlib.Path) -> None:
    """§21: un startup que falla después de adquirir ownership libera la lease."""
    recursos = _instancia(tmp_path)
    registro = _registro(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    viejo = tmp_path / "Work Viejo"
    nuevo = tmp_path / "Work Nuevo"
    rid = ws.Stage9Coordination.resource_id_de_ownership(recursos.clave())
    try:
        primero = await ws.resolver_workspace(
            preferencia=str(viejo),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        assert primero is not None
        await primero.ownership.liberar()

        _sabotear_con_backups(viejo)
        with pytest.raises(ws.WorkspaceRechazadoError) as excinfo:
            await ws.resolver_workspace(
                preferencia=str(nuevo),
                recursos=recursos,
                prohibidas=_prohibidas(tmp_path),
                registro=registro,
                coordinacion=coordinacion,
            )
        assert excinfo.value.motivo is ws.MotivoDeRechazo.TRANSICION_REQUERIDA

        manager = await coordinacion.manager_del_ritual()
        assert await manager.get_lock_info(rid) is None, "el rechazo dejó la lease viva"
    finally:
        await coordinacion.close()


async def test_la_cancelacion_durante_la_resolucion_libera_la_lease(
    tmp_path: pathlib.Path,
) -> None:
    """§21: cancelación a mitad de resolución => cleanup correcto, sin lease viva."""
    recursos = _instancia(tmp_path)
    registro = _registro(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    viejo = tmp_path / "Work Viejo"
    nuevo = tmp_path / "Work Nuevo"
    rid = ws.Stage9Coordination.resource_id_de_ownership(recursos.clave())

    def _inspector_lento(root: pathlib.Path) -> ws.InspeccionDeRootViejo:
        time.sleep(1.0)
        return ws.inspeccionar_root_para_transicion(root)

    try:
        primero = await ws.resolver_workspace(
            preferencia=str(viejo),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        assert primero is not None
        await primero.ownership.liberar()

        tarea = asyncio.create_task(
            ws.resolver_workspace(
                preferencia=str(nuevo),
                recursos=recursos,
                prohibidas=_prohibidas(tmp_path),
                registro=registro,
                coordinacion=coordinacion,
                inspector=_inspector_lento,
            )
        )
        await asyncio.sleep(0.15)
        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea

        manager = await coordinacion.manager_del_ritual()
        assert await manager.get_lock_info(rid) is None, "la cancelación dejó la lease viva"
    finally:
        await coordinacion.close()


async def test_perder_la_lease_de_ownership_aborta_la_resolucion_sin_escribir(
    tmp_path: pathlib.Path,
) -> None:
    """§13: lease de ownership perdida a MEDIA resolución => aborta sin escribir.

    La sonda de §25 corre DENTRO de la validación de transición, con la lease
    de ownership ya tomada: acá roba el recurso por debajo. El fence la detecta
    antes de `registrar_transicion`/`registrar_activa`, y el registro durable
    no queda repuntado a la raíz nueva.
    """
    from sky_claw.app.db.locks import LockLeaseLostError

    recursos = _instancia(tmp_path)
    registro = _registro(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    viejo = tmp_path / "Work Viejo"
    nuevo = tmp_path / "Work Nuevo"
    rid = ws.Stage9Coordination.resource_id_de_ownership(recursos.clave())

    async def _sonda_que_roba() -> bool:
        manager = await coordinacion.manager_del_ritual()
        await manager.force_release(rid)
        await manager.acquire_lock(rid, "intruso", ttl=120.0)
        return False

    try:
        primero = await ws.resolver_workspace(
            preferencia=str(viejo),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        assert primero is not None
        await primero.ownership.liberar()

        with pytest.raises(LockLeaseLostError):
            await ws.resolver_workspace(
                preferencia=str(nuevo),
                recursos=recursos,
                prohibidas=_prohibidas(tmp_path),
                registro=registro,
                coordinacion=coordinacion,
                sonda_de_transaccion_pendiente=_sonda_que_roba,
            )

        entrada = registro.entrada(recursos.clave())
        assert entrada is not None and entrada.root == str(viejo.resolve())
        assert entrada.transicion_pendiente is None, "la transición no se registró bajo una lease ajena"
    finally:
        await (await coordinacion.manager_del_ritual()).force_release(rid)
        await coordinacion.close()


def test_el_cleanup_del_ownership_vivo_cierra_despues_de_resolver_y_antes_de_la_coordinacion() -> None:
    """§11: el orden de teardown está congelado, no escrito en prosa.

    La lease de ownership se adquiere en la resolución y se libera en el
    cleanup del `AppContext`. El registro del cierre de la coordinación —la
    DB que sostiene la lease— está ANTES de la resolución en el fuente, y el
    de la liberación DESPUÉS: el LIFO del exit stack hace que la liberación
    corra después de cerrar los consumidores del runtime y antes de que la DB
    de la lease se cierre. Reordenar cualquiera de las dos rompe este ancla.
    """
    import ast as _ast

    import sky_claw.app_context as app_context_mod

    fuente = pathlib.Path(app_context_mod.__file__).read_text(encoding="utf-8")
    arbol = _ast.parse(fuente)
    inner = next(n for n in _ast.walk(arbol) if isinstance(n, _ast.AsyncFunctionDef) and n.name == "_start_full_inner")

    posicion_resolucion: int | None = None
    push_liberar: int | None = None
    push_coordinacion: int | None = None
    for nodo in _ast.walk(inner):
        if not isinstance(nodo, _ast.Call):
            continue
        if isinstance(nodo.func, _ast.Attribute) and nodo.func.attr == "_resolver_workspace_de_dyndolod":
            posicion_resolucion = nodo.lineno
            continue
        es_push_cleanup = (isinstance(nodo.func, _ast.Name) and nodo.func.id == "_push_startup_cleanup") or (
            isinstance(nodo.func, _ast.Attribute) and nodo.func.attr == "_push_startup_cleanup"
        )
        if not es_push_cleanup or not nodo.args:
            continue
        arg = nodo.args[0]
        if not isinstance(arg, _ast.Attribute):
            continue
        if arg.attr == "_liberar_ownership_del_workspace":
            push_liberar = nodo.lineno
        if arg.attr == "close" and isinstance(arg.value, _ast.Attribute) and arg.value.attr == "stage9_coordination":
            push_coordinacion = nodo.lineno

    assert posicion_resolucion is not None, "el resolver dejó de correr en _start_full_inner"
    assert push_coordinacion is not None, "la coordinación ya no se cierra en el cleanup del arranque"
    assert push_liberar is not None, "la lease de ownership ya no se libera en el cleanup del arranque"
    assert push_coordinacion < posicion_resolucion < push_liberar, (
        "el cierre de la coordinación debe registrarse antes de resolver, "
        "y la liberación del ownership después (LIFO del exit stack)"
    )


async def test_el_helper_de_liberacion_suelta_la_lease_del_snapshot(
    tmp_path: pathlib.Path,
) -> None:
    """§10/§11: `_liberar_ownership_del_workspace` (que `AppContext` registra)
    suelta la lease larga; el snapshot queda sin derecho a mutar."""
    from sky_claw.app_context import AppContext

    recursos = _instancia(tmp_path)
    registro = _registro(tmp_path)
    coordinacion = _coordinacion(tmp_path)
    root = tmp_path / "Sky-Claw Work"
    rid = ws.Stage9Coordination.resource_id_de_ownership(recursos.clave())
    try:
        resuelto = await ws.resolver_workspace(
            preferencia=str(root),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        assert resuelto is not None

        manager = await coordinacion.manager_del_ritual()
        assert await manager.get_lock_info(rid) is not None

        await AppContext._liberar_ownership_del_workspace(resuelto)

        assert await manager.get_lock_info(rid) is None, "la liberación no soltó la lease"
    finally:
        await coordinacion.close()


async def test_preferencia_ausente_no_adquiere_lease_de_ownership(
    tmp_path: pathlib.Path,
) -> None:
    """§22: sin `external_work_root` no se adquiere ownership innecesario."""
    coordinacion = _coordinacion(tmp_path)
    try:
        resuelto = await ws.resolver_workspace(
            preferencia=None,
            recursos=_instancia(tmp_path),
            prohibidas=_prohibidas(tmp_path),
            registro=_registro(tmp_path),
            coordinacion=coordinacion,
        )
        assert resuelto is None
        rid = ws.Stage9Coordination.resource_id_de_ownership(_instancia(tmp_path).clave())
        manager = await coordinacion.manager_del_ritual()
        assert await manager.get_lock_info(rid) is None
    finally:
        await coordinacion.close()


def test_las_escrituras_del_registro_van_fenceadas_por_el_ownership() -> None:
    """Ancla de hermanos: `registrar_transicion` y `registrar_activa` tienen
    fence de ownership previo en `resolver_workspace`.

    Las dos son mutaciones del registro durable; una sin fence reintroduciría
    el defecto de este repo —el fix en un camino, su gemelo intacto— con la
    forma más silenciosa posible.
    """
    import ast

    fuente = pathlib.Path(ws.__file__).read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    resolver = next(
        n for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef) and n.name == "resolver_workspace"
    )
    escrituras: list[tuple[int, str]] = []
    fences_previos: list[int] = []
    for nodo in ast.walk(resolver):
        # Las escrituras viajan como argumento de `asyncio.to_thread(...)`, así
        # que no son nodos `Call`: se detecta la REFERENCIA
        # `registro.registrar_*` donde aparezca.
        if (
            isinstance(nodo, ast.Attribute)
            and nodo.attr in ("registrar_transicion", "registrar_activa")
            and isinstance(nodo.value, ast.Name)
            and nodo.value.id == "registro"
        ):
            escrituras.append((nodo.lineno, nodo.attr))
        if (
            isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Attribute)
            and nodo.func.attr == "assert_owned"
            and isinstance(nodo.func.value, ast.Name)
            and nodo.func.value.id == "ownership"
        ):
            fences_previos.append(nodo.lineno)
    assert escrituras, "no hay escrituras del registro en resolver_workspace"
    for linea, nombre in escrituras:
        anteriores = [f for f in fences_previos if f < linea]
        assert anteriores, f"{nombre} (línea {linea}) no tiene fence de ownership previo"


def test_la_liberacion_del_ownership_envuelve_al_async_with_del_workspace() -> None:
    """Ancla de forma (finding P1 de Codex/Copilot): la liberación del ownership
    envuelve el `async with sostener_workspace`, no vive adentro.

    Si el `__aexit__` del lock corto levanta —lease del workspace perdida entre
    el último fence y la salida del contexto— la excepción nace FUERA de
    cualquier try anidado al `async with`. Una liberación anidada adentro
    dejaría la lease de ownership huérfana hasta su TTL. El try que contiene el
    `async with` tiene que ser el que libera.
    """

    def _contiene(nodo: ast.AST, contenedor: ast.AST) -> bool:
        return any(n is nodo for n in ast.walk(contenedor))

    import ast

    fuente = pathlib.Path(ws.__file__).read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    resolver = next(
        n for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef) and n.name == "resolver_workspace"
    )
    con_workspace = next(
        n
        for n in ast.walk(resolver)
        if isinstance(n, ast.AsyncWith)
        and isinstance(n.items[0].context_expr, ast.Call)
        and isinstance(n.items[0].context_expr.func, ast.Attribute)
        and n.items[0].context_expr.func.attr == "sostener_workspace"
    )
    try_externo = next(n for n in ast.walk(resolver) if isinstance(n, ast.Try) and _contiene(con_workspace, n))
    libera_el_ownership = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "liberar"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "ownership"
        for handler in try_externo.handlers
        for n in ast.walk(handler)
    )
    assert libera_el_ownership, (
        "la liberación del ownership no está en el try que envuelve el `async with`: "
        "un `__aexit__` que levante filtraría la lease hasta su TTL"
    )


_GUION_OWNERSHIP_VIVO = """\
import json, os, pathlib, sys, time
sys.path.insert(0, {raiz!r})
import asyncio
from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.tools import dyndolod_workspace as ws

estado = pathlib.Path(sys.argv[1])
salida = pathlib.Path(sys.argv[2])
señales = pathlib.Path(sys.argv[3])
rol = sys.argv[4]
root = pathlib.Path(sys.argv[5])
ttl = float(sys.argv[6])
crash = sys.argv[7] == "1"
game, mo2data, mods = (pathlib.Path(sys.argv[8]), pathlib.Path(sys.argv[9]), pathlib.Path(sys.argv[10]))
temp = pathlib.Path(sys.argv[11])
cwd = pathlib.Path(sys.argv[12])
os.chdir(cwd)

recursos = ws.ResourceBinding.desde_paths(
    game_path=game, mo2_instance_data_root=mo2data, mo2_mods_path=mods
)
prohibidas = ws.RaicesProhibidas.desde_entorno(
    game=game,
    mo2_install=pathlib.Path(mo2data).parent / "mo2_install",
    mo2_instance_data_root=mo2data,
    mo2_mods_path=mods,
    dyndolod_exe=temp.parent / "tools" / "DynDOLOD" / "DynDOLODx64.exe",
    texgen_exe=temp.parent / "tools" / "TexGen" / "TexGenx64.exe",
    temp_dir=temp,
    known_folders_prohibidos=(),
)


def _coordinacion():
    directorio = ws.ruta_de_estado_de_etapa9(estado)
    directorio.mkdir(parents=True, exist_ok=True)
    return ws.Stage9Coordination(
        lock_manager=DistributedLockManager(
            db_path=directorio / "stage9_locks.db",
            max_retries=3,
            backoff_base=0.05,
            backoff_max=0.1,
        ),
        snapshot_manager=FileSnapshotManager(snapshot_dir=directorio / "snapshots"),
    )


def _esperar(marcador: pathlib.Path, plazo: float = 90.0) -> None:
    limite = time.monotonic() + plazo
    while not marcador.exists():
        if time.monotonic() >= limite:
            raise TimeoutError(f"el proceso no vio {{marcador.name}}")
        time.sleep(0.02)


async def _resolver(coordinacion, registro):
    return await ws.resolver_workspace(
        preferencia=str(root), recursos=recursos, prohibidas=prohibidas,
        registro=registro, coordinacion=coordinacion,
        ttl_del_ownership_vivo=ttl,
    )


async def main():
    coordinacion = _coordinacion()
    try:
        registro = ws.registro_de_roots_activos(estado)
        if rol == "holder":
            resuelto = await _resolver(coordinacion, registro)
            (señales / "holder_resolvio").write_text("ok", encoding="utf-8")
            while not (señales / "retador_termino").exists():
                await asyncio.sleep(0.02)
            if crash:
                # Muerte dura: sin liberar. La lease queda y expira por TTL.
                salida.write_text(json.dumps({{"ok": True, "root": str(resuelto.root)}}), encoding="utf-8")
                os._exit(0)
            await resuelto.ownership.liberar()
            (señales / "holder_libero").write_text("ok", encoding="utf-8")
            salida.write_text(json.dumps({{"ok": True, "root": str(resuelto.root)}}), encoding="utf-8")
        else:
            _esperar(señales / "holder_resolvio")
            try:
                await _resolver(coordinacion, registro)
                fase1 = {{"entro": True}}
            except ws.WorkspaceRechazadoError as exc:
                fase1 = {{"entro": False, "motivo": exc.motivo.value}}
            except Exception as exc:  # noqa: BLE001 - el guión reporta, el test juzga
                fase1 = {{"entro": False, "error": type(exc).__name__}}
            _entrada = registro.entrada(recursos.clave())
            registro_en_fase1 = None if _entrada is None else _entrada.root
            (señales / "retador_termino").write_text("ok", encoding="utf-8")
            if crash:
                fase2 = None
                for _ in range(200):
                    await asyncio.sleep(0.1)
                    try:
                        await _resolver(coordinacion, registro)
                        fase2 = {{"entro": True}}
                        break
                    except ws.WorkspaceRechazadoError as exc:
                        if exc.motivo.value != "busy":
                            fase2 = {{"entro": False, "motivo": exc.motivo.value}}
                            break
                    except Exception as exc:  # noqa: BLE001
                        fase2 = {{"entro": False, "error": type(exc).__name__}}
                        break
                if fase2 is None:
                    fase2 = {{"entro": False, "error": "timeout-reintentos"}}
            else:
                _esperar(señales / "holder_libero")
                try:
                    await _resolver(coordinacion, registro)
                    fase2 = {{"entro": True}}
                except ws.WorkspaceRechazadoError as exc:
                    fase2 = {{"entro": False, "motivo": exc.motivo.value}}
                except Exception as exc:  # noqa: BLE001
                    fase2 = {{"entro": False, "error": type(exc).__name__}}
            salida.write_text(
                json.dumps({{"fase1": fase1, "fase2": fase2, "registro_en_fase1": registro_en_fase1}}),
                encoding="utf-8",
            )
    finally:
        await coordinacion.close()

asyncio.run(main())
"""


def _correr_ownership_vivo(
    tmp_path: pathlib.Path,
    *,
    estado: pathlib.Path,
    root_a: pathlib.Path,
    root_b: pathlib.Path,
    ttl: float,
    crash: bool,
) -> tuple[dict, dict]:
    """Lanza holder + retador REALES, con cwd distinto, contra el mismo estado."""
    raiz_repo = str(pathlib.Path(ws.__file__).resolve().parents[3])
    guion = tmp_path / "ownership_vivo.py"
    guion.write_text(_GUION_OWNERSHIP_VIVO.format(raiz=raiz_repo), encoding="utf-8")
    señales = tmp_path / "señales_ownership"
    señales.mkdir(exist_ok=True)
    recursos = _instancia(tmp_path)
    cwd_a = tmp_path / "cwd_a"
    cwd_b = tmp_path / "cwd_b"
    cwd_a.mkdir(exist_ok=True)
    cwd_b.mkdir(exist_ok=True)
    procesos: list[subprocess.Popen[bytes]] = []
    for rol, cwd, root in (("holder", cwd_a, root_a), ("retador", cwd_b, root_b)):
        salida = tmp_path / f"ownership_{rol}.json"
        procesos.append(
            subprocess.Popen(  # noqa: S603 - argv fijo, sin shell
                [
                    sys.executable,
                    str(guion),
                    str(estado),
                    str(salida),
                    str(señales),
                    rol,
                    str(root),
                    f"{ttl:.1f}",
                    str(int(crash)),
                    recursos.game_path,
                    recursos.mo2_instance_data_root,
                    recursos.mo2_mods_path,
                    str(tmp_path / "temp"),
                    str(cwd),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
    resultados: list[dict] = []
    try:
        for rol, proceso in (("holder", procesos[0]), ("retador", procesos[1])):
            _, err = proceso.communicate(timeout=180)
            assert proceso.returncode == 0, f"{rol}: {err.decode(errors='replace')}"
            resultados.append(json.loads((tmp_path / f"ownership_{rol}.json").read_text(encoding="utf-8")))
    finally:
        # Un proceso que quedó vivo (esperando una señal que nunca llegó) no
        # puede seguir colgado: su cwd mantiene el tmp_path bloqueado en
        # Windows y envenena la limpieza de pytest.
        for proceso in procesos:
            if proceso.poll() is None:
                proceso.kill()
                proceso.wait(timeout=10)
    return resultados[0], resultados[1]


def test_dos_procesos_ownership_vivo_root_a_vs_root_b(tmp_path: pathlib.Path) -> None:
    """§15: A conserva ownership de X+root A; B intenta activar X+root B.

    DOS PROCESOS reales, cwd distintos, MISMO estado durable: B rechaza
    OCUPADO mientras A vive, el registro sigue apuntando a A, y recién cuando
    A libera (con TODAS las validaciones P0 de transición de por medio) B
    activa su root.
    """
    estado = tmp_path / "estado"
    root_a = tmp_path / "Work A"
    root_b = tmp_path / "Work B"
    holder, retador = _correr_ownership_vivo(
        tmp_path, estado=estado, root_a=root_a, root_b=root_b, ttl=60.0, crash=False
    )

    assert holder["ok"] is True
    assert retador["fase1"] == {"entro": False, "motivo": "busy"}
    assert retador["fase2"] == {"entro": True}

    recursos = _instancia(tmp_path)
    registro = ws.registro_de_roots_activos(estado)
    # Mientras A conservó ownership, el registro NO se movió a B.
    assert retador["registro_en_fase1"] == str(root_a.resolve())
    # Tras liberar y revalidar la transición, B activó su root.
    assert registro.entrada(recursos.clave()).root == str(root_b.resolve())


def test_dos_procesos_ownership_vivo_mismo_root(tmp_path: pathlib.Path) -> None:
    """§23: B con X+root A (el MISMO root) también respeta la exclusividad.

    No basta "un solo pathname en el registro": mientras A vive, B no puede
    ser un segundo holder vivo aunque quiera exactamente el mismo root.
    """
    estado = tmp_path / "estado"
    root_a = tmp_path / "Work A"
    holder, retador = _correr_ownership_vivo(
        tmp_path, estado=estado, root_a=root_a, root_b=root_a, ttl=60.0, crash=False
    )

    assert holder["ok"] is True
    assert retador["fase1"] == {"entro": False, "motivo": "busy"}
    # Tras la liberación limpia de A, B resuelve el mismo root (caso C).
    assert retador["fase2"] == {"entro": True}


def test_dos_procesos_muerte_dura_expira_y_permite_reacquisicion(tmp_path: pathlib.Path) -> None:
    """§12/§15/§20: A muere SIN cleanup; la lease expira y B readquiere.

    La muerte dura no deja un ownership permanente que haya que borrar a mano:
    el heartbeat de A deja de renovar, la lease expira por TTL y B la reclama.
    Antes de activar su root, B pasa la transición completa de P0 (inspección
    del root viejo, ritual libre).
    """
    estado = tmp_path / "estado"
    root_a = tmp_path / "Work A"
    root_b = tmp_path / "Work B"
    holder, retador = _correr_ownership_vivo(tmp_path, estado=estado, root_a=root_a, root_b=root_b, ttl=1.2, crash=True)

    assert holder["ok"] is True
    assert retador["fase1"] == {"entro": False, "motivo": "busy"}
    assert retador["fase2"] == {"entro": True}

    recursos = _instancia(tmp_path)
    registro = ws.registro_de_roots_activos(estado)
    assert registro.entrada(recursos.clave()).root == str(root_b.resolve())
