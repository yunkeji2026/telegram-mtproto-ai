"""Float32 vector ↔ BLOB helpers + cosine similarity for episodic memory."""

from __future__ import annotations

import struct
from typing import List, Optional


def vec_to_blob(vec: List[float]) -> bytes:
    if not vec:
        return b""
    return struct.pack(f"{len(vec)}f", *vec)


def blob_to_vec(blob: Optional[bytes]) -> Optional[List[float]]:
    if not blob or len(blob) < 8 or len(blob) % 4 != 0:
        return None
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def cosine_similarity(a: Optional[List[float]], b: Optional[List[float]]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na < 1e-12 or nb < 1e-12:
        return 0.0
    return dot / ((na**0.5) * (nb**0.5))
