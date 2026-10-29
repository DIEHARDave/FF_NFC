#!/bin/sh
# Run by Zmod after Moonraker's update manager pulls a new version. The
# extras link points into this repository, so an update only needs the link
# to exist; install.sh is idempotent. (Kiln OS does not run it; there
# NFC_STATUS asks for install.sh when the root hook changes.)
[ -d /usr/data/zmod ] || exit 0
cd "$(dirname "$0")" && ./install.sh
