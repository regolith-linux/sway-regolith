#!/usr/bin/python3
"""Checks of the keyboard-lock-state autopkgtest (30-keyboard-lock-state).

Each scenario runs a fresh sway nested in its own Xvfb through the wlroots
X11 backend, so that sway has a real (not virtual) keyboard. Keys and the
X server's Num Lock / Caps Lock state are driven with XTEST; the X11 backend
passes that state on to sway with every key. Bindings run "nop <tag>" and
are seen through the IPC binding event.

  keyboard-lock-state.py <work directory>
"""
import json
import os
import select
import socket
import struct
import subprocess
import sys
import tempfile
import time

from Xlib import X, display
from Xlib.ext import xtest

SWAY = os.environ.get("SWAY", "/usr/bin/sway")
RUN_COMMAND, SUBSCRIBE, GET_TREE, GET_INPUTS, GET_SEATS = 0, 2, 4, 100, 101
EVENT_INPUT, EVENT_BINDING = 0x80000015, 0x80000005
KC_NUM, KC_CAPS, KC_SHIFT, KC_SUPER = 77, 66, 50, 133
KC_KP1, KC_KP7, KC_Q, KC_1, KC_F12 = 87, 79, 24, 10, 96
NUM, CAPS = 0x10, 0x02

work = sys.argv[1]
failures = []


def check(cond, what, detail=""):
    print(("ok   " if cond else "FAIL ") + what +
          (": " + detail if detail and not cond else ""), flush=True)
    if not cond:
        failures.append(what)


class Ipc:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.buf = b""

    def _read(self, n, timeout):
        end = time.monotonic() + timeout
        while len(self.buf) < n:
            left = end - time.monotonic()
            if left <= 0 or not select.select([self.sock], [], [], left)[0]:
                return None
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("sway closed the IPC socket")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self, timeout):
        head = self._read(14, timeout)
        if head is None:
            return None
        length, kind = struct.unpack("=II", head[6:])
        return kind, json.loads(self._read(length, 10))

    def request(self, kind, payload=""):
        data = payload.encode()
        self.sock.sendall(b"i3-ipc" + struct.pack("=II", len(data), kind) + data)
        while True:
            msg = self.recv(10)
            if msg is None:
                raise TimeoutError("no IPC reply")
            if not msg[0] & 0x80000000:
                return msg[1]


class Session:
    def __init__(self, config, backends="x11"):
        self.dir = tempfile.mkdtemp(dir=work)
        self.procs = []
        r, w = os.pipe()
        self.procs.append(subprocess.Popen(
            ["Xvfb", "-displayfd", str(w), "-screen", "0", "1280x800x24",
             "-nolisten", "tcp", "-noreset"], pass_fds=(w,),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        os.close(w)
        num = b""
        end = time.monotonic() + 30
        while not num.endswith(b"\n") and time.monotonic() < end:
            if select.select([r], [], [], 1)[0]:
                chunk = os.read(r, 16)
                if not chunk:
                    break
                num += chunk
        os.close(r)
        self.display = ":" + num.decode().strip()
        self.runtime = os.path.join(self.dir, "run")
        os.mkdir(self.runtime, 0o700)
        self.sock = os.path.join(self.dir, "ipc.sock")
        with open(os.path.join(self.dir, "config"), "w") as f:
            f.write(config)
        env = dict(os.environ, DISPLAY=self.display,
                   XDG_RUNTIME_DIR=self.runtime, SWAYSOCK=self.sock,
                   WLR_BACKENDS=backends, WLR_X11_OUTPUTS="1",
                   WLR_RENDERER="pixman", WLR_LIBINPUT_NO_DEVICES="1")
        env.pop("WAYLAND_DISPLAY", None)
        self.log = os.path.join(self.dir, "sway.log")
        sway = subprocess.Popen(
            [SWAY, "-c", os.path.join(self.dir, "config"), "-d"], env=env,
            stdout=open(self.log, "w"), stderr=subprocess.STDOUT)
        self.procs.append(sway)
        end = time.monotonic() + 30
        while not os.path.exists(self.sock):
            if sway.poll() is not None or time.monotonic() > end:
                self.close()
                raise RuntimeError("sway did not start")
            time.sleep(0.05)
        self.ipc = Ipc(self.sock)
        self.events = Ipc(self.sock)
        self.events.request(SUBSCRIBE, '["binding", "input"]')
        self.x = display.Display(self.display)
        root = self.x.screen().root
        end = time.monotonic() + 30
        while True:
            wins = [c for c in root.query_tree().children
                    if c.get_attributes().map_state == X.IsViewable]
            if wins:
                break
            if time.monotonic() > end:
                raise RuntimeError("no sway window on the Xvfb")
            time.sleep(0.05)
        xtest.fake_input(self.x, X.MotionNotify, x=640, y=400)
        wins[-1].set_input_focus(X.RevertToPointerRoot, X.CurrentTime)
        self.x.sync()
        self.sync()

    def sync(self):
        time.sleep(0.15)
        self.ipc.request(RUN_COMMAND, "nop sync")

    def tap(self, *keycodes):
        for k in keycodes:
            xtest.fake_input(self.x, X.KeyPress, k)
        for k in reversed(keycodes):
            xtest.fake_input(self.x, X.KeyRelease, k)
        self.x.sync()

    def drain(self, timeout=0.3):
        out = []
        while True:
            msg = self.events.recv(timeout)
            if msg is None:
                return out
            out.append(msg)

    def fired(self):
        self.sync()
        tags = []
        for kind, body in self.drain():
            cmd = body.get("binding", {}).get("command", "")
            if kind == EVENT_BINDING and cmd != "nop sync":
                tags.append(cmd[4:])
        return ",".join(tags) or "-"

    def lock_events(self):
        self.sync()
        return [b for k, b in self.drain()
                if k == EVENT_INPUT and b.get("change") == "xkb_locks"]

    def set_x_lock(self, keycode, mask, on):
        for _ in range(2):
            if bool(self.x.screen().root.query_pointer().mask & mask) == on:
                return
            self.tap(keycode)
            self.sync()
        raise RuntimeError("could not set the X lock state")

    def keyboards(self):
        return [d for d in self.ipc.request(GET_INPUTS)
                if d.get("type") == "keyboard"]

    def locks(self):
        return [(d.get("xkb_num_lock"), d.get("xkb_caps_lock"))
                for d in self.keyboards()]

    def command(self, cmd):
        reply = self.ipc.request(RUN_COMMAND, cmd)
        self.sync()
        return all(r.get("success") for r in reply)

    def close(self):
        for p in reversed(self.procs):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()

    def dump_log(self):
        with open(self.log) as f:
            print("".join(f.readlines()[-40:]))


class Wev:
    """A focused wev window: the locked modifiers the client received."""

    def __init__(self, s):
        env = dict(os.environ, XDG_RUNTIME_DIR=s.runtime,
                   WAYLAND_DISPLAY="wayland-1")
        env.pop("DISPLAY", None)
        self.path = os.path.join(s.dir, "wev.log")
        s.procs.append(subprocess.Popen(
            ["stdbuf", "-oL", "wev", "-f", "wl_keyboard:modifiers"], env=env,
            stdout=open(self.path, "w"), stderr=subprocess.STDOUT))
        end = time.monotonic() + 15
        while not self._focused(s.ipc.request(GET_TREE)):
            if time.monotonic() > end:
                raise RuntimeError("wev did not get the focus")
            time.sleep(0.1)
        s.sync()

    def _focused(self, node):
        if node.get("app_id") == "wev" and node.get("focused"):
            return True
        return any(self._focused(c) for c in
                   node.get("nodes", []) + node.get("floating_nodes", []))

    def locked(self):
        last = None
        with open(self.path) as f:
            for line in f:
                if "locked:" in line:
                    last = int(line.split("locked:")[1].split()[0]
                               .rstrip(":"), 16)
        return last


# config, keys, Caps Lock on, expected tag with Num Lock (off, on)
BINDINGS = [
    ("bindsym KP_End nop nav\nbindsym KP_1 nop num\n", (KC_KP1,), False,
     ("nav", "num")),
    ("bindsym KP_End nop nav\nbindsym Mod2+KP_1 nop num\n", (KC_KP1,), False,
     ("nav", "num")),
    ("bindcode 87 nop code\n", (KC_KP1,), False, ("code", "-")),
    ("bindcode Mod2+87 nop code\n", (KC_KP1,), False, ("-", "code")),
    ("bindsym Mod4+KP_Home nop home\n", (KC_SUPER, KC_KP7), False,
     ("home", "-")),
    ("bindsym --release KP_End nop nav\n"
     "bindsym --release Mod2+KP_1 nop num\n", (KC_KP1,), False,
     ("nav", "num")),
    ("mode pad {\n  bindsym KP_End nop nav\n  bindsym Mod2+KP_1 nop num\n}\n"
     "bindsym F12 mode pad\n", (KC_KP1,), False, ("nav", "num")),
    ("bindcode 24 nop q\n", (KC_Q,), False, ("q", "q")),
    ("bindsym Mod4+q nop letter\n", (KC_SUPER, KC_Q), False,
     ("letter", "letter")),
    ("bindsym Mod4+1 nop one\n", (KC_SUPER, KC_1), False, ("one", "one")),
    ("bindsym Mod4+q nop letter\n", (KC_SUPER, KC_Q), True,
     ("letter", "letter")),
    ("bindsym KP_End nop nav\nbindsym Mod2+KP_1 nop num\n", (KC_KP1,), True,
     ("nav", "num")),
]


def bindings():
    for config, keys, caps, want in BINDINGS:
        s = Session(config)
        try:
            if "mode pad" in config:
                s.tap(KC_F12)
                s.fired()
            if caps:
                s.set_x_lock(KC_CAPS, X.LockMask, True)
            got = []
            for num in (False, True):
                s.set_x_lock(KC_NUM, X.Mod2Mask, num)
                s.drain()
                s.tap(*keys)
                got.append(s.fired())
            name = " / ".join(config.strip().splitlines()) + \
                (" (Caps Lock on)" if caps else "")
            check(tuple(got) == want, "Num Lock off/on: %s -> %s" % (
                name, "/".join(want)), "got " + "/".join(got))
        finally:
            s.close()


def ipc_and_runtime():
    s = Session("")
    try:
        check(s.locks() == [(False, False)],
              "get_inputs: xkb_num_lock and xkb_caps_lock false", str(s.locks()))
        devs = [d for seat in s.ipc.request(GET_SEATS)
                for d in seat["devices"] if d.get("type") == "keyboard"]
        check(devs and all("xkb_num_lock" in d and "xkb_caps_lock" in d
                           for d in devs), "get_seats: devices carry the fields")
        s.drain()
        s.set_x_lock(KC_NUM, X.Mod2Mask, True)
        ev = s.lock_events()
        check(s.locks() == [(True, False)] and len(ev) == 1 and
              ev[0]["input"].get("xkb_num_lock") is True,
              "Num Lock key: xkb_num_lock true, one xkb_locks event",
              "%s, %d events" % (s.locks(), len(ev)))
        s.set_x_lock(KC_CAPS, X.LockMask, True)
        ev = s.lock_events()
        check(s.locks() == [(True, True)] and len(ev) == 1,
              "Caps Lock key: xkb_caps_lock true, one xkb_locks event",
              "%s, %d events" % (s.locks(), len(ev)))
        s.tap(KC_SUPER)
        s.tap(KC_SHIFT)
        check(s.lock_events() == [], "Super and Shift: no xkb_locks event")
        s.set_x_lock(KC_NUM, X.Mod2Mask, False)
        s.set_x_lock(KC_CAPS, X.LockMask, False)
        s.lock_events()

        w = Wev(s)
        s.drain()
        for cmd, want_locked, want in (
                ("input type:keyboard xkb_numlock enabled", NUM,
                 (True, False)),
                ("input * xkb_capslock enabled", NUM | CAPS, (True, True)),
                ('input "0:0:x11-keyboard" xkb_numlock disabled', CAPS,
                 (False, True)),
                ("input type:keyboard xkb_capslock disabled", 0,
                 (False, False))):
            ok = s.command(cmd)
            ev = s.lock_events()
            check(ok and s.locks() == [want] and w.locked() == want_locked
                  and len(ev) == 1, "at run time: " + cmd,
                  "success %s, get_inputs %s, client locked %s, %d events" % (
                      ok, s.locks(), w.locked(), len(ev)))
        s.command("input type:keyboard xkb_numlock enabled")
        s.lock_events()
        s.command('input "1:2:no-such-keyboard" xkb_numlock disabled')
        check(s.locks() == [(True, False)] and s.lock_events() == [],
              "an identifier matching no keyboard changes nothing",
              str(s.locks()))
        s.command("input type:keyboard xkb_numlock disabled")
        s.command("input type:keyboard repeat_rate 31")
        check(s.locks() == [(False, False)] and w.locked() == 0,
              "a later unrelated input command does not apply the lock again",
              "%s, client locked %s" % (s.locks(), w.locked()))
    except Exception:
        s.dump_log()
        raise
    finally:
        s.close()

    s = Session("seat * keyboard_grouping smart\n", backends="x11,x11")
    try:
        check(len(s.keyboards()) == 2, "two X11 keyboards (one group)")
        s.drain()
        s.command("input type:keyboard xkb_numlock enabled")
        ev = s.lock_events()
        check(s.locks() == [(True, False)] * 2 and len(ev) == 2,
              "grouped: both members report Num Lock, one event each",
              "%s, %d events" % (s.locks(), len(ev)))
    finally:
        s.close()


try:
    bindings()
    ipc_and_runtime()
except Exception as e:
    check(False, "probe ran to the end", repr(e))
if failures:
    print("%d check(s) failed" % len(failures))
    sys.exit(1)
