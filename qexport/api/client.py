from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from qexport.api import endpoints as ep
from qexport.api.models import RawBroadcastJob, RawBroadcastLog, RawComment, RawRoom
from qexport.config import Settings

Api = Literal["omni", "sdk"]
M = TypeVar("M", bound=BaseModel)


class QiscusError(Exception): ...


class AuthError(QiscusError):
    """401/403 on a *global* call -> abort run (resumable)."""

    def __init__(self, api: Api, status: int) -> None:
        super().__init__(f"{api} API rejected credentials (HTTP {status})")
        self.api, self.status = api, status


class RoomAccessError(QiscusError):
    """403/404 on a single room or broadcast job -> mark that item failed, keep going."""

    def __init__(self, room_id: str, status: int) -> None:
        super().__init__(f"access denied for {room_id} (HTTP {status})")
        self.room_id, self.status = room_id, status


class ItemRejectedError(QiscusError):
    """Any other 4xx on a single room or broadcast job (e.g. a campaign that is scheduled, cancelled
    or still sending) -> mark that item failed, keep going. Keeps Qiscus's reason for the report."""

    def __init__(self, item: str, status: int, detail: str = "") -> None:
        msg = f"{item} rejected by Qiscus (HTTP {status})"
        super().__init__(f"{msg}: {detail}" if detail else msg)
        self.item, self.status, self.detail = item, status, detail


class ApiRequestError(QiscusError):
    """Unexpected 4xx on a *global* call -> abort run (resumable) with Qiscus's reason, not a traceback."""

    def __init__(self, endpoint: str, status: int, detail: str = "") -> None:
        msg = f"{endpoint}: Qiscus refused the request (HTTP {status})"
        super().__init__(f"{msg}: {detail}" if detail else msg)
        self.endpoint, self.status, self.detail = endpoint, status, detail


class RateLimitExhausted(QiscusError): ...


class NetworkExhausted(QiscusError):
    """Timeouts / connection errors / 5xx persisted past max retries."""


class SchemaDriftError(QiscusError):
    def __init__(self, endpoint: str, detail: str) -> None:
        super().__init__(f"{endpoint}: response no longer matches the expected schema: {detail}")
        self.endpoint, self.detail = endpoint, detail


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class RateLimiter:
    """Token bucket. capacity=1 -> strict spacing; never bursts."""

    def __init__(self, rate_per_s: float, clock: Clock) -> None:
        if rate_per_s <= 0:
            raise ValueError("rate_per_s must be > 0")
        self._initial = rate_per_s
        self._rate = rate_per_s
        self._clock = clock
        self._next_at: float | None = None

    def acquire(self) -> None:
        now = self._clock.monotonic()
        if self._next_at is not None and now < self._next_at:
            self._clock.sleep(self._next_at - now)
            now = self._next_at
        self._next_at = now + 1.0 / self._rate

    def penalize(self, factor: float = 0.5, floor_rps: float = 0.2) -> None:
        self._rate = max(floor_rps, self._rate * factor)

    def recover(self, step: float = 1.05) -> None:
        self._rate = min(self._initial, self._rate * step)

    @property
    def current_rate(self) -> float:
        return self._rate


class _Transient(Exception):
    def __init__(self, reason: str, is_429: bool = False) -> None:
        super().__init__(reason)
        self.is_429 = is_429


@dataclass
class ClientStats:
    requests: int = 0
    http_429: int = 0
    retries: int = 0
    by_endpoint: dict[str, int] = field(default_factory=dict)


class QiscusClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None,
                 clock: Clock | None = None) -> None:
        self._s = settings
        self._clock = clock or SystemClock()
        self._http = http or httpx.Client(timeout=settings.request_timeout_s)
        self._omni_headers = ep.omni_headers(settings.app_code, settings.admin_token.get_secret_value())
        self._sdk_headers = ep.sdk_headers(settings.app_code, settings.sdk_user_id,
                                           settings.sdk_token.get_secret_value())
        self.rooms_limiter = RateLimiter(settings.rooms_rps, self._clock)
        self.messages_limiter = RateLimiter(settings.messages_rps, self._clock)
        self.broadcast_limiter = RateLimiter(settings.broadcast_rps, self._clock)
        self.stats = ClientStats()

    # ------------------------------------------------------------------ transport

    def _send(self, method: str, url: str, limiter: RateLimiter, label: str,
              **kw) -> httpx.Response:
        retryer = Retrying(
            retry=retry_if_exception_type(_Transient),
            wait=wait_exponential_jitter(initial=2, max=60, jitter=1),
            stop=stop_after_attempt(self._s.max_retry_attempts),
            sleep=self._clock.sleep,
            before_sleep=lambda _rs: self._bump("retries"),
            reraise=True,
        )
        try:
            for attempt in retryer:
                with attempt:
                    limiter.acquire()
                    self.stats.requests += 1
                    self.stats.by_endpoint[label] = self.stats.by_endpoint.get(label, 0) + 1
                    try:
                        resp = self._http.request(method, url, **kw)
                    except httpx.TransportError as e:   # timeouts, connect errors, resets
                        raise _Transient(type(e).__name__) from e
                    if resp.status_code == 429:
                        self.stats.http_429 += 1
                        limiter.penalize()
                        raise _Transient("HTTP 429", is_429=True)
                    if resp.status_code >= 500:
                        raise _Transient(f"HTTP {resp.status_code}")
                    limiter.recover()
                    return resp
        except _Transient as e:
            if e.is_429:
                raise RateLimitExhausted(
                    f"{label}: still rate-limited after {self._s.max_retry_attempts} attempts") from e
            raise NetworkExhausted(f"{label}: {e} after {self._s.max_retry_attempts} attempts") from e
        raise AssertionError("unreachable")

    def _bump(self, name: str) -> None:
        setattr(self.stats, name, getattr(self.stats, name) + 1)

    @staticmethod
    def _json(resp: httpx.Response, endpoint: str) -> dict:
        try:
            return resp.json()
        except ValueError as e:
            raise SchemaDriftError(endpoint, f"body is not JSON ({e})") from e

    @staticmethod
    def _parse(model: type[M], items: object, endpoint: str) -> list[M]:
        if not isinstance(items, list):
            raise SchemaDriftError(endpoint, f"expected a list, got {type(items).__name__}")
        try:
            return [model.model_validate(i) for i in items]
        except ValidationError as e:
            raise SchemaDriftError(endpoint, str(e).splitlines()[0] + " " + _first_error(e)) from e

    @staticmethod
    def _dig(body: dict, endpoint: str, *path: str) -> object:
        cur: object = body
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                raise SchemaDriftError(endpoint, f"missing key {'.'.join(path)}")
            cur = cur[key]
        return cur

    # ------------------------------------------------------------------ omnichannel

    def list_customer_rooms(self, cursor_after: str | None, limit: int = 100
                            ) -> tuple[list[RawRoom], str | None]:
        """POST /api/v2/customer_rooms. Returns (rooms, next_cursor | None)."""
        body: dict[str, object] = {"limit": limit}
        if cursor_after:
            body["cursor_after"] = cursor_after
        resp = self._send("POST", self._s.omni_base_url + ep.CUSTOMER_ROOMS, self.rooms_limiter,
                          "customer_rooms", json=body, headers=self._omni_headers)
        if resp.status_code in (401, 403):
            raise AuthError("omni", resp.status_code)
        if resp.status_code >= 400:
            raise ApiRequestError("customer_rooms", resp.status_code, _detail(resp))
        data = self._json(resp, "customer_rooms")
        rooms = self._parse(RawRoom, self._dig(data, "customer_rooms", "data", "customer_rooms"),
                            "customer_rooms")
        nxt = (data.get("meta") or {}).get("cursor_after")
        return rooms, (str(nxt) if nxt else None)

    def list_broadcast_jobs(self, cursor_after: str | None, limit: int = 100
                            ) -> tuple[list[RawBroadcastJob], str | None]:
        """POST /api/v3/<role>/broadcast_jobs. Newest first."""
        path = ep.BROADCAST_JOBS.format(role=self._s.role)
        body: dict[str, object] = {"limit": limit}
        if cursor_after:
            body["cursor_after"] = cursor_after
        resp = self._send("POST", self._s.omni_base_url + path, self.broadcast_limiter,
                          "broadcast_jobs", json=body, headers=self._omni_headers)
        if resp.status_code in (401, 403):
            raise AuthError("omni", resp.status_code)
        if resp.status_code >= 400:
            raise ApiRequestError("broadcast_jobs", resp.status_code, _detail(resp))
        data = self._json(resp, "broadcast_jobs")
        jobs = self._parse(RawBroadcastJob, self._dig(data, "broadcast_jobs", "data", "broadcast_jobs"),
                           "broadcast_jobs")
        nxt = (data.get("meta") or {}).get("cursor_after")
        return jobs, (str(nxt) if nxt else None)

    def list_broadcast_logs(self, job_id: int, cursor_after: str | None, limit: int = 100
                            ) -> tuple[list[RawBroadcastLog], str | None]:
        """GET /api/v2/<role>/broadcast_logs/<job_id>?pagination=cursor. One row per recipient."""
        path = ep.BROADCAST_LOGS.format(role=self._s.role, job_id=job_id)
        params: dict[str, object] = {"pagination": "cursor", "limit": limit}
        if cursor_after:
            params["cursor_after"] = cursor_after
        resp = self._send("GET", self._s.omni_base_url + path, self.broadcast_limiter,
                          "broadcast_logs", params=params, headers=self._omni_headers)
        if resp.status_code == 401:
            raise AuthError("omni", 401)
        if resp.status_code in (403, 404):
            raise RoomAccessError(f"broadcast job {job_id}", resp.status_code)
        if resp.status_code >= 400:
            raise ItemRejectedError(f"broadcast job {job_id}", resp.status_code, _detail(resp))
        data = self._json(resp, "broadcast_logs")
        logs = self._parse(RawBroadcastLog, self._dig(data, "broadcast_logs", "data", "broadcast_logs"),
                           "broadcast_logs")
        nxt = (data.get("meta") or {}).get("cursor_after")
        return logs, (str(nxt) if nxt else None)

    # ------------------------------------------------------------------ sdk

    def load_comments(self, room_id: str, last_comment_id: int | None, limit: int = 100
                      ) -> list[RawComment]:
        """GET /api/v2/sdk/load_comments?topic_id=…  Newest-first."""
        params: dict[str, object] = {"topic_id": room_id, "limit": limit}
        if last_comment_id is not None:
            params["last_comment_id"] = last_comment_id
        resp = self._send("GET", self._s.sdk_base_url + ep.LOAD_COMMENTS, self.messages_limiter,
                          "load_comments", params=params, headers=self._sdk_headers)
        if resp.status_code == 401:
            raise AuthError("sdk", 401)
        if resp.status_code in (403, 404):
            raise RoomAccessError(room_id, resp.status_code)
        if resp.status_code >= 400:
            raise ItemRejectedError(f"room {room_id}", resp.status_code, _detail(resp))
        data = self._json(resp, "load_comments")
        return self._parse(RawComment, self._dig(data, "load_comments", "results", "comments"),
                           "load_comments")

    def ping(self) -> dict[Api, bool]:
        """One cheap call per API, used by `auth-check`. Never raises."""
        result: dict[Api, bool] = {"omni": False, "sdk": False}
        try:
            self.list_customer_rooms(None, limit=1)
            result["omni"] = True
        except Exception:
            pass
        try:
            resp = self._send("GET", self._s.sdk_base_url + ep.MY_PROFILE, self.messages_limiter,
                              "my_profile", headers=self._sdk_headers)
            result["sdk"] = resp.status_code == 200
        except Exception:
            pass
        return result

    def close(self) -> None:
        self._http.close()


def _detail(resp: httpx.Response) -> str:
    """Qiscus's error body, squashed to one short line for logs and the Export Info sheet."""
    return " ".join((resp.text or "").split())[:200]


def _first_error(e: ValidationError) -> str:
    errs = e.errors()
    if not errs:
        return ""
    first = errs[0]
    return f"(field {'.'.join(str(p) for p in first.get('loc', ()))}: {first.get('msg')})"


def _smoke(print_fn: Callable[[str], None] = print) -> None:
    from qexport.config import load_settings

    c = QiscusClient(load_settings())
    try:
        rooms, cur = c.list_customer_rooms(None, limit=5)
        print_fn(f"rooms page: {len(rooms)} rooms, next cursor={cur!r}")
        if rooms:
            comments = c.load_comments(rooms[0].room_id, None)
            print_fn(f"room {rooms[0].room_id}: {len(comments)} comments on first page")
        jobs, _ = c.list_broadcast_jobs(None, limit=5)
        print_fn(f"broadcast jobs page: {len(jobs)} jobs")
        if jobs:
            logs, _ = c.list_broadcast_logs(jobs[0].id, None, limit=5)
            print_fn(f"job {jobs[0].id} '{jobs[0].name}': {len(logs)} log rows on first page")
    finally:
        c.close()


if __name__ == "__main__":
    import sys

    if "--smoke" in sys.argv:
        _smoke()
