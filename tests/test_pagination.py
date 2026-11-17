"""Cursor pagination, including the two ways it must refuse to loop."""

from __future__ import annotations

import pytest

from slack_api_gateway.errors import PaginationError
from slack_api_gateway.pagination import paginate


def scribe(pages):
    """Return a fetch callable that serves the given payloads in order."""
    queue = list(pages)
    seen: list[dict] = []

    def fetch(params):
        seen.append(dict(params))
        return queue.pop(0)

    fetch.seen = seen  # type: ignore[attr-defined]
    return fetch


def test_collects_every_page_in_order() -> None:
    fetch = scribe(
        [
            {
                "ok": True,
                "channels": [{"id": "C1"}, {"id": "C2"}],
                "response_metadata": {"next_cursor": "two"},
            },
            {"ok": True, "channels": [{"id": "C3"}], "response_metadata": {"next_cursor": ""}},
        ]
    )
    items = paginate(fetch, page_size=2, max_pages=5)
    assert [item["id"] for item in items] == ["C1", "C2", "C3"]
    assert fetch.seen[0]["limit"] == 2
    assert "cursor" not in fetch.seen[0]
    assert fetch.seen[1]["cursor"] == "two"


def test_single_page_needs_no_second_request() -> None:
    fetch = scribe([{"ok": True, "members": [{"id": "U1"}]}])
    assert paginate(fetch) == [{"id": "U1"}]
    assert len(fetch.seen) == 1


def test_generic_items_key_is_supported() -> None:
    fetch = scribe([{"ok": True, "items": [{"id": "X"}]}])
    assert paginate(fetch) == [{"id": "X"}]


def test_non_mapping_entries_are_dropped() -> None:
    fetch = scribe([{"ok": True, "messages": [{"id": "M1"}, "junk"]}])
    assert paginate(fetch) == [{"id": "M1"}]


def test_repeated_cursor_raises_instead_of_spinning() -> None:
    fetch = scribe(
        [
            {"ok": True, "channels": [], "response_metadata": {"next_cursor": "stuck"}},
            {"ok": True, "channels": [], "response_metadata": {"next_cursor": "stuck"}},
        ]
    )
    with pytest.raises(PaginationError) as caught:
        paginate(fetch, max_pages=10)
    assert caught.value.code == "pagination_loop"
    assert "stuck" in str(caught.value)


def test_page_cap_raises_rather_than_truncating() -> None:
    pages = [
        {"ok": True, "channels": [{"id": f"C{i}"}], "response_metadata": {"next_cursor": f"p{i}"}}
        for i in range(5)
    ]
    with pytest.raises(PaginationError) as caught:
        paginate(scribe(pages), max_pages=2)
    assert caught.value.code == "pagination_cap"


def test_invalid_bounds_are_rejected() -> None:
    with pytest.raises(ValueError, match="page_size must be positive"):
        paginate(lambda p: {}, page_size=0)
    with pytest.raises(ValueError, match="max_pages must be positive"):
        paginate(lambda p: {}, max_pages=0)
