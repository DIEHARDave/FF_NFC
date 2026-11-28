"""The "klipper" platform (any Klipper printer: one toolhead, or AFC lanes),
against a minimal fake Klipper. Ported from FLSUN_NFC's T1 tests."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))

from ffnfc import klipper_plugin  # noqa: E402
from ffnfc.tag import Tag  # noqa: E402
from ffnfc.platforms import klipper as platform  # noqa: E402


class CommandError(Exception):
    pass


class FakeReactor(object):
    NEVER = 9999999999.

    def monotonic(self):
        return 100.

    def register_timer(self, callback, waketime=None):
        return callback

    def update_timer(self, timer, waketime):
        pass

    def register_async_callback(self, callback):
        callback(100.)


class FakeGcode(object):
    def __init__(self):
        self.commands, self.raw, self.info, self.scripts = {}, [], [], []
        self.ready_gcode_handlers = self.commands

    def register_command(self, name, fn, desc=None):
        self.commands[name] = fn

    def respond_raw(self, msg):
        self.raw.append(msg)

    def respond_info(self, msg):
        self.info.append(msg)

    def run_script(self, script):
        self.scripts.append(script)

    run_script_from_command = run_script


class FakeWebhooks(object):
    def __init__(self):
        self.calls = []

    def register_endpoint(self, path, callback):
        pass

    def call_remote_method(self, method, **kwargs):
        self.calls.append((method, kwargs))


class FakeSensor(object):
    class runout_helper(object):
        filament_present = True


class FakePrinter(object):
    command_error = CommandError

    def __init__(self, objects):
        self.objects = objects
        self.reactor = FakeReactor()

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def register_event_handler(self, event, fn):
        pass

    def add_object(self, name, obj):
        self.objects[name] = obj


class FakeConfig(object):
    error = ValueError

    def __init__(self, printer, options):
        self.printer, self.options = printer, options

    def get_printer(self):
        return self.printer

    def get(self, name, default=None):
        return self.options.get(name, default)

    def getint(self, name, default=None, **kw):
        return int(self.options.get(name, default))

    def getfloat(self, name, default=None, **kw):
        return float(self.options.get(name, default))

    def getboolean(self, name, default=None):
        value = self.options.get(name, default)
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true")

    def getchoice(self, name, choices, default=None):
        return choices[self.options.get(name, default)]

    def get_prefix_sections(self, prefix):
        return []


def text_tag(text, uid=b"\x04\x11\x22\x33\x44\x55\x66"):
    """An NTAG215 with one NDEF text record."""
    payload = b"\x02en" + text.encode()
    record = bytes([0xD1, 1, len(payload)]) + b"T" + payload
    memory = bytearray(135 * 4)
    memory[0:7] = uid[:3] + b"\x88" + uid[3:6]
    memory[12:16] = bytes([0xE1, 0x10, 131 * 4 // 8, 0x00])
    data = bytes([0x03, len(record)]) + record + b"\xfe"
    memory[16:16 + len(data)] = data
    return Tag(uid, "iso14443a", "type2", bytes(memory), sak=0x00)


def make_plugin(options=None):
    gcode, webhooks = FakeGcode(), FakeWebhooks()
    objects = {"gcode": gcode, "webhooks": webhooks,
               "filament_switch_sensor filament_sensor": FakeSensor()}
    printer = FakePrinter(objects)
    options = dict({"lane_data": False, "spoolman": False}, **(options or {}))
    plugin = klipper_plugin.load_config(FakeConfig(printer, options), "klipper")
    plugin.platform.ready()
    return plugin, gcode, webhooks


class KlipperPlatformTest(unittest.TestCase):
    def test_scan_assigns_without_prompt(self):
        plugin, gcode, webhooks = make_plugin()
        written = []
        plugin._write_lane_data = lambda slot, info: written.append((slot, info))
        scan = plugin._process(text_tag("PETG:00FF00"))
        self.assertEqual(plugin._handle_scan(scan), "assigned")
        self.assertFalse(any("prompt_begin" in r for r in gcode.raw))
        self.assertEqual(written[0][0], 1)
        self.assertEqual((written[0][1]["material"], written[0][1]["color"]), ("PETG", "00FF00"))
        # Not a Spoolman spool: the active spool is cleared.
        self.assertEqual(webhooks.calls, [("spoolman_set_active_spool", {"spool_id": None})])
        self.assertIn("The toolhead now has PETG #00FF00", gcode.info[-1])
        self.assertTrue(plugin._slots()["slots"][1]["loaded"])

    def test_keep_active_spool(self):
        plugin, gcode, webhooks = make_plugin({"clear_active_spool": False})
        plugin._handle_scan(plugin._process(text_tag("PLA:112233")))
        self.assertEqual(webhooks.calls, [])

    def test_spoolman_spool_becomes_active(self):
        plugin, gcode, webhooks = make_plugin()
        bound = []

        class FakeSpoolman(object):
            def bind_uid(self, spool, uid, fmt=None):
                bound.append((spool["id"], uid))

        plugin.spoolman = FakeSpoolman()
        plugin._in_background = lambda fn, *args: fn(*args)
        scan = plugin._process(text_tag("PLA:112233"))
        scan.spool = {"id": 20, "filament": {"material": "PLA", "color_hex": "112233"},
                      "extra": {}, "tags": []}
        plugin._handle_scan(scan)
        self.assertEqual(webhooks.calls, [("spoolman_set_active_spool", {"spool_id": 20})])
        self.assertEqual(bound, [(20, scan.uid)])
        self.assertIn("Spoolman spool 20", gcode.info[-1])

    # --- With AFC ------------------------------------------------------------

    def make_afc_plugin(self, current=None, weight_check=True):
        plugin, gcode, webhooks = make_plugin()

        class Lane(object):
            def __init__(self, name, prep, spool_id=None):
                self.name, self.prep_state, self.spool_id = name, prep, spool_id
                self.material, self.color = "PLA", "#ff0000"

        class Function(object):
            def get_current_lane(self):
                return current

        class Afc(object):
            pass
        afc = Afc()
        afc.lanes = {"lane1": Lane("lane1", True), "lane2": Lane("lane2", False, 20),
                     "lane3": Lane("lane3", False)}
        afc.function = Function()
        afc.disable_weight_check = not weight_check
        plugin.printer.objects["AFC"] = afc
        written = []
        plugin._write_lane_data = lambda slot, info: written.append((slot, info))
        return plugin, gcode, webhooks, afc, written

    def spool_scan(self, plugin, spool_id=20, weight=500):
        scan = plugin._process(text_tag("PLA:112233"))
        scan.spool = {"id": spool_id, "remaining_weight": weight, "extra": {}, "tags": [],
                      "filament": {"material": "PLA", "color_hex": "112233"}}
        return scan

    def test_afc_lanes_are_the_slots(self):
        plugin, gcode, _, _, _ = self.make_afc_plugin(current="lane2")
        slots = plugin._slots()
        self.assertEqual(sorted(slots["slots"]), [1, 2, 3])
        self.assertEqual(slots["current"], 2)
        self.assertTrue(slots["presence"])
        self.assertEqual(slots["slots"][1], {"material": "PLA", "color": "FF0000", "loaded": True})
        self.assertEqual(plugin.platform.slot_label(slots, 3), "Lane lane3")

    def test_afc_scan_asks_for_a_lane(self):
        plugin, gcode, webhooks, _, written = self.make_afc_plugin()
        self.assertEqual(plugin._handle_scan(self.spool_scan(plugin)), "prompt")
        self.assertTrue(any("NFC_ASSIGN SLOT=3" in r for r in gcode.raw))
        self.assertEqual((webhooks.calls, written), ([], []))

    def test_afc_spool_goes_to_the_lane(self):
        plugin, gcode, webhooks, _, written = self.make_afc_plugin()
        plugin.pending = self.spool_scan(plugin)
        plugin._assign(plugin.pending, 3, gcode.respond_info, gcode.run_script)
        # Spool 20 was on lane2: AFC refuses it on two lanes, so it leaves there.
        self.assertEqual(gcode.scripts, ["SET_SPOOL_ID LANE=lane2 SPOOL_ID=",
                                         "SET_SPOOL_ID LANE=lane3 SPOOL_ID=20"])
        # AFC sets the active spool and lane_data, not lanetag-nfc.
        self.assertEqual((webhooks.calls, written), ([], []))
        self.assertIn("Lane lane3 is now", gcode.info[-1])

    def test_afc_refused_spool_is_reported(self):
        plugin, gcode, _, afc, _ = self.make_afc_plugin()
        plugin._assign(self.spool_scan(plugin, spool_id=33), 1, gcode.respond_info,
                       gcode.run_script)
        plugin.platform._check_spools(200.)
        self.assertIn("AFC did not take Spoolman spool 33", gcode.info[-1])
        afc.lanes["lane1"].spool_id = 33
        plugin.platform._check_later("lane1", 33, "Lane lane1")
        gcode.info.clear()
        plugin.platform._check_spools(200.)
        self.assertEqual(gcode.info, [])

    def test_afc_empty_spool_is_left_out(self):
        plugin, gcode, _, _, _ = self.make_afc_plugin()
        plugin._assign(self.spool_scan(plugin, weight=0), 1, gcode.respond_info, gcode.run_script)
        self.assertEqual(gcode.scripts, [])
        self.assertIn("no filament left", gcode.info[-1])

    def test_afc_tag_without_spool_sets_material_and_color(self):
        plugin, gcode, webhooks, _, written = self.make_afc_plugin()
        plugin._assign(plugin._process(text_tag("PETG:00FF00")), 2, gcode.respond_info,
                       gcode.run_script)
        self.assertEqual(gcode.scripts, ["SET_SPOOL_ID LANE=lane2 SPOOL_ID=",
                                         "SET_MATERIAL LANE=lane2 MATERIAL=PETG",
                                         "SET_COLOR LANE=lane2 COLOR=00FF00"])
        self.assertEqual((webhooks.calls, written), ([], []))

    def test_afc_material_is_one_word(self):
        self.assertEqual(platform.afc_material("PLA SILK"), "PLA-SILK")
        self.assertIsNone(platform.afc_material("  "))

    def test_afc_lane_names_for_assign(self):
        plugin, gcode, _, _, _ = self.make_afc_plugin()
        plugin.pending = self.spool_scan(plugin, spool_id=40)
        gcode.commands["NFC_ASSIGN"](FakeGcmd(LANE="lane3"))
        self.assertEqual(gcode.scripts, ["SET_SPOOL_ID LANE=lane3 SPOOL_ID=40"])
        self.assertEqual(plugin.platform.lane_slot("nope"), None)

    def test_lane_data_producer(self):
        plugin, _, _ = make_plugin()
        self.assertIsNone(plugin.lane_data_producer())
        plugin, _, _, _, _ = self.make_afc_plugin()
        self.assertEqual(plugin.lane_data_producer(), "AFC")

    def test_lane_tags_registered(self):
        plugin, _, _ = make_plugin()
        status = plugin.printer.lookup_object("lane_tags ff_nfc").get_status(0.)
        self.assertEqual(status["tags"]["pending"], None)


class FakeGcmd(object):
    def __init__(self, **params):
        self.params = dict((k, str(v)) for k, v in params.items())

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, minval=None):
        return int(self.params.get(name, default))

    def respond_info(self, msg):
        pass

    def error(self, msg):
        return CommandError(msg)


if __name__ == "__main__":
    unittest.main()
