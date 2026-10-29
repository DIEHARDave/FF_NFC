#!/bin/sh
# FF_NFC uninstall, as root.
#
# Zmod: DISABLE_PLUGIN name=ff_nfc runs this; it removes the extras links.
#
# Kiln OS: removes the root part (install.sh):
#   sh /data/ad5x/plugins/ff_nfc/uninstall.sh
# Reader nodes go back to root-only at their next replug or reboot.
# Then DISABLE_PLUGIN NAME=ff_nfc and delete /data/ad5x/plugins/ff_nfc.

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
    HOOK=/data/ad5x/ff-nfc-hook.sh
    CONF=/data/ad5x/screen.conf
    WATCH_PID=/run/ff-nfc-usb.pid

    [ "$(id -u)" = 0 ] || { echo "uninstall.sh: run as root"; exit 1; }
    start-stop-daemon -K -q -p $WATCH_PID 2>/dev/null || true
    rm -f $WATCH_PID
    if [ -f $CONF ]; then
        sed -i -e '/^# FF_NFC plugin/d' -e "\\|^\\. $HOOK\$|d" $CONF
        # nothing left but comments and blank lines: it was ours
        grep -qv '^[[:space:]]*\(#.*\)\{0,1\}$' $CONF || rm -f $CONF
    fi
    rm -f $HOOK
    echo "FF_NFC root part removed"
}

if [ -d /usr/data/zmod ]; then
    zmod_uninstall
elif [ -d /data/ad5x ]; then
    kiln_uninstall
fi
