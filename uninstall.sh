#!/bin/sh
# FF_NFC uninstall, as root.
#
# Zmod: DISABLE_PLUGIN name=ff_nfc runs this; it removes the extras links.
#
# Kiln OS (plugin API 1.6) doesn't run this file: DISABLE_PLUGIN NAME=ff_nfc
# runs root/uninstall.sh.

[ -f /usr/data/zmod/zmod/.shell/0.sh ] && . /usr/data/zmod/zmod/.shell/0.sh

set -eu

PLUGIN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)

zmod_uninstall() {
    MODULE=ff_nfc.py
    for EXTRAS_DIR in \
        /usr/prog/klipper/klippy/extras \
        /usr/data/zmod/klipper/klippy/extras \
        ${KLIPPER_DIR:+"$KLIPPER_DIR/klippy/extras"}
    do
        target="$EXTRAS_DIR/$MODULE"
        [ -L "$target" ] || continue
        # Only remove the link if it is this plugin's.
        if [ "$(readlink "$target")" = "$PLUGIN_DIR/$MODULE" ] \
            || [ "$(readlink -f "$target" || true)" = "$PLUGIN_DIR/$MODULE" ]; then
            rm "$target"
        fi
    done
    echo "ff_nfc Klipper module links removed"
}

kiln_uninstall() {
    echo "uninstall.sh: on Kiln OS, DISABLE_PLUGIN NAME=ff_nfc removes the root part" >&2
}

if [ -d /usr/data/zmod ]; then
    zmod_uninstall
elif [ -d /data/ad5x ]; then
    kiln_uninstall
fi
