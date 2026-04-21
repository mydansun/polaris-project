from redis.asyncio import Redis

from polaris_api.config import get_settings


def get_redis() -> Redis:
    return Redis.from_url(get_settings().redis_url, decode_responses=True)

