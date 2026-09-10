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

import json
import os
import pathlib
import subprocess
import sys

import pytest

from sky_claw.app.security import known_folders
from sky_claw.local.tools import dyndolod_workspace as ws

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
    guid = known_folders.IDENTIFICADORES[carpeta]
    monkeypatch.setattr(known_folders, "_resolver_por_api", lambda g: ruta_efectiva if g == guid else None)
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
    guid = known_folders.IDENTIFICADORES["Documents"]
    monkeypatch.setattr(
        known_folders,
        "_resolver_por_api",
        lambda g: r"C:\Users\facha\Documents" if g == guid else None,
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


@pytest.mark.skipif(
    sys.platform == "win32" and not os.environ.get("SKYCLAW_TEST_SYMLINKS"),
    reason="crear symlinks en Windows exige privilegio de desarrollador",
)
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


@pytest.mark.skipif(
    sys.platform == "win32" and not os.environ.get("SKYCLAW_TEST_SYMLINKS"),
    reason="crear symlinks en Windows exige privilegio de desarrollador",
)
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


@pytest.mark.skipif(
    sys.platform == "win32" and not os.environ.get("SKYCLAW_TEST_SYMLINKS"),
    reason="crear symlinks en Windows exige privilegio de desarrollador",
)
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
while not all(p.exists() for p in sorted(listo.parent.glob("listo_*"))[:2]):
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
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
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
    monkeypatch: pytest.MonkeyPatch,
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
            info = await coordinacion.lock_manager.get_lock_info("dyndolod-pipeline")
            assert info is not None and not info.is_expired
        assert await coordinacion.lock_manager.get_lock_info("dyndolod-pipeline") is None
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
                await coordinacion.lock_manager.force_release("dyndolod-pipeline")
                await coordinacion.lock_manager.acquire_lock("dyndolod-pipeline", "intruso", ttl=60.0)
                with pytest.raises(LockLeaseLostError):
                    await sostenido.assert_owned()
                perdida_vista_desde_el_cuerpo = sostenido.lease_lost

        assert perdida_vista_desde_el_cuerpo is True
        # Y el lock del intruso sigue siendo del intruso: no se lo robamos al salir.
        info = await coordinacion.lock_manager.get_lock_info("dyndolod-pipeline")
        assert info is not None and info.agent_id == "intruso"
    finally:
        await coordinacion.lock_manager.force_release("dyndolod-pipeline")
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
                info = await coordinacion.lock_manager.get_lock_info("dyndolod-pipeline")
                vistos.append(info is not None and not info.is_expired)
                raise RuntimeError("el ritual explotó a mitad de una mutación")

        assert vistos == [True]
        assert await coordinacion.lock_manager.get_lock_info("dyndolod-pipeline") is None
    finally:
        await coordinacion.close()


def test_el_orden_de_adquisicion_esta_documentado_y_es_aciclico() -> None:
    """§21: el orden vive escrito en el módulo, no en la cabeza de quien lo escribió."""
    doc = ws.__doc__ or ""
    orden = ws.ORDEN_DE_ADQUISICION
    assert orden == (
        "dyndolod-workspace",
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
        await ws.resolver_workspace(
            preferencia=str(viejo),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )

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
    finally:
        await coordinacion.close()


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
        await ws.resolver_workspace(
            preferencia=str(viejo),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
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
        await ws.resolver_workspace(
            preferencia=str(viejo),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
        await coordinacion.initialize()
        await coordinacion.lock_manager.acquire_lock("dyndolod-pipeline", "otra-instancia", ttl=120.0)

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
        await coordinacion.lock_manager.force_release("dyndolod-pipeline")
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
        await ws.resolver_workspace(
            preferencia=str(viejo),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
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
        await ws.resolver_workspace(
            preferencia=str(viejo),
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )
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
        await ws.resolver_workspace(
            preferencia=Config(config_path).external_work_root,
            recursos=recursos,
            prohibidas=_prohibidas(tmp_path),
            registro=registro,
            coordinacion=coordinacion,
        )

        # Edición MANUAL del TOML + residuo pendiente en el root viejo.
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(str(viejo), str(nuevo)),
            encoding="utf-8",
        )
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
    encontrados: dict[str, bool] = {}
    for archivo in paquete.rglob("*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        for nodo in ast.walk(arbol):
            if (
                isinstance(nodo, ast.Call)
                and isinstance(nodo.func, ast.Name)
                and nodo.func.id == "DynDOLODPipelineService"
            ):
                clave = archivo.relative_to(raiz).as_posix()
                pasa = any(kw.arg == "stage9_coordination" for kw in nodo.keywords)
                encontrados[clave] = encontrados.get(clave, True) and pasa

    assert set(encontrados) == CONSTRUCTORES_DEL_SERVICIO_DYNDOLOD
    sin_coordinacion = sorted(k for k, v in encontrados.items() if not v)
    assert not sin_coordinacion, f"construyen el servicio sin coordinar: {sin_coordinacion}"


def test_el_reconciliador_rutea_el_ritual_de_etapa9_a_la_base_durable(
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

    assert _manager_del_ritual("dyndolod-pipeline", por_defecto, coordinacion) is coordinacion.lock_manager
    # Los otros rituales NO se migran incidentalmente.
    for otro in ("behavior-graphs", "bodyslide-meshes", "load-order"):
        assert _manager_del_ritual(otro, por_defecto, coordinacion) is por_defecto
    # Y sin coordinación cableada, todo conserva el comportamiento previo.
    assert _manager_del_ritual("dyndolod-pipeline", por_defecto, None) is por_defecto
