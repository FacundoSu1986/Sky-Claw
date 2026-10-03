"""Ancla estructural GP2-S4A: la frontera de seguridad del slice.

Este archivo existe para que una violación accidental de la HARD SAFETY BOUNDARY
sea difícil de comitear. No prueba comportamiento: prueba AUSENCIA de capacidad.

- ``SB-01``: los cuatro módulos de S4-A no importan ni llaman
  ``SetSecurityInfo`` / ``SetNamedSecurityInfoW`` / ``SetFileSecurityW``.
- ``SB-02``: ``protection_journal`` (modelo puro) no importa el journal de
  aplicación ``sky_claw.app.db.journal`` (GP2-T16).
- ``SB-03``: ningún módulo de S4-A importa ``apply_target_dacl_by_handle`` ni
  ningún mutador de rollback de descriptores.
- ``SB-04``: S4-A no afirma ``COMMITTED`` ni ``ARCHIVING_BACKUP`` productivos.
- ``SB-05``: la CLI del helper sigue aceptando sólo ``--operation-id`` y
  ``--staging-digest`` (sin ``--golden-root``/``--authorized-plan-path``/etc.).
- ``SB-06``: el candidato nunca se copia al destino autoritativo (sin
  ``copyfile``/``shutil.copy``/``os.replace`` hacia ``authorized_plan.json``).

Si algún assert de acá falla, el commit está prohibido hasta explicar por qué.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from sky_claw.local.runtime_vault.privileged_boundary import HELPER_CLI_ALLOWED_FLAGS

_PAQUETE = pathlib.Path(__file__).resolve().parents[1] / "sky_claw" / "local" / "runtime_vault"

_MODULOS_S4A = (
    "authorized_plan.py",
    "authorized_plan_store.py",
    "protection_journal.py",
    "protection_journal_store.py",
)

#: Símbolos que NINGÚN módulo de S4-A puede referenciar.
_SIMBOLOS_PROHIBIDOS = frozenset(
    {
        "SetSecurityInfo",
        "SetNamedSecurityInfoW",
        "SetFileSecurityW",
        "SetFileSecurity",
        "apply_target_dacl_by_handle",
        "apply_rollback_security_descriptor",
        "restore_pre_sd",
        "set_kernel_object_sacl",
        "AdjustTokenPrivileges",
        "SeDebugPrivilege",
    }
)

#: Módulos que S4-A no puede importar (frontera de aplicación / GUI).
_MODULOS_PROHIBIDOS = frozenset(
    {
        "sky_claw.app.db.journal",
        "sky_claw.app.gui",
        "sky_claw.app.web",
    }
)

#: Módulos cuyas primitivas de copia convertirían staging en autoridad.
_MODULOS_DE_COPIA = frozenset({"os", "shutil", "pathlib"})

#: Atributos de copia prohibidos sobre esos módulos.
_COPIAS_PROHIBIDAS = frozenset({"copyfile", "copy", "copy2", "copytree", "replace"})

#: Símbolo representativo de cada módulo del slice (prueba de cableado en __init__).
_SIMBOLO_POR_MODULO = {
    "authorized_plan": "AuthorizedPlan",
    "authorized_plan_store": "DurableAuthorizedPlan",
    "protection_journal": "ProtectionJournal",
    "protection_journal_store": "DurableProtectionJournal",
}

# ============================================================================
# Frontera de S4-B: SetSecurityInfo pasa a permitirse, PERO SÓLO en target_dacl
# ============================================================================

#: Único módulo del paquete al que S4-B concede el permiso nuevo de mutación.
_MODULO_MUTADOR_AUTORIZADO_S4B = "target_dacl.py"

#: Único otro portador PREEXISTENTE de la primitiva: el bootstrap del namespace
#: propio de Sky-Claw (``trusted_goldens.json`` y sus ancestros). No es la capa de
#: mutación ACL del Golden: es de S1, queda fuera del alcance de S4-B y S4-B no la
#: toca. ``SB-09`` es lo que impide que se sume un TERCER portador.
_MODULOS_MUTADORES_PREEXISTENTES = frozenset({"trusted_namespace.py"})

#: Primitivas Win32 cuyo uso productivo queda confinado a los módulos autorizados.
_PRIMITIVAS_MUTADORAS = frozenset(
    {
        "SetSecurityInfo",
        "SetNamedSecurityInfoW",
        "SetFileSecurityW",
        "SetFileSecurity",
    }
)

#: Capas donde ``SetSecurityInfo`` sigue PROHIBIDO en S4-B (§41 del ADR).
_CAPAS_SIN_MUTACION = (
    # planner / staging
    "golden_protection_plan.py",
    "planning_orchestrator.py",
    "clone.py",
    "ppsc.py",
    # TGR: el registro, su lock y la admisión (NO el bootstrap del namespace)
    "trusted_registry.py",
    "trusted_registry_lock.py",
    "golden_admission.py",
    "golden_admission_service.py",
    "golden_admission_store.py",
    # store del plan autoritativo
    "authorized_plan.py",
    "authorized_plan_store.py",
    # modelo y store del journal
    "protection_journal.py",
    "protection_journal_store.py",
    # frontera / GUI / LLM
    "privileged_boundary.py",
    "authorization_context.py",
    "operator_verifier_bridge.py",
    "coordinator_identity.py",
    # orquestador de apply: COMPONE, no construye
    "mutation_executor.py",
    # orquestador de recovery: COMPONE, no construye ni muta por path
    "recovery_orchestrator.py",
    # binding PRE-plan: escribe SÓLO su propio JSON protegido; nunca ACLs
    "operation_lock_binding.py",
)

#: Símbolos que delatarían un segundo builder/WAL/lock dentro del orquestador.
_SIMBOLOS_DUPLICADOS_S4B = frozenset(
    {
        "build_native_target_dacl",
        "build_target_dacl_spec",
        "TargetDaclSpec",
        "CreateHardLinkW",
        "AdjustTokenPrivileges",
        "SeDebugPrivilege",
        "SeSecurityPrivilege",
        "_MINT_PROOF",
        "FlushFileBuffers",
    }
)


def _arbol(modulo: str) -> ast.AST:
    ruta = _PAQUETE / modulo
    assert ruta.is_file(), f"no existe el módulo esperado del slice: {ruta}"
    return ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))


def _modulos_del_paquete() -> tuple[str, ...]:
    return tuple(p.name for p in sorted(_PAQUETE.glob("*.py")) if p.name != "__init__.py")


def _simbolos_referenciados(arbol: ast.AST) -> set[str]:
    return {nodo.id for nodo in ast.walk(arbol) if isinstance(nodo, ast.Name)} | {
        nodo.attr for nodo in ast.walk(arbol) if isinstance(nodo, ast.Attribute)
    }


def _modulos_importados(arbol: ast.AST) -> set[str]:
    modulos: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            modulos.update(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.level == 0 and nodo.module:
            modulos.add(nodo.module)
    return modulos


class TestFronteraDeSeguridad:
    @pytest.mark.parametrize("modulo", _MODULOS_S4A)
    def test_sb01_sin_primitivas_mutadoras_de_seguridad(self, modulo: str) -> None:
        arbol = _arbol(modulo)
        prohibidos = _simbolos_referenciados(arbol) & _SIMBOLOS_PROHIBIDOS
        assert prohibidos == set(), f"{modulo} referencia primitivas mutadoras prohibidas: {sorted(prohibidos)}"

    @pytest.mark.parametrize("modulo", _MODULOS_S4A)
    def test_sb02_sin_imports_de_la_frontera_de_aplicacion(self, modulo: str) -> None:
        arbol = _arbol(modulo)
        importados = _modulos_importados(arbol)
        prohibidos = {m for m in importados if any(m == p or m.startswith(p + ".") for p in _MODULOS_PROHIBIDOS)}
        assert prohibidos == set(), f"{modulo} importa módulos prohibidos: {sorted(prohibidos)}"

    def test_sb03_journal_puro_no_importa_el_journal_de_aplicacion(self) -> None:
        arbol = _arbol("protection_journal.py")
        assert "sky_claw.app.db.journal" not in _modulos_importados(arbol)

    def test_sb04_sin_committed_ni_archiving_productivos(self) -> None:
        """S4-A puede CONOCER los estados (son normativos) pero no escribirlos."""
        arbol = _arbol("protection_journal_store.py")
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.FunctionDef) and nodo.name in {"commit", "mark_committed", "archive_backup"}:
                pytest.fail(f"S4-A no debe exponer la función productiva '{nodo.name}'")
        assert "PrematureCommitError" in _simbolos_referenciados(arbol)

    def test_sb05_cli_del_helper_con_dos_flags(self) -> None:
        assert set(HELPER_CLI_ALLOWED_FLAGS) == {"--operation-id", "--staging-digest"}

    @pytest.mark.parametrize("modulo", _MODULOS_S4A)
    def test_sb06_staging_nunca_se_copia_al_destino_autoritativo(self, modulo: str) -> None:
        """Ni ``copyfile`` ni ``replace``: la promoción RE-LEE y RE-SERIALIZA."""
        arbol = _arbol(modulo)
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call) or not isinstance(nodo.func, ast.Attribute):
                continue
            base = nodo.func.value
            if isinstance(base, ast.Name) and base.id in _MODULOS_DE_COPIA:
                assert nodo.func.attr not in _COPIAS_PROHIBIDAS, (
                    f"{modulo} usa la copia prohibida '{base.id}.{nodo.func.attr}': staging nunca es autoridad"
                )

    def test_sb07_modulos_del_slice_estan_cableados(self) -> None:
        import sky_claw.local.runtime_vault as pkg

        for raiz, simbolo in _SIMBOLO_POR_MODULO.items():
            assert (_PAQUETE / f"{raiz}.py").is_file(), f"falta el módulo del slice: {raiz}.py"
            assert hasattr(pkg, simbolo), f"{simbolo} no está exportado por el paquete"
            assert simbolo in pkg.__all__, f"{simbolo} no está en __init__.__all__"

    def test_sb08_namespace_solo_agrega_dos_objetos(self) -> None:
        """``trusted_namespace`` gana dos object_name, sin tocar los existentes."""
        from sky_claw.local.runtime_vault import trusted_namespace

        for objeto in ("trusted_goldens.json", "golden_admission_record.json"):
            spec = trusted_namespace.build_namespace_dacl_spec(objeto)
            assert spec is not None
        assert trusted_namespace.AUTHORIZED_PLAN_OBJECT == "authorized_plan.json"
        assert trusted_namespace.PROTECTION_JOURNAL_OBJECT == "protection_journal.json"


class TestFronteraDeMutacionS4B:
    """S4-B habilita ``SetSecurityInfo``: la frontera se vuelve selectiva.

    ``SB-09``/``SB-10`` confinan la primitiva a ``target_dacl.py``. ``SB-11``
    exige que el orquestador COMPONGA en vez de duplicar. ``SB-12`` verifica que
    la API productiva no acepte ``(path, backup)``. ``SB-13`` verifica que S4-B
    nunca escriba ``trusted_goldens.json``.
    """

    def test_sb09_primitivas_mutadoras_solo_en_modulos_autorizados(self) -> None:
        """S4-B sólo concede el permiso NUEVO a ``target_dacl.py`` (§41).

        El conjunto de portadores es CERRADO: ``target_dacl`` (capa auditada de
        mutación ACL del Golden, habilitada por S4-B) más ``trusted_namespace``
        (bootstrap preexistente del namespace de Sky-Claw, fuera de alcance). Si
        aparece un tercer portador, el commit está prohibido.
        """
        portadores: dict[str, set[str]] = {}
        for modulo in _modulos_del_paquete():
            prohibidos = _simbolos_referenciados(_arbol(modulo)) & _PRIMITIVAS_MUTADORAS
            if prohibidos:
                portadores[modulo] = prohibidos
        esperado = {_MODULO_MUTADOR_AUTORIZADO_S4B, *_MODULOS_MUTADORES_PREEXISTENTES}
        assert set(portadores) == esperado, (
            "los portadores de SetSecurityInfo deben ser exactamente "
            f"{sorted(esperado)}; detectados: {sorted(portadores)}"
        )

    def test_sb09_el_permiso_nuevo_es_solo_target_dacl(self) -> None:
        """``target_dacl`` es el módulo que S4-B habilita; el resto ya existía."""
        arbol = _arbol(_MODULO_MUTADOR_AUTORIZADO_S4B)
        assert "SetSecurityInfo" in _simbolos_referenciados(arbol)
        for modulo in _MODULOS_MUTADORES_PREEXISTENTES:
            assert "SetSecurityInfo" in _simbolos_referenciados(_arbol(modulo))

    @pytest.mark.parametrize("modulo", _CAPAS_SIN_MUTACION)
    def test_sb10_capas_no_mutadoras_sin_primitivas_de_seguridad(self, modulo: str) -> None:
        arbol = _arbol(modulo)
        prohibidos = _simbolos_referenciados(arbol) & _PRIMITIVAS_MUTADORAS
        assert prohibidos == set(), f"{modulo} referencia primitivas mutadoras prohibidas: {sorted(prohibidos)}"

    def test_sb11_el_orquestador_compone_no_duplica(self) -> None:
        arbol = _arbol("mutation_executor.py")
        duplicados = _simbolos_referenciados(arbol) & _SIMBOLOS_DUPLICADOS_S4B
        assert duplicados == set(), (
            f"mutation_executor debe componer las primitivas auditadas, no reimplementarlas: {sorted(duplicados)}"
        )

    def test_sb11_el_orquestador_usa_las_primitivas_auditadas(self) -> None:
        arbol = _arbol("mutation_executor.py")
        usados = _simbolos_referenciados(arbol)
        for primitiva in (
            "open_node_security_handle",
            "close_security_handle",
            "apply_target_dacl_by_handle",
            "verify_target_dacl_by_handle",
            "restore_security_descriptor_by_handle",
            "verify_restored_security_descriptor_by_handle",
            "record_node_mutation_intent",
            "record_node_mutation_completed",
            "mark_consumed",
        ):
            assert primitiva in usados, f"mutation_executor no compone '{primitiva}'"

    def test_sb12_la_api_productiva_no_acepta_path_ni_backup(self) -> None:
        """Hard-to-misuse: la entrada productiva NO es ``(path, backup)``."""
        import inspect

        from sky_claw.local.runtime_vault import mutation_executor

        firma = inspect.signature(mutation_executor.apply_authorized_plan)
        parametros = list(firma.parameters.values())
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parametros), (
            "apply_authorized_plan debe ser keyword-only para que nadie pase un path posicional"
        )
        nombres = {p.name for p in parametros}
        assert {"plan", "journal", "session"} <= nombres
        assert "path" not in nombres and "backup" not in nombres and "node" not in nombres

    def test_sb13_s4b_nunca_escribe_trusted_goldens(self) -> None:
        """El contador de escrituras del TGR debe seguir en 0 durante el apply."""
        arbol = _arbol("mutation_executor.py")
        referidos = _simbolos_referenciados(arbol)
        assert "trusted_goldens" not in referidos
        assert "refresh_trusted_registry" not in referidos

    def test_sb13_el_paquete_sigue_exportando_el_orquestador(self) -> None:
        import sky_claw.local.runtime_vault as pkg

        assert hasattr(pkg, "apply_authorized_plan")
        assert "apply_authorized_plan" in pkg.__all__


#: Símbolos que el recovery JAMÁS puede referenciar: reescribir el WAL de nodos,
#: crear journals, leer staging, escribir el TGR, comitear o habilitar
#: privilegios. El recovery RECONCILIA; no amplía el estado autoritativo.
_SIMBOLOS_PROHIBIDOS_S4C = frozenset(
    {
        # WAL de nodos y creación de journals: el recovery sólo transiciona FSM.
        "record_node_mutation_intent",
        "record_node_mutation_completed",
        "create_protection_journal",
        # Staging: NUNCA fuente de recuperación (§41/§46 del encargo).
        "read_candidate_manifest_bytes",
        "derive_candidate_manifest_path",
        "CANDIDATE_MANIFEST_FILE_NAME",
        "candidate_manifest",
        # TGR: TGR_WRITES = 0 (§34).
        "trusted_goldens",
        "refresh_trusted_registry",
        "load_trusted_golden_registry",
        "RegisterOrRefreshTrustedGoldenRequest",
        # COMMITTED pertenece a S4-D (§43).
        "COMMITTED",
        # Privilegios: jamás (SeDebug jamás, §35).
        "SeDebugPrivilege",
        "SeRestorePrivilege",
        "SeBackupPrivilege",
        "AdjustTokenPrivileges",
        # El recovery no construye Target DACLs ni aplica el endurecimiento.
        "build_native_target_dacl",
        "build_target_dacl_spec",
        "TargetDaclSpec",
        "apply_target_dacl_by_handle",
    }
)

#: Primitivas que el recovery DEBE componer (cableado, no muestreo).
_PRIMITIVAS_COMPUESTAS_S4C = (
    "open_protection_journal",
    "classify_protection_journal",
    "classify_durable_authorized_plan",
    "load_durable_authorized_plan",
    "acquire_golden_mutation_lock_for_recovery",
    "rollback_order",
    "transition_to",
)


class TestFronteraDeRecoveryS4C:
    """S4-C: el recovery reconcilia con autoridad durable; no amplía autoridad."""

    def test_sb14_recovery_sin_simbolos_prohibidos(self) -> None:
        arbol = _arbol("recovery_orchestrator.py")
        prohibidos = _simbolos_referenciados(arbol) & _SIMBOLOS_PROHIBIDOS_S4C
        assert prohibidos == set(), f"recovery_orchestrator referencia símbolos prohibidos: {sorted(prohibidos)}"

    def test_sb15_recovery_no_reescribe_el_wal_de_nodos(self) -> None:
        """El schema por nodo (None->MUTATING->MUTATED) está congelado: el
        recovery no inventa RESTORING/RESTORED ni re-registra nodos."""
        arbol = _arbol("recovery_orchestrator.py")
        referidos = _simbolos_referenciados(arbol)
        assert "RESTORING" not in referidos and "RESTORED" not in referidos
        assert "WalMutationPermit" not in referidos

    def test_sb16_recovery_compone_las_primitivas_auditadas(self) -> None:
        arbol = _arbol("recovery_orchestrator.py")
        usados = _simbolos_referenciados(arbol)
        faltantes = [p for p in _PRIMITIVAS_COMPUESTAS_S4C if p not in usados]
        assert faltantes == [], f"recovery_orchestrator no compone: {faltantes}"

    def test_sb17_api_de_recovery_sin_paths_ni_raiz(self) -> None:
        """Hard-to-misuse: la entrada es ``operation_id``; nada de
        ``path``/``canonical_root``/``pre_sd``/``journal_path`` (§45/§46)."""
        import inspect

        from sky_claw.local.runtime_vault import recovery_orchestrator

        firma = inspect.signature(recovery_orchestrator.recover_interrupted_protection)
        parametros = list(firma.parameters.values())
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parametros)
        nombres = {p.name for p in parametros}
        assert "operation_id" in nombres
        for prohibido in ("path", "root", "canonical_root", "pre_sd", "sd_bytes", "journal_path", "backup"):
            assert prohibido not in nombres, f"la API de recovery no acepta '{prohibido}'"

    def test_sb18_el_paquete_exporta_el_recovery(self) -> None:
        import sky_claw.local.runtime_vault as pkg

        for simbolo in (
            "recover_interrupted_protection",
            "RecoveryDisposition",
            "RecoveryForensicReport",
            "acquire_golden_mutation_lock_for_recovery",
            "RecoveryLockAcquisition",
            "GoldenLockOrphanedOperationMismatchError",
        ):
            assert hasattr(pkg, simbolo), f"{simbolo} no está exportado por el paquete"
            assert simbolo in pkg.__all__, f"{simbolo} no está en __init__.__all__"

    def test_sb19_takeover_solo_desde_la_funcion_de_recovery(self) -> None:
        """``allow_orphaned_takeover`` se enumera por call-site, no se muestrea.

        El adquirente NORMAL (``acquire_golden_mutation_lock``) llama al núcleo
        SIN habilitar el takeover; sólo ``acquire_golden_mutation_lock_for_recovery``
        pasa ``True``. Un tercer call-site con takeover rompe el ancla.
        """
        arbol = _arbol("golden_mutation_lock.py")
        llamadas: dict[str, dict[str, str]] = {}
        for funcion in (n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef)):
            for nodo in ast.walk(funcion):
                if (
                    isinstance(nodo, ast.Call)
                    and isinstance(nodo.func, ast.Name)
                    and nodo.func.id == "_acquire_lock_core"
                ):
                    valores = {kw.arg: ast.unparse(kw.value) for kw in nodo.keywords}
                    llamadas[funcion.name] = valores
        assert set(llamadas) == {
            "acquire_golden_mutation_lock",
            "acquire_golden_mutation_lock_for_recovery",
            "_acquire_golden_mutation_lock_at",
        }, f"call-sites de _acquire_lock_core cambiaron: {sorted(llamadas)}"
        assert "allow_orphaned_takeover" not in llamadas["acquire_golden_mutation_lock"]
        assert "allow_orphaned_takeover" not in llamadas["_acquire_golden_mutation_lock_at"]
        assert llamadas["acquire_golden_mutation_lock_for_recovery"].get("allow_orphaned_takeover") == "True"

    def test_sb20_objetos_protegidos_de_operations_congelados(self) -> None:
        """La enumeración de objetos planos protegidos es un conjunto CERRADO.

        Cada miembro tiene verificador por handle y DACL canónica
        (``_verificador_de_archivo_protegido`` falla cerrado sin registro). Un
        objeto nuevo fuera de este conjunto se escribiría sin comprobar su
        contrato; uno que salga de él pierde su protección.
        """
        from sky_claw.local.runtime_vault import trusted_namespace

        assert (
            frozenset(
                {
                    "authorized_plan.json",
                    "protection_journal.json",
                    "operation_lock_binding.json",
                }
            )
            == trusted_namespace._AUTHORIZED_OPERATIONS_FILE_OBJECTS
        )
        for objeto in sorted(trusted_namespace._AUTHORIZED_OPERATIONS_FILE_OBJECTS):
            assert callable(trusted_namespace._verificador_de_archivo_protegido(objeto))
            spec = trusted_namespace.build_namespace_dacl_spec(objeto)
            assert spec is not None

    def test_sb21_el_binding_se_publica_antes_del_lock(self) -> None:
        """AST: en ``establish_privileged_authorization``, el binding precede al lock.

        Es la propiedad que cierra la ventana de §12.2 paso 6→7: si el lock se
        adquiriera antes, un crash entre ambos volvería a dejar un lock huérfano
        sin evidencia durable localizable. Se ancla sobre la función exacta, no
        sobre un fuzzy match de nombres en el módulo.
        """
        arbol = _arbol("authorization_context.py")
        objetivo = next(
            n
            for n in ast.walk(arbol)
            if isinstance(n, ast.FunctionDef) and n.name == "establish_privileged_authorization"
        )
        llamadas: list[tuple[str, int]] = []
        for nodo in ast.walk(objetivo):
            if isinstance(nodo, ast.Call):
                llamadas.append((ast.unparse(nodo.func), nodo.lineno))
        publicacion = [lin for nombre, lin in llamadas if nombre == "_publish_or_verify_operation_lock_binding"]
        lock = [lin for nombre, lin in llamadas if nombre == "lock_fn"]
        assert publicacion, "el establecimiento debe publicar el binding pre-plan"
        assert lock, "el establecimiento debe adquirir el GoldenMutationLock"
        assert min(publicacion) < min(lock), "el binding debe publicarse ANTES de adquirir el lock"

    def test_sb22_recovery_consume_el_binding_pero_no_lo_publica(self) -> None:
        """El recovery LEE el binding para derivar identidad; NUNCA lo crea.

        Publicarlo es autoridad de la frontera privilegiada
        (``establish_privileged_authorization``), no del recovery.
        """
        arbol = _arbol("recovery_orchestrator.py")
        usados = _simbolos_referenciados(arbol)
        assert "classify_operation_lock_binding" in usados
        assert "load_operation_lock_binding" in usados
        assert "promote_operation_lock_binding" not in usados, (
            "el recovery no puede publicar evidencia de identidad: eso es de la frontera privilegiada"
        )

    def test_sb23_el_binding_no_es_autoridad_de_mutacion(self) -> None:
        """El binding liga identidad; no autoriza ACLs, plan, journal ni TGR."""
        arbol = _arbol("operation_lock_binding.py")
        usados = _simbolos_referenciados(arbol)
        for prohibido in (
            "SetSecurityInfo",
            "restore_security_descriptor_by_handle",
            "apply_target_dacl_by_handle",
            "build_target_dacl_spec",
            "promote_durable_authorized_plan",
            "create_protection_journal",
            "open_protection_journal",
            "read_candidate_manifest_bytes",
            "derive_candidate_manifest_path",
            "refresh_trusted_registry",
            "probe_node_evidence",
        ):
            assert prohibido not in usados, f"el binding no puede referenciar '{prohibido}'"
        # Y tampoco puede tomar el lock por su cuenta (el lock tiene su contrato).
        assert "acquire_golden_mutation_lock" not in usados
