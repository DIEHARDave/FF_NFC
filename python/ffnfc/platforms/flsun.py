"""FLSUN T1 / T1 Pro: the "klipper" platform (one toolhead, or AFC lanes
with FLSUN_AFC) plus a guard for the T1's mainboard. Formerly FLSUN_NFC;
the Klipper section stays [flsun_nfc] (flsun/flsun_nfc.py).

The T1's mainboard is a CH340 USB serial chip (1a86:7523, no serial
number) on an internal USB port. Its port is never probed: the one at
MAINBOARD_PATH, and the one the mainboard's by-id name points to. If this
printer has no port at MAINBOARD_PATH, no CH340 port is probed unless
[flsun_nfc] names one with device:. A CH340 reader gets the same by-id name
as the mainboard; flsun/install.sh's udev rule (61-flsun-nfc-mcu.rules)
keeps that name on the mainboard."""

import os

from ..transport.serial_port import usb_serial_ports
from .klipper import OPTIONS, KlipperPlatform  # noqa: F401 (OPTIONS: same options)

CH340_VID = "1a86"
MAINBOARD_PATH = "/dev/serial/by-path/platform-xhci-hcd.0.auto-usb-0:1:1.0-port0"
MAINBOARD_ID = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"


class Ch340Excluded(list):
    """The configured excluded ports, plus the mainboard's port, worked out
    each time the reader search iterates it (readers are looked for again
    every retry_interval, after USB devices came and went)."""

    def __iter__(self):
        ports = list(list.__iter__(self))
        if os.path.exists(MAINBOARD_PATH):
            ports.append(MAINBOARD_PATH)
            if os.path.exists(MAINBOARD_ID):
                ports.append(MAINBOARD_ID)
        else:
            # Unknown USB layout: no CH340 is safe to probe.
            for path, vid, _pid, _product in usb_serial_ports():
                if vid == CH340_VID:
                    ports.append(path)
        return iter(ports)


class FlsunPlatform(KlipperPlatform):
    name = "flsun"

    def ready(self):
        KlipperPlatform.ready(self)
        if not self.core.device and not isinstance(self.core.excluded_ports, Ch340Excluded):
            self.core.excluded_ports = Ch340Excluded(self.core.excluded_ports)
