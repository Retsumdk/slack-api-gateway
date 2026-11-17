"""TTL cache and the idempotency ledger."""

from __future__ import annotations

from slack_api_gateway.cache import IdempotencyCache, TTLCache, stable_key


def test_values_expire(clock) -> None:
    cache: TTLCache[str] = TTLCache(30.0, clock=clock)
    cache.set("a", "value")
    assert cache.get("a") == "value"
    clock.now += 29
    assert cache.get("a") == "value"
    clock.now += 2
    assert cache.get("a") is None
    assert cache.stats()["misses"] == 1.0


def test_zero_ttl_disables_storage(clock) -> None:
    cache: TTLCache[str] = TTLCache(0.0, clock=clock)
    cache.set("a", "value")
    assert cache.get("a") is None
    assert len(cache) == 0


def test_per_entry_ttl_override(clock) -> None:
    cache: TTLCache[str] = TTLCache(60.0, clock=clock)
    cache.set("short", "v", ttl=1.0)
    cache.set("long", "v")
    clock.now += 2
    assert cache.get("short") is None
    assert cache.get("long") == "v"


def test_oldest_entries_are_evicted_at_the_ceiling(clock) -> None:
    cache: TTLCache[int] = TTLCache(60.0, clock=clock, max_entries=2)
    cache.set("first", 1)
    clock.now += 1
    cache.set("second", 2)
    clock.now += 1
    cache.set("third", 3)
    assert len(cache) == 2
    assert cache.get("first") is None
    assert cache.stats()["evictions"] == 1.0


def test_prune_removes_only_expired_entries(clock) -> None:
    cache: TTLCache[str] = TTLCache(10.0, clock=clock)
    cache.set("old", "v")
    clock.now += 5
    cache.set("new", "v")
    clock.now += 6
    assert cache.prune() == 1
    assert cache.get("new") == "v"


def test_delete_clear_and_membership(clock) -> None:
    cache: TTLCache[str] = TTLCache(60.0, clock=clock)
    cache.set("a", "v")
    assert "a" in cache
    assert cache.delete("a") is True
    assert cache.delete("a") is False
    cache.set("b", "v")
    cache.clear()
    assert len(cache) == 0


def test_hit_rate_is_reported(clock) -> None:
    cache: TTLCache[str] = TTLCache(60.0, clock=clock)
    cache.set("a", "v")
    cache.get("a")
    cache.get("b")
    stats = cache.stats()
    assert stats["hits"] == 1.0
    assert stats["misses"] == 1.0
    assert stats["hit_rate"] == 0.5


def test_idempotency_ledger_replays_and_forgets(clock) -> None:
    ledger = IdempotencyCache(600.0, clock=clock)
    ledger.remember("k", {"ok": True, "ts": "1.0"})
    assert ledger.replay("k") == {"ok": True, "ts": "1.0"}
    assert ledger.forget("k") is True
    assert ledger.replay("k") is None


def test_stable_key_is_order_insensitive_for_dicts() -> None:
    one = stable_key("chat.postMessage", {"channel": "C1", "text": "hi", "blocks": [{"a": 1}]})
    two = stable_key("chat.postMessage", {"blocks": [{"a": 1}], "text": "hi", "channel": "C1"})
    assert one == two
    assert stable_key("m", {"channel": "C1"}) != stable_key("m", {"channel": "C2"})


def test_stable_key_distinguishes_types_and_floats() -> None:
    assert stable_key({"a": 1}) != stable_key({"a": "1"})
    assert stable_key({"a": 1.0}) == stable_key({"a": 1.0})
