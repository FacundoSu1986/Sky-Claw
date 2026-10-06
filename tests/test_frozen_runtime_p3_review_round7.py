"""P3 ronda 7 — P3-Z: la canonicalizacion de relpaths aceptaba componentes
inseguros o ambiguos (NUL, caracteres de control, DEL, espacio/punto final).

Findings de una auditoria adversarial EXTERNA sobre ``c5472b7a``. Dos probes
apuntan a la MISMA raiz y se tratan como UN solo blocker:

* **T3** — ``Data/\\x00evil``: el canonicalizador no prohibia NUL ni controles,
  asi que un relpath hostil sobrevivia la canonicalizacion. Como P3 promete
  "validar TODO el lote ANTES de la primera mutacion", el rechazo tiene que
  ocurrir en la primitive y no mas tarde, cuando Python/OS materialice el path
  (``ValueError: embedded null byte``), que produce un fallo TARDIO con mutacion
  parcial y una familia de excepcion equivocada.

* **T13** — ``Data/Foo `` / ``Data/Foo.``: componentes con espacio o punto final
  son ambiguos en Windows (el SO los recorta, asi que dos nombres distintos
  apuntan al mismo archivo). Sobre un arbol SELLADO, "limpiarlos" destruiria la
  identidad: ``Foo`` y ``Foo `` NO pueden colapsar en el mismo canonico.

Contrato que estos tests congelan:

* se RECHAZAN NUL (U+0000), los ASCII de control (U+0001..U+001F) y DEL (U+007F);
* se RECHAZAN los componentes terminados en espacio o en punto;
* se ACEPTA el espacio INTERNO legitimo (``Data/My Folder``): no es "prohibir
  espacios", es prohibir los que Windows recorta;
* se PRESERVA la canonicalizacion ya declarada (``\\`` -> ``/``, ``//``
  colapsado, ``.`` eliminado).

El space LEADING queda FUERA de este micro-slice: Windows lo acepta de forma
estable, asi que no se inventa una prohibicion que ningun contrato del repo pide
(la regla de ``candidate_id.py`` -- ``strip()`` -- no es trasladable a un
relpath, donde el espacio inicial de un componente es significativo).

RED-first: cada test se escribio y se vio FALLAR contra ``5472b7a`` (``P3_Z_PRE_FIX_RED``).
"""

from __future__ import annotations

import pathlib

import pytest

from sky_claw.local.frozen_runtime.copying import copiar_arbol_independiente
from sky_claw.local.frozen_runtime.errors import CandidateCopyError
from sky_claw.local.frozen_runtime.membership import (
    DirectoryMembershipError,
    canonicalizar_archivo,
    canonicalizar_directorio,
    canonicalizar_relpath_de_scope,
)
from sky_claw.local.runtime_vault.models import FileIdentity

# Tipos de scope: la primitive es UNICA y comparten membership y copia, asi que
# un contrato que valga para uno solo de los dos es exactamente el hueco por
# donde pasaria el traversal.
TIPOS = ("archivo", "directorio")


def canonicos(entrada: str, tipo: str) -> str:
    return canonicalizar_relpath_de_scope(entrada, tipo=tipo)


# ── P3-Z (T3) · NUL y controles ASCII ──────────────────────────────────────


@pytest.mark.parametrize("tipo", TIPOS)
def test_p3z_nul_es_rechazado(tipo: str) -> None:
    """T3: ``Data/\\x00evil`` no puede sobrevivir la canonicalizacion.

    Pre-fix se aceptaba tal cual (``'Data/\\x00evil'``), y el fallo aparecia
    despues -- al unir el relpath a una raiz, o al abrirlo -- fuera de la
    frontera tipada y con el arbol ya mutado.
    """
    with pytest.raises(DirectoryMembershipError):
        canonicos("Data/\x00evil", tipo)


@pytest.mark.parametrize("tipo", TIPOS)
@pytest.mark.parametrize(
    "entrada",
    [
        "Data/\x01evil",  # primer control imprimible-adyacente (SOH)
        "Data/\tevil",  # TAB
        "Data/\nevil",  # LF
        "Data/\revil",  # CR
        "Data/\x1fevil",  # ULTIMO control de la familia U+0001..U+001F
        "Data/\x7fevil",  # DEL (fuera de <0x20: se comprueba aparte)
    ],
)
def test_p3z_control_y_del_son_rechazados(tipo: str, entrada: str) -> None:
    """T3/T13: los boundaries 0x00, 0x1F y 0x7F quedan congelados.

    No se enumeran los 33 caracteres: se cubren los extremos de la familia mas
    representantes del medio, y la implementacion es estructural (un solo
    predicado sobre la entrada), no una lista de casos.
    """
    with pytest.raises(DirectoryMembershipError):
        canonicos(entrada, tipo)


@pytest.mark.parametrize("tipo", TIPOS)
def test_p3z_control_no_puede_colarse_como_componente_interno(tipo: str) -> None:
    """El control embebido tampoco vale en un componente intermedio ni en la raiz."""
    with pytest.raises(DirectoryMembershipError):
        canonicos("Data\x00/Meshes/a.nif", tipo)
    with pytest.raises(DirectoryMembershipError):
        canonicos("Data/Meshes/a\x0b.nif", tipo)


@pytest.mark.parametrize("tipo", TIPOS)
def test_p3z_una_entrada_solo_control_es_rechazada(tipo: str) -> None:
    """``" "``/``"\\t"``/``"\\n"`` como entrada COMPLETA siguen siendo degeneradas.

    El caso vacio ya se rechazaba; estos son sus hermanos de control y tienen
    que caer por la MISMA razon (no porque "queden" degenerados por casualidad).
    """
    for entrada in ("\t", "\n", " ", "\x00", "\x7f"):
        with pytest.raises(DirectoryMembershipError):
            canonicos(entrada, tipo)


# ── P3-Z (T13) · componente terminado en espacio o punto ───────────────────


@pytest.mark.parametrize("tipo", TIPOS)
@pytest.mark.parametrize("entrada", ["Data/Foo ", "Data/Foo."])
def test_p3z_componente_con_espacio_o_punto_final_es_rechazado(tipo: str, entrada: str) -> None:
    """T13: Windows recorta el sufijo, asi que ``Foo `` y ``Foo`` serian el mismo path.

    Aceptarlos (o peor: normalizarlos a ``Foo``) destruiria la identidad sellada:
    el digest de membership afirmaria haber visto un arbol que no es el que hay.
    El contrato es REJECT, nunca "limpiar".
    """
    with pytest.raises(DirectoryMembershipError):
        canonicos(entrada, tipo)


@pytest.mark.parametrize("tipo", TIPOS)
@pytest.mark.parametrize("entrada", ["Data/Meshes /x.nif", "Data/Meshes./x.nif"])
def test_p3z_componente_intermedio_ambiguo_es_rechazado(tipo: str, entrada: str) -> None:
    """El componente ambiguo puede estar en el medio: no se mira solo el ultimo."""
    with pytest.raises(DirectoryMembershipError):
        canonicos(entrada, tipo)


@pytest.mark.parametrize("tipo", TIPOS)
def test_p3z_los_espacios_internos_siguen_siendo_legitimos(tipo: str) -> None:
    """Control: NO es "prohibir espacios", es prohibir los que Windows recorta."""
    assert canonicos("Data/My Folder", tipo) == "Data/My Folder"
    assert canonicos("Meshes/Armor Set/file.nif", tipo) == "Meshes/Armor Set/file.nif"
    assert canonicos("Data/My  Folder", tipo) == "Data/My  Folder"  # doble espacio interno


@pytest.mark.parametrize("tipo", TIPOS)
def test_p3z_la_canonicalizacion_ya_declarada_se_preserva(tipo: str) -> None:
    """Control de no-regresion: el hardening no toca lo que P3 ya normalizaba.

    ``Data/./Meshes`` es un componente ESTRUCTURAL (describe el mismo
    directorio) y se normaliza; ``Data/Foo.`` es un NOMBRE ambiguo y se rechaza.
    La diferencia es el motivo del rechazo, no el caracter.
    """
    assert canonicos("Data\\Meshes", tipo) == "Data/Meshes"
    assert canonicos("Data//Meshes", tipo) == "Data/Meshes"
    assert canonicos("Data/./Meshes", tipo) == "Data/Meshes"
    assert canonicos("Data", tipo) == "Data"
    assert canonicos("Data/My Folder/file.nif", tipo) == "Data/My Folder/file.nif"


def test_p3z_el_espacio_leading_queda_fuera_del_contrato_de_rechazo() -> None:
    """Documentacion ejecutable del recorte: el leading space no se prohibe.

    Windows acepta de forma estable un componente que EMPIEZA con espacio, y
    ningun contrato del repo exige rechazarlo. ``candidate_id.py`` si exige
    ``candidate_id == candidate_id.strip()``, pero su razon no es trasladable:
    un id es un token opaco, mientras que un relpath de scope describe un nombre
    real del arbol, donde el espacio inicial es significativo. Si algun dia se
    decide prohibirlo, este test es el que hay que cambiar -- a proposito.
    """
    assert canonicalizar_directorio("Data/ Foo") == "Data/ Foo"


# ── P3-Z · la copia rechaza el lote hostil ANTES de la primera mutacion ────


def _armar(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """Origen con un archivo legitimo + contenedor listo para el payload."""
    raiz_origen = tmp_path / "origen"
    (raiz_origen / "Data").mkdir(parents=True)
    (raiz_origen / "Data" / "a.bin").write_bytes(b"AAA")

    contenedor = tmp_path / "frozen"
    (contenedor / "candidates" / "cand_x").mkdir(parents=True)
    destino = contenedor / "candidates" / "cand_x" / "payload"
    return raiz_origen, contenedor, destino


def _snapshot(raiz: pathlib.Path) -> dict[str, bytes | None]:
    salida: dict[str, bytes | None] = {}
    for entrada in sorted(raiz.rglob("*")):
        rel = entrada.relative_to(raiz).as_posix()
        salida[rel] = entrada.read_bytes() if entrada.is_file() else None
    return salida


@pytest.mark.parametrize(
    "hostil",
    [
        "Data/\x00evil.bin",
        "Data/\x01evil.bin",
        "Data/\x1fevil.bin",
        "Data/\x7fevil.bin",
        "Data/tab\t.bin",
        # Sufijo ambiguo REAL: el componente (el ULTIMO) termina en el espacio o
        # el punto. `Data/foo .bin` NO sirve como caso: termina en `bin`, y el
        # punto/espacio interno es legitimo. El sufijo tiene que estar en el
        # borde del name del componente, no en el medio.
        "Data/trailing ",
        "Data/trailing.",
    ],
)
def test_p3z_un_lote_hostil_no_muta_nada_antes_de_fallar(tmp_path, hostil: str) -> None:
    """El ancla de ``ALL_COPY_INPUTS_VALIDATED_BEFORE_FIRST_MUTATION``.

    La entrada hostil NO es la primera: hay un archivo legitimo (que existe en
    disco) por delante. Si la copia canonicalizara entrada por entrada mientras
    copia -- en vez de validar el lote completo primero -- el legitimo ya
    estaria escrito cuando reventara el hostil. El resultado exigido es
    ``CandidateCopyError`` con CERO mutaciones.
    """
    raiz_origen, contenedor, destino = _armar(tmp_path)
    antes_origen = _snapshot(raiz_origen)
    antes_contenedor = _snapshot(contenedor)

    archivos = (
        FileIdentity(rel_path="Data/a.bin", size=3, digest="a" * 64),
        FileIdentity(rel_path=hostil, size=1, digest="b" * 64),
    )

    with pytest.raises(CandidateCopyError):
        copiar_arbol_independiente(raiz_origen, destino, archivos, ("Data",), contenedor=contenedor)

    assert not destino.exists(), "se creo el payload antes de validar el lote"
    assert _snapshot(contenedor) == antes_contenedor, "hubo mutacion en el contenedor"
    assert _snapshot(raiz_origen) == antes_origen, "el source fue mutado (MANAGED_SOURCE_WRITES=NO)"


def test_p3z_un_directorio_hostil_tambien_corta_el_lote(tmp_path) -> None:
    """El hermano: el hostil puede venir por la lista de DIRECTORIOS."""
    raiz_origen, contenedor, destino = _armar(tmp_path)
    antes_contenedor = _snapshot(contenedor)

    archivos = (FileIdentity(rel_path="Data/a.bin", size=3, digest="a" * 64),)

    with pytest.raises(CandidateCopyError):
        copiar_arbol_independiente(
            raiz_origen,
            destino,
            archivos,
            ("Data", "Data/Foo ", "Data/Meshes"),
            contenedor=contenedor,
        )

    assert not destino.exists(), "se creo el payload antes de validar el lote"
    assert _snapshot(contenedor) == antes_contenedor


def test_p3z_el_padre_refusado_no_abre_una_segunda_via_de_entrada(tmp_path) -> None:
    """Hermano CERRADO: la lista de archivos no puede ser un camino alternativo.

    ``Data/Dir `` se refusa como directorio. La pregunta es si un ARCHIVO que
    vive dentro de ese directorio refusado puede colarse igual, porque su propia
    lista lo valida por su cuenta y el nombre del padre no aparece en ella.

    Respuesta verificada: NO. El relpath del archivo es ``Data/Dir /b.bin``, y su
    componente ambiguo es el PADRE, que viaja dentro del string del archivo; la
    validacion de archivos lo ve y corta el lote antes de su primer ``mkdir``.

    Se deja como prueba de que la doble validacion no es decorativa y de que la
    superficie de entrada no tiene una segunda puerta: si alguien reescribe la
    validacion de archivos para mirar solo el nombre final (``Data/Dir /b.bin`` ->
    ``b.bin``), este test se rompe.
    """
    raiz_origen, contenedor, destino = _armar(tmp_path)
    antes_contenedor = _snapshot(contenedor)

    archivos = (
        FileIdentity(rel_path="Data/a.bin", size=3, digest="a" * 64),
        FileIdentity(rel_path="Data/Dir /b.bin", size=3, digest="b" * 64),
    )

    with pytest.raises(CandidateCopyError):
        copiar_arbol_independiente(raiz_origen, destino, archivos, ("Data", "Data/Dir "), contenedor=contenedor)

    assert not destino.exists(), "se creo el payload antes de validar el lote"
    assert _snapshot(contenedor) == antes_contenedor, "hubo mutacion en el contenedor"


def test_p3z_el_limite_canonicalizacion_copia_traduce_la_familia(tmp_path) -> None:
    """La frontera de copying traduce ``DirectoryMembershipError`` a ``CandidateCopyError``.

    No se exige que la copia reciba un ``FileIdentity`` invalido por su dataclass
    (no valida): lo que se ancla es la PARIDAD membership<->copying. Una
    canonicalizacion fallida en la primitive nunca puede escapar cruda desde la
    copia (``ValueError: embedded null byte`` de una API de filesystem), porque
    el llamador tipado de la copia no sabria convertirla en veredicto.
    """
    raiz_origen, contenedor, destino = _armar(tmp_path)
    antes_contenedor = _snapshot(contenedor)

    with pytest.raises(CandidateCopyError) as excinfo:
        copiar_arbol_independiente(
            raiz_origen,
            destino,
            (FileIdentity(rel_path="Data/a.bin", size=3, digest="a" * 64),),
            ("Data", "Data/\x00evil"),
            contenedor=contenedor,
        )

    assert isinstance(excinfo.value.__cause__, DirectoryMembershipError), (
        "la copia no puede perder la causa tipada de la canonicalizacion"
    )
    assert _snapshot(contenedor) == antes_contenedor


def test_p3z_una_copia_legitima_sigue_funcionando(tmp_path) -> None:
    """Control: el hardening no puede volverse un rechazo general de la copia."""
    raiz_origen, contenedor, destino = _armar(tmp_path)
    (raiz_origen / "Data" / "My Folder").mkdir(parents=True)
    (raiz_origen / "Data" / "My Folder" / "b.bin").write_bytes(b"BBB")

    archivos = (
        FileIdentity(rel_path="Data/a.bin", size=3, digest="a" * 64),
        FileIdentity(rel_path="Data/My Folder/b.bin", size=3, digest="b" * 64),
    )

    copiados = copiar_arbol_independiente(
        raiz_origen, destino, archivos, ("Data", "Data/My Folder"), contenedor=contenedor
    )

    assert copiados == 2
    assert (destino / "Data" / "a.bin").read_bytes() == b"AAA"
    assert (destino / "Data" / "My Folder" / "b.bin").read_bytes() == b"BBB"


def test_p3z_la_primitive_es_la_unica_y_los_wrappers_la_respetan() -> None:
    """Las dos caras publicas heredan el contrato: no hay regla duplicada."""
    for hostil in ("Data/\x00evil", "Data/Foo ", "Data/Foo."):
        with pytest.raises(DirectoryMembershipError):
            canonicalizar_archivo(hostil)
        with pytest.raises(DirectoryMembershipError):
            canonicalizar_directorio(hostil)
