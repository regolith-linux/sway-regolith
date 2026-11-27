#!/usr/bin/python3
"""28-xwayland-dock-windows: X11 docks sit at an output edge, like in i3.

Starts headless sway with Xwayland on 1280x800 outputs and maps python3-xlib
windows of type _NET_WM_WINDOW_TYPE_DOCK. Checks where docks are placed, the
space they reserve (the workspace rectangle reported over IPC), that they
never join a workspace or take the focus, and how they follow height changes,
unmapping, type changes, workspace switches and output removal.
29-xwayland-map-on-late-association: a dock mapped again many times with an
unchanged geometry shows up every time.
"""
import glob
import json
import os
import subprocess
import tempfile
import time

from Xlib import X, Xatom, display

W, H = 1280, 800
failures = []


def check(name, observed, expected):
    ok = observed == expected
    print(f'{"PASS" if ok else "FAIL"} {name}: {observed!r}'
          + ('' if ok else f' (expected {expected!r})'), flush=True)
    if not ok:
        failures.append(name)


def until(fn, expected, timeout=5.0):
    deadline = time.monotonic() + timeout
    value = fn()
    while value != expected and time.monotonic() < deadline:
        time.sleep(0.1)
        value = fn()
    return value


def settle(fn, delay=1.0):
    time.sleep(delay)
    return fn()


class Client:
    """An X11 client with one top-level window, on its own connection."""

    def __init__(self, sway, name, x=0, y=0, width=W, height=30, dock=True,
                 top=0, bottom=0):
        self.d = display.Display(sway.env['DISPLAY'])
        screen = self.d.screen()
        self.root = screen.root
        self.w = screen.root.create_window(
            x, y, width, height, 0, screen.root_depth, X.InputOutput,
            X.CopyFromParent, background_pixel=screen.black_pixel,
            event_mask=X.StructureNotifyMask | X.FocusChangeMask)
        self.id = self.w.id
        self.w.set_wm_class(name, name)
        self.w.set_wm_name(name)
        if dock:
            self.set_type('DOCK')
        if top or bottom:
            partial = [0, 0, top, bottom, 0, 0, 0, 0, 0, W - 1 if top else 0,
                       0, W - 1 if bottom else 0]
            self.w.change_property(self.d.intern_atom('_NET_WM_STRUT_PARTIAL'),
                                   Xatom.CARDINAL, 32, partial)
        self.map()

    def set_type(self, kind):
        self.w.change_property(self.d.intern_atom('_NET_WM_WINDOW_TYPE'), Xatom.ATOM, 32,
                               [self.d.intern_atom('_NET_WM_WINDOW_TYPE_' + kind)])
        self.d.flush()

    def map(self):
        self.w.map()
        self.d.flush()

    def unmap(self):
        self.w.unmap()
        self.d.flush()

    def resize_height(self, height):
        self.w.configure(height=height)
        self.d.flush()

    def close(self):
        self.d.close()

    def geometry(self):
        """Absolute (x, y, width, height), or None while not viewable."""
        try:
            if self.w.get_attributes().map_state != X.IsViewable:
                return None
            g = self.w.get_geometry()
            t = self.w.translate_coords(self.root, 0, 0)
            return (-t.x, -t.y, g.width, g.height)
        except Exception:
            return 'gone'

    def got_focus(self):
        self.d.sync()
        while self.d.pending_events():
            e = self.d.next_event()
            if e.type == X.FocusIn and e.detail != X.NotifyPointer:
                return True
        return False


class Sway:
    def __init__(self, work, outputs=1, extra=''):
        run = tempfile.mkdtemp(dir=work)
        os.chmod(run, 0o700)
        config = os.path.join(run, 'config')
        with open(config, 'w') as f:
            f.write('xwayland force\n'
                    'default_border none\n'
                    f'output HEADLESS-1 mode --custom {W}x{H} position 0 0\n'
                    f'output HEADLESS-2 mode --custom {W}x{H} position {W} 0\n' + extra)
        self.clients = []
        self.env = {'PATH': os.environ['PATH'], 'HOME': work, 'XDG_RUNTIME_DIR': run}
        env = dict(self.env, WLR_BACKENDS='headless', WLR_LIBINPUT_NO_DEVICES='1',
                   WLR_RENDERER='pixman', WLR_HEADLESS_OUTPUTS=str(outputs))
        self.log = open(os.path.join(run, 'sway.log'), 'w')
        self.proc = subprocess.Popen(['sway', '-c', config], env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 15
        while True:
            socks = glob.glob(run + '/sway-ipc.*.sock')
            if socks and self.raw('-s', socks[0], '-t', 'get_version').returncode == 0:
                self.env['SWAYSOCK'] = socks[0]
                break
            if time.monotonic() > deadline or self.proc.poll() is not None:
                self.fail_start('sway did not start')
            time.sleep(0.2)
        if until(lambda: len(self.msg('-t', 'get_outputs')), outputs, 10) != outputs:
            self.fail_start(f'sway did not create {outputs} outputs')
        self.cmd('focus output HEADLESS-1')
        # A file per session: an old one would point at the previous Xwayland
        disp = os.path.join(run, 'display')
        self.cmd(f'exec echo $DISPLAY > {disp}')
        if not until(lambda: os.path.exists(disp) and open(disp).read().strip() != '',
                     True, 10):
            self.fail_start('no DISPLAY from sway')
        self.env['DISPLAY'] = open(disp).read().strip()
        deadline = time.monotonic() + 30
        while True:
            try:
                d = display.Display(self.env['DISPLAY'])
                break
            except Exception:
                if time.monotonic() > deadline:
                    self.fail_start('cannot connect to Xwayland')
                time.sleep(0.2)
        root = d.screen().root
        until(lambda: root.get_full_property(d.intern_atom('_NET_SUPPORTED'),
                                             X.AnyPropertyType) is not None, True, 15)
        d.close()

    def fail_start(self, why):
        self.log.flush()
        print(open(self.log.name).read())
        raise SystemExit('FAIL: ' + why)

    def raw(self, *args):
        return subprocess.run(['swaymsg', *args], env=self.env, capture_output=True,
                              text=True, timeout=10)

    def msg(self, *args):
        return json.loads(self.raw(*args).stdout)

    def cmd(self, command):
        return self.raw(command).returncode

    def stop(self):
        for c in self.clients:
            try:
                c.close()
            except Exception:
                pass
        alive = self.proc.poll() is None
        self.proc.terminate()
        self.proc.wait(timeout=10)
        return alive

    def dock(self, name, **kw):
        c = Client(self, name, **kw)
        self.clients.append(c)
        return c

    def window(self, name):
        c = Client(self, name, x=10, y=10, width=200, height=150, dock=False)
        self.clients.append(c)
        until(lambda: self.in_workspace(c), True, 10)
        until(self.focused, c.id, 5)
        return c

    def walk(self):
        stack = [(None, self.msg('-t', 'get_tree'))]
        while stack:
            ws, n = stack.pop()
            if n.get('type') == 'workspace':
                ws = n['name']
            yield ws, n
            for c in n.get('nodes', []) + n.get('floating_nodes', []):
                stack.append((ws, c))

    def in_workspace(self, c):
        return any(ws is not None and n.get('window') == c.id for ws, n in self.walk())

    def focused(self):
        for _, n in self.walk():
            if n.get('focused'):
                return n.get('window') or n.get('name')
        return None

    def ws_rect(self, name=None, output=None):
        for w in self.msg('-t', 'get_workspaces'):
            if (name is None and output is None and w['focused']) or w['name'] == name or \
                    (output is not None and w['output'] == output and w['visible']):
                r = w['rect']
                return (r['x'], r['y'], r['width'], r['height'])
        return None

    def con_rect(self, c):
        for _, n in self.walk():
            if n.get('window') == c.id:
                r = n['rect']
                return (r['x'], r['y'], r['width'], r['height'])
        return None


def top_and_bottom(s):
    n = s.window('normal')
    top = s.dock('top', y=0, height=30, top=30)
    check('top dock geometry', until(top.geometry, (0, 0, W, 30)), (0, 0, W, 30))
    check('workspace below the top dock', until(s.ws_rect, (0, 30, W, H - 30)),
          (0, 30, W, H - 30))
    check('tiled window gets the workspace',
          until(lambda: s.con_rect(n), (0, 30, W, H - 30)), (0, 30, W, H - 30))
    check('focus stays on the window', settle(s.focused, 0.5), n.id)
    check('dock is in no workspace', s.in_workspace(top), False)
    check('dock got no X focus', top.got_focus(), False)
    s.cmd(f'[id={top.id}] focus')
    check('focus command cannot reach the dock', settle(s.focused, 0.3), n.id)
    bottom = s.dock('bottom', y=H - 40, height=40, bottom=40)
    check('bottom dock geometry', until(bottom.geometry, (0, H - 40, W, 40)),
          (0, H - 40, W, 40))
    check('workspace between both docks', until(s.ws_rect, (0, 30, W, H - 70)),
          (0, 30, W, H - 70))
    s.cmd('workspace 2')
    check('docks stay put on a workspace switch', settle(top.geometry, 0.5), (0, 0, W, 30))
    check('other workspace has the same area',
          until(lambda: s.ws_rect('2'), (0, 30, W, H - 70)), (0, 30, W, H - 70))


def stacking(s):
    s.window('normal')
    first = s.dock('first', y=0, height=20, top=20)
    until(first.geometry, (0, 0, W, 20))
    second = s.dock('second', y=0, height=30, top=30)
    check('first dock flush with the edge', until(first.geometry, (0, 0, W, 20)),
          (0, 0, W, 20))
    check('second dock next to it', until(second.geometry, (0, 20, W, 30)), (0, 20, W, 30))
    check('both reserved', until(s.ws_rect, (0, 50, W, H - 50)), (0, 50, W, H - 50))
    first.unmap()
    check('second dock slides to the edge', until(second.geometry, (0, 0, W, 30)),
          (0, 0, W, 30))
    check('space given back on unmap', until(s.ws_rect, (0, 30, W, H - 30)),
          (0, 30, W, H - 30))
    second.resize_height(50)
    check('height change followed', until(second.geometry, (0, 0, W, 50)), (0, 0, W, 50))
    check('height change reserved', until(s.ws_rect, (0, 50, W, H - 50)),
          (0, 50, W, H - 50))
    second.close()
    check('space given back when the client quits', until(s.ws_rect, (0, 0, W, H)),
          (0, 0, W, H))


def position_and_capacity(s):
    s.window('normal')
    low = s.dock('low', y=700, height=25)
    check('no strut, lower half: bottom edge', until(low.geometry, (0, H - 25, W, 25)),
          (0, H - 25, W, 25))
    high = s.dock('high', y=10, height=25)
    check('no strut, upper half: top edge', until(high.geometry, (0, 0, W, 25)),
          (0, 0, W, 25))
    both = s.dock('both', y=600, height=30, top=30, bottom=30)
    check('strut on both edges: position decides', until(both.geometry, (0, H - 55, W, 30)),
          (0, H - 55, W, 30))
    big = s.dock('big', y=0, height=900, top=900)
    check('dock gets only the height left', until(big.geometry, (0, 25, W, H - 80)),
          (0, 25, W, H - 80))
    check('workspace empty', until(lambda: s.ws_rect()[3], 0), 0)


def with_bar(s):
    s.window('normal')
    bar = until(lambda: s.ws_rect()[1] > 0, True, 10) and s.ws_rect()[1]
    check('swaybar reserves its height', bar > 0, True)
    d = s.dock('panel', y=0, height=30, top=30)
    check('dock next to the bar', until(d.geometry, (0, bar, W, 30)), (0, bar, W, 30))
    check('bar and dock reserved', until(s.ws_rect, (0, bar + 30, W, H - bar - 30)),
          (0, bar + 30, W, H - bar - 30))


def type_change(s):
    a = s.window('a')
    b = s.window('b')
    b.set_type('DOCK')
    check('window turned dock leaves the workspace',
          until(lambda: s.in_workspace(b), False), False)
    check('focus moves to the remaining window', until(s.focused, a.id), a.id)
    check('anchored with its requested height', until(b.geometry, (0, 0, W, 150)),
          (0, 0, W, 150))
    check('requested height reserved', until(s.ws_rect, (0, 150, W, H - 150)),
          (0, 150, W, H - 150))
    b.set_type('NORMAL')
    check('dock turned window is managed again', until(lambda: s.in_workspace(b), True), True)
    check('its space is released', until(s.ws_rect, (0, 0, W, H)), (0, 0, W, H))


def output_removal(s):
    s.window('normal')
    d = s.dock('panel', x=W, y=0, height=30, top=30)
    check('dock on the output it asked for', until(d.geometry, (W, 0, W, 30)), (W, 0, W, 30))
    s.cmd('output HEADLESS-2 disable')
    check('dock moves to the remaining output', until(d.geometry, (0, 0, W, 30)),
          (0, 0, W, 30))
    check('remaining output reserves it',
          until(lambda: s.ws_rect(output='HEADLESS-1'), (0, 30, W, H - 30)), (0, 30, W, H - 30))
    s.cmd('output HEADLESS-2 enable')
    check('dock stays there when the output returns', settle(d.geometry), (0, 0, W, 30))


def remap_cycles(s):
    s.window('normal')
    d = s.dock('panel', y=0, height=30, top=30)
    until(d.geometry, (0, 0, W, 30))
    shown = 0
    for _ in range(20):
        d.unmap()
        until(s.ws_rect, (0, 0, W, H))
        d.map()
        if until(s.ws_rect, (0, 30, W, H - 30), 3) == (0, 30, W, H - 30):
            shown += 1
    check('dock mapped again 20 times reserves its space every time', shown, 20)


def main():
    work = tempfile.mkdtemp()
    scenarios = [
        (top_and_bottom, {}),
        (stacking, {}),
        (position_and_capacity, {}),
        (with_bar, {'extra': 'bar {\n  position top\n  status_command true\n}\n'}),
        (type_change, {}),
        (output_removal, {'outputs': 2}),
        (remap_cycles, {}),
    ]
    for fn, kw in scenarios:
        print(f'# {fn.__name__}', flush=True)
        s = Sway(work, **kw)
        try:
            fn(s)
        finally:
            check(f'{fn.__name__}: sway still running', s.stop(), True)
    if failures:
        raise SystemExit(f'{len(failures)} check(s) failed: {failures}')
    print('all checks passed')


main()
