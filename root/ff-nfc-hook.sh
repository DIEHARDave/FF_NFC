#!/bin/sh
#####################################################################
# FF_NFC root hook: USB and speaker access for FF_NFC on Kiln OS (AD5X)
#
# Klipper runs as user klipper, and Kiln's /dev/bus/usb nodes are
# root-only (mdev.conf is on the read-only system). Kiln's stock 5.10
# kernel also has no USB serial drivers, so the plugin drives the
# readers through those nodes itself (usbfs).
#
# Kiln OS plugin API 1.6 runs it as root from root's copy of the
# plugin's root/ folder (/data/ad5x/plugins-root/ff_nfc), never from the
# clone user klipper owns:
#   ff-nfc-hook.sh start   boot script S50ff_nfc (and install.sh): starts
#                          the watcher
#   ff-nfc-hook.sh stop    stops it
#   ff-nfc-hook.sh watch   the watcher
# The watcher gives group klipper read/write on the nodes of USB devices
# that can be NFC readers - CCID class (ACR122U, ACR1552U), and the
# serial chips PN532 boards and reader bridges use - whenever USB devices
# come or go. It also gives group klipper /dev/jz_pwm (the stock
# kernel's PWM driver, behind the speaker), for the scan beep. Nothing
# else is touched. (A udev rule would not do: on the stock kernel Kiln
# runs mdev, and /dev/jz_pwm only exists there.)
#####################################################################

# Bump when this file changes: NFC_STATUS compares it with root's copy
# (Kiln runs update.sh after a git update, so they should match)
FF_NFC_HOOK_VERSION=2

NFC_HOOK=/data/ad5x/plugins-root/ff_nfc/ff-nfc-hook.sh
NFC_WATCH_PID=/run/ff-nfc-usb.pid

# Vendors of the USB serial chips and boards the plugin probes (keep in
# sync with readers/detect.py PROBE_VIDS), and ACS (CCID readers)
NFC_VIDS="1a86 4348 10c4 067b 0403 2e8a 303a 072f"

# nfc_reader DIR: true when the USB device at sysfs DIR may be a reader
nfc_reader() {
	nfc_vid=$(cat "$1/idVendor" 2>/dev/null)
	for v in $NFC_VIDS; do
		[ "$nfc_vid" = "$v" ] && return 0
	done
	for c in "$1"/*:*/bInterfaceClass; do
		[ "$(cat "$c" 2>/dev/null)" = 0b ] && return 0
	done
	return 1
}

nfc_grant() {
	for d in /sys/bus/usb/devices/*; do
		[ -f "$d/busnum" ] && [ -f "$d/devnum" ] || continue
		nfc_reader "$d" || continue
		node=$(printf '/dev/bus/usb/%03d/%03d' "$(cat $d/busnum)" "$(cat $d/devnum)")
		[ -c "$node" ] || continue
		chgrp klipper "$node" && chmod 660 "$node"
	done
}

nfc_watch() {
	[ -c /dev/jz_pwm ] && chgrp klipper /dev/jz_pwm && chmod 660 /dev/jz_pwm
	last=
	while :; do
		now=$(ls /sys/bus/usb/devices 2>/dev/null | tr '\n' ' ')
		if [ "$now" != "$last" ]; then
			# mdev creates the node a moment after sysfs shows the device
			sleep 1
			nfc_grant
			last=$now
		fi
		sleep 2
	done
}

nfc_running() {
	[ -f $NFC_WATCH_PID ] && kill -0 "$(cat $NFC_WATCH_PID)" 2>/dev/null
}

case "$1" in
watch)
	nfc_watch
	;;
start)
	nfc_running ||
		start-stop-daemon -S -b -m -p $NFC_WATCH_PID -x /bin/sh -- $NFC_HOOK watch
	;;
stop)
	start-stop-daemon -K -q -p $NFC_WATCH_PID 2>/dev/null
	rm -f $NFC_WATCH_PID
	;;
*)
	echo "usage: $0 start|stop|watch"
	exit 1
	;;
esac
exit 0
