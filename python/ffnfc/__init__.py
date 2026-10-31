"""FF_NFC: NFC/RFID filament tags for Flashforge printers on Zmod (AD5X,
Creator 5 Pro) and Kiln OS (AD5X).

Everything in this package uses only the Python standard library and stays
compatible with Python 3.8, the interpreter Klipper runs under on Zmod
(Kiln OS runs 3.14). Readers are reached through termios (USB-serial
bridges) and raw usbfs ioctls (CCID readers, and serial chips without a
kernel driver), so no pyserial, libusb, pcscd or pyscard is needed.
Firmware specifics live in platforms/.
"""

__version__ = "0.2.1"
