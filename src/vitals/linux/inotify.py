"""Minimal Linux inotify via ctypes (stdlib-only)."""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

IN_ACCESS = 0x00000001
IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_ONLYDIR = 0x01000000

_EVENT_SIZE = struct.calcsize("iIII")


class Inotify:
    def __init__(self):
        fd = _libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), "inotify_init1")
        self.fd = fd
        self.wds: dict[int, str] = {}

    def add_watch(self, path: str, mask: int) -> int:
        wd = _libc.inotify_add_watch(self.fd, path.encode(), mask)
        if wd < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), path)
        self.wds[wd] = path
        return wd

    def rm_watch(self, wd: int) -> None:
        _libc.inotify_rm_watch(self.fd, wd)
        self.wds.pop(wd, None)

    def read_events(self) -> list[tuple[int, str]]:
        """Return [(wd, name)]; empty list if nothing readable. Overflow -> (-1, '')."""
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return []
        events: list[tuple[int, str]] = []
        overflow = False
        off = 0
        while off + _EVENT_SIZE <= len(data):
            wd, mask, cookie, length = struct.unpack_from("iIII", data, off)
            off += _EVENT_SIZE
            if mask & 0x00004000:  # IN_Q_OVERFLOW
                overflow = True
            name = data[off:off + length].split(b"\0", 1)[0].decode("utf-8", "replace")
            off += length
            events.append((wd, name))
        if overflow:
            events.append((-1, ""))
        return events

    def close(self) -> None:
        os.close(self.fd)
        self.fd = -1
