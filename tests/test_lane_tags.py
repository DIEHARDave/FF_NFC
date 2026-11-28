"""The `lane_tags ff_nfc` object (lane-status-spec section 2.4) and
NFC_ASSIGN LANE=, on the Zmod fakes (test_plugin.py) and the Kiln ones."""

import unittest

from helpers import text_record, type2_tag

import test_kiln_plugin as kiln_tests
import test_plugin as zmod_tests
from ffnfc import __version__, lane_tags
from ffnfc.readers.base import Reader
from ffnfc.readers.ccid import CcidReader
from ffnfc.readers.pn532 import Pn532Reader
from ffnfc.readers.rc522 import Rc522Reader

KEYS = {"spec_version", "version", "tags", "assign", "cancel"}


def status(plugin):
    return plugin.printer.lookup_object("lane_tags ff_nfc").get_status(0.)


class LaneTagsTest(unittest.TestCase):
    def test_registered_with_every_key_from_the_start(self):
        plugin, _, _ = zmod_tests.make_plugin()
        st = status(plugin)
        self.assertEqual(set(st), KEYS)
        self.assertEqual(st["spec_version"], "0.1")
        self.assertEqual(st["version"], __version__)
        self.assertEqual((st["assign"], st["cancel"]), ("NFC_ASSIGN", "NFC_CANCEL"))
        self.assertEqual(st["tags"], {
            "readers": {"ff_nfc": {"type": None, "alive": False, "lanes": [], "port": None}},
            "lanes": {}, "pending": None})

    def test_reader_connected_and_lost(self):
        plugin, _, _ = zmod_tests.make_plugin()
        reader = Pn532Reader("/dev/ttyUSB0")
        plugin._reader_changed("connected", "PN532 on /dev/ttyUSB0", "",
                               lane_tags.reader_details(reader))
        self.assertEqual(status(plugin)["tags"]["readers"]["ff_nfc"],
                         {"type": "pn532", "alive": True, "lanes": [], "port": "/dev/ttyUSB0"})
        plugin._reader_changed("searching", "", "gone")
        # Still the same reader, just not answering.
        self.assertEqual(status(plugin)["tags"]["readers"]["ff_nfc"],
                         {"type": "pn532", "alive": False, "lanes": [], "port": "/dev/ttyUSB0"})

    def test_new_dicts_each_status(self):
        plugin, _, _ = zmod_tests.make_plugin()
        self.assertIsNot(status(plugin)["tags"], status(plugin)["tags"])

    def test_pending_tag_and_spool(self):
        plugin, gcode, _ = zmod_tests.make_plugin()
        scan = plugin._process(type2_tag(text_record("PETG:00FF00")))
        scan.spool = {"id": 12, "extra": {}, "filament": {"material": "PETG"}}
        plugin._handle_scan(scan)
        self.assertEqual(status(plugin)["tags"]["pending"],
                         {"uid": scan.uid, "spool": {"tracker": "spoolman", "id": "12"}})
        gcode.commands["NFC_CANCEL"](zmod_tests.FakeGcmd(gcode))
        self.assertIsNone(status(plugin)["tags"]["pending"])

    def test_pending_without_a_spool(self):
        plugin, _, _ = zmod_tests.make_plugin()
        scan = plugin._process(type2_tag(text_record("PETG:00FF00")))
        plugin._handle_scan(scan)
        self.assertEqual(status(plugin)["tags"]["pending"], {"uid": scan.uid, "spool": None})

    def test_reader_types_and_ports(self):
        self.assertEqual(lane_tags.reader_details(Pn532Reader("/dev/ttyACM0")),
                         {"type": "pn532", "port": "/dev/ttyACM0"})

        class Transport(object):
            path = "/dev/ttyUSB1"
        rc = Rc522Reader.__new__(Rc522Reader)
        rc.transport = Transport()
        self.assertEqual(lane_tags.reader_details(rc), {"type": "rc522", "port": "/dev/ttyUSB1"})
        rc.transport = object()
        self.assertEqual(lane_tags.reader_details(rc)["port"], "bridge")

        class Device(object):
            ident = "1-1.4"
        cc = CcidReader.__new__(CcidReader)
        cc.device = Device()
        self.assertEqual(lane_tags.reader_details(cc), {"type": "ccid", "port": "1-1.4"})
        self.assertEqual(lane_tags.reader_details(Reader()), {"type": None, "port": None})


class AssignLaneTest(unittest.TestCase):
    def _scan(self, plugin, text="PETG:00FF00"):
        plugin._handle_scan(plugin._process(type2_tag(text_record(text))))

    def test_zmod_lane_names(self):
        plugin, gcode, _ = zmod_tests.make_plugin()
        self._scan(plugin)
        gcode.commands["NFC_ASSIGN"](zmod_tests.FakeGcmd(gcode, LANE="lane2"))
        self.assertEqual(gcode.in_command, ["CHANGE_ZCOLOR SLOT=2 HEX=00FF00 TYPE=PETG SILENT=1"])
        self.assertIsNone(plugin.pending)

    def test_t_and_e_names_count_from_zero(self):
        for name in ("T2", "E2"):
            plugin, gcode, _ = zmod_tests.make_plugin()
            self._scan(plugin)
            gcode.commands["NFC_ASSIGN"](zmod_tests.FakeGcmd(gcode, LANE=name))
            self.assertEqual(gcode.in_command,
                             ["CHANGE_ZCOLOR SLOT=3 HEX=00FF00 TYPE=PETG SILENT=1"], name)

    def test_unknown_lane_keeps_the_scan(self):
        plugin, gcode, _ = zmod_tests.make_plugin()
        self._scan(plugin)
        for name in ("lane9", "Lane_X", ""):
            with self.assertRaises(zmod_tests.CommandError):
                gcode.commands["NFC_ASSIGN"](zmod_tests.FakeGcmd(gcode, LANE=name))
        self.assertIsNotNone(plugin.pending)
        self.assertEqual(gcode.in_command, [])

    def test_kiln_without_afc(self):
        plugin, gcode, _ = kiln_tests.make_plugin()
        self._scan(plugin)
        gcode.commands["NFC_ASSIGN"](kiln_tests.FakeGcmd(gcode, LANE="lane4"))
        self.assertEqual(gcode.in_command, ["IFS_SET_MATERIAL SLOT=4 TYPE=PETG COLOR=00FF00"])

    def test_kiln_afc_lane_names(self):
        plugin, gcode, objects = kiln_tests.make_plugin()

        class Lane(object):
            def __init__(self, name):
                self.name = name
                self.spool_id = None
        # AFC's lane names need not follow the slot numbers.
        by_slot = {1: Lane("left"), 2: Lane("lane1"), 3: Lane("c"), 4: Lane("d")}

        class Unit(object):
            def lane_for_slot(self, slot):
                return by_slot.get(slot)

        class Afc(object):
            units = {"IFS": Unit()}
            lanes = {lane.name: lane for lane in by_slot.values()}
        objects["AFC"] = Afc()
        self._scan(plugin)
        gcode.commands["NFC_ASSIGN"](kiln_tests.FakeGcmd(gcode, LANE="lane1"))
        self.assertEqual(gcode.in_command, ["IFS_SET_MATERIAL SLOT=2 TYPE=PETG COLOR=00FF00"])


if __name__ == "__main__":
    unittest.main()
