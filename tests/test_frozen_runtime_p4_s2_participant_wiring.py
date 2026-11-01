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

--- F04 residual (misma revision externa, dos hallazgos P2) -------------------

**Finding 1 -- resolucion por NOMBRE, no por SIMBOLO.** `_resuelve_a_root_lock`
devolvia True para cualquier `ast.Name` llamado `participant_lock` y aceptaba un
receptor por lista fija (`root_lock`/`rl`/`modulo_root_lock`) sin comprobar que
viniera de un import del modulo autorizado. Un `def participant_lock(...)` local
daba por cableado un mutador que jamas tomo el lock. Ahora se construye una TABLA
DE ENLACES por cuerpo (dos niveles: modulo + funcion actual; alcanza para el
import diferido real y no se convierte en un resolvedor generico de Python) y se
reconocen alias (`as pl`) y modulo importado (`as rl`); un homonimo local SOMBREA.

**Finding 2 -- censo de MODULOS, no de FUNCIONES.** El censo previo verificaba
que cada modulo escritor estuviera contemplado y que una lista FIJA de funciones
adquiriera: un mutador publico NUEVO dentro de un modulo ya contemplado (p.ej.
`state.py`) no rompia nada. Ahora el censo DERIVA las funciones del AST y toda
funcion con un call site de mutador debe adquirir o estar declarada como primitiva
de bajo nivel; esas primitivas se anclan ADEMAS por call site, para que la lista
no funcione como silenciador. El limite del analisis estatico esta declarado en el
comentario de `_PRIMITIVAS_BAJO_LOCK`.

**F04-IMPORT-ORIGIN -- el sufijo no autoriza.** `_tabla_de_enlaces` aceptaba
cualquier modulo cuyo ultimo componente fuera `root_lock`, asi que un origen ajeno
(`from unrelated.root_lock import participant_lock`) contaba como adquisicion. La
autoridad es la RUTA COMPLETA: `sky_claw.local.frozen_runtime.root_lock` (modulo) y
`sky_claw.local.frozen_runtime` con su submodulo explicito `root_lock`. Los alias
legitimos y el import diferido del codigo productivo siguen aceptandose; un
`import a.b.c` SIN alias no se registra, porque Python liga `a` y no `c`.

**F06 -- vinculaciones autorizadas OBSOLETAS.** `_tabla_de_enlaces` solo AGREGABA.
Un import AJENO POSTERIOR que reutilizaba el nombre local ya autorizado
(`from unrelated.root_lock import participant_lock`, o el mismo alias `rl`/`pl`) no
satisfacia ninguna condicion y dejaba VIVO el enlace anterior: la tabla seguia
diciendo que `participant_lock` designaba la primitiva real y el cuerpo contaba como
adquirente del root lock aunque llamara a un objeto ajeno. Ahora cada `import`
invalida PRIMERO el nombre local que introduce y registra DESPUES solo si el origen
es autorizado; el caso inverso (ajeno primero, autorizado despues) queda
correctamente como adquisicion, porque el enlace FINAL es el autorizado.

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
    MUTADORES_FILESYSTEM,
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

#: Origenes AUTORIZADOS del root lock, por RUTA COMPLETA (no por sufijo). Un modulo
#: ajeno que termine en `root_lock` (p.ej. `unrelated.root_lock`) NO habilita el
#: lock: aceptarlo por sufijo era el falso positivo F04-IMPORT-ORIGIN.
_MODULO_ROOT_LOCK = "sky_claw.local.frozen_runtime.root_lock"
_PAQUETE_FROZEN_RUNTIME = "sky_claw.local.frozen_runtime"


def _paquete() -> pathlib.Path:
    return _RAIZ_WORKTREE / "sky_claw" / "local" / "frozen_runtime"


def _arbol_de(ruta: pathlib.Path) -> ast.Module:
    return ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))


def _nombre_llamado(nodo: ast.Call) -> str | None:
    func = nodo.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


# ---------------------------------------------------------------------------
# F04 residual — Finding 1: resolucion de SIMBOLO, no de nombre
# ---------------------------------------------------------------------------
#
# Un censo por NOMBRE acepta un homonimo local: `def participant_lock(...)` en el
# mismo modulo satisfacia al guard previo. El instrumento construye una tabla de
# enlaces por cuerpo (dos niveles: modulo + funcion actual; alcanza para el patron
# real de import diferido y NO se convierte en un resolvedor generico de Python) y
# exige que el nombre llamado provenga del modulo autorizado.
#
# Formas legitimas reconocidas (RUTA COMPLETA, no sufijo):
#   from sky_claw.local.frozen_runtime.root_lock import participant_lock [as X]
#   from sky_claw.local.frozen_runtime.root_lock import acquire_frozen_runtime_root_lock [as X]
#   from sky_claw.local.frozen_runtime import root_lock [as X]     -> X es el MODULO
#   import sky_claw.local.frozen_runtime.root_lock as X            -> X es el MODULO
# Se RECHAZAN: un `def`/`class`/asignacion homonima en el mismo cuerpo (sombra), un
# receptor que no proviene de un import autorizado, y un modulo AJENO que solo
# comparta el sufijo `root_lock` (`unrelated.root_lock` era el falso positivo
# F04-IMPORT-ORIGIN).
#
# LIMITES DECLARADOS (fail-closed: si el codigo productivo cambiara a una de estas
# formas, el ancla da ROJO y hay que revisarla a mano, nunca verde en falso):
#   - Los imports RELATIVOS (`from .root_lock import ...`) no se resuelven: exigiria
#     conocer el paquete del modulo y eso ya seria un resolvedor generico.
#   - `import a.b.c` SIN alias no se registra: en Python vincula `a` (el paquete
#     raiz), no `c`, asi que no habilita un receptor directo `root_lock`.


def _objetivos_asignados(nodo: ast.stmt) -> set[str]:
    if isinstance(nodo, ast.Assign):
        objetivos = list(nodo.targets)
    elif isinstance(nodo, (ast.AnnAssign, ast.AugAssign)):
        objetivos = [nodo.target]
    else:
        return set()
    return {sub.id for objetivo in objetivos for sub in ast.walk(objetivo) if isinstance(sub, ast.Name)}


def _tabla_de_enlaces(cuerpo: list[ast.stmt], base: tuple[set[str], set[str]]) -> tuple[set[str], set[str]]:
    """(nombres_del_entrypoint, nombres_del_MODULO) visibles en este cuerpo.

    `base` es la tabla del ambito contenedor. Un `def`/`class`/asignacion con el
    mismo nombre SOMBREA el enlace importado: si el cuerpo define su propio
    `participant_lock`, ese nombre deja de designar la primitiva real. La sombra
    gana siempre (fail-closed hacia el lado seguro): es justo el falso positivo
    que el hallazgo senala.

    F06 -- el ORDEN de las importaciones importa. Cada `import` INVALIDA primero
    el enlace previo del nombre local que va a introducir y SOLO despues lo
    registra si el origen es autorizado. La version previa solo AGREGABA: un
    import AJENO posterior que reutilizaba el nombre no cumplia ninguna condicion
    y dejaba VIVO el enlace autorizado anterior, asi que un cuerpo que en la
    practica llama a un objeto ajeno se clasificaba como adquirente del root lock.
    Se invalida SIEMPRE (origen ajeno o no), porque el nombre local pasa a
    designar el objeto NUEVO; registrar despues no es mas que un caso particular.
    """
    entrypoints, modulos = set(base[0]), set(base[1])

    def _sombra(nombre: str) -> None:
        entrypoints.discard(nombre)
        modulos.discard(nombre)

    for nodo in cuerpo:
        if isinstance(nodo, ast.ImportFrom):
            # Se exige la RUTA COMPLETA autorizada: `from <...>.root_lock import X`
            # trae el entrypoint; `from <paquete> import root_lock` trae el MODULO.
            for alias in nodo.names:
                if alias.name == "*":
                    # `from X import *` introduce un conjunto de nombres que no se
                    # puede resolver estaticamente: fail-closed, invalida TODO.
                    entrypoints.clear()
                    modulos.clear()
                    continue
                local = alias.asname or alias.name
                _sombra(local)  # F06: el import REVINCULA `local`; el enlace previo caduca.
                if nodo.module == _MODULO_ROOT_LOCK and alias.name in _ENTRYPOINTS_ADQUISICION:
                    entrypoints.add(local)
                elif nodo.module == _PAQUETE_FROZEN_RUNTIME and alias.name == "root_lock":
                    modulos.add(local)
        elif isinstance(nodo, ast.Import):
            for alias in nodo.names:
                # `import a.b.c` SIN alias vincula `a` (el paquete raiz), no `c`: no
                # habilita un receptor directo, asi que no se registra nada -- pero
                # SI invalida el nombre realmente ligado (`a`), que es lo que Python
                # rebindea.
                local = alias.asname or alias.name.split(".")[0]
                _sombra(local)  # F06: idem, con el nombre que Python liga de verdad.
                if alias.name == _MODULO_ROOT_LOCK and alias.asname:
                    modulos.add(alias.asname)
        elif isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _sombra(nodo.name)
        elif isinstance(nodo, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for objetivo in _objetivos_asignados(nodo):
                _sombra(objetivo)
    return entrypoints, modulos


def _resuelve_a_root_lock(nodo: ast.Call, entrypoints: set[str], modulos: set[str]) -> bool:
    """Decide si la llamada apunta al root lock y no a un homonimo ajeno.

    `mi_objeto.acquire()` satisface cualquier censo por nombre; un `def
    participant_lock` local satisfacia a la version previa de este mismo guard.
    Aca el nombre tiene que estar LIGADO por un import del modulo autorizado en el
    ambito que lo llama (o su contenedor).
    """
    func = nodo.func
    if isinstance(func, ast.Name):
        return func.id in entrypoints
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id in modulos and func.attr in _ENTRYPOINTS_ADQUISICION
    return False


def _funciones_que_adquieren_lock(arbol: ast.Module) -> set[str]:
    """Funciones que invocan una adquisicion RESOLUBLE al root lock.

    Recorre el arbol llevando el contenedor inmediato y la tabla de enlaces del
    ambito: una llamada dentro de un `with` anidado sigue atribuyendose a la
    funcion que la contiene, y el import diferido DENTRO del participante cuenta
    para ESA funcion (que es donde vive la seccion critica).
    """
    adquirentes: set[str] = set()

    def _visitar(nodo: ast.AST, contenedor: str | None, tabla: tuple[set[str], set[str]]) -> None:
        for hijo in ast.iter_child_nodes(nodo):
            if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _visitar(hijo, hijo.name, _tabla_de_enlaces(hijo.body, tabla))
                continue
            if isinstance(hijo, ast.Call) and contenedor is not None and _resuelve_a_root_lock(hijo, *tabla):
                adquirentes.add(contenedor)
            _visitar(hijo, contenedor, tabla)

    _visitar(arbol, None, _tabla_de_enlaces(arbol.body, (set(), set())))
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


# ---------------------------------------------------------------------------
# F04 residual — Finding 2: censo de FUNCIONES mutadoras, no solo de modulos
# ---------------------------------------------------------------------------
#
# El censo previo verificaba que cada modulo escritor estuviera contemplado y que
# una lista FIJA de funciones adquiriera. Un mutador publico NUEVO dentro de un
# modulo ya contemplado (p.ej. `state.py`) no rompia nada: el modulo seguia en la
# lista y la funcion fija seguia adquiriendo. Este censo DERIVA las funciones del
# AST: toda funcion top-level con un call site de mutador del inventario debe
# adquirir el lock o estar declarada como primitiva de bajo nivel.
#
# LIMITE DECLARADO del analisis estatico (no se esconde): cubre funciones
# TOP-LEVEL de los modulos escritores y los mutadores del inventario
# (`MUTADORES_FILESYSTEM`). No sigue el grafo de llamadas de forma general ni
# resuelve metodos de clase; un mutador escondido tras una indireccion dinamica
# (`getattr`) no se detecta por AST. Ese limite lo cubren las pruebas de
# COMPORTAMIENTO de este mismo modulo (exclusion real multiproceso), que no
# dependen del analisis estatico.

#: Primitivas de escritura que NO adquieren el lock por diseno: su contrato es
#: correr DENTRO de una seccion critica ya tomada por un entrypoint publico. La
#: lista sola seria un silenciador, por eso el call site se ancla aparte
#: (`test_s2_las_primitivas_bajo_lock_solo_se_invocan_bajo_lock`): declarar aca un
#: mutador nuevo no lo salva si se lo invoca fuera de un lock.
#:
#: P4-S3 (`journal.py`) escribe SOLO por delegacion declarada
#: (`state_module.write_json_atomic` / `reservar_ruta_json_exclusiva`): no tiene
#: mutadores propios, pero sus funciones `_locked` SI invocan las primitivas del
#: inventario, asi que se declaran aca y el ancla de call site las obliga a
#: correr bajo el root lock (ver `test_s3_toda_escritura_del_journal_corre_bajo_el_lock`).
_PRIMITIVAS_BAJO_LOCK: frozenset[tuple[str, str]] = frozenset(
    {
        ("state.py", "write_json_atomic"),
        ("state.py", "_save_frozen_runtime_state_locked"),
        ("storage.py", "_initialize_frozen_runtime_storage_locked"),
        ("candidates.py", "_exigir_candidates_state_dir"),
        ("candidates.py", "_reservar_candidate_id"),
        ("candidates.py", "_persistir_metadata"),
        ("candidates.py", "_persistir_metadata_inicial"),
        ("candidates.py", "_marcar_invalid"),
        ("candidates.py", "_crear_candidate_bajo_lock"),
        ("copying.py", "copiar_arbol_independiente"),
        ("copying.py", "copiar_archivo"),
        ("copying.py", "_materializar_directorio_del_destino"),
        ("journal.py", "_reservar_seq_locked"),
        ("journal.py", "_persistir_registro_locked"),
        ("journal.py", "_rotar_journal_locked"),
        ("journal.py", "_crear_pendiente_locked"),
        ("journal.py", "_start_next_locked"),
        ("journal.py", "_actualizar_locked"),
        ("journal.py", "_finalizar_locked"),
        # P4-S4 (`approval.py`): la autoridad de aprobación escribe por DOS
        # caminos, y ninguno adquiere el lock por su cuenta — reciben el
        # `FrozenRuntimeRootLockHandle` ya adquirido y lo verifican con
        # `handle.assert_owned()`. Declararlos acá los somete al ancla de call
        # site: si alguna vez se los invoca fuera de una sección crítica, el
        # test falla. Se declaran también `_crear_witness`, `_crear_tombstone` y
        # `_reconciliar_hacia_adelante` aunque el censo por AST no los vea
        # (llaman funciones del proyecto, no mutadores del inventario): el censo
        # NO es transitivo —límite declarado arriba— y esas tres son justamente
        # las que crean el witness y el tombstone, es decir la emisión y la
        # terminalización.
        ("approval.py", "_crear_json_inmutable"),
        ("approval.py", "_escribir_ledger"),
        ("approval.py", "_escribir_ledger_cas"),
        ("approval.py", "_emitir_aprobacion_bajo_lock"),
        ("approval.py", "_crear_witness"),
        ("approval.py", "_crear_tombstone"),
        ("approval.py", "_reconciliar_hacia_adelante"),
        # P5-C1 (`binder_recovery.py`): el registro durable y la restauración
        # reciben el handle ya adquirido por el binder o por
        # `recuperar_transicion_interrumpida`, que sí es un participante público.
        # Ninguna adquiere por su cuenta: declararlas acá las somete al ancla de
        # call site.
        ("binder_recovery.py", "escribir_registro_locked"),
        ("binder_recovery.py", "marcar_fase_locked"),
        ("binder_recovery.py", "eliminar_registro_locked"),
        ("binder_recovery.py", "restaurar_registro_locked"),
        ("binder_recovery.py", "_restaurar_persistida"),
        ("binder_recovery.py", "_escribir_bytes_atomico"),
        # P5-C2: preserva los bytes NO atribuibles antes de abstenerse de
        # restaurar. Corre dentro de `_restaurar_persistida`, es decir bajo el
        # handle que el llamador ya adquirió, y escribe en
        # `recovery_evidence/`, dentro del root. Declararla la somete al ancla de
        # call site: si alguien la invocara fuera de la sección crítica, falla.
        ("binder_recovery.py", "_preservar_evidencia"),
        # P5-C1 (`mo2_ini_escritura.py`): la escritura del INI es la mutación más
        # sensible del slice. Corre SIEMPRE bajo el participant lock del binder.
        ("mo2_ini_escritura.py", "actualizar_game_path_en_ini"),
        ("mo2_ini_escritura.py", "_escribir_atomico"),
        # P5-C1: `_rollback` corre dentro del `with participant_lock(...)` del
        # binder y recibe el handle ya adquirido; no vuelve a adquirir (el lock
        # no es reentrante). Declararla la somete al ancla de call site.
        ("runtime_clone_binder.py", "_rollback"),
        # P5-C1 ronda 2: el retiro del registro corre FUERA de la transacción,
        # con el handle ya adquirido por el binder.
        ("runtime_clone_binder.py", "_retirar_registro_finalizado"),
        # P5-C1: el cuerpo del CAS sobre `active.json`. Su hermano público
        # `escribir_estado_activo_cas` adquiere el lock; éste recibe la sección
        # crítica ya tomada por el binder o por el público.
        ("state.py", "_escribir_estado_activo_cas_locked"),
    }
)

#: Participantes publicos del slice: deben adquirir SIEMPRE. Es una enumeracion
#: CURADA y el limite esta declarado: `generations.py` escribe por DELEGACION
#: (`state.write_json_atomic`), asi que no aparece en el censo derivado por
#: mutadores directos y necesita su propia fila.
_PARTICIPANTES_PUBLICOS: dict[str, str] = {
    "generations.py": "registrar_generation_metadata",
    "candidates.py": "crear_candidate",
    "storage.py": "initialize_frozen_runtime_storage",
    "state.py": "save_frozen_runtime_state",
}

#: Garantia REAL por participante: la prueba de COMPORTAMIENTO que lo EJECUTA, no
#: el ancla AST (que es sintactica y tiene los limites declarados en
#: `test_s2_el_ancla_ast_es_necesaria_pero_no_suficiente`). Se enumeran los CUATRO:
#: tres tienen exclusion multiproceso en este modulo; `generations.py` no tiene
#: exclusion concurrente en este slice y su cobertura es funcional, en el modulo
#: P2. Declararlo como limite es parte de la evidencia: un participante sin
#: cobertura comportamental quedaria como hueco visible, no como omision silenciosa.
_COBERTURA_COMPORTAMENTAL: dict[str, tuple[str, ...]] = {
    "storage.py": (
        "tests/test_frozen_runtime_p4_s2_participant_wiring.py::test_s2_init_concurrente_es_excluyente_y_sin_deadlock",
    ),
    "state.py": (
        "tests/test_frozen_runtime_p4_s2_participant_wiring.py::test_s2_escritura_concurrente_de_estado_serializa",
        "tests/test_frozen_runtime_p4_s2_participant_wiring.py::test_s2_escritura_de_estado_bajo_contencion_nunca_corrompe",
    ),
    "candidates.py": (
        "tests/test_frozen_runtime_p4_s2_participant_wiring.py::test_s2_crear_candidate_concurrente_no_deadlock",
        "tests/test_frozen_runtime_p4_s2_participant_wiring.py::test_s2_candidate_colision_de_id_no_pisa_el_historial",
        "tests/test_frozen_runtime_p4_s2_participant_wiring.py::test_s2_candidate_con_root_ocupado_devuelve_indeterminate_acotado",
    ),
    "generations.py": ("tests/test_frozen_runtime_p2_generations.py::test_g06_mismo_id_misma_identidad_idempotente",),
}


def _existe_test_en_fuente(fuente: str, nombre: str) -> bool:
    """True si `fuente` DEFINE el test `nombre` como pytest lo COLECTA.

    Se resuelve por AST y no por substring: un `def <nombre>(` dentro de un
    COMENTARIO, de un DOCSTRING o de una funcion ANIDADA (que pytest no colecta)
    satisfaria un match textual y daria por viva una prueba que no existe —
    justamente lo contrario de lo que este ancla promete.

    Se aceptan las dos formas que pytest colecta en estos modulos con la config del
    repo (sin `python_classes`/`python_functions` propios, o sea los defaults):
    funcion de modulo, y metodo de una clase de modulo `Test*`. El segundo caso no
    es decorativo: `test_g06_mismo_id_misma_identidad_idempotente` (la cobertura
    comportamental de `generations.py`) vive dentro de `class TestGenerations`, y
    un chequeo limitado a `arbol.body` lo daria por inexistente.
    """
    arbol = ast.parse(fuente)
    for nodo in arbol.body:
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)) and nodo.name == nombre:
            return True
        if isinstance(nodo, ast.ClassDef) and nodo.name.startswith("Test"):
            for hijo in nodo.body:
                if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef)) and hijo.name == nombre:
                    return True
    return False


def _prueba_comportamental_existe(referencia: str) -> bool:
    """`<ruta-relativa-al-worktree>::<nombre-del-test>` existe de verdad.

    Ancla por ENUMERACION: se verifica que la prueba nombrada siga existiendo en su
    archivo, para que borrarla (o renombrarla) rompa este test en vez de degradar
    la cobertura en silencio. La resolucion sintactica vive en
    `_existe_test_en_fuente`, que se puede ejercitar con fuentes sinteticas.
    """
    archivo, _, nombre = referencia.partition("::")
    ruta = _RAIZ_WORKTREE / archivo
    if not ruta.is_file():
        return False
    return _existe_test_en_fuente(ruta.read_text(encoding="utf-8"), nombre)


def _funciones_top_level(arbol: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in arbol.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _llamadas_propias(funcion: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Call]:
    """Llamadas dentro de `funcion`, sin descender a funciones anidadas."""
    llamadas: list[ast.Call] = []
    pila: list[ast.AST] = list(funcion.body)
    while pila:
        nodo = pila.pop()
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(nodo, ast.Call):
            llamadas.append(nodo)
        pila.extend(ast.iter_child_nodes(nodo))
    return llamadas


def _contiene_mutador(funcion: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(_nombre_llamado(llamada) in MUTADORES_FILESYSTEM for llamada in _llamadas_propias(funcion))


def _funciones_mutadoras(nombre_modulo: str, fuente: str) -> set[str]:
    """Funciones top-level con un call site de mutador del inventario."""
    arbol = ast.parse(fuente, filename=nombre_modulo)
    return {funcion.name for funcion in _funciones_top_level(arbol) if _contiene_mutador(funcion)}


def _mutadores_sin_cobertura(nombre_modulo: str, fuente: str) -> set[str]:
    """Funciones que escriben en el root sin adquirir el lock ni estar declaradas."""
    adquirentes = _funciones_que_adquieren_lock(ast.parse(fuente, filename=nombre_modulo))
    declaradas = {funcion for (modulo, funcion) in _PRIMITIVAS_BAJO_LOCK if modulo == nombre_modulo}
    return _funciones_mutadoras(nombre_modulo, fuente) - adquirentes - declaradas


def _call_sites_de(nombre_funcion: str) -> list[tuple[str, str]]:
    """(modulo, funcion_contenedora) de cada invocacion de `nombre_funcion`."""
    sitios: list[tuple[str, str]] = []
    for ruta in sorted(_paquete().glob("*.py")):
        for funcion in _funciones_top_level(_arbol_de(ruta)):
            if any(_nombre_llamado(llamada) == nombre_funcion for llamada in _llamadas_propias(funcion)):
                sitios.append((ruta.name, funcion.name))
    return sitios


def test_s2_los_participantes_publicos_adquieren_el_lock() -> None:
    """Ancla curada: cada participante publico del slice adquiere el root lock."""
    for modulo, funcion in _PARTICIPANTES_PUBLICOS.items():
        adquirentes = _funciones_que_adquieren_lock(_arbol_de(_paquete() / modulo))
        assert funcion in adquirentes, f"{modulo}: {funcion} no adquiere el root lock"


def test_s2_censo_de_funciones_que_mutan_el_root() -> None:
    """Ancla DERIVADA: toda funcion que muta el root adquiere o es primitiva declarada.

    No hay lista fija de funciones: el censo se calcula del AST de cada modulo con
    escritura declarada. Un mutador nuevo sin lock (o una primitiva nueva no
    declarada) rompe el ancla.
    """
    for modulo in sorted(_escritores_reales_del_paquete() - {"root_lock.py"}):
        fuente = (_paquete() / modulo).read_text(encoding="utf-8")
        sin_cobertura = _mutadores_sin_cobertura(modulo, fuente)
        assert not sin_cobertura, (
            f"{modulo}: funciones que escriben en el root sin adquisicion ni declaracion: "
            f"{sorted(sin_cobertura)} -- si muta el root, necesita el root lock"
        )


def test_s2_las_primitivas_bajo_lock_solo_se_invocan_bajo_lock() -> None:
    """Las primitivas de bajo nivel no se pueden invocar fuera de una seccion critica.

    Cierra el silenciador: declarar una funcion en `_PRIMITIVAS_BAJO_LOCK` la exime
    del censo, pero TODOS sus call sites tienen que estar dentro de una funcion que
    adquiere el lock o de otra primitiva declarada.
    """
    primitivas = {funcion for (_, funcion) in _PRIMITIVAS_BAJO_LOCK}
    adquirentes_por_modulo: dict[str, set[str]] = {}
    for _, primitiva in sorted(_PRIMITIVAS_BAJO_LOCK):
        for modulo_origen, contenedora in _call_sites_de(primitiva):
            if modulo_origen not in adquirentes_por_modulo:
                adquirentes_por_modulo[modulo_origen] = _funciones_que_adquieren_lock(
                    _arbol_de(_paquete() / modulo_origen)
                )
            cubierta = contenedora in adquirentes_por_modulo[modulo_origen] or contenedora in primitivas
            assert cubierta, (
                f"{primitiva} se invoca desde {modulo_origen}::{contenedora}, que no adquiere el lock "
                "ni es una primitiva declarada"
            )


def test_s2_el_censo_rechaza_una_adquisicion_homonima() -> None:
    """Control negativo: `objeto.acquire()` NO cuenta como adquisicion.

    Sin esto, el censo pasaria por el nombre de un metodo cualquiera: la version
    original reconocia `acquire` a secas y daba por cableada una funcion que jamas
    tomo el root lock (F04).
    """
    falso = textwrap.dedent(
        """
        def mutador_falso(root):
            object.__new__(type("C", (), {"acquire": lambda self: None})).acquire()
            root.joinpath("x").write_text("boom")
        """
    )
    assert "mutador_falso" not in _funciones_que_adquieren_lock(ast.parse(falso))

    # Y una adquisicion REAL si se reconoce (el control no es vacuamente falso).
    real = textwrap.dedent(
        """
        from sky_claw.local.frozen_runtime.root_lock import participant_lock

        def mutador_real(root):
            with participant_lock(root, timeout=0.0) as h:
                h.assert_owned()
        """
    )
    assert "mutador_real" in _funciones_que_adquieren_lock(ast.parse(real))


def test_s2_el_censo_rechaza_un_participant_lock_local() -> None:
    """Control negativo (Finding 1): un `participant_lock` DEFINIDO localmente no adquiere.

    Es el defecto residual: la version previa devolvia True para cualquier
    `ast.Name` llamado `participant_lock`, asi que una funcion homonima local
    daba por cableado un mutador que nunca tomo el root lock.
    """
    falso = textwrap.dedent(
        """
        def participant_lock(root, timeout=0.0):
            return None

        def mutador_con_homonimo(root):
            with participant_lock(root, timeout=0.0):
                (root / "x").write_text("boom", encoding="utf-8")
        """
    )
    assert "mutador_con_homonimo" not in _funciones_que_adquieren_lock(ast.parse(falso))
    # Y el censo de mutadores lo ve como no cubierto (no se salva por el nombre).
    assert "mutador_con_homonimo" in _mutadores_sin_cobertura("fake.py", falso)


def test_s2_el_censo_reconoce_alias_y_modulo_autorizado() -> None:
    """Control positivo (Finding 1): alias y modulo importado SI cuentan.

    Sin esto, la correccion podria ser vacuamente estricta (rechazar todo) y el
    control negativo de arriba pasaria por la razon equivocada.
    """
    alias_entrypoint = textwrap.dedent(
        """
        from sky_claw.local.frozen_runtime.root_lock import participant_lock as pl

        def mutador_alias(root):
            with pl(root, timeout=0.0) as h:
                h.assert_owned()
        """
    )
    assert "mutador_alias" in _funciones_que_adquieren_lock(ast.parse(alias_entrypoint))

    modulo_alias = textwrap.dedent(
        """
        from sky_claw.local.frozen_runtime import root_lock as rl

        def mutador_modulo(root):
            with rl.participant_lock(root, timeout=0.0) as h:
                h.assert_owned()
        """
    )
    assert "mutador_modulo" in _funciones_que_adquieren_lock(ast.parse(modulo_alias))


def test_s2_el_censo_invalida_un_enlace_autorizado_obsoleto() -> None:
    """Control negativo (F06): un import POSTERIOR revincula el nombre local.

    `_tabla_de_enlaces` solo AGREGABA. Un import AJENO posterior que reutilizaba el
    nombre ya autorizado no satisfacia ninguna condicion y dejaba VIVO el enlace
    anterior, asi que el cuerpo se clasificaba como adquirente del root lock
    llamando a un objeto ajeno (cobertura falsa).

    Se enumeran los CUATRO ordenes relevantes -- no una muestra: los tres "ajeno
    sobre autorizado" deben quedar FUERA y el INVERSO debe seguir DENTRO, porque
    su enlace FINAL si es el autorizado. Un control que solo probara el primero
    dejaria pasar los otros dos, y uno que exigiera "ningun import ajeno" romperia
    el caso legitimo.

    No se comprueba solo el booleano: se comprueba la TABLA DE ENLACES, que es
    donde se ve que el analizador clasifica el simbolo que queda VINCULADO y no el
    texto del import.
    """
    casos: dict[str, tuple[str, str, str, str, bool]] = {
        # nombre: (primer_import, segundo_import, nombre_local, invocacion, debe_adquirir)
        "autorizado_luego_ajeno_mismo_nombre": (
            "from sky_claw.local.frozen_runtime.root_lock import participant_lock",
            "from unrelated.root_lock import participant_lock",
            "participant_lock",
            "participant_lock(root, timeout=0.0)",
            False,
        ),
        "autorizado_modulo_luego_ajeno_mismo_alias": (
            "from sky_claw.local.frozen_runtime import root_lock as rl",
            "from unrelated import root_lock as rl",
            "rl",
            "rl.participant_lock(root, timeout=0.0)",
            False,
        ),
        "autorizado_alias_luego_ajeno_mismo_alias": (
            "from sky_claw.local.frozen_runtime.root_lock import participant_lock as pl",
            "from unrelated.root_lock import participant_lock as pl",
            "pl",
            "pl(root, timeout=0.0)",
            False,
        ),
        "ajeno_luego_autorizado_enlace_final_autorizado": (
            "from unrelated.root_lock import participant_lock",
            "from sky_claw.local.frozen_runtime.root_lock import participant_lock",
            "participant_lock",
            "participant_lock(root, timeout=0.0)",
            True,
        ),
    }
    for nombre, (primero, segundo, local, invocacion, debe_adquirir) in casos.items():
        fuente = textwrap.dedent(
            f"""
            {primero}
            {segundo}

            def mutador(root):
                with {invocacion} as h:
                    h.assert_owned()
            """
        )
        arbol = ast.parse(fuente)
        tabla_modulo = _tabla_de_enlaces(arbol.body, (set(), set()))
        funcion = next(n for n in arbol.body if isinstance(n, ast.FunctionDef))
        tabla_funcion = _tabla_de_enlaces(funcion.body, tabla_modulo)
        adquiere = "mutador" in _funciones_que_adquieren_lock(ast.parse(fuente))

        if debe_adquirir:
            assert local in tabla_modulo[0] | tabla_modulo[1], (
                f"{nombre}: el enlace FINAL es el autorizado y debe quedar registrado en la tabla"
            )
            assert local in tabla_funcion[0] | tabla_funcion[1], f"{nombre}: el enlace debe alcanzar la funcion"
            assert adquiere, f"{nombre}: un enlace autorizado posterior SI debe contar como adquisicion"
        else:
            assert local not in tabla_modulo[0], (
                f"{nombre}: el import ajeno posterior debe EXPULSAR `{local}` de los entrypoints"
            )
            assert local not in tabla_modulo[1], (
                f"{nombre}: el import ajeno posterior debe EXPULSAR `{local}` de los modulos"
            )
            assert local not in tabla_funcion[0] | tabla_funcion[1], (
                f"{nombre}: el enlace obsoleto no debe sobrevivir al cuerpo de la funcion"
            )
            assert not adquiere, f"{nombre}: un enlace obsoleto NO puede dar cobertura al mutador"


def test_s2_el_ancla_ast_es_necesaria_pero_no_suficiente() -> None:
    """Limite declarado (Phase 3 de F06): el ancla es SINTACTICA, no semantica.

    Verificacion adversarial: el analizador NO evalua alcanzabilidad ni exige que
    la llamada al entrypoint se use como context manager. Se declaran las DOS
    coberturas falsas que eso admite para que nadie confunda el ancla con una
    garantia:

      - una adquisicion dentro de `if False:` SI cuenta;
      - una llamada al entrypoint SIN `with` (sin seccion critica efectiva) SI cuenta.

    Lo que SI se excluye, y se fija aca, es una mera REFERENCIA sin llamada. La
    garantia real sobre los CUATRO participantes son las pruebas de COMPORTAMIENTO
    que se enumeran abajo: si se borrara una, esta ancla lo delata (no se muestrea).
    """
    limite_inalcanzable = textwrap.dedent(
        """
        from sky_claw.local.frozen_runtime.root_lock import participant_lock

        def mutador(root):
            if False:
                with participant_lock(root, timeout=0.0) as h:
                    h.assert_owned()
        """
    )
    assert "mutador" in _funciones_que_adquieren_lock(ast.parse(limite_inalcanzable)), (
        "limite declarado: el ancla es sintactica, no evalua alcanzabilidad"
    )

    limite_sin_seccion_critica = textwrap.dedent(
        """
        from sky_claw.local.frozen_runtime.root_lock import participant_lock

        def mutador(root):
            participant_lock(root, timeout=0.0)
        """
    )
    assert "mutador" in _funciones_que_adquieren_lock(ast.parse(limite_sin_seccion_critica)), (
        "limite declarado: el ancla no exige que la llamada sea un context manager"
    )

    referencia_sin_llamada = textwrap.dedent(
        """
        from sky_claw.local.frozen_runtime.root_lock import participant_lock

        def mutador(root):
            fabrica = participant_lock
        """
    )
    assert "mutador" not in _funciones_que_adquieren_lock(ast.parse(referencia_sin_llamada)), (
        "una mera referencia al entrypoint NO es una adquisicion"
    )

    # El ancla de EXISTENCIA resuelve por AST, no por substring. Un match textual
    # (`"def <nombre>(" in fuente`) daria por viva una prueba que pytest NO
    # colecta: prosa en un comentario o un docstring, o una funcion ANIDADA.
    assert not _existe_test_en_fuente("# def fantasma():\nX = 1\n", "fantasma")
    assert not _existe_test_en_fuente('"""def fantasma()"""\n', "fantasma")
    assert not _existe_test_en_fuente("def externa():\n    def fantasma():\n        pass\n", "fantasma")
    assert not _existe_test_en_fuente("class Auxiliar:\n    def fantasma(self):\n        pass\n", "fantasma")
    # Y SI reconoce lo que pytest colecta: funcion de modulo (sync y async) y
    # metodo de una clase `Test*`.
    assert _existe_test_en_fuente("def fantasma():\n    pass\n", "fantasma")
    assert _existe_test_en_fuente("async def fantasma():\n    pass\n", "fantasma")
    assert _existe_test_en_fuente("class TestX:\n    def fantasma(self):\n        pass\n", "fantasma")

    for modulo, pruebas in _COBERTURA_COMPORTAMENTAL.items():
        assert pruebas, f"{modulo} es participante real y no declara prueba de comportamiento"
        for referencia in pruebas:
            assert _prueba_comportamental_existe(referencia), (
                f"{modulo}: falta la prueba de comportamiento que lo protege ({referencia})"
            )


def test_s2_el_censo_rechaza_un_origen_ajeno_por_sufijo() -> None:
    """Control negativo (F04-IMPORT-ORIGIN): el sufijo `root_lock` NO autoriza.

    `_tabla_de_enlaces` aceptaba cualquier modulo cuyo ultimo componente fuera
    `root_lock`, asi que `from unrelated.root_lock import participant_lock` contaba
    como adquisicion del FrozenRuntimeRoot lock. La ruta autorizada es COMPLETA.

    El control es doble a proposito: los origenes ajenos se rechazan Y los
    autorizados (con y sin alias) se siguen aceptando. Sin la segunda mitad, una
    correccion vacuamente estricta pasaria el test por la razon equivocada.
    """
    ajenos: dict[str, tuple[str, str]] = {
        "ImportFrom_de_modulo_ajeno": (
            "from unrelated.root_lock import participant_lock as adquirir",
            "adquirir(root, timeout=0.0)",
        ),
        "ImportFrom_de_paquete_ajeno": (
            "from unrelated import root_lock as rl",
            "rl.participant_lock(root, timeout=0.0)",
        ),
        "Import_de_modulo_ajeno": (
            "import unrelated.root_lock as rl",
            "rl.participant_lock(root, timeout=0.0)",
        ),
        "hermano_del_paquete_autorizado": (
            "from sky_claw.local.frozen_runtime.otro import participant_lock",
            "participant_lock(root, timeout=0.0)",
        ),
    }
    for nombre, (encabezado, invocacion) in ajenos.items():
        fuente = textwrap.dedent(
            f"""
            {encabezado}

            def mutador(root):
                with {invocacion} as h:
                    h.assert_owned()
            """
        )
        assert "mutador" not in _funciones_que_adquieren_lock(ast.parse(fuente)), (
            f"{nombre}: un origen ajeno con sufijo root_lock NO debe contar como adquisicion"
        )

    autorizados: dict[str, tuple[str, str]] = {
        "entrypoint_sin_alias": (
            "from sky_claw.local.frozen_runtime.root_lock import participant_lock",
            "participant_lock(root, timeout=0.0)",
        ),
        "entrypoint_con_alias": (
            "from sky_claw.local.frozen_runtime.root_lock import participant_lock as adquirir",
            "adquirir(root, timeout=0.0)",
        ),
        "acquire_con_alias": (
            "from sky_claw.local.frozen_runtime.root_lock import acquire_frozen_runtime_root_lock as acq",
            "acq(root, timeout=0.0)",
        ),
        "modulo_del_paquete_sin_alias": (
            "from sky_claw.local.frozen_runtime import root_lock",
            "root_lock.participant_lock(root, timeout=0.0)",
        ),
        "modulo_del_paquete_con_alias": (
            "from sky_claw.local.frozen_runtime import root_lock as rl",
            "rl.participant_lock(root, timeout=0.0)",
        ),
        "import_del_modulo_con_alias": (
            "import sky_claw.local.frozen_runtime.root_lock as rl",
            "rl.participant_lock(root, timeout=0.0)",
        ),
    }
    for nombre, (encabezado, invocacion) in autorizados.items():
        fuente = textwrap.dedent(
            f"""
            {encabezado}

            def mutador(root):
                with {invocacion} as h:
                    h.assert_owned()
            """
        )
        assert "mutador" in _funciones_que_adquieren_lock(ast.parse(fuente)), (
            f"{nombre}: un import AUTORIZADO debe seguir contando como adquisicion"
        )


def test_s2_el_censo_no_registra_un_import_sin_alias() -> None:
    """Control negativo: `import a.b.c` sin alias vincula `a`, no `c`.

    En Python `import sky_claw.local.frozen_runtime.root_lock` liga `sky_claw` en el
    ambito local, no `root_lock`. Registrarlo como si ligara `root_lock` habilitaria
    un receptor que en la practica no existe (y enmascararia un mutador sin lock).
    """
    sin_alias = textwrap.dedent(
        """
        import sky_claw.local.frozen_runtime.root_lock

        def mutador(root):
            with root_lock.participant_lock(root, timeout=0.0) as h:
                h.assert_owned()
        """
    )
    assert "mutador" not in _funciones_que_adquieren_lock(ast.parse(sin_alias))

    # Con alias SI liga el nombre local: el mismo cuerpo pasa a contar.
    con_alias = textwrap.dedent(
        """
        import sky_claw.local.frozen_runtime.root_lock as root_lock

        def mutador(root):
            with root_lock.participant_lock(root, timeout=0.0) as h:
                h.assert_owned()
        """
    )
    assert "mutador" in _funciones_que_adquieren_lock(ast.parse(con_alias))


def test_s2_el_censo_rechaza_un_receptor_homonimo() -> None:
    """Control negativo (Finding 1): `rl.participant_lock(...)` con `rl` local no cuenta.

    La version previa aceptaba un receptor por NOMBRE (`rl` estaba en una lista
    fija), sin comprobar que viniera de un import del modulo autorizado.
    """
    falso = textwrap.dedent(
        """
        class Fake:
            def participant_lock(self, root, timeout=0.0):
                return None

        def mutador_falso(root):
            rl = Fake()
            with rl.participant_lock(root, timeout=0.0):
                (root / "x").write_text("boom", encoding="utf-8")
        """
    )
    assert "mutador_falso" not in _funciones_que_adquieren_lock(ast.parse(falso))


def test_s2_el_censo_rechaza_un_mutador_nuevo_sin_lock() -> None:
    """Control negativo (Finding 2): un mutador publico NUEVO sin lock da RED.

    Se simula la incorporacion de `guardar_nuevo_estado` a `state.py` (modulo ya
    contemplado) que escribe sin adquirir. El censo previo no lo veia; el derivado
    si.
    """
    original = (_paquete() / "state.py").read_text(encoding="utf-8")
    assert _mutadores_sin_cobertura("state.py", original) == set()

    mutado = original + textwrap.dedent(
        """

        def guardar_nuevo_estado(root, payload):
            (root / "state" / "nuevo.json").write_text(payload, encoding="utf-8")
        """
    )
    sin_cobertura = _mutadores_sin_cobertura("state.py", mutado)
    assert "guardar_nuevo_estado" in sin_cobertura, "un mutador publico nuevo sin lock debe quedar sin cobertura (RED)"


def test_s2_cada_modulo_escritor_declara_su_adquisicion_si_muta_el_root() -> None:
    """Un modulo escritor que muta el root sin adquirir el lock es un agujero.

    Se enumeran los modulos con escritura permitida y se verifica que los que
    tienen un call site de mutacion del root tambien tengan adquisicion. Los
    delegadores (`generations.py` escribe via `state.write_json_atomic`) quedan
    cubiertos por su entrypoint publico en el censo anterior.
    """
    # `mo2_ini_escritura.py` es una PRIMITIVA de escritura, igual que
    # `copying.py`: no adquiere el lock porque su contrato es correr dentro de
    # la sección crítica del binder. El ancla de call site de abajo la obliga.
    delegados = {"copying.py", "root_lock.py", "mo2_ini_escritura.py"}
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


def _recolectar(procesos: list[subprocess.Popen[str]]) -> None:
    """Termina y recolecta los hijos que sigan vivos. Nunca deja procesos colgados.

    Se usa en el `finally` de los tests de contencion: un fallo de asercion no
    puede dejar un escritor reteniendo el lock (bloquearia a los tests siguientes
    del modulo, que comparten la maquina) ni descriptores de pipe sin cerrar. Un
    hijo que ya termino se recolecta sin efectos; `communicate` sobre un proceso
    ya recolectado devuelve inmediatamente.
    """
    for proc in procesos:
        if proc.poll() is None:
            proc.terminate()
    for proc in procesos:
        try:
            proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - solo si terminate no alcanza
            proc.kill()
            proc.communicate()


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

    Sincronizacion DETERMINISTA (estabilizacion). La version previa lanzaba los
    CUATRO procesos a la vez con una barrera que nunca se liberaba: el ganador
    agotaba su espera interna de 60 s dentro de la seccion critica, lo que metia
    ~60 s de latencia artificial en cada corrida (80-110 s de test contra el cap
    `--timeout=120` de `pyproject.toml`) y lo volvia intermitente bajo carga
    (medido: 1 fallo en 4 corridas locales). Ahora la secuencia es por EVENTOS,
    nunca por reloj ni por orden de planificacion:

    1. se lanza SOLO el primer escritor y se espera la senal que el mismo crea
       desde DENTRO de su seccion critica (el `os.replace` del commit atomico);
    2. con el lock YA retenido se lanzan los tres competidores;
    3. los tres deben fallar CERRADO con `LockBusy`;
    4. recien entonces se libera al ganador, que debe terminar con exito.

    La liberacion vive en un `finally` junto con la recoleccion de hijos: un fallo
    de asercion no puede dejar al ganador girando ni un proceso sin recolectar.
    """
    root = _crear_root(tmp_path)
    assert initialize_frozen_runtime_storage(root).success
    solapamiento = tmp_path / "solapamiento.txt"
    solapamiento.write_text("", encoding="utf-8")
    # La barrera de liberacion NO existe todavia: el ganador entra a la seccion
    # critica y espera ahi hasta que el test la cree.
    liberar = tmp_path / "liberar.txt"

    def _args(indice: int) -> dict:
        return {
            "solapamiento": str(solapamiento),
            "dentro": str(tmp_path / f"dentro_{indice}.txt"),
            "mantener_hasta": str(liberar),
            "desired": f"1.6.1170__{indice:012x}",
            "updated_at_ns": 1000 + indice,
        }

    ganador: subprocess.Popen[str] | None = None
    competidores: list[subprocess.Popen[str]] = []
    try:
        # 1. Primer escritor: entra a su seccion critica real y se queda ahi.
        ganador = _lanzar("estado", root, _args(0))
        assert _esperar_archivo(tmp_path / "dentro_0.txt"), (
            "el primer escritor no llego a su seccion critica: no hay lock retenido que medir"
        )

        # 2. Competidores mientras el lock esta RETENIDO. `timeout=0.0` ⇒ fail-fast.
        competidores = [_lanzar("estado", root, _args(i)) for i in (1, 2, 3)]

        # 3. Cada competidor falla CERRADO con el error TIPADO del lock.
        for indice, proc in enumerate(competidores, start=1):
            out, err = proc.communicate(timeout=90)
            assert proc.returncode != 0, (
                f"el competidor {indice} adquirio un lock que el ganador retenia (exclusion perdida); stdout={out!r}"
            )
            assert "LockBusy" in err, f"perdida inesperada en el competidor {indice}: {err!r}"

        # Exclusion REAL, no un timeout: cada hijo escribe su PROPIO archivo desde
        # dentro de la seccion critica, asi que la evidencia es por proceso y no
        # depende de una escritura compartida (que seria racy). Si algun competidor
        # hubiera entrado, su archivo estaria aca.
        intrusos = sorted(p.name for p in tmp_path.glob("dentro_*.txt") if p.name != "dentro_0.txt")
        assert not intrusos, f"escritores que entraron a la seccion critica sin el lock: {intrusos}"

        # 4. Liberar al ganador y exigir que COMPLETE (no que se caiga).
        liberar.write_text("liberar", encoding="utf-8")
        assert _resultado(ganador, timeout=60)["ok"] is True
    finally:
        # Incondicional: sin esto, un fallo de asercion dejaria al ganador girando
        # hasta su cota interna y a los hijos sin recolectar.
        liberar.write_text("liberar", encoding="utf-8")
        _recolectar([p for p in [ganador, *competidores] if p is not None])

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
