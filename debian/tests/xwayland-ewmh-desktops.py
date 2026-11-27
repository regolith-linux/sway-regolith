#!/usr/bin/python3
"""26-xwayland-ewmh-desktops: X11 clients see the workspaces as EWMH desktops.

Starts headless sway with Xwayland on two 1280x800 outputs side by side and
checks, with python3-xlib windows and the real wmctrl and xdotool, the root
window properties, each window's _NET_WM_DESKTOP, the desktop requests and a
_NET_WM_DESKTOP set before mapping, as i3 4.25.1 handles them.
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from Xlib import X, display
from Xlib.protocol import event

ALL = 0xFFFFFFFF
SUPPORTED = ['_NET_NUMBER_OF_DESKTOPS', '_NET_DESKTOP_NAMES',
             '_NET_CURRENT_DESKTOP', '_NET_DESKTOP_VIEWPORT', '_NET_WM_DESKTOP']
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


def settle(fn):
    time.sleep(1)
    return fn()


class Sway:
    def __init__(self, work, extra=''):
        self.work = work
        run = tempfile.mkdtemp(dir=work)
        os.chmod(run, 0o700)
        config = os.path.join(work, 'config')
        with open(config, 'w') as f:
            f.write('xwayland force\n'
                    'focus_follows_mouse no\n'
                    'output HEADLESS-1 mode --custom 1280x800 position 0 0\n'
                    'output HEADLESS-2 mode --custom 1280x800 position 1280 0\n'
                    'workspace 1 output HEADLESS-1\n'
                    'workspace 3 output HEADLESS-2\n'
                    'workspace "2: mail" output HEADLESS-1\n'
                    'workspace 2 output HEADLESS-1\n'
                    'workspace 9 output HEADLESS-1\n' + extra)
        self.env = {'PATH': os.environ['PATH'], 'HOME': work, 'XDG_RUNTIME_DIR': run}
        env = dict(self.env, WLR_BACKENDS='headless', WLR_LIBINPUT_NO_DEVICES='1',
                   WLR_RENDERER='pixman', WLR_HEADLESS_OUTPUTS='2')
        self.log = open(os.path.join(work, 'sway.log'), 'w')
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
        if until(lambda: len(self.msg('-t', 'get_outputs')), 2, 10) != 2:
            self.fail_start('sway did not create two outputs')
        # Start every session on the left output, workspace 1
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
                self.d = display.Display(self.env['DISPLAY'])
                break
            except Exception:
                if time.monotonic() > deadline:
                    self.fail_start('cannot connect to Xwayland')
                time.sleep(0.2)
        self.screen = self.d.screen()
        self.root = self.screen.root
        until(lambda: self.root_prop('_NET_SUPPORTED') is not None, True, 15)

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

    def tool(self, *args):
        return subprocess.run(list(args), env=self.env, capture_output=True,
                              text=True, timeout=10)

    def stop(self):
        self.d.close()
        alive = self.proc.poll() is None
        self.proc.terminate()
        self.proc.wait(timeout=10)
        return alive

    def atom(self, name):
        return self.d.get_atom(name)

    def root_prop(self, name):
        self.d.sync()
        p = self.root.get_full_property(self.atom(name), X.AnyPropertyType)
        if p is None:
            return None
        if p.format == 8:
            value = p.value if isinstance(p.value, bytes) else p.value.encode()
            return [n.decode() for n in value.split(b'\0')[:-1]]
        return list(p.value)

    def desktops(self):
        return [self.root_prop(n) for n in ('_NET_NUMBER_OF_DESKTOPS',
                                            '_NET_DESKTOP_NAMES',
                                            '_NET_CURRENT_DESKTOP',
                                            '_NET_DESKTOP_VIEWPORT')]

    def wm_desktop(self, win):
        self.d.sync()
        p = win.get_full_property(self.atom('_NET_WM_DESKTOP'), X.AnyPropertyType)
        return None if p is None else p.value[0]

    def window(self, name, desktop=None):
        w = self.root.create_window(0, 0, 200, 150, 0, self.screen.root_depth,
                                    X.InputOutput, X.CopyFromParent,
                                    background_pixel=self.screen.white_pixel)
        w.set_wm_class(name, name)
        w.set_wm_name(name)
        if desktop is not None:
            w.change_property(self.atom('_NET_WM_DESKTOP'), self.atom('CARDINAL'), 32,
                              [desktop])
        w.map()
        self.d.flush()
        if until(lambda: self.ws_of(w) is not None, True, 10) is not True:
            failures.append('window ' + name)
            print('FAIL window did not appear:', name, flush=True)
        return w

    def send_wm_desktop(self, win, value):
        ev = event.ClientMessage(window=win, client_type=self.atom('_NET_WM_DESKTOP'),
                                 data=(32, [value, 2, 0, 0, 0]))
        self.root.send_event(ev, event_mask=X.SubstructureRedirectMask
                             | X.SubstructureNotifyMask)
        self.d.flush()

    def node(self, win):
        stack = [(None, self.msg('-t', 'get_tree'))]
        while stack:
            ws, n = stack.pop()
            if n.get('type') == 'workspace':
                ws = n['name']
            if n.get('window') == win.id:
                return ws, n
            stack += [(ws, c) for c in n.get('nodes', []) + n.get('floating_nodes', [])]
        return None

    def ws_of(self, win):
        found = self.node(win)
        return found[0] if found else None

    def floating_sticky(self, win):
        found = self.node(win)
        return found and [found[1]['type'] == 'floating_con', found[1]['sticky']]

    def focused_ws(self):
        return next(w['name'] for w in self.msg('-t', 'get_workspaces') if w['focused'])


def three_desktops(s):
    s.cmd('workspace 1')
    a = s.window('a')
    s.cmd('workspace "2: mail"')
    b = s.window('b')
    s.cmd('workspace 3')
    c = s.window('c')
    s.cmd('workspace "2: mail"')
    return a, b, c


def properties(s):
    a, b, c = three_desktops(s)
    names = {s.d.get_atom_name(x) for x in s.root_prop('_NET_SUPPORTED')}
    check('_NET_SUPPORTED lists the desktop hints', [n for n in SUPPORTED if n in names],
          SUPPORTED)
    want = [[3], ['1', '2: mail', '3'], [1], [0, 0, 0, 0, 1280, 0]]
    check('root window desktops', until(s.desktops, want), want)
    check('_NET_WM_DESKTOP of a b c', [s.wm_desktop(w) for w in (a, b, c)], [0, 1, 2])
    lines = s.tool('wmctrl', '-d').stdout.splitlines()
    check('wmctrl -d lists 3 desktops, the second active',
          [len(lines), [i for i, ln in enumerate(lines) if ' * ' in ln]], [3, [1]])
    s.cmd('move workspace to output HEADLESS-2')
    check('move workspace to output updates the viewport',
          until(lambda: s.root_prop('_NET_DESKTOP_VIEWPORT'), [0, 0, 1280, 0, 1280, 0]),
          [0, 0, 1280, 0, 1280, 0])


def requests(s):
    a, b, c = three_desktops(s)
    until(lambda: s.root_prop('_NET_NUMBER_OF_DESKTOPS'), [3])
    check('xdotool set_desktop 2 succeeds', s.tool('xdotool', 'set_desktop', '2').returncode, 0)
    check('set_desktop 2 focuses workspace 3', until(s.focused_ws, '3'), '3')
    s.tool('xdotool', 'set_desktop', '7')
    check('unknown desktop is ignored', settle(s.focused_ws), '3')
    s.tool('wmctrl', '-s', '0')
    check('wmctrl -s 0 focuses workspace 1', until(s.focused_ws, '1'), '1')
    s.tool('xdotool', 'set_desktop_for_window', str(b.id), '2')
    check('set_desktop_for_window moves b to workspace 3', until(lambda: s.ws_of(b), '3'), '3')
    check('the focused workspace stays 1', s.focused_ws(), '1')
    check('the emptied desktop goes away',
          until(lambda: s.root_prop('_NET_DESKTOP_NAMES'), ['1', '3']), ['1', '3'])
    check('b reports desktop 1', until(lambda: s.wm_desktop(b), 1), 1)
    s.tool('wmctrl', '-i', '-r', hex(a.id), '-t', '1')
    check('wmctrl -t 1 moves a to workspace 3', until(lambda: s.ws_of(a), '3'), '3')


def sticky(s):
    s.cmd('workspace 3')
    s.window('c')
    s.cmd('workspace 2')
    s.window('u')
    s.cmd('workspace 1')
    w = s.window('w')
    s.window('t')
    until(lambda: s.root_prop('_NET_NUMBER_OF_DESKTOPS'), [3])
    s.send_wm_desktop(w, ALL)
    check('0xFFFFFFFF makes a tiled window floating and sticky',
          until(lambda: s.floating_sticky(w), [True, True]), [True, True])
    check('it reports 0xFFFFFFFF', until(lambda: s.wm_desktop(w), ALL), ALL)
    s.cmd('workspace 2')
    check('it follows a workspace switch on its output', until(lambda: s.ws_of(w), '2'), '2')
    s.send_wm_desktop(w, 2)
    check('an index on the other output moves it to the workspace shown there',
          until(lambda: s.ws_of(w), '3'), '3')
    check('it stays sticky, focus stays on 2',
          [s.floating_sticky(w), s.focused_ws()], [[True, True], '2'])


def renumber_and_premap(s):
    s.cmd('workspace 1')
    t = s.window('t')
    s.cmd('workspace 9')
    m = s.window('m')
    s.cmd('workspace 2')
    e = s.window('e')
    check('desktops 1 2 9 on the left output, 3 on the right one',
          until(lambda: s.root_prop('_NET_DESKTOP_NAMES'), ['1', '2', '9', '3']),
          ['1', '2', '9', '3'])
    s.cmd('[class="^t$"] move container to workspace 9')
    check('moving t away from 1 renumbers the desktops',
          until(lambda: s.root_prop('_NET_DESKTOP_NAMES'), ['2', '9', '3']), ['2', '9', '3'])
    check('t and m report 1, e reports 0',
          until(lambda: [s.wm_desktop(x) for x in (t, e, m)], [1, 0, 1]), [1, 0, 1])
    focused = s.focused_ws()
    p = s.window('p', desktop=1)
    check('_NET_WM_DESKTOP set before mapping places the window, focus stays',
          [focused, s.ws_of(p), s.focused_ws()], ['2', '9', '2'])
    s.cmd('[class="^m$"] move scratchpad')
    check('a hidden scratchpad window reports 0xFFFFFFFF',
          until(lambda: s.wm_desktop(m), ALL), ALL)
    q = s.window('q', desktop=ALL)
    check('0xFFFFFFFF set before mapping maps it floating and sticky',
          until(lambda: s.floating_sticky(q), [True, True]), [True, True])
    t.unmap()
    s.d.flush()
    check('unmapping deletes _NET_WM_DESKTOP', until(lambda: s.wm_desktop(t), None), None)


def main():
    version = subprocess.run(['dpkg-query', '-W', '-f', '${Package} ${Version}',
                              'sway-regolith'], capture_output=True, text=True).stdout
    print('sway under test:', version, shutil.which('sway'), flush=True)
    work = tempfile.mkdtemp()
    try:
        for part in (properties, requests, sticky, renumber_and_premap):
            print(f'--- {part.__name__}', flush=True)
            s = Sway(work)
            try:
                part(s)
            finally:
                check(f'sway still running after {part.__name__}', s.stop(), True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if failures:
        print(f'FAIL: {len(failures)} check(s) failed')
        return 1
    print('PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
