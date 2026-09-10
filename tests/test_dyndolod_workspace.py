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
