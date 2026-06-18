try:
    from pottery import RedisList, Redlock as _PotteryRedlock
    from pottery.exceptions import QuorumNotAchieved, ReleaseUnlockedLock

    _POTTERY_AVAILABLE = True
except ImportError:
    _POTTERY_AVAILABLE = False

from .cache_counter import _RedisCounter
from .exceptions import NoTimeoutCheck, ObjectLockTimeout


class ConfigurableREDLock:
    """
    Simplified Redlock API using pottery's Redlock algorithm with configurable TTL.

    Automatic TTL on the lock key prevents deadlocks when a process dies while
    holding the lock. When timeout != 0, call the lock object as the first
    statement inside the with block:
        with ConfigurableREDLock(name='mylock', timeout=5) as rl:
            rl()
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
        :param redis_client: single Redis client or iterable of clients for multi-master Redlock; defaults to redis.Redis()
        :param auto_release_time: lock TTL in seconds (deadlock prevention). Default: 30s.
        """
        if not _POTTERY_AVAILABLE:
            raise ImportError(
                "pottery is required for ConfigurableREDLock. Install it with: pip install pottery"
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
        import redis

        if self._redis_client is None:
            self._redis_client = redis.Redis()
        if isinstance(self._redis_client, redis.Redis):
            return {self._redis_client}
        return set(self._redis_client)

    def _get_redis_client(self):
        return next(iter(self._get_masters()))

    def _make_redlock(self):
        return _PotteryRedlock(
            key=self.name,
            masters=self._get_masters(),
            auto_release_time=self._auto_release_time,
        )

    def append_waiting_key(self, key):
        r = self._get_redis_client()
        inserted_key = f"Inserted.{key}"
        if not r.exists(inserted_key):
            r.set(inserted_key, 1)
            queue = RedisList(key="ConfigurableLockKeys", redis=r)
            queue.append(key)

    def set_waiting(self, is_waiting):
        if is_waiting:
            if not self.is_waiting:
                self.is_waiting = True
                key = f"Waiting.{self.stats_name}"
                self.waiting_counter = _RedisCounter(key, self._get_redis_client())
                self.waiting_counter.incr()
                self.append_waiting_key(key)
        elif self.is_waiting and not is_waiting:
            self.waiting_counter.incr(step=-1)

    def __enter__(self):
        try:
            self._redlock = self._make_redlock()

            if self.timeout == -1:
                if self._redlock.acquire(blocking=False):
                    self._lock_acquired = True
                else:
                    self.raise_timeout_exception = True
            elif self.timeout == 0:
                if not self._redlock.acquire(blocking=False):
                    self.set_waiting(True)
                    self._redlock.acquire(blocking=True, timeout=-1)
                    self.set_waiting(False)
                self._lock_acquired = True
            else:
                if self._redlock.acquire(blocking=False):
                    self._lock_acquired = True
                else:
                    self.set_waiting(True)
                    acquired = self._redlock.acquire(blocking=True, timeout=self.timeout)
                    self.set_waiting(False)
                    if acquired:
                        self._lock_acquired = True
                    else:
                        self.raise_timeout_exception = True

            return self
        except Exception as e:
            self.set_waiting(False)
            raise e

    def _release_lock(self):
        try:
            self._redlock.release()
        except (ReleaseUnlockedLock, QuorumNotAchieved):
            pass

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.timeout != 0 and not self.timeout_checked:
            if self._lock_acquired:
                self._release_lock()
            raise NoTimeoutCheck()

        if exc_type is ObjectLockTimeout:
            return self.silence_object_lock_timeout

        if self._lock_acquired:
            self._release_lock()
        return None

    def __call__(self, *args, **kwargs):
        self.timeout_checked = True
        if self.raise_timeout_exception:
            raise ObjectLockTimeout()
