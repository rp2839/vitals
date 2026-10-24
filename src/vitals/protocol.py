"""Newline-delimited JSON protocol helpers (see docs/DESIGN.md §3)."""
from __future__ import annotations

import json

VERSION = 1
MAX_FRAME = 64 * 1024

ERROR_CODES = {
    "invalid_request", "unsupported_version", "unknown_control",
    "invalid_value", "unavailable", "permission_denied", "timeout",
    "internal",
}


def encode(obj: dict) -> bytes:
    return (json.dumps(obj, separators=(",", ":"), allow_nan=False) + "\n").encode()


class FrameParser:
    """Feed bytes, get whole parsed dicts. Raises ProtocolError on bad input."""

    class ProtocolError(Exception):
        pass

    def __init__(self):
        self._buf = b""

    def feed(self, data: bytes) -> list[dict]:
        self._buf += data
        if len(self._buf) > MAX_FRAME:
            raise self.ProtocolError("frame too large")
        out = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise self.ProtocolError(f"bad json: {e}") from e
            if not isinstance(obj, dict):
                raise self.ProtocolError("frame is not an object")
            out.append(obj)
        return out


def validate_request(obj: dict) -> tuple[str, object]:
    """Return (op, id) or raise ProtocolError."""
    if obj.get("v") != VERSION:
        raise FrameParser.ProtocolError("unsupported_version")
    op = obj.get("op")
    if not isinstance(op, str) or not op:
        raise FrameParser.ProtocolError("missing op")
    return op, obj.get("id")


def reply(req_id, ok: bool, **kw) -> dict:
    out = {"v": VERSION, "id": req_id, "ok": ok}
    out.update(kw)
    return out


def error_reply(req_id, code: str, message: str) -> dict:
    if code not in ERROR_CODES:
        code = "internal"
    return reply(req_id, False, error={"code": code, "message": message})
