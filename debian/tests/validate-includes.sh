#!/bin/sh
# In-build check for 11-validate-included-configs: sway -C fails when a file
# pulled in with include has errors, at any depth, and passes otherwise.
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

# check NAME accepted|rejected: validate $tmp/NAME/config.
check() {
	if "$SWAY" -C -c "$tmp/$1/config" > "$tmp/out.log" 2>&1; then
		[ "$2" = accepted ] || fail "$1: accepted"
	else
		[ "$2" = rejected ] || fail "$1: rejected"
	fi
}

mkdir "$tmp/clean" "$tmp/clean/config.d" "$tmp/clean/config.d/subdir"
printf 'include config.d/*\ninclude does-not-exist/*\n' > "$tmp/clean/config"
printf 'set $mod Mod4\n' > "$tmp/clean/config.d/10_vars"
printf 'bindsym $mod+Return exec false\n' > "$tmp/clean/config.d/20_keys"
# A directory matched by the pattern is skipped, as at run time.
check clean accepted
if grep -q "Error on line" "$tmp/out.log"; then
	fail "clean: errors reported"
fi

mkdir "$tmp/broken" "$tmp/broken/config.d"
printf 'include config.d/*\n' > "$tmp/broken/config"
printf 'set $mod Mod4\n' > "$tmp/broken/config.d/10_vars"
printf '\nbindsym $mod+Retrun exec false\n' > "$tmp/broken/config.d/20_keys"
check broken rejected
grep -qF "Error on line 2 'bindsym \$mod+Retrun exec false'" "$tmp/out.log" ||
	fail "broken: error not reported"
grep -qF "$tmp/broken/config.d/20_keys" "$tmp/out.log" ||
	fail "broken: path of the included file not reported"

mkdir "$tmp/nested" "$tmp/nested/a"
printf 'include a/first\n' > "$tmp/nested/config"
printf 'include second\n' > "$tmp/nested/a/first"
printf 'no_such_command\n' > "$tmp/nested/a/second"
check nested rejected
grep -qF "$tmp/nested/a/second" "$tmp/out.log" ||
	fail "nested: path of the included file not reported"

echo "PASS: validate-includes"
