#!/bin/sh
# In-build check for 25-tiling-drag-i3-modes: sway -C accepts i3's
# tiling_drag forms (drag styles in either order, i3's boolean words,
# swap_modifier with + combinations) and rejects anything else, naming the
# offending line.
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

# check accepted|rejected LINE: validate a config whose second line is LINE.
check() {
	printf '%s\n' 'floating_modifier Mod4' "$2" > "$tmp/config"
	if "$SWAY" -C -c "$tmp/config" > "$tmp/out.log" 2>&1; then
		[ "$1" = accepted ] || fail "accepted: $2"
		! grep -q "Error on line" "$tmp/out.log" || fail "errors reported: $2"
	else
		[ "$1" = rejected ] || fail "rejected: $2"
		grep -q "Error on line 2" "$tmp/out.log" || fail "line not named: $2"
	fi
}

for line in 'tiling_drag modifier titlebar' 'tiling_drag titlebar modifier' \
		'tiling_drag modifier' 'tiling_drag titlebar' 'tiling_drag off' \
		'tiling_drag Modifier TITLEBAR' 'tiling_drag toggle' \
		'tiling_drag swap_modifier Control' 'tiling_drag swap_modifier Mod1+Shift' \
		'tiling_drag swap_modifier none'; do
	check accepted "$line"
done
for word in enable enabled yes on true active 1 disable no false inactive 0; do
	check accepted "tiling_drag $word"
done

for line in 'tiling_drag' 'tiling_drag bogus' 'tiling_drag disabled' \
		'tiling_drag titlebar titlebar' 'tiling_drag modifier on' \
		'tiling_drag modifier titlebar off' 'tiling_drag swap_modifier' \
		'tiling_drag swap_modifier Bogus' 'tiling_drag swap_modifier Mod1+Bogus' \
		'tiling_drag swap_modifier Shift+' 'tiling_drag swap_modifier +Shift' \
		'tiling_drag swap_modifier Shift++Mod1' 'tiling_drag swap_modifier Shift Control'; do
	check rejected "$line"
done

echo "PASS: tiling-drag-config"
