#!/bin/bash
# Removes lanetag-nfc from the FLSun T1. Spoolman spools and their linked
# tags are kept, and so is flsun_nfc_user.cfg. Run as the pi user.

set -e

CONFIG_DIR="${HOME}/printer_data/config"
PRINTER_CFG="${CONFIG_DIR}/printer.cfg"
MOONRAKER_CONF="${CONFIG_DIR}/moonraker.conf"

msg() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

[ "$(id -u)" -ne 0 ] || { echo "run as the pi user, not root" >&2; exit 1; }

msg "Removing the Klipper module and config"
for klipper in "${HOME}/klipper-upstream" "${HOME}/klipper"; do
    rm -f "${klipper}/klippy/extras/flsun_nfc.py"
    exclude="${klipper}/.git/info/exclude"
    [ -f "$exclude" ] && sed -i '\|^klippy/extras/flsun_nfc\.py$|d' "$exclude"
done
sed -i '/^\[include flsun_nfc\.cfg\]/d' "$PRINTER_CFG"
rm -f "${CONFIG_DIR}/flsun_nfc.cfg"

msg "Removing the USB reader rules"
sudo rm -f /etc/udev/rules.d/99-flsun-nfc.rules /etc/udev/rules.d/61-flsun-nfc-mcu.rules
sudo udevadm control --reload-rules

msg "Removing the Moonraker settings"
sed -i '/^\[include lanetag-nfc\.conf\]/d' "$MOONRAKER_CONF"
rm -f "${CONFIG_DIR}/lanetag-nfc.conf"

msg "Restarting Moonraker and Klipper"
sudo systemctl restart moonraker
state=$(curl -s -m 5 "http://127.0.0.1:7125/printer/objects/query?print_stats=state" \
    | sed -n 's/.*"state": *"\([a-z]*\)".*/\1/p')
case "$state" in
    printing|paused) echo "A print is $state; Klipper drops lanetag-nfc at its next restart." ;;
    *) sudo systemctl restart klipper ;;
esac
msg "Done. Delete flsun_nfc_user.cfg if you made one, then ~/lanetag-nfc."
