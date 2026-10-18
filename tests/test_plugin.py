"""The Klipper module against a minimal fake Klipper."""

import unittest

from helpers import text_record, type2_tag

from zmodnfc import klipper_plugin


class CommandError(Exception):
    pass


class ConfigError(Exception):
    pass


class FakeReactor(object):
    NOW = 0.
    NEVER = 9999999999.

    def __init__(self):
        self.time = 100.
        self.timers = []

    def monotonic(self):
        return self.time

    def register_timer(self, callback, waketime=None):
        self.timers.append(callback)
        return callback

    def update_timer(self, timer, waketime):
        pass

    def unregister_timer(self, timer):
        pass

    def register_async_callback(self, callback):
        callback(self.time)


class FakeGcode(object):
    def __init__(self):
        self.commands = {}
        self.raw = []
        self.info = []
        self.scripts = []
        self.in_command = []

    def register_command(self, name, fn, desc=None):
        self.commands[name] = fn

    def respond_raw(self, msg):
        self.raw.append(msg)

    def respond_info(self, msg):
        self.info.append(msg)

    def run_script(self, script):
        self.scripts.append(script)

    def run_script_from_command(self, script):
        self.in_command.append(script)


class FakeGcmd(object):
    def __init__(self, gcode, **params):
        self.gcode = gcode
        self.params = {k.upper(): str(v) for k, v in params.items()}

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, minval=None):
        return int(self.params.get(name, default))

    def respond_info(self, msg):
        self.gcode.info.append(msg)

    def error(self, msg):
        return CommandError(msg)


class FakeZmodColor(object):
    def __init__(self, tools=1, palette=None):
        self.status = {
            "total_tools": tools, "color_limit": 4, "channel": 1, "active_tool_id": 0,
            "valid_types": ["PLA", "PETG", "ABS", "TPU", "?"],
            "palette": palette or [], "filament_known": True,
            "slots": [{"ID": n, "HEX": "FFFFFF", "Material": "PLA", "hasFilament": n == 1}
                      for n in range(1, 5)],
        }

    def get_status(self, eventtime):
        return self.status


class FakePrinter(object):
    command_error = CommandError

    def __init__(self, objects):
        self.objects = objects
        self.reactor = FakeReactor()
        self.handlers = {}

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def register_event_handler(self, event, fn):
        self.handlers[event] = fn


class FakeWebhooks(object):
    def __init__(self):
        self._remote_methods = {}
        self.calls = []

    def call_remote_method(self, method, **kwargs):
        self.calls.append((method, kwargs))


class FakeConfig(object):
    error = ConfigError

    def __init__(self, printer, options):
        self.printer = printer
        self.options = options

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
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes")

    def getchoice(self, name, choices, default=None):
        return choices[self.options.get(name, default)]

    def get_prefix_sections(self, prefix):
        return []


def make_plugin(options=None, tools=1, palette=None, afclite=False):
    gcode = FakeGcode()
    objects = {"gcode": gcode, "webhooks": FakeWebhooks(),
               "zmod_color": FakeZmodColor(tools, palette)}
    if afclite:
        objects["zmod_afclite"] = object()
    printer = FakePrinter(objects)
    plugin = klipper_plugin.ZmodNfc(FakeConfig(printer, options or {}))
    plugin.zmod_color = objects["zmod_color"]
    return plugin, gcode, objects


class PluginTest(unittest.TestCase):
    def scan(self, plugin, text):
        scan = plugin._process(type2_tag(text_record(text)))
        plugin._handle_scan(scan)
        return scan

    def test_prompt_and_assign(self):
        plugin, gcode, _ = make_plugin()
        self.scan(plugin, "PETG:00FF00")
        self.assertTrue(gcode.raw[0].startswith("// action:prompt_begin NFC: PETG"))
        buttons = [r for r in gcode.raw if "prompt_button " in r]
        self.assertEqual(len(buttons), 4)
        self.assertIn("Slot 2: PLA|NFC_ASSIGN SLOT=2|primary", buttons[1])
        self.assertIn("Slot 1: PLA|NFC_ASSIGN SLOT=1|secondary", buttons[0])
        self.assertEqual(gcode.raw[-1], "// action:prompt_show")
        gcode.commands["NFC_ASSIGN"](FakeGcmd(gcode, SLOT=2))
        self.assertEqual(gcode.in_command, ["CHANGE_ZCOLOR SLOT=2 HEX=00FF00 TYPE=PETG SILENT=1"])
        self.assertIn("// action:prompt_end", gcode.raw)
        self.assertIsNone(plugin.pending)

    def test_assign_on_insert(self):
        plugin, gcode, objects = make_plugin()
        self.scan(plugin, "PLA:112233")
        objects["zmod_color"].status["slots"][2]["hasFilament"] = True
        plugin._tick(plugin.reactor.monotonic())
        self.assertEqual(gcode.scripts, ["CHANGE_ZCOLOR SLOT=3 HEX=112233 TYPE=PLA SILENT=1"])
        self.assertIsNone(plugin.pending)

    def test_prompt_timeout(self):
        plugin, gcode, _ = make_plugin({"prompt_timeout": 5})
        self.scan(plugin, "PLA:112233")
        plugin._tick(plugin.reactor.monotonic() + 10)
        self.assertIsNone(plugin.pending)
        self.assertIn("// action:prompt_end", gcode.raw)

    def test_afclite_handles_spoolman_spools(self):
        plugin, gcode, _ = make_plugin(afclite=True)
        scan = plugin._process(type2_tag(text_record("PLA:112233")))
        scan.spool = {"id": 5, "filament": {"material": "PLA", "color_hex": "112233"}, "extra": {}}
        plugin._handle_scan(scan)
        gcode.commands["NFC_ASSIGN"](FakeGcmd(gcode, SLOT=2))
        self.assertEqual(gcode.in_command, ["SET_SPOOL_ID LANE=E1 SPOOL_ID=5 CARD_UID=%s" % scan.uid])

    def test_spool_without_afclite(self):
        plugin, gcode, objects = make_plugin()
        objects["save_variables"] = object()
        plugin.bind_card_uid = False
        scan = plugin._process(type2_tag(text_record("PLA:112233")))
        scan.spool = {"id": 8, "filament": {"material": "PETG", "color_hex": "445566"},
                      "extra": {"card_uids": '"%s"' % scan.uid}}
        plugin._handle_scan(scan)
        gcode.commands["NFC_ASSIGN"](FakeGcmd(gcode, SLOT=1))
        self.assertEqual(gcode.in_command, [
            "CHANGE_ZCOLOR SLOT=1 HEX=445566 TYPE=PETG SILENT=1",
            "SAVE_VARIABLE VARIABLE=zmod_nfc_slot1_spool VALUE=8"])
        self.assertEqual(objects["webhooks"].calls, [("spoolman_set_active_spool", {"spool_id": 8})])

    def test_creator5_palette_and_labels(self):
        palette = ["FFFFFF", "000000", "FF0000", "00FF00"]
        plugin, gcode, _ = make_plugin(tools=4, palette=palette)
        self.scan(plugin, "PLA:EE1111")
        self.assertTrue(any("T3: PLA|NFC_ASSIGN SLOT=4" in r for r in gcode.raw))
        gcode.commands["NFC_ASSIGN"](FakeGcmd(gcode, SLOT=1))
        self.assertEqual(gcode.in_command, ["CHANGE_ZCOLOR SLOT=1 HEX=FF0000 TYPE=PLA SILENT=1"])

    def test_material_fallbacks(self):
        valid = ["PLA", "PETG", "ABS"]
        self.assertEqual(klipper_plugin.resolve_material("PETG-CF", valid), "PETG")
        self.assertEqual(klipper_plugin.resolve_material("ASA", valid), "ABS")
        self.assertEqual(klipper_plugin.resolve_material("PCTG", valid), "PETG")
        self.assertIsNone(klipper_plugin.resolve_material("PEEK", valid))

    def test_ignored_while_printing(self):
        plugin, gcode, objects = make_plugin()

        class Stats(object):
            def get_status(self, eventtime):
                return {"state": "printing"}

        objects["print_stats"] = Stats()
        self.scan(plugin, "PLA:112233")
        self.assertIsNone(plugin.pending)
        self.assertIn("ignored while printing", gcode.info[-1])

    def test_test_scan_command(self):
        plugin, gcode, _ = make_plugin()
        plugin._wait_background = lambda fn, *args: fn(*args)
        gcode.commands["NFC_TEST_SCAN"](FakeGcmd(gcode, TEXT="ABS:010203"))
        self.assertEqual(plugin.pending.material(), "ABS")
        gcode.commands["NFC_DUMP"](FakeGcmd(gcode, RAW=1))
        self.assertIn("NDEF text: ABS:010203", gcode.info[-1])

    def test_single_slot_test_scan_stays_in_command_context(self):
        plugin, gcode, objects = make_plugin()
        objects["zmod_color"].status["color_limit"] = 1
        plugin._wait_background = lambda fn, *args: fn(*args)
        gcode.commands["NFC_TEST_SCAN"](FakeGcmd(gcode, TEXT="PLA:112233"))
        # run_script would deadlock inside a G-code command.
        self.assertEqual(gcode.scripts, [])
        self.assertEqual(gcode.in_command, ["CHANGE_ZCOLOR SLOT=1 HEX=112233 TYPE=PLA SILENT=1"])

    def test_bad_vendor_key(self):
        with self.assertRaises(ConfigError):
            make_plugin({"bambu_key": "00" * 16})

    def test_no_filament_data(self):
        plugin, gcode, _ = make_plugin()
        plugin._handle_scan(plugin._process(type2_tag(b"")))
        self.assertIsNone(plugin.pending)
        self.assertIn("has no filament data", gcode.info[-1])


if __name__ == "__main__":
    unittest.main()
