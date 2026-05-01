---
name: python-performance-optimization
version: 2.0.0
architecture: context-plumbing
description: "Profile and optimize Python code for CPU, memory, and I/O performance. Use this skill whenever the user mentions slow code, performance issues, bottlenecks, profiling, optimization, speeding up Python, reducing memory usage, high CPU usage, latency problems, benchmarking, or wants to make their Python application faster or more efficient. Also trigger when the user asks about cProfile, line_profiler, memory_profiler, py-spy, memray, scalene, or any Python performance tool. Even if the user doesn't explicitly say 'performance' — phrases like 'takes too long', 'uses too much RAM', 'how do I make this faster', or 'can we optimize this loop' should activate this skill."
---

# Python Performance Optimization

Systematic workflow for identifying, measuring, and resolving Python performance bottlenecks across CPU execution, memory consumption, and I/O throughput. This skill provides a decision framework, tool selection guidance, and proven optimization patterns.

---

## Architecture: Context Plumbing & Anti-Rot Mitigation

This skill operates under a **multilevel memory architecture** designed to prevent context degradation, token pollution, and goal drift during extended optimization sessions.

### Memory Hierarchy

| Level | Type | Purpose | Persistence |
|---|---|---|---|
| **L0** | Working Memory | Scratchpads for intermediate profiling results, benchmark data | Per-session |
| **L1** | Episodic Memory | Technical log of past interactions to avoid rework loops | Cross-session |
| **L2** | MCP Integration | Model Context Protocol connections to external tools/profilers | On-demand |

### Anti-Rot Protocols

| Protocol | Mechanism | Trigger |
|---|---|---|
| **Context Quarantine** | Isolate subtask execution in dedicated scopes; each profiling run is self-contained | Before any profiling/measurement step |
| **Context Pruning** | Remove redundant tokens and superfluous metadata before generation | Before each output generation |
| **Structured Output** | All technical output in YAML or Markdown to minimize Grep Tax | All reporting sections |

### Tool Loadout Constraints

| Constraint | Value | Rationale |
|---|---|---|
| **Tool Limit** | ≤ 20 simultaneous tools | Prevent model confusion and context bloat |
| **Goal Drift Control** | Inject primary objective at each iteration | Maintain optimization target focus |
| **Mirror Reflection** | Self-validate output against System Prompt restrictions | Before final delivery |

---

## Workflow: How to Approach Any Performance Problem

Follow this sequence. Skipping steps leads to optimizing code that isn't the bottleneck — a waste of time and a source of unnecessary complexity.

> **Goal Drift Control — Primary Objective:** Identify → Measure → Optimize → Validate. Every step must advance one of these four phases.

### Step 1 — Understand the Problem Before Measuring

Talk to the user to define what "slow" or "inefficient" means concretely:

- Is it latency (a single request takes too long) or throughput (not enough requests per second)?
- Is the problem CPU-bound, memory-bound, or I/O-bound? This determines the profiling strategy.
- Is it a one-time batch job or a long-running service? Batch jobs care about total time; services care about tail latency (p99, p95).
- Are there specific error messages, OOM kills, or CPU spikes the user has observed?

**Context Quarantine:** Store user requirements in a dedicated scratchpad. Do not mix requirements with profiling data.

If the user cannot answer these questions, suggest running a quick measurement (Step 2) to gather data first.

### Step 2 — Measure, Don't Guess

Always profile before optimizing. Intuition about where time is spent is notoriously wrong in Python due to C extension overhead, interpreter quirks, and hidden allocations.

Use the **Tool Selection Guide** below to pick the right profiler for the situation. Run the profiler on a realistic workload — synthetic microbenchmarks that don't reflect real input distributions can be misleading.

**Context Quarantine:** Each profiling run produces isolated results. Tag results with:
- `timestamp`: When the measurement was taken
- `workload`: Description of the input data
- `tool`: Which profiler was used
- `bottleneck_type`: CPU | memory | I/O | mixed

### Step 3 — Identify the Bottleneck

Look at the profiling output and answer:

- Which function or code path consumes the most time/memory?
- Is the bottleneck algorithmic (wrong complexity class) or implementation-level (inefficient Python patterns)?
- Is the hot path in Python code or in a C extension/library call?

**Context Pruning:** Before reporting, strip raw profiler output to only the top-5 relevant entries. Do not dump full profiler tables into the conversation.

Only after identifying the bottleneck, proceed to Step 4.

### Step 4 — Apply the Right Optimization

Match the optimization to the bottleneck type:

- **Algorithmic**: Change data structures or algorithms (biggest gains)
- **Implementation**: Apply Python-specific speed patterns (see Pattern Catalog)
- **Concurrency**: Add threading, multiprocessing, or async I/O
- **Native extensions**: Use NumPy, Numba, Cython, or Rust for compute-heavy paths

Re-measure after each change. Never apply multiple optimizations at once — you need to know which change produced the improvement.

**Episodic Memory:** Record each optimization attempt and its measured impact to avoid repeating failed strategies.

### Step 5 — Validate and Document

- Confirm the improvement with a benchmark on the same workload
- Ensure correctness hasn't regressed (run tests)
- Document what was measured, what changed, and the measured improvement

**Mirror Reflection:** Before delivering results, validate:
1. Does the output address the user's original problem statement?
2. Are all metrics backed by actual measurements (not estimates)?
3. Is the output structured in Markdown/YAML per Anti-Rot protocol?

---

## Tool Selection Guide

Choose the profiler that matches the bottleneck type. Using the wrong tool wastes time.

> **Tool Loadout Constraint:** Never activate more than 20 tools simultaneously. Select the minimum set needed for the current phase.

| Situation | Primary Tool | Install | Quick Command |
|---|---|---|---|
| General CPU profiling | cProfile | built-in | `python -m cProfile -o out.prof script.py` |
| Line-level CPU breakdown | line_profiler | `pip install line-profiler` | `kernprof -l -v script.py` |
| Memory allocation tracking | Memray | `pip install memray` | `memray run -o out.bin script.py` |
| Memory line-by-line | memory_profiler | `pip install memory-profiler` | `python -m memory_profiler script.py` |
| Production process profiling | py-spy | `pip install py-spy` | `py-spy top --pid PID` |
| Full-stack CPU + memory + GPU | Scalene | `pip install scalene` | `scalene script.py` |
| Statistical profiler (low overhead) | Austin | `pip install austin` | `austin -o out.txt python script.py` |
| I/O-bound profiling | pyinstrument | `pip install pyinstrument` | `pyinstrument script.py` |
| Regression benchmarking | pytest-benchmark | `pip install pytest-benchmark` | `pytest --benchmark-only` |

### Tool Details

For detailed usage, output interpretation, and advanced flags for each tool, read `references/profiling-tools.md`. That file covers:

- cProfile: sorting, filtering, call graph analysis, snakeviz visualization
- line_profiler: manual API usage, integrating with tests
- Memray: flame graph generation, live tracking, allocation tracking
- py-spy: flamegraphs, dump stacks, Docker compatibility
- Scalene: GPU profiling, % in Python vs C, per-line memory
- Austin: statistical sampling theory, output formats
- pyinstrument: tree rendering, HTML reports, async support

---

## Profiling Patterns

### Pattern 1: Programmatic cProfile with Selective Focus

Use this when you need to profile specific functions within a larger codebase, not the entire script:

```python
import cProfile
import pstats
import io

def function_of_interest(data):
    """The function you suspect is slow."""
    return [x * 2 for x in data if x > 0]

def run_profiling():
    profile = cProfile.Profile()
    profile.enable()

    # --- Code under measurement starts ---
    function_of_interest(range(1_000_000))
    # --- Code under measurement ends ---

    profile.disable()

    # Format and display results
    stream = io.StringIO()
    stats = pstats.Stats(profile, stream=stream)
    stats.sort_stats(pstats.SortKey.CUMULATIVE)
    stats.print_stats(15)  # Top 15 entries by cumulative time

    print(stream.getvalue())

    # Save for visualization tools
    stats.dump_stats("profile_output.prof")
```

**Why cumulative sort?** Cumulative time shows the total time spent in a function AND everything it calls. This surfaces bottlenecks in high-level orchestrators, not just leaf functions. For identifying the innermost expensive calls, switch to `SortKey.TIME`.

### Pattern 2: Line-Level Profiling with Manual API

Avoid the `@profile` decorator in production code. Use the programmatic API instead:

```python
from line_profiler import LineProfiler

def process_data(records):
    """Process a batch of records — candidate for line-level profiling."""
    cleaned = []
    for record in records:
        if record.get("status") == "active":
            cleaned.append({
                "id": record["id"],
                "score": record["value"] / record["max_value"],
            })
    return cleaned

def main():
    data = [{"id": i, "status": "active", "value": i % 100, "max_value": 100}
            for i in range(500_000)]

    lp = LineProfiler()
    lp_wrapper = lp(process_data)
    lp_wrapper(data)
    lp.print_stats()
```

### Pattern 3: Memory Tracking with Memray

Memray is the modern replacement for `memory_profiler` — it tracks every allocation with negligible overhead and produces rich visualizations:

```python
# Run from CLI:
# memray run -o alloc.bin script.py
# memray flamegraph alloc.bin
# memray tree alloc.bin

# Programmatic API for in-process memory tracking:
import memray

def memory_intensive():
    data = {i: [j for j in range(100)] for i in range(10000)}
    return sum(len(v) for v in data.values())

with memray.Tracker("alloc.bin"):
    memory_intensive()
```

### Pattern 4: Production Profiling with py-spy

Attach to a running process without code changes, without stopping it, and without significant overhead:

```bash
# Top-style live view of a running process
py-spy top --pid <PID>

# Record a flamegraph over 60 seconds of production traffic
py-spy record -o flame.svg --pid <PID> --duration 60

# Dump all thread stacks right now (useful for deadlocks or hung processes)
py-spy dump --pid <PID>
```

---

## Pattern Catalog: Common Python Optimizations

These patterns address the most frequently encountered bottlenecks in real-world Python code. Each includes a benchmark and guidance on when the tradeoff is worth it.

### Pattern 5: List Comprehensions and Generator Expressions

List comprehensions are faster than equivalent `for` loops because they run at C speed inside CPython. Generator expressions add the benefit of constant memory usage for large datasets.

```python
# Slower: manual loop (Python bytecode per iteration)
def slow_filter(data, threshold):
    result = []
    for item in data:
        if item > threshold:
            result.append(item * 2)
    return result

# Faster: list comprehension (single C-level loop)
def fast_filter(data, threshold):
    return [item * 2 for item in data if item > threshold]

# Memory-efficient: generator expression (constant memory, same speed characteristics)
def memory_efficient_filter(data, threshold):
    return (item * 2 for item in data if item > threshold)
```

**When to use generators**: Anytime the full result doesn't need to reside in memory simultaneously. Common cases: feeding `sum()`, `max()`, `any()`, `all()`, `json.dumps()`, `csv.writer.writerows()`, or iterating in a pipeline. The generator pattern enables processing datasets larger than available RAM.

**When NOT to use generators**: When you need random access to elements, need to iterate multiple times, or need `len()`. Convert to a list in those cases.

### Pattern 6: String Building with `str.join()`

String concatenation with `+` in a loop creates a new string object on every iteration because Python strings are immutable. For building large strings, `join()` pre-computes the final size and allocates once:

```python
# Avoid: quadratic time and memory due to repeated allocation
def slow_build(parts):
    result = ""
    for p in parts:
        result += p  # Creates new string each time
    return result

# Preferred: linear time — single allocation
def fast_build(parts):
    return "".join(parts)

# For mixed types, build a list EXPLICITLY then join
def mixed_build(items):
    return "".join([str(item) for item in items])

# Subtlety: join() with a generator is NOT optimal — CPython needs to
# pre-compute the total size to allocate memory in one pass, so it
# internally materializes the generator into a list anyway. Passing a
# list directly avoids this hidden cost:
#   GOOD:   "".join([str(x) for x in items])   # list → single allocation
#   OK but: "".join(str(x) for x in items)     # generator → CPython builds list internally
# The performance difference is small, but the list form is always equal
# or faster and communicates intent more clearly.
```

### Pattern 7: Use `set` and `dict` for Membership Tests

List `in` checks are O(n). Set and dict lookups are O(1) average case. This is one of the highest-impact Python-specific optimizations:

```python
# Slow: O(n) per check
def slow_deduplicate(items):
    seen = []
    for item in items:
        if item not in seen:  # Linear scan each time
            seen.append(item)
    return seen

# Fast: O(1) per check
def fast_deduplicate(items):
    seen = set()
    for item in items:
        if item not in seen:
            seen.add(item)
    return list(seen)
```

**Rule of thumb**: If you're calling `x in collection` more than a few times, convert `collection` to a `set` first if it doesn't need to maintain order. Use `dict.fromkeys()` if you need to preserve insertion order while deduplicating.

### Pattern 8: Local Variable Caching for Hot Loops

Local variable access in CPython is faster than global, attribute, or closure lookups because `LOAD_FAST` bytecode is a simple array index, while `LOAD_GLOBAL` and `LOAD_ATTR` involve hash table lookups:

```python
# Slower: repeated attribute access inside a tight loop
def process_with_attr(obj_list):
    total = 0
    for obj in obj_list:
        total += obj.value * obj.weight  # Two LOAD_ATTR per iteration
    return total

# Faster: cache the attribute once before the loop
def process_with_local(obj_list):
    total = 0
    for obj in obj_list:
        value = obj.value
        weight = obj.weight
        total += value * weight  # LOAD_FAST for cached locals
    return total
```

This matters in tight loops executing millions of iterations. In code that runs a few hundred times, the difference is negligible — prefer readability.

### Pattern 9: `functools.lru_cache` for Repeated Computations

When a function is called repeatedly with the same arguments, memoization eliminates redundant computation at the cost of memory:

```python
from functools import lru_cache

@lru_cache(maxsize=1024)
def expensive_computation(n):
    """Cached — subsequent calls with same 'n' return instantly."""
    return sum(i ** 2 for i in range(n))

# For methods, use maxsize=None with caution (unbounded cache):
# Or clear periodically with .cache_clear()
```

**When to use**: Pure functions called repeatedly with overlapping arguments. Common in parsing, recursive algorithms, and data processing pipelines.

**When NOT to use**: Functions with mutable arguments (lists, dicts), functions where results change over time, or when memory pressure is a concern.

### Pattern 10: Batch I/O Operations

Individual I/O calls have fixed overhead per call (system call, context switch). Batching reduces the number of calls:

```python
# Slow: one write per item
def slow_write(items, filepath):
    with open(filepath, "w") as f:
        for item in items:
            f.write(f"{item}\n")

# Fast: buffer in memory, write once
def fast_write(items, filepath):
    with open(filepath, "w") as f:
        f.writelines(f"{item}\n" for item in items)

# Fastest for very large data: write in chunks to control memory
def chunked_write(items, filepath, chunk_size=10000):
    with open(filepath, "w") as f:
        chunk = []
        for item in items:
            chunk.append(f"{item}\n")
            if len(chunk) >= chunk_size:
                f.writelines(chunk)
                chunk.clear()
        if chunk:
            f.writelines(chunk)
```

---

## Decision Framework: When to Escalate Optimization Level

Not all performance problems require advanced techniques. Escalate only when simpler approaches have been exhausted:

| Level | Techniques | When to Apply |
|---|---|---|
| **L0: Measurement** | cProfile, timeit, pyinstrument | Always — never skip this level |
| **L1: Python patterns** | List comprehensions, set lookups, `join()`, `lru_cache`, generators | First optimization pass after identifying bottleneck |
| **L2: Concurrency** | `threading` for I/O, `multiprocessing` for CPU, `asyncio` for async I/O | When the profiler shows idle CPU time or blocked I/O |
| **L3: Libraries** | NumPy vectorization, pandas optimized ops, `collections.deque`, `bisect` | When L1 patterns aren't enough and data is numerical or structural |
| **L4: JIT / Compilation** | Numba, Cython, mypyc | When specific hot functions remain slow after L1-L3 |
| **L5: Architecture** | Caching layer, message queues, sharding, pre-computation | When single-process optimization hits diminishing returns |

For details on Levels 2-5 (concurrency, NumPy, Numba, Cython, architectural patterns), read `references/advanced-optimization.md`.

---

## Common Anti-Patterns to Detect

When reviewing user code, watch for these performance anti-patterns:

1. **String concatenation in a loop** with `+=` — replace with `join()`
2. **List membership checks** (`x in my_list`) inside loops — use `set()`
3. **Repeated function calls** with identical arguments — add `lru_cache`
4. **Reading files line by line** for structured data — use `csv`, `json`, or `pandas`
5. **Database queries inside loops** — batch into a single query with `IN` clause
6. **Deep copying** when a shallow copy or view suffices (`copy.deepcopy` is very slow)
7. **Global variables** accessed in tight loops — cache as local variables
8. **`isinstance` checks** on every call when dispatch happens once — restructure with protocol or registry
9. **Creating large intermediate lists** — use generator expressions or `itertools`
10. **Synchronous I/O in async code** — blocks the entire event loop
11. **`map(lambda, ...)` for transformations** — `lambda` incurs function call overhead on every element. Use a list comprehension instead: `[x**2 for x in data]` is both faster and more readable than `list(map(lambda x: x**2, data))`. The only case where `map` wins is with a C-level callable like `map(int, data)` or `map(operator.add, a, b)` — never with `lambda`.

---

## Reporting Results

When presenting optimization results to the user, include:

1. **Before/after metrics**: execution time, memory usage, or throughput
2. **The profiling evidence** that led to the optimization (what tool, what the data showed)
3. **The change made**: specific code diff with explanation
4. **Tradeoffs discussed**: readability, memory, complexity, maintainability
5. **Remaining bottlenecks**: if any, suggest next steps

**Structured Output Format (Anti-Rot Protocol):**

```markdown
## Optimization Report

### Problem Statement
- **Type**: [CPU | Memory | I/O | Mixed]
- **Symptom**: [latency | throughput | OOM | CPU spike]
- **Workload**: [description]

### Measurements

| Metric | Before | After | Delta |
|---|---|---|---|
| Execution time | X ms | Y ms | -Z% |
| Peak memory | X MB | Y MB | -Z% |
| Throughput | X ops/s | Y ops/s | +Z% |

### Changes Applied
1. [Description of change 1]
2. [Description of change 2]

### Tradeoffs
- [Tradeoff 1]
- [Tradeoff 2]

### Next Steps
- [Remaining bottleneck or recommendation]
```

Use `timeit` for reliable microbenchmarks (it disables the garbage collector and runs multiple repetitions). For end-to-end measurements, use the full production workload.

For **automated regression detection in CI/CD**, use `pytest-benchmark`. It wraps `timeit` into a pytest fixture, stores historical results across runs, and can fail the build if a benchmark degrades beyond a threshold:

```python
def test_process_performance(benchmark):
    result = benchmark(process_data, test_input)
    assert result is not None
```

Run with `pytest --benchmark-only` to skip non-benchmark tests, or `pytest --benchmark-compare` to compare against a previous run baseline.

---

## Mirror Reflection: Self-Validation Checklist

Before delivering any optimization result, verify against this checklist:

- [ ] **Goal Alignment**: Does this address the user's original performance problem?
- [ ] **Measurement-Backed**: Are all claims supported by profiler data or benchmarks?
- [ ] **Correctness Preserved**: Have tests been run to confirm no regression?
- [ ] **Context Pruned**: Is the output free of redundant raw profiler dumps?
- [ ] **Structured Format**: Is the report in Markdown/YAML per Anti-Rot protocol?
- [ ] **Tool Limit Respected**: Were ≤ 20 tools used in this session?
- [ ] **Episodic Log**: Have optimization attempts and outcomes been recorded?
