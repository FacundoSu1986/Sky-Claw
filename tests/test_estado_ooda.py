"""Contrato ejecutable del inventario vivo de pendientes OODA.

El documento no es fuente canónica por sí solo: estas pruebas cruzan sus filas
con las anclas de familia que enumeran el código productivo.
"""

from __future__ import annotations

import pathlib
import re

from tests.test_borrado_recursivo import MECANISMO_DE_BORRADO, MECANISMO_DE_MEDICION
from tests.test_rollback_reconciler import (
    PRODUCTORES_DEL_NOMBRE,
    RECONCILIADORES_DE_ARRANQUE,
)
from tests.test_rollback_salida import MECANISMO_DE_ROLLBACK, MOTIVO

_RAIZ = pathlib.Path(__file__).resolve().parent.parent
_INVENTARIO = _RAIZ / "docs" / "pending_ooda_status.md"
_SOP = _RAIZ / "sky_claw" / "local" / "AGENTS.md"
_ESTADOS = {"Cerrado", "Parcial", "Abierto", "Bloqueado (rig humano)"}
_COLUMNAS = ["Ítem", "Estado", "Cerrado en", "Qué falta", "Verificado por"]
_ITEMS = frozenset(
    {
        "T-10",
        "T-11",
        "T-12",
        "T-16c",
        "T-22",
        "T-23",
        "T-24",
        "T-25",
        "T-26",
        "T-27",
        "T-28",
        "T-29",
        "T-30",
        "T-31",
        *(f"U-{numero:02d}" for numero in range(1, 13)),
        *(f"F{numero}" for numero in range(1, 10)),
        "F8 USVFS",
        "Detección de enlaces",
        "Borrado recursivo",
        "Medición de árboles",
        "Fugas de lifecycle en tests",
        "Smoke real de QuickAutoClean",
        "Smokes reales restantes",
        "Residuos OODA de bajo valor",
        "Residuos de crash logging",
        "Contrato de argumentos CLI",
        "Contrato de veredicto de éxito",
        "Etapa 6 (Wrye Bash) sin build headless",
        "Orden de masters sin validar",
        "Segundo parser TES4 sin gate",
        # PR-0 (fix/mo2-resolve-instance-mods-dir): la instalación del programa
        # (ModOrganizer.exe) y los datos de la instancia pueden vivir en discos
        # distintos; get_mo2_mods_path() resuelve mods/ desde la metadata de la
        # instancia. Ancla de la deuda hermanada en test_path_resolution_service.py.
        "MO2 executable path != MO2 instance base_directory",
        # Rig T5 2026-08-11 (`INFORME_T5_ARGV_DYNDOLOD_ALPHA209.md` §7.3): el preset
        # persistido de TexGen pre-llena el campo Output de la GUI y desvía las
        # escrituras fuera del root del `-o:`, aunque el argv se parsee exacto. El
        # informe lo rotula "F1"; acá lleva ID descriptivo porque `F1` ya está tomado
        # por la auditoría de resiliencia #319 (cerrado en #328).
        "Preset de TexGen desvía `OutputPath`",
        # Los dos defectos de post-check que el rig 2026-08-10 midió sobre
        # Alpha-209 y que hasta ahora no tenían fila propia: el gate `not errors`
        # rechaza todo éxito real (121 líneas `Error:` en una corrida buena) y el
        # candidato único de TexGen apunta a un directorio que no existe nunca.
        # `U-06` rozaba la completitud de la etapa 9 pero no nombraba ninguno, así
        # que ninguno tenía gate. Plan de cierre en
        # `docs/design/plans/2026-08-16-dyndolod-roadmap-v2.md`.
        "Clasificación del log de la etapa 9",
        "Candidato de salida de TexGen",
        # Los tres agujeros que destapó corregir el candidato: el gate roto los
        # contenía por accidente (TexGen fallaba SIEMPRE, así que nunca se llegaba
        # a empaquetar su salida ni a leerla desde DynDOLOD). Fila propia y no un
        # apéndice de la anterior porque son propiedades distintas —ownership del
        # namespace compartido, propiedad del staging y visibilidad en el Data
        # físico— y cada una tiene su propio conjunto de anclas.
        "Fronteras del handoff TexGen → DynDOLOD",
    }
)


def _celdas(linea: str) -> list[str]:
    """Normaliza el espaciado exterior de una fila Markdown."""
    return [celda.strip() for celda in linea.strip().strip("|").split("|")]


def _tabla() -> dict[str, dict[str, str]]:
    """Lee la tabla de estado sin acoplarla al espaciado Markdown."""
    lineas = _INVENTARIO.read_text(encoding="utf-8").splitlines()
    inicio = next(indice for indice, linea in enumerate(lineas) if _celdas(linea) == _COLUMNAS)
    filas: dict[str, dict[str, str]] = {}
    for linea in lineas[inicio + 2 :]:
        if not linea.strip().startswith("|"):
            break
        valores = _celdas(linea)
        assert len(valores) == len(_COLUMNAS), linea
        fila = dict(zip(_COLUMNAS, valores, strict=True))
        assert fila["Ítem"] not in filas
        filas[fila["Ítem"]] = fila
    return filas


def test_la_tabla_tiene_estados_cerrados_y_anclas_externas() -> None:
    texto = _INVENTARIO.read_text(encoding="utf-8")
    filas = _tabla()

    assert {fila["Estado"] for fila in filas.values()} <= _ESTADOS
    assert "## 2.3 Zero-Trust" in texto
    assert "## Decide — recomendación de próximo frente" in texto
    assert "audits/2026-07_historial_ooda.md" in texto
    assert set(filas) == _ITEMS


def test_el_parser_tolera_espacios_exteriores_en_la_tabla() -> None:
    assert _celdas("  | Ítem | Estado | Cerrado en | Qué falta | Verificado por |   ") == _COLUMNAS


def test_u04_sigue_parcial_solo_por_el_smoke_real_de_pandora() -> None:
    """BodySlide se cerró vía subárbol administrado por grupo
    (``output_targets.bodyslide_output_target`` + ``DirectoryRollback`` en
    ``run_bodyslide_batch``): ya no queda ningún módulo clasificado
    ``"pendiente"``. Lo único que mantiene a U-04 en ``Parcial`` es el smoke
    real de rollback de Pandora — deuda de rig humano, no de código."""
    fila = _tabla()["U-04"]
    pendientes = {modulo for modulo, mecanismo in MECANISMO_DE_ROLLBACK.items() if mecanismo == "pendiente"}

    assert pendientes == set()
    assert set(MOTIVO) == {"sky_claw/local/tools/vramr_service.py"}
    assert fila["Estado"] == "Parcial"
    assert "Pandora" in fila["Qué falta"]
    assert "BodySlide" not in fila["Qué falta"]
    assert "Smoke real de rollback de Pandora" in fila["Qué falta"]
    assert "test_rollback_salida.py" in fila["Verificado por"]
    assert "test_pandora_service.py" in fila["Verificado por"]
    assert "test_bodyslide_lock.py" in fila["Verificado por"]


def test_pandora_documenta_la_salida_administrada_sin_cerrar_el_sandbox() -> None:
    sandbox = (_RAIZ / "sky_claw" / "local" / "mo2" / "sandbox_run.py").read_text(encoding="utf-8")
    adr = (_RAIZ / "docs" / "adr" / "0005-sandbox-promocion-sincrona-hitl.md").read_text(encoding="utf-8")
    deployment = (_RAIZ / "docs" / "operations" / "deployment_standalone_usvfs.md").read_text(encoding="utf-8")

    assert "<game>/Pandora_Output" in sandbox
    assert "<game>/Pandora_Output" in adr
    assert "<juego resuelto>/Pandora_Output" in deployment
    for texto in (sandbox, adr, deployment):
        assert "USVFS" in texto
    assert "supersedida" in adr.lower()


def test_t25_nombra_t27_antes_del_rig_humano() -> None:
    fila = _tabla()["T-25"]
    que_falta = fila["Qué falta"]

    assert fila["Estado"] == "Parcial"
    assert "T-27" in que_falta
    assert que_falta.index("T-27") < que_falta.index("matriz E2E")
    assert "TECHNICAL_REVIEW_TASKS.md" in fila["Verificado por"]


def test_t27_sigue_parcial_hasta_aislar_los_mutadores_restantes() -> None:
    fila = _tabla()["T-27"]

    assert fila["Estado"] == "Parcial"
    assert "USVFS" in fila["Qué falta"]
    assert "Pandora" in fila["Qué falta"]
    assert "DynDOLOD" in fila["Qué falta"]
    assert "Wrye Bash" in fila["Qué falta"]
    assert "test_pandora_service.py" in fila["Verificado por"]


def test_u08_cerrado_se_apoya_en_el_reconciliador_enumerativo() -> None:
    fila = _tabla()["U-08"]

    assert PRODUCTORES_DEL_NOMBRE
    assert RECONCILIADORES_DE_ARRANQUE
    assert fila["Estado"] == "Cerrado"
    assert "test_rollback_reconciler.py" in fila["Verificado por"]


def test_deteccion_de_enlaces_delega_en_un_solo_modulo() -> None:
    fila = _tabla()["Detección de enlaces"]
    ancla = (_RAIZ / "tests" / "test_links.py").read_text(encoding="utf-8")

    assert 'implementan == {"sky_claw/app/security/links.py"}' in ancla
    assert fila["Estado"] == "Cerrado"
    assert "test_links.py" in fila["Verificado por"]


def test_borrado_recursivo_no_puede_cerrarse_con_modulos_pendientes() -> None:
    fila = _tabla()["Borrado recursivo"]
    pendientes = {modulo for modulo, mecanismo in MECANISMO_DE_BORRADO.items() if mecanismo == "pendiente"}

    assert not pendientes
    assert fila["Estado"] == "Cerrado"
    assert "test_borrado_recursivo.py" in fila["Verificado por"]


def _ruta_del_test_citado(nombre: str) -> pathlib.Path:
    """Resuelve un ``.py`` citado en la tabla, conservando su directorio.

    Acepta tanto el nombre pelado (``test_links.py``, que es como está citada
    hoy toda la tabla) como una ruta con subdirectorio (``bdd/test_x.py``) o con
    el prefijo ``tests/`` explícito.

    **No se colapsa a ``.name``**: quedarse con el nombre del archivo haría que
    un ``tests/test_x.py`` cualquiera satisficiera una cita de
    ``bdd/test_x.py``, y el gate diría que existe un test que no existe — el
    mismo error que este gate existe para atajar, un nivel más abajo. Con
    ``tests/bdd/`` ya en el árbol, la ruta con subdirectorio es una cita
    plausible.
    """
    ruta = pathlib.Path(nombre)
    return _RAIZ / (ruta if ruta.parts[0] == "tests" else "tests" / ruta)


def test_todo_test_citado_como_verificacion_existe() -> None:
    """Ningún ``.py`` citado en «Verificado por» puede ser un archivo inexistente.

    Una fila que se apoya en un test borrado sigue diciendo «Cerrado» y ya no la
    respalda nada: el inventario pasa de consolidar estado a inventarlo, que es
    exactamente lo que este documento existe para no hacer.

    Pasó en #415: la fila de lifecycle citaba ``test_lifecycle_de_la_sesion.py``,
    que el propio PR eliminó al cambiar de un hook defensivo al fix de raíz. Los
    gates de Python siguieron verdes —ninguno lee este documento— y lo atajó un
    revisor automático. Este test lo convierte en rojo.

    Se enumeran **todas** las filas en vez de revisar la que se acaba de tocar:
    un caso escrito a mano para la fila de hoy no ataja a la de mañana.
    """
    citados = {nombre for fila in _tabla().values() for nombre in re.findall(r"[\w/]+\.py", fila["Verificado por"])}
    faltantes = {nombre for nombre in citados if not _ruta_del_test_citado(nombre).is_file()}

    assert not faltantes, f"el inventario cita tests que no existen: {sorted(faltantes)}"


# Co-ocurrencia de "rig" y una fecha dentro de una ventana corta, **en cualquiera
# de los dos órdenes**: cubre `rig 2026-08-10`, `rig del …`, `rig, en la corrida
# del …` y también `el 2026-08-10 el rig midió …`, sin enumerar conectores a mano.
# La ventana es corta a propósito: es lo único que evita emparejar un "rig humano"
# con una fecha que vive en otra parte de la misma celda.
#
# `IGNORECASE` porque "Rig" con mayúscula inicial es lo natural al empezar una
# celda, y sin el flag esa fila no disparaba nada: evidencia de rig externa sin
# declarar, con la suite en verde. Los negativos no se alteran — no llevan fecha.
_CITA_DE_RIG_FECHADO = re.compile(
    r"\brig\b.{0,25}?\d{4}-\d{2}-\d{2}|\d{4}-\d{2}-\d{2}.{0,25}?\brig\b",
    re.IGNORECASE,
)


def _seccion_29_del_sop() -> str:
    """El texto de §2.9 y nada más.

    Leer el archivo entero deja el ancla afirmando menos de lo que su nombre
    promete: si §2.9 pierde la declaración pero la misma frase aparece en otra
    sección, el test sigue verde y el drift pasa. Se recorta desde el encabezado
    de §2.9 hasta el siguiente de nivel igual o mayor.
    """
    texto = _SOP.read_text(encoding="utf-8")
    inicio = texto.index("### 2.9 TexGen & DynDOLOD 3")
    # Se busca DESPUÉS de la línea del propio encabezado: arrancando en `inicio`,
    # el primer match del patrón es ese mismo encabezado y la sección sale vacía.
    cuerpo = texto.index("\n", inicio) + 1
    siguiente = re.search(r"^#{1,3} ", texto[cuerpo:], re.MULTILINE)
    return texto[inicio:] if siguiente is None else texto[inicio : cuerpo + siguiente.start()]


_MARCA_DE_EVIDENCIA_EXTERNA = "fuera del repo"

# Las filas que se apoyan en un informe de rig que NO está en el árbol. Igualdad
# literal en las dos direcciones: quitarle la marca a una de estas rompe el test
# aunque el patrón no la detecte, y una fila nueva que el patrón SÍ detecte rompe
# hasta que se la declare acá o se la reformule.
#
# Este conjunto es el gate; el patrón es la red. La distinción importa porque la
# propiedad "esta fila afirma un hecho de rig" NO es decidible por regex sobre
# prosa: en el PR #485 el patrón se corrigió tres veces —formato canónico, luego
# falsos positivos cruzando celdas, luego orden inverso— y cada vuelta movió el
# hueco en vez de cerrarlo. Con el conjunto congelado, el caso conocido queda
# anclado sin depender de cómo esté redactado.
_FILAS_CON_EVIDENCIA_DE_RIG_EXTERNA = frozenset(
    {
        "U-06",
        "Preset de TexGen desvía `OutputPath`",
        "Clasificación del log de la etapa 9",
        "Candidato de salida de TexGen",
    }
)


def test_el_patron_de_cita_de_rig_reconoce_las_variantes_que_se_escriben() -> None:
    """La red vale lo que valga su patrón, y su límite tiene que estar escrito.

    Un oráculo que solo reconoce el formato canónico deja el agujero justo donde
    importa: la fila futura que escriba la fecha con otro conector pasa sin marca y
    nadie se entera. Se enumeran las variantes que SÍ deben disparar y las frases
    de trabajo pendiente que NO, porque las dos mitades son el contrato.
    """
    afirman_un_hecho = (
        "rig 2026-08-10",
        "rig T5 2026-08-11",
        "rig del 2026-08-10",
        "rig, 2026-08-10",
        "rig de 2026-08-10",
        "rig, en la corrida del 2026-08-10, midió",
        "el 2026-08-10 el rig midió que DynDOLOD persiste el .esp",
        "Rig 2026-08-10 midió que DynDOLOD persiste el .esp",
    )
    for cita in afirman_un_hecho:
        assert _CITA_DE_RIG_FECHADO.search(cita), f"debería exigir la marca: {cita!r}"

    trabajo_pendiente = (
        "Bloqueado (rig humano)",
        "Verificar contra rig real qué severidad usa Pandora",
        "humano",
        # `rig` como SUBSTRING no es una cita de rig. Sin `\b`, "trigger" la
        # dispara y una fila que hable de triggers con fecha queda obligada a
        # declarar evidencia externa que no tiene.
        "trigger 2026-08-05",
        "2026-08-05 trigger",
    )
    for frase in trabajo_pendiente:
        assert not _CITA_DE_RIG_FECHADO.search(frase), f"no afirma un hecho de rig: {frase!r}"


def test_el_sop_declara_que_su_evidencia_de_rig_es_externa() -> None:
    """La misma regla que el inventario, en el archivo que los agentes SÍ cargan.

    `sky_claw/local/AGENTS.md` §2.9 apoya un **blocker de merge** en corridas de
    rig cuyos informes viven en la máquina del operador. Sin declararlo, un agente
    —o un maintainer— lee «Rig 2026-08-11 measured …» como hecho establecido y no
    tiene cómo auditarlo desde el árbol.

    Pasó en el PR #485, y de la peor forma: ese PR introdujo la regla, la cableó
    sobre la tabla del inventario, y en el MISMO diff escribió una cita fechada
    nueva en el SOP sin la marca. La regla propia, violada por su propio autor, en
    el commit que la creaba. Lo atajó el revisor Regression & Test Oracle.

    Se enumeran las fechas: una cita de rig con una fecha no declarada rompe el
    test hasta que se la agregue a la declaración de la sección. `_tabla()` no
    cubre este archivo —solo lee el inventario—, así que sin este ancla el SOP no
    tenía ningún gate equivalente.
    """
    texto = _seccion_29_del_sop()

    assert "Rig evidence in this section is EXTERNAL and not auditable from this repo." in texto, (
        "§2.9 dejó de declarar que su evidencia de rig es externa"
    )

    # `\brig\b` en las dos: sin los límites, «trigger 2026-08-05» se lee como cita
    # de rig y el ancla exige declarar una evidencia que no existe.
    citadas = {m.group(1) for m in re.finditer(r"\brig\b[^.\n]{0,25}?(\d{4}-\d{2}-\d{2})", texto, re.IGNORECASE)}
    citadas |= {m.group(1) for m in re.finditer(r"(\d{4}-\d{2}-\d{2})[^.\n]{0,25}?\brig\b", texto, re.IGNORECASE)}
    declaradas = {"2026-08-05", "2026-08-10", "2026-08-11"}

    assert citadas == declaradas, (
        f"el SOP cita corridas de rig fechadas que su declaración no cubre: {sorted(citadas - declaradas)}; "
        f"o declara fechas que ya no cita: {sorted(declaradas - citadas)}"
    )


def test_la_cita_de_rig_no_se_arma_cruzando_dos_celdas() -> None:
    """Dos celdas vecinas no pueden fabricar una cita que ninguna contiene.

    Uniendo las celdas con un espacio —como hacía la primera versión— un «rig» al
    final de una columna y una fecha al principio de la siguiente caen dentro de la
    ventana del patrón y producen un match espurio: la fila termina obligada a
    declarar evidencia externa que nunca citó (revisor adversarial, PR #485).
    """
    celdas = ("Bloqueado por rig", "2026-09-01 queda pendiente")

    assert not any(_CITA_DE_RIG_FECHADO.search(celda) for celda in celdas)
    assert _CITA_DE_RIG_FECHADO.search(" ".join(celdas)), (
        "si unir las celdas dejara de producir el match, este test ya no estaría "
        "probando nada y habría que revisar el patrón"
    )


def test_toda_fila_que_afirma_un_hecho_de_rig_declara_que_la_evidencia_es_externa() -> None:
    """Una corrida de rig fechada respalda una AFIRMACIÓN, y su informe no está en el árbol.

    Los informes viven en la máquina del rig operador (hallazgo del revisor Codex
    en el PR #463): un maintainer no puede auditarlos ni reproducirlos desde este
    repo. Una fila que cita una corrida fechada sin decirlo convierte el inventario
    en fuente de hechos que nadie puede verificar — justo lo que este documento
    existe para no hacer.

    Pasó en el PR #485: las dos filas nuevas de la etapa 9 llevaban la marca y
    `U-06` —que en el MISMO diff pasó a afirmar que el rig había cerrado su
    incógnita— quedó sin ella. El defecto hermano de `AGENTS.md` dentro de una sola
    tabla, y lo atajó un revisor automático.

    Se enumeran TODAS las filas: un caso escrito a mano para la que se acaba de
    tocar no ataja a la próxima. Las filas que nombran trabajo de rig PENDIENTE
    («rig humano», «rig real», sin fecha) no afirman ningún hecho y quedan fuera
    por construcción del patrón, no por una excepción escrita a mano.

    La cita se busca **celda por celda**, nunca sobre las celdas unidas: uniéndolas,
    un «rig» al final de una columna y una fecha al principio de la siguiente caían
    dentro de la ventana del patrón y disparaban un match que no existe en ninguna
    de las dos. La marca sí se acepta en cualquier celda de la fila, porque hoy
    unas la llevan en «Qué falta» y otras en «Verificado por».

    LIMITACIÓN CONOCIDA del detector, y es deliberado no taparla con vocabulario:
    el patrón reconoce la CO-OCURRENCIA de «rig» y una fecha, no distingue afirmar
    de planificar. Una fila futura que diga «correr el rig el 2026-09-01» —un plan,
    sin hecho afirmado— va a pedir la marca sin corresponderle. La respuesta
    correcta ahí es **reformular la fila**, no agregarle un «fuera del repo» que
    sería falso: no hay informe todavía. Se descartó discriminar por verbo de
    resultado («midió»/«confirmó»/…) porque es la misma trampa que este patrón ya
    tuvo una vez: una lista cerrada de conectores dejó pasar «rig del 2026-08-10»
    (PR #485), y una lista cerrada de verbos dejaría pasar «según el rig» igual.

    Por eso el patrón NO es el gate: lo es
    `_FILAS_CON_EVIDENCIA_DE_RIG_EXTERNA`, que se afirma en las dos direcciones.
    Una fila declarada tiene que llevar la marca aunque el patrón no la vea, y una
    fila que el patrón vea tiene que estar declarada. Lo que queda afuera —una fila
    nueva, con evidencia de rig, redactada de una forma que el patrón no reconozca y
    sin declarar— es el límite irreducible de medir esto sobre prosa, y está acá
    escrito en vez de disimulado detrás de una regex cada vez más larga.
    """
    filas = _tabla()

    declaradas_sin_marca = sorted(
        item
        for item in _FILAS_CON_EVIDENCIA_DE_RIG_EXTERNA
        if not any(_MARCA_DE_EVIDENCIA_EXTERNA in celda for celda in filas[item].values())
    )
    assert not declaradas_sin_marca, (
        f"estas filas se apoyan en un informe de rig fuera del repo y dejaron de declararlo: {declaradas_sin_marca}"
    )

    detectadas_sin_declarar = sorted(
        item
        for item, fila in filas.items()
        if item not in _FILAS_CON_EVIDENCIA_DE_RIG_EXTERNA
        and any(_CITA_DE_RIG_FECHADO.search(celda) for celda in fila.values())
    )
    assert not detectadas_sin_declarar, (
        "estas filas citan una corrida de rig fechada y no están declaradas en "
        f"_FILAS_CON_EVIDENCIA_DE_RIG_EXTERNA: {detectadas_sin_declarar}. Si afirman un hecho "
        "medido, declaralas y poneles la marca; si solo planifican una corrida, reformulalas."
    )


def test_los_smokes_sin_ancla_automatizable_declaran_verificacion_humana() -> None:
    filas = _tabla()

    for item in ("Smoke real de QuickAutoClean", "Smokes reales restantes"):
        assert filas[item]["Estado"] == "Bloqueado (rig humano)"
        assert filas[item]["Verificado por"] == "humano"


def test_medicion_de_arboles_se_apoya_en_el_censo_de_medidores() -> None:
    """La fila sólo cierra con un censo poblado y de vocabulario cerrado.

    El docstring anterior decía que la fila «no puede decir Cerrado con un
    medidor sin clasificar», y la aserción no verificaba eso: un medidor sin
    clasificar no está en el dict, así que no aporta ningún valor y la
    comprobación de vocabulario seguía verde. Afirmaba más de lo que probaba,
    que es el defecto que este archivo persigue.

    La cobertura —que ningún medidor quede afuera— la verifica
    ``test_borrado_recursivo.py::test_la_enumeracion_cubre_a_todo_el_que_mide_arboles``,
    que es a lo que apunta la celda «Verificado por». Acá se ata lo que sí es
    comprobable desde este archivo: que el censo no esté vacío y que todo lo
    declarado ``"sin-contraparte-que-borre"`` efectivamente no borre.
    """
    fila = _tabla()["Medición de árboles"]
    eximidos = {m for m, mecanismo in MECANISMO_DE_MEDICION.items() if mecanismo == "sin-contraparte-que-borre"}

    assert MECANISMO_DE_MEDICION
    assert set(MECANISMO_DE_MEDICION.values()) <= {"link-aware", "sin-contraparte-que-borre"}
    assert not (eximidos & set(MECANISMO_DE_BORRADO))
    assert fila["Estado"] == "Cerrado"
    assert "test_borrado_recursivo.py" in fila["Verificado por"]
