#!/bin/sh
# Zmod's environment provides KLIPPER_DIR for the Klipper build that is running.
[ -f /usr/data/zmod/zmod/.shell/0.sh ] && . /usr/data/zmod/zmod/.shell/0.sh

set -eu

PLUGIN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
MODULE=ff_nfc.py
SOURCE="$PLUGIN_DIR/$MODULE"

# The AD5X can have both the native Klipper and Zmod's Klipper 13 installed, so
# link into every extras directory rather than guessing which one is running.
linked=0
for EXTRAS_DIR in \
    /usr/prog/klipper/klippy/extras \
    /usr/data/zmod/klipper/klippy/extras \
    ${KLIPPER_DIR:+"$KLIPPER_DIR/klippy/extras"}
do
    [ -d "$EXTRAS_DIR" ] || continue
    TARGET="$EXTRAS_DIR/$MODULE"

    # A dangling link (e.g. to a path that only exists inside Moonraker's
    # chroot) cannot be a working module, so it is safe to replace.
    if [ -L "$TARGET" ] && [ ! -e "$TARGET" ]; then
        rm "$TARGET"
    fi

    if [ -L "$TARGET" ]; then
        if [ "$(readlink -f "$TARGET")" != "$SOURCE" ]; then
            echo "Refusing to replace another symlink: $TARGET -> $(readlink "$TARGET")" >&2
            exit 1
        fi
    elif [ -e "$TARGET" ]; then
        echo "Refusing to replace an existing Klipper module: $TARGET" >&2
        exit 1
    else
        ln -s "$SOURCE" "$TARGET"
    fi
    linked=$((linked + 1))
    echo "ff_nfc linked in $EXTRAS_DIR"
done

if [ "$linked" -eq 0 ]; then
    echo "Could not find a supported Zmod Klipper extras directory" >&2
    exit 1
fi

echo "ff_nfc installed"
