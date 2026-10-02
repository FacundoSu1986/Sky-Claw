"""PROBE TEMPORAL S4-E0 / R4 — censo reproducible de `runtime_vault.__all__`."""

from __future__ import annotations

import ast
import collections
import importlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import sky_claw.local.runtime_vault as rv  # noqa: E402

PKG_DIR = pathlib.Path(rv.__file__).resolve().parent


def main() -> None:
    alls = list(rv.__all__)
    out: dict[str, object] = {}

    # 1. Duplicados
    dups = {n: c for n, c in collections.Counter(alls).items() if c > 1}
    out["__all___len"] = len(alls)
    out["__all___unicos"] = len(set(alls))
    out["DUPLICADOS"] = dups

    # 2. Nombres que NO existen en el paquete (ni como atributo, ni como submodule)
    faltantes = []
    for name in sorted(set(alls)):
        if hasattr(rv, name):
            continue
        try:
            importlib.import_module(f"sky_claw.local.runtime_vault.{name}")
        except Exception:  # noqa: BLE001
            faltantes.append(name)
    out["FALTANTES_controversia_55"] = faltantes
    out["TOTAL_FALTANTES"] = len(faltantes)

    # 3. Nombres públicos de submódulos NO declarados en __all__
    submodulos = sorted(
        p.stem for p in PKG_DIR.glob("*.py") if not p.stem.startswith("_") and p.stem != "__init__"
    )
    from_imports: set[str] = set()
    for p in PKG_DIR.glob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("sky_claw.local.runtime_vault"):
                    from_imports.update(a.name for a in node.names)
    out["simbolos_importados_entre_submodulos_no_reexportados"] = sorted(from_imports - set(alls))

    # 4. Nombres declarados en __all__ de submódulos que el paquete no reexporta
    sub_alls: dict[str, list[str]] = {}
    for p in sorted(PKG_DIR.glob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "__all__":
                        try:
                            sub_alls[p.stem] = ast.literal_eval(node.value)
                        except Exception:  # noqa: BLE001
                            pass
    sub_alls.pop("__init__", None)
    no_reexportados = {
        mod: sorted(set(names) - set(alls)) for mod, names in sub_alls.items() if set(names) - set(alls)
    }
    out["__all___de_submodulos_no_reexportados_por_el_paquete"] = {
        k: v for k, v in no_reexportados.items() if v
    }
    out["total_simbolos_publicos_declarados_en_submodulos"] = sum(len(v) for v in sub_alls.values())
    out["total_reexportados_por_el_paquete"] = len(set(alls))

    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
