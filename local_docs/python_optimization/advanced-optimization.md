# Advanced Optimization Patterns

This file covers optimization techniques beyond basic Python patterns: concurrency models, NumPy vectorization, JIT compilation, Cython, and architectural approaches. Read this when the SKILL.md Pattern Catalog (L0-L1) has been applied but the bottleneck persists.

---

> **Context Plumbing Integration:** This is an **L2-L5 escalation reference**. Only consult this file after the SKILL.md Decision Framework indicates escalation beyond L1 patterns. Each section is self-contained (Context Quarantine) to prevent cross-contamination of optimization strategies.

## Table of Contents

1. [Concurrency: Threading vs Multiprocessing vs Async](#concurrency)
2. [NumPy Vectorization](#numpy)
3. [Numba JIT Compilation](#numba)
4. [Cython Compilation](#cython)
5. [Memory Optimization Deep Dive](#memory)
6. [Database Optimization](#database)
7. [Caching Strategies](#caching)
8. [Architectural Patterns](#architecture)

---

## Concurrency

Python's concurrency story is governed by the GIL (Global Interpreter Lock). Understanding what the GIL does and doesn't lock is essential for choosing the right concurrency model.

### The GIL Rule of Thumb

| Workload | GIL Impact | Recommended Model |
|---|---|---|
| CPU-bound computation | GIL serializes threads | `multiprocessing` or `concurrent.futures.ProcessPoolExecutor` |
| I/O-bound (network, files, DB) | GIL released during I/O wait | `threading`, `asyncio`, or `concurrent.futures.ThreadPoolExecutor` |
| Mixed CPU + I/O | Depends on ratio | Separate processes for CPU, threads/async for I/O |

### Threading for I/O-Bound Work

```python
import concurrent.futures
import urllib.request

urls = [f"https://example.com/data/{i}" for i in range(100)]

def fetch(url):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.read()

# ThreadPoolExecutor: ideal for I/O-bound tasks
with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
    futures = {pool.submit(fetch, url): url for url in urls}
    results = {}
    for future in concurrent.futures.as_completed(futures):
        url = futures[future]
        try:
            results[url] = future.result()
        except Exception as e:
            results[url] = None
```

**Why `ThreadPoolExecutor` over raw `threading.Thread`?** It manages thread lifecycle, provides `as_completed()` for handling results as they arrive, limits concurrency with `max_workers`, and handles exceptions cleanly.

### Multiprocessing for CPU-Bound Work

```python
import concurrent.futures
import multiprocessing

def heavy_computation(n):
    """CPU-intensive work that benefits from parallelism."""
    return sum(i ** 2 for i in range(n))

# Use ProcessPoolExecutor — same API as ThreadPoolExecutor
# Workers run in separate processes, bypassing the GIL
with concurrent.futures.ProcessPoolExecutor(
    max_workers=multiprocessing.cpu_count()
) as pool:
    numbers = [10_000_000 + i * 1_000_000 for i in range(8)]
    results = list(pool.map(heavy_computation, numbers))
```

**Important tradeoffs with multiprocessing:**
- Each process has its own memory space — data must be serialized (pickled) to pass between processes
- Startup overhead is significant (process creation costs milliseconds, not microseconds)
- Best for large, coarse-grained work units — don't use for small tasks where serialization overhead exceeds computation time
- `max_workers` should typically match CPU count for CPU-bound work

### asyncio for High-Concurrency I/O

```python
import asyncio
import aiohttp

async def fetch(session, url):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            return await resp.text()
    except Exception:
        return None

async def fetch_all(urls):
    async with aiohttp.ClientSession() as session:
        tasks = [fetch(session, url) for url in urls]
        return await asyncio.gather(*tasks)

# Run with: asyncio.run(fetch_all(urls))
```

**When to choose asyncio over threading:**
- When managing thousands of concurrent connections (web crawlers, WebSocket servers, API gateways)
- When the I/O operations have compatible async libraries (aiohttp, asyncpg, aioredis)
- When callback-based coordination is complex (asyncio provides structured concurrency with `gather`, `wait`, `TaskGroup`)

**When NOT to use asyncio:**
- When libraries don't support async (most scientific computing, older database drivers)
- When mixing with blocking (synchronous) I/O — this blocks the entire event loop
- When the workload is CPU-bound (use multiprocessing instead)

### Shared Memory with multiprocessing (Python 3.8+)

Avoid serialization overhead by sharing memory between processes:

```python
from multiprocessing import shared_memory
import numpy as np

def process_chunk(shm_name, shape, dtype, offset):
    """Worker that reads from shared memory — no copy needed."""
    existing = shared_memory.SharedMemory(name=shm_name)
    array = np.ndarray(shape, dtype=dtype, buffer=existing.buf)
    chunk = array[offset:offset + 1000]
    # Process chunk...
    existing.close()
```

---

## NumPy

NumPy vectorization replaces Python-level loops with C-level array operations, delivering 10-100x speedups for numerical code.

### Core Vectorization Pattern

```python
import numpy as np

# Slow: Python loop
def slow_normalize(data):
    mean = sum(data) / len(data)
    std = (sum((x - mean) ** 2 for x in data) / len(data)) ** 0.5
    return [(x - mean) / std for x in data]

# Fast: NumPy vectorized operations
def fast_normalize(data):
    arr = np.array(data, dtype=np.float64)
    mean = arr.mean()
    std = arr.std()
    return (arr - mean) / std
```

### Key Vectorization Techniques

```python
# 1. Boolean indexing instead of conditional loops
data = np.random.randn(1_000_000)
filtered = data[data > 0]  # vs [x for x in data if x > 0]

# 2. Broadcasting instead of nested loops
matrix = np.random.randn(1000, 1000)
vector = np.random.randn(1000)
result = matrix - vector  # vs manual row-by-row subtraction

# 3. Built-in aggregations
np.sum(data[data > 0])  # vs sum(x for x in data if x > 0)
np.percentile(data, [25, 50, 75])

# 4. np.where for conditional assignment
result = np.where(data > 0, data, 0)  # vs loop with if/else

# 5. Avoiding copies with views
small = data[:1000]  # View — no copy
small_sorted = np.sort(data[:1000])  # Copy — sort creates new array
```

### Memory Layout Matters

NumPy performs operations faster on contiguous arrays. `C_CONTIGUOUS` (row-major) is the default and fastest for row-wise operations:

```python
# Check memory layout
arr = np.random.randn(10000, 100)
print(arr.flags)

# Force contiguous layout if needed
arr = np.ascontiguousarray(arr)

# Fortran order (column-major) can be faster for column-wise operations
arr_f = np.asfortranarray(arr)
```

### Pre-allocation

```python
# Bad: growing array in a loop (O(n^2) copies)
result = np.array([])
for chunk in chunks:
    result = np.concatenate([result, chunk])

# Good: pre-allocate and fill
result = np.empty(total_size)
offset = 0
for chunk in chunks:
    size = len(chunk)
    result[offset:offset + size] = chunk
    offset += size
```

---

## Numba

Numba is a JIT compiler that translates Python functions with NumPy arrays into optimized machine code. It requires minimal code changes and can deliver C-like performance.

### Basic Usage

```python
import numba
import numpy as np

@numba.jit(nopython=True)
def mandelbrot(c, max_iter=80):
    """Compute Mandelbrot iterations — benefits enormously from JIT."""
    z = 0
    for n in range(max_iter):
        if abs(z) > 2:
            return n
        z = z * z + c
    return max_iter

@numba.jit(nopython=True, parallel=True)
def compute_fractal(width, height, xmin, xmax, ymin, ymax):
    """Parallel computation across all pixels."""
    result = np.empty((height, width), dtype=np.int64)
    dx = (xmax - xmin) / width
    dy = (ymax - ymin) / height
    for i in numba.prange(height):  # parallel range
        for j in range(width):
            c = complex(xmin + j * dx, ymin + i * dy)
            result[i, j] = mandelbrot(c)
    return result
```

### When Numba Works Well

- Loops over NumPy arrays where the loop body is mathematical
- Functions that use NumPy array operations but also have Python-level loops
- Scientific computing: simulation, linear algebra, statistics, image processing

### When Numba Doesn't Help

- Code already using pure NumPy operations (NumPy's C implementation is already fast)
- Code dominated by I/O or object creation
- Code using Python data structures (dicts, lists of objects) inside the JIT function
- Code with complex Python features (generators, classes with inheritance)

### Compilation Strategy

The first call to a `@jit` function triggers compilation (warmup cost). For hot functions called many times, this cost amortizes. For functions called once or twice, JIT overhead exceeds the benefit:

```python
# Warm up before timing
_ = compute_fractal(10, 10, -2, 2, -2, 2)  # First call compiles

# Now time the compiled version
# timeit will measure the compiled, fast version
```

---

## Cython

Cython compiles Python-like code to C, allowing direct manipulation of C types, C-level arrays, and C function calls. It offers more control than Numba but requires more setup.

### Basic Cython Module

```cython
# fast_ops.pyx
# cython: language_level=3, boundscheck=False, wraparound=False

import numpy as np
cimport numpy as np
from libc.math cimport sqrt, exp

def compute_distance(double[:, :] points_a, double[:, :] points_b):
    """Compute pairwise distances — C-speed with typed memoryviews."""
    cdef int n = points_a.shape[0]
    cdef int m = points_b.shape[0]
    cdef int d = points_a.shape[1]
    cdef double[:, :] result = np.empty((n, m), dtype=np.float64)

    cdef int i, j, k
    cdef double dist_sq

    for i in range(n):
        for j in range(m):
            dist_sq = 0.0
            for k in range(d):
                dist_sq += (points_a[i, k] - points_b[j, k]) ** 2
            result[i, j] = sqrt(dist_sq)

    return np.asarray(result)
```

### Build Configuration

```python
# setup.py
from setuptools import setup, Extension
from Cython.Build import cythonize

extensions = [
    Extension("fast_ops", ["fast_ops.pyx"],
              include_dirs=[np.get_include()])
]

setup(name="fast_ops", ext_modules=cythonize(extensions))
```

Build with: `python setup.py build_ext --inplace`

### When to Use Cython Over Numba

- When you need fine-grained control over memory layout and C operations
- When the function has complex control flow that Numba's compiler doesn't handle well
- When you need to interface directly with C libraries
- When you want to distribute compiled extensions without requiring Numba at runtime

---

## Memory

### `__slots__` for Object-Heavy Code

Python objects have a `__dict__` for dynamic attributes, which costs ~50 bytes per object. `__slots__` eliminates this:

```python
# Without __slots__: each instance has __dict__ (~56 bytes overhead)
class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y

# With __slots__: no __dict__ (~32 bytes overhead)
class SlotPoint:
    __slots__ = ("x", "y")
    def __init__(self, x, y):
        self.x = x
        self.y = y
```

Impact: for 1 million objects, `__slots__` saves ~24 MB of RAM. Worth it for data structures holding millions of small objects.

### `__slots__` Tradeoffs

- Cannot add attributes not listed in `__slots__` (by design — catches typos)
- Inheritance is more complex: child classes need their own `__slots__`
- Cannot use `__dict__`-based features like `pickle` default protocol (but can specify `__getstate__`/`__setstate__`)
- No performance improvement for CPU — this is purely a memory optimization

### Efficient Data Structures

```python
from collections import deque
import array

# deque: O(1) append/pop from both ends (list is O(1) append, O(n) prepend)
buffer = deque(maxlen=1000)  # Fixed-size ring buffer

# array.array: compact typed arrays (vs list of Python objects)
# Stores raw C values, no per-element Python object overhead
ints = array.array("l")  # Signed long
floats = array.array("d")  # Double precision float
bytes_arr = array.array("b")  # Signed byte

# struct: compact binary record packing
import struct
packed = struct.pack(">id", 42, 3.14)  # 12 bytes vs Python objects
value, ratio = struct.unpack(">id", packed)
```

### Memory-Efficient Alternatives

| Python Type | Memory per Element | Efficient Alternative | Memory per Element |
|---|---|---|---|
| `list` of ints | 28 bytes | `array.array("l")` | 8 bytes |
| `list` of floats | 24 bytes | `array.array("d")` | 8 bytes |
| `list` of bools | 28 bytes | `array.array("b")` | 1 byte |
| `dict` with int keys | ~72 bytes/entry | `collections.defaultdict` | ~72 bytes (same) but no key errors |
| Large `list` of objects | ~56 bytes + fields | `__slots__` objects | ~32 bytes + fields |
| NumPy array of int64 | 8 bytes | Already efficient | — |

### Generators and itertools for Pipeline Processing

For data processing pipelines, generators allow processing data element-by-element without loading the full dataset into memory:

```python
import itertools
from itertools import islice, chain, groupby

# Process a multi-GB file line by line
def process_large_file(filepath):
    with open(filepath) as f:
        for line in f:
            yield parse_line(line.strip())

# Chain multiple generators — no intermediate lists
pipeline = (
    row for row in process_large_file("data.csv")
    if row["status"] == "active"
)
pipeline = (
    transform(row) for row in pipeline
    if row.get("value", 0) > threshold
)

# Consume in batches to limit memory
BATCH_SIZE = 1000
while True:
    batch = list(islice(pipeline, BATCH_SIZE))
    if not batch:
        break
    write_batch(batch)
```

---

## Database

### Batch Queries

The most common database performance anti-pattern: executing a query per item in a loop.

```python
# Slow: N+1 queries
for user_id in user_ids:
    user = session.query(User).filter_by(id=user_id).first()
    process(user)

# Fast: single batch query
users = session.query(User).filter(User.id.in_(user_ids)).all()
users_by_id = {u.id: u for u in users}
for user_id in user_ids:
    process(users_by_id[user_id])
```

### Connection Pooling

Opening a new database connection for each request is extremely expensive (TCP handshake, authentication, TLS negotiation). Always use a connection pool:

```python
# SQLAlchemy (built-in pool)
engine = create_engine(
    "postgresql://user:pass@host/db",
    pool_size=10,       # Persistent connections in pool
    max_overflow=20,    # Allow burst above pool_size
    pool_timeout=30,    # Wait time for available connection
    pool_recycle=1800,  # Recycle connections after 30 minutes
)

# psycopg2 (via psycopg2.pool)
from psycopg2 import pool
connection_pool = pool.ThreadedConnectionPool(
    minconn=5, maxconn=20,
    host="localhost", database="mydb"
)
```

### Bulk Inserts

```python
# Slow: row-by-row insert
for record in records:
    session.add(record)
    session.commit()

# Fast: bulk insert (single INSERT statement)
session.bulk_save_objects(records)
session.commit()

# Fastest: raw COPY (PostgreSQL-specific, bypasses ORM)
# Or use executemany with batch size
```

### Query Optimization Checklist

1. Add indexes on columns used in `WHERE`, `JOIN`, and `ORDER BY` clauses
2. Use `EXPLAIN ANALYZE` to verify the query plan uses indexes
3. Select only needed columns (`SELECT id, name` not `SELECT *`)
4. Use pagination (`LIMIT`/`OFFSET` or cursor-based pagination) for large result sets
5. Avoid `LIKE '%pattern%'` (leading wildcard prevents index use) — use full-text search instead
6. Use `JOIN` instead of subqueries when possible (query optimizers handle JOINs better)
7. Consider materialized views or denormalization for frequently-run expensive queries

---

## Caching

### MultiLevel Caching Strategy

```python
import functools
import hashlib
import json

# Level 1: In-process cache (fast, shared within process)
@functools.lru_cache(maxsize=2048)
def compute_cached(key):
    return expensive_computation(key)

# Level 2: Application-level cache (shared across requests)
from cachetools import TTLCache

app_cache = TTLCache(maxsize=1000, ttl=300)  # 5 minute TTL

def get_with_app_cache(key):
    if key not in app_cache:
        app_cache[key] = expensive_computation(key)
    return app_cache[key]

# Level 3: Distributed cache (shared across processes/servers)
# Redis, Memcached — beyond this skill's scope, but the pattern is:
# 1. Check local cache
# 2. Check distributed cache
# 3. Compute and populate both caches
```

### Cache Invalidation

"The only hard problems in computer science are naming, cache invalidation, and off-by-one errors."

Strategies:
- **TTL (Time To Live)**: Set an expiration and let stale data expire naturally. Simple but may serve stale data.
- **Explicit invalidation**: Clear cache when the underlying data changes. Correct but requires discipline.
- **Cache versioning**: Include a version key in the cache key. Increment version when data changes.

```python
def get_user_cache_key(user_id, version):
    return f"user:{user_id}:v{version}"

# When user data changes:
cache_version += 1  # All previous cache entries become invalid
```

---

## Architecture

### Pre-computation and Materialization

When data can be computed once and read many times:

```python
# On application startup, pre-compute expensive lookup tables
LOOKUP_TABLE = build_lookup_table()

# Or use job scheduling to refresh periodically
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler()
scheduler.add_job(refresh_cache, "interval", hours=1)
scheduler.start()
```

### Chunked Processing

For data too large to fit in memory, process in chunks:

```python
import pandas as pd

# Process a multi-GB CSV in chunks
chunk_size = 50_000
for chunk in pd.read_csv("large_file.csv", chunksize=chunk_size):
    processed = transform(chunk)
    processed.to_parquet(f"output/chunk_{chunk_counter}.parquet", index=False)
    chunk_counter += 1
```

### Lazy Loading

Don't compute or load data until it's actually needed:

```python
class LazyLoader:
    """Compute expensive value only when first accessed."""
    def __init__(self, compute_fn):
        self._compute_fn = compute_fn
        self._value = None
        self._computed = False

    @property
    def value(self):
        if not self._computed:
            self._value = self._compute_fn()
            self._computed = True
        return self._value

# Usage
heavy_data = LazyLoader(lambda: load_gigabyte_file("data.json"))
# data isn't loaded until heavy_data.value is accessed
```

### Horizontal Scaling Patterns

When single-process optimization is exhausted:

1. **Task queues**: Celery, RQ, Dramatiq — distribute CPU work across worker processes
2. **Sharding**: Split data across multiple database instances
3. **Read replicas**: Offload read queries to replicated databases
4. **CDN caching**: Cache static/computed results at the edge
5. **Circuit breakers**: Prevent cascading failures when downstream services are slow

---

## Escalation Decision Matrix

> **Goal Drift Control:** When consulting this file, always return to the primary objective — resolving the specific bottleneck identified in SKILL.md Step 3.

| Bottleneck Type | First Escalation | Second Escalation | Last Resort |
|---|---|---|---|
| CPU-bound loop | NumPy vectorization (L3) | Numba JIT (L4) | Cython (L4) |
| I/O-bound wait | threading/ThreadPoolExecutor (L2) | asyncio (L2) | Architectural (L5) |
| Memory pressure | `__slots__` + generators (L3) | Shared memory (L2) | Chunked processing (L5) |
| Database latency | Batch queries + indexes (L3) | Connection pooling (L3) | Read replicas (L5) |
| Mixed CPU+I/O | Separate processes for CPU (L2) | Task queues (L5) | Sharding (L5) |
