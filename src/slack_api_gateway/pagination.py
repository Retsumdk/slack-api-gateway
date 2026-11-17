"""Cursor pagination with loop protection.

Slack pages every list endpoint with ``response_metadata.next_cursor``. The
naive loop —

.. code-block:: python

    while cursor:
        page = call(cursor=cursor)
        cursor = page["response_metadata"]["next_cursor"]

— hangs forever if the server ever echoes a cursor back, which happens under
partial failures and middleware bugs. :func:`paginate` makes that impossible:
it records every cursor it has seen and raises :class:`PaginationError` on a
repeat or when ``max_pages`` is exceeded.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .errors import PaginationError


def paginate(
    fetch: Callable[[dict[str, Any]], Mapping[str, Any]],
    *,
    params: Mapping[str, Any] | None = None,
    cursor_param: str = "cursor",
    limit_param: str = "limit",
    page_size: int = 200,
    max_pages: int = 50,
) -> list[dict[str, Any]]:
    """Collect every item across a cursor-paginated Slack endpoint.

    Args:
        fetch: Callable taking a params dict, returning a raw Slack payload.
        params: Base parameters copied into every request.
        page_size: Value sent as ``limit`` (Slack caps most endpoints at 200).
        max_pages: Hard stop; raises instead of silently truncating.

    Returns:
        Flat list of raw item dicts, in server order.

    Raises:
        PaginationError: A cursor repeated, or ``max_pages`` was reached while
            Slack still advertised another page.
    """
    if page_size < 1:
        raise ValueError("page_size must be positive")
    if max_pages < 1:
        raise ValueError("max_pages must be positive")

    base: dict[str, Any] = dict(params or {})
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor = ""

    for page_number in range(1, max_pages + 1):
        request: dict[str, Any] = dict(base)
        request[limit_param] = page_size
        if cursor:
            request[cursor_param] = cursor
        payload = fetch(request)
        raw_items = payload.get("items") if "items" in payload else None
        if raw_items is None:
            for key in ("channels", "members", "messages", "files", "users"):
                candidate = payload.get(key)
                if isinstance(candidate, list):
                    raw_items = candidate
                    break
        if isinstance(raw_items, list):
            items.extend(dict(item) for item in raw_items if isinstance(item, Mapping))

        metadata = payload.get("response_metadata")
        next_cursor = ""
        if isinstance(metadata, Mapping):
            next_cursor = str(metadata.get("next_cursor") or "")
        if not next_cursor:
            return items
        if next_cursor in seen:
            raise PaginationError(
                f"cursor {next_cursor!r} repeated after {page_number} page(s)",
                code="pagination_loop",
            )
        seen.add(next_cursor)
        cursor = next_cursor

    raise PaginationError(
        f"page cap of {max_pages} reached with more results pending; raise max_pages "
        "or narrow the query with a date filter",
        code="pagination_cap",
    )


__all__ = ["paginate"]
