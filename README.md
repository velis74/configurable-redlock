# configurable-redlock

A Python distributed lock built on top of [pottery](https://github.com/brainix/pottery)'s Redlock implementation. It wraps pottery's `Redlock` with a simpler timeout API and adds two features that pottery cannot provide natively: silent skip (skip a `with` block body without `try/except`) and waiting statistics (Redis counters tracking how many processes are waiting per lock key).

## Features

- **Distributed locking via Redis** — uses pottery's Redlock algorithm, safe across multiple processes and machines, with automatic TTL (`auto_release_time`, default 30 s) to prevent deadlocks on process crash.
- **Simple timeout API** — one `timeout` parameter: `0` = wait forever, `-1` = skip if locked, `N` = wait up to N seconds.
- **Silent skip** — `cl()` + `silence_object_lock_timeout=True` skips the block body without `try/except` when the lock is not acquired.
- **Waiting statistics** — while a process waits for the lock, the number of waiters and the set of contended lock keys are tracked in Redis, allowing external monitoring of lock contention.
- **Configurable Redis client** — pass a single `redis.Redis` client or an iterable of clients for true multi-master Redlock quorum. Defaults to `redis.Redis()`.
- **Async support** — `ConfigurableAIOREDLock` provides the same API for `async with` / `await` usage.

## Documentation

Full documentation: https://docs.velis.si/configurable-redlock.html

## Why not just use pottery's Redlock?

> pottery's `Redlock` already covers distributed locking with TTL, timeout, and skip-if-taken. The two things
> `ConfigurableREDLock` adds are a cleaner API for single-Redis setups (one `timeout` parameter instead of
> `context_manager_blocking` + `context_manager_timeout`) and two features pottery cannot provide:
>
> 1. **Silent skip** — skip the `with` block body without a `try/except` around it (see below)
> 2. **Waiting statistics** — Redis counters tracking how many processes are waiting per lock key
>
> The `cl()` call exists solely to enable silent skip: Python's context manager protocol does not allow
> `__enter__` to skip the block body, and raising from `__enter__` prevents `__exit__` from running,
> which means the exception can never be silenced. By deferring the raise to `cl()` inside the block,
> `__exit__` always runs and can swallow the exception.

## Comparison with pottery Redlock

### Timeout

**pottery:**
```python
from pottery import Redlock
from pottery.exceptions import QuorumNotAchieved

try:
    with Redlock(key='res', masters={redis}, context_manager_blocking=True, context_manager_timeout=5):
        do_work()
except QuorumNotAchieved:
    pass  # lock not acquired within 5 seconds
```

**ConfigurableREDLock:**
```python
from configurable_redlock import ConfigurableREDLock, ObjectLockTimeout

try:
    with ConfigurableREDLock(name='res', timeout=5) as cl:
        cl()
        do_work()
except ObjectLockTimeout:
    pass  # lock not acquired within 5 seconds
```

### Silent skip (no try/except)

**pottery** — not possible without `try/except`:
```python
from pottery.exceptions import QuorumNotAchieved

try:
    with Redlock(key='res', masters={redis}, context_manager_blocking=False):
        do_work()
except QuorumNotAchieved:
    pass  # must always handle explicitly
```

**ConfigurableREDLock:**
```python
with ConfigurableREDLock(name='res', timeout=-1, silence_object_lock_timeout=True) as cl:
    cl()      # silently skips everything below if lock not acquired — no try/except needed
    do_work()
```

---

## Installation

```bash
pip install configurable-redlock
```

## Usage

```python
from configurable_redlock import ConfigurableREDLock
```

`ConfigurableREDLock` is used as a context manager. The `timeout` parameter controls what happens when the lock
is already held by another process.

### timeout=0 — wait forever

The process blocks until the lock becomes available. No `cl()` call is needed.

```python
with ConfigurableREDLock(name="my-resource") as cl:
    # lock is always acquired here
    do_work()
```

### timeout=-1 — skip if locked

The idiomatic way to use `timeout=-1` is together with `silence_object_lock_timeout=True`. With this
combination, `cl()` raises `ObjectLockTimeout` when the lock is not acquired, and the exception is
silently swallowed on exit — no `try/except` needed, the rest of the block is simply skipped.

```python
with ConfigurableREDLock(name="my-resource", timeout=-1, silence_object_lock_timeout=True) as cl:
    cl()        # skips everything below if lock was not acquired
    do_work()
```

Moving `cl()` down allows some code to run before the skip — useful for logging that the context
was entered regardless of whether the lock was acquired:

```python
with ConfigurableREDLock(name="my-resource", timeout=-1, silence_object_lock_timeout=True) as cl:
    log.debug("entered my-resource block")
    cl()        # skips everything below if lock was not acquired
    do_work()
```

If you need to distinguish the "skipped" case from success, catch `ObjectLockTimeout` explicitly
instead of using `silence_object_lock_timeout`:

```python
from configurable_redlock import ObjectLockTimeout

try:
    with ConfigurableREDLock(name="my-resource", timeout=-1) as cl:
        cl()
        do_work()
except ObjectLockTimeout:
    log.debug("skipped: lock already held")
```

### timeout=N — wait up to N seconds

The process waits at most N seconds. Call `cl()` as the first line — it raises `ObjectLockTimeout` if
the lock could not be acquired within the given time.

```python
from configurable_redlock import ObjectLockTimeout

try:
    with ConfigurableREDLock(name="my-resource", timeout=5) as cl:
        cl()  # raises ObjectLockTimeout if N seconds elapsed without acquiring
        do_work()
except ObjectLockTimeout:
    pass  # could not acquire within 5 seconds
```

> **Why is `cl()` needed for non-zero timeouts?**
> Python's context manager protocol does not allow `__enter__` to skip the body of a `with` block.
> Calling `cl()` as the first statement is the mechanism that skips the rest of the block when the lock
> was not acquired — it raises `ObjectLockTimeout`, which immediately exits the block.
> If you forget the call entirely, exiting the `with` block raises `NoTimeoutCheck` as a reminder.

### Silencing ObjectLockTimeout on exit

If you want `ObjectLockTimeout` raised *inside* the block to not propagate out:

```python
with ConfigurableREDLock(name="my-resource", timeout=-1, silence_object_lock_timeout=True) as cl:
    cl()
    raise ObjectLockTimeout()  # swallowed on exit
```

## Parameters

| Parameter | Default | Description |
|---|---|---|
| `name` | required | Lock identifier. Stored in Redis as `ConfigurableLock.<name>`. |
| `timeout` | `0` | `0` = wait forever, `-1` = skip if locked, `N` = wait up to N seconds. |
| `auto_release_time` | `30.0` | Lock TTL in seconds. The lock is automatically released after this time, preventing deadlocks if the process crashes. |
| `silence_object_lock_timeout` | `False` | If `True`, an `ObjectLockTimeout` raised inside the `with` block is suppressed on exit. |
| `stats_name` | `name` | Override the name used for waiting-process statistics in Redis. |
| `redis_client` | `redis.Redis()` | A single Redis client or an iterable of clients for multi-master Redlock. Defaults to a localhost connection. |

## Waiting statistics

While a process waits to acquire the lock, `ConfigurableREDLock` tracks the number of waiting processes in Redis:

- `Waiting.ConfigurableLock.<name>` — a counter of currently waiting processes
- `ConfigurableLockKeys` — a Redis list of all keys that have ever had waiters

This allows external monitoring of lock contention.

## Exceptions

| Exception | When raised |
|---|---|
| `ObjectLockTimeout` | Raised by `cl()` when the lock was not acquired (`timeout=-1` or `timeout=N` expired). |
| `NoTimeoutCheck` | Raised on `__exit__` when `timeout != 0` and `cl()` was never called inside the block. |
