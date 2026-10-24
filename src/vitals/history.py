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
        # bucket-average downsample
        n_buckets = max(1, max_points)
        span = pts[-1][0] - pts[0][0]
        if span <= 0:
            span = 1.0
        bucket_s = span / n_buckets
        out: dict[str, list] = {f: [] for f in fields}
        cur_b = -1
        acc: dict[str, list[float]] = {f: [] for f in fields}
        acc_t: list[float] = []

        def flush(b: int) -> None:
            ts = pts[0][0] + (b + 0.5) * bucket_s
            for f in fields:
                vals = acc[f]
                out[f].append([ts, sum(vals) / len(vals) if vals else None])
                acc[f] = []
            acc_t.clear()

        for t, v in pts:
            b = min(int((t - pts[0][0]) / bucket_s), n_buckets - 1)
            if b != cur_b:
                if cur_b >= 0:
                    flush(cur_b)
                cur_b = b
            for f in fields:
                val = v.get(f)
                if isinstance(val, (int, float)):
                    acc[f].append(float(val))
        if cur_b >= 0:
            flush(cur_b)
        return out
