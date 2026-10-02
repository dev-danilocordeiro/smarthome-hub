from redis.asyncio import Redis

from smarthome.shared.config import Settings


def create_redis(settings: Settings) -> Redis:
    return Redis.from_url(
        str(settings.redis_url),
        decode_responses=True,
        socket_connect_timeout=settings.readiness_timeout_seconds,
        socket_timeout=settings.readiness_timeout_seconds,
    )
