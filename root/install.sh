#!/bin/sh
# FF_NFC on Kiln OS: root's part, plugin API 1.6. Kiln runs it as root
# from /data/ad5x/plugins-root/ff_nfc (root's copy of root/) at
#   ENABLE_PLUGIN NAME=ff_nfc ROOT=1
# and, as update.sh, after each update that changes root/. (Zmod uses the
# install.sh at the top of the repository instead.)
#  1. removes the hooks installed by hand before: FF_NFC 0.2's line in
#     /data/ad5x/screen.conf and /data/ad5x/ff-nfc-hook.sh, and KILN_NFC's
#     /data/ad5x/nfc-hook.sh, with their watchers
#  2. puts the boot script S50ff_nfc in place and runs it: the USB watcher
#     that lets Klipper open USB readers and the speaker
set -e
ROOTDIR=/data/ad5x/plugins-root
SCREEN=/data/ad5x/screen.conf
WATCH_PID=/run/ff-nfc-usb.pid

# hook file, pid file, comment marker of the hand-installed hooks
for OLD in "/data/ad5x/ff-nfc-hook.sh /run/ff-nfc-usb.pid FF_NFC" \
	"/data/ad5x/nfc-hook.sh /run/kiln-nfc-usb.pid KILN_NFC"; do
	set -- $OLD
	start-stop-daemon -K -q -p $2 2>/dev/null || true
	rm -f $2
	for CONF in $SCREEN /data/ad5x/camera.conf; do
		grep -qxF ". $1" $CONF 2>/dev/null || continue
		sed -i -e "/^# $3 plugin/d" -e "\\|^\\. $1\$|d" $CONF
		echo "removed the old hook line ($1) from $CONF"
		# nothing left but comments and blank lines: it was ours
		grep -qv '^[[:space:]]*\(#.*\)\{0,1\}$' $CONF || rm -f $CONF
	done
	rm -f $1
done

mkdir -p $ROOTDIR/init.d
cp S50ff_nfc $ROOTDIR/init.d/S50ff_nfc.new
chmod 755 $ROOTDIR/init.d/S50ff_nfc.new
mv $ROOTDIR/init.d/S50ff_nfc.new $ROOTDIR/init.d/S50ff_nfc
$ROOTDIR/init.d/S50ff_nfc start
echo "FF_NFC: USB watcher started; plug the reader in (or replug it) and run NFC_STATUS"
