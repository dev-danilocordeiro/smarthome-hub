from redis.asyncio import Redis

from smarthome.shared.config import Settings


def create_redis(settings: Settings, *, socket_timeout: float | None = None) -> Redis:
    """`socket_timeout` overrides the default (the readiness timeout) for clients that
    block on purpose, like a stream consumer waiting for entries."""
    return Redis.from_url(
        str(settings.redis_url),
        decode_responses=True,
        socket_connect_timeout=settings.readiness_timeout_seconds,
        socket_timeout=socket_timeout or settings.readiness_timeout_seconds,
    )
