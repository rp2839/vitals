"""Kernel uevent netlink listener (NETLINK_KOBJECT_UEVENT).

Used to catch power_supply changes (plug/unplug, charge state) with
sub-500ms latency; inotify on sysfs directories does not see attribute
writes. Requires no privileges for the kernel group (1).
"""
from __future__ import annotations

import os
import socket


def open_uevent_socket(subsystems: tuple[str, ...] = ("power_supply",)) -> socket.socket:
    s = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, 15)  # NETLINK_KOBJECT_UEVENT
    s.bind((os.getuid(), 1))  # group 1 = kernel uevents
    s.setblocking(False)
    return s


def matches(data: bytes, subsystems: tuple[str, ...]) -> bool:
    """True if the uevent datagram belongs to one of the subsystems."""
    try:
        text = data.decode("utf-8", "replace")
    except Exception:
        return False
    fields = {p: True for p in text.split("\0") if p.startswith(
        tuple(f"SUBSYSTEM={x}" for x in subsystems))}
    return bool(fields)
