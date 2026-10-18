#!/bin/sh
# Run by Zmod after Moonraker's update manager pulls a new version. The
# extras link points into this repository, so an update only needs the link
# to exist; install.sh is idempotent.
cd "$(dirname "$0")" && ./install.sh
