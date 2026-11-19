"""Volcado crudo de lo que QSettings interpreta de un INI (para no adivinar)."""

from __future__ import annotations

import pathlib
import sys

from PyQt6.QtCore import QSettings, qVersion

ruta = pathlib.Path(sys.argv[1])
qs = QSettings(str(ruta), QSettings.Format.IniFormat)
print("qt", qVersion())
print("status", qs.status().name)
print("fileName", qs.fileName())
claves = qs.allKeys()
print("n_keys", len(claves))
for clave in claves:
    print(f"  {clave!r} = {qs.value(clave)!r}")
