#!/usr/bin/python3
"""30-xwayland-window-requests: X11 window requests are honoured like in i3.

Starts headless sway with Xwayland and checks, with python3-xlib windows:
configure requests and _NET_MOVERESIZE_WINDOW on floating, tiled and
fullscreen windows, the initial placement of floating windows,
_NET_CLOSE_WINDOW, _NET_WM_STATE_STICKY requests and the reflected sticky
state, and _NET_FRAME_EXTENTS / _NET_REQUEST_FRAME_EXTENTS.
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from Xlib import X, Xatom, display
from Xlib.protocol import event

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


class Sway:
    def __init__(self, work, outputs):
        run = tempfile.mkdtemp(dir=work)
        os.chmod(run, 0o700)
        config = os.path.join(run, 'config')
        lines = ['xwayland force', 'focus_follows_mouse no',
                 'font pango:DejaVu Sans 10',
                 'default_border normal 3', 'default_floating_border normal 3',
                 'for_window [class="^float$"] floating enable']
        x = 0
        for i, (w, h) in enumerate(outputs, 1):
            lines.append(f'output HEADLESS-{i} mode --custom {w}x{h} position {x} 0')
            lines.append(f'workspace {i} output HEADLESS-{i}')
            x += w
        with open(config, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        self.env = {'PATH': os.environ['PATH'], 'HOME': work, 'XDG_RUNTIME_DIR': run}
        env = dict(self.env, WLR_BACKENDS='headless', WLR_LIBINPUT_NO_DEVICES='1',
                   WLR_RENDERER='pixman', WLR_HEADLESS_OUTPUTS=str(len(outputs)))
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
        if until(lambda: len(self.msg('-t', 'get_outputs')), len(outputs), 10) != len(outputs):
            self.fail_start('sway did not create the outputs')
        self.cmd('focus output HEADLESS-1')
        disp = os.path.join(run, 'display')
        self.cmd(f'exec echo $DISPLAY > {disp}')
        if not until(lambda: os.path.exists(disp) and open(disp).read().strip() != '',
                     True, 10):
            self.fail_start('no DISPLAY from sway')
        self.env['DISPLAY'] = open(disp).read().strip()
        deadline = time.monotonic() + 30
        while True:
            try:
                self.d = display.Display(self.env['DISPLAY'])
                break
            except Exception:
                if time.monotonic() > deadline:
                    self.fail_start('cannot connect to Xwayland')
                time.sleep(0.2)
        self.root = self.d.screen().root
        until(lambda: self.prop(self.root, '_NET_SUPPORTED') is not None, True, 15)

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
        if self.raw(command).returncode != 0:
            failures.append('command: ' + command)
            print('FAIL command:', command, flush=True)

    def stop(self):
        self.d.close()
        alive = self.proc.poll() is None
        self.proc.terminate()
        self.proc.wait(timeout=10)
        return alive

    def atom(self, name):
        return self.d.get_atom(name)

    def prop(self, win, name):
        self.d.sync()
        p = win.get_full_property(self.atom(name), X.AnyPropertyType)
        return list(p.value) if p is not None else None

    def window(self, cls, x, y, w, h, hints=None, leader=None, delete=False, mapped=True):
        # A background makes Xwayland commit a buffer, which sway needs to
        # map the window
        win = self.root.create_window(x, y, w, h, 0, X.CopyFromParent, X.InputOutput,
                                      X.CopyFromParent,
                                      background_pixel=self.d.screen().white_pixel,
                                      event_mask=X.StructureNotifyMask)
        win.set_wm_class(cls, cls)
        if hints:
            # USPosition | USSize. python3-xlib's set_wm_normal_hints zeroes
            # the x, y, width and height fields, so write all 18 values.
            win.change_property(Xatom.WM_NORMAL_HINTS, Xatom.WM_SIZE_HINTS, 32,
                                [1 | 2, *hints] + [0] * 13)
        if leader is not None:
            win.change_property(self.atom('WM_CLIENT_LEADER'), Xatom.WINDOW, 32,
                                [leader.id])
        if delete:
            win.change_property(self.atom('WM_PROTOCOLS'), Xatom.ATOM, 32,
                                [self.atom('WM_DELETE_WINDOW')])
        if mapped:
            win.map()
            self.d.sync()
            until(lambda: self.node(win) is not None, True, 10)
            time.sleep(0.3)
        return win

    def node(self, win):
        def walk(n, ws):
            if n.get('type') == 'workspace':
                ws = n['name']
            if n.get('window') == win.id:
                return dict(n, workspace=ws)
            for c in n.get('nodes', []) + n.get('floating_nodes', []):
                r = walk(c, ws)
                if r:
                    return r
            return None
        return walk(self.msg('-t', 'get_tree'), None)

    def client(self, win):
        """The window's position and size, as the client sees them."""
        self.d.sync()
        g = win.get_geometry()
        t = win.translate_coords(self.root, 0, 0)
        return (-t.x, -t.y, g.width, g.height)

    def settled(self, win):
        """Waits until the window geometry stops changing."""
        last = None
        value = self.client(win)
        deadline = time.monotonic() + 5
        while value != last and time.monotonic() < deadline:
            time.sleep(0.3)
            last, value = value, self.client(win)
        return value

    def send(self, win, type_, data):
        ev = event.ClientMessage(window=win, client_type=self.atom(type_),
                                 data=(32, (list(data) + [0] * 5)[:5]))
        self.root.send_event(ev, event_mask=X.SubstructureRedirectMask |
                             X.SubstructureNotifyMask)
        self.d.sync()

    def focused_workspace(self):
        return [w['name'] for w in self.msg('-t', 'get_workspaces') if w['focused']]

    def sticky_state(self, win):
        return self.atom('_NET_WM_STATE_STICKY') in (self.prop(win, '_NET_WM_STATE') or [])


def one_output(work):
    s = Sway(work, [(1280, 800)])

    win = s.window('float', 200, 300, 300, 200)
    check('floating window maps where it asked', s.settled(win), (200, 300, 300, 200))
    n = s.node(win)
    # In IPC, rect starts below the title bar, which deco_rect describes
    left = n['window_rect']['x']
    top = n['deco_rect']['height'] + n['window_rect']['y']
    check('the frame is drawn around the client',
          (n['rect']['x'], n['deco_rect']['y']), (200 - left, 300 - top))

    win.configure(x=100, y=150)
    check('configure request moves the client', s.settled(win), (100, 150, 300, 200))
    win.configure(width=400, height=250)
    check('size-only request keeps the position', s.settled(win), (100, 150, 400, 250))

    flags = (1 << 8) | (1 << 9) | (1 << 10) | (1 << 11) | 1 | (2 << 12)
    s.send(win, '_NET_MOVERESIZE_WINDOW', [flags, 150, 200, 320, 240])
    check('_NET_MOVERESIZE_WINDOW', s.settled(win), (150, 200, 320, 240))
    s.send(win, '_NET_MOVERESIZE_WINDOW', [1 | (2 << 12), 5, 5, 5, 5])
    check('_NET_MOVERESIZE_WINDOW selecting no field', s.settled(win), (150, 200, 320, 240))
    supported = s.prop(s.root, '_NET_SUPPORTED')
    check('_NET_MOVERESIZE_WINDOW is advertised',
          s.atom('_NET_MOVERESIZE_WINDOW') in supported, True)
    check('_NET_FRAME_EXTENTS is not advertised',
          s.atom('_NET_FRAME_EXTENTS') in supported, False)

    check('_NET_FRAME_EXTENTS floating',
          until(lambda: s.prop(win, '_NET_FRAME_EXTENTS'), [3, 3, top, 3]), [3, 3, top, 3])
    s.cmd('[class="^float$"] border pixel 5')
    check('_NET_FRAME_EXTENTS border pixel 5',
          until(lambda: s.prop(win, '_NET_FRAME_EXTENTS'), [5] * 4), [5] * 4)
    s.cmd('[class="^float$"] border normal 3')
    s.cmd('[class="^float$"] fullscreen enable')
    check('_NET_FRAME_EXTENTS fullscreen',
          until(lambda: s.prop(win, '_NET_FRAME_EXTENTS'), [0] * 4), [0] * 4)
    win.configure(x=10, y=10, width=100, height=100)
    check('fullscreen window ignores configure requests', s.settled(win), (0, 0, 1280, 800))
    s.cmd('[class="^float$"] fullscreen disable')
    check('back to its floating geometry', s.settled(win), (150, 200, 320, 240))

    sticky = s.atom('_NET_WM_STATE_STICKY')
    s.send(win, '_NET_WM_STATE', [1, sticky, 0, 2])
    check('_NET_WM_STATE add sticky', until(lambda: s.node(win)['sticky'], True), True)
    check('sticky state reflected', until(lambda: s.sticky_state(win), True), True)
    s.cmd('workspace 7')
    check('sticky window follows to workspace 7',
          until(lambda: s.node(win)['workspace'], '7'), '7')
    s.send(win, '_NET_WM_STATE', [2, sticky, 0, 2])
    check('_NET_WM_STATE toggle sticky', until(lambda: s.node(win)['sticky'], False), False)
    check('sticky state cleared', until(lambda: s.sticky_state(win), False), False)
    s.cmd('[class="^float$"] sticky enable')
    check('sticky command reflected', until(lambda: s.sticky_state(win), True), True)
    s.cmd('[class="^float$"] sticky disable')
    check('sticky disable reflected', until(lambda: s.sticky_state(win), False), False)

    s.cmd('workspace 4')
    tiled = s.window('tile', 0, 0, 300, 200)
    s.window('tile2', 0, 0, 300, 200)
    check('_NET_FRAME_EXTENTS tiled',
          until(lambda: s.prop(tiled, '_NET_FRAME_EXTENTS'), [3, 3, top, 3]), [3, 3, top, 3])
    s.cmd('layout tabbed')
    check('_NET_FRAME_EXTENTS tabbed: the title belongs to the parent',
          until(lambda: s.prop(tiled, '_NET_FRAME_EXTENTS'), [3, 3, 0, 3]), [3, 3, 0, 3])
    before = s.settled(tiled)
    tiled.configure(x=5, y=5, width=100, height=100)
    check('tiled window ignores configure requests', s.settled(tiled), before)

    unmapped = s.window('early', 0, 0, 100, 100, mapped=False)
    s.send(unmapped, '_NET_REQUEST_FRAME_EXTENTS', [])
    check('_NET_REQUEST_FRAME_EXTENTS before mapping',
          until(lambda: s.prop(unmapped, '_NET_FRAME_EXTENTS'), [3, 3, top, 3]),
          [3, 3, top, 3])

    s.cmd('workspace 5')
    hinted = s.window('float', 10, 10, 50, 50, hints=(600, 400, 240, 160))
    check('WM_NORMAL_HINTS position and size', s.settled(hinted), (600, 400, 240, 160))
    lead = s.window('float', 100, 100, 400, 300)
    dialog = s.window('float', 0, 0, 200, 100, leader=lead)
    lr, dr = s.node(lead)['rect'], s.node(dialog)['rect']
    check('position (0, 0) centres the frame over the WM_CLIENT_LEADER window',
          (dr['x'] + dr['width'] / 2, dr['y'] + dr['height'] / 2),
          (lr['x'] + lr['width'] / 2, lr['y'] + lr['height'] / 2))

    closing = s.window('float', 300, 300, 100, 100, delete=True)
    s.send(closing, '_NET_CLOSE_WINDOW', [0, 2])
    got = None
    deadline = time.monotonic() + 5
    while got is None and time.monotonic() < deadline:
        while s.d.pending_events():
            ev = s.d.next_event()
            if ev.type == X.ClientMessage and ev.window == closing:
                got = s.d.get_atom_name(ev.data[1][0])
        time.sleep(0.1)
    check('_NET_CLOSE_WINDOW sends WM_DELETE_WINDOW', got, 'WM_DELETE_WINDOW')

    check('sway survived', s.stop(), True)


def two_outputs(work):
    s = Sway(work, [(640, 800), (640, 800)])
    win = s.window('float', 100, 100, 160, 80)
    win.configure(x=800, y=200)
    check('configure onto the right output', s.settled(win), (800, 200, 160, 80))
    check('the window joins workspace 2', until(lambda: s.node(win)['workspace'], '2'), '2')
    check('workspace 2 gains focus', until(s.focused_workspace, ['2']), ['2'])
    win.configure(x=5000, y=200, width=300)
    check('a position on no output voids the request', s.settled(win), (800, 200, 160, 80))

    s.cmd('workspace 1')
    other = s.window('float', 720, 240, 160, 80)
    check('a position on the other output is carried over to this one',
          s.settled(other), (80, 240, 160, 80))
    check('and the window stays on workspace 1', s.node(other)['workspace'], '1')
    check('sway survived', s.stop(), True)


def main():
    work = tempfile.mkdtemp(prefix='xwayland-window-requests-')
    try:
        one_output(work)
        two_outputs(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if failures:
        print('FAILED:', ', '.join(failures))
        sys.exit(1)
    print('all checks passed')


if __name__ == '__main__':
    main()
