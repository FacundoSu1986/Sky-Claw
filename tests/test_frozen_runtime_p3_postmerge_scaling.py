"""Caracterizacion del scan de conflictos estructurales de P3 (finding F1 de PR #682).

F1 · `copying.py::_validar_coherencia_del_lote` comparaba cada archivo contra
     TODOS los demas (y contra todos los directorios): `N² + N×D` comparaciones de
     prefijo. El scan corre ANTES de la primera mutacion, asi que el preflight de
     un arbol de Skyrim real (decenas de miles de archivos) dominaba el costo de
     la operacion entera. Medido con entradas sinteticas deterministas contra
     `97dcc7ab`: 20k archivos / 2k directorios tardaban ~21.7 s.

El veredicto NO puede cambiar: duplicados canonicos, colision archivo/directorio
y archivo-ancestro siguen siendo los tres motivos de rechazo, y las jerarquias
legitimas siguen pasando. Por eso la optimizacion se ancla con DOS propiedades:

1. equivalencia exhaustiva contra una referencia cuadratica congelada, sobre
   lotes random con vocabulario adversarial; y
2. una cota de OPERACIONES (comparaciones de prefijo) que congela la clase
   algoritmica sin depender de umbrales de tiempo fragiles.

RED-first: `test_f1_el_scan_no_compara_cada_archivo_contra_todos_los_demas` se
escribio y se vio FALLAR contra `97dcc7ab` (`P3_POSTMERGE_PRE_FIX_RED`), midiendo
176.000 comparaciones para 440 rutas.
"""

from __future__ import annotations

import pathlib
import random

import pytest

from sky_claw.local.frozen_runtime import copying as copying_module
from sky_claw.local.frozen_runtime.errors import CandidateCopyError

# ===========================================================================
# F1 · el scan de conflictos estructurales no puede ser cuadratico
# ===========================================================================


def _referencia_cuadratica(directorios: tuple[str, ...], archivos: tuple[str, ...]) -> str | None:
    """Algoritmo ORIGINAL (`97dcc7ab`), congelado como referencia semantica.

    Devuelve el motivo del primer conflicto detectado, o ``None`` si el lote es
    coherente. Se conserva la forma cuadratica a proposito: es el oraculo contra
    el que se compara la implementacion nueva, y la unica forma de garantizar que
    la optimizacion no cambio el veredicto sobre NINGUN lote.
    """
    vistos_archivos_cf: set[str] = set()
    for a in archivos:
        cf = a.casefold()
        if cf in vistos_archivos_cf:
            return "archivos canonicos duplicados"
        vistos_archivos_cf.add(cf)

    vistos_dirs_cf: set[str] = set()
    for d in directorios:
        cf = d.casefold()
        if cf in vistos_dirs_cf:
            return "directorios canonicos duplicados"
        vistos_dirs_cf.add(cf)

    if vistos_archivos_cf & vistos_dirs_cf:
        return "colision archivo/directorio"

    for a in archivos:
        prefijo = a.casefold() + "/"
        for otro_a in archivos:
            if otro_a.casefold().startswith(prefijo):
                return "archivo ancestro de archivo"
        for d in directorios:
            if d.casefold().startswith(prefijo):
                return "archivo ancestro de directorio"
    return None


def _veredicto(directorios: tuple[str, ...], archivos: tuple[str, ...]) -> str | None:
    try:
        copying_module._validar_coherencia_del_lote(directorios, archivos)
    except CandidateCopyError as exc:
        return str(exc)
    return None


#: Vocabulario adversarial: jerarquias legitimas, variantes de mayusculas,
#: duplicados exactos, colision archivo/directorio y archivo-ancestro.
_VOCABULARIO = (
    "Data",
    "data",
    "Data/Meshes",
    "Data/meshes",
    "Data/Meshes/Characters",
    "Data/Meshes/Characters/a.nif",
    "Data/Meshes/Characters/A.NIF",
    "Data/Meshes/b.nif",
    "Data/Foo",
    "Data/Foo.bin",
    "Data/Foo/bar.bin",
    "Data/Textures",
    "Data/Textures/c.dds",
    "SkyrimSE.exe",
    "skyrimse.exe",
    "Data/Empty",
)


def test_f1_el_scan_conserva_el_veredicto_de_la_referencia_cuadratica() -> None:
    """Equivalencia exhaustiva contra el algoritmo original sobre lotes random.

    La optimizacion no puede cambiar QUE lotes se rechazan ni por que familia:
    duplicados canonicos, colision archivo/directorio y archivo-ancestro siguen
    siendo los tres motivos, y las jerarquias legitimas siguen pasando.
    """
    azar = random.Random(20261007)
    discrepancias: list[str] = []
    for _ in range(400):
        n_archivos = azar.randint(0, 8)
        n_dirs = azar.randint(0, 5)
        archivos = tuple(azar.choice(_VOCABULARIO) for _ in range(n_archivos))
        directorios = tuple(azar.choice(_VOCABULARIO) for _ in range(n_dirs))

        esperado = _referencia_cuadratica(directorios, archivos) is not None
        obtenido = _veredicto(directorios, archivos) is not None
        if esperado != obtenido:
            discrepancias.append(f"dirs={directorios} archivos={archivos} esperado={esperado} obtenido={obtenido}")

    assert not discrepancias, "el scan cambio de veredicto respecto de la referencia:\n" + "\n".join(discrepancias[:5])


def test_f1_los_motivos_de_rechazo_siguen_siendo_los_tres_de_siempre() -> None:
    """Cada familia de conflicto sigue disparando su propio rechazo (no un catch-all)."""
    casos = [
        ((), ("Data/a.bin", "Data/a.bin"), "duplicados"),
        (("Data/Meshes", "Data/Meshes"), (), "duplicados"),
        (("Data/Foo",), ("Data/Foo",), "colision"),
        ((), ("Data/Foo", "Data/Foo/bar.bin"), "ancestro"),
        (("Data/Foo/Bar",), ("Data/Foo",), "ancestro"),
        # El caso que romperia un chequeo "solo contra el vecino inmediato": el
        # path intermedio `Data/Foo.bin` se ordena ENTRE `Data/Foo` y
        # `Data/Foo/Bar` porque '.' (0x2E) < '/' (0x2F). El barrido por bisect no
        # depende de la adyacencia; comparar solo con el siguiente lo perderia.
        (("Data/Foo/Bar",), ("Data/Foo", "Data/Foo.bin"), "ancestro"),
        # El mismo vocabulario con AMBOS descendientes en la lista de ARCHIVOS:
        # `Data/Foo.bin` queda entre `Data/Foo` y `Data/Foo/z.bin`, asi que
        # "solo el vecino inmediato" no llega a ver el descendiente real.
        ((), ("Data/Foo", "Data/Foo.bin", "Data/Foo/z.bin"), "ancestro"),
        # Jerarquia legitima: el directorio ancestro de un archivo NO es conflicto.
        (("Data", "Data/Meshes"), ("Data/Meshes/a.nif",), None),
        (("Data",), ("Data/a.bin", "Data/b.bin"), None),
    ]
    for directorios, archivos, fragmento in casos:
        motivo = _veredicto(directorios, archivos)
        if fragmento is None:
            assert motivo is None, f"{directorios} {archivos} deberia pasar, dio: {motivo}"
        else:
            assert motivo is not None, f"{directorios} {archivos} deberia rechazarse por '{fragmento}'"
            assert fragmento in motivo, f"se esperaba '{fragmento}' en: {motivo}"


def test_f1_el_tipo_de_conflicto_conserva_la_precedencia_archivo_antes_que_directorio() -> None:
    """Con descendientes de ambos tipos, gana el motivo `archivo ancestro de archivo`.

    La referencia cuadratica escaneaba primero TODOS los archivos y despues los
    directorios, asi que ante un mismo `a` con descendiente archivo Y descendiente
    directorio reportaba archivo-ancestro-de-archivo. Consultar una lista combinada
    elegia el primero en orden lexicografico —aca `Data/Foo/0`, un directorio,
    porque `'0' < 'z'`— y cambiaba el motivo reportado (CodeRabbit sobre #698).
    """
    directorios = ("Data/Foo/0",)
    archivos = ("Data/Foo", "Data/Foo/z.bin")

    assert _referencia_cuadratica(directorios, archivos) == "archivo ancestro de archivo"
    motivo = _veredicto(directorios, archivos)
    assert motivo is not None
    assert "ancestro del archivo" in motivo, motivo

    # Espejo: sin descendiente archivo, el unico motivo posible es el directorio.
    motivo_solo_dir = _veredicto(("Data/Foo/0",), ("Data/Foo",))
    assert motivo_solo_dir is not None
    assert "ancestro del directorio" in motivo_solo_dir, motivo_solo_dir


class _CanonicoObservado(str):
    """Path ya canonicalizado que cuenta comparaciones de prefijo contra el.

    El costo dominante del algoritmo original era comparar cada archivo contra
    todos los demas; contar `startswith` sobre el valor canonicalizado mide
    exactamente esa clase de trabajo sin depender de relojes ni de umbrales de
    tiempo fragiles.
    """

    comparaciones_de_prefijo = 0

    def startswith(self, *args: object, **kwargs: object) -> bool:  # type: ignore[override]
        type(self).comparaciones_de_prefijo += 1
        return str.startswith(self, *args, **kwargs)  # type: ignore[arg-type]


class _RelPathObservado(str):
    """`rel_path` de entrada cuyo `casefold()` devuelve el instrumento."""

    def casefold(self) -> str:  # type: ignore[override]
        return _CanonicoObservado(str.casefold(self))


def test_f1_el_scan_no_compara_cada_archivo_contra_todos_los_demas() -> None:
    """Cota de operaciones: comparaciones de prefijo lineales en (archivos + directorios).

    Con el algoritmo cuadratico esto hacia `N² + N×D` comparaciones; con la
    implementacion nueva queda en `N`. La cota es holgada a proposito: congela la
    CLASE algoritmica, no una constante de implementacion.
    """
    n_archivos, n_dirs = 400, 40
    archivos = tuple(_RelPathObservado(f"Data/D{i:03d}/a{i:04d}.bin") for i in range(n_archivos))
    directorios = tuple(_RelPathObservado(f"Data/D{i:03d}") for i in range(n_dirs))

    _CanonicoObservado.comparaciones_de_prefijo = 0
    copying_module._validar_coherencia_del_lote(directorios, archivos)  # type: ignore[arg-type]
    comparaciones = _CanonicoObservado.comparaciones_de_prefijo

    total = n_archivos + n_dirs
    cuadratico = n_archivos * n_archivos + n_archivos * n_dirs
    assert comparaciones <= 4 * total, (
        f"el scan hizo {comparaciones} comparaciones de prefijo para {total} rutas "
        f"(cuadratico seria {cuadratico}): sigue comparando cada par"
    )


def test_f1_el_scan_sigue_rechazando_antes_de_la_primera_mutacion(tmp_path: pathlib.Path) -> None:
    """Un lote incoherente no deja NADA escrito: el orden validar→mutar se preserva.

    La optimizacion no puede haber movido el scan detras de la creacion de
    directorios; se comprueba sobre la primitive publica de copia.
    """
    origen = tmp_path / "origen"
    origen.mkdir()
    (origen / "Data").mkdir()
    contenedor = tmp_path / "frozen"
    (contenedor / "candidates" / "cand_x").mkdir(parents=True)
    destino = contenedor / "candidates" / "cand_x" / "payload"

    with pytest.raises(CandidateCopyError):
        copying_module.copiar_arbol_independiente(
            origen,
            destino,
            (),
            ("Data/Foo", "Data/Foo"),
            contenedor=contenedor,
        )

    assert not destino.exists(), "se muto el destino antes de validar el lote entero"
