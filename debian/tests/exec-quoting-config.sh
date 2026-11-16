#!/bin/sh
# In-build check for 21-exec-quoting-parity: sway -C judges exec and
# exec_always lines by i3's config reading. Text after the closing quote
# and an empty quoted command are errors; the rest of an unquoted line,
# ; and , included, is the command; a binding is only judged when it fires.
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

# check accepted|rejected LINE: validate a config made of LINE.
check() {
	printf '%s\n' "$2" > "$tmp/config"
	if "$SWAY" -C -c "$tmp/config" > "$tmp/out.log" 2>&1; then
		[ "$1" = accepted ] || fail "accepted: $2"
		! grep -q "Error on line" "$tmp/out.log" || fail "errors reported: $2"
	else
		[ "$1" = rejected ] || fail "rejected: $2"
	fi
}

check accepted 'exec "\"/opt/My App/run me.sh\" --flag"'
check accepted 'exec_always --no-startup-id "\"/opt/My App/run me.sh\""   '
check accepted "exec /bin/true; exec /bin/false, \"a b\" it\\'s"
check accepted "exec 'single quoted'"
check accepted 'bindsym Mod4+y exec "\\"/opt/My App/run me.sh\\" --ready"'
# Text after the closing quote is reported when the binding fires.
check accepted 'bindsym Mod4+u exec "/bin/true" junk'

check rejected 'exec "/bin/true" junk'
grep -qF "expected only blanks after the closing quote, got 'junk'" "$tmp/out.log" ||
	fail "trailing text: error not reported"
check rejected 'exec_always "/bin/true"junk'
check rejected 'exec ""'
grep -qF "the quoted command is empty" "$tmp/out.log" ||
	fail "empty command: error not reported"
check rejected 'exec --no-startup-id'
# 08-variable-command-name: a variable holding exec reads the same way.
# shellcheck disable=SC2016 # $ex is for sway to replace
check rejected "$(printf 'set $ex exec\n$ex "/bin/true" junk')"

echo "PASS: exec-quoting-config"
