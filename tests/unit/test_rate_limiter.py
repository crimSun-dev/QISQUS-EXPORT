"""Limiters shared across threads: endpoints on one host never add up past the host's ceiling."""

import threading
import time

import httpx
import respx

from qexport.api.client import CoordinatedRateLimiter, Limiters, QiscusClient, RateLimiter, SystemClock
from tests.conftest import OMNI, FakeClock, rooms_page


def _hammer(limiters: list, calls_each: int) -> float:
    t0 = time.monotonic()
    threads = [threading.Thread(target=lambda lim=lim: [lim.acquire() for _ in range(calls_each)])
               for lim in limiters]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return time.monotonic() - t0


def test_rate_limiter_is_thread_safe():
    rl = RateLimiter(40, SystemClock())
    elapsed = _hammer([rl, rl, rl], 8)               # 24 calls at 40/s -> >= 23 intervals
    assert elapsed >= 23 / 40 * 0.95


def test_rooms_and_broadcasts_share_the_omni_host_ceiling(settings):
    settings.rooms_rps = settings.broadcast_rps = 40
    lim = Limiters.from_settings(settings)
    elapsed = _hammer([lim.rooms, lim.broadcast], 10)    # 20 calls, one 40/s host
    assert elapsed >= 19 / 40 * 0.95


def test_messages_use_their_own_host(settings):
    settings.rooms_rps, settings.messages_rps = 20, 20
    lim = Limiters.from_settings(settings)
    elapsed = _hammer([lim.rooms, lim.messages], 10)     # in parallel: ~9 intervals, not 19
    assert elapsed < 19 / 20 * 0.8


def test_429_on_one_endpoint_slows_every_endpoint_on_that_host(settings):
    clock = FakeClock()
    lim = Limiters.from_settings(settings, clock)
    before = lim.rooms.current_rate
    lim.broadcast.penalize()
    assert lim.rooms.current_rate < before
    assert lim.messages.current_rate == settings.messages_rps     # other host unaffected


def test_penalize_makes_the_next_caller_wait():
    clock = FakeClock()
    rl = RateLimiter(10, clock)
    rl.acquire()
    rl.penalize()                    # 5/s now, and the next slot is pushed back
    rl.acquire()
    assert clock.slept and clock.slept[-1] >= 1 / 5


@respx.mock
def test_clients_sharing_limiters_back_off_together(settings):
    clock = FakeClock()
    lim = Limiters.from_settings(settings, clock)
    a = QiscusClient(settings, http=httpx.Client(), clock=clock, limiters=lim)
    b = QiscusClient(settings, http=httpx.Client(), clock=clock, limiters=lim)
    respx.post(f"{OMNI}/api/v2/customer_rooms").mock(side_effect=[
        httpx.Response(429), httpx.Response(200, json=rooms_page([], None))])
    try:
        a.list_customer_rooms(None)
        assert a.stats.http_429 == 1
        assert b.broadcast_limiter.host is a.rooms_limiter.host
        assert isinstance(b.rooms_limiter, CoordinatedRateLimiter)
        assert b.rooms_limiter.current_rate < settings.rooms_rps
    finally:
        a.close()
        b.close()
