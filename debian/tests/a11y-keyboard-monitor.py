#!/usr/bin/python3
"""Screen-reader side of the a11y-keyboard-monitor autopkgtest.

Acts like Orca: owns org.gnome.Orca.KeyboardMonitor, calls the
org.freedesktop.a11y.KeyboardMonitor methods on sway and listens for
KeyEvent signals while wtype types F5 and F6. The sway config binds F5 and
F6 to commands that create marker files in the work directory.
"""
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

MANAGER = "org.freedesktop.a11y.Manager"
PATH = "/org/freedesktop/a11y/Manager"
IFACE = "org.freedesktop.a11y.KeyboardMonitor"
ORCA_NAME = "org.gnome.Orca.KeyboardMonitor"
KEY_F5, KEY_F6 = 0xFFC2, 0xFFC3
EXPECTED = {
    "GrabKeyboard": "", "UngrabKeyboard": "", "WatchKeyboard": "",
    "UnwatchKeyboard": "", "SetKeyGrabs": "aua(uu)", "KeyEvent": "buuuq",
}

work = sys.argv[1]
failures = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what, flush=True)
    if not cond:
        failures.append(what)


def connect():
    return Gio.DBusConnection.new_for_address_sync(
        os.environ["DBUS_SESSION_BUS_ADDRESS"],
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)


def bus_call(conn, method, args=None, reply=None):
    return conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                          "org.freedesktop.DBus", method, args,
                          GLib.VariantType(reply) if reply else None,
                          Gio.DBusCallFlags.NONE, 2000, None)


def call(conn, method, args=None):
    """Return None on success, the D-Bus error name otherwise."""
    try:
        conn.call_sync(MANAGER, PATH, IFACE, method, args, None,
                       Gio.DBusCallFlags.NONE, 1000, None)
        return None
    except GLib.Error as error:
        return Gio.DBusError.get_remote_error(error)


def pump(seconds):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while ctx.iteration(False):
            pass
        time.sleep(0.02)


def marker(key):
    return os.path.join(work, key)


def tap(key):
    for name in ("F5", "F6"):
        if os.path.exists(marker(name)):
            os.unlink(marker(name))
    events.clear()
    subprocess.run(["wtype", "-k", key], check=True)
    pump(1.5)


events = []

conn = connect()
deadline = time.monotonic() + 5
while not bus_call(conn, "NameHasOwner", GLib.Variant("(s)", (MANAGER,)),
                   "(b)").unpack()[0]:
    if time.monotonic() > deadline:
        sys.exit(f"FAIL {MANAGER} is not on the session bus")
    time.sleep(0.1)
check(True, f"{MANAGER} is on the session bus")

xml = conn.call_sync(MANAGER, PATH, "org.freedesktop.DBus.Introspectable",
                     "Introspect", None, GLib.VariantType("(s)"),
                     Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
found = {}
for iface in ET.fromstring(xml).iter("interface"):
    if iface.get("name") == IFACE:
        for member in iface:
            found[member.get("name")] = "".join(
                arg.get("type") for arg in member.iter("arg"))
check(found == EXPECTED, f"Introspect lists {IFACE}: {found}")

check(call(conn, "GrabKeyboard") == "org.freedesktop.DBus.Error.AccessDenied",
      f"a caller that does not own {ORCA_NAME} is denied")

tap("F6")
check(os.path.exists(marker("F6")), "with no screen reader, F6 runs its binding")

ret = bus_call(conn, "RequestName", GLib.Variant("(su)", (ORCA_NAME, 4)),
               "(u)").unpack()[0]
check(ret == 1, f"took {ORCA_NAME}")
conn.signal_subscribe(None, IFACE, "KeyEvent", PATH, None,
                      Gio.DBusSignalFlags.NONE,
                      lambda *a: events.append(a[5].unpack()))

check(call(conn, "SetKeyGrabs",
           GLib.Variant("(aua(uu))", ([], [(KEY_F5, 0)]))) is None,
      "SetKeyGrabs([], [(F5, 0)])")
tap("F5")
check([(e[0], e[2]) for e in events] == [(False, KEY_F5), (True, KEY_F5)],
      f"grabbed F5 is reported, press and release: {events}")
check(not os.path.exists(marker("F5")), "grabbed F5 does not run its binding")
tap("F6")
check(events == [], f"F6, neither watched nor grabbed, is not reported: {events}")
check(os.path.exists(marker("F6")), "F6 still runs its binding")

call(conn, "WatchKeyboard")
tap("F6")
check([(e[0], e[2]) for e in events] == [(False, KEY_F6), (True, KEY_F6)],
      f"WatchKeyboard reports F6: {events}")
check(os.path.exists(marker("F6")), "a watched key still runs its binding")
call(conn, "UnwatchKeyboard")

call(conn, "GrabKeyboard")
tap("F6")
check(len(events) == 2 and not os.path.exists(marker("F6")),
      f"GrabKeyboard reports F6 and keeps it from its binding: {events}")
call(conn, "UngrabKeyboard")

call(conn, "SetKeyGrabs", GLib.Variant("(aua(uu))", ([], [])))
tap("F5")
check(events == [] and os.path.exists(marker("F5")),
      "after SetKeyGrabs([], []), F5 runs its binding again")

# A client's grab ends with its bus connection.
call(conn, "GrabKeyboard")
conn.close_sync(None)
time.sleep(2)
tap("F6")
check(os.path.exists(marker("F6")), "the grab ended with the bus connection")

if failures:
    sys.exit(f"{len(failures)} check(s) failed")
