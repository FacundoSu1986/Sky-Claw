"""GP2-S4D: adapter de producción para las verificaciones de post-verificación.

Este módulo conecta el :class:`FinalizationVerificationPort` con las primitivas
**ya auditadas** del paquete. No reimplementa ninguna: compone las mismas que
usan S4-B, S4-C y Admission, y devuelve un :class:`GateVerdict`.

Reutilización, no duplicación (regla del repo)
----------------------------------------------
===========  =========================================================
Gate         Primitiva compuesta
===========  =========================================================
GP1          ``protection.inspect_golden_protection`` (§9.2.1)
RV-2         ``inventory.inventory_tree`` + ``verification.tree_digest_from_files``
             (§9.3) — el TreeDigest es contenido, NO metadata de seguridad
NodeSet      ``node_evidence.probe_node_evidence`` (§10, §23.1)
quiescence   ``quiescence.probe_tree_quiescence`` (§4.1.1)
===========  =========================================================

Las cuatro son read-only. Ninguna muta, ninguna escribe y ninguna acepta un
``path`` del caller: todas reciben la raíz canónica derivada del plan.

Por qué el RV-2 final NO llama a ``verify_golden_master``
-------------------------------------------------------
``verify_golden_master`` exige ``expected_runtime`` y devuelve ``VERIFIED`` sólo
si el eje de runtime también lo es. El plan autoritativo de S4-A
(:class:`AuthorizedPlan`) NO tiene campo de identidad de runtime — tiene
``tree_digest``, identidad física, ``policy_version`` y la tabla de nodos. O
sea: el único eje que GP2 tiene autorización para comparar es el de CONTENIDO.

Esta es una discrepancia ADR↔código registrada explícitamente (§2 del plan de
trabajo), y la resolución es fail-closed, no improvisada:

* **no** se fabrica un ``expected_runtime`` copiándolo del ``observed_runtime``
  (eso es exactamente R02, el tautología que el ADR prohíbe);
* **no** se degrada un ``UNKNOWN`` a ``VERIFIED``;
* el gate compara el TreeDigest observado contra ``AuthorizedPlan.tree_digest`` y
  exige igualdad EXACTA de digest, número de archivos y bytes.

La identidad de runtime se registra como observación de evidencia, no como gate.
Un operador tiene que saber que el eje NO se verificó, y el veredicto lo dice.

ACL y TreeDigest: la pregunta explícita del enunciado
-----------------------------------------------------
"El apply del ACL ¿cambió el TreeDigest?" — Verificado contra la implementación
real, no supuesto:

* ``inventory_tree`` produce ``FileEntry(rel_path, is_dir, size, digest)`` donde
  ``digest`` es SHA-256 de los BYTES del archivo. No hay ningún campo de
  descriptor de seguridad, propietario, grupo ni flags de DACL.
* ``tree_digest_from_files`` hashea ``rel_path + size + digest`` y sólo de los
  archivos (``is_file()``), excluidos los directorios.

Por lo tanto ``SetSecurityInfo`` — que sólo cambia el Security Descriptor — deja
el TreeDigest intacto, y comparar el digest POST-apply contra el PRE-apply del
plan es una comparación legítima y no una tautología. El test
``test_el_apply_del_acl_no_altera_el_tree_digest`` lo demuestra sobre un árbol
real, no lo afirma en un comentario.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
from typing import Any, Final

from sky_claw.local.runtime_vault.finalization_orchestrator import (
    FinalizationUnsupportedError,
    GateVerdict,
)
from sky_claw.local.runtime_vault.golden_protection_plan import (
    DuplicateFileIdError,
)
from sky_claw.local.runtime_vault.models import (
    InventoryError,
    RuntimeVaultError,
    TreeDigest,
    VerificationState,
)
from sky_claw.local.runtime_vault.node_evidence import (
    NativeEvidenceError,
    probe_node_evidence,
)
from sky_claw.local.runtime_vault.protection import GoldenProtectionState, inspect_golden_protection
from sky_claw.local.runtime_vault.quiescence import (
    DEFAULT_BASE_BACKOFF_SECONDS,
    MAX_PROBE_RETRIES,
    QuiescenceError,
    QuiescenceViolationError,
    probe_tree_quiescence,
)
from sky_claw.local.runtime_vault.verification import verify_tree

logger = logging.getLogger(__name__)


def _digest(detalle: str) -> str:
    return hashlib.sha256(detalle.encode("utf-8")).hexdigest()


def _verdict(gate: str, passed: bool, detalle: str) -> GateVerdict:
    return GateVerdict(gate=gate, passed=passed, detail=detalle, evidence_digest=_digest(f"{gate}:{detalle}"))


#: Errores de DOMINIO que ``probe_node_evidence`` puede levantar y que NO
#: derivan de ``NativeEvidenceError``.
#:
#: Se enumeran a propósito, y con la comprobación de que cada uno realmente
#: escapa del ``except NativeEvidenceError`` que el adapter tenía antes. Un
#: ``except (NativeEvidenceError,)`` dejaba pasar:
#:
#: * ``InventoryLinkError`` (reparse point / junction dentro del árbol) —
#:   deriva de ``InventoryError``, no de ``NativeEvidenceError``. Un reparse
#:   inesperado es exactamente el caso I de la matriz adversarial, y se
#:   escapaba al caller en vez de volverse veredicto FAIL.
#: * ``DuplicateFileIdError`` (hardlink o identidad física duplicada) — deriva
#:   de ``GoldenProtectionPlanError``. Es el caso M, y también se escapaba.
#:
#: Ninguno de los dos puede "colarse como veredicto favorable": el adapter
#: devuelve ``passed=False`` con el motivo, y el orquestador traduce eso a
#: ROLLBACK_REQUIRED. Un gate que no puede observar es un gate que no pasa.
_ERRORES_DE_EVIDENCIA: Final[tuple[type[Exception], ...]] = (
    NativeEvidenceError,
    InventoryError,
    DuplicateFileIdError,
)


class Win32FinalizationVerificationPort:
    """Verificaciones de post-verificación sobre el Golden FÍSICO, en Windows.

    Todas son read-only y todas aceptan la raíz canónica derivada del plan: el
    caller no puede señalar otro árbol. Los fallos de observación NO se
    reescriben como veredicto favorable —devuelven ``passed=False`` con el
    motivo, que es la diferencia entre "no encontré nada" y "no pude mirar".
    """

    def observar_gp1(self, *, raiz: str, esperado: str) -> GateVerdict:
        """GP1 fresco: el Golden está ``HARDENED`` (§12.2 paso 8, §9.2.1).

        El estado esperado se compara contra el enum real
        (``GoldenProtectionState.HARDENED``), no contra una cadena: comparar
        strings sería admitir que un valor desconocido pase por ``"hardened"``.
        """
        try:
            resultado = inspect_golden_protection(pathlib.Path(raiz))
        except RuntimeVaultError as exc:
            return _verdict("gp1", False, f"GP1 no pudo observar el Golden: {type(exc).__name__}")
        if resultado.state is not GoldenProtectionState.HARDENED:
            return _verdict("gp1", False, f"GP1={resultado.state.value}; se esperaba HARDENED")
        return _verdict("gp1", True, "GP1=HARDENED")

    def observar_rv2(self, *, raiz: str, tree_digest_esperado: Any) -> GateVerdict:
        """RV-2 fresco: el contenido del Golden sigue siendo el autorizado.

        Delega en ``verification.verify_tree``, que ya es la primitiva auditada
        de comparación de contenido: inventaría el árbol desde el filesystem
        ACTUAL, recalcula el TreeDigest y compara digest + cantidad de archivos
        + bytes. Comparar sólo el hex dejaría pasar un árbol con el mismo hash
        nominal y distinto volumen de contenido.

        Se exige ``VERIFIED`` explícito: un ``UNKNOWN`` (que es lo que devuelve
        cuando el inventario falla cerrado) NO se reinterpreta como "no había
        nada raro". Es la diferencia entre "no encontré nada" y "no pude mirar".
        """
        if not isinstance(tree_digest_esperado, TreeDigest):
            return _verdict("rv2", False, "el plan autoritativo no aporta un TreeDigest: no hay baseline de contenido")
        resultado = verify_tree(pathlib.Path(raiz), tree_digest_esperado)
        if resultado.state is not VerificationState.VERIFIED:
            return _verdict("rv2", False, f"RV-2 {resultado.state.value}: {resultado.message}")
        observado = resultado.observed
        assert observado is not None  # VERIFIED implica observado
        return _verdict("rv2", True, f"RV-2 VERIFIED (TreeDigest={observado.digest[:12]}…)")

    def observar_node_set(self, *, raiz: str, nodos_autorizados: tuple[Any, ...]) -> GateVerdict:
        """NodeSet fresco: el conjunto FÍSICO de nodos es el autorizado (§23.1).

        La comparación es por IDENTIDAD, no por path: ``relative_path`` +
                ``node_kind`` + ``VolumeSerialNumber`` + ``FileId``. Un path igual no
                implica el mismo objeto: un archivo sustituido conserva su nombre y
                cambia su FileId, y ése es el caso I de la matriz adversarial.

                La igualdad es de CONJUNTO en las dos direcciones. Un subconjunto
                verificado no es el conjunto verificado, y un nodo extra es un nodo que
                nadie autorizó.
        """
        try:
            evidencia = probe_node_evidence(raiz)
        except _ERRORES_DE_EVIDENCIA as exc:
            # Todo error de dominio de la evidencia se vuelve veredicto FAIL, no
            # excepción al caller. La enumeración de `_ERRORES_DE_EVIDENCIA`
            # incluye las clases que NO derivan de `NativeEvidenceError`
            # (reparse, hardlink) — ver su comentario.
            return _verdict("node_set", False, f"el probe de nodos falló fail-closed: {type(exc).__name__}: {exc}")

        observado = {
            (item.backup.relative_path, item.backup.node_kind, item.backup.volume_serial_number, item.backup.file_id)
            for item in evidencia
        }
        esperado = {
            (node.relative_path, node.node_kind, node.volume_serial_number, node.file_id) for node in nodos_autorizados
        }

        faltantes = sorted(esperado - observado)
        sobrantes = sorted(observado - esperado)
        if faltantes or sobrantes:
            detalle = f"NodeSet difiere: {len(faltantes)} faltante(s), {len(sobrantes)} sobrante(s)"
            if faltantes:
                detalle += f"; faltantes={[item[0] for item in faltantes][:8]}"
            if sobrantes:
                detalle += f"; sobrantes={[item[0] for item in sobrantes][:8]}"
            return _verdict("node_set", False, detalle)

        # Un nodo con ReparseTag != 0 o NumberOfLinks != 1 rompe la garantía de
        # que un path nombre un único objeto. `probe_node_evidence` ya falla
        # cerrado en varios de estos casos; se vuelve a comprobar acá porque el
        # gate tiene que poder demostrarlo por sí mismo y no confiar en que la
        # otra capa lo hizo.
        for item in evidencia:
            if item.reparse_tag != 0:
                return _verdict(
                    "node_set", False, f"el nodo '{item.backup.relative_path}' es un reparse point: fail-closed"
                )
            if item.number_of_links != 1:
                return _verdict(
                    "node_set",
                    False,
                    f"el nodo '{item.backup.relative_path}' tiene NumberOfLinks={item.number_of_links} != 1",
                )
            if item.delete_pending:
                return _verdict("node_set", False, f"el nodo '{item.backup.relative_path}' está marcado delete-pending")
        return _verdict("node_set", True, f"NodeSet idéntico ({len(observado)} nodos por identidad física)")

    def observar_quiescence(self, *, raiz: str, nodos_autorizados: tuple[Any, ...]) -> GateVerdict:
        """Quiescence rerun: ningún proceso tiene el Golden tomado (§4.1.1).

        Usa la MISMA primitiva y la MISMA política de reintentos del ADR
        (``MAX_PROBE_RETRIES`` intentos, backoff exponencial con jitter) y
        reporta los nodos bloqueantes.

        LIMITACIÓN DECLARADA y no maquillada: el probe solicita apertura
        write/delete sobre cada nodo y falla si la concede. Eso detecta handles
        abiertos CON acceso de escritura. NO detecta un handle abierto sólo de
        lectura que podría impedir un rename posterior. El ADR lo dice en §4.1.1
        y S4-D no puedeVa más allá con la primitiva disponible: afirmar
        exclusividad absoluta sería mentir.
        """
        try:
            probe_tree_quiescence(
                pathlib.Path(raiz),
                nodos_autorizados,
                max_attempts=MAX_PROBE_RETRIES,
                base_backoff_seconds=DEFAULT_BASE_BACKOFF_SECONDS,
            )
        except QuiescenceViolationError as exc:
            return _verdict(
                "quiescence",
                False,
                f"quiescence: {len(exc.blocked_paths)} nodo(s) bloqueado(s) tras {MAX_PROBE_RETRIES} intentos: "
                f"{list(exc.blocked_paths)[:8]}",
            )
        except QuiescenceError as exc:
            return _verdict("quiescence", False, f"quiescence no concluyente: {type(exc).__name__}: {exc}")
        return _verdict("quiescence", True, f"quiescence: sin nodos bloqueantes tras {MAX_PROBE_RETRIES} intentos")


def build_default_verification_port() -> Win32FinalizationVerificationPort:
    """Resuelve el port de producción. Fuera de Windows falla cerrado y-tipado."""
    if os.name != "nt":
        raise FinalizationUnsupportedError(
            "S4-D requiere Win32: GP1, NodeSet y quiescence dependen de handles nativos y ACLs NTFS"
        )
    return Win32FinalizationVerificationPort()


__all__ = [
    "Win32FinalizationVerificationPort",
    "build_default_verification_port",
]
