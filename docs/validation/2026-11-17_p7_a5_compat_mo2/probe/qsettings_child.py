"""Cliente QSettings en PROCESO NUEVO.

Qt mantiene un caché de ``QConfFile`` por proceso, así que medir «MO2 arranca con
el INI bloqueado» desde el mismo proceso que autoró el archivo mide el caché y no
el disco.  Este cliente se lanza como proceso separado para que la primera
lectura tenga que abrir el archivo de verdad.

Modos:
  read       lee y reporta estado/valores/claves.
  write      lee, escribe una clave y sincroniza con el lock retenido.
  deferred   lee con lock, sincroniza con lock (falla), espera la liberación y
             vuelve a sincronizar.
  dtor       lee con lock, deja una clave sucia y termina: el destructor
             sincroniza después de la liberación.
  hold       lee y mantiene el objeto vivo hasta que exista <release>.

Uso: python qsettings_child.py <ini> <modo> <salida_json> [<ready> <release>]
"""

from __future__ import annotations

import json
import pathlib
import sys
import time

from PyQt6.QtCore import QByteArray, QSettings, qVersion


def _norm(valor: object) -> object:
    if isinstance(valor, QByteArray):
        return bytes(valor).decode("utf-8", "replace")
    if isinstance(valor, bytes):
        return valor.decode("utf-8", "replace")
    return valor


def semantica(ini: pathlib.Path) -> dict[str, object]:
    qs = QSettings(str(ini), QSettings.Format.IniFormat)
    ejecutables = []
    total = qs.beginReadArray("customExecutables")
    for indice in range(total):
        qs.setArrayIndex(indice)
        ejecutables.append(
            {campo: _norm(qs.value(campo)) for campo in ("title", "binary", "arguments", "workingDirectory")}
        )
    qs.endArray()
    datos = {
        "gameName": _norm(qs.value("gameName")),
        "gamePath": _norm(qs.value("gamePath")),
        "selected_profile": _norm(qs.value("selected_profile")),
        "n_ejecutables": total,
        "ejecutables": ejecutables,
        "n_keys": len(qs.allKeys()),
        "status": qs.status().name,
    }
    del qs
    return datos


def esperar(path: pathlib.Path, limite_s: float = 60.0) -> None:
    """Espera el marcador; si no llega, FALLA.

    Devolver normalmente al vencer dejaría que ``deferred`` registre un
    ``status_sync_tras_liberar`` que quizá ocurrió con la exclusión todavía
    tomada: mediría un escenario que no es el declarado.
    """
    limite = time.monotonic() + limite_s
    while time.monotonic() < limite:
        if path.exists():
            return
        time.sleep(0.005)
    raise TimeoutError(f"no llegó {path.name} en {limite_s}s: la liberación no ocurrió")


def main() -> int:
    ini = pathlib.Path(sys.argv[1])
    modo = sys.argv[2]
    salida = pathlib.Path(sys.argv[3])
    ready = pathlib.Path(sys.argv[4]) if len(sys.argv) > 4 else None
    release = pathlib.Path(sys.argv[5]) if len(sys.argv) > 5 else None

    resultado: dict[str, object] = {"modo": modo, "qt_runtime": qVersion(), "pid": __import__("os").getpid()}

    if modo in {"deferred", "dtor", "hold"} and (ready is None or release is None):
        # Sin marcadores, el modo no puede observar la liberación: publicar un
        # resultado equivaldría a medir un escenario que no ocurrió.
        raise SystemExit(f"modo {modo}: requiere <ready> y <release>")

    if modo == "read":
        qs = QSettings(str(ini), QSettings.Format.IniFormat)
        resultado["status_tras_ctor"] = qs.status().name
        claves = qs.allKeys()
        resultado["status_tras_leer"] = qs.status().name
        resultado["n_claves"] = len(claves)
        resultado["gamePath"] = _norm(qs.value("gamePath"))
        resultado["gameName"] = _norm(qs.value("gameName"))
        resultado["n_ejecutables"] = qs.beginReadArray("customExecutables")
        qs.endArray()
        # Claves que el escenario esperaba persistir: sin ellas, «conserva los
        # ejecutables» no prueba que el sync haya escrito nada.
        resultado["p7probe"] = {
            "deferred_1": _norm(qs.value("p7probe/deferred_1")),
            "deferred_2": _norm(qs.value("p7probe/deferred_2")),
            "dtor": _norm(qs.value("p7probe/dtor")),
        }
        del qs
        # La cantidad sola no prueba preservación: con índices corridos devuelve
        # 3 igual aunque un elemento esté vacío y otro se haya perdido. Se
        # reportan los campos para compararlos contra el original.
        resultado["ejecutables"] = semantica(ini)["ejecutables"]

    elif modo == "write":
        qs = QSettings(str(ini), QSettings.Format.IniFormat)
        claves = qs.allKeys()
        resultado["n_claves_leidas"] = len(claves)
        qs.setValue("p7probe/escrita_con_lock", "1")
        qs.sync()
        resultado["status_tras_sync"] = qs.status().name
        del qs

    elif modo == "deferred":
        qs = QSettings(str(ini), QSettings.Format.IniFormat)
        claves = qs.allKeys()
        resultado["n_claves_leidas_con_lock"] = len(claves)
        resultado["status_tras_leer"] = qs.status().name
        qs.setValue("p7probe/deferred_1", "1")
        qs.sync()
        resultado["status_sync_con_lock"] = qs.status().name
        if ready is not None:
            ready.write_text("listo", encoding="utf-8")
        if release is not None:
            esperar(release)
        qs.setValue("p7probe/deferred_2", "2")
        qs.sync()
        resultado["status_sync_tras_liberar"] = qs.status().name
        del qs
        resultado["semantica_final"] = semantica(ini)

    elif modo == "dtor":
        qs = QSettings(str(ini), QSettings.Format.IniFormat)
        qs.allKeys()
        qs.setValue("p7probe/dtor", "1")
        if ready is not None:
            ready.write_text("listo", encoding="utf-8")
        if release is not None:
            esperar(release)
        # Sale del bloque sin sync explícito: el destructor sincroniza.
        resultado["nota"] = "salida con clave sucia; el destructor sincroniza"

    elif modo == "hold":
        qs = QSettings(str(ini), QSettings.Format.IniFormat)
        claves = qs.allKeys()
        resultado["n_claves_leidas"] = len(claves)
        resultado["status"] = qs.status().name
        if ready is not None:
            ready.write_text("listo", encoding="utf-8")
        if release is not None:
            esperar(release)
        del qs

    else:
        raise SystemExit(f"modo desconocido: {modo}")

    salida.write_text(json.dumps(resultado, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps({"ok": True, "modo": modo}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
