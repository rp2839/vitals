"""rfkill state (sysfs) and soft unblock (/dev/rfkill).

The sysfs `soft` attribute is root-only; /dev/rfkill carries a logind
uaccess ACL for the active session, so unblocking goes through it, as
`rfkill unblock bluetooth` does.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

SYSFS_ROOT = Path("/sys/class/rfkill")
DEV = "/dev/rfkill"

TYPE_BLUETOOTH = 2
OP_CHANGE_ALL = 3


def state(kind: str = "bluetooth", root: Path = SYSFS_ROOT) -> str:
    """'hard', 'soft', 'none', or 'absent' when no switch of `kind` exists.
    A hard block wins over a soft one."""
    found = False
    soft = False
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return "absent"
    for e in entries:
        try:
            if (e / "type").read_text().strip() != kind:
                continue
            found = True
            if (e / "hard").read_text().strip() == "1":
                return "hard"
            if (e / "soft").read_text().strip() == "1":
                soft = True
        except OSError:
            continue
    if not found:
        return "absent"
    return "soft" if soft else "none"


def unblock(rf_type: int = TYPE_BLUETOOTH, dev: str = DEV) -> tuple[bool, str | None]:
    """Clear the soft block on every switch of rf_type."""
    # struct rfkill_event { u32 idx; u8 type; u8 op; u8 soft; u8 hard; }
    event = struct.pack("=IBBBB", 0, rf_type, OP_CHANGE_ALL, 0, 0)
    try:
        fd = os.open(dev, os.O_WRONLY | os.O_CLOEXEC)
    except PermissionError:
        return False, "permission_denied"
    except OSError:
        return False, "unavailable"
    try:
        os.write(fd, event)
    except PermissionError:
        return False, "permission_denied"
    except OSError:
        return False, "unavailable"
    finally:
        os.close(fd)
    return True, None
