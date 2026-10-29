#!/bin/sh
# FF_NFC install, as root.
#
# Zmod: ENABLE_PLUGIN name=ff_nfc runs this (and update.sh after updates).
# It links ff_nfc.py into Klipper's extras directories.
#
# Kiln OS: run it once after cloning, and again when NFC_STATUS says the
# hook is outdated:
#   sh /data/ad5x/plugins/ff_nfc/install.sh
# It installs the root part a plugin running as user klipper can't do
# itself, needed for USB readers and the scan beep on the stock kernel
# (LaneTag and other network readers work without it): root/ff-nfc-hook.sh
# is copied to /data/ad5x/ff-nfc-hook.sh (root-owned, so the clone that
# Moonraker updates as klipper is never run as root), its line is added to
# /data/ad5x/screen.conf (sourced as root at every boot by Kiln's screen
# service) and it is started. A stopgap: Kiln has no boot hook for plugins.
# KILN_NFC's hook, if it is still there, is removed.

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
    HOOK=/data/ad5x/ff-nfc-hook.sh
    CONF=/data/ad5x/screen.conf
    WATCH_PID=/run/ff-nfc-usb.pid
    SERVICE=/etc/init.d/S65ad5x-screen
    LINE=". $HOOK"

    [ "$(id -u)" = 0 ] || { echo "install.sh: run as root"; exit 1; }
    grep -qs '^[[:space:]]*\[ -f \$CONF \] && \. \$CONF' $SERVICE &&
        grep -qs "^CONF=$CONF\$" $SERVICE || {
        echo "install.sh: $SERVICE missing or doesn't source $CONF;"
        echo "this Kiln OS has no hook left to use (see README)"
        exit 1
    }

    # KILN_NFC's hook (screen.conf, or camera.conf from its hook version 1)
    OLD_HOOK=/data/ad5x/nfc-hook.sh
    OLD_PID=/run/kiln-nfc-usb.pid
    if [ -f $OLD_HOOK ] || [ -f $OLD_PID ]; then
        start-stop-daemon -K -q -p $OLD_PID 2>/dev/null || true
        rm -f $OLD_PID
        for OLD_CONF in $CONF /data/ad5x/camera.conf; do
            [ -f $OLD_CONF ] || continue
            sed -i -e '/^# KILN_NFC plugin/d' -e "\\|^\\. $OLD_HOOK\$|d" $OLD_CONF
            # nothing left but comments and blank lines: it was KILN_NFC's
            grep -qv '^[[:space:]]*\(#.*\)\{0,1\}$' $OLD_CONF || rm -f $OLD_CONF
        done
        rm -f $OLD_HOOK
        echo "removed KILN_NFC's hook"
    fi

    cp "$PLUGIN_DIR/root/ff-nfc-hook.sh" $HOOK.new
    chown root:root $HOOK.new
    chmod 755 $HOOK.new
    mv $HOOK.new $HOOK
    echo "installed $HOOK (version $(sed -n 's/^FF_NFC_HOOK_VERSION=//p' $HOOK))"

    if ! grep -qxF "$LINE" $CONF 2>/dev/null; then
        [ -f $CONF ] && cp -p $CONF $CONF.before-ff-nfc
        printf '# FF_NFC plugin (uninstall.sh removes these two lines)\n%s\n' \
            "$LINE" >> $CONF
        echo "added the hook to $CONF"
    fi
    chown root:root $CONF
    chmod 644 $CONF

    # Restart the watcher with the new code (the screen stream keeps running)
    start-stop-daemon -K -q -p $WATCH_PID 2>/dev/null || true
    rm -f $WATCH_PID
    start-stop-daemon -S -b -m -p $WATCH_PID -x /bin/sh -- $HOOK watch
    echo "USB watcher started: plug the reader in (or replug it) and run NFC_STATUS"
}

if [ -d /usr/data/zmod ]; then
    zmod_install
elif [ -d /data/ad5x ]; then
    kiln_install
else
    echo "install.sh: neither Zmod nor Kiln OS found" >&2
    exit 1
fi
