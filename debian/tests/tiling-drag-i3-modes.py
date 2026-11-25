#!/usr/bin/python3
"""Drag side of the tiling-drag-i3-modes autopkgtest.

Starts headless sway sessions, opens foot windows and drags them with a
virtual pointer while a virtual keyboard holds modifiers, the way a mouse
and keyboard would. Checks the trees, the focus, the window IPC events and
which window gets typed text against what i3 4.25.1 does with the same
config and gestures. Uses only the Python standard library: the two
virtual devices speak the Wayland wire protocol directly.
"""
import array
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

WORK = sys.argv[1]
LAYOUT = {"splith": "H", "splitv": "V", "tabbed": "T", "stacked": "S"}
MODS = {"shift": 1, "ctrl": 4, "alt": 8, "super": 64}
BTN_LEFT = 0x110
KEY_K, KEY_ENTER = 37, 28
KEYMAP = b"""xkb_keymap {
    xkb_keycodes { include "evdev+aliases(qwerty)" };
    xkb_types { include "complete" };
    xkb_compat { include "complete" };
    xkb_symbols { include "pc+us" };
};
"""
failures = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what, flush=True)
    if not cond:
        failures.append(what)


def wl_string(s):
    b = s.encode() + b"\0"
    return struct.pack("=I", len(b)) + b + b"\0" * ((4 - len(b) % 4) % 4)


class VirtualInput:
    """zwlr_virtual_pointer_v1 + zwp_virtual_keyboard_v1 client."""

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.next_id, self.buf, self.globals = 2, b"", {}
        self.registry = self.new_id()
        self.send(1, 1, struct.pack("=I", self.registry))  # get_registry
        self.roundtrip()
        seat = self.bind("wl_seat")
        pointers = self.bind("zwlr_virtual_pointer_manager_v1")
        keyboards = self.bind("zwp_virtual_keyboard_manager_v1")
        self.ptr, self.kbd = self.new_id(), self.new_id()
        self.send(pointers, 0, struct.pack("=II", seat, self.ptr))
        self.send(keyboards, 0, struct.pack("=II", seat, self.kbd))
        fd = os.memfd_create("keymap")
        os.write(fd, KEYMAP + b"\0")
        self.send(self.kbd, 0, struct.pack("=II", 1, len(KEYMAP) + 1), fd)
        os.close(fd)
        self.roundtrip()
        self.extent = (1, 1)

    def new_id(self):
        self.next_id += 1
        return self.next_id - 1

    def send(self, obj, opcode, payload=b"", fd=None):
        msg = struct.pack("=II", obj, ((8 + len(payload)) << 16) | opcode) + payload
        if fd is None:
            self.sock.sendall(msg)
        else:
            fds = array.array("i", [fd])
            self.sock.sendmsg([msg], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)])

    def bind(self, interface):
        for name, iface in self.globals.items():
            if iface == interface:
                new = self.new_id()
                self.send(self.registry, 0, struct.pack("=I", name) +
                          wl_string(interface) + struct.pack("=II", 1, new))
                return new
        raise RuntimeError(f"sway does not offer {interface}")

    def read(self, n):
        while len(self.buf) < n:
            data = self.sock.recv(65536)
            if not data:
                raise RuntimeError("sway closed the connection")
            self.buf += data

    def roundtrip(self):
        done = self.new_id()
        self.send(1, 0, struct.pack("=I", done))  # sync
        while True:
            self.read(8)
            obj, word = struct.unpack_from("=II", self.buf)
            self.read(word >> 16)
            payload, self.buf = self.buf[8:word >> 16], self.buf[word >> 16:]
            if obj == 1 and word & 0xFFFF == 0:
                raise RuntimeError(f"wayland error: {payload!r}")
            if obj == self.registry and word & 0xFFFF == 0:
                name, n = struct.unpack_from("=II", payload)
                self.globals[name] = payload[8:8 + n - 1].decode()
            if obj == done:
                return

    @staticmethod
    def ms():
        return int(time.monotonic() * 1000) & 0xFFFFFFFF

    def move(self, x, y):
        self.send(self.ptr, 1, struct.pack("=IIIII", self.ms(), x, y, *self.extent))
        self.send(self.ptr, 4)  # frame
        self.roundtrip()

    def button(self, pressed):
        self.send(self.ptr, 2, struct.pack("=III", self.ms(), BTN_LEFT, int(pressed)))
        self.send(self.ptr, 4)
        self.roundtrip()

    def mods(self, names):
        mask = sum(MODS[n] for n in names)
        self.send(self.kbd, 2, struct.pack("=IIII", mask, 0, 0, 0))
        self.roundtrip()

    def key(self, code):
        for state in (1, 0):
            self.send(self.kbd, 1, struct.pack("=III", self.ms(), code, state))
        self.roundtrip()


class Session:
    count = 0

    def __init__(self, lines, outputs=1):
        Session.count += 1
        self.dir = os.path.join(WORK, f"s{Session.count}")
        run = os.path.join(self.dir, "run")
        os.makedirs(run, 0o700)
        self.env = dict(os.environ, XDG_RUNTIME_DIR=run, WLR_HEADLESS_OUTPUTS=str(outputs))
        for var in ("WAYLAND_DISPLAY", "SWAYSOCK"):
            self.env.pop(var, None)
        config = ["xwayland disable", "default_border normal", "focus_follows_mouse no",
                  "mouse_warping none", "floating_modifier Mod4",
                  "output HEADLESS-1 mode --custom 1280x800 position 0 0",
                  "output HEADLESS-2 mode --custom 1280x800 position 1280 0",
                  "workspace 1 output HEADLESS-1", "workspace 2 output HEADLESS-2"] + lines
        with open(os.path.join(self.dir, "config"), "w") as f:
            f.write("\n".join(config) + "\n")
        self.log = open(os.path.join(self.dir, "sway.log"), "w")
        self.proc = subprocess.Popen(["sway", "-c", os.path.join(self.dir, "config")],
                                     env=self.env, stdout=self.log, stderr=subprocess.STDOUT)
        self.children = []
        for _ in range(100):
            names = os.listdir(run)
            ipc = [n for n in names if n.startswith("sway-ipc.")]
            way = [n for n in names if n.startswith("wayland-") and not n.endswith(".lock")]
            if ipc and way:
                self.env["SWAYSOCK"] = os.path.join(run, ipc[0])
                self.env["WAYLAND_DISPLAY"] = way[0]
                if subprocess.run(["swaymsg", "-t", "get_version"], env=self.env,
                                  capture_output=True).returncode == 0:
                    break
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("headless sway did not start")
        self.vin = VirtualInput(os.path.join(run, self.env["WAYLAND_DISPLAY"]))
        self.cmd("workspace 1")

    def close(self):
        for child in self.children:
            child.terminate()
            child.wait()
        self.proc.terminate()
        self.proc.wait()

    def swaymsg(self, *args):
        return subprocess.run(["swaymsg", *args], env=self.env, capture_output=True,
                              text=True).stdout

    def cmd(self, command):
        return json.loads(self.swaymsg("-r", command))

    def tree(self):
        tree = json.loads(self.swaymsg("-t", "get_tree", "-r"))
        self.vin.extent = (tree["rect"]["width"], tree["rect"]["height"])
        return tree

    def nodes(self, node=None):
        node = node or self.tree()
        yield node
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            yield from self.nodes(child)

    def compact(self, node):
        if node.get("app_id"):
            return node["app_id"]
        kids = " ".join(self.compact(c) for c in node.get("nodes", []))
        return LAYOUT.get(node.get("layout"), "?") + f"[{kids}]"

    def layout(self):
        return {n["name"]: self.compact(n) for n in self.nodes()
                if n.get("type") == "workspace" and not n["name"].startswith("__")}

    def focused(self):
        for n in self.nodes():
            if n.get("focused"):
                return n.get("app_id") or n.get("name")
        return None

    def window(self, name):
        for n in self.nodes():
            if n.get("app_id") == name:
                return n
        raise RuntimeError(f"no window {name}")

    def body(self, name):
        r = self.window(name)["rect"]
        return (r["x"] + r["width"] // 2, r["y"] + r["height"] // 2)

    def title(self, name):
        n = self.window(name)
        return (n["rect"]["x"] + n["rect"]["width"] // 2,
                n["rect"]["y"] - n["deco_rect"]["height"] // 2)

    def bottom_edge(self, name):
        r = self.window(name)["rect"]
        return (r["x"] + r["width"] // 2, r["y"] + r["height"] - 3)

    def open(self, *names):
        for name in names:
            self.children.append(subprocess.Popen(
                ["foot", "--app-id", name, "-T", name, "sh", "-c",
                 f"stty -echo; exec cat > '{self.dir}/typed-{name}'"],
                env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            for _ in range(100):
                if any(n.get("app_id") == name and n.get("focused") for n in self.nodes()):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError(f"window {name} did not appear")
        time.sleep(0.3)

    def focus(self, name):
        self.cmd(f'[app_id="^{name}$"] focus')

    def row(self, focus="c"):
        self.open("a", "b", "c")
        self.focus(focus)

    def two_outputs(self, focus):
        self.cmd("workspace 2")
        self.open("c")
        self.cmd("workspace 1")
        self.open("a", "b")
        self.focus(focus)

    def drag(self, start, end, mods=(), mid_mods=None, steps=12):
        self.tree()
        self.vin.mods(mods)
        self.vin.move(*start)
        self.vin.button(True)
        for i in range(1, steps + 1):
            self.vin.move(start[0] + (end[0] - start[0]) * i // steps,
                          start[1] + (end[1] - start[1]) * i // steps)
            if mid_mods is not None and i == steps // 3:
                self.vin.mods(mid_mods)
            time.sleep(0.02)
        self.vin.button(False)
        self.vin.mods(())
        time.sleep(0.4)

    def events(self):
        return Events(self)

    def typed_target(self):
        self.vin.key(KEY_K)
        self.vin.key(KEY_ENTER)
        for _ in range(30):
            for name in sorted(os.listdir(self.dir)):
                path = os.path.join(self.dir, name)
                if name.startswith("typed-") and "k" in open(path).read():
                    os.truncate(path, 0)
                    return name[len("typed-"):]
            time.sleep(0.1)
        return None


class Events:
    """Window events (change:app_id) received while the block runs."""

    def __init__(self, session):
        self.proc = subprocess.Popen(["swaymsg", "-t", "subscribe", "-m", "-r", '["window"]'],
                                     env=session.env, stdout=subprocess.PIPE, text=True)
        self.got = []
        threading.Thread(target=self.read, daemon=True).start()
        time.sleep(0.3)

    def read(self):
        decoder, buf = json.JSONDecoder(), ""
        for chunk in iter(lambda: self.proc.stdout.read(1), ""):
            buf += chunk
            try:
                event, _ = decoder.raw_decode(buf.lstrip())
            except ValueError:
                continue
            buf = ""
            self.got.append(f"{event['change']}:{event['container'].get('app_id')}")

    def stop(self):
        time.sleep(0.4)
        self.proc.terminate()
        self.proc.wait()
        return self.got


def scenario(name, lines, outputs=1):
    def wrap(fn):
        s = Session(lines, outputs)
        try:
            fn(s)
        except Exception as e:  # report and keep going
            check(False, f"{name}: {e!r}")
        finally:
            s.close()
    return wrap


BOTH = ["tiling_drag modifier titlebar"]


@scenario("runtime", BOTH)
def _(s):
    for command, ok in [("tiling_drag titlebar", True), ("tiling_drag modifier titlebar", True),
                        ("tiling_drag swap_modifier Mod1+Shift", True),
                        ("tiling_drag titlebar titlebar", False), ("tiling_drag bogus", False),
                        ("tiling_drag swap_modifier Shift+", False)]:
        reply = s.cmd(command)[0]
        check(reply["success"] is ok, f"runtime: {command} -> {reply}")
    # A rejected swap_modifier keeps the previous one (Mod1+Shift).
    s.row()
    s.drag(s.body("a"), s.body("c"), mods=("super", "shift"))
    check(s.layout()["1"] == "H[b c a]", f"runtime: Shift alone no longer swaps: {s.layout()}")
    s.drag(s.body("b"), s.body("a"), mods=("super", "alt", "shift"))
    check(s.layout()["1"] == "H[a c b]", f"runtime: Mod1+Shift swaps: {s.layout()}")


@scenario("titlebar-move", BOTH)
def _(s):
    s.row()
    ev = s.events()
    s.drag(s.title("a"), s.body("c"))
    got = (s.layout(), s.focused(), ev.stop())
    check(got == ({"1": "H[b c a]"}, "a", ["focus:a", "move:a"]),
          f"title bar drop on the middle puts a after c, focused: {got}")


@scenario("titlebar-shift-swap", BOTH)
def _(s):
    s.row()
    s.drag(s.title("a"), s.body("c"), mods=("shift",))
    got = (s.layout(), s.focused())
    check(got == ({"1": "H[c b a]"}, "a"), f"Shift + title bar drop swaps: {got}")


@scenario("mod-move", BOTH)
def _(s):
    s.row()
    ev = s.events()
    s.drag(s.body("a"), s.body("c"), mods=("super",))
    got = (s.layout(), s.focused(), ev.stop())
    check(got == ({"1": "H[b c a]"}, "c", ["move:a"]),
          f"modifier drop on the middle moves a after c, focus stays on c: {got}")


@scenario("mod-swap", BOTH)
def _(s):
    s.row(focus="b")
    ev = s.events()
    s.drag(s.body("a"), s.body("c"), mods=("super", "shift"))
    got = (s.layout(), s.focused(), sorted(ev.stop()))
    check(got == ({"1": "H[c b a]"}, "b", ["move:a", "move:c"]),
          f"modifier + Shift drop swaps, focus stays on b: {got}")


@scenario("swap-decided-at-press", BOTH)
def _(s):
    s.row()
    s.drag(s.body("a"), s.body("c"), mods=("super",), mid_mods=("super", "shift"))
    check(s.layout()["1"] == "H[b c a]", f"Shift pressed during the drag: {s.layout()}")
    s.drag(s.body("b"), s.body("a"), mods=("super", "shift"), mid_mods=("super",))
    check(s.layout()["1"] == "H[a c b]", f"Shift released during the drag: {s.layout()}")


@scenario("tabbed-target", BOTH)
def _(s):
    s.open("a", "b")
    s.cmd("splith")
    s.cmd("layout tabbed")
    s.open("c")
    s.drag(s.body("a"), s.body("c"), mods=("super",))
    got = (s.layout(), s.focused())
    check(got == ({"1": "H[T[b c a]]"}, "c"), f"drop on a tab's content adds a tab after it: {got}")


@scenario("edge-drop", BOTH)
def _(s):
    s.row()
    s.drag(s.body("a"), s.bottom_edge("c"), mods=("super",))
    got = (s.layout(), s.focused())
    check(got == ({"1": "V[H[b c] a]"}, "c"), f"drop near the bottom edge splits as before: {got}")


@scenario("titlebar-only", ["tiling_drag titlebar", "tiling_drag_threshold 2000"])
def _(s):
    s.row()
    s.drag(s.title("a"), s.body("c"), mods=("super",))
    got = (s.layout(), s.focused())
    check(got == ({"1": "H[a b c]"}, "a"),
          f"titlebar only: Super does not skip the threshold: {got}")
    s.cmd("tiling_drag_threshold 9")
    s.focus("c")
    s.drag(s.body("a"), s.body("c"), mods=("super",))
    check(s.layout()["1"] == "H[a b c]", f"titlebar only: no modifier drag: {s.layout()}")
    s.drag(s.title("a"), s.body("c"))
    check(s.layout()["1"] == "H[b c a]", f"titlebar only: title bar drag: {s.layout()}")


@scenario("modifier-only", ["tiling_drag modifier"])
def _(s):
    s.row()
    s.drag(s.title("a"), s.body("c"))
    got = (s.layout(), s.focused())
    check(got == ({"1": "H[a b c]"}, "a"), f"modifier only: a title bar press only focuses: {got}")


@scenario("swap-modifier-none", BOTH + ["tiling_drag swap_modifier none"])
def _(s):
    s.row()
    s.drag(s.body("a"), s.body("c"), mods=("super", "shift"))
    check(s.layout()["1"] == "H[b c a]", f"swap_modifier none never swaps: {s.layout()}")


@scenario("cross-output-swap", BOTH, outputs=2)
def _(s):
    s.two_outputs("c")
    ev = s.events()
    s.drag(s.body("a"), s.body("c"), mods=("super", "shift"))
    got = (s.layout(), s.focused(), sorted(ev.stop()), s.typed_target())
    check(got == ({"1": "H[c b]", "2": "H[a]"}, "c", ["move:a", "move:c"], "c"),
          f"modifier swap across outputs keeps focus and keyboard on c: {got}")


@scenario("cross-output-titlebar-swap", BOTH, outputs=2)
def _(s):
    s.two_outputs("c")
    s.drag(s.title("a"), s.body("c"), mods=("shift",))
    got = (s.layout(), s.focused(), s.typed_target())
    check(got == ({"1": "H[c b]", "2": "H[a]"}, "a", "a"),
          f"title bar swap across outputs keeps focus and keyboard on a: {got}")


if failures:
    print(f"{len(failures)} check(s) failed")
    sys.exit(1)
