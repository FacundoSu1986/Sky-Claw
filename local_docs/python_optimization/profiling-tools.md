# Profiling Tools — Detailed Reference

This file provides in-depth usage guides, output interpretation, and advanced flags for each profiling tool. Read this when the SKILL.md guidance is not sufficient for the user's specific profiling scenario.

---

> **Context Plumbing Integration:** This is a **L0 Measurement reference**. Each tool section is self-contained (Context Quarantine) — read only the section relevant to your current profiling task. Do not load all sections into working memory simultaneously.
>
> **Tool Loadout Constraint:** Select ≤ 3 profiling tools per session. Running multiple profilers simultaneously produces unreliable results due to measurement interference.

## Table of Contents

1. [cProfile — Built-in CPU Profiler](#cprofile)
2. [line_profiler — Line-Level CPU Profiler](#line_profiler)
3. [Memray — Modern Memory Profiler](#memray)
4. [memory_profiler — Classic Memory Profiler](#memory_profiler)
5. [py-spy — Production Profiler](#py-spy)
6. [Scalene — Full-Stack Profiler](#scalene)
7. [Austin — Statistical Profiler](#austin)
8. [pyinstrument — Low-Overhead Profiler](#pyinstrument)
9. [timeit — Microbenchmarking](#timeit)

---

## cProfile

cProfile is a deterministic profiler built into Python's standard library. It instruments every function call, recording call count, total time, and cumulative time. It adds significant overhead (2-5x slowdown) but provides exact, reproducible results.

### Running cProfile

```bash
# Profile a script, save output
python -m cProfile -o output.prof script.py

# Profile a specific function
python -m cProfile -s cumulative script.py

# Profile a module
python -m cProfile -m mypackage.main
```

### Understanding the Output

```
   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
    100000    0.050    0.000    0.050    0.000 script.py:10(helper)
        1    0.001    0.001    0.120    0.120 script.py:20(main)
     1000    0.020    0.000    0.080    0.080 script.py:15(process)
```

| Column | Meaning |
|---|---|
| `ncalls` | Number of calls. Format `n/m` means n direct calls + m recursive |
| `tottime` | Time spent in this function alone (excluding sub-calls) |
| `percall` | `tottime / ncalls` |
| `cumtime` | Cumulative time in this function + all functions it calls |
| `percall` | `cumtime / ncalls` |

### Sorting Strategies

```python
import pstats

stats = pstats.Stats("output.prof")

# Sort by cumulative time — find the expensive call chain
stats.sort_stats(pstats.SortKey.CUMULATIVE)

# Sort by self time — find functions that are themselves slow
stats.sort_stats(pstats.SortKey.TIME)

# Sort by call count — find frequently called functions
stats.sort_stats(pstats.SortKey.CALLS)

# Restrict to functions matching a pattern
stats.print_stats("my_module")
stats.print_stats("process_data")

# Print callers of a specific function (reverse call graph)
stats.print_callers("expensive_function")
```

### Visualization: snakeviz

```bash
pip install snakeviz
snakeviz output.prof  # Opens interactive viewer in browser
```

snakeviz provides a treemap visualization of the profile data, making it easy to spot large blocks of time at a glance.

### Context Pruning: Focused Output

When reporting cProfile results, apply Context Pruning to avoid flooding the conversation:

```python
# Pruned output: only top-10 by cumulative time, filtered to project modules
stats.sort_stats(pstats.SortKey.CUMULATIVE)
stats.print_stats("my_project", 10)  # Top 10 in my_project only
```

### Limitations

- High overhead makes it unsuitable for production workloads
- Cannot profile C extension functions in detail (shows as opaque blocks)
- No memory profiling capability
- Deterministic profiling can alter timing characteristics

---

## line_profiler

line_profiler provides per-line timing for Python functions, which is essential when cProfile shows a function is expensive but you cannot tell which specific line is the bottleneck.

### CLI Usage

```bash
# Add @profile decorator to functions you want to profile (no import needed)
# Then run:
kernprof -l -v script.py
```

The `-l` flag enables line-by-line profiling and `-v` prints results immediately.

### Programmatic API

```python
from line_profiler import LineProfiler

def target_function(data):
    # ... code to profile ...
    pass

def main():
    lp = LineProfiler()
    lp.add_function(target_function)
    # Add additional functions if they're called by target_function
    lp.add_function(helper_function)

    lp_wrapper = lp(target_function)
    lp_wrapper(test_data)

    lp.print_stats()
    # Or save to file:
    lp.dump_stats("line_profile.txt")
```

### Reading the Output

```
Timer unit: 1e-06 s

Total time: 0.520 s
File: script.py

Line #      Hits         Time  Per Hit   % Time  Line Contents
==============================================================
    10                                           def process(items):
    11      1000       50000     50.0     10.0      result = []
    12   1000000     2500000      2.5     48.0      for item in items:
    13    999000     1500000      1.5     28.8          if item > threshold:
    14     500000      800000      1.6     15.4              result.append(transform(item))
    15      1000        5000      5.0      1.0      return result
```

Key columns:
- **Hits**: How many times the line executed — high values combined with high Time indicate hot paths
- **% Time**: Proportion of total function time spent on this line
- **Per Hit**: Average time per execution — useful for spotting lines where each individual execution is expensive

### Integrating with Tests

```python
import unittest
from line_profiler import LineProfiler

class TestPerformance(unittest.TestCase):
    def test_process_performance(self):
        lp = LineProfiler()
        lp.add_function(process_data)
        lp_wrapper = lp(process_data)

        result = lp_wrapper(large_test_data)

        # Get stats programmatically
        stats = lp.get_stats()
        # Check that the hot loop takes less than expected time
        # (fragile, but useful for regression detection)
```

---

## Memray

Memray is a memory profiler developed by Bloomberg. It tracks every memory allocation in a running Python process with low overhead, replacing the older `memory_profiler` for most use cases.

### Basic Usage

```bash
# Track all allocations in a script
memray run -o output.bin script.py

# View a flame graph of memory allocations
memray flamegraph output.bin

# View a tree of allocations
memray tree output.bin

# Live tracking (updates every second)
memray live output.bin
```

### Programmatic API

```python
import memray

# Track a specific code block
with memray.Tracker("allocations.bin"):
    result = memory_intensive_function()

# Track with a filter (only certain modules)
with memray.Tracker("allocations.bin", trace_python_allocators=False):
    result = function_with_native_allocations()

# Access statistics programmatically
from memray import FileReader

reader = FileReader("allocations.bin")
reader.close()
```

### Key Features

- **Native allocation tracking**: Can track C extension allocations (NumPy arrays, etc.)
- **Flame graphs**: Visualize which call paths allocate the most memory
- **Leak detection**: Compare snapshots over time to find memory leaks
- **Low overhead**: Typically 10-30% slowdown vs 5-10x for memory_profiler
- **Python 3.8+ support**: Modern, actively maintained

### Flame Graph Interpretation

The flame graph in Memray shows:
- Width = total memory allocated by that call path
- Height = call stack depth
- Color = usually by module (different colors for different libraries)

Wide, shallow blocks indicate top-level functions that allocate a lot. Narrow, deep blocks indicate allocations buried in helper functions.

### Leak Detection

```bash
# Record allocations over time
memray run -o live.bin script.py

# Generate a flame graph showing only allocations that were NOT freed
memray flamegraph live.bin --leaks
```

This highlights allocations that grew over time — strong evidence of a memory leak.

---

## memory_profiler

The classic line-by-line memory profiler. Slower than Memray but provides per-line granularity similar to line_profiler but for memory:

```bash
python -m memory_profiler script.py

# With time-based sampling (default: 0.1s intervals)
python -m memory_profiler --interval 0.01 script.py
```

### Programmatic API

```python
from memory_profiler import profile, memory_usage

@profile(precision=4)
def my_function():
    big = [0] * 1_000_000
    return sum(big)

# Track memory over time (useful for detecting leaks)
def detect_leak():
    mem_history = memory_usage((run_repeatedly, (), {}), interval=0.01)
    # If mem_history shows steady growth, there's a leak
```

### When to Use memory_profiler Over Memray

- When you need per-line memory changes (Memray shows allocations, not per-line deltas)
- When working with Python versions below 3.8
- When you need the `@profile` decorator without programmatic setup

---

## py-spy

A sampling profiler written in Rust that can attach to running Python processes without modifying the code. It reads the Python stack from `/proc/<pid>/mem` and samples at configurable intervals.

### Key Commands

```bash
# Live top view (like htop but for Python functions)
py-spy top --pid <PID>

# Record flame graph over a time window
py-spy record -o flame.svg --pid <PID> --duration 60

# Record a script from start to finish
py-spy record -o flame.svg -- python script.py

# Dump all thread stacks (useful for debugging deadlocks)
py-spy dump --pid <PID>

# Generate a speedscope-compatible JSON output
py-spy record -o speedscope.json --format speedscope -- python script.py
```

### Advanced Flags

```bash
# Increase sampling rate for more precision (default: 100Hz)
py-spy record --rate 500 -o flame.svg --pid <PID>

# Show native (C/Rust) frames in addition to Python
py-spy record --native -o flame.svg --pid <PID>

# Show line numbers in the flame graph
py-spy record --lines -o flame.svg --pid <PID>

# Profile a subprocess
py-spy record --subprocesses -o flame.svg -- python manage.py runserver
```

### Docker Compatibility

py-spy can profile processes inside Docker containers if you have `--pid=host` or run py-spy from the host with access to the container's PID namespace:

```bash
docker run --pid=host myimage python app.py
py-spy top --pid $(docker inspect --format '{{.State.Pid}}' container_name)
```

### Limitations

- Linux and macOS only (no Windows support)
- Requires `ptrace` permissions (Docker, some security policies block this)
- Sampling-based: cannot tell you exact call counts, only relative proportions
- Cannot profile code that releases the GIL during sampling

---

## Scalene

Scalene is a CPU and memory profiler designed to provide actionable insights for Python performance. It uniquely distinguishes between time spent in Python vs. time spent in C code.

### Running Scalene

```bash
# Profile a script (replaces cProfile + memory_profiler)
scalene script.py

# Generate an HTML report
scalene --html --outfile report.html script.py

# Profile a specific function via API
scalene --cli function_to_profile script.py
```

### Key Output Columns

Scalene's output includes:
- **% Python / % C**: Shows what percentage of time is spent in Python code vs C extensions. If most time is in C, Python-level optimization won't help — optimize the algorithm or data flow instead
- **Memory timeline**: Shows memory usage over time
- **Copy volume**: Tracks unnecessary data copies
- **Per-line CPU and memory**: Combines line_profiler and memory_profiler in one tool

### GPU Profiling

Scalene can profile GPU memory and utilization for code using PyTorch, TensorFlow, or CUDA:

```bash
scalene --gpu script.py
```

### When to Use Scalene

- When you want a single tool that gives CPU + memory + GPU insights
- When you need to understand Python vs C breakdown to decide optimization strategy
- When profiling Jupyter notebooks (`%load_ext scalene`)

---

## Austin

Austin is a statistical profiler for CPython written in C. It works by sampling the stack at regular intervals without instrumenting the code, resulting in very low overhead.

### Running Austin

```bash
# Profile a script
austin -o output.txt python script.py

# Profile a running process
austin -o output.txt -p <PID>

# Control sampling rate (default: 1000Hz)
austin -o output.txt -i 100 python script.py

# Generate a flame graph from Austin output
austin2flamegraph output.txt > flame.svg
```

### Austin vs py-spy

Both are sampling profilers, but:
- **Austin** is faster (written in C, lower overhead) and produces structured text output
- **py-spy** has better visualization (built-in SVG flame graphs) and native frame support
- Use Austin when you need maximum profiling accuracy on performance-sensitive workloads
- Use py-spy when you want a better out-of-the-box visualization experience

---

## pyinstrument

A low-overhead sampling profiler that focuses on producing readable, actionable call trees. Unlike cProfile, it doesn't instrument every call — it samples the stack periodically, resulting in much less overhead.

### Running pyinstrument

```bash
pyinstrument script.py
pyinstrument -o output.html script.py  # Save HTML report
pyinstrument -r renderer=html script.py
```

### Programmatic API

```python
import pyinstrument

profiler = pyinstrument.Profiler()
profiler.start()

# ... code to profile ...

profiler.stop()
print(profiler.output_text(unicode=True, color=True))

# Save HTML report
with open("profile.html", "w") as f:
    f.write(profiler.output_html())
```

### Why pyinstrument Over cProfile

- 10-100x less overhead than cProfile
- Produces much more readable output (collapses recursion, shows time percentages)
- Built-in HTML reports with interactive drill-down
- Excellent for profiling web requests in Django/Flask (as middleware)
- Naturally handles async code

### Django/Flask Integration

```python
# Django middleware
def pyinstrument_middleware(get_response):
    def middleware(request):
        profiler = pyinstrument.Profiler()
        profiler.start()
        response = get_response(request)
        profiler.stop()
        response.headers["X-Profile"] = profiler.output_text()
        return response
    return middleware

# Flask
@app.before_request
def before_request():
    g.profiler = pyinstrument.Profiler()
    g.profiler.start()

@app.after_request
def after_request(response):
    g.profiler.stop()
    # Save or attach to response
    return response
```

---

## timeit

The standard library module for reliable microbenchmarking. It disables the garbage collector and runs multiple repetitions, reporting minimum time.

### Basic Usage

```python
import timeit

# Time a single statement
t = timeit.timeit("sum(range(1000))", number=10000)
print(f"{t/10000:.6f} seconds per call")

# Time a code snippet with setup
t = timeit.timeit(
    "process(data)",
    setup="data = list(range(1000))\nfrom __main__ import process",
    number=10000
)

# Time a callable directly (most reliable)
t = timeit.timeit(lambda: my_function(test_arg), number=1000)

# Repeat multiple times to get statistics
times = timeit.repeat(
    "my_function(test_arg)",
    setup="from __main__ import my_function, test_arg",
    number=1000,
    repeat=5
)
print(f"Mean: {min(times)/1000:.6f}s (best of 5 runs)")
```

### Command Line

```bash
python -m timeit -n 1000 -r 5 "sum(range(1000))"

# With setup
python -m timeit -s "data = list(range(1000))" "sum(data)"
```

### Important: Report the Minimum, Not the Mean

When benchmarking, report the best (minimum) time, not the mean. The minimum represents the time without interference from other system processes. Use `min(timeit.repeat(...))` for reliable results:

```python
import timeit
import statistics

times = timeit.repeat("target()", setup="from __main__ import target", number=100, repeat=20)

print(f"Best:   {min(times)/100:.6f}s")
print(f"Median: {statistics.median(times)/100:.6f}s")
print(f"Mean:   {statistics.mean(times)/100:.6f}s")
print(f"Stdev:  {statistics.stdev(times)/100:.6f}s")
```

The standard deviation tells you how noisy the measurement is. If stdev is large relative to the mean, the benchmark environment is not stable — close other programs, pin CPU frequency, or increase the number of repetitions.

### Common Pitfalls

- **Benchmarking in the global scope**: Setup and import costs contaminate the measurement. Always use `timeit`'s `setup` parameter or the CLI.
- **Not disabling GC**: `timeit` does this by default, but if you're measuring manually with `time.time()`, call `gc.disable()` before the timed section.
- **Microbenchmarks don't predict macro performance**: The fastest way to do X may not matter if X is not the bottleneck. Always profile first.
- **Python startup time**: Don't include interpreter startup. Use `-m timeit` or `timeit.timeit()` which avoids this.

---

## Tool Selection Decision Tree

> **Goal Drift Control:** Always select the profiler based on the bottleneck type identified in SKILL.md Step 3. Do not profile speculatively.

```yaml
# YAML format per Anti-Rot Structured Output protocol
tool_selection:
  cpu_bound:
    development:
      first_choice: cProfile
      line_level: line_profiler
      low_overhead: pyinstrument
    production:
      first_choice: py-spy
      alternative: Austin
  memory_bound:
    allocation_tracking: Memray
    line_by_line: memory_profiler
    full_stack: Scalene
  io_bound:
    first_choice: pyinstrument
    alternative: cProfile
  mixed:
    first_choice: Scalene
    alternative: cProfile + Memray (run separately, never simultaneously)
  regression_testing:
    framework: pytest-benchmark
    micro: timeit
```
