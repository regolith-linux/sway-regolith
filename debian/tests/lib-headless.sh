# Shared helpers for the headless autopkgtests (sourced, not run).

fail() {
	echo "FAIL: $*"
	exit 1
}

# Re-run the calling test inside a private D-Bus session bus.
in_dbus_session() {
	if [ -z "${SWAY_REGOLITH_TEST_BUS:-}" ]; then
		SWAY_REGOLITH_TEST_BUS=1 exec dbus-run-session -- sh "$0" "$@"
	fi
}

cleanup() {
	if [ -n "${SWAY_PID:-}" ]; then
		kill "$SWAY_PID" 2>/dev/null || true
		wait "$SWAY_PID" 2>/dev/null || true
	fi
	if [ -n "${TRAWLD_PID:-}" ]; then
		kill "$TRAWLD_PID" 2>/dev/null || true
		wait "$TRAWLD_PID" 2>/dev/null || true
	fi
	rm -rf "$WORK"
}

setup_runtime() {
	WORK=$(mktemp -d)
	mkdir -m 700 "$WORK/run"
	export XDG_RUNTIME_DIR="$WORK/run"
	export WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 WLR_RENDERER=pixman
	trap cleanup EXIT
}

start_trawld() {
	echo "trawld under test: $(dpkg-query -W -f '${Package} ${Version}' trawld) ($(command -v trawld))"
	trawld > "$WORK/trawld.log" 2>&1 &
	TRAWLD_PID=$!
	# trawldb exits 0 even when trawld is not on the bus yet, so wait for the
	# bus name itself.
	i=0
	until dbus-send --session --print-reply --dest=org.freedesktop.DBus \
			/org/freedesktop/DBus org.freedesktop.DBus.NameHasOwner \
			string:org.regolith.Trawl 2>/dev/null | grep -q 'boolean true'; do
		i=$((i + 1))
		if [ "$i" -gt 300 ] || ! kill -0 "$TRAWLD_PID" 2>/dev/null; then
			cat "$WORK/trawld.log"
			fail "trawld did not come up"
		fi
		sleep 0.2
	done
}

# load_resources FILE KEY: load FILE into trawl and confirm KEY is now set.
load_resources() {
	trawldb --load "$1"
	trawldb --query 2>&1 | grep -q "^$2[[:space:]]*:" ||
		fail "resource $2 not present in trawl after loading $1"
}

# start_sway CONFIG: start headless sway and export SWAYSOCK once IPC answers.
start_sway() {
	sway -c "$1" > "$WORK/sway.log" 2>&1 &
	SWAY_PID=$!
	i=0
	while [ "$i" -lt 50 ]; do
		for sock in "$XDG_RUNTIME_DIR"/sway-ipc.*.sock; do
			if [ -S "$sock" ] && swaymsg -s "$sock" -t get_version > /dev/null 2>&1; then
				export SWAYSOCK="$sock"
				return 0
			fi
		done
		kill -0 "$SWAY_PID" 2>/dev/null || break
		i=$((i + 1))
		sleep 0.2
	done
	cat "$WORK/sway.log"
	fail "headless sway did not start"
}
