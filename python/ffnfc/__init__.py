"""ff_nfc: NFC/RFID filament tags for Zmod printers (Flashforge AD5X and
Creator 5).

Everything in this package uses only the Python standard library and stays
compatible with Python 3.8, the interpreter Klipper runs under on the AD5X.
Readers are reached through termios (USB-serial bridges) and raw usbfs
ioctls (CCID readers), so no pyserial, libusb, pcscd or pyscard is needed.
"""

__version__ = "0.1.0"
