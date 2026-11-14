"""B-1 (H-5/H-17): el bridge declara la IDENTIDAD de su bundle y el broker rechaza uno distinto.

Antes, el ``hello`` del bridge solo llevaba ``protocol_version``, que NO cambió con #856, #860 ni #877: Sky-Claw se
actualizaba, el plugin instalado en MO2 quedaba viejo y se conectaba igual, con el bug viejo vivo y sin aviso. Ahora el
bridge manda el SHA-256 de TODOS los archivos de su bundle, calculado UNA vez al cargar, y el broker lo compara con el
del bundle que trae esta versión de Sky-Claw; si no coincide, rechaza con la instrucción de reinstalar y reabrir MO2.
"""

from __future__ import annotations

import ast
import json
import pathlib
import shutil

import pytest

from sky_claw.local.mo2 import bridge_installer
from sky_claw.local.mo2.bridge_bundle import RUTA_DEL_BUNDLE, digest_del_bundle_del_producto
from sky_claw.local.mo2.bridge_installer import MO2BridgeInstaller, MO2BridgeInstallError
from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.protocol import bundle_digest
from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.runtime import PROTOCOL_VERSION, bridge_hello
from sky_claw.local.mo2.vfs_broker import VfsBrokerError, VfsExecutionBroker

RAIZ = pathlib.Path(__file__).resolve().parents[1]
_IGNORADOS = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")


def _archivos_del_bundle() -> list[str]:
    """Todos los archivos reales del bundle, detectados (no escritos a mano): uno nuevo entra solo a la parametrización."""
    return sorted(
        ruta.relative_to(RUTA_DEL_BUNDLE).as_posix()
        for ruta in RUTA_DEL_BUNDLE.rglob("*")
        if ruta.is_file() and "__pycache__" not in ruta.parts and ruta.suffix not in {".pyc", ".pyo"}
    )


def _bundle_de_juguete(destino: pathlib.Path, archivos: dict[str, bytes]) -> pathlib.Path:
    for relativa, contenido in archivos.items():
        ruta = destino / relativa
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(contenido)
    return destino


# --------------------------------------------------------------------------------------------------- el digest


def test_el_digest_no_depende_del_orden_de_creacion_ni_de_lo_generado(tmp_path: pathlib.Path) -> None:
    # Arrange: el mismo contenido creado en dos órdenes distintos
    a = _bundle_de_juguete(tmp_path / "a", {"plugin.py": b"x", "sub/otro.py": b"y", "__init__.py": b""})
    b = _bundle_de_juguete(tmp_path / "b", {"__init__.py": b"", "sub/otro.py": b"y", "plugin.py": b"x"})
    base = bundle_digest(a)

    # Act: lo que NO es del bundle (caché, bytecode y la config por instancia) se agrega solo en `a`
    _bundle_de_juguete(
        a,
        {
            "__pycache__/plugin.cpython-312.pyc": b"cache",
            "sub/__pycache__/x.pyc": b"cache",
            "viejo.pyo": b"bytecode",
            "bridge_config.json": b'{"instance_id": "portable-main"}',
        },
    )

    # Assert
    assert bundle_digest(a) == base == bundle_digest(b)
    assert len(base) == 64 and set(base) <= set("0123456789abcdef")


def test_cambiar_agregar_o_renombrar_un_archivo_cambia_el_digest(tmp_path: pathlib.Path) -> None:
    base = bundle_digest(_bundle_de_juguete(tmp_path / "base", {"plugin.py": b"x", "protocol.py": b"y"}))

    cambiado = bundle_digest(_bundle_de_juguete(tmp_path / "c", {"plugin.py": b"x2", "protocol.py": b"y"}))
    agregado = bundle_digest(
        _bundle_de_juguete(tmp_path / "d", {"plugin.py": b"x", "protocol.py": b"y", "nuevo.py": b""})
    )
    renombrado = bundle_digest(_bundle_de_juguete(tmp_path / "e", {"plugin.py": b"x", "protocolo.py": b"y"}))
    # el contenido de un archivo no puede «pasar» a otro nombre: mismo conjunto de bytes, distinta asignación
    intercambiado = bundle_digest(_bundle_de_juguete(tmp_path / "f", {"plugin.py": b"y", "protocol.py": b"x"}))

    assert len({base, cambiado, agregado, renombrado, intercambiado}) == 5


def test_renombrar_un_archivo_a_un_nombre_del_mismo_largo_cambia_el_digest(tmp_path: pathlib.Path) -> None:
    """Mismo contenido, mismo largo de nombre, mismo orden: el único dato que cambia es el nombre mismo."""
    a = bundle_digest(_bundle_de_juguete(tmp_path / "a", {"aa.py": b"x", "zz.py": b"y"}))
    b = bundle_digest(_bundle_de_juguete(tmp_path / "b", {"ab.py": b"x", "zz.py": b"y"}))

    assert a != b


def test_el_bundle_real_tiene_los_archivos_esperados_y_el_ancla_no_queda_vacia() -> None:
    archivos = _archivos_del_bundle()

    assert {"__init__.py", "plugin.py", "protocol.py", "runtime.py"} <= set(archivos)


@pytest.mark.parametrize("relativa", _archivos_del_bundle())
def test_cada_archivo_del_bundle_real_participa_del_digest(relativa: str, tmp_path: pathlib.Path) -> None:
    """Enumera la familia: tocar CUALQUIER archivo del bundle cambia el digest (un archivo nuevo entra solo)."""
    # Arrange
    copia = tmp_path / "bundle"
    shutil.copytree(RUTA_DEL_BUNDLE, copia, ignore=_IGNORADOS)
    original = bundle_digest(copia)
    assert original == digest_del_bundle_del_producto()

    # Act
    with (copia / relativa).open("ab") as archivo:
        archivo.write(b"\n# alterado\n")

    # Assert
    assert bundle_digest(copia) != original


def test_un_enlace_dentro_del_bundle_es_un_error_y_no_se_sigue(tmp_path: pathlib.Path) -> None:
    bundle = _bundle_de_juguete(tmp_path / "b", {"plugin.py": b"x"})
    try:
        (bundle / "enlace.py").symlink_to(bundle / "plugin.py")
    except (OSError, NotImplementedError):
        pytest.skip("este sistema no permite crear symlinks")

    with pytest.raises(ValueError, match="enlace"):
        bundle_digest(bundle)


# ------------------------------------------------------------------------------------------------- el instalador


def _instalar(tmp_path: pathlib.Path) -> tuple[MO2BridgeInstaller, dict[str, object]]:
    mo2 = tmp_path / "MO2"
    (mo2 / "plugins").mkdir(parents=True)
    (mo2 / "ModOrganizer.exe").write_bytes(b"mo2")
    worker = tmp_path / "SkyClawApp.exe"
    worker.write_bytes(b"worker")
    argumentos: dict[str, object] = {
        "mo2_root": mo2,
        "worker_executable": worker,
        "worker_prefix": ("--vfs-worker",),
        "descriptor_path": tmp_path / "state" / "portable-main.json",
        "instance_id": "portable-main",
    }
    return MO2BridgeInstaller(hardener=lambda _path: None), argumentos


def test_el_instalador_registra_el_digest_y_lo_instalado_coincide_con_el_del_producto(tmp_path: pathlib.Path) -> None:
    instalador, argumentos = _instalar(tmp_path)

    instalado = instalador.install(**argumentos)  # type: ignore[arg-type]

    config = json.loads((instalado / "bridge_config.json").read_text(encoding="utf-8"))
    assert config["bundle_digest"] == digest_del_bundle_del_producto()
    # La config por instancia NO entra al digest: lo instalado hashea igual que la fuente.
    assert bundle_digest(instalado) == digest_del_bundle_del_producto()


def test_una_copia_que_no_coincide_con_la_fuente_aborta_la_instalacion_sin_dejar_nada(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instalador, argumentos = _instalar(tmp_path)
    copytree_real = shutil.copytree

    def copytree_que_corrompe(origen, destino, *args, **kwargs):
        resultado = copytree_real(origen, destino, *args, **kwargs)
        with (pathlib.Path(destino) / "plugin.py").open("ab") as archivo:
            archivo.write(b"\n# corrupto\n")
        return resultado

    monkeypatch.setattr(bridge_installer.shutil, "copytree", copytree_que_corrompe)

    with pytest.raises(MO2BridgeInstallError, match="copia"):
        instalador.install(**argumentos)  # type: ignore[arg-type]

    assert not (argumentos["mo2_root"] / "plugins" / "skyclaw_bridge").exists()  # type: ignore[operator]


# ---------------------------------------------------------------------------------------------- el hello y el plugin


def test_el_hello_del_bridge_declara_el_digest() -> None:
    hello = bridge_hello(instance_id="portable-main", session_id="s-1", bundle_digest="ab" * 32)

    assert hello == {
        "protocol_version": PROTOCOL_VERSION,
        "type": "hello",
        "role": "bridge",
        "instance_id": "portable-main",
        "session_id": "s-1",
        "bundle_digest": "ab" * 32,
    }


def _arbol(ruta: pathlib.Path) -> ast.AST:
    return ast.parse(ruta.read_text(encoding="utf-8"))


def _produccion() -> list[pathlib.Path]:
    return sorted((RAIZ / "sky_claw").rglob("*.py"))


def test_el_unico_hello_de_bridge_declara_el_digest_del_bundle() -> None:
    """Hermano de ``test_el_unico_hello_de_worker_declara_la_capacidad_de_fence``: mismo literal, mismo contrato."""
    literales: list[tuple[str, set[str]]] = []
    for ruta in _produccion():
        for nodo in ast.walk(_arbol(ruta)):
            if not isinstance(nodo, ast.Dict):
                continue
            pares = {
                clave.value: valor.value
                for clave, valor in zip(nodo.keys, nodo.values, strict=True)
                if isinstance(clave, ast.Constant) and isinstance(valor, ast.Constant)
            }
            if pares.get("role") == "bridge" and pares.get("type") == "hello":
                claves = {k.value for k in nodo.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                literales.append((ruta.relative_to(RAIZ).as_posix(), claves))

    assert [archivo for archivo, _ in literales] == ["sky_claw/local/mo2/plugin_bundle/skyclaw_bridge/runtime.py"]
    assert "bundle_digest" in literales[0][1]


def test_el_plugin_calcula_el_digest_una_sola_vez_al_cargar_y_no_en_cada_hello() -> None:
    """Un reinstall con MO2 abierto cambia los archivos en disco pero el código CARGADO sigue siendo el viejo.

    Recalcular en cada ``hello`` afirmaría el digest del código nuevo sobre el viejo en ejecución: un falso «ok».
    """
    arbol = _arbol(RUTA_DEL_BUNDLE / "plugin.py")
    llamadas_a_digest = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "bundle_digest"
    ]
    a_nivel_de_modulo = [
        nodo
        for nodo in arbol.body  # type: ignore[attr-defined]
        if isinstance(nodo, ast.Assign)
        and any(isinstance(n, ast.Call) for n in ast.walk(nodo.value))
        and any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "bundle_digest"
            for n in ast.walk(nodo.value)
        )
    ]
    llama_a_bridge_hello = any(
        isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "bridge_hello"
        for nodo in ast.walk(arbol)
    )

    assert len(llamadas_a_digest) == 1, "el digest se calcula en UN solo sitio"
    assert len(a_nivel_de_modulo) == 1, "y ese sitio es el nivel de módulo (al cargar), no un método"
    assert llama_a_bridge_hello, "el plugin arma su hello con la función única, no con un literal propio"


# ------------------------------------------------------------------------------------------------------- el broker

_INSTRUCCION = "install-vfs-bridge"


def _broker(tmp_path: pathlib.Path, **kwargs: object) -> VfsExecutionBroker:
    return VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        descriptor_hardener=lambda _path: None,
        **kwargs,  # type: ignore[arg-type]
    )


def _hello_de_bridge(broker: VfsExecutionBroker, **cambios: object) -> dict[str, object]:
    hello: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "type": "hello",
        "role": "bridge",
        "instance_id": "portable-main",
        "session_id": broker._session_id,
        "bundle_digest": digest_del_bundle_del_producto(),
    }
    hello.update(cambios)
    for clave in [k for k, v in cambios.items() if v is _AUSENTE]:
        del hello[clave]
    return hello


_AUSENTE = object()


def test_el_broker_acepta_un_bridge_con_el_digest_del_bundle_del_producto(tmp_path: pathlib.Path) -> None:
    broker = _broker(tmp_path)

    assert broker._validate_hello(_hello_de_bridge(broker)) == ("bridge", None)


@pytest.mark.parametrize(
    "digest",
    [_AUSENTE, "", "0" * 64, 12345, None, ["a"]],
    ids=["ausente(bridge viejo)", "vacio", "otro_digest", "no_es_str", "null", "lista"],
)
def test_el_broker_rechaza_un_bridge_sin_el_digest_o_con_otro_y_dice_como_arreglarlo(
    digest: object, tmp_path: pathlib.Path
) -> None:
    broker = _broker(tmp_path)

    with pytest.raises(VfsBrokerError) as error:
        broker._validate_hello(_hello_de_bridge(broker, bundle_digest=digest))

    mensaje = str(error.value)
    assert _INSTRUCCION in mensaje and "MO2" in mensaje and "reabr" in mensaje


def test_el_digest_esperado_se_puede_inyectar_y_manda_sobre_el_del_producto(tmp_path: pathlib.Path) -> None:
    broker = _broker(tmp_path, expected_bundle_digest="f" * 64)

    assert broker._validate_hello(_hello_de_bridge(broker, bundle_digest="f" * 64)) == ("bridge", None)
    with pytest.raises(VfsBrokerError):
        broker._validate_hello(_hello_de_bridge(broker))  # el del producto ya no es el esperado


async def test_quien_espera_al_bridge_ve_la_razon_del_rechazo_y_un_bridge_bueno_la_limpia(
    tmp_path: pathlib.Path,
) -> None:
    broker = _broker(tmp_path)

    with pytest.raises(VfsBrokerError):
        broker._validate_hello(_hello_de_bridge(broker, bundle_digest=_AUSENTE))
    with pytest.raises(VfsBrokerError) as error:
        await broker.wait_until_ready(timeout=0.01)
    assert "no se conectó" in str(error.value) and _INSTRUCCION in str(error.value)

    broker._validate_hello(_hello_de_bridge(broker))
    with pytest.raises(VfsBrokerError) as error:
        await broker.wait_until_ready(timeout=0.01)
    assert _INSTRUCCION not in str(error.value)


def test_el_hello_del_worker_no_pide_el_digest_del_bundle(tmp_path: pathlib.Path) -> None:
    """El digest es del BRIDGE (el plugin dentro de MO2); el worker es el mismo ejecutable que el daemon."""
    broker = _broker(tmp_path)
    # Un worker sin job conocido se rechaza por SU motivo, no por el digest.
    hello_de_worker = {
        "protocol_version": PROTOCOL_VERSION,
        "type": "hello",
        "role": "worker",
        "instance_id": "portable-main",
        "session_id": broker._session_id,
        "supervision_fence": True,
        "job_id": "job-desconocido",
        "job_object": "x",
    }

    with pytest.raises(VfsBrokerError) as error:
        broker._validate_hello(hello_de_worker)

    assert _INSTRUCCION not in str(error.value)
