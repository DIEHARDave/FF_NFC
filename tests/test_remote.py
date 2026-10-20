"""Network readers: request parsing, the Moonraker-published endpoints,
lane_data merging."""

import base64
import json
import time
import unittest

from helpers import mime_record, text_record, type2_tag
from test_plugin import CommandError, FakeGcmd, make_plugin

from zmodnfc import lane_data
from zmodnfc.remote import RemoteError, tag_from_json


def scan_request(tag, **extra):
    body = {
        "reader": "ESP32 PN532",
        "uid": tag.uid_hex,
        "kind": tag.kind,
        "sak": tag.sak,
        "atqa": tag.atqa.hex(),
        "memory": base64.b64encode(tag.memory).decode(),
    }
    body.update(extra)
    return body


class FakeWebRequest(object):
    """Klipper's WebRequest as the endpoint handlers use it."""

    def __init__(self, method, params):
        self.method = method
        self.params = params
        self.response = None

    def get_method(self):
        return self.method

    def get_int(self, item, default=None):
        value = self.params.get(item, default)
        if not isinstance(value, int):
            raise CommandError("Invalid Argument Type [%s]" % item)
        return value

    def send(self, data):
        self.response = data


class TagFromJsonTest(unittest.TestCase):
    def test_type2_round_trip(self):
        tag = type2_tag(text_record("PLA:FF0000"))
        parsed = tag_from_json(scan_request(tag))
        self.assertEqual(parsed.uid, tag.uid)
        self.assertEqual(parsed.memory, tag.memory)
        self.assertEqual(parsed.kind, "type2")
        self.assertEqual(parsed.reader, "ESP32 PN532")
        self.assertFalse(parsed.incomplete)

    def test_mifare_sector_keys(self):
        body = {"uid": "A1B2C3D4", "kind": "mifare_classic", "sak": 8,
                "memory": base64.b64encode(bytes(1024)).decode(),
                "sector_keys": {"1": "Bambu", "2": "default"}}
        tag = tag_from_json(body)
        self.assertEqual(tag.sector_keys, {1: "Bambu", 2: "default"})
        self.assertEqual(tag.sectors_read, {1, 2})

    def test_rejects_bad_requests(self):
        good = {"uid": "04112233445566", "kind": "type2", "memory": ""}
        for change in ({"kind": "floppy"}, {"uid": "zz"}, {"uid": "0102"},
                       {"memory": "not base64!"}, {"sak": 300},
                       {"kind": "mifare_classic", "memory": base64.b64encode(b"x" * 15).decode()},
                       {"kind": "mifare_classic", "sector_keys": {"50": "x"}}):
            with self.assertRaises(RemoteError, msg=change):
                tag_from_json(dict(good, **change))
        with self.assertRaises(RemoteError):
            tag_from_json([1, 2])


class EndpointTest(unittest.TestCase):
    def setUp(self):
        self.plugin, self.gcode, objects = make_plugin()
        self.endpoints = objects["webhooks"].endpoints

    def call(self, name, **params):
        request = FakeWebRequest("zmod_nfc/" + name, params)
        self.endpoints["zmod_nfc/" + name](request)
        return request.response

    def scan(self, tag, **extra):
        reply = self.call("scan", **scan_request(tag, **extra))
        if not reply["accepted"]:
            return reply
        deadline = time.time() + 5
        while (self.plugin.remote_result or {}).get("id") != reply["id"]:
            self.assertLess(time.time(), deadline, "the scan never finished")
            time.sleep(0.01)
        return self.plugin.remote_result

    def test_endpoints_are_registered(self):
        self.assertEqual(sorted(self.endpoints), ["zmod_nfc/assign", "zmod_nfc/cancel",
                                                  "zmod_nfc/info", "zmod_nfc/scan"])

    def test_scan_prompts_like_a_local_reader(self):
        result = self.scan(type2_tag(text_record("PETG:00FF00")))
        self.assertEqual(result["outcome"], "prompt")
        self.assertEqual(result["scan"]["material"], "PETG")
        self.assertEqual(self.plugin.get_status(0)["remote"], result)
        self.assertTrue(self.gcode.raw[0].startswith("// action:prompt_begin NFC: PETG"))
        reply = self.call("assign", slot=3)
        self.assertIn("Slot 3", reply["message"])
        self.assertEqual(self.gcode.scripts, ["CHANGE_ZCOLOR SLOT=3 HEX=00FF00 TYPE=PETG SILENT=1"])
        self.assertIn("// action:prompt_end", self.gcode.raw)
        with self.assertRaises(CommandError):
            self.call("assign", slot=3)

    def test_reader_fixed_to_a_slot_skips_the_prompt(self):
        result = self.scan(type2_tag(text_record("PETG:00FF00")), slot=2)
        self.assertEqual(result["outcome"], "assigned")
        self.assertEqual(result["slot"], 2)
        self.assertIn("Slot 2 is now PETG", result["message"])
        self.assertEqual(self.gcode.scripts, ["CHANGE_ZCOLOR SLOT=2 HEX=00FF00 TYPE=PETG SILENT=1"])
        self.assertFalse([r for r in self.gcode.raw if "prompt_begin" in r])
        self.assertIsNone(self.plugin.pending)
        # Zmod's colour popup after inserting into that slot is skipped.
        self.assertIn(2, self.plugin._assigned)

    def test_fixed_slot_errors(self):
        tag = type2_tag(text_record("PLA:FF0000"))
        self.assertEqual(self.scan(tag, slot=9)["outcome"], "no_slot")
        self.assertEqual(self.gcode.scripts, [])
        with self.assertRaises(CommandError):
            self.call("scan", **scan_request(tag, slot="two"))
        # slot 0 means "ask", as when the field is missing.
        self.assertEqual(self.scan(tag, slot=0)["outcome"], "prompt")

    def test_openspool_json_is_decoded_on_the_printer(self):
        payload = json.dumps({"protocol": "openspool", "version": "1.0", "type": "PLA",
                              "color_hex": "FFAA00", "brand": "Sunlu",
                              "min_temp": 200, "max_temp": 220}).encode()
        result = self.scan(type2_tag(mime_record("application/json", payload)))
        self.assertEqual(result["scan"]["format"], "OpenSpool")
        self.assertEqual(result["scan"]["filament"]["brand"], "Sunlu")

    def test_incomplete_disabled_and_bad_requests(self):
        tag = type2_tag(text_record("PLA:FF0000"))
        self.assertEqual(self.scan(tag, incomplete=True)["outcome"], "incomplete")
        self.gcode.commands["NFC_DISABLE"](FakeGcmd(self.gcode))
        self.assertEqual(self.scan(tag)["outcome"], "disabled")
        # Errors must reach Klipper as command errors; anything else shuts it down.
        with self.assertRaises(CommandError):
            self.call("scan", kind="type2", uid="xx")
        with self.assertRaises(CommandError):
            self.call("assign", slot="two")

    def test_info_and_cancel(self):
        info = self.call("info")
        self.assertEqual(info["plugin"], "zmod_nfc")
        self.assertEqual([s["label"] for s in info["slots"]], ["Slot 1", "Slot 2", "Slot 3", "Slot 4"])
        self.scan(type2_tag(text_record("ABS:112233")))
        self.assertIsNotNone(self.call("info")["pending"])
        self.call("cancel")
        self.assertIsNone(self.plugin.pending)


class LaneDataTest(unittest.TestCase):
    def test_entry_key_follows_lane_field(self):
        entries = {"T0": {"lane": "0"}, "T1": {"lane": "1"}}
        self.assertEqual(lane_data.entry_key(entries, 2), "T1")
        self.assertEqual(lane_data.entry_key(entries, 4), "lane4")
        self.assertEqual(lane_data.entry_key({}, 1), "lane1")

    def test_value_keeps_other_writers_keys(self):
        existing = {"lane": "1", "color": "#FFFFFF", "zmod_afclite": True,
                    "spool_id": 9, "nozzle_temp": 250}
        value = lane_data.lane_value(existing, 2, {
            "material": "PLA", "color": "ff0000", "vendor": "Bambu Lab", "name": "Bambu PLA",
            "nozzle_temp": 220, "uid": "04AA"})
        self.assertTrue(value["zmod_afclite"])
        self.assertEqual(value["color"], "#FF0000")
        self.assertEqual(value["nozzle_temp"], 220)
        self.assertNotIn("spool_id", value)          # the old spool's
        self.assertEqual(value["vendor_name"], "Bambu Lab")
        self.assertEqual(existing["spool_id"], 9)    # input untouched

    def test_new_spool_drops_old_spool_details(self):
        existing = {"lane": "1", "color": "#48358C", "spool_name": "Gold", "name": "Gold",
                    "color_name": "Medium Soft Green", "helix_material": "PLA",
                    "helix_spoolman_filament_id": 39, "spoolman_vendor_id": 2,
                    "zmod_afclite": True, "zmod_nfc_uid": "80FDF63E"}
        value = lane_data.lane_value(existing, 2, {
            "material": "PLA", "color": "6600CC", "spool_id": 19, "uid": "9065F63E",
            "name": "Snapmaker PLA Matte Violet Purple", "vendor": "Snapmaker"})
        for key in ("spool_name", "color_name", "helix_material", "helix_spoolman_filament_id",
                    "spoolman_vendor_id"):
            self.assertNotIn(key, value)
        self.assertEqual(value["name"], "Snapmaker PLA Matte Violet Purple")
        self.assertEqual(value["color"], "#6600CC")
        self.assertTrue(value["zmod_afclite"])

    def test_value_notes_zmod_colour_and_time(self):
        value = lane_data.lane_value({"lane": "0", "zmod_afclite_color": "#24E4A0"}, 1, {
            "material": "PLA", "color": "6600CC", "zmod_color": "48358c", "uid": "04AA",
            "scan_time": "2026-10-20T23:00:00Z"})
        self.assertEqual(value["color"], "#6600CC")
        self.assertEqual(value["zmod_afclite_color"], "#48358C")
        self.assertEqual(value["scan_time"], "2026-10-20T23:00:00Z")
        self.assertNotIn("zmod_color", value)

    def test_same_spool_keeps_other_writers_details(self):
        existing = {"lane": "0", "spool_id": 19, "spool_name": "Violet", "helix_material": "PLA"}
        value = lane_data.lane_value(existing, 1, {
            "material": "PLA", "color": "6600CC", "spool_id": 19, "uid": "9065F63E", "name": "x"})
        self.assertEqual(value["spool_name"], "Violet")
        self.assertEqual(value["helix_material"], "PLA")


if __name__ == "__main__":
    unittest.main()
