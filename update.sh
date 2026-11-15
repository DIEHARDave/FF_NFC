#!/bin/sh
# Run by Zmod after Moonraker's update manager pulls a new version. The
# extras link points into this repository, so an update only needs the link
# to exist; install.sh is idempotent. (Kiln OS does not run it; it runs
# root/update.sh when root/ changed.)
[ -d /usr/data/zmod ] || exit 0
cd "$(dirname "$0")" && ./install.sh
