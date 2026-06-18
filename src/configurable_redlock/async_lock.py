import json

try:
    from pottery import AIORedlock as _PotteryAIORedlock
    from pottery.exceptions import QuorumNotAchieved, ReleaseUnlockedLock

    _POTTERY_AVAILABLE = True
except ImportError:
    _POTTERY_AVAILABLE = False

from .exceptions import NoTimeoutCheck, ObjectLockTimeout


class _AIORedisCounter:
    def __init__(self, key, redis_client):
        self._redis = redis_client
        self.key = key

    async def incr(self, step=1):
        if step > 0:
            await self._redis.incr(self.key, step)
        elif step < 0:
            await self._redis.decr(self.key, -step)


class ConfigurableAIOREDLock:
    """
    Async version of ConfigurableREDLock for use with async with / await lock().

    Uses pottery's native AIORedlock with redis.asyncio clients — no thread-pool
    executor needed. When timeout != 0, await the lock object as the first
    statement inside the block:
        async with ConfigurableAIOREDLock(name='mylock', timeout=5) as rl:
            await rl()
            ...
    """

    DEFAULT_AUTO_RELEASE_TIME = 30.0

    is_waiting = False
    waiting_counter = None

    def __init__(
        self,
        name,
        timeout=0,
        silence_object_lock_timeout=False,
        stats_name=None,
        redis_client=None,
        auto_release_time=None,
    ):
        """
        :param name: lock name
        :param timeout: 0 = wait forever; -1 = skip if locked; N = wait up to N seconds
        :param silence_object_lock_timeout: suppress ObjectLockTimeout on exit
        :param redis_client: redis.asyncio.Redis client or iterable of clients for multi-master Redlock; defaults to redis.asyncio.Redis()
        :param auto_release_time: lock TTL in seconds (deadlock prevention). Default: 30s.
        """
        if not _POTTERY_AVAILABLE:
            raise ImportError(
                "pottery is required for ConfigurableAIOREDLock. Install it with: pip install pottery"
            )

        if timeout is None or (timeout < 0 and timeout != -1):
            raise Exception("Invalid timeout value")

        self.name = "ConfigurableLock." + name
        self.stats_name = "ConfigurableLock." + (stats_name or name)
        self.timeout = timeout
        self.silence_object_lock_timeout = silence_object_lock_timeout
        self.raise_timeout_exception = False
        self.timeout_checked = False
        self._lock_acquired = False
        self._redlock = None
        self._auto_release_time = auto_release_time or self.DEFAULT_AUTO_RELEASE_TIME
        self._redis_client = redis_client

    def _get_masters(self):
        from redis.asyncio import Redis as AIORedis

        if self._redis_client is None:
            self._redis_client = AIORedis()
        if isinstance(self._redis_client, AIORedis):
            return {self._redis_client}
        return set(self._redis_client)

    def _get_redis_client(self):
        return next(iter(self._get_masters()))

    def _make_redlock(self):
        return _PotteryAIORedlock(
            key=self.name,
            masters=self._get_masters(),
            auto_release_time=self._auto_release_time,
        )

    async def append_waiting_key(self, key):
        r = self._get_redis_client()
        inserted_key = f"Inserted.{key}"
        if not await r.exists(inserted_key):
            await r.set(inserted_key, 1)
            await r.rpush("ConfigurableLockKeys", json.dumps(key))

    async def set_waiting(self, is_waiting):
        if is_waiting:
            if not self.is_waiting:
                self.is_waiting = True
                key = f"Waiting.{self.stats_name}"
                self.waiting_counter = _AIORedisCounter(key, self._get_redis_client())
                await self.waiting_counter.incr()
                await self.append_waiting_key(key)
        elif self.is_waiting and not is_waiting:
            await self.waiting_counter.incr(step=-1)

    async def _acquire(self, blocking, timeout=None):
        if timeout is not None:
            return await self._redlock.acquire(blocking=blocking, timeout=timeout)
        return await self._redlock.acquire(blocking=blocking)

    async def _release_lock(self):
        try:
            await self._redlock.release()
        except (ReleaseUnlockedLock, QuorumNotAchieved):
            pass

    async def __aenter__(self):
        try:
            self._redlock = self._make_redlock()

            if self.timeout == -1:
                if await self._acquire(blocking=False):
                    self._lock_acquired = True
                else:
                    self.raise_timeout_exception = True
            elif self.timeout == 0:
                if not await self._acquire(blocking=False):
                    await self.set_waiting(True)
                    await self._acquire(blocking=True, timeout=-1)
                    await self.set_waiting(False)
                self._lock_acquired = True
            else:
                if await self._acquire(blocking=False):
                    self._lock_acquired = True
                else:
                    await self.set_waiting(True)
                    acquired = await self._acquire(blocking=True, timeout=self.timeout)
                    await self.set_waiting(False)
                    if acquired:
                        self._lock_acquired = True
                    else:
                        self.raise_timeout_exception = True

            return self
        except Exception as e:
            await self.set_waiting(False)
            raise e

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.timeout != 0 and not self.timeout_checked:
            if self._lock_acquired:
                await self._release_lock()
            raise NoTimeoutCheck()

        if exc_type is ObjectLockTimeout:
            return self.silence_object_lock_timeout

        if self._lock_acquired:
            await self._release_lock()
        return None

    async def __call__(self, *args, **kwargs):
        self.timeout_checked = True
        if self.raise_timeout_exception:
            raise ObjectLockTimeout()
