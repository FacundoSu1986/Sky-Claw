"""Anclas de seguridad de GP2-S4D: qué NO puede hacer la finalización.

El problema dominante de un slice transaccional no es el camino feliz — es que
alguien (o un refactor) quite una garantía y nada lo note hasta que un Golden
queda con ``COMMITTED`` sin backup. Estos tests son el instrumento que lo
impide, y son de la familia que el repo ya usa: congelar la SUPERFICIE por
igualdad literal o por AST, no escribir un caso para el hermano que uno se
acordó de mirar.

Dos mitades:

* **Anclas de superficie (AST / igualdad literal).** Congelan el conjunto de
  módulos y call-sites autorizados. Si mañana un tercer módulo llama a
  ``commit_finalized``, o S4-D importa un lector de staging, la lista congelada
  se desactualiza y el test falla. No verifica comportamiento: verifica que la
  frontera siga teniendo la forma que se decidió.
* **Mutantes (comportamiento).** Comprueban que el orden de los pasos es
  observable desde el resultado, mutando la evidencia que el orquestador
  consume. Un mutant que pasara es un mutant que rompió una garantía.

Ninguna de las dos mitades cubre a la otra: una superficie correcta con lógica
invertida pasa el AST y falla el comportamiento, y al revés.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from sky_claw.local.runtime_vault import protection_journal_store
from sky_claw.local.runtime_vault.finalization_orchestrator import (
    FinalizationAuthorityError,
    FinalizationDisposition,
)
from sky_claw.local.runtime_vault.protection_journal import ProtectionTransactionState
from sky_claw.local.runtime_vault.protection_journal_store import open_protection_journal
from tests.test_runtime_vault_s4d_finalization import (
    _OTRA_OPERACION,
    _finalize,
    _Harness,
    _journal_path,
    _KernelDiario,
    _obs,
    _plan,
    _resolver,
)

_RV = pathlib.Path(protection_journal_store.__file__).parent
_ORCH = _RV / "finalization_orchestrator.py"
_ARCHIVE = _RV / "golden_backup_archive.py"
_VERIF = _RV / "finalization_verification.py"

#: Conjunto CERRADO de módulos que participan de la finalización. Un cuarto
#: módulo nuevo tiene que romper este test, no aparecer en silencio: la
#: pregunta "¿quién más puede escribir COMMITTED?" tiene que tener una sola
#: respuesta, y una lista congelada es la que la hace verificable.
S4D_AUTHORIZED_MODULES: frozenset[str] = frozenset(
    {
        "finalization_orchestrator.py",
        "finalization_verification.py",
        "golden_backup_archive.py",
    }
)

#: Símbolos que sólo S4-D puede usar para tocar la frontera de commit. Deliberadamente
#: NO incluye ``archive_golden_backup``/``load_durable_golden_backup``: ésos son el
#: store de lectura/escritura del backup, y su posición se ancla aparte (ver
#: ``test_el_archivado_ocurre_antes_del_commit_...``). Lo que esta lista congela
#: es quién puede PRODUCIR un COMMITTED y quién puede acuñar autoridad durable.
_SIMBOLOS_DE_LA_FRONTERA: frozenset[str] = frozenset(
    {
        "commit_finalized",
        "DurableGoldenBackupArchive",
    }
)


# ============================================================================
# Anclas de superficie
# ============================================================================


def _arbol_de(modulo: pathlib.Path) -> ast.Module:
    return ast.parse(modulo.read_text(encoding="utf-8"))


def _nombres_de_llamada(arbol: ast.Module) -> set[str]:
    """Nombres de TODO atributo/función llamado en el módulo (call-sites)."""
    nombres: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call):
            func = nodo.func
            if isinstance(func, ast.Name):
                nombres.add(func.id)
            elif isinstance(func, ast.Attribute):
                nombres.add(func.attr)
    return nombres


def _simbolos_referenciados(modulo: pathlib.Path) -> set[str]:
    """Nombres y atributos REFERENCIADOS en código (excluye comentarios/strings).

    Es la diferencia entre un anchor que verifica una frontera y uno que verifica
    un comentario: el docstring de S4-D menciona ``SetSecurityInfo`` para explicar
    POR QUÉ no lo llama, y un grep lo leería como una violación.
    """
    arbol = _arbol_de(modulo)
    nombres: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Name):
            nombres.add(nodo.id)
        elif isinstance(nodo, ast.Attribute):
            nombres.add(nodo.attr)
    return nombres


def _llamadas_ordenadas_por_linea(funcion: ast.FunctionDef, nombres: set[str]) -> list[str]:
    """Secuencia de llamadas de ``funcion``, en ORDEN DE FUENTE.

    ``ast.walk`` no garantiza orden, así que un anchor de orden de llamadas
    hecho sobre su salida no distinguiría "commit antes de release" de su
    inverso. Se filtra por función para no mezclar ramas.
    """
    encontradas: list[tuple[int, str]] = []
    for nodo in ast.walk(funcion):
        if not isinstance(nodo, ast.Call):
            continue
        func = nodo.func
        nombre = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if nombre in nombres:
            encontradas.append((nodo.lineno, nombre))
    return [nombre for _, nombre in sorted(encontradas)]


def _funcion(modulo: pathlib.Path, nombre: str) -> ast.FunctionDef:
    return next(
        nodo for nodo in ast.walk(_arbol_de(modulo)) if isinstance(nodo, ast.FunctionDef) and nodo.name == nombre
    )


def _imports_de(modulo: pathlib.Path) -> set[str]:
    """Módulos de runtime_vault importados por el módulo dado (call-sites de API)."""
    arbol = _arbol_de(modulo)
    importados: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module and "runtime_vault" in nodo.module:
            importados.add(nodo.module)
        elif isinstance(nodo, ast.Import):
            for alias in nodo.names:
                if "runtime_vault" in alias.name:
                    importados.add(alias.name)
    return importados


def test_el_conjunto_de_modulos_autorizados_para_finalizar_esta_congelado() -> None:
    """La frontera de S4-D son TRES módulos, y son los mismos mañana.

    Congelar el conjunto —y no escribir un caso por módulo— es lo que hace que
    un cuarto participante aparezca como rotura y no como detalle.
    """
    assert {
        "finalization_orchestrator.py",
        "finalization_verification.py",
        "golden_backup_archive.py",
    } == S4D_AUTHORIZED_MODULES
    for nombre in S4D_AUTHORIZED_MODULES:
        assert (_RV / nombre).is_file(), f"el módulo autorizado '{nombre}' no existe"


def test_solo_el_orchestrator_llama_a_los_simbolos_de_la_frontera_de_commit() -> None:
    """Producir un COMMITTED tiene UN call-site en todo el paquete.

    Se cuenta por AST sobre todos los módulos de ``runtime_vault`` en vez de por
    grep: un ``getattr(journal, "commit_" + "finalized")`` no lo vería un
    regex, y una búsqueda de texto tampoco detectaría una llamada indirecta.
    La lista de call-sites se congela por igualdad literal.

    ``DurableGoldenBackupArchive`` aparece SÓLO en el propio store, y por su
    constructor. Eso es deliberado: la clase tiene prueba de acuñación privada,
    así que llamarla desde otro módulo no es una forma de acuñar autoridad
    (``_MINT_PROOF`` no se exporta). El store es el único que puede fabricar un
    backup "durable".
    """
    call_sites: list[str] = []
    for modulo in sorted(_RV.glob("*.py")):
        for nombre in _nombres_de_llamada(_arbol_de(modulo)):
            if nombre in _SIMBOLOS_DE_LA_FRONTERA:
                call_sites.append(f"{modulo.name}:{nombre}")
    assert sorted(call_sites) == [
        "finalization_orchestrator.py:commit_finalized",
        "golden_backup_archive.py:DurableGoldenBackupArchive",
    ]


def test_s4d_no_importa_ningun_lector_de_staging() -> None:
    """M-D8: la recuperación no puede tomar autoridad de staging.

    Se congela la lista de MÓDULOS importados, no la de símbolos: un
    ``from ... import read_candidate_manifest_bytes`` con otro alias seguiría
    trayendo staging, y la importación es la superficie que hay que cerrar.
    """
    for modulo in (_ORCH, _ARCHIVE, _VERIF):
        assert "staging" not in modulo.name
        assert not any("staging" in importado for importado in _imports_de(modulo)), (
            f"{modulo.name} importa un módulo de staging: el backup y la verificación de S4-D "
            "no pueden leer evidencia no autoritativa"
        )
    # Y el texto de los módulos tampoco puede citar las dos funciones que leen
    # staging, ni siquiera en un comentario que normalice su uso.
    for modulo in (_ORCH, _ARCHIVE, _VERIF):
        contenido = modulo.read_text(encoding="utf-8")
        assert "read_candidate_manifest_bytes" not in contenido
        assert "seal_golden_protection_plan" not in contenido


def test_s4d_no_escribe_el_tgr() -> None:
    """M: S4-D no escribe el Trusted Golden Registry.

    El TGR es la admission authority; escribirlo desde la finalización
    permitiría que una operación de protección altere qué se considera Golden
    autorizado. Ni siquiera un ``apply_canonical_tgr_file_security``.
    """
    for modulo in (_ORCH, _ARCHIVE, _VERIF):
        referenciados = _simbolos_referenciados(modulo)
        for prohibido in (
            "apply_canonical_tgr_file_security",
            "bootstrap_trusted_namespace",
            "write_trusted_golden_registry",
        ):
            assert prohibido not in referenciados, f"{modulo.name} referencia {prohibido}: S4-D no escribe el TGR"
        # Tampoco puede importar el módulo del TGR entero. Se permite
        # `trusted_registry_lock` (de ahí sale el resolver de ProgramData, que
        # sólo lee); lo que no se permite es el módulo que MUTA el registro.
        assert "sky_claw.local.runtime_vault.trusted_registry" not in _imports_de(modulo)


def test_s4d_no_invoca_setsecurityinfo_ni_construye_target_dacl() -> None:
    """M: S4-D no muta ACLs. Es un finalizador, no un mutador.

    La Target DACL ya se aplicó y ya se verificó por S4-B. Si S4-D volviera a
    llamar a ``apply_target_dacl_by_handle``, existiría una segunda superficie
    de mutación con su propia lógica de revalidación — la clase de defecto que
    el repoRt документа como "arreglar un hermano y no al otro".
    """
    for modulo in (_ORCH, _ARCHIVE, _VERIF):
        referenciados = _simbolos_referenciados(modulo)
        for prohibido in (
            "SetSecurityInfo",
            "apply_target_dacl_by_handle",
            "verify_target_dacl_by_handle",
            "build_target_dacl_spec",
        ):
            assert prohibido not in referenciados, (
                f"{modulo.name} referencia {prohibido}: S4-D es un finalizador, no un mutador de ACLs"
            )


def test_s4d_no_introduce_se_debug_privilege() -> None:
    """M: S4-D no pide ``SeDebugPrivilege``.

     GP2 no necesita tomar la propiedad de lo que el usuario ya es dueño. Pedir
     debug abre la puerta a leer o mutar memoria de otros procesos, y eso es
    ORÁ de lo que S4-B ya se abstuvo.
    """
    for modulo in (_ORCH, _ARCHIVE, _VERIF):
        referenciados = {nombre.lower() for nombre in _simbolos_referenciados(modulo)}
        assert "sedebugprivilege" not in referenciados
        assert "se_backup_privilege" not in referenciados


def test_s4d_no_acepta_una_raiz_arbitraria_del_caller() -> None:
    """M: S4-D no recibe un ``path`` de Golden del caller.

    La única raíz que se observa es ``plan.canonical_root``: el plan es
    autoridad durable y su raíz está ligada por identidad física (volumen +
    FileId). Un parámetro ``golden_root`` en la API pública sería una vía
    para verificar el árbol equivocado y comitear igual.
    """
    arbol = _arbol_de(_ORCH)
    firma = next(
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.FunctionDef) and nodo.name == "finalize_protection_transaction"
    )
    argumentos = [arg.arg for arg in firma.args.kwonlyargs]
    assert argumentos == [
        "operation_id",
        "plan",
        "journal",
        "port",
        "programdata_resolver",
        "archive_writer",
        "lock_kernel",
        "session",
        "rollback_notifier",
    ]
    for prohibido in ("golden_root", "root", "canonical_root", "path", "candidates"):
        assert prohibido not in argumentos


def test_la_liberacion_del_lock_ocurre_despues_del_commit_en_el_orden_del_codigo() -> None:
    """M-D2: ``release()`` no puede preceder a ``commit_finalized()``.

    Se ancla por ORDEN DE FUENTE dentro de cada función, no por una aserción
    de resultado: el orden de las llamadas ES la propiedad, y un test que sólo
    mirara el disposition final pasaría aunque el código soltara el lock
    primero y comiteara después (la ventana del caso R de la matriz).

    Se recorre cada función por separado porque hay dos ramas —el camino feliz
    y la normalización del huérfano— y en las dos el commit va primero. La
    normalización no llama a ``commit_finalized`` porque el commit ya ocurrió
    antes, en otro proceso: por eso su assert es distinto y explícito.
    """
    camino_feliz = _llamadas_ordenadas_por_linea(
        _funcion(_ORCH, "finalize_protection_transaction"),
        {"commit_finalized", "release"},
    )
    assert camino_feliz == ["commit_finalized", "release"], (
        f"el camino feliz cambió de orden: {camino_feliz}. Un release antes del commit abre una "
        "ventana donde otra mutadora entra sobre un Golden que puede terminar en ROLLBACK_REQUIRED"
    )

    # La normalización del huérfano post-COMMITTED no commitea (el commit ya
    # ocurrió) pero tampoco puede liberar nada antes de haber releído el backup.
    normalizacion = _llamadas_ordenadas_por_linea(
        _funcion(_ORCH, "_normalizar_commit_ya_durable"),
        {"load_durable_golden_backup", "commit_finalized", "release"},
    )
    assert normalizacion == ["load_durable_golden_backup", "release"], (
        f"la normalización del lock huérfano cambió de orden: {normalizacion}"
    )


def test_el_archivado_ocurre_antes_del_commit_en_el_orden_del_codigo() -> None:
    """M-D1: el backup se publica antes de escribir COMMITTED.

    Misma técnica y mismo motivo: el orden de los pasos ES la garantía, y una
    garantía que sólo se comprueba mirando el resultado final no distingue
    "archivó y comiteó" de "comiteó y después archivó, salvo que algo fallara".
    """
    orden = _llamadas_ordenadas_por_linea(
        _funcion(_ORCH, "finalize_protection_transaction"),
        {"archive_golden_backup", "commit_finalized"},
    )
    assert orden == ["archive_golden_backup", "commit_finalized"]


def test_el_store_de_s4a_sigue_sin_poder_escribir_committed() -> None:
    """SB-04 (heredado) sigue valiendo: ``transition_to`` no alcanza COMMITTED.

    S4-D abrió una puerta nueva (``commit_finalized``); NO abrió la vieja. Un
    cambio que hiciera ``transition_to`` alcanzable para COMMITTED sería la
    regresión exacta que S4-A cerró.
    """
    contenido = protection_journal_store.__file__
    texto = pathlib.Path(contenido).read_text(encoding="utf-8")
    assert "PrematureCommitError" in texto
    # Y el único `state=ProtectionTransactionState.COMMITTED` del store es el de
    # `commit_finalized`, que exige el backup durable.
    apariciones = texto.count("state=ProtectionTransactionState.COMMITTED")
    assert apariciones == 1, (
        f"se encontraron {apariciones} escrituras de COMMITTED en el store; sólo commit_finalized puede"
    )


def test_los_estados_de_finalizacion_son_los_que_declara_el_adr() -> None:
    """El conjunto de fases de S4-D se congela contra §19.2.

    Si mañana se agrega ``VERIFYING_ALGO`` "sólo por robustez", este test
    falla. Agregar una fase nueva es una decisión de contrato, no un refactor.
    """
    from sky_claw.local.runtime_vault.protection_journal_store import (
        _S4D_FINALIZATION_ENTRY_POINTS,
        _S4D_FINALIZATION_TARGETS,
    )

    assert {
        ProtectionTransactionState.VERIFYING_GP1,
        ProtectionTransactionState.VERIFYING_RV2,
        ProtectionTransactionState.VERIFYING_NODE_SET,
        ProtectionTransactionState.ARCHIVING_BACKUP,
    } == _S4D_FINALIZATION_TARGETS
    assert {estado.value for estado in _S4D_FINALIZATION_ENTRY_POINTS} == {
        "verifying_gp1",
        "verifying_rv2",
        "verifying_node_set",
        "archiving_backup",
    }
    # Y cada fase tiene exactamente los predecesores que §19.2 dibuja.
    assert _S4D_FINALIZATION_ENTRY_POINTS[ProtectionTransactionState.VERIFYING_GP1] == {
        ProtectionTransactionState.APPLYING,
        ProtectionTransactionState.VERIFYING_GP1,
    }
    assert _S4D_FINALIZATION_ENTRY_POINTS[ProtectionTransactionState.VERIFYING_RV2] == {
        ProtectionTransactionState.VERIFYING_GP1
    }
    assert _S4D_FINALIZATION_ENTRY_POINTS[ProtectionTransactionState.VERIFYING_NODE_SET] == {
        ProtectionTransactionState.VERIFYING_RV2
    }
    assert _S4D_FINALIZATION_ENTRY_POINTS[ProtectionTransactionState.ARCHIVING_BACKUP] == {
        ProtectionTransactionState.VERIFYING_NODE_SET
    }


# ============================================================================
# Mutantes: el orden y la frescura se comprueban por COMPORTAMIENTO
# ============================================================================


def test_m_d3_una_vez_evaluado_rv2_no_se_reutiliza_tras_un_crash(tmp_path: pathlib.Path) -> None:
    """M-D3/M-D10: tras un crash, el RV-2 se VUELVE a observar.

    Se reproduce el estado durable en que queda el sistema tras un crash en
    D02/D03 (``VERIFYING_RV2`` durable) y se comprueba que el proceso que
    retoma vuelve a ejecutar el gate. Un flag "RV2_OK" en memoria —o peor, un
    flag durable— haría que esta llamada no lo volviera a mirar.
    """
    h = _Harness(tmp_path)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_GP1)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_RV2)

    # El proceso B arranca con un port QUE NO SABE NADA del proceso A, y cuyo
    # RV-2 falla. Si S4-D reutilizara un resultado previo, comitearía.
    h.puerto.rv2 = _obs(False, "el_contenido_cambio_mientras_el_proceso_A_estaba_muerto", "rv2")
    reporte = _finalize(h)

    assert reporte.disposition is FinalizationDisposition.ROLLBACK_REQUIRED
    assert h.puerto.llamadas == ["rv2"], "el RV-2 se re-observa; no se recuerda"
    assert not h.backup_path().exists()


def test_m_d4_una_vez_evaluado_gp1_no_se_reutiliza_tras_un_crash(tmp_path: pathlib.Path) -> None:
    """M-D4: el GP1 final también se re-observa tras un crash.

    Simétrico al anterior y por el mismo motivo: «S4-B verificó cada nodo» es
    una afirmación PRE-apply, y usarla como evidencia POST-apply es el defecto
    que el enunciado prohíbe explícitamente.
    """
    h = _Harness(tmp_path)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_GP1)

    h.puerto.gp1 = _obs(False, "el_estado_de_proteccion_cambio", "gp1")
    reporte = _finalize(h)

    assert reporte.disposition is FinalizationDisposition.ROLLBACK_REQUIRED
    assert h.puerto.llamadas == ["gp1"]
    assert h.journal_estado() is ProtectionTransactionState.ROLLBACK_REQUIRED


def test_m_d5_no_se_puede_saltar_el_nodeset(tmp_path: pathlib.Path) -> None:
    """M-D5: no hay camino que llegue a COMMITTED sin pasar el NodeSet.

    Se fuerza el journal a ``VERIFYING_RV2`` (sin gates ejecutados) y se
    comprueba que el orquestador NO confía en el estado: vuelve a verificar
    desde ahí, y el NodeSet se ejecuta.

    Y el otro lado de la misma moneda: al reanudar desde ``ARCHIVING_BACKUP``
    el FSM de §19.2 no permite retroceder a VERIFYING_NODE_SET —y por eso no se
    re-observa el NodeSet, sino que se re-observa el RV-2 previo al
    archivado. Que esa sea la puerta correcta en vez de un hueco es lo que
    comprueba la segunda mitad de este test.
    """
    h = _Harness(tmp_path)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_GP1)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_RV2)

    h.puerto.node_set = _obs(False, "un_nodo_aparecio_desde_el_ultimo_ciclo", "node_set")
    reporte = _finalize(h)

    assert reporte.disposition is FinalizationDisposition.ROLLBACK_REQUIRED
    assert h.puerto.llamadas == ["rv2", "node_set"], "el NodeSet no se puede saltar"
    assert h.journal_estado() is ProtectionTransactionState.ROLLBACK_REQUIRED

    # Y desde ARCHIVING_BACKUP la puerta es el RV-2 previo al archivo.
    h2 = _Harness(tmp_path / "desde_archiving")
    for fase in (
        ProtectionTransactionState.VERIFYING_GP1,
        ProtectionTransactionState.VERIFYING_RV2,
        ProtectionTransactionState.VERIFYING_NODE_SET,
        ProtectionTransactionState.ARCHIVING_BACKUP,
    ):
        h2.journal.enter_finalization_phase(fase)
    h2.puerto.rv2 = _obs(False, "el_contenido_cambio_durante_el_crash_del_proceso_anterior", "rv2")

    reporte2 = _finalize(h2)

    assert reporte2.disposition is FinalizationDisposition.ROLLBACK_REQUIRED
    assert h2.puerto.llamadas == ["rv2"]
    assert not h2.backup_path().exists(), "sin RV-2 fresco no se archiva"


def test_m_d6_el_backup_se_revalida_antes_del_commit(tmp_path: pathlib.Path) -> None:
    """M-D6: un backup ya presente se RE-LEE y se compara byte a byte.

    Se publica un backup legítimo, se trunca en el medio y se reintenta la
    finalización. Un store que aceptara el objeto por existencia —en vez de por
    equivalencia de bytes— comitearía sobre evidencia corrupta.
    """
    h = _Harness(tmp_path)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_GP1)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_RV2)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_NODE_SET)
    h.journal.enter_finalization_phase(ProtectionTransactionState.ARCHIVING_BACKUP)
    h._publicar_backup()

    destino = h.backup_path()
    bytes_completos = destino.read_bytes()
    destino.write_bytes(bytes_completos[: len(bytes_completos) // 2])

    reporte = _finalize(h)

    assert reporte.disposition is FinalizationDisposition.INDETERMINATE
    assert not reporte.committed
    # Y el archivo corrupto NO fue sobrescrito ni reparado en silencio.
    assert destino.read_bytes() == bytes_completos[: len(bytes_completos) // 2]


def test_m_d7_un_backup_de_otra_operacion_no_habilita_el_commit(tmp_path: pathlib.Path) -> None:
    """M-D7: que exista un archivo donde va el backup no habilita el COMMITTED.

    Se pone ahí un manifiesto de una operación DISTINTA, con la misma forma y
    un backup de nodos completo. La forma no es la identidad: el store exige
    que el ``operation_id`` y el ``authorized_plan_digest`` atan.
    """
    h = _Harness(tmp_path)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_GP1)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_RV2)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_NODE_SET)
    h.journal.enter_finalization_phase(ProtectionTransactionState.ARCHIVING_BACKUP)

    otro_plan = _plan(operation_id=_OTRA_OPERACION)
    from sky_claw.local.runtime_vault.golden_backup_archive import build_golden_backup_archive

    destino = h.backup_path()
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(build_golden_backup_archive(otro_plan).canonical_bytes())

    reporte = _finalize(h)

    assert reporte.disposition is FinalizationDisposition.INDETERMINATE
    assert not reporte.committed
    assert h.journal_estado() is ProtectionTransactionState.ARCHIVING_BACKUP


def test_m_d9_el_recovery_no_acepta_el_lock_de_otra_operacion(tmp_path: pathlib.Path) -> None:
    """M-D9: un COMMITTED durable NO justifica tomar el lock de otro.

    Es la intersección de §15 con el caso S de la matriz: el journal dice
    COMMITTED (de la operación X) pero el lock en disco es de la operación Y.
    Normalizar ese lock sería tomar el Golden de otra transacción. S4-D no lo
    hace: clasifica y deja evidencia para el operador.
    """
    h = _Harness(tmp_path)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_GP1)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_RV2)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_NODE_SET)
    h.journal.enter_finalization_phase(ProtectionTransactionState.ARCHIVING_BACKUP)
    h._publicar_backup()
    h.journal.commit_finalized(
        archive=h._cargar_backup(),
        plan=h.plan,
    )
    assert h.journal_estado() is ProtectionTransactionState.COMMITTED

    # Ahora el lock en disco pasa a ser de otra operación.
    h._sembrar_lock_de_otra_operacion()

    reporte = _finalize(h)

    assert reporte.disposition is FinalizationDisposition.INDETERMINATE
    assert reporte.lock.acquired is False
    assert h.puerto.llamadas == []


def test_m_d10_el_estado_durable_manda_sobre_la_memoria_del_proceso(tmp_path: pathlib.Path) -> None:
    """M-D10: un objeto ``DurableProtectionJournal`` con vista vieja no manda.

    Se construye el caso real: el proceso A tiene un journal en memoria que
    quedó en ``APPLYING``; OTRO escritor (el proceso B) lleva el archivo hasta
    ``COMMITTED``. Se le pasa su propio objeto y S4-D tiene que reconocer el
    commit leyendo el DISCO — no confiando en lo que A cree.

    Es el patrón exacto de un crash mal sincronizado o de un handle de journal
    cacheado: el objeto dice una cosa y los bytes otra.
    """
    h = _Harness(tmp_path)
    # A: el objeto en memoria que quedará congelado en APPLYING.
    objeto_de_a = h.journal
    assert objeto_de_a.transaction_state is ProtectionTransactionState.APPLYING

    # B: otro escritor lleva el MISMO archivo hasta COMMITTED.
    escritor_b = open_protection_journal(
        h.plan.operation_id,
        h.plan,
        programdata_resolver=_resolver(h.raiz),
        kernel=_KernelDiario(),
    )
    for fase in (
        ProtectionTransactionState.VERIFYING_GP1,
        ProtectionTransactionState.VERIFYING_RV2,
        ProtectionTransactionState.VERIFYING_NODE_SET,
        ProtectionTransactionState.ARCHIVING_BACKUP,
    ):
        escritor_b.enter_finalization_phase(fase)
    h._publicar_backup()
    escritor_b.commit_finalized(archive=h._cargar_backup(), plan=h.plan)
    assert escritor_b.transaction_state is ProtectionTransactionState.COMMITTED

    # El objeto de A sigue diciendo APPLYING: su memoria está desactualizada.
    assert objeto_de_a.transaction_state is ProtectionTransactionState.APPLYING

    reporte = _finalize(h, journal=objeto_de_a)

    assert reporte.disposition is FinalizationDisposition.ALREADY_COMMITTED
    assert h.puerto.llamadas == []


def test_un_journal_con_tail_cortado_es_indeterminate_nunca_un_commit(tmp_path: pathlib.Path) -> None:
    """§20 C4b: evidencia ambigua -> INDETERMINATE, no "todavía no había pasado".

    Se trunca el journal a mitad de una línea. Es el estado que deja un crash
    durante un append. Leerme "todavía no había terminado" sería inventar.
    """
    h = _Harness(tmp_path)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_GP1)
    camino = _journal_path(h.raiz)
    bytes_completos = camino.read_bytes()
    camino.write_bytes(bytes_completos[: len(bytes_completos) - 30])

    reporte = _finalize(h)

    assert reporte.disposition is FinalizationDisposition.INDETERMINATE
    assert not reporte.committed
    assert h.puerto.llamadas == []
    assert h.lock_kernel.close_calls == [], "no se acquires ningún lock sobre evidencia ambigua"


def test_sin_journal_durable_s4d_no_hace_nada(tmp_path: pathlib.Path) -> None:
    """Sin journal no hay transacción: S4-D no opera sobre un Golden huérfano."""
    h = _Harness(tmp_path)
    _journal_path(h.raiz).unlink()

    reporte = _finalize(h)

    assert reporte.disposition is FinalizationDisposition.INDETERMINATE
    assert h.puerto.llamadas == []
    assert not h.backup_path().exists()
    assert h.lock_kernel.close_calls == []


# ============================================================================
# Backup: propiedades del store que sostienen el COMMITTED
# ============================================================================


def test_el_manifiesto_de_backup_incluye_toda_la_evidencia_que_gp3_necesita(
    tmp_path: pathlib.Path,
) -> None:
    """§26: el backup no puede impedir una restauración futura.

    Se enumera la evidencia que un GP3 necesitaría para restaurar y se exige que
    esté TODA en el manifiesto. La lista es explícita a propósito: «los campos
    que se leoccurren» no es una garantía, es una intención.
    """
    h = _Harness(tmp_path)
    h._publicar_backup()
    from sky_claw.local.runtime_vault.golden_backup_archive import load_durable_golden_backup

    backup = load_durable_golden_backup(h.plan.plan, programdata_resolver=_resolver(h.raiz))
    cuerpo = backup.archive

    # Identidad de la raíz y del volumen: sin esto no se sabe QUÉ restaurar.
    assert cuerpo.canonical_root == h.plan.plan.canonical_root
    assert cuerpo.volume_serial_number == h.plan.plan.volume_serial_number
    assert cuerpo.root_file_id == h.plan.plan.root_file_id
    # Versión de política: restaurar con otra versión escribiría otra DACL.
    assert cuerpo.policy_version == h.plan.plan.policy_version
    # Baseline de contenido.
    assert cuerpo.tree_digest == h.plan.plan.tree_digest
    # Y por cada nodo: PRE SD en bytes, su hash, owner, group, flags de control
    # del DACL, el flag de protección PRE e identidad física.
    assert len(cuerpo.nodes) == h.plan.plan.node_count
    for nodo in cuerpo.nodes:
        assert nodo.pre_sd_bytes_b64
        assert nodo.pre_sd_sha256
        assert nodo.owner_sid
        assert nodo.group_sid
        assert nodo.dacl_control_flags is not None
        assert nodo.pre_dacl_protected_flag is not None
        assert nodo.volume_serial_number > 0
        assert nodo.file_id > 0


def test_el_archivo_de_backup_rechaza_campos_de_mas(tmp_path: pathlib.Path) -> None:
    """El esquema del manifiesto es CERRADO: un campo extra es corrupción.

    Fail-closed bidireccional, como el resto del paquete. Un esquema que acepta
    campos desconocidos deja que un atacante que puede escribir el archivo
    (Administrator legítimo) agregue un ``"autorizado": true`` que un GP3 futuro
    lea sin saber que no es parte del contrato.
    """
    from sky_claw.local.runtime_vault.golden_backup_archive import deserialize_golden_backup_archive

    h = _Harness(tmp_path)
    h._publicar_backup()
    import json

    cuerpo = json.loads(h.backup_path().read_text(encoding="utf-8"))
    cuerpo["autorizado"] = True

    with pytest.raises(Exception, match="claves raíz inesperadas"):
        deserialize_golden_backup_archive(json.dumps(cuerpo, sort_keys=True, separators=(",", ":")).encode())


def test_el_esquema_del_backup_es_versionado(tmp_path: pathlib.Path) -> None:
    """Un ``schema_version`` desconocido NO se interpreta como autoridad actual.

    GP3 tiene que poder rechazar un esquema futuro en vez de asumirlo. Es la
    diferencia entre "no lo conozco" y "probablemente esté bien".
    """
    from sky_claw.local.runtime_vault.golden_backup_archive import deserialize_golden_backup_archive

    h = _Harness(tmp_path)
    h._publicar_backup()
    import json

    cuerpo = json.loads(h.backup_path().read_text(encoding="utf-8"))
    cuerpo["schema_version"] = "gp2-golden-backup-v99"

    with pytest.raises(Exception, match="no se interpreta como autoridad actual|schema_version"):
        deserialize_golden_backup_archive(json.dumps(cuerpo, sort_keys=True, separators=(",", ":")).encode())


def test_el_backup_es_determinista_para_el_mismo_plan(tmp_path: pathlib.Path) -> None:
    """Dos construcciones del mismo plan producen bytes idénticos.

    Sin determinismo, la revalidación de replay («aceptar el objeto existente
    sólo con equivalencia exacta») sería imposible: cada proceso produciría un
    digest distinto y el replay legitimate se clasificaría como corrupto.
    """
    from sky_claw.local.runtime_vault.golden_backup_archive import build_golden_backup_archive

    h = _Harness(tmp_path)
    primero = build_golden_backup_archive(h.plan.plan).canonical_bytes()
    segundo = build_golden_backup_archive(h.plan.plan).canonical_bytes()
    assert primero == segundo


def test_el_backup_no_se_puede_publicar_si_el_padre_no_existe(tmp_path: pathlib.Path) -> None:
    """El store NO crea el directorio de destino: el namespace debe aprovisionarlo.

    Crear el directorio por dentro sería decidir la DACL desde S4-D, que es
    jurisdicción del namespace de confianza. Fallar cerrado obliga a que el
    bootstrap haya hecho su parte.
    """
    from sky_claw.local.runtime_vault.golden_backup_archive import GoldenBackupWriteError, archive_golden_backup

    h = _Harness(tmp_path)
    # Se borra el árbol de directorios que el fixture creó para el backup.
    import shutil

    shutil.rmtree(h.backup_path().parent)

    with pytest.raises(GoldenBackupWriteError, match="No se pudo publicar el backup"):
        archive_golden_backup(h.plan, programdata_resolver=_resolver(h.raiz), writer=h._writer())
    assert not h.backup_path().exists()


def test_m_d11_un_journal_de_otra_operacion_no_puede_recibir_las_transiciones(
    tmp_path: pathlib.Path,
) -> None:
    """M-D11 (fila T de la matriz): el journal tiene que ser de ESTA operación.

    S4-D lee el estado durable de la ruta derivada del ``operation_id`` pero
    ESCRIBE con los métodos del objeto ``journal``, que escribe en la ruta del
    ``operation_id`` de ESE objeto. Con un journal ajeno se leería el estado de
    un archivo y se escribiría la transición en otro — dos operaciones
    inclinándose hacia estados distintos a partir del mismo input.

    El caso lo arma un caller equivocado (un mix accidental), no un atacante:
    los dos archivos están protegidos por el namespace. Y por eso tiene que
    fallar cerrado en vez de "funcionar".
    """
    h = _Harness(tmp_path)
    # Segunda operación COMPLETA y ajena, con su propio journal durable.
    h2 = _Harness(tmp_path / "otra", operation_id=_OTRA_OPERACION)
    assert h2.journal.operation_id != h.journal.operation_id

    with pytest.raises(FinalizationAuthorityError, match="el journal corresponde a"):
        _finalize(h, journal=h2.journal)

    # Cero gates y cero escrituras en AMBOS journals.
    assert h.puerto.llamadas == []
    assert h2.puerto.llamadas == []
    assert h.journal_estado() is ProtectionTransactionState.APPLYING
    assert h2.journal_estado() is ProtectionTransactionState.APPLYING
