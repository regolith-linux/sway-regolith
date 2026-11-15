#!/bin/sh
# In-build check for 20-screen-magnifier: sway -C accepts the zoom settings
# and rejects malformed ones.
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

# rejected MESSAGE LINE: a config made of LINE alone fails with MESSAGE.
rejected() {
	printf '%s\n' "$2" > "$tmp/rejected.conf"
	if "$SWAY" -C -c "$tmp/rejected.conf" > "$tmp/out.log" 2>&1; then
		fail "'$2': accepted"
	fi
	if ! grep -qF "$1" "$tmp/out.log"; then
		fail "'$2': no \"$1\" in the output"
	fi
}

accepted output \
	'output * zoom 2' \
	'output HEADLESS-1 zoom 1.5 scale 1' \
	'output HEADLESS-1 scale 1 zoom off' \
	'output HEADLESS-1 zoom 32' \
	'output HEADLESS-1 zoom 1'

accepted commands \
	'zoom in' \
	'zoom in 0.25' \
	'zoom out' \
	'zoom out 2' \
	'zoom set 3' \
	'zoom set off' \
	'zoom off' \
	'zoom toggle' \
	'bindsym Mod4+equal zoom in' \
	'bindsym Mod4+minus zoom out 0.5'

accepted tracking \
	'zoom_mouse_tracking centered' \
	'zoom_mouse_tracking proportional' \
	'zoom_mouse_tracking push' \
	'zoom_mouse_tracking none' \
	'zoom_focus_tracking centered' \
	'zoom_focus_tracking push' \
	'zoom_focus_tracking none'

factor="expected a number from 1 to 32"
for bad in 0.5 0 -2 32.5 33 1e3 nan inf -inf abc 2x; do
	rejected "Invalid zoom factor" "output * zoom $bad"
	rejected "$factor" "zoom set $bad"
done
rejected "Missing zoom argument" "output * zoom"
for bad in 0 -1 nan inf abc; do
	rejected "Invalid zoom step '$bad'" "zoom in $bad"
	rejected "Invalid zoom step '$bad'" "zoom out $bad"
done
rejected "Invalid zoom command" "zoom"
rejected "Expected 'zoom in|out" "zoom sideways"
rejected "Expected 'zoom in|out" "zoom in 1 2"
rejected "Expected 'zoom in|out" "zoom set"
rejected "Expected 'zoom in|out" "zoom off 2"
rejected "Expected 'zoom in|out" "zoom toggle 2"
rejected "Expected 'zoom_mouse_tracking centered|proportional|push|none'" \
	"zoom_mouse_tracking follow"
rejected "Expected 'zoom_focus_tracking centered|push|none'" \
	"zoom_focus_tracking proportional"
rejected "Invalid zoom_mouse_tracking command" "zoom_mouse_tracking"
rejected "Invalid zoom_focus_tracking command" "zoom_focus_tracking push none"

echo "PASS: zoom-config"
