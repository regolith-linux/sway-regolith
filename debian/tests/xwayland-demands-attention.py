#!/usr/bin/python3
"""32-xwayland-demands-attention: X11 attention requests drive urgency.

Starts headless sway with Xwayland and checks, with python3-xlib windows:
_NET_WM_STATE add/remove/toggle of _NET_WM_STATE_DEMANDS_ATTENTION and the
urgency, IPC events and reflected atom that follow; requests from the focused
window; urgency cleared by switching to the workspace; a window that maps
with the atom set; for_window [urgent=...] rules; urgent deny over attention,
WM_HINTS and _NET_ACTIVE_WINDOW requests; a window removing WM_HINTS; and a
window that unmaps while urgent.
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
ADD, REMOVE, TOGGLE = 1, 0, 2
DA = '_NET_WM_STATE_DEMANDS_ATTENTION'


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
    def __init__(self, work, extra=()):
        run = tempfile.mkdtemp(dir=work)
        os.chmod(run, 0o700)
        config = os.path.join(run, 'config')
        lines = ['xwayland force', 'focus_follows_mouse no',
                 'font pango:DejaVu Sans 10', *extra]
        with open(config, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        self.env = {'PATH': os.environ['PATH'], 'HOME': work, 'XDG_RUNTIME_DIR': run}
        env = dict(self.env, WLR_BACKENDS='headless', WLR_LIBINPUT_NO_DEVICES='1',
                   WLR_RENDERER='pixman')
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
        # Window and workspace urgent events, read from swaymsg -m
        self.events_file = os.path.join(run, 'events')
        self.events_out = open(self.events_file, 'w')
        self.monitor = subprocess.Popen(
            ['swaymsg', '-m', '-r', '-t', 'subscribe', '["window","workspace"]'],
            env=self.env, stdout=self.events_out, stderr=subprocess.DEVNULL)
        time.sleep(0.3)

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

    def alive(self):
        return self.proc.poll() is None

    def stop(self):
        alive = self.alive()
        self.monitor.terminate()
        self.monitor.wait(timeout=10)
        self.events_out.close()
        try:
            self.d.close()
        except Exception:
            pass
        self.proc.terminate()
        self.proc.wait(timeout=10)
        return alive

    def atom(self, name):
        return self.d.get_atom(name)

    def window(self, cls, attention=False):
        # Xwayland occasionally never pairs a window with its wl_surface
        # under load; such a window never reaches sway, so try a new one.
        for _ in range(3):
            win = self.create(cls, attention)
            if until(lambda: self.node(win) is not None, True, 5) is True:
                return win
            win.destroy()
            self.d.sync()
        self.fail_start(f'window {cls} never appeared')

    def create(self, cls, attention):
        win = self.root.create_window(0, 0, 200, 150, 0, self.d.screen().root_depth,
                                      X.InputOutput, X.CopyFromParent,
                                      # a background gives Xwayland a buffer
                                      # to commit, so the window maps
                                      background_pixel=self.d.screen().white_pixel,
                                      event_mask=X.StructureNotifyMask)
        win.set_wm_class(cls, cls)
        win.set_wm_name(cls)
        win.set_wm_hints(flags=1, input=1)
        if attention:
            # wlroots learns about _NET_WM_STATE set before mapping only
            # through PropertyNotify, which it selects when it handles the
            # window's CreateNotify; it rewrites the property from its own
            # state on MapNotify. Give it time to select before setting.
            self.d.sync()
            time.sleep(0.5)
            win.change_property(self.atom('_NET_WM_STATE'), Xatom.ATOM, 32,
                                [self.atom(DA)])
        win.map()
        self.d.sync()
        return win

    def node(self, win):
        stack = [self.msg('-t', 'get_tree')]
        while stack:
            n = stack.pop()
            if n.get('window') == win.id:
                return n
            stack += n.get('nodes', []) + n.get('floating_nodes', [])
        return None

    def urgent(self, win):
        n = self.node(win)
        return None if n is None else n['urgent']

    def focused(self, win):
        n = self.node(win)
        return None if n is None else n['focused']

    def ws_urgent(self, name):
        for ws in self.msg('-t', 'get_workspaces'):
            if ws['name'] == name:
                return ws['urgent']
        return None

    def attention(self, win):
        prop = win.get_full_property(self.atom('_NET_WM_STATE'), Xatom.ATOM)
        return self.atom(DA) in (prop.value if prop else [])

    def atoms(self, win):
        prop = win.get_full_property(self.atom('_NET_WM_STATE'), Xatom.ATOM)
        names = [self.d.get_atom_name(a) for a in (prop.value if prop else [])]
        return sorted(n for n in names if n in (DA, '_NET_WM_STATE_ABOVE'))

    def root_message(self, win, kind, data):
        ev = event.ClientMessage(window=win, client_type=self.atom(kind),
                                 data=(32, data))
        self.root.send_event(ev, event_mask=X.SubstructureRedirectMask
                             | X.SubstructureNotifyMask)
        self.d.sync()

    def state(self, win, action, first, second=None):
        self.root_message(win, '_NET_WM_STATE', [
            action, self.atom(first), self.atom(second) if second else 0, 1, 0])

    def activate(self, win):
        self.root_message(win, '_NET_ACTIVE_WINDOW', [1, X.CurrentTime, 0, 0, 0])

    def hint(self, win, urgent):
        win.set_wm_hints(flags=1 | (256 if urgent else 0), input=1)
        self.d.sync()

    def events(self):
        out = []
        self.events_out.flush()
        with open(self.events_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ev = json.loads(line)
                if ev.get('change') != 'urgent':
                    continue
                if 'container' in ev:
                    out.append(('window', ev['container']['urgent']))
                else:
                    out.append(('workspace', ev['current']['urgent']))
        return out


def requests(work):
    s = Sway(work)
    a = s.window('a')
    s.window('b')
    s.state(a, ADD, DA)
    check('add: window urgent', until(lambda: s.urgent(a), True), True)
    check('add: workspace urgent', until(lambda: s.ws_urgent('1'), True), True)
    check('add: atom set', until(lambda: s.attention(a), True), True)
    check('add: one window and one workspace event',
          until(s.events, [('window', True), ('workspace', True)]),
          [('window', True), ('workspace', True)])
    s.state(a, ADD, DA)
    time.sleep(0.4)
    check('repeated add: no event', len(s.events()), 2)
    s.state(a, REMOVE, DA)
    check('remove: window urgent', until(lambda: s.urgent(a), False), False)
    check('remove: workspace urgent', until(lambda: s.ws_urgent('1'), False), False)
    check('remove: atom set', until(lambda: s.attention(a), False), False)
    s.state(a, TOGGLE, DA)
    check('toggle: window urgent', until(lambda: s.urgent(a), True), True)
    s.state(a, TOGGLE, DA)
    check('toggle again: window urgent', until(lambda: s.urgent(a), False), False)
    s.state(a, ADD, '_NET_WM_STATE_ABOVE', DA)
    check('add through data.l[2]: window urgent', until(lambda: s.urgent(a), True), True)
    s.cmd(f'[id={a.id}] focus')
    check('focus: window urgent', until(lambda: s.urgent(a), False), False)
    check('focus: only the attention atom goes',
          until(lambda: s.atoms(a), ['_NET_WM_STATE_ABOVE']), ['_NET_WM_STATE_ABOVE'])
    s.state(a, ADD, DA)
    time.sleep(0.4)
    check('focused window: add refused', s.urgent(a), False)
    check('focused window: atom taken back', until(lambda: s.attention(a), False), False)

    s.cmd('workspace 2')
    s.window('c')
    s.state(a, ADD, DA)
    check('hidden workspace: workspace 1 urgent', until(lambda: s.ws_urgent('1'), True), True)
    s.cmd('workspace 1')
    check('switch: window urgent', until(lambda: s.urgent(a), False), False)
    check('switch: atom set', until(lambda: s.attention(a), False), False)

    s.cmd('[class="b"] focus')
    s.cmd(f'[id={a.id}] urgent enable')
    check('urgent enable sets the atom', until(lambda: s.attention(a), True), True)
    s.cmd(f'[id={a.id}] urgent disable')
    check('urgent disable clears the atom', until(lambda: s.attention(a), False), False)

    a.delete_property(s.atom('WM_HINTS'))
    s.d.sync()
    time.sleep(1)
    check('a window removing WM_HINTS: sway alive', s.alive(), True)
    check('sway survived', s.stop(), True)


def mapping(work):
    s = Sway(work, ['no_focus [class="^nofocus"]'])
    s.window('anchor')
    p = s.window('nofocus-preset', attention=True)
    check('mapped with the atom under no_focus: not focused', s.focused(p), False)
    check('mapped with the atom: urgent', until(lambda: s.urgent(p), True), True)
    check('mapped with the atom: atom kept', s.attention(p), True)
    f = s.window('focused-preset', attention=True)
    time.sleep(0.3)
    check('mapped focused with the atom: urgent', s.urgent(f), False)
    check('mapped focused with the atom: atom taken back',
          until(lambda: s.attention(f), False), False)

    r = s.window('nofocus-remap')
    s.hint(r, True)
    check('before unmap: urgent', until(lambda: s.urgent(r), True), True)
    r.unmap()
    s.d.sync()
    until(lambda: s.node(r) is None, True)
    s.hint(r, False)
    for _ in range(3):  # see Sway.window()
        r.map()
        s.d.sync()
        if until(lambda: s.node(r) is not None, True) is True:
            break
        r.unmap()
        s.d.sync()
    time.sleep(0.3)
    check('remapped: not urgent', s.urgent(r), False)
    check('remapped: atom set', s.attention(r), False)
    check('sway survived', s.stop(), True)


def rules(work):
    s = Sway(work, ['focus_on_window_activation urgent',
                    'for_window [urgent=latest class="special"] focus',
                    'for_window [class="denied"] urgent deny'])
    s.window('a')
    sp = s.window('special')
    s.window('c')
    s.state(sp, ADD, DA)
    check('for_window [urgent=latest]: window focused', until(lambda: s.focused(sp), True), True)
    s.cmd('[class="c"] focus')
    s.hint(sp, True)
    check('second urgency: urgent', until(lambda: s.urgent(sp), True), True)
    check('second urgency: rule does not run again', s.focused(sp), False)

    d = s.window('denied')
    s.cmd('[class="c"] focus')
    s.state(d, ADD, DA)
    s.hint(d, True)
    s.activate(d)
    time.sleep(0.5)
    check('urgent deny: attention, WM_HINTS and activation refused', s.urgent(d), False)
    check('urgent deny: atom taken back', until(lambda: s.attention(d), False), False)
    s.cmd('[class="denied"] urgent enable')
    check('urgent deny: urgent enable still works', until(lambda: s.urgent(d), True), True)
    check('sway survived', s.stop(), True)


def main():
    work = tempfile.mkdtemp(prefix='xwayland-demands-attention-')
    try:
        requests(work)
        mapping(work)
        rules(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if failures:
        print('FAILED:', ', '.join(failures))
        sys.exit(1)
    print('all checks passed')


if __name__ == '__main__':
    main()
