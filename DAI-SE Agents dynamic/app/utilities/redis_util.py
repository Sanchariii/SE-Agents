"""Thin Redis wrapper for session state, chat memory and refresh tokens.

Falls back to an in-process dict when Redis is unreachable so a developer can run
the service without infrastructure. The fallback is process-local and non-durable,
so it is refused outside local mode.
"""

import json
from functools import lru_cache

from app.config.constants import AppConstants, AppType
from app.config.settings import get_settings
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


class RedisUtil:
    def __init__(self, url: str | None = None) -> None:
        self._mem: dict[str, str] = {}
        self._lists: dict[str, list[str]] = {}
        self._client = None
        try:
            import redis
            self._client = redis.from_url(url or get_settings().redis_url,
                                          decode_responses=True)
            self._client.ping()
            log.info("Redis connected")
        except Exception as exc:                       # noqa: BLE001
            if AppConstants.APP_TYPE != AppType.LOCAL:
                raise RuntimeError(f"Redis is required in hosted mode: {exc}") from exc
            log.warning("Redis unavailable, using in-memory store (local only): %s", exc)
            self._client = None

    # --- strings ---
    def get(self, key: str) -> str | None:
        return self._client.get(key) if self._client else self._mem.get(key)

    def set(self, key: str, value: str) -> None:
        if self._client:
            self._client.set(key, value)
        else:
            self._mem[key] = value

    def set_ex(self, key: str, value: str, ttl_seconds: int) -> None:
        if self._client:
            self._client.setex(key, ttl_seconds, value)
        else:
            self._mem[key] = value                     # in-memory ignores TTL

    def delete(self, key: str) -> None:
        if self._client:
            self._client.delete(key)
        else:
            self._mem.pop(key, None)

    # --- lists ---
    def rpush(self, key: str, value: str) -> None:
        if self._client:
            self._client.rpush(key, value)
        else:
            self._lists.setdefault(key, []).append(value)

    def lrange(self, key: str, start: int = 0, end: int = -1) -> list[str]:
        if self._client:
            return self._client.lrange(key, start, end)
        items = self._lists.get(key, [])
        return items[start:] if end == -1 else items[start:end + 1]

    # --- json ---
    def get_json(self, key: str):
        raw = self.get(key)
        return json.loads(raw) if raw else None

    def set_json(self, key: str, obj) -> None:
        self.set(key, json.dumps(obj, default=str))

    def set_json_ex(self, key: str, obj, ttl_seconds: int) -> None:
        self.set_ex(key, json.dumps(obj, default=str), ttl_seconds)


@lru_cache
def get_redis() -> RedisUtil:
    """One connection pool per process. Every store shares it."""
    return RedisUtil()
