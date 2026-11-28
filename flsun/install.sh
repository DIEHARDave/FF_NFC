#!/bin/bash
# lanetag-nfc installer for the FLSun T1 / T1 Pro (formerly FLSUN_NFC).
# FLSUN_Updater runs it (plugin lanetag-nfc); by hand, as the pi user:
#
#   cd ~ && git clone https://github.com/DIEHARDave/lanetag-nfc.git
#   ~/lanetag-nfc/flsun/install.sh
#
# It replaces an FLSUN_NFC install in place: same Klipper module
# (flsun_nfc.py), section ([flsun_nfc]), config (flsun_nfc.cfg,
# flsun_nfc_user*.cfg) and udev rules, so your settings keep working. Only
# the update entry moves from flsun_nfc.conf to lanetag-nfc.conf.

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${HOME}/printer_data/config"
PRINTER_CFG="${CONFIG_DIR}/printer.cfg"
MOONRAKER_CONF="${CONFIG_DIR}/moonraker.conf"
UDEV_RULES="99-flsun-nfc.rules 61-flsun-nfc-mcu.rules"
INCLUDE="[include flsun_nfc.cfg]"
SAVE_MARK="#*# <---------------------- SAVE_CONFIG ---------------------->"

msg() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "run as the pi user, not root (sudo is used where needed)"
[ -f "$PRINTER_CFG" ] || die "$PRINTER_CFG not found"
[ -f "$MOONRAKER_CONF" ] || die "$MOONRAKER_CONF not found"

msg "Linking the Klipper module"
linked=0
for klipper in "${HOME}/klipper-upstream" "${HOME}/klipper"; do
    extras="${klipper}/klippy/extras"
    [ -d "$extras" ] || continue
    ln -sfn "${REPO_DIR}/flsun/flsun_nfc.py" "${extras}/flsun_nfc.py"
    linked=1
    exclude="${klipper}/.git/info/exclude"
    if [ -d "$(dirname "$exclude")" ] && ! grep -qx "klippy/extras/flsun_nfc.py" "$exclude" 2> /dev/null; then
        echo "klippy/extras/flsun_nfc.py" >> "$exclude"
    fi
done
[ "$linked" = 1 ] || die "no Klipper found in ${HOME}/klipper-upstream or ${HOME}/klipper"

msg "Including flsun_nfc.cfg in printer.cfg"
ln -sfn "${REPO_DIR}/flsun/flsun_nfc.cfg" "${CONFIG_DIR}/flsun_nfc.cfg"
if ! grep -qF "$INCLUDE" "$PRINTER_CFG"; then
    cp "$PRINTER_CFG" "${PRINTER_CFG}.flsun_nfc.bak"
    awk -v inc="$INCLUDE" -v mark="$SAVE_MARK" '
        !done && index($0, mark) == 1 { print inc; print ""; done = 1 }
        { print }
        END { if (!done) { print ""; print inc } }' "${PRINTER_CFG}.flsun_nfc.bak" > "$PRINTER_CFG"
fi

msg "Giving Klipper (user pi) access to USB readers"
for rule in $UDEV_RULES; do
    sudo install -m 644 "${REPO_DIR}/flsun/${rule}" "/etc/udev/rules.d/${rule}"
done
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb --action=add
sudo udevadm trigger --subsystem-match=tty --action=add
sudo udevadm settle
mainboard=$(readlink -f /dev/serial/by-path/platform-xhci-hcd.0.auto-usb-0:1:1.0-port0 2> /dev/null || true)
byid=$(readlink -f /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0 2> /dev/null || true)
if [ -n "$mainboard" ] && [ "$mainboard" != "$byid" ]; then
    die "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0 points to $byid, not the mainboard ($mainboard). Unplug CH340 USB devices other than the mainboard and run install.sh again; don't restart Klipper until then."
fi
for group in dialout plugdev; do
    id -nG | tr ' ' '\n' | grep -qx "$group" || sudo usermod -aG "$group" "$USER"
done

msg "Adding the Moonraker update manager entry"
if [ ! -f "${CONFIG_DIR}/lanetag-nfc.conf" ]; then
    sed "s#@REPO_DIR@#${REPO_DIR}#" "${REPO_DIR}/flsun/lanetag-nfc.conf" > "${CONFIG_DIR}/lanetag-nfc.conf"
fi
if ! grep -q '^\[include lanetag-nfc\.conf\]' "$MOONRAKER_CONF"; then
    cp "$MOONRAKER_CONF" "${MOONRAKER_CONF}.lanetag-nfc.bak"
    printf '\n[include lanetag-nfc.conf]\n' >> "$MOONRAKER_CONF"
fi
# FLSUN_NFC's own update entry (the repo it pointed at is retired).
if grep -q '^\[include flsun_nfc\.conf\]' "$MOONRAKER_CONF"; then
    sed -i '/^\[include flsun_nfc\.conf\]/d' "$MOONRAKER_CONF"
fi
rm -f "${CONFIG_DIR}/flsun_nfc.conf"

msg "Restarting Moonraker and Klipper"
sudo systemctl restart moonraker
state=$(curl -s -m 5 "http://127.0.0.1:7125/printer/objects/query?print_stats=state" \
    | sed -n 's/.*"state": *"\([a-z]*\)".*/\1/p')
case "$state" in
    printing|paused) echo "A print is $state; lanetag-nfc starts at Klipper's next restart." ;;
    *) sudo systemctl restart klipper ;;
esac

if [ -d "${HOME}/FLSUN_NFC" ] && [ "${HOME}/FLSUN_NFC" != "$REPO_DIR" ]; then
    msg "lanetag-nfc replaced FLSUN_NFC; ~/FLSUN_NFC is no longer used and can be deleted."
fi
msg "Done. Plug in a reader and run NFC_STATUS in the console."
