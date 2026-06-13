#!/usr/bin/env python3
"""Tiny client for the SpireLink in-game TCP server (line-delimited JSON).

Usage:
    spire_cli.py ping
    spire_cli.py get_state
    spire_cli.py get_map
    spire_cli.py get_deck
    spire_cli.py <cmd> '<json-args>'      # generic: raw args object

Connects to 127.0.0.1:5555 by default (override with SPIRE_HOST / SPIRE_PORT).
"""
import itertools
import json
import os
import socket
import sys

HOST = os.environ.get("SPIRE_HOST", "127.0.0.1")
PORT = int(os.environ.get("SPIRE_PORT", "5555"))
_counter = itertools.count(1)  # thread-safe under the GIL (single atomic next())


def call(cmd, args=None, host=HOST, port=PORT, timeout=20.0):
    """Send one request, return the parsed response dict."""
    req = {"id": next(_counter), "cmd": cmd}
    if args:
        req["args"] = args
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    line = buf.split(b"\n", 1)[0]
    return json.loads(line.decode("utf-8"))


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1]
    args = json.loads(argv[2]) if len(argv) > 2 else None
    try:
        resp = call(cmd, args)
    except (ConnectionRefusedError, OSError) as e:
        print(f"ERROR: cannot reach SpireLink at {HOST}:{PORT} ({e}).", file=sys.stderr)
        print("Is the modded game running? (launch via Steam after install.sh)", file=sys.stderr)
        return 2
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
