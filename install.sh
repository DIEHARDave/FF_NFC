#!/bin/sh
# FF_NFC install, as root.
#
# Zmod: ENABLE_PLUGIN name=ff_nfc runs this (and update.sh after updates).
# It links ff_nfc.py into Klipper's extras directories.
#
# Kiln OS (plugin API 1.6) doesn't run this file: its root part is in
# root/ (ENABLE_PLUGIN NAME=ff_nfc ROOT=1).

# Zmod's environment provides KLIPPER_DIR for the Klipper build that is running.
[ -f /usr/data/zmod/zmod/.shell/0.sh ] && . /usr/data/zmod/zmod/.shell/0.sh

set -eu

PLUGIN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)

zmod_install() {
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

        # zmod_nfc, which FF_NFC replaces: a link left after its clone was
        # deleted goes; a working one means both would open the reader.
        OLD="$EXTRAS_DIR/zmod_nfc.py"
        if [ -L "$OLD" ] && [ ! -e "$OLD" ]; then
            rm "$OLD"
            echo "removed the dead zmod_nfc link in $EXTRAS_DIR"
        elif [ -e "$OLD" ]; then
            echo "zmod_nfc is still installed: DISABLE_PLUGIN name=zmod_nfc (see README)" >&2
        fi
    done

    if [ "$linked" -eq 0 ]; then
        echo "Could not find a supported Zmod Klipper extras directory" >&2
        exit 1
    fi

    echo "ff_nfc installed"
}

kiln_install() {
    echo "install.sh: on Kiln OS, enable FF_NFC with ENABLE_PLUGIN NAME=ff_nfc ROOT=1" >&2
    echo "  (Kiln runs root/install.sh then; see README)" >&2
    exit 1
}

if [ -d /usr/data/zmod ]; then
    zmod_install
elif [ -d /data/ad5x ]; then
    kiln_install
else
    echo "install.sh: neither Zmod nor Kiln OS found" >&2
    exit 1
fi
