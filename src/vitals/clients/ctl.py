"""vitalsctl — command-line client (panel toggle, status, controls)."""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys

from .bar import connect, fmt_time


def _rpc(op: str, timeout: float = 10.0, **kw) -> dict:
    sock = connect()
    sock.settimeout(timeout)
    try:
        req = {"v": 1, "id": "c1", "op": op}
        req.update(kw)
        sock.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            data = sock.recv(65536)
            if not data:
                raise ConnectionError("daemon closed")
            buf += data
        return json.loads(buf.split(b"\n", 1)[0])
    finally:
        sock.close()


def cmd_status(_args) -> int:
    r = _rpc("snapshot")
    if not r.get("ok"):
        print("error:", r.get("error"), file=sys.stderr)
        return 1
    d = r["data"]
    b = d.get("battery", {})
    p = d.get("power", {})
    print(f"state:      {b.get('state')}")
    print(f"status:     {b.get('status')}")
    pct = b.get("percent")
    print(f"percent:    {pct if pct is None else f'{pct:.1f}%'}")
    pw = b.get("power_w")
    print(f"power:      {pw if pw is None else f'{pw:.1f} W'} ({b.get('power_source')})")
    t = b.get("time_to_empty_s") or b.get("time_to_full_s")
    print(f"time:       {fmt_time(t) or 'n/a'}")
    h = b.get("health_percent")
    print(f"health:     {h if h is None else f'{h:.1f}%'}  cycles: {b.get('cycle_count')}")
    print(f"profile:    {p.get('active_profile')} (of {', '.join(p.get('profiles') or [])})")
    bp = p.get("brightness_percent")
    print(f"brightness: {bp if bp is None else f'{bp:.0f}%'}")
    return 0


def cmd_panel(args) -> int:
    if args.action == "toggle":
        r = _rpc("panel.toggle", panel=args.name)
    else:
        r = _rpc("panel.set", panel=args.name, visible=(args.action == "show"))
    print(json.dumps(r))
    return 0 if r.get("ok") else 1


def cmd_profile(args) -> int:
    if args.name == "get":
        r = _rpc("controls.get")
        print(r["data"]["power.profile"]["value"])
        return 0
    if args.name == "cycle":
        r = _rpc("controls.get")
        prof = r["data"]["power.profile"]
        choices = prof.get("choices") or []
        if not choices:
            print("vitalsctl: no profiles available", file=sys.stderr)
            return 1
        cur = prof.get("value")
        try:
            nxt = choices[(choices.index(cur) + 1) % len(choices)]
        except ValueError:
            nxt = choices[0]
        r = _rpc("controls.set", control="power.profile", value=nxt)
        if r.get("ok"):
            print(nxt)
        return 0 if r.get("ok") else 1
    r = _rpc("controls.set", control="power.profile", value=args.name)
    return 0 if r.get("ok") else 1


def cmd_brightness(args) -> int:
    if args.value == "get":
        r = _rpc("controls.get")
        v = r["data"]["display.brightness"]["value"]
        print(v if v is not None else "n/a")
        return 0
    val = None
    if args.value.startswith(("+", "-")) and args.value[1:].isdigit():
        r = _rpc("controls.get")
        cur = r["data"]["display.brightness"]["value"]
        if cur is None:
            return 1
        val = max(1.0, min(100.0, cur + int(args.value)))
    elif args.value.isdigit():
        val = max(1.0, min(100.0, float(args.value)))
    else:
        print("vitalsctl: brightness must be 1-100, or +/-N", file=sys.stderr)
        return 2
    r = _rpc("controls.set", control="display.brightness", value=val)
    return 0 if r.get("ok") else 1


def cmd_bluetooth(args) -> int:
    if args.verb == "power":
        want = args.arg
        if want == "toggle":
            r = _rpc("controls.get")
            want = "off" if r["data"]["bluetooth.power"]["value"] else "on"
        if want not in ("on", "off"):
            print("vitalsctl: bluetooth power on|off|toggle", file=sys.stderr)
            return 2
        r = _rpc("controls.set", timeout=10.0, control="bluetooth.power",
                 value=(want == "on"))
    else:
        if not args.arg:
            print(f"vitalsctl: bluetooth {args.verb} <MAC>", file=sys.stderr)
            return 2
        r = _rpc("action", timeout=35.0, action=f"bluetooth.{args.verb}",
                 args={"device": args.arg})
    if not r.get("ok"):
        print(f"vitalsctl: {r.get('error', {}).get('message')}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="vitalsctl")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    p = sub.add_parser("panel")
    p.add_argument("action", choices=["toggle", "show", "hide"])
    p.add_argument("name", nargs="?", default="battery",
                   help="panel name (default: battery)")
    p.set_defaults(fn=cmd_panel)
    pr = sub.add_parser("profile")
    pr.add_argument("name", help="'get' or a profile name")
    pr.set_defaults(fn=cmd_profile)
    br = sub.add_parser("brightness")
    br.add_argument("value", help="'get' or a percent 1-100")
    br.set_defaults(fn=cmd_brightness)
    bt = sub.add_parser("bluetooth")
    bt.add_argument("verb", choices=["power", "connect", "disconnect"])
    bt.add_argument("arg", nargs="?", help="on|off|toggle, or a device MAC")
    bt.set_defaults(fn=cmd_bluetooth)
    args = ap.parse_args()
    try:
        return args.fn(args)
    except OSError as e:
        print(f"vitalsctl: cannot reach vitalsd: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
