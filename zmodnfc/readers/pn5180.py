"""NXP PN5180 (ISO 14443A and ISO 15693) through a USB bridge.

The PN5180 only has SPI plus BUSY and RST lines, so on a printer it needs a
small USB bridge: a microcontroller (RP2040, ESP32-S3, ...) that appears as
a USB serial port and runs the PN5180 SPI transactions for the host. The
bridge protocol is documented in docs/pn5180-bridge.md; this module holds
the whole PN5180 command layer, so the bridge firmware stays a thin SPI
pipe.

Status: written from the PN5180 datasheet and not yet run against hardware;
the bridge firmware is not part of this release."""

import logging
import struct
import time

from ..tag import TECH_ISO14443A, TECH_ISO15693
from ..transport.serial_port import SerialError, SerialPort
from .base import CardSession, Reader, ReaderError

LOGGER = logging.getLogger("zmod_nfc")

# Host interface commands.
CMD_WRITE_REGISTER = 0x00
CMD_WRITE_REGISTER_OR_MASK = 0x01
CMD_WRITE_REGISTER_AND_MASK = 0x02
CMD_READ_REGISTER = 0x04
CMD_READ_EEPROM = 0x07
CMD_SEND_DATA = 0x09
CMD_READ_DATA = 0x0A
CMD_MIFARE_AUTHENTICATE = 0x0C
CMD_LOAD_RF_CONFIG = 0x11
CMD_RF_ON = 0x16
CMD_RF_OFF = 0x17

# Registers.
REG_SYSTEM_CONFIG = 0x00
REG_IRQ_STATUS = 0x02
REG_IRQ_CLEAR = 0x03
REG_CRC_RX_CONFIG = 0x12
REG_RX_STATUS = 0x13
REG_CRC_TX_CONFIG = 0x19
REG_RF_STATUS = 0x1D

IRQ_RX = 1 << 0
IRQ_GENERAL_ERROR = 1 << 17
IRQ_TX_RFON = 1 << 9

EEPROM_FIRMWARE_VERSION = 0x12

RF_CONFIG_ISO14443A_106 = (0x00, 0x80)
RF_CONFIG_ISO15693_26 = (0x0D, 0x8D)

REMOVAL_MISSES = 2


class Pn5180Error(IOError):
    """No or a bad answer from the card."""


class BridgeError(ReaderError):
    """The USB bridge failed; the reader is reopened."""


# --- transports ------------------------------------------------------------

def _crc8(data):
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


class SerialBridge(object):
    """PN5180 SPI over the zmod_nfc serial bridge protocol (v1)."""

    SYNC = 0xA5
    CMD_HELLO = 0x01
    CMD_RESET = 0x02
    CMD_EXCHANGE = 0x03
    MAGIC = b"PN5180BRIDGE"

    def __init__(self, path, baud=115200):
        self.port = SerialPort(path, baud)
        self.path = path
        self.version = None

    def open(self):
        try:
            self.port.open()
            reply = self._call(self.CMD_HELLO, b"", timeout=0.5)
        except (SerialError, BridgeError) as exc:
            self.port.close()
            raise BridgeError("%s: %s" % (self.path, exc))
        if not reply.startswith(self.MAGIC):
            self.port.close()
            raise BridgeError("%s: not a PN5180 bridge" % self.path)
        self.version = reply[len(self.MAGIC):].decode("ascii", "replace")

    def close(self):
        self.port.close()

    def reset(self):
        self._call(self.CMD_RESET, b"", timeout=1.0)

    def exchange(self, tx, rx_len=0, timeout=1.0):
        """Send one PN5180 command frame and, if rx_len, read the response
        frame; the bridge waits on BUSY around both."""
        return self._call(self.CMD_EXCHANGE, struct.pack("<H", rx_len) + bytes(tx), timeout)

    def _call(self, cmd, payload, timeout):
        body = bytes([cmd]) + struct.pack("<H", len(payload)) + payload
        try:
            self.port.flush_input()
            self.port.write(bytes([self.SYNC]) + body + bytes([_crc8(body)]))
            deadline = time.monotonic() + timeout
            while True:
                byte = self.port.read_exact(1, max(0., deadline - time.monotonic()))
                if not byte:
                    raise BridgeError("bridge did not answer")
                if byte[0] == self.SYNC:
                    break
            head = self.port.read_exact(3, timeout)
            if len(head) < 3:
                raise BridgeError("short bridge reply")
            length = struct.unpack_from("<H", head, 1)[0]
            rest = self.port.read_exact(length + 1, timeout)
        except SerialError as exc:
            raise BridgeError(str(exc))
        if len(rest) < length + 1 or _crc8(head + rest[:-1]) != rest[-1]:
            raise BridgeError("corrupt bridge reply")
        if head[0] != cmd | 0x80:
            raise BridgeError("bridge answered command 0x%02X" % head[0])
        data = rest[:-1]
        if not data or data[0] != 0:
            raise BridgeError("bridge error %d" % (data[0] if data else -1))
        return data[1:]


# --- PN5180 command layer --------------------------------------------------

class Pn5180(object):
    def __init__(self, transport):
        self.t = transport

    def write_register(self, reg, value):
        self.t.exchange(struct.pack("<BBI", CMD_WRITE_REGISTER, reg, value))

    def or_register(self, reg, mask):
        self.t.exchange(struct.pack("<BBI", CMD_WRITE_REGISTER_OR_MASK, reg, mask))

    def and_register(self, reg, mask):
        self.t.exchange(struct.pack("<BBI", CMD_WRITE_REGISTER_AND_MASK, reg, mask))

    def read_register(self, reg):
        return struct.unpack("<I", self.t.exchange(bytes([CMD_READ_REGISTER, reg]), 4))[0]

    def read_eeprom(self, addr, length):
        return self.t.exchange(bytes([CMD_READ_EEPROM, addr, length]), length)

    def load_rf_config(self, config):
        self.t.exchange(bytes([CMD_LOAD_RF_CONFIG, config[0], config[1]]))

    def rf_on(self):
        self.t.exchange(bytes([CMD_RF_ON, 0x00]))
        deadline = time.monotonic() + 0.1
        while not self.read_register(REG_IRQ_STATUS) & IRQ_TX_RFON:
            if time.monotonic() > deadline:
                break
        self.clear_irq()

    def rf_off(self):
        self.t.exchange(bytes([CMD_RF_OFF, 0x00]))
        self.clear_irq()

    def clear_irq(self):
        self.write_register(REG_IRQ_CLEAR, 0x000FFFFF)

    def crc(self, enabled):
        if enabled:
            self.or_register(REG_CRC_RX_CONFIG, 0x01)
            self.or_register(REG_CRC_TX_CONFIG, 0x01)
        else:
            self.and_register(REG_CRC_RX_CONFIG, 0xFFFFFFFE)
            self.and_register(REG_CRC_TX_CONFIG, 0xFFFFFFFE)

    def transceive(self, data, valid_bits=0, timeout=0.05):
        """Send a frame and return the card's answer, or raise Pn5180Error."""
        self.clear_irq()
        # Idle, then start the transceive state machine.
        self.and_register(REG_SYSTEM_CONFIG, 0xFFFFFFF8)
        self.or_register(REG_SYSTEM_CONFIG, 0x00000003)
        self.t.exchange(bytes([CMD_SEND_DATA, valid_bits]) + bytes(data))
        deadline = time.monotonic() + timeout
        while True:
            irq = self.read_register(REG_IRQ_STATUS)
            if irq & IRQ_RX:
                break
            if irq & IRQ_GENERAL_ERROR or time.monotonic() > deadline:
                raise Pn5180Error("no answer")
        status = self.read_register(REG_RX_STATUS)
        if status & (0x7 << 16):
            raise Pn5180Error("receive error 0x%08X" % status)
        length = status & 0x1FF
        if not length:
            raise Pn5180Error("empty answer")
        data = self.t.exchange(bytes([CMD_READ_DATA, 0x00]), length)
        self.clear_irq()
        return data

    def mifare_authenticate(self, key, key_b, block, uid4):
        resp = self.t.exchange(bytes([CMD_MIFARE_AUTHENTICATE]) + key
                               + bytes([0x61 if key_b else 0x60, block]) + uid4, 1,
                               timeout=1.0)
        return bool(resp) and resp[0] == 0


class Iso14443Session(CardSession):
    def __init__(self, chip, uid, atqa, sak):
        self.chip = chip
        self.tech = TECH_ISO14443A
        self.uid = uid
        self.atqa = atqa
        self.sak = sak

    def _try(self, data, timeout=0.05):
        try:
            return self.chip.transceive(data, timeout=timeout)
        except Pn5180Error:
            return None

    def type2_read(self, page):
        data = self._try(bytes([0x30, page]))
        return data if data is not None and len(data) >= 16 else None

    def mifare_auth(self, block, key, key_b=False):
        return self.chip.mifare_authenticate(key, key_b, block, self.uid[-4:])

    def mifare_read(self, block):
        return self.type2_read(block)

    def reselect(self):
        self.chip.rf_off()
        time.sleep(0.005)
        found = activate_iso14443a(self.chip)
        return found is not None and found[0] == self.uid

    def apdu(self, command):
        return None  # ISO-DEP framing is not implemented for the PN5180


class Iso15693Session(CardSession):
    def __init__(self, chip, uid_lsb):
        self.chip = chip
        self.tech = TECH_ISO15693
        self.uid_lsb = uid_lsb
        self.uid = uid_lsb[::-1]

    def iso15693_read(self, first_block, count):
        if count == 1:
            cmd = bytes([0x22, 0x20]) + self.uid_lsb + bytes([first_block])
        else:
            cmd = bytes([0x22, 0x23]) + self.uid_lsb + bytes([first_block, count - 1])
        try:
            resp = self.chip.transceive(cmd, timeout=0.05)
        except Pn5180Error:
            return None
        if not resp or resp[0] & 0x01:
            return None
        return resp[1:]

    def iso15693_system_info(self):
        try:
            resp = self.chip.transceive(bytes([0x22, 0x2B]) + self.uid_lsb, timeout=0.05)
        except Pn5180Error:
            return None
        if len(resp) < 10 or resp[0] & 0x01:
            return None
        flags = resp[1]
        pos = 10
        pos += 1 if flags & 0x01 else 0   # DSFID
        pos += 1 if flags & 0x02 else 0   # AFI
        if flags & 0x04 and len(resp) >= pos + 2:
            return resp[pos] + 1, (resp[pos + 1] & 0x1F) + 1
        return None


def activate_iso14443a(chip):
    """(uid, atqa, sak) of the card in the field, or None."""
    chip.load_rf_config(RF_CONFIG_ISO14443A_106)
    chip.rf_on()
    chip.and_register(REG_SYSTEM_CONFIG, 0xFFFFFFBF)   # MIFARE crypto off
    chip.crc(False)
    try:
        atqa = chip.transceive(b"\x26", valid_bits=7)   # REQA
    except Pn5180Error:
        return None
    uid = b""
    sak = 0
    for cascade in (0x93, 0x95, 0x97):
        chip.crc(False)
        try:
            answer = chip.transceive(bytes([cascade, 0x20]))
        except Pn5180Error:
            return None
        if len(answer) < 5:
            return None
        chip.crc(True)
        try:
            sak_frame = chip.transceive(bytes([cascade, 0x70]) + answer[:5])
        except Pn5180Error:
            return None
        sak = sak_frame[0]
        if answer[0] == 0x88:      # cascade tag: more UID bytes follow
            uid += answer[1:4]
        else:
            uid += answer[:4]
        if not sak & 0x04:
            break
    return uid, bytes(atqa[:2]), sak


def inventory_iso15693(chip):
    chip.load_rf_config(RF_CONFIG_ISO15693_26)
    chip.rf_on()
    try:
        resp = chip.transceive(b"\x26\x01\x00", timeout=0.05)
    except Pn5180Error:
        return None
    if len(resp) < 10 or resp[0] & 0x01:
        return None
    return bytes(resp[2:10])


class Pn5180Reader(Reader):
    def __init__(self, transport, keysets_for_uid=None):
        Reader.__init__(self, keysets_for_uid)
        self.transport = transport
        self.chip = Pn5180(transport)
        self.name = "PN5180"
        self._present = None
        self._misses = 0

    def open(self):
        self.transport.open()
        try:
            self.transport.reset()
            fw = self.chip.read_eeprom(EEPROM_FIRMWARE_VERSION, 2)
        except (BridgeError, Pn5180Error) as exc:
            self.transport.close()
            raise BridgeError("PN5180: %s" % exc)
        self.name = "PN5180 fw %d.%d via %s" % (fw[1], fw[0], getattr(self.transport, "path", "bridge"))

    def close(self):
        try:
            self.chip.rf_off()
        except Exception:
            pass
        self.transport.close()

    def poll(self):
        session = None
        found = activate_iso14443a(self.chip)
        if found is not None:
            session = Iso14443Session(self.chip, *found)
        else:
            self.chip.rf_off()
            uid_lsb = inventory_iso15693(self.chip)
            if uid_lsb is not None:
                session = Iso15693Session(self.chip, uid_lsb)
        if session is None:
            self.chip.rf_off()
            self._misses += 1
            if self._misses >= REMOVAL_MISSES:
                self._present = None
            return None
        self._misses = 0
        if session.uid == self._present:
            self.chip.rf_off()
            return None
        tag = self.read_card(session)
        self.chip.rf_off()
        if tag is not None:
            self._present = session.uid
        return tag

    def describe(self):
        return self.name


def probe(path, baud=115200):
    bridge = SerialBridge(path, baud)
    try:
        bridge.open()
        return True
    except ReaderError:
        return False
    finally:
        bridge.close()
