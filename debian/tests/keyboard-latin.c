/*
 * In-build check for 06-keybindings-us-fallback: which layouts get the US
 * fallback for key bindings, and which US keysyms a key yields.
 * Built against sway/input/keyboard_latin.c; needs xkb-data, no display.
 */
#include <stdio.h>
#include <stdlib.h>
#include <xkbcommon/xkbcommon.h>
#include <xkbcommon/xkbcommon-keysyms.h>
#include "sway/input/keyboard_latin.h"

static int failures = 0;

static void check(int ok, const char *what) {
	printf("%s: %s\n", ok ? "PASS" : "FAIL", what);
	if (!ok) {
		failures++;
	}
}

static struct xkb_keymap *compile(struct xkb_context *ctx, const char *layout,
		const char *variant) {
	struct xkb_rule_names names = { .layout = layout, .variant = variant };
	struct xkb_keymap *keymap = xkb_keymap_new_from_names(ctx, &names, 0);
	if (!keymap) {
		fprintf(stderr, "cannot compile %s\n", layout);
		exit(1);
	}
	return keymap;
}

static xkb_keysym_t latin(struct xkb_keymap *keymap, const char *key) {
	const xkb_keysym_t *syms;
	size_t n = keyboard_latin_keysyms(keymap,
			xkb_keymap_key_by_name(keymap, key), &syms);
	return n > 0 ? syms[0] : XKB_KEY_NoSymbol;
}

int main(void) {
	struct xkb_context *ctx = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
	struct xkb_keymap *ruus = compile(ctx, "ru,us", NULL);
	struct xkb_keymap *usde = compile(ctx, "us,de", NULL);
	struct xkb_keymap *gr = compile(ctx, "gr", NULL);
	struct xkb_keymap *dvorak = compile(ctx, "us", "dvorak");

	check(keyboard_layout_lacks_latin(ruus, 0), "Russian lacks Latin letters");
	check(!keyboard_layout_lacks_latin(ruus, 1), "US in ru,us has them");
	check(!keyboard_layout_lacks_latin(usde, 0), "US has them");
	check(!keyboard_layout_lacks_latin(usde, 1), "German has them");
	check(keyboard_layout_lacks_latin(gr, 0), "Greek lacks them");
	check(!keyboard_layout_lacks_latin(dvorak, 0), "Dvorak has them");

	check(latin(ruus, "AB01") == XKB_KEY_z, "<AB01> is z in US (from ru,us)");
	check(latin(ruus, "AD11") == XKB_KEY_bracketleft, "<AD11> is [ in US");
	check(latin(gr, "AC01") == XKB_KEY_a, "<AC01> is a in US (from gr)");
	check(latin(usde, "RTRN") == XKB_KEY_Return, "<RTRN> is Return");

	xkb_keymap_unref(ruus);
	xkb_keymap_unref(usde);
	xkb_keymap_unref(gr);
	xkb_keymap_unref(dvorak);
	xkb_context_unref(ctx);
	return failures ? 1 : 0;
}
