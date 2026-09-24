"""Política de settings headless de LOOT en el data root propiedad de Sky-Claw (PR-2).

Una corrida ``--auto-sort`` desatendida que termina en NO_CHANGE queda colgada
en un diálogo modal si LOOT conserva su default. Este módulo gestiona EXACTAMENTE
una clave de ``<loot-data-path>/settings.toml`` para que ese camino pueda
cerrar solo.

Evidencia upstream — ``loot/loot`` tag ``0.29.1``, commit
``77f3ba98966819fd6d92d97dcb2dbc4c1b9fb9b9``:

* ``src/gui/state/loot_settings.h:127``: ``bool useNoSortingChangesDialog_{true};``
  — el default es ``true`` (diálogo activo).
* ``src/gui/state/loot_settings.cpp:845-846`` (``load``):
  ``settings["useNoSortingChangesDialog"].value_or(useNoSortingChangesDialog_)``
  — clave TOML top-level exacta; un valor de otro tipo cae al default ``true``,
  así que el valor gestionado tiene que ser el booleano TOML ``false``.
* ``src/gui/state/loot_settings.cpp:968`` (``save``): la misma clave.
* ``src/gui/qt/main_window.cpp:1600-1613`` (``handlePluginsSorted``, rama sin
  cambios): ``isNoSortingChangesDialogEnabled()`` → ``QMessageBox::information``
  (modal: bloquea el event loop) ; si no → ``showNotification`` (status bar).
* ``src/gui/qt/main_window.cpp:2870-2884`` (``handlePluginsAutoSorted``):
  ``handlePluginsSorted`` corre ANTES del ``on_actionQuit_triggered()``. Con el
  diálogo activo, NO_CHANGE desatendido nunca llega al quit → timeout → FAIL.
  Con ``false`` → notificación → quit → código 0.
* ``src/gui/state/loot_state.cpp:210-222``: un ``settings.toml`` que no parsea
  agrega un init error y ``MainWindow::initialise`` (``main_window.cpp:383-385``)
  retorna sin cargar el juego: sin sort y sin quit. Por eso esta política valida
  el TOML resultante ANTES de escribir y falla cerrado ante un archivo que no
  entiende, en vez de "repararlo".

Política de propiedad (managed vs user):

* **Clave gestionada por Sky-Claw**: sólo ``useNoSortingChangesDialog = false``
  (:data:`MANAGED_LOOT_HEADLESS_SETTINGS`). Autoridad: el data root es
  propiedad EXCLUSIVA de Sky-Claw por instancia+perfil (PR-1,
  :mod:`sky_claw.local.loot.data_root`, que rechaza el default del GUI del
  operador). Si alguien abre LOOT GUI sobre ESTE root y reactiva el diálogo,
  Sky-Claw lo vuelve a ``false`` antes de la próxima corrida headless.
* **Todo lo demás es de LOOT/del operador** y se preserva byte a byte: la
  edición es textual (una línea antepuesta o el valor de una línea
  reemplazado), nunca una reserialización del archivo. No se tocan masterlist,
  prelude, ``updateMasterlist``, userlist ni ninguna otra preferencia.
* Idempotente: si la clave ya vale ``false`` no se escribe nada.
* Escritura atómica: temporal exclusivo + ``os.replace`` en el mismo directorio.
* Fail-closed: TOML inválido, no-UTF-8 o una forma de la clave que no se puede
  editar con garantías → :class:`LootHeadlessSettingsError` y NO se escribe.

Fuera de alcance (hallazgo documentado, no gestionado acá): el diálogo
"First-Time Tips" (``main_window.cpp:366-368,1311-1382``) se muestra cuando
``lastVersion`` (``loot_settings.cpp:854``) difiere de ``getLootVersion()``, y
``lastVersion`` sólo se persiste en ``closeEvent`` (``main_window.cpp:1489-1490``).
Un data root nuevo bloquea TODA corrida desatendida hasta un primer cierre
normal; sembrar ``lastVersion`` exige atestiguar la versión exacta del binario
y es un contrato aparte.
"""

from __future__ import annotations

import contextlib
import enum
import os
import pathlib
import re
import secrets
import tomllib
from collections.abc import Mapping
from typing import Any, Final

#: Nombre exacto del archivo bajo ``--loot-data-path`` (``loot_paths.cpp:62-64``).
LOOT_SETTINGS_FILENAME: Final[str] = "settings.toml"

#: Clave TOML exacta del diálogo modal de NO_CHANGE (``loot_settings.cpp:845``).
LOOT_NO_SORTING_CHANGES_DIALOG_KEY: Final[str] = "useNoSortingChangesDialog"

#: Conjunto COMPLETO de claves que Sky-Claw gestiona en el settings de LOOT.
#: Congelado por igualdad literal en tests: agregar una clave es una decisión de
#: política con evidencia upstream, no un detalle de implementación.
MANAGED_LOOT_HEADLESS_SETTINGS: Final[Mapping[str, bool]] = {LOOT_NO_SORTING_CHANGES_DIALOG_KEY: False}

_MANAGED_LINE: Final[str] = f"{LOOT_NO_SORTING_CHANGES_DIALOG_KEY} = false"

#: Línea top-level con la clave (desnuda o entre comillas) y un booleano TOML.
_MANAGED_KEY_LINE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<prefix>[ \t]*(?:useNoSortingChangesDialog|\"useNoSortingChangesDialog\"|'useNoSortingChangesDialog')"
    r"[ \t]*=[ \t]*)(?P<value>true|false)(?P<suffix>[ \t]*(?:#[^\r\n]*)?\r?)$",
    re.MULTILINE,
)

#: Primera cabecera de tabla (``[x]`` / ``[[x]]``): lo anterior es la tabla raíz.
_TABLE_HEADER: Final[re.Pattern[str]] = re.compile(r"^[ \t]*\[", re.MULTILINE)

_MISSING: Final[object] = object()


class LootHeadlessSettingsAction(enum.StrEnum):
    """Qué hizo :func:`ensure_loot_headless_settings` (observable en tests/logs)."""

    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


class LootHeadlessSettingsError(RuntimeError):
    """El settings de LOOT no se puede gestionar con garantías: LOOT NO se lanza."""


def ensure_loot_headless_settings(loot_data_path: pathlib.Path) -> LootHeadlessSettingsAction:
    """Garantiza ``useNoSortingChangesDialog = false`` preservando todo lo demás.

    Raises:
        LootHeadlessSettingsError: el archivo existe pero no se puede leer, no
            es UTF-8, no es TOML válido, o la clave tiene una forma que no se
            puede editar sin riesgo de alterar otra preferencia.
    """
    settings_path = loot_data_path / LOOT_SETTINGS_FILENAME
    try:
        raw = settings_path.read_bytes()
    except FileNotFoundError:
        contenido = _MANAGED_LINE + "\n"
        _validar_edicion(contenido, antes={})
        _escribir_atomico(settings_path, contenido.encode("utf-8"))
        return LootHeadlessSettingsAction.CREATED
    except OSError as exc:
        raise LootHeadlessSettingsError(f"No se pudo leer {settings_path}: {exc}") from exc

    try:
        texto = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LootHeadlessSettingsError(
            f"{settings_path} no es UTF-8 válido; no se modifica (LOOT tampoco podría usarlo)."
        ) from exc
    # tomllib rechaza el BOM; se aparta para parsear y se conserva al escribir.
    bom = "\ufeff" if texto.startswith("\ufeff") else ""
    cuerpo = texto[len(bom) :]
    antes = _parsear_o_fallar(cuerpo, settings_path)
    actual = antes.get(LOOT_NO_SORTING_CHANGES_DIALOG_KEY, _MISSING)
    if actual is False:
        return LootHeadlessSettingsAction.UNCHANGED

    if actual is _MISSING:
        # Una línea antepuesta a TODO el documento siempre cae en la tabla raíz.
        # Respeta el estilo de salto del archivo (CRLF si su primera línea lo usa).
        primer_salto = cuerpo.find("\n")
        salto = "\r\n" if primer_salto > 0 and cuerpo[primer_salto - 1] == "\r" else "\n"
        nuevo_cuerpo = _MANAGED_LINE + salto + cuerpo
    else:
        nuevo_cuerpo = _reemplazar_valor_top_level(cuerpo, settings_path)
    _validar_edicion(nuevo_cuerpo, antes=antes)
    _escribir_atomico(settings_path, (bom + nuevo_cuerpo).encode("utf-8"))
    return LootHeadlessSettingsAction.UPDATED


def _parsear_o_fallar(cuerpo: str, settings_path: pathlib.Path) -> dict[str, Any]:
    try:
        return tomllib.loads(cuerpo)
    except tomllib.TOMLDecodeError as exc:
        raise LootHeadlessSettingsError(
            f"{settings_path} no es TOML válido ({exc}); no se sobrescribe: LOOT tampoco lo "
            "cargaría (init error → sin sort) y reescribirlo destruiría las preferencias."
        ) from exc


def _reemplazar_valor_top_level(cuerpo: str, settings_path: pathlib.Path) -> str:
    """Reemplaza el valor de la ÚNICA línea top-level de la clave por ``false``."""
    cabecera = _TABLE_HEADER.search(cuerpo)
    fin_raiz = cabecera.start() if cabecera is not None else len(cuerpo)
    coincidencias = list(_MANAGED_KEY_LINE.finditer(cuerpo, 0, fin_raiz))
    if len(coincidencias) != 1:
        raise LootHeadlessSettingsError(
            f"{settings_path}: '{LOOT_NO_SORTING_CHANGES_DIALOG_KEY}' no es un booleano top-level "
            "editable de forma inequívoca; no se modifica. Corregilo a mano a "
            f"'{_MANAGED_LINE}' (clave de loot/loot 0.29.1 loot_settings.cpp:845)."
        )
    match = coincidencias[0]
    return f"{cuerpo[: match.start()]}{match['prefix']}false{match['suffix']}{cuerpo[match.end() :]}"


def _validar_edicion(nuevo_cuerpo: str, *, antes: Mapping[str, Any]) -> None:
    """La edición sólo puede fijar la clave gestionada: nada más cambia."""
    despues = tomllib.loads(nuevo_cuerpo)
    otras_antes = {k: v for k, v in antes.items() if k != LOOT_NO_SORTING_CHANGES_DIALOG_KEY}
    otras_despues = {k: v for k, v in despues.items() if k != LOOT_NO_SORTING_CHANGES_DIALOG_KEY}
    if despues.get(LOOT_NO_SORTING_CHANGES_DIALOG_KEY, _MISSING) is not False or otras_despues != otras_antes:
        raise LootHeadlessSettingsError(
            "La edición del settings de LOOT no fijaría sólo la clave gestionada; no se escribe."
        )


def _escribir_atomico(destino: pathlib.Path, contenido: bytes) -> None:
    temporal = destino.with_name(f"{destino.name}.skyclaw-{secrets.token_hex(8)}.tmp")
    try:
        with temporal.open("xb") as handle:
            handle.write(contenido)
        os.replace(temporal, destino)
    except OSError as exc:
        with contextlib.suppress(OSError):
            temporal.unlink()
        raise LootHeadlessSettingsError(f"No se pudo escribir {destino}: {exc}") from exc
