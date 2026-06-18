import pytest
import fakeredis.aioredis as aioredis
from unittest.mock import AsyncMock, MagicMock, patch

from pottery.exceptions import QuorumNotAchieved, ReleaseUnlockedLock

from configurable_redlock import ConfigurableAIOREDLock, NoTimeoutCheck, ObjectLockTimeout
from configurable_redlock.async_lock import _AIORedisCounter


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def redis():
    return aioredis.FakeRedis()


@pytest.fixture
def mock_redlock():
    """Patches pottery AIORedlock; yields (cls_mock, instance_mock)."""
    with patch("configurable_redlock.async_lock._PotteryAIORedlock") as cls:
        inst = MagicMock()
        inst.acquire = AsyncMock(return_value=True)
        inst.release = AsyncMock()
        cls.return_value = inst
        yield cls, inst


def make_lock(redis, **kwargs):
    return ConfigurableAIOREDLock(name="test", redis_client=redis, **kwargs)


# ── __init__ ──────────────────────────────────────────────────────────────────

class TestInit:
    def test_name_is_prefixed(self, redis):
        assert make_lock(redis).name == "ConfigurableLock.test"

    def test_stats_name_defaults_to_name(self, redis):
        assert make_lock(redis).stats_name == "ConfigurableLock.test"

    def test_stats_name_custom(self, redis):
        assert make_lock(redis, stats_name="custom").stats_name == "ConfigurableLock.custom"

    def test_default_auto_release_time(self, redis):
        assert make_lock(redis)._auto_release_time == ConfigurableAIOREDLock.DEFAULT_AUTO_RELEASE_TIME

    def test_custom_auto_release_time(self, redis):
        assert make_lock(redis, auto_release_time=10.0)._auto_release_time == 10.0

    def test_timeout_none_raises(self, redis):
        with pytest.raises(Exception):
            make_lock(redis, timeout=None)

    def test_timeout_minus_two_raises(self, redis):
        with pytest.raises(Exception):
            make_lock(redis, timeout=-2)

    def test_timeout_zero_valid(self, redis):
        make_lock(redis, timeout=0)

    def test_timeout_minus_one_valid(self, redis):
        make_lock(redis, timeout=-1)

    def test_timeout_positive_valid(self, redis):
        make_lock(redis, timeout=5)


# ── _get_masters ──────────────────────────────────────────────────────────────

class TestGetMasters:
    def test_single_client_returns_set_of_one(self, redis):
        assert make_lock(redis)._get_masters() == {redis}

    def test_list_of_clients_returns_set(self):
        r1, r2 = aioredis.FakeRedis(), aioredis.FakeRedis()
        cl = ConfigurableAIOREDLock(name="test", redis_client=[r1, r2])
        assert cl._get_masters() == {r1, r2}

    def test_get_redis_client_returns_single_client(self, redis):
        assert make_lock(redis)._get_redis_client() is redis


# ── _make_redlock ─────────────────────────────────────────────────────────────

class TestMakeRedlock:
    async def test_passes_correct_params(self, redis, mock_redlock):
        cls, _ = mock_redlock
        async with make_lock(redis):
            pass
        cls.assert_called_once_with(
            key="ConfigurableLock.test",
            masters={redis},
            auto_release_time=ConfigurableAIOREDLock.DEFAULT_AUTO_RELEASE_TIME,
        )

    async def test_passes_custom_auto_release_time(self, redis, mock_redlock):
        cls, _ = mock_redlock
        async with make_lock(redis, auto_release_time=10.0):
            pass
        cls.assert_called_once_with(
            key="ConfigurableLock.test",
            masters={redis},
            auto_release_time=10.0,
        )


# ── timeout=0 (wait forever) ──────────────────────────────────────────────────

class TestTimeoutZero:
    async def test_acquires_lock(self, redis, mock_redlock):
        async with make_lock(redis, timeout=0) as cl:
            assert cl._lock_acquired

    async def test_no_cl_call_needed(self, redis, mock_redlock):
        async with make_lock(redis, timeout=0):
            pass  # no NoTimeoutCheck raised

    async def test_lock_released_on_exit(self, redis, mock_redlock):
        _, inst = mock_redlock
        async with make_lock(redis, timeout=0):
            pass
        inst.release.assert_called_once()

    async def test_blocks_when_not_immediately_acquired(self, redis, mock_redlock):
        _, inst = mock_redlock
        inst.acquire.side_effect = [False, True]
        async with make_lock(redis, timeout=0) as cl:
            assert cl._lock_acquired
        assert inst.acquire.call_count == 2


# ── timeout=-1 (skip if locked) ───────────────────────────────────────────────

class TestTimeoutMinusOne:
    async def test_acquires_when_free(self, redis, mock_redlock):
        async with make_lock(redis, timeout=-1) as cl:
            await cl()
            assert cl._lock_acquired

    async def test_raises_object_lock_timeout_when_locked(self, redis, mock_redlock):
        _, inst = mock_redlock
        inst.acquire.return_value = False
        with pytest.raises(ObjectLockTimeout):
            async with make_lock(redis, timeout=-1) as cl:
                await cl()
                pytest.fail("should not reach here")

    async def test_silent_skip_when_locked(self, redis, mock_redlock):
        _, inst = mock_redlock
        inst.acquire.return_value = False
        reached = False
        async with make_lock(redis, timeout=-1, silence_object_lock_timeout=True) as cl:
            await cl()
            reached = True
        assert not reached

    async def test_lock_not_released_when_not_acquired(self, redis, mock_redlock):
        _, inst = mock_redlock
        inst.acquire.return_value = False
        with pytest.raises(ObjectLockTimeout):
            async with make_lock(redis, timeout=-1) as cl:
                await cl()
        inst.release.assert_not_called()

    async def test_no_timeout_check_without_cl(self, redis, mock_redlock):
        with pytest.raises(NoTimeoutCheck):
            async with make_lock(redis, timeout=-1):
                pass


# ── timeout=N (wait up to N seconds) ─────────────────────────────────────────

class TestTimeoutPositive:
    async def test_acquires_immediately(self, redis, mock_redlock):
        async with make_lock(redis, timeout=5) as cl:
            await cl()
            assert cl._lock_acquired

    async def test_acquires_after_waiting(self, redis, mock_redlock):
        _, inst = mock_redlock
        inst.acquire.side_effect = [False, True]
        async with make_lock(redis, timeout=5) as cl:
            await cl()
            assert cl._lock_acquired

    async def test_raises_object_lock_timeout_when_expired(self, redis, mock_redlock):
        _, inst = mock_redlock
        inst.acquire.side_effect = [False, False]
        with pytest.raises(ObjectLockTimeout):
            async with make_lock(redis, timeout=5) as cl:
                await cl()

    async def test_lock_released_on_exit(self, redis, mock_redlock):
        _, inst = mock_redlock
        async with make_lock(redis, timeout=5) as cl:
            await cl()
        inst.release.assert_called_once()

    async def test_no_timeout_check_without_cl(self, redis, mock_redlock):
        with pytest.raises(NoTimeoutCheck):
            async with make_lock(redis, timeout=5):
                pass

    async def test_lock_released_on_no_timeout_check(self, redis, mock_redlock):
        _, inst = mock_redlock
        with pytest.raises(NoTimeoutCheck):
            async with make_lock(redis, timeout=5):
                pass
        inst.release.assert_called_once()


# ── silence_object_lock_timeout ───────────────────────────────────────────────

class TestSilenceObjectLockTimeout:
    async def test_swallows_object_lock_timeout(self, redis, mock_redlock):
        async with make_lock(redis, timeout=0, silence_object_lock_timeout=True):
            raise ObjectLockTimeout()

    async def test_does_not_swallow_other_exceptions(self, redis, mock_redlock):
        with pytest.raises(ValueError):
            async with make_lock(redis, timeout=0, silence_object_lock_timeout=True):
                raise ValueError("other")

    async def test_false_propagates_object_lock_timeout(self, redis, mock_redlock):
        with pytest.raises(ObjectLockTimeout):
            async with make_lock(redis, timeout=0, silence_object_lock_timeout=False):
                raise ObjectLockTimeout()


# ── _release_lock ─────────────────────────────────────────────────────────────

class TestReleaseLock:
    async def test_swallows_release_unlocked_lock(self, redis, mock_redlock):
        _, inst = mock_redlock
        inst.release.side_effect = ReleaseUnlockedLock(key="k", masters=frozenset())
        async with make_lock(redis, timeout=0):
            pass  # should not raise

    async def test_swallows_quorum_not_achieved(self, redis, mock_redlock):
        _, inst = mock_redlock
        inst.release.side_effect = QuorumNotAchieved(key="k", masters=frozenset())
        async with make_lock(redis, timeout=0):
            pass  # should not raise


# ── Statistics ────────────────────────────────────────────────────────────────

class TestStatistics:
    async def test_waiting_counter_decremented_after_acquire(self, redis, mock_redlock):
        _, inst = mock_redlock
        inst.acquire.side_effect = [False, True]
        async with make_lock(redis, timeout=0):
            pass
        assert int(await redis.get("Waiting.ConfigurableLock.test") or 0) == 0

    async def test_waiting_key_appended_to_list(self, redis, mock_redlock):
        _, inst = mock_redlock
        inst.acquire.side_effect = [False, True]
        async with make_lock(redis, timeout=0):
            pass
        items = await redis.lrange("ConfigurableLockKeys", 0, -1)
        assert b'"Waiting.ConfigurableLock.test"' in items

    async def test_waiting_key_not_duplicated(self, redis, mock_redlock):
        cl = make_lock(redis, timeout=0)
        await cl.set_waiting(True)
        await cl.set_waiting(True)
        assert (await redis.lrange("ConfigurableLockKeys", 0, -1)).count(b'"Waiting.ConfigurableLock.test"') == 1

    async def test_set_waiting_false_when_not_waiting_is_noop(self, redis):
        cl = make_lock(redis, timeout=0)
        await cl.set_waiting(False)  # should not raise


# ── _AIORedisCounter ──────────────────────────────────────────────────────────

class TestAIORedisCounter:
    async def test_incr_increments(self, redis):
        c = _AIORedisCounter("counter", redis)
        await c.incr()
        assert int(await redis.get("counter")) == 1

    async def test_incr_by_step(self, redis):
        c = _AIORedisCounter("counter", redis)
        await c.incr(step=3)
        assert int(await redis.get("counter")) == 3

    async def test_decr(self, redis):
        c = _AIORedisCounter("counter", redis)
        await c.incr(step=5)
        await c.incr(step=-2)
        assert int(await redis.get("counter")) == 3

    async def test_zero_step_is_noop(self, redis):
        c = _AIORedisCounter("counter", redis)
        await c.incr(step=0)
        assert await redis.get("counter") is None
