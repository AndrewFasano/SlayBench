#!/usr/bin/env python3
"""Game-instance lifecycle: launch/stop isolated STS2 instances with hermetic HOMEs.

A GameInstance owns one game process: its SpireLink port, its HOME directory, and its
PID. `fresh()` stamps a brand-new HOME from home_template/ (see make_home_template.sh)
into a temp directory, so every run starts from an identical meta-progression state —
required because run content depends on the save state, not just the run seed.

Safety properties:
- Cross-process arbitration: fresh() takes an exclusive flock on
  /tmp/spirelink_<port>.lock held until stop(); a second eval on the same port fails
  fast instead of killing the first one's game mid-sample.
- Kills are PID- and identity-verified (process cmdline must be the test game) so a
  recycled PID or an unrelated listener is never killed.
- Stale temp HOMEs from crashed prior evals are swept on fresh().

Usage:
    inst = GameInstance(port=5555)
    inst.fresh()        # lock port, stop old instance, stamp fresh HOME, launch, wait
    ... play ...
    inst.stop()         # kill process, release lock; temp HOME removed unless keep_home
"""
import fcntl
import glob
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time

from spire_cli import call

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
LAUNCHER = os.path.join(ROOT, "launch_instance.sh")
TEMPLATE = os.path.join(ROOT, "home_template")
GAME_CMDLINE = "Slay the Spire 2"  # test instances run as "./Slay the Spire 2"


def _pid_is_game(pid: int) -> bool:
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True)
        return GAME_CMDLINE in out.stdout
    except Exception:
        return False


class GameInstance:
    def __init__(self, port=5555, keep_home=False):
        self.port = port
        self.keep_home = keep_home
        self.pid = None
        self.home = None
        self._home_is_temp = False
        self._lock_fh = None

    # ---- cross-process port lock ----

    def _acquire_port_lock(self):
        if self._lock_fh is not None:
            return
        path = f"/tmp/spirelink_{self.port}.lock"
        fh = open(path, "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            raise RuntimeError(
                f"port {self.port} is locked by another eval/process (lock {path}). "
                f"Run on a different SPIRE_PORT or stop the other eval.")
        fh.write(str(os.getpid()))
        fh.flush()
        self._lock_fh = fh

    def _release_port_lock(self):
        if self._lock_fh is not None:
            try:
                fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
                self._lock_fh.close()
            except Exception:
                pass
            self._lock_fh = None

    # ---- lifecycle ----

    def fresh(self, timeout=240.0):
        """Lock the port, stop any prior instance, stamp a fresh HOME, launch, wait ready."""
        if not os.path.isdir(TEMPLATE):
            raise RuntimeError("no home_template/ — run make_home_template.sh first")
        self._acquire_port_lock()
        self.stop(release_lock=False)
        self._kill_port_owner()
        self._sweep_stale_homes()
        home = tempfile.mkdtemp(prefix=f"spirelink_home_{self.port}_")
        shutil.copytree(TEMPLATE, home, dirs_exist_ok=True)
        self.home = home
        self._home_is_temp = True
        self._launch()
        self.wait_ready(timeout)
        return self

    def _launch(self):
        out = subprocess.run(["bash", LAUNCHER, str(self.port), self.home],
                             capture_output=True, text=True, check=True)
        self.pid = int(out.stdout.strip().splitlines()[-1])

    def stop(self, release_lock=True):
        """Kill our game process (identity-verified) and clean up a temp HOME."""
        if self.pid is not None:
            if _pid_is_game(self.pid):
                try:
                    os.kill(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                time.sleep(1.0)
            self.pid = None
        if self.home and self._home_is_temp and not self.keep_home:
            shutil.rmtree(self.home, ignore_errors=True)
            self.home = None
        if release_lock:
            self._release_port_lock()

    def _kill_port_owner(self):
        """Kill a stale GAME instance listening on our port (never anything else)."""
        try:
            out = subprocess.run(["lsof", "-tnP", f"-iTCP:{self.port}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True)
            killed = False
            for line in out.stdout.split():
                try:
                    pid = int(line)
                except ValueError:
                    continue
                if _pid_is_game(pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                        killed = True
                    except ProcessLookupError:
                        pass
                else:
                    raise RuntimeError(
                        f"port {self.port} is held by a non-game process (pid {pid}); "
                        f"refusing to kill it — choose another SPIRE_PORT")
            if killed:
                time.sleep(2.0)
        except FileNotFoundError:
            pass

    def _sweep_stale_homes(self):
        """Remove temp HOMEs leaked by crashed prior evals on this port."""
        pattern = os.path.join(tempfile.gettempdir(), f"spirelink_home_{self.port}_*")
        for d in glob.glob(pattern):
            if d != self.home:
                shutil.rmtree(d, ignore_errors=True)

    # ---- readiness / RPC ----

    def ping(self):
        try:
            r = call("ping", port=self.port, timeout=5)
            return bool(r.get("ok"))
        except (OSError, ValueError, json.JSONDecodeError):
            return False

    def wait_ready(self, timeout=240.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.pid is not None:
                # crashed at startup? surface it instead of waiting out the clock
                try:
                    os.kill(self.pid, 0)
                except ProcessLookupError:
                    hint = ""
                    try:
                        steam = subprocess.run(["pgrep", "-x", "steam_osx"],
                                               capture_output=True)
                        if steam.returncode != 0:
                            hint = (" — the Steam client is not running, which makes "
                                    "Steamworks init fail and the game exit; start "
                                    "Steam and retry")
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"game process {self.pid} exited during startup "
                        f"(log: /tmp/sts2_{self.port}.log){hint}")
            if self.ping():
                return
            time.sleep(2.0)
        raise TimeoutError(f"game not ready on port {self.port} after {timeout}s")

    def rpc(self, cmd, args=None, timeout=90):
        r = call(cmd, args, port=self.port, timeout=timeout)
        if not r.get("ok"):
            raise RuntimeError(f"{cmd}: {r.get('error')}")
        return r["data"]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
