#!/bin/sh
# FF_NFC on Kiln OS: undo install.sh. Kiln runs it (root's copy) at
# DISABLE_PLUGIN / REMOVE_PLUGIN, then deletes root's copy and the boot
# script. Reader nodes go back to root-only at their next replug or
# reboot.
/bin/sh ./ff-nfc-hook.sh stop
echo "FF_NFC: USB watcher stopped"
exit 0
