import ctypes
import unittest

import helpers  # noqa: F401  (sets sys.path)

from zmodnfc.readers import base, ccid, pn532
from zmodnfc.readers.base import KeySet
from zmodnfc.tag import KIND_MIFARE_CLASSIC, KIND_TYPE2
from zmodnfc.transport import usbfs


class FakeCard(base.CardSession):
    """A tag in a fake reader: memory plus per-sector keys."""

    def __init__(self, memory, sak=0x00, keys=None):
        self.uid = b"\x01\x02\x03\x04"
        self.sak = sak
        self.memory = memory
        self.keys = keys or {}
        self.authed = None
        self.auth_attempts = 0

    def type2_read(self, page):
        if page * 4 >= len(self.memory):
            return None
        return (self.memory + self.memory)[page * 4:page * 4 + 16]

    def mifare_auth(self, block, key, key_b=False):
        self.auth_attempts += 1
        sector = block // 4
        self.authed = sector if self.keys.get(sector) == key else None
        return self.authed is not None

    def mifare_read(self, block):
        if self.authed != block // 4:
            return None
        return self.memory[block * 16:block * 16 + 16]

    def reselect(self):
        return True


class ReadTest(unittest.TestCase):
    def test_type2_stops_at_cc_size(self):
        memory = bytearray(180)
        memory[12:16] = b"\xe1\x10\x12\x00"    # NTAG213: 144 data bytes
        tag = base.read_type2(FakeCard(bytes(memory)))
        self.assertEqual((tag.kind, len(tag.memory)), (KIND_TYPE2, 160))

    def test_mifare_vendor_keys_then_defaults(self):
        vendor = bytes.fromhex("A1A2A3A4A5A6")
        keys = {s: b"\xff" * 6 for s in range(16)}
        keys[1] = vendor
        memory = bytes(range(256)) * 4
        card = FakeCard(memory, sak=0x08, keys=keys)
        keysets = [KeySet("Other", [b"\x99" * 6] * 16), KeySet("Vendor", [None, vendor] + [None] * 14)]
        tag = base.read_mifare_classic(card, keysets)
        self.assertEqual(tag.kind, KIND_MIFARE_CLASSIC)
        self.assertEqual(tag.sectors_read, set(range(16)))
        self.assertEqual(tag.sector_keys[1], "Vendor")
        self.assertEqual(tag.sector_keys[0], "default")
        self.assertEqual(tag.memory, memory)

    def test_mifare_unreadable_sectors_are_zero(self):
        keys = {0: b"\xff" * 6}
        card = FakeCard(b"\x55" * 1024, sak=0x08, keys=keys)
        tag = base.read_mifare_classic(card, [])
        self.assertEqual(tag.sectors_read, {0})
        self.assertEqual(tag.memory[64:80], b"\x00" * 16)


class IoctlTest(unittest.TestCase):
    def test_generic_layout(self):
        # x86/ARM: dir bits at 30, READ|WRITE = 3.
        size = ctypes.sizeof(usbfs._BulkTransfer)
        expected = (3 << 30) | (size << 16) | (ord("U") << 8) | 2
        if usbfs._IOC_SIZEBITS == 14:
            self.assertEqual(usbfs.USBDEVFS_BULK, expected)
            self.assertEqual(usbfs.USBDEVFS_CLAIMINTERFACE, 0x8004550F)

    def test_mips_layout(self):
        # MIPS: dir bits at 29, READ=2 WRITE=4; CLAIMINTERFACE is _IOR.
        value = (2 << 29) | (4 << 16) | (ord("U") << 8) | 15
        self.assertEqual(value, 0x4004550F)


class CcidTest(unittest.TestCase):
    def test_classify(self):
        def atr(standard, name):
            return bytes.fromhex("3B8F8001804F0CA000000306%02X%04X00000000" % (standard, name))
        self.assertEqual(ccid.classify_atr(atr(0x03, 0x0001)), ("iso14443a", 0x08))
        self.assertEqual(ccid.classify_atr(atr(0x03, 0x0003)), ("iso14443a", 0x00))
        self.assertEqual(ccid.classify_atr(atr(0x0B, 0x0000))[0], "iso15693")
        self.assertEqual(ccid.classify_atr(bytes.fromhex("3B8180018080")), ("iso14443a", 0x20))
        self.assertEqual(ccid.classify_atr(bytes.fromhex("3B9F958131FE9F006576")), (None, None))


class FakeAcr122uSlot(object):
    """An ACR122U with an NTAG216: READ BINARY fails from page 44 on, the
    PN532 passthrough works everywhere."""

    def __init__(self, memory):
        self.memory = memory

    def apdu(self, cmd):
        ok = b"\x90\x00"
        if cmd[:2] == b"\xff\xca":
            return b"\x53\xa1\xe8\xbf\x52\x00\x01" + ok
        if cmd[:2] == b"\xff\xb0":
            page, length = cmd[3], cmd[4]
            if page >= 44:
                return b"\x6a\x82"
            return self.memory[page * 4:page * 4 + length] + ok
        if cmd[:8] == b"\xff\x00\x00\x00\x04\xd4\x42\x30":
            page = cmd[8]
            return b"\xd5\x43\x00" + self.memory[page * 4:page * 4 + 16] + ok
        return b"\x6d\x00"


class Acr122uTest(unittest.TestCase):
    def test_ntag216_beyond_page_44(self):
        from helpers import mime_record, type2_tag
        from zmodnfc import decoders
        payload = (b'{"protocol":"openspool","version":"1.0","brand":"SUNLU","type":"PLA",'
                   b'"color_hex":"0022FF","min_temp":"190","max_temp":"230",'
                   b'"bed_min_temp":"50","bed_max_temp":"60"}')
        image = type2_tag(mime_record("application/json", payload), pages=231).memory
        atr = bytes.fromhex("3B8F8001804F0CA000000306030003000000006A")
        session = ccid.CcidSession(FakeAcr122uSlot(image), atr)
        tag = base.read_type2(session, "fake")
        filament, _ = decoders.decode(tag)
        self.assertEqual((filament.fmt, filament.brand, filament.material, filament.color),
                         ("OpenSpool", "SUNLU", "PLA", "0022FF"))
        # Stops after the NDEF message instead of reading all 231 pages.
        self.assertLess(len(tag.memory), 300)


class FakeAcr122uMifare(object):
    """ACR122U with a MIFARE Classic card: PC/SC auth always fails (like the
    real reader once the card is halted), the PN532 passthrough works."""

    def __init__(self, uid, memory, keys_a):
        self.uid = uid
        self.memory = memory
        self.keys_a = keys_a          # sector -> key
        self.authed = None
        self.halted = False

    def apdu(self, cmd):
        ok = b"\x90\x00"
        if cmd[:2] == b"\xff\xca":
            return self.uid + ok
        if cmd[:2] in (b"\xff\x82", b"\xff\x86"):
            return b"\x63\x00"
        if cmd[:5] == b"\xff\x00\x00\x00" + bytes([len(cmd) - 5]) and cmd[5] == 0xD4:
            pn = cmd[6]
            body = cmd[7:]
            if pn == 0x02:
                return b"\xd5\x03\x32\x01\x06\x07" + ok
            if pn == 0x4A:
                self.halted = False
                self.authed = None
                return b"\xd5\x4b\x01\x01\x00\x04\x08\x04" + self.uid + ok
            if pn == 0x40 and not self.halted:
                op, block = body[1], body[2]
                if op in (0x60, 0x61):
                    sector = block // 4
                    if body[3:9] == self.keys_a.get(sector) and body[9:13] == self.uid:
                        self.authed = sector
                        return b"\xd5\x41\x00" + ok
                    self.halted = True
                    return b"\xd5\x41\x14" + ok
                if op == 0x30 and self.authed == block // 4:
                    return b"\xd5\x41\x00" + self.memory[block * 16:block * 16 + 16] + ok
            return b"\xd5\x41\x01" + ok
        return b"\x6d\x00"


class Acr122uMifareTest(unittest.TestCase):
    def test_snapmaker_tag_through_pn532(self):
        from zmodnfc import decoders
        from zmodnfc.decoders import vendors
        uid = bytes.fromhex("012772F0")
        keyset = vendors.snapmaker_keysets(uid, {})[0]
        memory = bytearray(1024)
        memory[16:25] = b"Snapmaker"
        memory[66] = 1              # PLA
        memory[68] = 4              # Silk
        memory[72] = 1              # one colour
        memory[80:83] = b"\x12\x34\x56"
        memory[128:130] = (175).to_bytes(2, "little")
        memory[130:132] = (500).to_bytes(2, "little")
        memory[160:168] = b"20260312"
        slot = FakeAcr122uMifare(uid, bytes(memory), {s: keyset.keys_a[s] for s in range(16)})
        atr = bytes.fromhex("3B8F8001804F0CA000000306030001000000006A")
        session = ccid.CcidSession(slot, atr)
        tag = base.read_mifare_classic(session, decoders.mifare_keysets(uid, {}))
        self.assertEqual(tag.sectors_read, set(range(16)))
        filament, _ = decoders.decode(tag)
        self.assertEqual((filament.fmt, filament.material, filament.modifiers, filament.color,
                          filament.weight, filament.diameter),
                         ("Snapmaker", "PLA", ["Silk"], "123456", 500, 1.75))
        self.assertEqual(filament.manufactured, "2026-03-12")


class FakePort(object):
    def __init__(self, replies):
        self.replies = list(replies)
        self.written = []
        self.buffer = b""

    def write(self, data, timeout=1.0):
        self.written.append(bytes(data))
        if self.replies and not data.startswith(b"\x55"):
            self.buffer += self.replies.pop(0)

    def read(self, size, timeout):
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def flush_input(self):
        pass


def pn532_frame(body):
    body = b"\xd5" + body
    return (b"\x00\x00\xff" + bytes([len(body), (-len(body)) & 0xFF]) + body
            + bytes([(-sum(body)) & 0xFF, 0x00]))


class Pn532Test(unittest.TestCase):
    def test_command_round_trip(self):
        port = FakePort([pn532._ACK + pn532_frame(b"\x03\x32\x01\x06\x07")])
        chip = pn532.Pn532(port)
        self.assertEqual(chip.command(pn532.CMD_GET_FIRMWARE_VERSION), b"\x32\x01\x06\x07")
        frame = port.written[-1]
        self.assertEqual(frame[:5], b"\x00\x00\xff\x02\xfe")
        self.assertEqual(frame[5:7], b"\xd4\x02")

    def test_garbage_before_frame(self):
        port = FakePort([b"\x00\xff\x12" + pn532._ACK + b"\x00" + pn532_frame(b"\x4b\x00")])
        self.assertEqual(pn532.Pn532(port).command(pn532.CMD_IN_LIST_PASSIVE_TARGET, b"\x01\x00"),
                         b"\x00")

    def test_no_ack(self):
        port = FakePort([b""])
        with self.assertRaises(pn532.Pn532Error):
            pn532.Pn532(port).command(pn532.CMD_GET_FIRMWARE_VERSION, timeout=0.05)


if __name__ == "__main__":
    unittest.main()
