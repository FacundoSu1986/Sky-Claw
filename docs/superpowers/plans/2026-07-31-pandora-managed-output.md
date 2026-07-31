# Pandora Managed Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hacer que toda ejecución de Pandora escriba únicamente en
`<game_path resuelto>/Pandora_Output`, con rollback y reconciliación sobre ese
mismo árbol y sin bypass desde el agente LLM.

**Architecture:** `output_targets.pandora_output_target` será la única fuente de
la ruta administrada. `PandoraRunner` la incluirá explícitamente en argv; el
servicio reutilizará esa ruta para permisos, manifiesto y `DirectoryRollback`;
el reconciliador barrerá solo su padre. Los caminos GUI y LLM seguirán
convergiendo en `PandoraPipelineService`, y el LLM fallará cerrado si faltan los
managers transaccionales.

**Tech Stack:** Python 3.11+, `asyncio`, `pathlib`, pytest, `unittest.mock`, Ruff,
mypy, markdownlint-cli2.

---

## Mapa de archivos

- Crear `tests/test_pandora_runner.py`: contrato ejecutable del argv y del `cwd`.
- Modificar `sky_claw/local/tools/output_targets.py`: reemplazar las candidatas
  ambiguas por el único resolver `pandora_output_target`.
- Modificar `sky_claw/local/tools/pandora_runner.py`: exponer `output_path` y
  pasar `--output` exactamente una vez.
- Modificar `tests/test_output_targets.py`: anclar que Pandora tiene un solo
  destino absoluto y que el servicio consume esa misma fuente.
- Modificar `sky_claw/local/tools/pandora_service.py`: unificar preflight,
  manifiesto y rollback sobre la salida administrada.
- Modificar `tests/test_pandora_service.py`: cubrir éxito, primer run, non-zero,
  timeout, cancelación, restore fallido y ausencia de mutación colateral.
- Modificar `sky_claw/app/agent/tools/system_tools.py`: retirar el fallback
  directo de Pandora cuando faltan managers.
- Modificar `tests/test_pandora_agent_gate.py`: anclar el fail-closed LLM y el
  uso del mismo servicio que la GUI.
- Modificar `sky_claw/local/tools/rollback_reconciler.py`: reconciliar Pandora
  solo bajo la raíz del juego administrada.
- Modificar `sky_claw/app_context.py`: dejar de pasar el ejecutable al
  constructor del productor de Pandora.
- Modificar `tests/test_rollback_reconciler.py`: exigir una única raíz de
  Pandora y conservar las anclas exhaustivas existentes.
- Modificar `docs/pending_ooda_status.md` y `tests/test_estado_ooda.py`: registrar
  el avance sin cerrar U-04, T-27 ni T-25.

### Task 1: Fijar el destino único en el resolver y el runner

**Files:**

- Create: `tests/test_pandora_runner.py`
- Modify: `tests/test_output_targets.py:29-36,146-263`
- Modify: `sky_claw/local/tools/output_targets.py:53-153`
- Modify: `sky_claw/local/tools/pandora_runner.py:13-88`

- [ ] **Step 1: Escribir los tests rojos del resolver y del comando**

Crear `tests/test_pandora_runner.py` con este contenido:

```python
from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, patch

from sky_claw.local.tools.pandora_runner import PandoraConfig, PandoraRunner


async def test_pandora_pasa_una_sola_salida_absoluta_y_preserva_espacios(
    tmp_path: pathlib.Path,
) -> None:
    game = tmp_path / "Skyrim Special Edition"
    game.mkdir()
    exe = tmp_path / "Pandora Tool" / "Pandora Behaviour Engine+.exe"
    exe.parent.mkdir()
    runner = PandoraRunner(PandoraConfig(pandora_exe=exe, game_path=game))

    capture = AsyncMock(return_value=(b"ok", b"", 0))
    with patch("sky_claw.local.tools.pandora_runner.run_capture", capture):
        result = await runner.run_pandora()

    args = capture.await_args.args[0]
    assert result.success is True
    assert args.count("--output") == 1
    output_index = args.index("--output") + 1
    assert args[output_index] == str(game.resolve() / "Pandora_Output")
    assert pathlib.Path(args[output_index]).is_absolute()
    assert capture.await_args.kwargs["cwd"] == str(game.resolve())


def test_output_path_del_runner_es_el_destino_administrado(
    tmp_path: pathlib.Path,
) -> None:
    game = tmp_path / "game" / ".." / "Skyrim"
    runner = PandoraRunner(
        PandoraConfig(
            pandora_exe=tmp_path / "Pandora.exe",
            game_path=game,
        )
    )

    assert runner.output_path == game.resolve() / "Pandora_Output"
```

En `tests/test_output_targets.py`, importar `pandora_output_target` en lugar de
`pandora_output_candidates` y `pandora_rollback_dirs`, y sustituir los tests
plurales de Pandora por:

```python
def test_pandora_tiene_un_unico_destino_administrado(tmp_path: pathlib.Path) -> None:
    game = tmp_path / "game" / ".." / "Skyrim Special Edition"

    assert pandora_output_target(game=game) == game.resolve() / "Pandora_Output"
    assert pandora_output_target(game=None) is None


def test_pandora_service_usa_el_destino_administrado(tmp_path: pathlib.Path) -> None:
    game = tmp_path / "game"
    exe = tmp_path / "pandora" / "Pandora.exe"
    resolver = _resolver(game=game, mo2=tmp_path / "mo2")
    resolver.get_pandora_exe.return_value = exe
    svc = PandoraPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=resolver,
    )

    managed = game.resolve() / "Pandora_Output"
    assert svc._managed_output() == managed
    assert svc._rollback_output_dirs() == [managed]
    assert game / "Data" not in svc._rollback_output_dirs()
    assert exe.parent not in svc._rollback_output_dirs()
```

- [ ] **Step 2: Ejecutar los tests y verificar el rojo**

Run:

```powershell
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m pytest `
  tests/test_pandora_runner.py tests/test_output_targets.py -q `
  --basetemp E:\pytest-tmp\pandora-managed-output-red-1
```

Expected: FAIL de colección porque `pandora_output_target` y
`PandoraRunner.output_path` todavía no existen.

- [ ] **Step 3: Implementar el resolver único y el argv mínimo**

En `sky_claw/local/tools/output_targets.py`, conservar
`PANDORA_OUTPUT_DIR = "Pandora_Output"` y añadir:

```python
def pandora_output_target(*, game: pathlib.Path | None) -> pathlib.Path | None:
    """Destino físico único y administrado de Pandora.

    ``PandoraRunner`` pasa esta ruta absoluta mediante ``--output``. No se
    acepta un override externo porque este mismo árbol es el que el servicio
    mueve aparte y restaura transaccionalmente.
    """
    if game is None:
        return None
    return game.resolve() / PANDORA_OUTPUT_DIR
```

Conservar temporalmente `pandora_output_candidates` y `pandora_rollback_dirs`
durante esta tarea porque el servicio y el reconciliador todavía las importan.
La Task 4 las elimina una vez migrados todos los consumidores; así cada commit
permanece importable y testeable.

En `sky_claw/local/tools/pandora_runner.py`, importar el resolver y cambiar el
runner así:

```python
from sky_claw.local.tools.output_targets import pandora_output_target


class PandoraRunner:
    """Asynchronous runner for Pandora Behavior Engine."""

    def __init__(self, config: PandoraConfig):
        self.config = config

    @property
    def output_path(self) -> pathlib.Path:
        """Salida absoluta administrada por Sky-Claw."""
        output = pandora_output_target(game=self.config.game_path)
        assert output is not None
        return output

    async def run_pandora(self) -> PandoraResult:
        """Execute Pandora in auto mode with a managed absolute output."""
        logger.info("[M-02] Executing Pandora Behavior Engine...")
        start_time = time.monotonic()
        game_path = self.config.game_path.resolve()
        args = [
            str(self.config.pandora_exe),
            "--game",
            "Skyrim Special Edition",
            "--auto",
            "--output",
            str(self.output_path),
        ]

        try:
            stdout, stderr, return_code = await run_capture(
                args,
                timeout=self.config.timeout_seconds,
                cwd=str(game_path),
            )
        except TimeoutError as exc:
            logger.error("Pandora execution timed out.")
            raise PandoraTimeoutError(self.config.timeout_seconds) from exc
        except Exception as exc:
            logger.error("Pandora execution failed: %s", exc)
            detail = f"Failed to execute Pandora: {exc}"
            raise PandoraExecutionError(detail) from exc

        return PandoraResult(
            success=return_code == 0,
            return_code=return_code,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
            duration_seconds=time.monotonic() - start_time,
        )
```

- [ ] **Step 4: Ejecutar tests enfocados y verificar verde**

Run: el mismo comando del Step 2 con `red-1` reemplazado por `green-1`.

Expected: PASS en `test_pandora_runner.py` y en la sección Pandora de
`test_output_targets.py`; los helpers plurales transitorios mantienen importable
el resto del árbol hasta su retiro en la Task 4.

- [ ] **Step 5: Commit**

```powershell
git add tests/test_pandora_runner.py tests/test_output_targets.py `
  sky_claw/local/tools/output_targets.py sky_claw/local/tools/pandora_runner.py
git commit -m "feat: force managed Pandora output"
```

### Task 2: Unificar preflight, manifiesto y rollback del servicio

**Files:**

- Modify: `tests/test_pandora_service.py`
- Modify: `sky_claw/local/tools/pandora_service.py:1-291,395-445,489-661`

- [ ] **Step 1: Adaptar helpers de test al output administrado y escribir regresiones**

En los tests de rollback existentes, mover toda salida de
`exe.parent / PANDORA_OUTPUT_DIR` a `game.resolve() / PANDORA_OUTPUT_DIR`.
Actualizar los helpers para que ningún mock o test use una ruta implícita:

```python
def _runner_returning(
    game_path: pathlib.Path,
    result: PandoraResult | None = None,
) -> MagicMock:
    runner = MagicMock()
    runner.config = PandoraConfig(
        pandora_exe=game_path.parent / "Pandora.exe",
        game_path=game_path,
    )
    runner.run_pandora = AsyncMock(
        return_value=result
        or PandoraResult(
            success=True,
            return_code=0,
            stdout="ok",
            stderr="",
            duration_seconds=1.0,
        )
    )
    return runner


def _make_service(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    runner: MagicMock,
    game_path: pathlib.Path,
) -> PandoraPipelineService:
    runner.config = PandoraConfig(
        pandora_exe=game_path.parent / "Pandora.exe",
        game_path=game_path,
    )
    return PandoraPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        pandora_runner=runner,
    )
```

Añadir `tmp_path` a cada test que use esos helpers, crear
`game = tmp_path / "game"`, ejecutar `game.mkdir()` y pasar `game` al helper.
Aplicar la misma firma a `_svc_with_journal`: recibir `game_path`, asignar el
`PandoraConfig` al runner y no inyectar un `path_resolver=MagicMock()` ambiguo.

Añadir estas pruebas, reutilizando las fixtures `lock_manager` y
`snapshot_manager`:

```python
def test_preflight_sondea_output_existente_o_su_padre(
    tmp_path: pathlib.Path,
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    runner = MagicMock()
    runner.config = PandoraConfig(pandora_exe=tmp_path / "Pandora.exe", game_path=game)
    svc = _make_service(MagicMock(), MagicMock(), runner, game)
    managed = game.resolve() / "Pandora_Output"

    assert svc._permission_targets() == [game.resolve()]
    managed.mkdir()
    assert svc._permission_targets() == [managed]
    assert svc._rollback_output_dirs() == [managed]


async def test_primer_run_fallido_elimina_la_salida_parcial(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    managed = game.resolve() / "Pandora_Output"

    async def _run() -> PandoraResult:
        managed.mkdir()
        (managed / "partial.hkx").write_bytes(b"parcial")
        return PandoraResult(False, 1, "", "boom", 0.1)

    runner = MagicMock()
    runner.config = PandoraConfig(pandora_exe=tmp_path / "Pandora.exe", game_path=game)
    runner.run_pandora = AsyncMock(side_effect=_run)

    service = _make_service(lock_manager, snapshot_manager, runner, game)
    result = await service.generate_animations()

    assert result["success"] is False
    assert not managed.exists()


async def test_fallo_restaura_bytes_y_no_toca_data_ni_el_directorio_del_exe(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    game = tmp_path / "game"
    data = game / "Data"
    data.mkdir(parents=True)
    exe = tmp_path / "Pandora" / "Pandora.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"exe")
    (data / "canary.bin").write_bytes(b"data-original")
    managed = game.resolve() / "Pandora_Output"
    managed.mkdir()
    (managed / "behavior.hkx").write_bytes(b"behavior-original")

    async def _run() -> PandoraResult:
        (managed / "behavior.hkx").write_bytes(b"corrupto")
        return PandoraResult(False, 1, "", "boom", 0.1)

    runner = MagicMock()
    runner.config = PandoraConfig(pandora_exe=exe, game_path=game)
    runner.run_pandora = AsyncMock(side_effect=_run)

    service = _make_service(lock_manager, snapshot_manager, runner, game)
    result = await service.generate_animations()

    assert result["success"] is False
    assert (managed / "behavior.hkx").read_bytes() == b"behavior-original"
    assert (data / "canary.bin").read_bytes() == b"data-original"
    assert exe.read_bytes() == b"exe"
```

Actualizar el test de manifiesto para recibir `tmp_path`, crear `game`, pasarlo
a `_svc_with_journal`, extraer el objeto persistido y afirmar:

```python
manifest = mock_journal.persist_action_manifest.await_args.args[0]
assert manifest.files_touched == [str(game.resolve() / "Pandora_Output")]
```

Mantener las pruebas existentes de timeout, cancelación, pérdida de lease y
restore fallido; únicamente cambiar su salida fixture a la ruta administrada.

- [ ] **Step 2: Ejecutar el servicio y verificar el rojo**

Run:

```powershell
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m pytest `
  tests/test_pandora_service.py -q `
  --basetemp E:\pytest-tmp\pandora-managed-output-red-2
```

Expected: FAIL porque el servicio todavía usa candidatas plurales y el
manifiesto recibe targets de permisos en vez del output exacto.

- [ ] **Step 3: Implementar una única ruta dentro del servicio**

Cambiar el import a `pandora_output_target` y sustituir los dos resolvers del
servicio por:

```python
def _managed_output(self) -> pathlib.Path | None:
    """Única salida que runner, manifiesto y rollback comparten."""
    game, _, _, _, _ = self._resolve_pandora_paths()
    return pandora_output_target(game=game)

def _permission_targets(self) -> list[pathlib.Path]:
    """Destino escribible a comprobar antes de crear o reemplazar la salida."""
    output = self._managed_output()
    if output is None:
        return []
    return [output if output.exists() else output.parent]

def _rollback_output_dirs(self) -> list[pathlib.Path]:
    """Árbol regenerable que DirectoryRollback protege por move-aside."""
    output = self._managed_output()
    return [output] if output is not None else []
```

En `generate_animations`, calcular una vez el target exacto antes del lock y
usarlo para manifiesto y rollback:

```python
managed_output = self._managed_output()
if managed_output is None:
    detail = "Cannot run Pandora: SKYRIM_PATH is not configured."
    return _attach_preflight(
        {"status": "error", "success": False, "message": detail, "logs": detail},
        preflight_report,
    )
rollback_outputs = [managed_output]
```

Luego reemplazar:

```python
journal_tx_id = await self._emit_action_manifest(tx, [managed_output])
```

y:

```python
for output_dir in rollback_outputs:
    dr = DirectoryRollback(output_dir, should_rollback=lambda: not tx.lease_lost)
    await tx_stack.enter_async_context(dr)
    dir_rollbacks.append(dr)
```

Actualizar docstrings del módulo, `_ensure_preflight`, `_permission_targets` y
`_emit_action_manifest` para declarar el único `--output`; eliminar referencias
a `Data`, al directorio del ejecutable y a “candidatas”. No cambiar
`_dict_de_resultado`, el manejo de cancelación ni el cierre del journal.

- [ ] **Step 4: Ejecutar el servicio y verificar verde**

Run: el mismo comando del Step 2 con `red-2` reemplazado por `green-2`.

Expected: PASS, incluyendo éxito, non-zero, timeout, cancelación, primer run,
lease perdida y restore fallido.

- [ ] **Step 5: Commit**

```powershell
git add tests/test_pandora_service.py sky_claw/local/tools/pandora_service.py
git commit -m "fix: align Pandora safeguards on managed output"
```

### Task 3: Cerrar el bypass del agente LLM y anclar ambos entry points

**Files:**

- Modify: `tests/test_pandora_agent_gate.py:1-201`
- Modify: `sky_claw/app/agent/tools/system_tools.py:472-543`

- [ ] **Step 1: Reemplazar la expectativa legacy por fail-closed**

Sustituir `test_run_pandora_direct_path_preserved_without_lock` por:

```python
@pytest.mark.parametrize(
    ("lock_manager", "snapshot_manager", "faltante"),
    [
        (None, MagicMock(), "lock_manager"),
        (MagicMock(), None, "snapshot_manager"),
        (None, None, "lock_manager, snapshot_manager"),
    ],
)
async def test_run_pandora_falla_cerrado_si_falta_proteccion(
    lock_manager: object | None,
    snapshot_manager: object | None,
    faltante: str,
) -> None:
    runner = MagicMock()
    runner.run_pandora = AsyncMock(return_value=_runner_result())

    out = json.loads(
        await run_pandora(
            runner,
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
        )
    )

    assert out == {"error": f"Pandora requiere protección; faltan: {faltante}"}
    runner.run_pandora.assert_not_awaited()
```

Actualizar `test_run_pandora_agent_path_tambien_revierte_la_salida` para usar
`game.resolve() / PANDORA_OUTPUT_DIR`, no el directorio del ejecutable. Conservar
los tests de lock y journal: prueban que el LLM construye el mismo
`PandoraPipelineService` que usa `GenerateAnimationsStrategy` en la GUI.
Importar `PandoraConfig` junto a los imports productivos del módulo de test.

En cada test protegido, asignar al mock antes de llamar al tool:

```python
runner.config = PandoraConfig(
    pandora_exe=tmp_path / "Pandora" / "Pandora.exe",
    game_path=tmp_path / "game",
)
runner.config.game_path.mkdir(parents=True)
```

Añadir `tmp_path` a las firmas de esos tests. El caso fail-closed no necesita
config porque debe retornar antes de resolver o invocar el runner.

- [ ] **Step 2: Ejecutar y verificar el rojo**

Run:

```powershell
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m pytest `
  tests/test_pandora_agent_gate.py -q `
  --basetemp E:\pytest-tmp\pandora-managed-output-red-3
```

Expected: FAIL porque `run_pandora` todavía ejecuta el runner directamente sin
managers.

- [ ] **Step 3: Implementar el fail-closed antes de construir el servicio**

En `system_tools.run_pandora`, después del guard de runner, añadir:

```python
missing_managers = [
    name
    for name, manager in (
        ("lock_manager", lock_manager),
        ("snapshot_manager", snapshot_manager),
    )
    if manager is None
]
if missing_managers:
    return json.dumps(
        {"error": f"Pandora requiere protección; faltan: {', '.join(missing_managers)}"}
    )
```

Desindentar la construcción y ejecución de `PandoraPipelineService` para que sea
el único camino posterior. Eliminar por completo el `try` final que llama
`pandora_runner.run_pandora()` directamente. Actualizar la docstring para indicar
que todos los callers mutantes deben participar en lock y rollback.

- [ ] **Step 4: Ejecutar y verificar verde**

Run: el mismo comando del Step 2 con `red-3` reemplazado por `green-3`.

Expected: PASS; el runner no se invoca cuando falta uno o ambos managers y los
caminos protegidos conservan el JSON previo.

- [ ] **Step 5: Commit**

```powershell
git add tests/test_pandora_agent_gate.py sky_claw/app/agent/tools/system_tools.py
git commit -m "fix: fail closed Pandora agent runs"
```

### Task 4: Limitar el reconciliador a la raíz administrada

**Files:**

- Modify: `tests/test_rollback_reconciler.py:24-337`
- Modify: `sky_claw/local/tools/rollback_reconciler.py:46,205-249`
- Modify: `sky_claw/app_context.py:1038-1042`

- [ ] **Step 1: Escribir la expectativa de una única raíz**

Cambiar el test del constructor de Pandora por:

```python
def test_el_constructor_barre_solo_la_raiz_administrada_de_pandora(
    tmp_path: pathlib.Path,
) -> None:
    game = tmp_path / "game" / ".." / "Skyrim"

    productores = construir_productores_de_move_aside(mo2_root=None, game=game)

    pandora = next(p for p in productores if p.nombre == "pandora")
    assert pandora.lock_resource_id == BEHAVIOR_GRAPHS_RESOURCE_ID
    assert pandora.raices == (game.resolve(),)
```

Actualizar las llamadas a `construir_productores_de_move_aside` para retirar
`pandora_exe`. Mantener los tests de restauración, lock, idempotencia y symlinks.

- [ ] **Step 2: Ejecutar y verificar el rojo**

Run:

```powershell
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m pytest `
  tests/test_rollback_reconciler.py -q `
  --basetemp E:\pytest-tmp\pandora-managed-output-red-4
```

Expected: FAIL porque el constructor todavía requiere el ejecutable para añadir
su segunda raíz.

- [ ] **Step 3: Derivar el productor desde `pandora_output_target`**

En `rollback_reconciler.py`, importar `pandora_output_target`, retirar el
parámetro `pandora_exe` y reemplazar la construcción plural por:

```python
output_pandora = pandora_output_target(game=game)
if output_pandora is not None:
    productores.append(
        ProductorDeMoveAside(
            nombre="pandora",
            lock_resource_id=BEHAVIOR_GRAPHS_RESOURCE_ID,
            raices=(output_pandora.parent,),
        )
    )
```

En el mismo cambio, eliminar de `output_targets.py` las funciones transitorias
`pandora_output_candidates` y `pandora_rollback_dirs`, y retirar todas sus
importaciones. En `tests/test_output_targets.py` ya no debe quedar ningún test de
las candidatas históricas.

En `app_context.py`, dejar la invocación así:

```python
productores=construir_productores_de_move_aside(
    mo2_root=mo2_root,
    game=configured_game,
),
```

Actualizar los comentarios para declarar que el residuo de Pandora solo puede
estar junto a `<game>/Pandora_Output`.

- [ ] **Step 4: Ejecutar y verificar verde**

Run: el mismo comando del Step 2 con `red-4` reemplazado por `green-4`.

Expected: PASS; las anclas exhaustivas siguen detectando todos los usuarios de
`DirectoryRollback` y el productor Pandora tiene una sola raíz.

- [ ] **Step 5: Commit**

```powershell
git add tests/test_rollback_reconciler.py `
  tests/test_output_targets.py sky_claw/local/tools/output_targets.py `
  sky_claw/local/tools/rollback_reconciler.py sky_claw/app_context.py
git commit -m "fix: reconcile only managed Pandora output"
```

### Task 5: Actualizar el inventario OODA sin sobreafirmar cierre

**Files:**

- Modify: `tests/test_estado_ooda.py:11-13,91-104`
- Modify: `docs/pending_ooda_status.md:1-52,73-91`

- [ ] **Step 1: Escribir primero el contrato documental**

Cambiar el import a `pandora_output_target` y sustituir el test U-04 por:

```python
def test_u04_sigue_parcial_hasta_bodyslide_y_el_smoke_real_de_pandora() -> None:
    fila = _tabla()["U-04"]
    pendientes = {
        modulo
        for modulo, mecanismo in MECANISMO_DE_ROLLBACK.items()
        if mecanismo == "pendiente"
    }

    assert pendientes
    assert fila["Estado"] == "Parcial"
    assert "BodySlide" in fila["Qué falta"]
    assert "smoke real de Pandora" in fila["Qué falta"]
    assert "test_pandora_service.py" in fila["Verificado por"]
    assert pandora_output_target(game=pathlib.Path("juego")) == (
        pathlib.Path("juego").resolve() / "Pandora_Output"
    )
```

Añadir al test de T-27:

```python
fila = _tabla()["T-27"]
assert fila["Estado"] == "Parcial"
assert "USVFS" in fila["Qué falta"]
assert "Pandora" in fila["Qué falta"]
```

- [ ] **Step 2: Ejecutar y verificar el rojo documental**

Run:

```powershell
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m pytest `
  tests/test_estado_ooda.py -q `
  --basetemp E:\pytest-tmp\pandora-managed-output-red-5
```

Expected: FAIL porque el inventario todavía describe el modo `Data` como deuda
de código y no separa el smoke real del aislamiento USVFS.

- [ ] **Step 3: Actualizar las filas y la recomendación**

En `docs/pending_ooda_status.md`, establecer la última verificación en
`2026-07-31` sobre `origin/main` `f986ffe` y actualizar las celdas así:

- T-27 — Estado: `Parcial`.
- T-27 — Cerrado en: `ADR 0005 para Synthesis; salida administrada de Pandora`.
- T-27 — Qué falta: `Migrar Pandora, DynDOLOD y Wrye Bash al flujo aislado
  USVFS con diff y promoción`.
- T-27 — Verificado por: `código, ADR 0005 y test_pandora_service.py`.
- U-04 — Estado: `Parcial`.
- U-04 — Cerrado en: `#397, #399 y salida administrada de Pandora`.
- U-04 — Qué falta: `BodySlide y smoke real de rollback de Pandora`.
- U-04 — Verificado por: `test_rollback_salida.py; test_pandora_service.py;
  humano`.

En “Decide”, explicar que Pandora ya no tiene un modo `Data` implícito en el
comando de Sky-Claw, pero que el rig aún debe demostrar que la versión real
respeta `--output`; T-27 sigue abierto hasta que la lectura y ejecución también
ocurran dentro del sandbox USVFS. Mantener T-25 como parcial y posterior a T-27.

- [ ] **Step 4: Ejecutar tests y lint Markdown enfocados**

Run:

```powershell
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m pytest `
  tests/test_estado_ooda.py -q `
  --basetemp E:\pytest-tmp\pandora-managed-output-green-5
npx --yes markdownlint-cli2@0.18.1 `
  docs/pending_ooda_status.md `
  docs/superpowers/specs/2026-07-31-pandora-managed-output-design.md `
  docs/superpowers/plans/2026-07-31-pandora-managed-output.md
```

Expected: pytest PASS y markdownlint `0 error(s)`.

- [ ] **Step 5: Commit**

```powershell
git add tests/test_estado_ooda.py docs/pending_ooda_status.md
git commit -m "docs: record managed Pandora output status"
```

### Task 6: Validación integral, revisión y publicación del único PR

**Files:**

- Verify only: all files changed in Tasks 1-5

- [ ] **Step 1: Confirmar que no quedan APIs o narrativas ambiguas**

Run:

```powershell
rg -n "pandora_output_candidates|pandora_rollback_dirs" `
  sky_claw tests docs/pending_ooda_status.md
rg -n "Data.*Pandora|exe\.parent.*Pandora_Output" `
  sky_claw tests docs/pending_ooda_status.md
```

Expected: sin referencias productivas ni tests que modelen destinos alternativos;
las únicas coincidencias documentales permitidas describen deuda histórica o el
hecho de que `Data` no se mueve ni restaura.

- [ ] **Step 2: Ejecutar la suite Pandora/OODA completa con warnings como error**

Run:

```powershell
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m pytest `
  tests/test_pandora_runner.py `
  tests/test_pandora_service.py `
  tests/test_pandora_agent_gate.py `
  tests/test_output_targets.py `
  tests/test_rollback_reconciler.py `
  tests/test_estado_ooda.py `
  -W error -q `
  --basetemp E:\pytest-tmp\pandora-managed-output-focused-final
```

Expected: PASS, cero warnings.

- [ ] **Step 3: Ejecutar gates estáticos**

Run:

```powershell
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m ruff check sky_claw/ tests/
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m ruff format --check sky_claw/ tests/
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m mypy sky_claw/
npx --yes markdownlint-cli2@0.18.1 `
  docs/pending_ooda_status.md `
  docs/superpowers/specs/2026-07-31-pandora-managed-output-design.md `
  docs/superpowers/plans/2026-07-31-pandora-managed-output.md
```

Expected: Ruff check PASS, Ruff format PASS, mypy PASS y markdownlint
`0 error(s)`. Recordar en el PR que `sky_claw.local.tools.*` está exento de mypy
estricto y que la confianza allí proviene de tests y Ruff.

- [ ] **Step 4: Ejecutar dos suites completas Windows**

Run:

```powershell
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m pytest -q `
  --basetemp E:\pytest-tmp\pandora-managed-output-full-1
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m pytest -q `
  --basetemp E:\pytest-tmp\pandora-managed-output-full-2
```

Expected: ambas PASS. Si aparece un fallo ajeno, reproducirlo enfocado antes de
atribuirlo; no ampliar este PR sin evidencia causal.

- [ ] **Step 5: Ejecutar revisión adversarial del diff**

Revisar `git diff origin/main...HEAD` con foco en: ruta no controlada, bypass
LLM, divergencia GUI/LLM, move-aside de árboles compartidos, cancelación,
pérdida de lease, journal marcado falsamente y reconciliación fuera de raíz.
Corregir únicamente hallazgos funcionales confirmados y repetir los gates
afectados.

- [ ] **Step 6: Publicar un solo PR listo para review**

```powershell
git status --short
git log --oneline origin/main..HEAD
git push -u origin codex/pandora-managed-output
gh pr create --repo FacundoSu1986/Sky-Claw `
  --base main `
  --head codex/pandora-managed-output `
  --title "fix: force managed Pandora output" `
  --body-file .github/pr-body-pandora-managed-output.md
```

Antes del comando, crear el body temporal fuera del repo o mediante el mecanismo
de edición permitido, incluyendo: alcance, TDD rojo/verde, gates ejecutados,
estado U-04/T-27, y “Pandora/MO2 real no ejecutado”. No commitear el body.

- [ ] **Step 7: Atender conversaciones y mergear solo con estado terminal verde**

Enumerar todas las conversaciones abiertas, validar cada observación contra el
HEAD actual, aplicar el diff mínimo si es consistente, responder con evidencia y
resolver después. Repetir checks requeridos y confirmar `MERGEABLE / CLEAN`, cero
conversaciones abiertas y HEAD remoto igual al local. Un rate limit de CodeRabbit
se documenta; no sustituye la revisión interna ni autoriza el merge con checks
pendientes o fallidos.
