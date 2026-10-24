"""Bounded in-memory history with downsampling for graph queries."""
from __future__ import annotations

import time
from collections import deque
from typing import Iterable, Optional


class History:
    """Ring of (wall_ts, values-dict) samples per module."""

    def __init__(self, maxlen: int = 7200):
        self._buf: deque[tuple[float, dict]] = deque(maxlen=maxlen)

    def append(self, ts: float, values: dict) -> None:
        self._buf.append((ts, dict(values)))

    def clear(self) -> None:
        self._buf.clear()

    def __len__(self) -> int:
        return len(self._buf)

    def window(self, window_s: float) -> list[tuple[float, dict]]:
        cutoff = time.time() - window_s
        return [(t, v) for t, v in self._buf if t >= cutoff]

    def series(self, fields: Iterable[str], window_s: float,
               max_points: int = 1440) -> dict[str, list]:
        """Return {field: [[ts, value|null], ...]} bucket-averaged to <= max_points."""
        fields = list(fields)
        pts = self.window(window_s)
        if not pts or not fields:
            return {f: [] for f in fields}
        if len(pts) <= max_points:
            return {f: [[t, v.get(f)] for t, v in pts] for f in fields}
        # bucket-average downsample; empty buckets emit explicit nulls so
        # graphs never connect across gaps (device loss, suspend, restart)
        n_buckets = max(1, max_points)
        t_start = pts[0][0]
        t_end = time.time()
        span = max(t_end - t_start, 1.0)
        bucket_s = span / n_buckets
        out: dict[str, list] = {f: [] for f in fields}
        acc: dict[int, dict[str, list[float]]] = {}
        bucket_ts = [t_start + (b + 0.5) * bucket_s for b in range(n_buckets)]
        for t, v in pts:
            b = min(max(int((t - t_start) / bucket_s), 0), n_buckets - 1)
            slot = acc.setdefault(b, {f: [] for f in fields})
            for f in fields:
                val = v.get(f)
                if isinstance(val, (int, float)):
                    slot[f].append(float(val))
        for b in range(n_buckets):
            slot = acc.get(b)
            for f in fields:
                vals = slot[f] if slot else []
                out[f].append([bucket_ts[b],
                               sum(vals) / len(vals) if vals else None])
        return out
