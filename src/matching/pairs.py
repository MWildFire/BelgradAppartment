"""Independent, one-to-one photo evidence. No model, IO, or database calls."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray


def unique_hash_indices(hashes: Sequence[int], distance: int = 2) -> list[int]:
    """Choose stable representatives of exact/near copies within one album."""
    kept: list[int] = []
    for i in sorted(range(len(hashes)), key=lambda i: (hashes[i], i)):
        if all((hashes[i] ^ hashes[j]).bit_count() > distance for j in kept):
            kept.append(i)
    return kept


def unique_vector_indices(vectors: NDArray[np.float32]) -> list[int]:
    kept: list[int] = []
    for i in range(len(vectors)):
        if np.isfinite(vectors[i]).all() and all(
            float(vectors[i] @ vectors[j]) < 0.9999 for j in kept
        ):
            kept.append(i)
    return kept


def independent_pairs(edges: dict[int, list[tuple[int, float]]]) -> list[tuple[int, int, float]]:
    """Maximum-cardinality bipartite matching, with deterministic edge preferences.

    An augmenting path prevents greedy selection from consuming the only partner of
    another photo. Edge scores are used only within this one evidence scale.
    """
    assigned: dict[int, tuple[int, float]] = {}

    def visit(left: int, seen: set[int]) -> bool:
        for right, score in sorted(edges[left], key=lambda e: (-e[1], e[0])):
            if right in seen:
                continue
            seen.add(right)
            if right not in assigned or visit(assigned[right][0], seen):
                assigned[right] = (left, score)
                return True
        return False

    for left in sorted(edges):
        visit(left, set())
    return sorted((left, right, score) for right, (left, score) in assigned.items())


def phash_pairs(
    left: Sequence[int],
    right: Sequence[int],
    max_hamming: int,
) -> list[tuple[int, int, float]]:
    li = unique_hash_indices(left, min(2, max_hamming))
    ri = unique_hash_indices(right, min(2, max_hamming))
    return independent_pairs(
        {
            i: [
                (j, 1.0 - (left[i] ^ right[j]).bit_count() / 64)
                for j in ri
                if (left[i] ^ right[j]).bit_count() <= max_hamming
            ]
            for i in li
        }
    )


def clip_pairs(
    left: Sequence[NDArray[np.float32]],
    right: Sequence[NDArray[np.float32]],
    threshold: float,
) -> list[tuple[int, int, float]]:
    if not left or not right:
        return []
    a, b = np.stack(left), np.stack(right)
    # Float16 storage perturbs norms. Normalise before copy detection and cosine scoring.
    a = (a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-12)).astype(np.float32)
    b = (b / np.maximum(np.linalg.norm(b, axis=1, keepdims=True), 1e-12)).astype(np.float32)
    li, ri = unique_vector_indices(a), unique_vector_indices(b)
    sim = a @ b.T
    return independent_pairs(
        {i: [(j, float(sim[i, j])) for j in ri if float(sim[i, j]) >= threshold] for i in li}
    )
