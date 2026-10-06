# Revisión de seguridad del PR #690 — bump de `multidict` por CVE-2026-104874

**Objeto:** `FacundoSu1986/Sky-Claw#690`, head `591ebdf` sobre `main` `0c2c541`
(+203 / −147; 3 archivos: `pyproject.toml`, `requirements.lock`,
`tests/test_project_config.py`). Un solo commit, autor `openhands-agent`.
**CI del PR:** 19 checks en verde + 1 *skipped*, incluido 🛡️ Security Scan y
🧪 Tests/windows-latest en py3.11 y py3.12.
**Postura:** revisión independiente. Ninguna afirmación del autor se dio por
buena porque estuviera declarada: la premisa CVE, la integridad de los hashes,
los gates de CI y **las dos rutas de instalación** se reprodujeron localmente.
**Veredicto:** 🔴 **Request changes** — el PR cumple su función en la vía
pip/CI (probado), pero **no** en la vía uv que el propio repo documenta, y el
ancla de test omite precisamente la capa que habría atrapado ese hueco.

---

## 0. Resumen ejecutivo

| ID | Hallazgo | Severidad | Cómo se confirmó |
|---|---|---|---|
| **F1** | `uv.lock` no regenerado: la ruta `uv sync --frozen` de `setup_env.ps1` sigue instalando la versión vulnerable, y `uv sync --locked` (guías) falla duro | 🔴 **Bloqueante** | Reproducido: `uv export --frozen` → `multidict==6.7.1`; `uv lock --check` → error |
| **F2** | El ancla de test omite la capa `uv.lock`, a diferencia de su hermana `anyio` → falsa seguridad con CI verde | 🟠 Mayor | Mutación M1/M4 |
| **F3** | Sin entrada en `CHANGELOG.md` (`### Security`) contra convención explícita del repo | 🟡 Menor | Precedentes literales (anyio, python-engineio, langgraph) |
| **F4** | El test sólo inspecciona la **primera** coincidencia `^multidict==` → ciego a duplicados | 🟡 Menor | Mutación M6 |
| **F5** | Oráculo rígido (igualdad exacta del `SpecifierSet`) → falso rojo al endurecer legítimamente el piso/cap | 🟡 Menor | Mutación M5 |
| **F6** | (Fuera de alcance) El gate `--require-hashes` no valida Linux/macOS; el `.exe` ya publicado conserva 6.7.1 | 🔵 Observación | Fallo idéntico en `main` y en el PR sobre Linux |
| **F7** | `<7` correcto hoy (espejo de aiohttp); dejar nota de seguimiento | ⚪ Nit | Metadatos de `aiohttp` en PyPI |

Lo que **sí** está bien y conviene decirlo: el objetivo del CVE es correcto, el
diff del lock es quirúrgico, los 171 hashes del pin nuevo son exactamente los de
PyPI (sin fabricar y sin faltar), `pip-audit` pasa de rojo (main) a verde (PR), y
no se toca código de producción.

---

## 1. Premisa: el CVE es real y el objetivo del fix es el correcto

- **CVE-2026-104874 / GHSA-54p9-h82j-f925**, CWE-401, CVSS 3.1 5.3
  (`AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:L`): **sólo disponibilidad**. Fuga de
  referencias en la extensión C (`multidict_itemsview_or2_impl`,
  `multidict_itemsview_sub1_impl`) al operar unión/resta de items-views con
  operandos cuyo tamaño controla el atacante; el GC no recupera los objetos.
  Rango afectado `>=6.7.0,<6.9.1`; arreglado en **6.9.1**. Exactamente el pin que
  tenía el lock (`6.7.1`).
- **Exposición en este producto:** NiceGUI bindea loopback
  (`sky_claw/app/gui/_bootloader.py:688`, `host="127.0.0.1"`), el server de
  métricas usa `127.0.0.1` (`sky_claw/app/core/metrics_server.py:24`) y la API
  `:8765` exige `X-Auth-Token`. La severidad práctica es baja; esto es higiene de
  dependencias, no respuesta a incidente.
- **6.9.1 existe y tiene cobertura universal:** PyPI publica 170 wheels + 1
  sdist, con 24 wheels `win*` y 23 `cp313` (el lock incluye los 171 hashes).
  `aiohttp` exige `multidict>=4.5,<7.0` y `yarl` `>=4.0` → el rango declarado
  `>=6.9.1,<7` es compatible y su cap `<7` es espejo del de `aiohttp`. `>=6.9.1`
  es el piso mínimo efectivo (no hay 6.9.x posterior; el siguiente es 7.0.0).

---

## 2. Verificación independiente (evidencia reproducible)

| Check | Comando / método | Resultado |
|---|---|---|
| **SCA A/B** | `pip-audit --strict -r <lock sin hashes>` (Linux, pip-audit 2.10.1) | **PR: 0 vulnerabilidades.** `main`: 1 → `multidict 6.7.1 CVE-2026-104874` (fix 6.9.1). **El PR cumple su función en la vía pip.** |
| **Integridad de hashes** | set-equality `--hash` del lock vs JSON de PyPI | `multidict==6.9.1`: **171/171 exactos**, 0 fabricados, 0 faltantes, sdist incluido |
| **Cadena ampliada** | ídem para `yarl`, `aiohttp`, `propcache`, `aiofiles`, `nicegui` | 544/544 hashes pertenecen a su release declarada; 0 ajenos |
| **Diff del lock** | `git diff main...591ebdf -- requirements.lock` | Sólo el bloque `multidict==` (1 línea de paquete); "no unrelated dependencies" **verificado** |
| **Tests** | `pytest tests/test_project_config.py` | 15 pass + 1 bloqueado por entorno local sin `pydantic` (16 recolectados, coincide con el cuerpo del PR). Los 3 rojos iniciales eran de entorno (faltaban `pytest-asyncio`/`pefile`) y ocurren igual en `main` |
| **Lint** | `ruff check` + `ruff format --check` sobre el test nuevo | PASS |
| **CI real** | `gh pr checks 690` | 19 pass + 1 skipped; el ancla corre en el job 🧪 Tests vía `pytest tests/` |

> Nota metodológica: `pip-audit -r requirements.lock` **con hashes falla en Linux**
> (y también sobre el lock de `main`): el lock no tiene marcadores (`;` = 0) y
> `keyring==25.7.0 → SecretStorage>=3.2` queda sin pin. El gate de CI corre en
> `windows-latest`, donde ese extra de Linux no se activa → verde sólo allí.
> Es preexistente y ajeno a este PR (ver F6).

---

## 3. F1 (bloqueante) — la vía uv queda sin remediar

`uv lock --check` en el head del PR:

```
error: The lockfile at `uv.lock` needs to be updated, but `--check` was provided.
```

…pero **también falla en `main`** (al lock le faltan `numpy`/`pillow`, drift
introducido por el merge `0c2c541` = #688). Es decir: el PR **no inventa** la
desincronización, pero la **agrava** y no la cierra.

Aislamiento del delta atribuible a este PR: partiendo de un `uv.lock` ya
sincronizado con `main`, `uv lock` imprime **`Updated multidict v6.7.1 -> v6.9.1`**
y nada más; tras regenerar, `uv export --frozen` devuelve `multidict==6.9.1`.
Es **un comando**, sin sorpresas en el grafo.

Impacto real, reproducido sobre el head:

- `uv export --frozen --extra dev` → **`multidict==6.7.1`** (vulnerable).
  `local_scripts/scripts/setup_env.ps1:51` usa exactamente
  `uv sync --frozen --extra dev`: el entorno de desarrollo documentado sigue
  instalando la versión afectada.
- `docs/user/installation.md:25` y `DEPLOYMENT.md:88` usan
  `uv sync --locked --extra dev`: ahora **falla duro** ("lockfile needs to be
  updated"), es decir se rompe la instalación documentada.
- `build.bat:44` y `CONTRIBUTING.md:20` usan `uv sync` a secas: re-lockea y
  arregla, pero ensucia el árbol de trabajo.

Convención del repo que este PR incumple (literal en `CHANGELOG.md:151` para el
bump de `anyio`, y equivalentes para `python-engineio`/`python-socketio` y
`langgraph`):

> …se sube floor directo en `pyproject.toml` … y **se regeneran
> `requirements.lock` y `uv.lock`**, de modo que tanto la instalación vía pip
> como la vía uv documentada … quedan remediadas.

**Parche:** `uv lock` + commit de `uv.lock`. Nota para el autor: el re-lock
arrastrará también `numpy`/`pillow` (drift preexistente de #688); conviene
declararlo en el cuerpo del PR o separarlo en un PR de bookkeeping — pero el
`uv.lock` no puede quedar sin re-lockear, porque el `pip-audit` de CI **no mira
esa ruta**.

---

## 4. F2/F4/F5 — las anclas del test, medidas por mutación

La auditoría del #460 (misma carpeta) fija el estándar del repo: romper cada
mecanismo y confirmar que el test cae *por la razón que declara*. Aplicado al
test nuevo (`test_multidict_piso_de_seguridad_declarado_y_bloqueado`) y a su
hermana `test_anyio_piso_de_seguridad_declarado_y_bloqueado`:

| # | Mutación aplicada | Ancla | Resultado |
|---|---|---|---|
| M1 | estado basal del PR: `uv.lock` pinnea `6.7.1` (vulnerable, sin tocar nada) | multidict | 🟢 **PASA** → punto ciego |
| M2 | `requirements.lock`: `6.9.1` → `6.7.1` | multidict | 🔴 cae (`requirements.lock tiene multidict 6.7.1 < 6.9.1`) |
| M3 | `pyproject.toml`: floor → `>=6.7.1,<7` | multidict | 🔴 cae (`rango de multidict inesperado`) |
| M4 | `uv.lock`: `anyio 4.14.2` → `4.13.0` | **anyio** (hermana) | 🔴 cae (`uv.lock tiene anyio 4.13.0 < 4.14.2`) |
| M5 | `pyproject.toml`: `>=6.9.1,<8` (cap relajado, **no** menos seguro) | multidict | 🔴 falso rojo |
| M6 | `requirements.lock`: añadir un `multidict==6.7.1` duplicado al final | multidict | 🟢 PASA → ciego al duplicado |

Lectura:

- **F2 (mayor):** M1 demuestra que la capa que falta es real y silenciosa; M4
  demuestra que la hermana `anyio` **sí** detecta un downgrade equivalente —el
  test anyio ancla las tres capas (`pyproject`, `requirements.lock`, `uv.lock`,
  `tests/test_project_config.py:389-422`)—. No es una limitación general: es una
  desviación del patrón que el propio repo ya fijó, y es exactamente la capa que
  protege la ruta uv. **Parche recomendado** (espejo del bloque anyio):

  ```python
      # 3. uv.lock
      with (REPO_ROOT / "uv.lock").open("rb") as file:
          uv_data = tomllib.load(file)
      paquetes_multidict = [p for p in uv_data.get("package", []) if p.get("name") == "multidict"]
      assert len(paquetes_multidict) == 1, f"se esperaba 1 paquete multidict en uv.lock, hay {len(paquetes_multidict)}"
      version_uv = Version(paquetes_multidict[0]["version"])
      assert version_uv >= piso_minimo, f"uv.lock tiene multidict {version_uv} < {piso_minimo}"
  ```

- **F4 (menor):** la regex `(?m)^multidict==([^\s\\]+)` sólo mira la primera
  coincidencia (M6). Mitigante: pip rechaza requirements duplicados en modo
  hash, así que el escenario requiere edición manual del lock. Opcional: iterar
  `re.findall` y exigir que **todas** las coincidencias cumplan.
- **F5 (menor):** la igualdad exacta del `SpecifierSet` (M5) convierte una
  decisión futura legítima (subir el piso a `>=6.9.2`, o soltar el cap `<7`
  cuando `aiohttp` lo relaje) en un rojo falso. Sugerencia: comprobar
  `req.specifier.contains("6.9.1")` y que el piso declarado sea `>=` el mínimo
  seguro, en vez de igualdad de string normalizado.

---

## 5. F3 (menor) — falta la entrada de CHANGELOG

No hay gate de CI que lo exija (verificado), pero sí convención escrita y
precedentes literales bajo `### Security` (anyio, python-engineio/socketio,
langgraph) que detallan CVE, floor y regeneración de **ambos** lockfiles.
Propuesta:

```markdown
### Security
- **`multidict` 6.7.1 → 6.9.1 (CVE-2026-104874)** — aviso que
  `pip-audit --strict --skip-editable -r requirements.lock` marca en el gate
  "Security Scan". Transitiva vía `aiohttp`/`yarl`; se sube floor directo en
  `pyproject.toml` (`multidict>=6.9.1,<7`, mismo patrón de pin transitivo que
  `anyio`) y se regeneran `requirements.lock` **y** `uv.lock`. Fuga de
  referencias (CWE-401, CVSS 5.3, sólo disponibilidad); sólo cambia ese pin.
```

---

## 6. F6/F7 — observaciones de seguimiento (no bloquean este PR)

- **F6a — El gate de hashes es efectivamente Windows-only.** `requirements.lock`
  no tiene marcadores (`;` = 0) ni `SecretStorage`, así que
  `pip install --require-hashes -r requirements.lock` y
  `pip-audit -r requirements.lock` fallan en Linux — **idéntico en `main` y en el
  PR**. El "lock installation check" del CI corre en `windows-latest` y por eso
  pasa. Recomendación (issue aparte): regenerar el lock en modo universal
  (`--python-platform` acorde) o añadir un job POSIX al gate.
- **F6b — El binario ya firmado no se cura solo.** `release.yml` construye con
  PyInstaller, genera SBOM y firma `dist/SkyClawApp.exe` con cosign: las
  versiones quedan embebidas al construir. El fix no alcanza a un `.exe` ya
  publicado; requiere release nuevo (como ya anotan bumps anteriores).
- **F7** — El cap `<7` es correcto hoy (espejo del `aiohttp`). Dejar nota para
  subir el piso cuando `aiohttp` relaje su propio cap.

---

## 7. Precisión del cuerpo del PR

| Afirmación del autor | Verificación |
|---|---|
| "exactly `multidict==6.7.1` -> `multidict==6.9.1` … No unrelated dependencies modified" | ✅ Verificado en `requirements.lock` |
| "No Frozen Runtime, runtime_vault, or runtime production code touched" | ✅ (3 archivos, ninguno de producción) |
| "Pytest config suite: PASS (16 passed)" | ✅ Consistente (15 + 1 bloqueado por mi entorno) y CI verde |
| "Lock installation check … PASS" | ⚠️ Válido sólo en Windows (ver F6a) |
| Mención de `uv.lock` | ❌ **Omitida por completo** (la parte incumplida) |
| Comentario automático qodo "Missing Import" | ❌ **Falso positivo**: `Requirement` y `SpecifierSet` se importan a nivel módulo (`tests/test_project_config.py:18-19`) y el test se ejecuta y pasa |
| Comentario automático CodeRabbit (regenerar `uv.lock`) | ✅ **Acertado** en el fondo; esta revisión lo confirma con evidencia |

---

## 8. Checklist para el autor

1. **`uv lock`** y commit de `uv.lock` (único cambio exigido por este PR:
   `multidict 6.7.1 → 6.9.1`; arrastra el drift preexistente `numpy`/`pillow`
   de #688 — declararlo o separarlo).
2. **Extender el test** con el bloque `uv.lock` (§4, espejo del anyio).
3. **Entrada de CHANGELOG** bajo `### Security` (§5).
4. *(Opcional, nit)* tolerar duplicados y validar
   `specifier.contains(version)` en vez de igualdad exacta (§4, F4/F5).
5. *(Seguimiento, fuera de este PR)* gate `uv lock --check` en CI + job POSIX
   para el gate de hashes (§6).

---

## Anexo — comandos usados (reproducibles)

```bash
# SCA A/B sobre los locks (quitar hashes primero: pip-audit en Linux no resuelve el lock tal cual)
sed 's/--hash=sha256:[0-9a-f]*//' requirements.lock > /tmp/pr-nohash.txt
pip-audit --strict -r /tmp/pr-nohash.txt        # -> No known vulnerabilities found
git show main:requirements.lock | sed 's/--hash=sha256:[0-9a-f]*//' > /tmp/main-nohash.txt
pip-audit --strict -r /tmp/main-nohash.txt      # -> 1 vuln: multidict 6.7.1 CVE-2026-104874

# Integridad de hashes contra PyPI
python3 - <<'EOF'
import json,re,urllib.request
lock=open('requirements.lock').read()
hs=set(re.findall(r'--hash=sha256:([0-9a-f]{64})',re.search(r'(?ms)^multidict==6\.9\.1.*?(?=^\S+==)',lock).group(0)))
d=json.loads(urllib.request.urlopen('https://pypi.org/pypi/multidict/json').read())
pypi={f['digests']['sha256'] for f in d['releases']['6.9.1']}
print(len(hs), len(pypi), hs-pypi, pypi-hs)   # -> 171 171 set() set()
EOF

# Ruta uv: estado y aislamiento
uv lock --check                 # -> error: needs to be updated (también en main por numpy/pillow)
uv export --frozen --extra dev | grep ^multidict   # -> multidict==6.7.1 (vulnerable)
# (con uv.lock ya sincronizado con main)  uv lock  ->  "Updated multidict v6.7.1 -> v6.9.1"
```

*Informe generado el 2026-10-06 contra el head `591ebdf`; el estado de `main`
citado es `0c2c541`.*
