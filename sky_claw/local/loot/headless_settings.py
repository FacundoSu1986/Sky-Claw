"""Política de settings headless de LOOT en el data root propiedad de Sky-Claw (PR-2).

Una corrida ``--auto-sort`` desatendida sólo cierra sola si ningún camino del GUI
la detiene en un modal o le cancela el auto-cierre. Este módulo gestiona
EXACTAMENTE tres claves top-level de ``<loot-data-path>/settings.toml``, cada
una con evidencia upstream; todo lo demás es de LOOT/del operador.

Evidencia upstream — ``loot/loot`` tag ``0.29.1``, commit
``77f3ba98966819fd6d92d97dcb2dbc4c1b9fb9b9`` (0.29.2 ``0402143e``: misma
semántica, líneas corridas entre paréntesis):

``useNoSortingChangesDialog = false`` — modal de NO_CHANGE.

* ``src/gui/state/loot_settings.h:127``: ``bool useNoSortingChangesDialog_{true};``
  — el default es ``true`` (diálogo activo).
* ``src/gui/state/loot_settings.cpp:845-846`` (``load``):
  ``settings["useNoSortingChangesDialog"].value_or(useNoSortingChangesDialog_)``
  — clave TOML top-level exacta; un valor de otro tipo cae al default ``true``,
  así que el valor gestionado tiene que ser el booleano TOML ``false``.
* ``src/gui/qt/main_window.cpp:1600-1613`` (``handlePluginsSorted``, rama sin
  cambios; 0.29.2 l.1636): ``isNoSortingChangesDialogEnabled()`` →
  ``QMessageBox::information`` (modal); si no → ``showNotification``.
* ``src/gui/qt/main_window.cpp:2870-2884`` (``handlePluginsAutoSorted``):
  ``handlePluginsSorted`` corre ANTES del ``on_actionQuit_triggered()``. Con el
  diálogo activo, NO_CHANGE desatendido nunca llega al quit → timeout → FAIL.

``lastVersion = "<versión atestiguada de LOOT.exe>"`` — modal "First-Time Tips".

* ``src/gui/qt/main_window.cpp:366-368`` (``initialise``; 0.29.2 l.391-392):
  ``if (getLastVersion() != getLootVersion()) showFirstRunDialog();`` — string
  exacto. ``showFirstRunDialog`` (l.1311-1382) termina en
  ``messageBox.exec()`` (modal) ANTES de ``loadGame`` → sin sort, sin quit.
* ``src/gui/state/loot_settings.h:131`` (``std::string lastVersion_;``, vacío
  por default) y ``loot_settings.cpp:854`` (``load``, ``value_or``): un data
  root nuevo SIEMPRE muestra el modal.
* ``src/gui/qt/main_window.cpp:1489-1490`` (``closeEvent``; 0.29.2 l.1517):
  ``updateLastVersion()`` + ``save`` — LOOT sólo persiste la clave al CERRAR,
  así que ni la primera corrida sobre un root nuevo ni la primera tras
  actualizar LOOT pueden cerrar solas sin sembrarla.
* El valor sale de :func:`sky_claw.local.loot.binary_version.read_loot_binary_version`
  (recurso ``VERSIONINFO`` del binario que se va a lanzar, ver evidencia allí);
  nunca se adivina ni se toma de configuración.

``enableLootUpdateCheck = false`` — error de update check que cancela el auto-sort.

* ``src/gui/state/loot_settings.h:126`` (default ``true``) y
  ``loot_settings.cpp:843-844`` (``load``, ``value_or``).
* ``src/gui/qt/main_window.cpp:405-416`` (``initialise``; 0.29.2 l.430): con
  la clave activa, CADA arranque consulta
  ``https://api.github.com/repos/loot/loot/releases/latest``
  (``src/gui/qt/tasks/check_for_update_task.cpp:112``).
* ``main_window.cpp:3062-3082`` / ``3084-3102``: un release más nuevo O un error
  de red agregan un mensaje general de tipo ``MessageType::error``.
* ``main_window.cpp:2830-2846`` (``handleStartupGameDataLoaded``): con algún
  mensaje de error el auto-sort se CANCELA; ``main_window.cpp:2878-2880``
  (``handlePluginsAutoSorted``): sólo sale si ``!hasErrorMessages()``
  (``counters.cpp:29-66`` cuenta generales y de plugins). Resultado: sin red, o
  apenas upstream publica otro release (0.29.2 salió el 2026-08-16), toda
  corrida desatendida de 0.29.1 termina en timeout. El chequeo no aporta nada
  al sort: sólo produce ese mensaje.

``src/gui/state/loot_state.cpp:210-222``: un ``settings.toml`` que no parsea
agrega un init error y ``MainWindow::initialise`` (``main_window.cpp:383-385``)
retorna sin cargar el juego: sin sort y sin quit. Por eso esta política valida
el TOML resultante ANTES de escribir y falla cerrado ante un archivo que no
entiende, en vez de "repararlo".

Política de propiedad (managed vs user):

* **Claves gestionadas por Sky-Claw** (:data:`MANAGED_LOOT_SETTINGS_KEYS`): las
  dos booleanas fijas de :data:`MANAGED_LOOT_HEADLESS_SETTINGS` más
  ``lastVersion``. Autoridad: el data root es propiedad EXCLUSIVA de Sky-Claw
  por instancia+perfil (PR-1, :mod:`sky_claw.local.loot.data_root`, que rechaza
  el default del GUI del operador). Si alguien abre LOOT GUI sobre ESTE root y
  cambia una clave gestionada, Sky-Claw la restaura antes de la próxima corrida.
* **Todo lo demás es de LOOT/del operador** y se preserva byte a byte: la
  edición es textual (líneas antepuestas o el valor de una línea reemplazado),
  nunca una reserialización del archivo. No se tocan masterlist, prelude,
  ``updateMasterlist`` (PR-3), userlist ni ninguna otra preferencia.
* Idempotente: si las tres claves ya tienen el valor gestionado no se escribe.
* Escritura atómica: temporal exclusivo + ``os.replace`` en el mismo directorio.
* Fail-closed: TOML inválido, no-UTF-8, versión mal formada o una forma de la
  clave que no se puede editar con garantías → :class:`LootHeadlessSettingsError`
  y NO se escribe.
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

#: Clave TOML exacta del update check de LOOT (``loot_settings.cpp:843-844``).
LOOT_UPDATE_CHECK_KEY: Final[str] = "enableLootUpdateCheck"

#: Clave TOML exacta que decide el modal "First-Time Tips" (``loot_settings.cpp:854``).
LOOT_LAST_VERSION_KEY: Final[str] = "lastVersion"

#: Claves booleanas gestionadas con su valor FIJO. Congelado por igualdad
#: literal en tests: agregar una clave es una decisión de política con
#: evidencia upstream, no un detalle de implementación.
MANAGED_LOOT_HEADLESS_SETTINGS: Final[Mapping[str, bool]] = {
    LOOT_NO_SORTING_CHANGES_DIALOG_KEY: False,
    LOOT_UPDATE_CHECK_KEY: False,
}

#: Conjunto COMPLETO de claves que Sky-Claw gestiona en el settings de LOOT:
#: las fijas más ``lastVersion`` (su valor es la versión atestiguada del binario).
MANAGED_LOOT_SETTINGS_KEYS: Final[frozenset[str]] = frozenset({*MANAGED_LOOT_HEADLESS_SETTINGS, LOOT_LAST_VERSION_KEY})

#: Forma exacta de ``getLootVersion()`` (``version.cpp.in:27-33``).
_LOOT_VERSION: Final[re.Pattern[str]] = re.compile(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}")

#: Valores editables por tipo: booleano TOML, string básico o literal de una línea.
_VALOR_BOOLEANO: Final[str] = r"true|false"
_VALOR_STRING: Final[str] = r"\"(?:[^\"\\\r\n]|\\.)*\"|'[^'\r\n]*'"


def _patron_de_linea(clave: str, valor: str) -> re.Pattern[str]:
    """Línea top-level ``clave = valor`` (clave desnuda o entre comillas)."""
    nombre = re.escape(clave)
    return re.compile(
        rf"^(?P<prefix>[ \t]*(?:{nombre}|\"{nombre}\"|'{nombre}')[ \t]*=[ \t]*)"
        rf"(?P<value>{valor})(?P<suffix>[ \t]*(?:#[^\r\n]*)?\r?)$",
        re.MULTILINE,
    )


_LINEA_GESTIONADA: Final[Mapping[str, re.Pattern[str]]] = {
    LOOT_NO_SORTING_CHANGES_DIALOG_KEY: _patron_de_linea(LOOT_NO_SORTING_CHANGES_DIALOG_KEY, _VALOR_BOOLEANO),
    LOOT_UPDATE_CHECK_KEY: _patron_de_linea(LOOT_UPDATE_CHECK_KEY, _VALOR_BOOLEANO),
    LOOT_LAST_VERSION_KEY: _patron_de_linea(LOOT_LAST_VERSION_KEY, _VALOR_STRING),
}

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


def managed_loot_settings(loot_version: str) -> dict[str, bool | str]:
    """Valores EXACTOS que Sky-Claw fija para un ``LOOT.exe`` de versión *loot_version*.

    Raises:
        LootHeadlessSettingsError: *loot_version* no tiene la forma de
            ``getLootVersion()`` (``MAJOR.MINOR.PATCH`` decimal).
    """
    if not _LOOT_VERSION.fullmatch(loot_version):
        raise LootHeadlessSettingsError(
            f"Versión de LOOT mal formada para lastVersion: {loot_version!r} (se espera MAJOR.MINOR.PATCH)."
        )
    return {**MANAGED_LOOT_HEADLESS_SETTINGS, LOOT_LAST_VERSION_KEY: loot_version}


def ensure_loot_headless_settings(loot_data_path: pathlib.Path, *, loot_version: str) -> LootHeadlessSettingsAction:
    """Garantiza las claves gestionadas para *loot_version* preservando todo lo demás.

    Args:
        loot_data_path: data root aislado propiedad de Sky-Claw (PR-1).
        loot_version: versión ATESTIGUADA del binario que se va a lanzar
            (:func:`~sky_claw.local.loot.binary_version.read_loot_binary_version`).

    Raises:
        LootHeadlessSettingsError: versión mal formada, o el archivo existe pero
            no se puede leer, no es UTF-8, no es TOML válido, o una clave
            gestionada tiene una forma que no se puede editar sin riesgo de
            alterar otra preferencia.
    """
    deseado = managed_loot_settings(loot_version)
    settings_path = loot_data_path / LOOT_SETTINGS_FILENAME
    try:
        raw = settings_path.read_bytes()
    except FileNotFoundError:
        contenido = "".join(f"{clave} = {_literal_toml(valor)}\n" for clave, valor in deseado.items())
        _validar_edicion(contenido, antes={}, deseado=deseado)
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
    pendientes = {clave: valor for clave, valor in deseado.items() if not _tiene_valor(antes, clave, valor)}
    if not pendientes:
        return LootHeadlessSettingsAction.UNCHANGED

    nuevo_cuerpo = cuerpo
    for clave, valor in pendientes.items():
        if clave in antes:
            nuevo_cuerpo = _reemplazar_valor_top_level(nuevo_cuerpo, clave, valor, settings_path)
    ausentes = [clave for clave in pendientes if clave not in antes]
    if ausentes:
        # Líneas antepuestas a TODO el documento siempre caen en la tabla raíz.
        # Respeta el estilo de salto del archivo (CRLF si su primera línea lo usa).
        primer_salto = cuerpo.find("\n")
        salto = "\r\n" if primer_salto > 0 and cuerpo[primer_salto - 1] == "\r" else "\n"
        nuevo_cuerpo = (
            "".join(f"{clave} = {_literal_toml(pendientes[clave])}{salto}" for clave in ausentes) + nuevo_cuerpo
        )
    _validar_edicion(nuevo_cuerpo, antes=antes, deseado=deseado)
    _escribir_atomico(settings_path, (bom + nuevo_cuerpo).encode("utf-8"))
    return LootHeadlessSettingsAction.UPDATED


def _literal_toml(valor: bool | str) -> str:
    if isinstance(valor, bool):
        return "true" if valor else "false"
    # Sólo versiones ya validadas (dígitos y puntos): no requieren escape.
    return f'"{valor}"'


def _tiene_valor(documento: Mapping[str, Any], clave: str, valor: bool | str) -> bool:
    """Igualdad ESTRICTA de tipo: ``1`` no es ``true`` ni ``"false"`` es ``false``."""
    actual = documento.get(clave, _MISSING)
    if isinstance(valor, bool):
        return actual is valor
    return type(actual) is str and actual == valor


def _parsear_o_fallar(cuerpo: str, settings_path: pathlib.Path) -> dict[str, Any]:
    try:
        return tomllib.loads(cuerpo)
    except tomllib.TOMLDecodeError as exc:
        raise LootHeadlessSettingsError(
            f"{settings_path} no es TOML válido ({exc}); no se sobrescribe: LOOT tampoco lo "
            "cargaría (init error → sin sort) y reescribirlo destruiría las preferencias."
        ) from exc


def _reemplazar_valor_top_level(cuerpo: str, clave: str, valor: bool | str, settings_path: pathlib.Path) -> str:
    """Reemplaza el valor de la ÚNICA línea top-level de *clave* por *valor*."""
    cabecera = _TABLE_HEADER.search(cuerpo)
    fin_raiz = cabecera.start() if cabecera is not None else len(cuerpo)
    coincidencias = list(_LINEA_GESTIONADA[clave].finditer(cuerpo, 0, fin_raiz))
    if len(coincidencias) != 1:
        tipo = "booleano" if isinstance(valor, bool) else "string de una línea"
        raise LootHeadlessSettingsError(
            f"{settings_path}: '{clave}' no es un {tipo} top-level editable de forma inequívoca; no se "
            f"modifica. Corregilo a mano a '{clave} = {_literal_toml(valor)}' (clave de loot/loot 0.29.1 "
            "loot_settings.cpp)."
        )
    match = coincidencias[0]
    return f"{cuerpo[: match.start()]}{match['prefix']}{_literal_toml(valor)}{match['suffix']}{cuerpo[match.end() :]}"


def _validar_edicion(nuevo_cuerpo: str, *, antes: Mapping[str, Any], deseado: Mapping[str, bool | str]) -> None:
    """La edición sólo puede fijar las claves gestionadas: nada más cambia.

    Excluye SÓLO las claves gestionadas top-level: una clave homónima dentro de
    una tabla (p. ej. ``[[games]]``) viaja dentro del valor de esa tabla y se
    compara entera, así que cualquier cambio en ella hace fallar la validación.
    """
    try:
        despues = tomllib.loads(nuevo_cuerpo)
    except tomllib.TOMLDecodeError as exc:
        raise LootHeadlessSettingsError(f"La edición del settings de LOOT produciría TOML inválido ({exc}).") from exc
    otras_antes = {k: v for k, v in antes.items() if k not in MANAGED_LOOT_SETTINGS_KEYS}
    otras_despues = {k: v for k, v in despues.items() if k not in MANAGED_LOOT_SETTINGS_KEYS}
    if otras_despues != otras_antes or not all(_tiene_valor(despues, k, v) for k, v in deseado.items()):
        raise LootHeadlessSettingsError(
            "La edición del settings de LOOT no fijaría sólo las claves gestionadas; no se escribe."
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
