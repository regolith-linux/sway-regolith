#!/bin/sh
# In-build check for 19-compositing-effects: sway -C accepts the dimming and
# shadow settings and rejects them with a missing, malformed or out-of-range
# argument, also inside a for_window rule.
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

# accepted LINE...: a config made of LINEs validates without output.
accepted() {
	printf '%s\n' "$@" > "$tmp/config"
	if ! "$SWAY" -C -c "$tmp/config" > "$tmp/out.log" 2>&1; then
		fail "rejected: $*"
	fi
	[ ! -s "$tmp/out.log" ] || fail "sway -C printed something for: $*"
}

# rejected LINE: a config made of LINE fails, reporting that line.
rejected() {
	printf '%s\n' "$1" > "$tmp/config"
	if "$SWAY" -C -c "$tmp/config" > "$tmp/out.log" 2>&1; then
		fail "accepted: $1"
	fi
	grep -qF "Error on line 1 '$1'" "$tmp/out.log" || fail "not reported: $1"
}

# $later is meant for sway, set after the rule that uses it.
# shellcheck disable=SC2016
accepted \
	'default_dim_inactive 0.25' \
	'dim_inactive_colors.unfocused #10203040' \
	'dim_inactive_colors.urgent #A0b0C0' \
	'shadows enable' \
	'shadows_on_csd disable' \
	'shadow_blur_radius 99' \
	'shadow_color #000000ff' \
	'shadow_inactive_color #00000040' \
	'shadow_offset -2147483648 2147483647' \
	'for_window [app_id="^a$"] dim_inactive 0.75; border pixel 2' \
	'for_window [app_id="^b$"] border none, dim_inactive "0"' \
	'for_window [app_id="^c$"] dim_inactive $later' \
	'set $later 0.4'
accepted 'default_dim_inactive 0' 'default_dim_inactive 1' 'default_dim_inactive 1.0' \
	'shadow_blur_radius 0' 'shadow_offset +3 -0' 'shadows DISABLE'

for line in \
	'default_dim_inactive' \
	'default_dim_inactive 1.5' \
	'default_dim_inactive -0.1' \
	'default_dim_inactive half' \
	'default_dim_inactive nan' \
	'default_dim_inactive 0.5 0.5' \
	'dim_inactive_colors.unfocused 000000' \
	'dim_inactive_colors.unfocused #00000' \
	'dim_inactive_colors.urgent #GG0000' \
	'shadows on' \
	'shadows_on_csd' \
	'shadow_blur_radius 100' \
	'shadow_blur_radius -1' \
	'shadow_blur_radius 2.5' \
	'shadow_blur_radius 0x10' \
	'shadow_color #0000007' \
	'shadow_inactive_color black' \
	'shadow_offset 4' \
	'shadow_offset 4 y' \
	'shadow_offset 2147483648 0' \
	'shadow_offset 0 -2147483649' \
	'for_window [app_id="^a$"] dim_inactive 2' \
	'for_window [app_id="^a$"] border pixel 2; dim_inactive' \
	'for_window [app_id="^a$"] border pixel 2, dim_inactive x'; do
	rejected "$line"
done

echo "PASS: effects-config"
