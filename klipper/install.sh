#!/bin/bash
# lanetag-nfc installer for any Klipper printer with Moonraker (Sovol,
# Voron, RatRig, ...). Zmod and Kiln OS have their own install paths; see
# README.md.
#
#   cd ~ && git clone https://github.com/DIEHARDave/lanetag-nfc.git
#   ~/lanetag-nfc/klipper/install.sh
#
# Run it as the user Klipper runs as (not root); sudo is used for the udev
# rule and the service restarts. Running it again is safe.
#
# Environment: PRINTER_DATA (default ~/printer_data), KLIPPER_DIR (default
# ~/klipper).

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PRINTER_DATA="${PRINTER_DATA:-${HOME}/printer_data}"
KLIPPER_DIR="${KLIPPER_DIR:-${HOME}/klipper}"
CONFIG_DIR="${PRINTER_DATA}/config"
PRINTER_CFG="${CONFIG_DIR}/printer.cfg"
MOONRAKER_CONF="${CONFIG_DIR}/moonraker.conf"
RULE="99-lanetag-nfc.rules"
INCLUDE="[include ff_nfc.cfg]"
SAVE_MARK="#*# <---------------------- SAVE_CONFIG ---------------------->"

msg() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mNOTE:\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "run as the user Klipper runs as, not root (sudo is used where needed)"
[ -d "${KLIPPER_DIR}/klippy/extras" ] || die "no Klipper at ${KLIPPER_DIR} (set KLIPPER_DIR)"
[ -f "$PRINTER_CFG" ] || die "$PRINTER_CFG not found (set PRINTER_DATA)"
[ -f "$MOONRAKER_CONF" ] || die "$MOONRAKER_CONF not found (set PRINTER_DATA)"
command -v python3 > /dev/null || die "python3 not found"

msg "Linking the Klipper module"
ln -sfn "${REPO_DIR}/klipper/ff_nfc.py" "${KLIPPER_DIR}/klippy/extras/ff_nfc.py"
exclude="${KLIPPER_DIR}/.git/info/exclude"
if [ -d "$(dirname "$exclude")" ] && ! grep -qx "klippy/extras/ff_nfc.py" "$exclude" 2> /dev/null; then
    # Keeps Moonraker from calling Klipper's checkout dirty.
    echo "klippy/extras/ff_nfc.py" >> "$exclude"
fi

msg "Including ff_nfc.cfg in printer.cfg"
ln -sfn "${REPO_DIR}/ff_nfc.cfg" "${CONFIG_DIR}/ff_nfc.cfg"
if ! grep -qF "$INCLUDE" "$PRINTER_CFG"; then
    cp "$PRINTER_CFG" "${PRINTER_CFG}.ff_nfc.bak"
    # Above SAVE_CONFIG's block, which must stay last.
    awk -v inc="$INCLUDE" -v mark="$SAVE_MARK" '
        !done && index($0, mark) == 1 { print inc; print ""; done = 1 }
        { print }
        END { if (!done) { print ""; print inc } }' "${PRINTER_CFG}.ff_nfc.bak" > "$PRINTER_CFG"
fi

msg "Giving Klipper access to USB readers"
sudo install -m 644 "${REPO_DIR}/klipper/${RULE}" "/etc/udev/rules.d/${RULE}"
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb --action=add
sudo udevadm settle || true
for group in dialout plugdev; do
    getent group "$group" > /dev/null || continue
    id -nG | tr ' ' '\n' | grep -qx "$group" || sudo usermod -aG "$group" "$USER"
done
if systemctl is-active --quiet pcscd.socket pcscd.service 2> /dev/null; then
    warn "pcscd is running and claims PC/SC readers (ACR122U, ACR1552U) before lanetag-nfc can."
    warn "If you use one of those, stop it: sudo systemctl disable --now pcscd.socket pcscd.service"
fi

msg "Adding the Moonraker update manager entry"
if [ ! -f "${CONFIG_DIR}/ff_nfc.conf" ]; then
    sed "s#@REPO_DIR@#${REPO_DIR}#" "${REPO_DIR}/klipper/ff_nfc.conf" > "${CONFIG_DIR}/ff_nfc.conf"
fi
if ! grep -q '^\[include ff_nfc\.conf\]' "$MOONRAKER_CONF"; then
    cp "$MOONRAKER_CONF" "${MOONRAKER_CONF}.ff_nfc.bak"
    printf '\n[include ff_nfc.conf]\n' >> "$MOONRAKER_CONF"
fi

msg "Restarting Moonraker and Klipper"
sudo systemctl restart moonraker
state=$(curl -s -m 5 "http://127.0.0.1:7125/printer/objects/query?print_stats=state" 2> /dev/null \
    | sed -n 's/.*"state": *"\([a-z]*\)".*/\1/p')
case "$state" in
    printing|paused) echo "A print is $state; lanetag-nfc starts at Klipper's next restart." ;;
    *) sudo systemctl restart klipper ;;
esac

msg "Done. Plug in a reader and run NFC_STATUS in the console."
echo "    A new group membership (dialout, plugdev) needs a reboot before Klipper has it."
