#!/bin/sh
# In-build check for 10-bind-none-disables-binding: a key combination that
# contains the key none is accepted quietly by sway -C, while a typo in the
# other keys of such a combination is still an error.
# Runs the freshly built binary; needs neither a display nor trawld.
set -eu

SWAY=${1:?usage: $0 path/to/built/sway}

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -m 700 "$tmp/run"
export XDG_RUNTIME_DIR="$tmp/run"
export WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 WLR_RENDERER=pixman
export DBUS_SESSION_BUS_ADDRESS="unix:path=$tmp/no-bus"

fail() {
	cat "$tmp/out.log"
	echo "FAIL: $*"
	exit 1
}

# accepted NAME LINE...: a config made of LINEs validates without output.
accepted() {
	name=$1
	shift
	printf '%s\n' "$@" > "$tmp/$name.conf"
	if ! "$SWAY" -C -c "$tmp/$name.conf" > "$tmp/out.log" 2>&1; then
		fail "$name: rejected"
	fi
	if [ -s "$tmp/out.log" ]; then
		fail "$name: sway -C printed something"
	fi
}

# rejected NAME MESSAGE LINE...: a config made of LINEs fails with MESSAGE.
rejected() {
	name=$1
	message=$2
	shift 2
	printf '%s\n' "$@" > "$tmp/$name.conf"
	if "$SWAY" -C -c "$tmp/$name.conf" > "$tmp/out.log" 2>&1; then
		fail "$name: accepted"
	fi
	if ! grep -qF "$message" "$tmp/out.log"; then
		fail "$name: no \"$message\" in the output"
	fi
}

accepted plain \
	'bindsym none exec false' \
	'bindsym None exec false' \
	'bindsym Mod4+NONE exec false'

# Several switched-off lines may share modifiers and repeat.
accepted shared-modifiers \
	'set $mod Mod4' \
	'set $wm.binding.reboot none' \
	'set $wm.binding.logout none' \
	'bindsym $mod+Shift+$wm.binding.reboot exec false' \
	'bindsym $mod+Shift+$wm.binding.logout exec false' \
	'bindsym $mod+Shift+none exec false'

accepted options \
	'bindsym --to-code --release --locked --inhibited --no-warn --no-repeat --input-device=* Mod4+none exec false' \
	'bindsym --whole-window --border --exclude-titlebar Mod4+none exec false' \
	'bindsym Group2+Mod4+none exec false'

accepted other-commands \
	'bindcode Mod4+none exec false' \
	'bindcode 36+none exec false' \
	'unbindsym Mod4+none' \
	'unbindcode Mod4+none'

accepted mode \
	'mode "resize" {' \
	'	bindsym Mod4+none exec false' \
	'}'

# The other keys of the combination are still checked, before or after none.
rejected typo-before "Unknown key or button 'Retrun'" 'bindsym Mod4+Retrun+none exec false'
rejected typo-after "Unknown key or button 'Retrun'" 'bindsym Mod4+none+Retrun exec false'
rejected unbind-typo "Unknown key or button 'Retrun'" 'unbindsym Mod4+Retrun+none'
rejected bindcode-invalid "Invalid keycode or button code '4294967295'" \
	'bindcode Mod4+none+4294967295 exec false'
rejected two-groups "Only one group can be specified" \
	'bindsym Group1+Group2+none exec false'

# Without none, unknown keys are rejected as before.
rejected unknown "Unknown key or button 'nonee'" 'bindsym Mod4+nonee exec false'

echo "PASS: bind-none"
