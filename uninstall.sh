#!/bin/sh
[ -f /usr/data/zmod/zmod/.shell/0.sh ] && . /usr/data/zmod/zmod/.shell/0.sh

set -eu

PLUGIN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
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
