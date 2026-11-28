#!/bin/bash
# Removes what klipper/install.sh added. The clone itself stays; delete it
# afterwards if you like. Run as the user Klipper runs as.

set -e

PRINTER_DATA="${PRINTER_DATA:-${HOME}/printer_data}"
KLIPPER_DIR="${KLIPPER_DIR:-${HOME}/klipper}"
CONFIG_DIR="${PRINTER_DATA}/config"
PRINTER_CFG="${CONFIG_DIR}/printer.cfg"
MOONRAKER_CONF="${CONFIG_DIR}/moonraker.conf"

msg() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

[ "$(id -u)" -ne 0 ] || { echo "run as the user Klipper runs as, not root" >&2; exit 1; }

msg "Removing the Klipper module and config include"
rm -f "${KLIPPER_DIR}/klippy/extras/ff_nfc.py"
if [ -f "$PRINTER_CFG" ] && grep -qx '\[include ff_nfc\.cfg\]' "$PRINTER_CFG"; then
    cp "$PRINTER_CFG" "${PRINTER_CFG}.ff_nfc-uninstall.bak"
    grep -vx '\[include ff_nfc\.cfg\]' "${PRINTER_CFG}.ff_nfc-uninstall.bak" > "$PRINTER_CFG"
fi
# A symlink into the clone; your own [ff_nfc] settings elsewhere stay.
[ -L "${CONFIG_DIR}/ff_nfc.cfg" ] && rm -f "${CONFIG_DIR}/ff_nfc.cfg"

msg "Removing the Moonraker update manager entry"
if [ -f "$MOONRAKER_CONF" ] && grep -qx '\[include ff_nfc\.conf\]' "$MOONRAKER_CONF"; then
    cp "$MOONRAKER_CONF" "${MOONRAKER_CONF}.ff_nfc-uninstall.bak"
    grep -vx '\[include ff_nfc\.conf\]' "${MOONRAKER_CONF}.ff_nfc-uninstall.bak" > "$MOONRAKER_CONF"
fi
rm -f "${CONFIG_DIR}/ff_nfc.conf"

msg "Removing the udev rule"
sudo rm -f /etc/udev/rules.d/99-lanetag-nfc.rules
sudo udevadm control --reload-rules

msg "Restarting Moonraker and Klipper"
sudo systemctl restart moonraker
sudo systemctl restart klipper
msg "Done. Remove any [ff_nfc] section you added yourself."
