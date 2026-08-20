"""Graph-structure generators
"""

from __future__ import annotations

from typing import Callable, Iterable, Sequence

__all__ = ["ring", "line", "clusters", "complete", "star", "REGISTRY", "generate"]


def _ids(n: int, ids: Sequence[int] | None) -> list[int]:
    if ids is not None:
        out = list(ids)
        if len(set(out)) != len(out):
            raise ValueError("ids contain duplicates")
        return out
    if n < 1:
        raise ValueError("n must be >= 1")
    return list(range(1, n + 1))


def ring(n: int | None = None, *, k: int = 1, directed: bool = False,
         ids: Sequence[int] | None = None) -> dict[int, list[int]]:
    """Ring where each node reads its ``k`` nearest neighbours on each side.

    ``directed=True`` gives a cycle in which each node reads only its predecessor.
    ``k=2`` with ``directed=False`` gives the degree-4 ring.

    >>> ring(4)
    {1: [4, 2], 2: [1, 3], 3: [2, 4], 4: [3, 1]}
    >>> ring(4, directed=True)
    {1: [4], 2: [1], 3: [2], 4: [3]}
    """
    order = _ids(n or 0, ids)
    m = len(order)
    if k < 1:
        raise ValueError("k must be >= 1")
    if not directed and 2 * k >= m:
        raise ValueError(f"k={k} too large for a {m}-node ring (2k must be < n)")
    if directed and m < 2:
        raise ValueError("a directed ring needs at least 2 nodes")

    out: dict[int, list[int]] = {}
    for i, node in enumerate(order):
        if directed:
            out[node] = [order[(i - 1) % m]]
        else:
            offs = [-d for d in range(k, 0, -1)] + list(range(1, k + 1))
            out[node] = [order[(i + o) % m] for o in offs]
    return out


def line(n: int | None = None, *, directed: bool = True,
         ids: Sequence[int] | None = None) -> dict[int, list[int]]:
    """Open chain (not strongly connected). Directed by default: each node reads its predecessor only.

    >>> line(3)
    {1: [], 2: [1], 3: [2]}
    """
    order = _ids(n or 0, ids)
    out: dict[int, list[int]] = {}
    for i, node in enumerate(order):
        nb = [] if i == 0 else [order[i - 1]]
        if not directed and i + 1 < len(order):
            nb.append(order[i + 1])
        out[node] = nb
    return out


def complete(n: int | None = None, *, ids: Sequence[int] | None = None) -> dict[int, list[int]]:
    """All-to-all. Useful as a best-case convergence reference."""
    order = _ids(n or 0, ids)
    return {node: [o for o in order if o != node] for node in order}


def star(n: int | None = None, *, hub: int | None = None,
         ids: Sequence[int] | None = None) -> dict[int, list[int]]:
    """Undirected star. Worst-case single point of failure; good for fault tests."""
    order = _ids(n or 0, ids)
    h = order[0] if hub is None else hub
    if h not in order:
        raise ValueError(f"hub {h} is not among the ids {order}")
    leaves = [o for o in order if o != h]
    out = {h: list(leaves)}
    out.update({leaf: [h] for leaf in leaves})
    return out


def clusters(groups: Sequence[Sequence[int]], *, bridges: Sequence[tuple[int, int]] = (),
             intra: str = "complete") -> dict[int, list[int]]:
    """Dense groups joined by explicit bridge edges.

    ``groups`` lists the id sets; ``intra`` is ``"complete"`` or ``"ring"`` within
    each group; ``bridges`` are undirected inter-group links given as id pairs.
    Bridges are declared rather than generated because which agent bridges which
    subnet is a hardware decision.
    """
    out: dict[int, list[int]] = {}
    for grp in groups:
        if intra == "complete":
            sub = complete(ids=grp)
        elif intra == "ring":
            sub = ring(ids=grp) if len(grp) > 2 else complete(ids=grp)
        else:
            raise ValueError(f"unknown intra pattern {intra!r}")
        for k, v in sub.items():
            out.setdefault(k, []).extend(v)

    known = set(out)
    for a, b in bridges:
        missing = {a, b} - known
        if missing:
            raise ValueError(f"bridge {(a, b)} references unknown node(s) {sorted(missing)}")
        out[a].append(b)
        out[b].append(a)

    return {k: sorted(dict.fromkeys(v)) for k, v in out.items()}


REGISTRY: dict[str, Callable[..., dict[int, list[int]]]] = {
    "ring": ring, "line": line, "complete": complete, "star": star, "clusters": clusters,
}


def generate(name: str, params: dict | None = None) -> dict[int, list[int]]:
    """Dispatch a :class:`~vertex.topology.models.StructureSpec`."""
    try:
        fn = REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"unknown generator {name!r}; available: {sorted(REGISTRY)}"
        ) from None
    return fn(**(params or {}))
