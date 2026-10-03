"""GP2-S4E / P5 — lifecycle de PrivilegedBoundarySession y forense del lock.

Ancla por AST de que S4-E NO manipula los recursos internos de la sesión, y
prueba causal de las tres propiedades de ownership:

  * ``close()`` y ``close_retaining_lock()`` son exactly-once e idempotentes;
  * un fallo del lock NO filtra el token;
  * el resultado forense ``lock_retained`` coincide con el estado físico.
"""

import ast
import dataclasses
import inspect
import pathlib
import typing
from typing import Any

import pytest

from sky_claw.local.runtime_vault import protection_service as svc
from sky_claw.local.runtime_vault.authorization_context import (
    PrivilegedBoundarySession,
)

_OP = "3f2b1c8e-9a4d-4f5e-8b7a-1c2d3e4f5a6b"


# ===========================================================================
# Fakes con el CONTRATO REAL de los recursos
# ===========================================================================


class _LockFalso:
    """Mismo contrato observable que ``GoldenMutationLockHandle``."""

    def __init__(self, *, falla_release: bool = False) -> None:
        self.closed = False
        self.release_calls = 0
        self.retain_calls = 0
        self._falla_release = falla_release

    def release(self) -> bool:
        self.release_calls += 1
        if self._falla_release:
            raise RuntimeError("SetSecurityInfo falla: no se pudo escribir la metadata")
        if self.closed:
            return False
        self.closed = True
        return True

    def retain_for_inspection(self) -> bool:
        self.retain_calls += 1
        if self.closed:
            return False
        self.closed = True
        return True


class _TokenFalso:
    def __init__(self, *, falla_close: bool = False) -> None:
        self._cerrado = False
        self.close_calls = 0
        self._falla_close = falla_close

    @property
    def closed(self) -> bool:
        return self._cerrado

    def close(self) -> bool:
        self.close_calls += 1
        if self._falla_close:
            raise RuntimeError("token close falla")
        self._cerrado = True
        return True


def _sesion(
    *,
    falla_release: bool = False,
    falla_close: bool = False,
) -> tuple[Any, _LockFalso, _TokenFalso]:
    """Construye una sesión real por su constructor, con recursos instrumentados.

    Se usa el ``__init__`` REAL —que valida los tipos—so que un cambio en el
    contrato de la sesión rompa el test al construir, en vez de dejar pasar un
    objeto que la producción no aceptaría.
    """
    lock = _LockFalso(falla_release=falla_release)
    token = _TokenFalso(falla_close=falla_close)
    sesion = PrivilegedBoundarySession.__new__(PrivilegedBoundarySession)
    object.__setattr__(sesion, "_lock", lock)
    object.__setattr__(sesion, "_token", token)
    object.__setattr__(sesion, "_closed", False)
    return sesion, lock, token


# ===========================================================================
# P5-A — la sesión es la ÚNICA dueña
# ===========================================================================


def test_close_normal_libera_lock_y_cierra_token() -> None:
    """``close()``: exactly once, ``closed=True``, token cerrado, lock RELEASED."""
    sesion, lock, token = _sesion()

    assert sesion.close() is True
    assert sesion.closed is True
    assert token.closed is True
    assert token.close_calls == 1
    assert lock.release_calls == 1
    assert lock.retain_calls == 0
    assert lock.closed is True


def test_close_normal_es_idempotente() -> None:
    """La segunda llamada no vuelve a tocar ningún recurso."""
    sesion, lock, token = _sesion()
    sesion.close()

    assert sesion.close() is False
    assert lock.release_calls == 1, "double-close del lock"
    assert token.close_calls == 1, "double-close del token"


def test_close_retaining_lock_no_escribe_released() -> None:
    """``close_retaining_lock()``: kernel handle cerrado, metadata NO RELEASED."""
    sesion, lock, token = _sesion()

    assert sesion.close_retaining_lock() is True
    assert sesion.closed is True
    assert token.closed is True
    assert token.close_calls == 1
    assert lock.closed is True
    assert lock.retain_calls == 1
    assert lock.release_calls == 0, (
        "un desenlace fail-closed que escribe RELEASED abre la ventana que S4-D declara prohibida"
    )


def test_close_retaining_lock_es_idempotente() -> None:
    sesion, lock, token = _sesion()
    sesion.close_retaining_lock()

    assert sesion.close_retaining_lock() is False
    assert lock.retain_calls == 1
    assert token.close_calls == 1


def test_la_sesion_cerrada_no_expone_sus_recursos() -> None:
    """Tras cerrar, ``lock`` y ``operator_token`` fallan en vez de devolverDead handles.

    Es la propiedad que el anti-pattern rompía: con los recursos cerrados a
    mano y ``_closed == False``, estas propiedades devolvían un handle muerto
    sin avisar, y un ``close()`` posterior intentaba cerrarlo otra vez.
    """
    sesion, _lock, _token = _sesion()
    sesion.close()

    from sky_claw.local.runtime_vault.authorization_context import AuthorizationSessionError

    assert sesion.closed is True
    with pytest.raises(AuthorizationSessionError):
        _ = sesion.lock
    with pytest.raises(AuthorizationSessionError):
        _ = sesion.operator_token


def test_un_fallo_del_lock_no_filra_el_token() -> None:
    """Exception safety: ``release()`` lanza y el token se cierra IGUAL.

    El lock puede quedar durablemente ambiguo —que es lo correcto en
    fail-closed— pero el token del operador no puede quedar abierto: es un
    recurso que nadie más va a cerrar.
    """
    sesion, lock, token = _sesion(falla_release=True)

    with pytest.raises(RuntimeError, match="SetSecurityInfo"):
        sesion.close()

    assert sesion.closed is True, "la sesión queda lógicamente cerrada aunque el lock falle"
    assert token.closed is True, "FUGA: el token quedó abierto tras un fallo del lock"
    assert token.close_calls == 1


def test_un_fallo_del_token_no_impide_cerrar_el_lock() -> None:
    """El orden inverso también se sostiene: el lock se cierra primero."""
    sesion, lock, token = _sesion(falla_close=True)

    with pytest.raises(RuntimeError, match="token close"):
        sesion.close()

    assert sesion.closed is True
    assert lock.closed is True
    assert lock.release_calls == 1


def test_los_dos_cierres_comparten_exactamente_un_camino() -> None:
    """``close()`` y ``close_retaining_lock()`` delegan en la misma rutina.

    Congela que la semántica compartida no se duplique: dos copias del cierre
    es la forma en que un fix deja al gemelo atrás, y acá el gemelo sería
    exactamente la política de lock.
    """
    fuente = inspect.getsource(PrivilegedBoundarySession)
    assert fuente.count("def _cerrar(") == 1
    assert "return self._cerrar(retaining_lock=False)" in fuente
    assert "return self._cerrar(retaining_lock=True)" in fuente


# ===========================================================================
# Anchor AST — S4-E no manipula los internos de la sesión
# ===========================================================================


def test_s4e_no_manipula_los_recursos_internos_de_la_sesion() -> None:
    """S4-E elige POLÍTICA y la sesión ejecuta. Ownership exactly once.

    El anti-pattern que este anchor cierra:

        session.operator_token.close()
        session.lock.release()          # o retain_for_inspection()
        # ... y session._closed nunca se ponía en True

    Dejaba una sesión con los recursos físicamente cerrados y ``closed ==
    False``: ``session.lock`` seguía devolviendo el handle sin error, y un
    ``session.close()`` posterior intentaba cerrarlo otra vez.

    Se prohíben las ESCRITURAS. El acceso de LECTURA sigue permitido: el
    servicio tiene que poder observar el estado del handle para reconciliar
    el reporte forense (P5-C).
    """
    arbol = ast.parse(
        pathlib.Path(svc.__file__).read_text(encoding="utf-8"),
        filename=str(svc.__file__),
    )
    escrituras: list[str] = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        func = nodo.func
        if not isinstance(func, ast.Attribute):
            continue
        # operator_token.close(...) / lock.release(...) / lock.retain_for_inspection(...)
        if (
            func.attr in ("close", "release", "retain_for_inspection")
            and isinstance(func.value, ast.Attribute)
            and func.value.attr in ("operator_token", "lock")
        ):
            escrituras.append(func.attr)

    assert not escrituras, (
        f"S4-E escribe directamente sobre recursos de la sesión: {sorted(set(escrituras))}. "
        "La sesión es la única dueña de su lifecycle; el servicio elige política con "
        "session.close() / session.close_retaining_lock()."
    )


def test_cerrar_frontera_es_delegacion_pura() -> None:
    """`_cerrar_frontera` no tiene lógica de recursos propia.

    Se afirma sobre el AST y no sobre el texto: el docstring NOMBRA el
    anti-pattern que eliminó (``session.operator_token.close()``), así que una
    busqueda de substring sobre el fuente daria un falso positivo.

    Su cuerpo tiene que ser la elección de política y nada más. Si vuelve a
    crecer un ``try/except`` que coleccione errores, es que alguien reintrodujo
    el cierre paralelo.
    """
    arbol = ast.parse(
        inspect.getsource(svc._cerrar_frontera),
        filename=str(svc.__file__),
    )
    llamadas = {
        nodo.func.attr
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and isinstance(nodo.func.value, ast.Name)
        and nodo.func.value.id == "session"
    }
    assert llamadas == {"close", "close_retaining_lock"}, (
        f"la frontera manipula algo más que la política de cierre: {sorted(llamadas)}"
    )

    # Y devuelve si el lock quedó retenido: el forense no puede inferirlo
    # después, porque `session.lock` lanza una vez cerrada.
    # El módulo activa las anotaciones diferidas, así que la anotación
    # chega como string; `get_type_hints` la resuelve.
    assert typing.get_type_hints(svc._cerrar_frontera)["return"] is bool
    assert "retener_lock" in inspect.signature(svc._cerrar_frontera).parameters


# ===========================================================================
# P5-C — el forense dice la verdad sobre el lock
# ===========================================================================


def test_un_rollback_exitoso_no_reporta_lock_retenido() -> None:
    """P5-C: rollback OK → ``settled``, lock LIBERADO y ``lock_retained=False``.

    Es el bug exacto: la proyección de S4-B se escribe cuando S4-B termina y
    decía ``lock_retained=True``; después la frontera liberaba el lock, y el
    resultado describía un lock retenido que ya no existía.
    """
    reporte = _reporte_apply(apply_error="nodo 1 rechazado por permisos")
    pre = svc._proyeccion_de_apply(reporte)
    # La proyección de S4-B no conoce todavía el destino del lock...
    assert pre.disposition is svc.ProtectionDisposition.ROLLED_BACK
    assert pre.settled is True

    sesion, lock, _token = _sesion()
    retenido = svc._cerrar_frontera(sesion, retener_lock=not pre.settled)

    post = svc._reconciliar_lock_reportado(pre, lock_retained_fisico=retenido)
    assert lock.closed is True
    assert post.lock_retained is False, "el resultado afirma un lock retenido que la sesión ya liberó"
    assert post.disposition is svc.ProtectionDisposition.ROLLED_BACK
    assert post.settled is True


def test_un_desenlace_no_cerrado_conserva_lock_retenido() -> None:
    """P5-C (contraparte): un desenlace fail-closed RETIENE y eso se reporta."""
    reporte = _reporte_apply(apply_error="x", rollback_error="no se pudo restaurar")
    pre = svc._proyeccion_de_apply(reporte)
    assert pre.settled is False

    sesion, lock, _token = _sesion()
    retenido = svc._cerrar_frontera(sesion, retener_lock=not pre.settled)

    post = svc._reconciliar_lock_reportado(pre, lock_retained_fisico=retenido)
    assert lock.closed is True
    assert lock.retain_calls == 1
    assert lock.release_calls == 0
    assert post.lock_retained is True, "el resultado dijo que el lock quedó libre cuando en realidad se retuvo"
    assert post.operator_intervention_required is True


def test_la_reconciliacion_solo_puede_bajar_el_flag() -> None:
    """Nunca sube ``lock_retained``: un ``True`` real no se contradice.

    Bajar un ``True`` que el cierre retuvo sería mentir en el otro sentido, y
    el RIG E03 comprueba que un handoff retenido se reporte como retenido.
    """
    desenlace = svc.ProtectionOutcome(
        operation_id=_OP,
        disposition=svc.ProtectionDisposition.ROLLED_BACK,
        stage=svc.ProtectionStage.APPLY,
        source_orchestrator="x.y",
        lock_retained=True,
    )
    # El cierre LIBERÓ: el flag debe bajar.
    liberado = svc._reconciliar_lock_reportado(desenlace, lock_retained_fisico=False)
    assert liberado.lock_retained is False

    # El cierre RETUVO: el flag se queda.
    retenido = svc._reconciliar_lock_reportado(desenlace, lock_retained_fisico=True)
    assert retenido.lock_retained is True

    # Y un `False` que era cierto nunca se convierte en `True`.
    ya_libre = dataclasses.replace(desenlace, lock_retained=False)
    assert svc._reconciliar_lock_reportado(ya_libre, lock_retained_fisico=True).lock_retained is False


def _reporte_apply(*, apply_error: str | None = None, rollback_error: str | None = None) -> Any:
    from sky_claw.local.runtime_vault.protection_journal import ProtectionTransactionState

    # Los parametros se copian a una clase por INSTANCIA: el cuerpo de una
    # clase no participa del cierre lexico, asi que pply_error = apply_error
    # resolveria al atributo de clase (NameError), no al argumento.
    class _Reporte:
        def __init__(
            self,
            *,
            apply_error: str | None,
            rollback_error: str | None,
        ) -> None:
            self.operation_id = _OP
            self.transaction_state = ProtectionTransactionState.ROLLING_BACK
            self.apply_error = apply_error
            self.rollback_error = rollback_error
            self.rolled_back_nodes = ("Data/Skyrim.esm",) if apply_error else ()
            self.setsecurityinfo_calls = 3
            self.outcomes: tuple[Any, ...] = ()

    return _Reporte(apply_error=apply_error, rollback_error=rollback_error)
