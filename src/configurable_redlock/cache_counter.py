class _RedisCounter:
    def __init__(self, key, redis_client):
        self._redis = redis_client
        self.key = key

    def incr(self, step=1):
        if step > 0:
            self._redis.incr(self.key, step)
        elif step < 0:
            self._redis.decr(self.key, -step)
