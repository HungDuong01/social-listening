"""Structure-agnostic search through captured JSON.

Platform GraphQL schemas change without notice, so nothing here hard-codes a
path like ``data.feedback.reaction_count``. Instead a caller supplies a
predicate describing what a post-shaped node looks like, and the miner walks
the whole document to find every match - which keeps adapters resilient when a
vendor renames a wrapper key.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Iterator

Predicate = Callable[[dict[str, Any]], bool]


def iter_json_nodes(
    payload: Any,
    predicate: Predicate,
    *,
    max_depth: int = 40,
    max_visits: int = 200_000,
) -> Iterator[dict[str, Any]]:
    """Yield every dict satisfying ``predicate``, in document order.

    Traversal is iterative (deep payloads would blow the recursion limit) and
    continues *into* matches, because nested structures such as a thread's
    reply list live inside the node that matched first.
    """
    stack: list[tuple[Any, int]] = [(payload, 0)]
    visits = 0

    while stack:
        node, depth = stack.pop()
        visits += 1
        if visits > max_visits or depth > max_depth:
            continue

        if isinstance(node, dict):
            if predicate(node):
                yield node
            children: Iterable[Any] = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue

        # Reversed so the stack pops children back in their original order.
        for child in reversed(list(children)):
            if isinstance(child, (dict, list)):
                stack.append((child, depth + 1))


def find_first_node(payload: Any, predicate: Predicate, **kwargs: Any) -> dict[str, Any] | None:
    for node in iter_json_nodes(payload, predicate, **kwargs):
        return node
    return None


def collect_nodes(
    payloads: Iterable[Any],
    predicate: Predicate,
    *,
    key: Callable[[dict[str, Any]], str | None],
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Mine several payloads, de-duplicating by ``key`` and keeping first-seen order.

    A later sighting of the same node replaces the earlier one only when it
    carries more keys, since partial stubs of a post routinely appear in
    listing responses before the fully hydrated version arrives.
    """
    collected: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        for node in iter_json_nodes(payload, predicate):
            identity = key(node)
            if not identity:
                continue
            existing = collected.get(identity)
            if existing is None:
                collected[identity] = node
            elif _richness(node) > _richness(existing):
                collected[identity] = node
    values = list(collected.values())
    return values[:limit] if limit is not None else values


def _richness(node: dict[str, Any]) -> int:
    """Rough completeness score - more populated keys means a better record."""
    return sum(1 for value in node.values() if value not in (None, "", [], {}))
