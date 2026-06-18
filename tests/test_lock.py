import pytest
import fakeredis
from unittest.mock import MagicMock, patch

from pottery.exceptions import QuorumNotAchieved, ReleaseUnlockedLock

from configurable_redlock import ConfigurableREDLock, NoTimeoutCheck, ObjectLockTimeout
from configurable_redlock.cache_counter import _RedisCounter


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def redis():
    return fakeredis.FakeRedis()


@pytest.fixture
def mock_redlock():
    """Patches pottery Redlock; yields (cls_mock, instance_mock)."""
    with patch("configurable_redlock.lock._PotteryRedlock") as cls:
        inst = MagicMock()
        inst.acquire.return_value = True
        cls.return_value = inst
        yield cls, inst


def make_lock(redis, **kwargs):
    return ConfigurableREDLock(name="test", redis_client=redis, **kwargs)


# ── __init__ ──────────────────────────────────────────────────────────────────

class TestInit:
    def test_name_is_prefixed(self, redis):
        assert make_lock(redis).name == "ConfigurableLock.test"

    def test_stats_name_defaults_to_name(self, redis):
        assert make_lock(redis).stats_name == "ConfigurableLock.test"

    def test_stats_name_custom(self, redis):
        assert make_lock(redis, stats_name="custom").stats_name == "ConfigurableLock.custom"

    def test_default_auto_release_time(self, redis):
        assert make_lock(redis)._auto_release_time == ConfigurableREDLock.DEFAULT_AUTO_RELEASE_TIME

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
        r1, r2 = fakeredis.FakeRedis(), fakeredis.FakeRedis()
        cl = ConfigurableREDLock(name="test", redis_client=[r1, r2])
        assert cl._get_masters() == {r1, r2}

    def test_get_redis_client_returns_single_client(self, redis):
        assert make_lock(redis)._get_redis_client() is redis


# ── _make_redlock ─────────────────────────────────────────────────────────────

class TestMakeRedlock:
    def test_passes_correct_params(self, redis, mock_redlock):
        cls, _ = mock_redlock
        with make_lock(redis):
            pass
        cls.assert_called_once_with(
            key="ConfigurableLock.test",
            masters={redis},
            auto_release_time=ConfigurableREDLock.DEFAULT_AUTO_RELEASE_TIME,
        )

    def test_passes_custom_auto_release_time(self, redis, mock_redlock):
        cls, _ = mock_redlock
        with make_lock(redis, auto_release_time=10.0):
            pass
        cls.assert_called_once_with(
            key="ConfigurableLock.test",
            masters={redis},
            auto_release_time=10.0,
        )


# ── timeout=0 (wait forever) ──────────────────────────────────────────────────

class TestTimeoutZero:
    def test_acquires_lock(self, redis, mock_redlock):
        with make_lock(redis, timeout=0) as cl:
            assert cl._lock_acquired

    def test_no_cl_call_needed(self, redis, mock_redlock):
        with make_lock(redis, timeout=0):
            pass  # no NoTimeoutCheck raised

    def test_lock_released_on_exit(self, redis, mock_redlock):
        _, inst = mock_redlock
        with make_lock(redis, timeout=0):
            pass
        inst.release.assert_called_once()

    def test_blocks_when_not_immediately_acquired(self, redis, mock_redlock):
        _, inst = mock_redlock
        inst.acquire.side_effect = [False, True]
        with make_lock(redis, timeout=0) as cl:
            assert cl._lock_acquired
        assert inst.acquire.call_count == 2


# ── timeout=-1 (skip if locked) ───────────────────────────────────────────────

class TestTimeoutMinusOne:
    def test_acquires_when_free(self, redis, mock_redlock):
        with make_lock(redis, timeout=-1) as cl:
            cl()
            assert cl._lock_acquired

    def test_raises_object_lock_timeout_when_locked(self, redis, mock_redlock):
        _, inst = mock_redlock
        inst.acquire.return_value = False
        with pytest.raises(ObjectLockTimeout):
            with make_lock(redis, timeout=-1) as cl:
                cl()
                pytest.fail("should not reach here")

    def test_silent_skip_when_locked(self, redis, mock_redlock):
        _, inst = mock_redlock
        inst.acquire.return_value = False
        reached = False
        with make_lock(redis, timeout=-1, silence_object_lock_timeout=True) as cl:
            cl()
            reached = True
        assert not reached

    def test_lock_not_released_when_not_acquired(self, redis, mock_redlock):
        _, inst = mock_redlock
        inst.acquire.return_value = False
        with pytest.raises(ObjectLockTimeout):
            with make_lock(redis, timeout=-1) as cl:
                cl()
        inst.release.assert_not_called()

    def test_no_timeout_check_without_cl(self, redis, mock_redlock):
        with pytest.raises(NoTimeoutCheck):
            with make_lock(redis, timeout=-1):
                pass


# ── timeout=N (wait up to N seconds) ─────────────────────────────────────────

class TestTimeoutPositive:
    def test_acquires_immediately(self, redis, mock_redlock):
        with make_lock(redis, timeout=5) as cl:
            cl()
            assert cl._lock_acquired

    def test_acquires_after_waiting(self, redis, mock_redlock):
        _, inst = mock_redlock
        inst.acquire.side_effect = [False, True]
        with make_lock(redis, timeout=5) as cl:
            cl()
            assert cl._lock_acquired

    def test_raises_object_lock_timeout_when_expired(self, redis, mock_redlock):
        _, inst = mock_redlock
        inst.acquire.side_effect = [False, False]
        with pytest.raises(ObjectLockTimeout):
            with make_lock(redis, timeout=5) as cl:
                cl()

    def test_lock_released_on_exit(self, redis, mock_redlock):
        _, inst = mock_redlock
        with make_lock(redis, timeout=5) as cl:
            cl()
        inst.release.assert_called_once()

    def test_no_timeout_check_without_cl(self, redis, mock_redlock):
        with pytest.raises(NoTimeoutCheck):
            with make_lock(redis, timeout=5):
                pass

    def test_lock_released_on_no_timeout_check(self, redis, mock_redlock):
        _, inst = mock_redlock
        with pytest.raises(NoTimeoutCheck):
            with make_lock(redis, timeout=5):
                pass
        inst.release.assert_called_once()


# ── silence_object_lock_timeout ───────────────────────────────────────────────

class TestSilenceObjectLockTimeout:
    def test_swallows_object_lock_timeout(self, redis, mock_redlock):
        with make_lock(redis, timeout=0, silence_object_lock_timeout=True):
            raise ObjectLockTimeout()

    def test_does_not_swallow_other_exceptions(self, redis, mock_redlock):
        with pytest.raises(ValueError):
            with make_lock(redis, timeout=0, silence_object_lock_timeout=True):
                raise ValueError("other")

    def test_false_propagates_object_lock_timeout(self, redis, mock_redlock):
        with pytest.raises(ObjectLockTimeout):
            with make_lock(redis, timeout=0, silence_object_lock_timeout=False):
                raise ObjectLockTimeout()


# ── _release_lock ─────────────────────────────────────────────────────────────

class TestReleaseLock:
    def test_swallows_release_unlocked_lock(self, redis, mock_redlock):
        _, inst = mock_redlock
        inst.release.side_effect = ReleaseUnlockedLock(key="k", masters=frozenset())
        with make_lock(redis, timeout=0):
            pass  # should not raise

    def test_swallows_quorum_not_achieved(self, redis, mock_redlock):
        _, inst = mock_redlock
        inst.release.side_effect = QuorumNotAchieved(key="k", masters=frozenset())
        with make_lock(redis, timeout=0):
            pass  # should not raise


# ── Statistics ────────────────────────────────────────────────────────────────

class TestStatistics:
    def test_waiting_counter_decremented_after_acquire(self, redis, mock_redlock):
        _, inst = mock_redlock
        inst.acquire.side_effect = [False, True]
        with make_lock(redis, timeout=0):
            pass
        assert int(redis.get("Waiting.ConfigurableLock.test") or 0) == 0

    def test_waiting_key_appended_to_list(self, redis, mock_redlock):
        _, inst = mock_redlock
        inst.acquire.side_effect = [False, True]
        with make_lock(redis, timeout=0):
            pass
        items = redis.lrange("ConfigurableLockKeys", 0, -1)
        assert b'"Waiting.ConfigurableLock.test"' in items

    def test_waiting_key_not_duplicated(self, redis, mock_redlock):
        cl = make_lock(redis, timeout=0)
        cl.set_waiting(True)
        cl.set_waiting(True)
        assert redis.lrange("ConfigurableLockKeys", 0, -1).count(b'"Waiting.ConfigurableLock.test"') == 1

    def test_set_waiting_false_when_not_waiting_is_noop(self, redis):
        cl = make_lock(redis, timeout=0)
        cl.set_waiting(False)  # should not raise


# ── _RedisCounter ─────────────────────────────────────────────────────────────

class TestRedisCounter:
    def test_incr_increments(self, redis):
        c = _RedisCounter("counter", redis)
        c.incr()
        assert int(redis.get("counter")) == 1

    def test_incr_by_step(self, redis):
        c = _RedisCounter("counter", redis)
        c.incr(step=3)
        assert int(redis.get("counter")) == 3

    def test_decr(self, redis):
        c = _RedisCounter("counter", redis)
        c.incr(step=5)
        c.incr(step=-2)
        assert int(redis.get("counter")) == 3

    def test_zero_step_is_noop(self, redis):
        c = _RedisCounter("counter", redis)
        c.incr(step=0)
        assert redis.get("counter") is None
