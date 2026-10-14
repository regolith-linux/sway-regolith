#!/bin/sh
# In-build check for 01-regolith-trawl: set_from_resource uses its fallback
# when the resource is missing, and fails when no fallback is given.
# Runs the freshly built binary; needs neither a display nor trawld.
set -eu

SWAY=${1:?usage: $0 path/to/built/sway}

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -m 700 "$tmp/run"
export XDG_RUNTIME_DIR="$tmp/run"
export WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 WLR_RENDERER=pixman
# No session bus: every trawl lookup misses, whatever the build host runs.
export DBUS_SESSION_BUS_ADDRESS="unix:path=$tmp/no-bus"
# GLib criticals (for example a call on a NULL trawl proxy) fail the test.
export G_DEBUG=fatal-criticals

printf 'set_from_resource $x some.missing.key fallback\n' > "$tmp/fallback.conf"
printf 'set_from_resource $x some.missing.key\n' > "$tmp/no-fallback.conf"

if ! "$SWAY" -C -c "$tmp/fallback.conf" > "$tmp/fallback.log" 2>&1; then
	cat "$tmp/fallback.log"
	echo "FAIL: set_from_resource with a fallback was rejected"
	exit 1
fi

if "$SWAY" -C -c "$tmp/no-fallback.conf" > "$tmp/no-fallback.log" 2>&1; then
	cat "$tmp/no-fallback.log"
	echo "FAIL: set_from_resource without a fallback was accepted"
	exit 1
fi
if ! grep -q "failed to fetch value of resource" "$tmp/no-fallback.log"; then
	cat "$tmp/no-fallback.log"
	echo "FAIL: missing 'failed to fetch value of resource' error"
	exit 1
fi

echo "PASS: set_from_resource fallback"
