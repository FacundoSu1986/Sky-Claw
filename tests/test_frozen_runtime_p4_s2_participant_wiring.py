"""Tests RED-first para P4-S2: wiring de participantes al root-keyed lock (P4-S1).

ADR 0012 §34.3 es la autoridad normativa. `LOCK_EXCLUSION_SET` enumera las
operaciones que deben correr bajo exclusión por FrozenRuntimeRoot:

    { PROMOTION, ROLLBACK, activation/rebinding, startup reconciliation,
      publish Generation, candidate build que escriba state/ }

De ese conjunto, los participantes REALES hoy en el código son los que mutan el
root: publicar Generation, construir Candidate y persistir el estado durable.
Promotion, rollback, reconciliation y approvals NO estan implementados (diseno
P4-D0 congelado); su wiring pertenece a slices posteriores y este slice NO los
implementa.

--- Nota de auditoria correctiva (P4-S2 / PR #718) ---------------------------

La primera version de este modulo tenia tres tests que NO ejercitaban la funcion
de la que tomaban el nombre (F01/F02/F03) y un ancla que no distinguia una
adquisicion real de una llamada homonima (F04). Los tests de comportamiento
invocan ahora al PARTICIPANTE real via `tests/_p4_s2_child.py` (un modulo, no un
string embebido: un script en texto es facil de degradar a un `participant_lock()`
pelado sin que nadie lo note). El ancla se contrasta contra el inventario real de
escritura del proyecto (`MODULOS_CON_ESCRITURA_PERMITIDA` /
`MODULOS_CON_OPEN_ESCRITURA`, importados -- no reimplementados) y exige que el
entrypoint que adquiere sea resolublemente el root lock.

Este modulo cubre:
- Contrato de adquisicion (ancla por AST contra el inventario real de escritores).
- Exclusion concurrente real (multiproceso) ejecutando los participantes.
- No-deadlock por adquisicion anidada.
- Fail-closed conservando el estado previo.
- Lifetime del lock en la construccion de Candidate (F05).
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys
import textwrap
import time

import pytest

from sky_claw.local.frozen_runtime import initialize_frozen_runtime_storage
from sky_claw.local.frozen_runtime.root_lock import acquire_frozen_runtime_root_lock
from tests.test_frozen_runtime_p1_readonly import (
    MODULOS_CON_ESCRITURA_PERMITIDA,
    MODULOS_CON_OPEN_ESCRITURA,
    _detectar_open_no_declarado,
)

# ============================================================================
# Infraestructura: subprocess con aislamiento de imports
# ============================================================================

_RAIZ_WORKTREE = pathlib.Path(__file__).resolve().parents[1]
_CHILD = _RAIZ_WORKTREE / "tests" / "_p4_s2_child.py"


def _entorno_hijo() -> dict[str, str]:
    """Entorno que fuerza al hijo a cargar el codigo del WORKTREE, no el editable.

    `sky_claw` esta instalado en modo editable apuntando al arbol principal
    (`.pth`). Sin este `PYTHONPATH`, un subprocess importa el paquete del repo
    PRINCIPAL y el test mediria otro commit — el defecto clasico de esta
    maquina. Se antepone la raiz del worktree para ganarle al `.pth`.
    """
    entorno = dict(os.environ)
    entorno["PYTHONPATH"] = str(_RAIZ_WORKTREE) + os.pathsep + entorno.get("PYTHONPATH", "")
    entorno["SKYCLAW_P4_S2_WORKTREE"] = str(_RAIZ_WORKTREE)
    return entorno


def _lanzar(modo: str, root: pathlib.Path, args: dict | None = None) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(_CHILD), modo, str(root), json.dumps(args or {})],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_entorno_hijo(),
        cwd=str(_RAIZ_WORKTREE),
    )


def _resultado(proc: subprocess.Popen[str], *, timeout: float = 90.0) -> dict:
    """Cierra un hijo y devuelve su linea JSON, exigiendo exito explicito.

    Un hijo que imprime `{"ok": false}` o termina con exit != 0 NUNCA puede dar
    por bueno al test: se asserta returncode, se parsea el JSON y se exige que no
    haya traceback en stderr.
    """
    out, err = proc.communicate(timeout=timeout)
    assert proc.returncode == 0, f"el hijo termino con {proc.returncode}; stderr={err!r}"
    assert "Traceback" not in err, err
    linea = [ln for ln in out.splitlines() if ln.strip().startswith("{")]
    assert linea, f"el hijo no imprimio JSON; stdout={out!r} stderr={err!r}"
    return json.loads(linea[-1])


def _escribir_rig_p3(base: pathlib.Path) -> pathlib.Path:
    """Managed Source Steam SINTETICA (nunca Steam real, §49).

    Replica el rig de `tests/_p3_rig.py` en disco para que un subprocess pueda
    consumirlo (las fixtures de pytest no cruzan la frontera de proceso).
    """
    steamapps = base / "library" / "steamapps"
    common = steamapps / "common" / "Skyrim Special Edition"
    (common / "Data" / "Meshes" / "Characters").mkdir(parents=True)
    (common / "Data" / "Textures").mkdir(parents=True)
    (common / "SkyrimSE.exe").write_bytes(b"fake-skyrimse-payload")
    (common / "Data" / "Meshes" / "Characters" / "a.nif").write_bytes(b"nif-payload")
    (common / "Data" / "Skyrim.esm").write_bytes(b"esm-payload")
    (steamapps / "appmanifest_489830.acf").write_text(
        '"AppState"\n'
        "{\n"
        '\t"appid"\t\t"489830"\n'
        '\t"name"\t\t"Skyrim Special Edition"\n'
        '\t"StateFlags"\t\t"4"\n'
        '\t"buildid"\t\t"1234567"\n'
        '\t"BytesToDownload"\t\t"0"\n'
        '\t"BytesDownloaded"\t\t"0"\n'
        '\t"UpdateResult"\t\t"0"\n'
        '\t"installdir"\t\t"Skyrim Special Edition"\n'
        "}\n",
        encoding="utf-8",
    )
    return steamapps


# ============================================================================
# Ancla por AST — contrastada contra el INVENTARIO REAL de escritura (F04)
# ============================================================================

#: Autoridad de adquisicion: los DOS unicos entrypoints validos del root lock.
#: Un guard que aceptara cualquier `acquire` daria por buena una llamada a un
#: objeto ajeno (F04): el nombre no prueba que se haya tomado EL lock.
_ENTRYPOINTS_ADQUISICION = frozenset({"acquire_frozen_runtime_root_lock", "participant_lock"})

#: Modulos importados del oracle del proyecto: el inventario de escritura NO se
#: reimplementa aca (una copia se desincroniza y mide otra cosa).


def _paquete() -> pathlib.Path:
    return _RAIZ_WORKTREE / "sky_claw" / "local" / "frozen_runtime"


def _arbol_de(ruta: pathlib.Path) -> ast.Module:
    return ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))


def _nombre_llamado(nodo: ast.Call) -> str | None:
    func = nodo.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def _resuelve_a_root_lock(nodo: ast.Call, arbol: ast.Module) -> bool:
    """Decide si la llamada apunta al root lock y no a un homonimo ajeno.

    Un `mi_objeto.acquire()` satisface cualquier censo por nombre. Aca se exige
    que el nombre este en el conjunto de entrypoints Y que, si es un atributo, el
    receptor no sea un `Name` distinto del modulo importado (p.ej.
    `root_lock.participant_lock` es valido; `self.conn.acquire` no).
    """
    nombre = _nombre_llamado(nodo)
    if nombre not in _ENTRYPOINTS_ADQUISICION:
        return False
    func = nodo.func
    if isinstance(func, ast.Name):
        return True
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        receptor = func.value.id
        # El receptor debe ser un modulo/alias del paquete, no un objeto cualquiera.
        return receptor in {"root_lock", "rl", "modulo_root_lock"}
    return False


def _funciones_que_adquieren_lock(arbol: ast.Module) -> set[str]:
    """Funciones que invocan una adquisicion RESOLUBLE al root lock.

    Recorre el arbol llevando el contenedor inmediato: a diferencia del walker
    plano, una llamada dentro de un `with` anidado sigue atribuyendose a la
    funcion que la contiene (que es donde vive la seccion critica).
    """
    adquirentes: set[str] = set()

    def _visitar(nodo: ast.AST, contenedor: str | None) -> None:
        for hijo in ast.iter_child_nodes(nodo):
            if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _visitar(hijo, hijo.name)
                continue
            if isinstance(hijo, ast.Call) and contenedor is not None and _resuelve_a_root_lock(hijo, arbol):
                adquirentes.add(contenedor)
            _visitar(hijo, contenedor)

    _visitar(arbol, None)
    return adquirentes


def _escritores_reales_del_paquete() -> set[str]:
    """Modulos con capacidad de ESCRITURA segun el inventario del proyecto.

    Se ancla en la autoridad existente (el oracle de P1) en vez de mantener una
    lista paralela: si un modulo nuevo gana un mutador sin declararlo, el oracle
    lo rechaza y este censo lo ve.
    """
    escritores: set[str] = set(MODULOS_CON_ESCRITURA_PERMITIDA)
    escritores |= set(MODULOS_CON_OPEN_ESCRITURA)
    for modulo in _paquete().glob("*.py"):
        if _detectar_open_no_declarado(modulo.read_text(encoding="utf-8"), modulo.name):
            escritores.add(modulo.name)
    return escritores


def test_s2_censo_publicos_que_mutan_el_root_adquieren_el_lock() -> None:
    """Ancla enumerativa: cada mutador REAL del root adquiere el root lock.

    El censo no es una lista fija de cuatro nombres: se DERIVA de los modulos que
    el proyecto declara con capacidad de escritura y se exige que los entrypoints
    publicos que mutan el root esten cableados. Agregar un mutador nuevo en un
    modulo escritor sin cablearlo rompe esta ancla.
    """
    esperado = {
        "generations.py": {"registrar_generation_metadata"},
        "candidates.py": {"crear_candidate"},
        "storage.py": {"initialize_frozen_runtime_storage"},
        "state.py": {"save_frozen_runtime_state"},
    }
    # Todo modulo que el inventario declara escritor debe estar contemplado.
    escritores = _escritores_reales_del_paquete()
    no_contemplados = escritores - set(esperado) - {"copying.py", "root_lock.py"}
    assert not no_contemplados, (
        "el inventario de escritura declara modulos que el censo de participantes no contempla: "
        f"{sorted(no_contemplados)} -- si escribe en el root, necesita adquisicion"
    )

    for modulo, funciones in esperado.items():
        arbol = _arbol_de(_paquete() / modulo)
        adquirentes = _funciones_que_adquieren_lock(arbol)
        faltantes = funciones - adquirentes
        assert not faltantes, f"{modulo}: los publicos mutadores no adquieren el root lock: {sorted(faltantes)}"


def test_s2_el_censo_rechaza_una_adquisicion_homonima() -> None:
    """Control negativo del ancla: `objeto.acquire()` NO cuenta como adquisicion.

    Sin esto, el censo pasaria por el nombre de un metodo cualquiera: la version
    original reconocia `acquire` a secas y daba por cableada una funcion que
    jamas tomo el root lock (F04).
    """
    falso = textwrap.dedent(
        """
        def mutador_falso(root):
            object.__new__(type("C", (), {"acquire": lambda self: None})).acquire()
            root.joinpath("x").write_text("boom")
        """
    )
    arbol = ast.parse(falso)
    assert "mutador_falso" not in _funciones_que_adquieren_lock(arbol)

    # Y una adquisicion REAL si se reconoce (el control no es vacuamente falso).
    real = textwrap.dedent(
        """
        def mutador_real(root):
            with participant_lock(root, timeout=0.0) as h:
                h.assert_owned()
        """
    )
    assert "mutador_real" in _funciones_que_adquieren_lock(ast.parse(real))


def test_s2_cada_modulo_escritor_declara_su_adquisicion_si_muta_el_root() -> None:
    """Un modulo escritor que muta el root sin adquirir el lock es un agujero.

    Se enumeran los modulos con escritura permitida y se verifica que los que
    tienen un call site de mutacion del root tambien tengan adquisicion. Los
    delegadores (`generations.py` escribe via `state.write_json_atomic`) quedan
    cubiertos por su entrypoint publico en el censo anterior.
    """
    delegados = {"copying.py", "root_lock.py"}
    for modulo in sorted(_escritores_reales_del_paquete() - delegados):
        arbol = _arbol_de(_paquete() / modulo)
        adquirentes = _funciones_que_adquieren_lock(arbol)
        assert adquirentes, f"{modulo} tiene escritura declarada pero ninguna funcion adquiere el root lock"


def test_s2_no_existe_segunda_autoridad_de_exclusion() -> None:
    """No se introduce un segundo lock manager: solo root_lock es autoridad."""
    for ruta in _paquete().glob("*.py"):
        arbol = _arbol_de(ruta)
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.ImportFrom) and nodo.module and "destination_lock" in nodo.module:
                pytest.fail(f"{ruta.name} importa destination_lock (autoridad paralela)")


# ============================================================================
# Comportamiento — exclusion concurrente sobre participantes REALES
# ============================================================================


def _crear_root(base: pathlib.Path, nombre: str = "root") -> pathlib.Path:
    root = base / nombre
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_s2_init_concurrente_es_excluyente_y_sin_deadlock(tmp_path: pathlib.Path) -> None:
    """Dos procesos inicializan el mismo root: ambos DEBEN reportar exito.

    El init adquiere el lock internamente; envolverlo en OTRA adquisicion del
    mismo root ejercitaria la no-reentrancia (y este test NO debe hacerlo: la
    exclusion es responsabilidad del participante, no del caller).

    F03: no alcanza con que haya salida. Se exige exit code 0, JSON parseable y
    `success is True` en CADA proceso. Un segundo proceso que devolviera
    `success: false` no puede pasar por bueno.
    """
    root = _crear_root(tmp_path)
    procesos = [_lanzar("init", root) for _ in range(3)]
    resultados = [_resultado(p) for p in procesos]

    for i, r in enumerate(resultados):
        assert r["success"] is True, f"proceso {i} no inicializo: {r}"
    # Idempotente: gane quien gane, el layout y el estado inicial quedan integros.
    assert (root / "state" / "active.json").exists()
    for sub in ("versions", "candidates", "state", "state/generations"):
        assert (root / sub).is_dir(), f"falta {sub} en el layout final"
    estado = json.loads((root / "state" / "active.json").read_text(encoding="utf-8"))
    assert estado["schema_version"] == 1
    assert estado["desired_active_generation"] is None


def _esperar_archivo(ruta: pathlib.Path, *, timeout: float = 60.0) -> bool:
    """Espera activa acotada a que un archivo exista (handshake entre procesos).

    Se usa en vez de `sleep` arbitrarios: el test no ADIVINA cuanto tarda el
    hijo en entrar a la seccion critica, lo OBSERVA (bandera escrita por el hijo
    desde dentro del lock). Sin esto, lanzar A y B seguidos convierte el test en
    una carrera de arranque de interpretes en la que A puede perder legitimamente.
    """
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if ruta.exists():
            return True
        time.sleep(0.02)
    return False


def test_s2_escritura_concurrente_de_estado_serializa(tmp_path: pathlib.Path) -> None:
    """Dos procesos ESCRIBEN estado con `save_frozen_runtime_state` real.

    F02: la version anterior importaba el escritor y nunca lo llamaba, asi que el
    marcador no se creaba y la comprobacion de solapamiento era vacua. Aca el
    participante REAL corre y el marcador se escribe DESDE DENTRO de su seccion
    critica (interceptando `os.replace`, el commit atomico que el mismo invoca).

    Sincronizacion (auditoria 2): el diseno previo lanzaba A y B casi juntos y
    ASUMI A ganador. Bajo `timeout=0.0` el ganador es quien llegue primero al
    syscall de adquisicion, y el orden de `Popen` NO garantiza eso: A podia
    recibir `LockBusy` legitimamente y el test lo reportaba como fallo (carrera
    real, observada). Ahora A entra, ESCRIBE la bandera `dentro` desde dentro del
    lock y ESPERA; el test comprueba `dentro` y solo ENTONCES lanza a B. Asi la
    exclusion se mide sobre un lock realmente retenido.
    """
    root = _crear_root(tmp_path)
    assert initialize_frozen_runtime_storage(root).success
    solapamiento = tmp_path / "solapamiento.txt"
    solapamiento.write_text("", encoding="utf-8")
    dentro = tmp_path / "dentro.txt"
    liberar = tmp_path / "liberar.txt"

    # Proceso A: entra a la seccion critica, se declara con `dentro` y ESPERA a
    # que el test lo libere. No depende de sleeps ni del orden de arranque.
    a = _lanzar(
        "estado",
        root,
        {
            "solapamiento": str(solapamiento),
            "dentro": str(dentro),
            "mantener_hasta": str(liberar),
            "desired": "1.6.1170__aaaaaaaaaaaa",
            "updated_at_ns": 111,
        },
    )
    try:
        assert _esperar_archivo(dentro), "A nunca entro a la seccion critica (no se creo `dentro`)"
        assert a.poll() is None, "A termino antes de que el test lo observara dentro del lock"

        # Proceso B: llega con el root ocupado. Debe FALLAR CERRADO de inmediato.
        b = _lanzar(
            "estado",
            root,
            {
                "solapamiento": str(solapamiento),
                "dentro": str(tmp_path / "dentro_b.txt"),
                "marcar_solapamiento": False,
                "desired": "1.6.1170__bbbbbbbbbbbb",
                "updated_at_ns": 222,
            },
        )
        b_out, b_err = b.communicate(timeout=90)
    finally:
        # Liberar SIEMPRE a A, incluso si una asercion falla antes.
        liberar.write_text("1", encoding="utf-8")

    a_out, a_err = a.communicate(timeout=90)

    # A gano la seccion critica y completo.
    assert a.returncode == 0, f"A fallo: {a_err!r}"
    # B NO pudo entrar: exit != 0 con el error tipado del lock (fail-closed).
    assert b.returncode != 0, f"B entro a la seccion critica de A (no hubo exclusion): {b_out!r}"
    assert "LockBusy" in b_err, f"B debe fallar por el lock, no por otra razon: {b_err!r}"

    # NINGUN solapamiento: el marcador solo pudo escribirlo quien estuvo dentro.
    # B lo tiene desactivado, asi que exactamente UNA marca prueba exclusion real.
    marcador = solapamiento.read_text(encoding="utf-8")
    assert marcador.count("SOLAPADO") == 1, f"hubo interleaving o nadie entro: {marcador!r}"

    # El estado final es JSON integro y valido (nunca a medias) y es el de A,
    # porque B jamas llego a escribir.
    final = json.loads((root / "state" / "active.json").read_text(encoding="utf-8"))
    assert final["desired_active_generation"] == "1.6.1170__aaaaaaaaaaaa"
    assert final["updated_at_ns"] == 111


def test_s2_escritura_de_estado_bajo_contencion_nunca_corrompe(tmp_path: pathlib.Path) -> None:
    """Bajo contencion, cada escritor GANA o FALLA CERRADO; nunca corrompe.

    Contrato real (ADR §34.3): el estado durable usa `timeout=0.0`, asi que un
    competidor que llega mientras otro escribe recibe `FrozenRuntimeLockBusyError`
    en vez de esperar. Este test fija las DOS mitades:

    - quien gana, escribe un JSON integro;
    - quien pierde, falla con el error tipado del lock (no con una excepcion
      cruda ni un archivo a medias);
    - el resultado final es SIEMPRE de uno de los escritores, jamas una mezcla.

    Nota (auditoria): la version previa asumia que "sin contencion ambas ganan".
    Bajo `timeout=0.0` eso NO esta garantizado y afirmarlo seria fijar un contrato
    que el ADR no promete. Lo que si esta garantizado es la integridad.
    """
    root = _crear_root(tmp_path)
    assert initialize_frozen_runtime_storage(root).success
    solapamiento = tmp_path / "solapamiento.txt"
    solapamiento.write_text("", encoding="utf-8")
    sin_barrera = tmp_path / "no_existe.txt"

    procesos = [
        _lanzar(
            "estado",
            root,
            {
                "solapamiento": str(solapamiento),
                "dentro": str(tmp_path / f"dentro_{i}.txt"),
                "mantener_hasta": str(sin_barrera),
                "desired": f"1.6.1170__{i:012x}",
                "updated_at_ns": 1000 + i,
            },
        )
        for i in range(4)
    ]
    ganadores = 0
    for p in procesos:
        out, err = p.communicate(timeout=90)
        if p.returncode == 0:
            ganadores += 1
            assert json.loads(out.strip().splitlines()[-1])["ok"] is True
        else:
            # El perdedor falla CERRADO con el error del lock, no con un traceback
            # arbitrario ni un archivo corrupto.
            assert "LockBusy" in err, f"perdida inesperada: {err!r}"

    assert ganadores >= 1, "ningun escritor completo: la exclusion no debe bloquear siempre"

    # Integridad: el estado final es de UNO de los escritores, nunca una mezcla,
    # y no quedo ningun temporal de escritura atomica.
    final = json.loads((root / "state" / "active.json").read_text(encoding="utf-8"))
    assert final["schema_version"] == 1
    assert final["desired_active_generation"] in {f"1.6.1170__{i:012x}" for i in range(4)}
    assert not list((root / "state").glob(".active.json.*.tmp")), "quedo un temporal de escritura atomica"


def test_s2_crear_candidate_concurrente_no_deadlock(tmp_path: pathlib.Path) -> None:
    """Dos procesos construyen Candidates REALES sobre el mismo root.

    F01: la version anterior solo invocaba `participant_lock()` y devolvia
    `{"ok": true}`; nunca llamaba a `crear_candidate`. Aca el hijo ejecuta el
    PARTICIPANTE real (PRE -> BUILDING -> copia -> verificacion -> POST ->
    READY), sobre dos candidate_id distintos, y se verifica el historial.

    No hay deadlock porque el lock se adquiere UNA vez por proceso y el timeout
    acotado convierte una espera larga en un resultado tipado.
    """
    root = _crear_root(tmp_path)
    assert initialize_frozen_runtime_storage(root).success
    steamapps = _escribir_rig_p3(tmp_path)

    cid_a = "cand_" + "a" * 32
    cid_b = "cand_" + "b" * 32
    procesos = [
        _lanzar("candidate", root, {"steamapps": str(steamapps), "candidate_id": cid_a}),
        _lanzar("candidate", root, {"steamapps": str(steamapps), "candidate_id": cid_b}),
    ]
    resultados = [_resultado(p, timeout=180.0) for p in procesos]

    assert [r["candidate_id"] for r in resultados] == [cid_a, cid_b]
    for r in resultados:
        assert r["state"] == "valid", f"el Candidate no quedo READY: {r}"

    # El historial conserva LAS DOS identidades correctas: ninguno pisa al otro.
    meta_dir = root / "state" / "candidates"
    assert (meta_dir / f"{cid_a}.json").is_file()
    assert (meta_dir / f"{cid_b}.json").is_file()
    for cid in (cid_a, cid_b):
        datos = json.loads((meta_dir / f"{cid}.json").read_text(encoding="utf-8"))
        assert datos["candidate_id"] == cid
        assert datos["state"] == "ready"
        assert datos["candidate_evidence"] is not None
        assert datos["post_source_evidence"] is not None


def test_s2_candidate_colision_de_id_no_pisa_el_historial(tmp_path: pathlib.Path) -> None:
    """Dos procesos con el MISMO candidate_id: uno gana, el otro falla cerrado.

    Demuestra que la exclusion preserva la evidencia: el segundo NO puede
    reemplazar el BUILDING/READY del primero (no-clobber), y el veredicto es
    tipado (INDETERMINATE), nunca una excepcion cruda.
    """
    root = _crear_root(tmp_path)
    assert initialize_frozen_runtime_storage(root).success
    steamapps = _escribir_rig_p3(tmp_path)
    cid = "cand_" + "c" * 32

    procesos = [_lanzar("candidate", root, {"steamapps": str(steamapps), "candidate_id": cid}) for _ in range(2)]
    resultados = [_resultado(p, timeout=180.0) for p in procesos]

    estados = sorted(r["state"] for r in resultados)
    assert estados[0] in {"valid", "indeterminate"}, estados
    assert "valid" in estados, f"ningun proceso construyo el Candidate: {resultados}"

    # El que gano dejo evidencia COMPLETA; el historial es consistente.
    datos = json.loads((root / "state" / "candidates" / f"{cid}.json").read_text(encoding="utf-8"))
    assert datos["candidate_id"] == cid
    assert datos["state"] == "ready"
    assert datos["candidate_evidence"] is not None


def test_s2_fallo_en_operacion_protegida_conserva_estado_previo(tmp_path: pathlib.Path) -> None:
    """Un fallo dentro de la seccion critica no corrompe el estado previo."""
    root = _crear_root(tmp_path)
    resultado = initialize_frozen_runtime_storage(root)
    assert resultado.success

    original = (root / "state" / "active.json").read_bytes()
    with pytest.raises(RuntimeError), acquire_frozen_runtime_root_lock(root, timeout=5.0) as handle:
        handle.assert_owned()
        raise RuntimeError("fallo simulado dentro de la seccion critica")

    assert (root / "state" / "active.json").read_bytes() == original
    # El lock se libera pese a la excepcion: se puede readquirir.
    with acquire_frozen_runtime_root_lock(root, timeout=5.0) as handle:
        handle.assert_owned()


def test_s2_estado_previo_sobrevive_a_un_fallo_del_escritor(tmp_path: pathlib.Path, monkeypatch) -> None:
    """Un fallo ANTES del commit de persistencia deja intacto el estado anterior.

    Se inyecta el fallo en `os.replace` (el commit atomico): todo lo anterior
    ocurrio bajo el lock y el estado previo debe seguir siendo el valido.
    """
    from sky_claw.local.frozen_runtime import state as state_module
    from sky_claw.local.frozen_runtime.storage_models import FrozenRuntimeState

    root = _crear_root(tmp_path)
    assert initialize_frozen_runtime_storage(root).success
    from sky_claw.local.frozen_runtime.state import save_frozen_runtime_state

    save_frozen_runtime_state(
        root / "state" / "active.json",
        FrozenRuntimeState(schema_version=1, desired_active_generation="1.6.1170__aaaaaaaaaaaa", updated_at_ns=10),
    )
    previo = (root / "state" / "active.json").read_bytes()

    def boom(_origen, _destino):
        raise OSError(5, "simulado: os.replace fallido")

    monkeypatch.setattr(state_module.os, "replace", boom)
    with pytest.raises(Exception):  # noqa: B017 - el tipo exacto lo fija el contrato de storage
        save_frozen_runtime_state(
            root / "state" / "active.json",
            FrozenRuntimeState(schema_version=1, desired_active_generation="1.6.1170__bbbbbbbbbbbb", updated_at_ns=20),
        )
    monkeypatch.undo()

    assert (root / "state" / "active.json").read_bytes() == previo
    # Y el lock se libero: se puede volver a escribir.
    save_frozen_runtime_state(
        root / "state" / "active.json",
        FrozenRuntimeState(schema_version=1, desired_active_generation="1.6.1170__cccccccccccc", updated_at_ns=30),
    )
    final = json.loads((root / "state" / "active.json").read_text(encoding="utf-8"))
    assert final["desired_active_generation"] == "1.6.1170__cccccccccccc"


def test_s2_lock_huerfano_bloquea_mutacion(tmp_path: pathlib.Path) -> None:
    """Un lock huerfano de un proceso muerto impide mutar (fail-closed) sin reclaim."""
    root = _crear_root(tmp_path)
    initialize_frozen_runtime_storage(root)

    script = textwrap.dedent(
        f"""
        import pathlib
        from sky_claw.local.frozen_runtime.root_lock import acquire_frozen_runtime_root_lock
        root = pathlib.Path(r"{root}")
        h = acquire_frozen_runtime_root_lock(root, timeout=5.0)
        print("HELD")
        """
    )
    archivo = tmp_path / "huerfano.py"
    archivo.write_text(script, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(archivo)], stdout=subprocess.PIPE, text=True, env=_entorno_hijo(), cwd=str(_RAIZ_WORKTREE)
    )
    try:
        proc.stdout.readline()
        proc.kill()  # muere sin liberar: deja evidencia forense
        proc.wait(timeout=30)
    finally:
        # Cerrar el pipe en ESTE proceso: si no, el GC de pytest lo recolecta
        # despues del test y emite PytestUnraisableExceptionWarning, que enmascara
        # el resultado real del contrato.
        if proc.stdout is not None:
            proc.stdout.close()

    from sky_claw.local.frozen_runtime.errors import FrozenRuntimeLockOrphanedError

    with pytest.raises(FrozenRuntimeLockOrphanedError), acquire_frozen_runtime_root_lock(root, timeout=1.0):
        pass


def test_s2_roots_distintos_no_se_bloquean(tmp_path: pathlib.Path) -> None:
    """Roots distintos tienen locks independientes (no hay exclusion global)."""
    root_a = _crear_root(tmp_path, "a")
    root_b = _crear_root(tmp_path, "b")
    with acquire_frozen_runtime_root_lock(root_a, timeout=5.0) as ha:
        ha.assert_owned()
        with acquire_frozen_runtime_root_lock(root_b, timeout=5.0) as hb:
            hb.assert_owned()


def test_s2_timeout_acotado_no_espera_indefinida(tmp_path: pathlib.Path) -> None:
    """Con el root ocupado, timeout=0 falla de inmediato (LOCK_BUSY acotado)."""
    root = _crear_root(tmp_path)
    from sky_claw.local.frozen_runtime.errors import FrozenRuntimeLockBusyError

    with acquire_frozen_runtime_root_lock(root, timeout=5.0) as handle:
        handle.assert_owned()
        with pytest.raises(FrozenRuntimeLockBusyError), acquire_frozen_runtime_root_lock(root, timeout=0.0):
            pass


# ============================================================================
# F05 — lifetime del lock durante la construccion de un Candidate
# ============================================================================


def test_s2_candidate_con_root_ocupado_devuelve_indeterminate_acotado(tmp_path: pathlib.Path) -> None:
    """Con el root ocupado, `crear_candidate` devuelve INDETERMINATE en plazo acotado.

    F05: `crear_candidate` retiene el lock durante copia + verificacion + POST.
    El ADR es explicito: `LOCK_SCOPE = toda mutacion de autoridad del runtime` y
    `LOCK_SE_CONSERVA_MIENTRAS_HAYA_MUTADOR = YES` (§35.8.1), asi que sostenerlo
    es CONTRACTUAL, no un defecto. Lo que el ADR tambien exige es que NO haya
    espera indefinida (`LOCK_BUSY = fail-closed con timeout acotado`).

    Este test fija la otra mitad del contrato: un competidor con el root ocupado
    obtiene un resultado TIPADO (INDETERMINATE) dentro del plazo, sin deadlock y
    sin tocar el estado ajeno.
    """
    root = _crear_root(tmp_path)
    assert initialize_frozen_runtime_storage(root).success
    steamapps = _escribir_rig_p3(tmp_path)
    cid = "cand_" + "d" * 32

    with acquire_frozen_runtime_root_lock(root, timeout=5.0) as handle:
        handle.assert_owned()
        # Con el root retenido por ESTE proceso, `crear_candidate` en OTRO proceso
        # debe agotar su timeout acotado y devolver INDETERMINATE.
        proc = _lanzar("candidate", root, {"steamapps": str(steamapps), "candidate_id": cid})
        resultado = _resultado(proc, timeout=180.0)

    assert resultado["state"] == "indeterminate", f"esperaba INDETERMINATE con el root ocupado: {resultado}"
    # Y NO dejo metadata parcial: el candidate_id no quedo reservado a medias.
    assert not (root / "state" / "candidates" / f"{cid}.json").exists()
