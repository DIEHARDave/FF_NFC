"""The Klipper module against a minimal fake Klipper."""

import json
import threading
import unittest

from helpers import text_record, type2_tag

from ffnfc import klipper_plugin


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

    @property
    def ready_gcode_handlers(self):
        return self.commands

    def register_command(self, name, fn, desc=None):
        if fn is None:
            return self.commands.pop(name, None)
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

    def get_float(self, name, default=None, minval=None, maxval=None):
        return float(self.params.get(name, default))

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
        self.endpoints = {}

    def register_endpoint(self, path, callback):
        self.endpoints[path] = callback

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
    # No lane_data writes to a Moonraker that is not there.
    options = dict({"lane_data": False}, **(options or {}))
    plugin = klipper_plugin.FfNfc(FakeConfig(printer, options))
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
            "SAVE_VARIABLE VARIABLE=ff_nfc_slot1_spool VALUE=8"])
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

    def test_foreign_spool_id_falls_back_to_uid(self):
        plugin, gcode, objects = make_plugin()
        objects["webhooks"]._remote_methods["spoolman_set_active_spool"] = True
        looked_up = []

        class FakeSpoolman(object):
            def get_spool(self, spool_id):
                looked_up.append(spool_id)
                raise klipper_plugin.SpoolmanError("no spool")

            def find_spool_by_uid(self, uid):
                return {"id": 12, "filament": {"material": "PLA"}, "extra": {}}

        plugin.spoolman = FakeSpoolman()
        payload = b'{"protocol":"openspool","type":"PLA","color_hex":"0022FF","spool_id":1776053727949617}'
        from helpers import mime_record
        scan = plugin._process(type2_tag(mime_record("application/json", payload)))
        self.assertEqual(looked_up, [])          # implausible ID not even tried
        self.assertEqual(scan.spool["id"], 12)

    def _spoolman_by_filament(self, spools):
        plugin, gcode, objects = make_plugin()
        objects["webhooks"]._remote_methods["spoolman_set_active_spool"] = True

        class FakeSpoolman(object):
            def find_spool_by_uid(self, uid):
                return None

            def request(self, method, path, query=None, body=None):
                return spools

        fake = FakeSpoolman()
        from ffnfc.spoolman import Spoolman
        fake.find_spools_by_filament = lambda f: Spoolman.find_spools_by_filament(fake, f)
        plugin.spoolman = fake
        payload = b'{"protocol":"openspool","brand":"Snapmaker","type":"PLA","color_hex":"6600CC"}'
        from helpers import mime_record
        return plugin._process(type2_tag(mime_record("application/json", payload)))

    def test_unbound_tag_matches_spool_by_filament(self):
        violet = {"id": 19, "extra": {}, "filament": {
            "vendor": {"name": "Snapmaker"}, "material": "PLA", "color_hex": "6600cc"}}
        red = {"id": 6, "extra": {}, "filament": {
            "vendor": {"name": "Snapmaker"}, "material": "PLA", "color_hex": "E36868"}}
        scan = self._spoolman_by_filament([red, violet])
        self.assertEqual(scan.spool["id"], 19)

    def test_several_filament_matches_are_not_guessed(self):
        spool = lambda n: {"id": n, "extra": {}, "filament": {
            "vendor": {"name": "Snapmaker"}, "material": "PLA", "color_hex": "6600CC"}}
        scan = self._spoolman_by_filament([spool(19), spool(30)])
        self.assertIsNone(scan.spool)
        self.assertIn("2 spools match", scan.spoolman_note)

    def test_native_spoolman_tag_matches(self):
        from ffnfc.spoolman import spool_uids
        spool = {"id": 76, "extra": {}, "tags": [{"uid": "531c89bc520001", "added": "2026-10-27T20:45:35Z"}]}
        self.assertEqual(spool_uids(spool), ["531C89BC520001"])

    def test_bind_links_native_tag_and_card_uids(self):
        from ffnfc.spoolman import Spoolman, SpoolmanError
        calls = []

        class Fake(Spoolman):
            def __init__(self, refuse_link=False):
                self.refuse_link = refuse_link

            def request(self, method, path, query=None, body=None):
                calls.append((method, path, body))
                if path.endswith("/tag") and self.refuse_link:
                    raise SpoolmanError("Spoolman: tag already linked to spool 3")
                if path == "/v1/field/spool":
                    return [{"key": "card_uids"}]
                if method == "GET":
                    return []
                return {}

        Fake().bind_uid({"id": 76, "extra": {}, "tags": []}, "53:1c:89:bc:52:00:01")
        self.assertIn(("POST", "/v1/spool/76/tag", {"uid": "531C89BC520001"}), calls)
        self.assertIn(("PATCH", "/v1/spool/76", {"extra": {"card_uids": '"531C89BC520001"'}}), calls)
        # Spoolman before 0.27: no tag list, no link attempt.
        del calls[:]
        Fake().bind_uid({"id": 5, "extra": {}}, "04AA")
        self.assertFalse([c for c in calls if c[1].endswith("/tag")])
        # A tag held elsewhere: Card UIDs still written.
        del calls[:]
        Fake(refuse_link=True).bind_uid({"id": 76, "extra": {}, "tags": []}, "04AA")
        self.assertIn(("PATCH", "/v1/spool/76", {"extra": {"card_uids": '"04AA"'}}), calls)

    def _with_zmod_popup(self):
        plugin, gcode, objects = make_plugin()
        popups = []
        gcode.commands["RUN_ZCOLOR"] = lambda gcmd: popups.append(gcmd.params)
        plugin._handle_connect()
        return plugin, gcode, popups

    def test_zmod_popup_skipped_after_assignment(self):
        plugin, gcode, popups = self._with_zmod_popup()
        self.scan(plugin, "PLA:112233")
        gcode.commands["NFC_ASSIGN"](FakeGcmd(gcode, SLOT=2))
        gcode.commands["RUN_ZCOLOR"](FakeGcmd(gcode, SLOT=2, HIDE=1))
        self.assertEqual(popups, [])
        self.assertIn("skipping the color selection", gcode.info[-1])
        # Only once: the next insert into slot 2 shows Zmod's popup again.
        gcode.commands["RUN_ZCOLOR"](FakeGcmd(gcode, SLOT=2, HIDE=1))
        self.assertEqual(len(popups), 1)

    def test_zmod_popup_assigns_pending_scan(self):
        plugin, gcode, popups = self._with_zmod_popup()
        self.scan(plugin, "PETG:445566")
        gcode.commands["RUN_ZCOLOR"](FakeGcmd(gcode, SLOT=1, HIDE=1))
        self.assertEqual(popups, [])
        self.assertEqual(gcode.in_command, ["CHANGE_ZCOLOR SLOT=1 HEX=445566 TYPE=PETG SILENT=1"])
        self.assertIsNone(plugin.pending)
        gcode.commands["RUN_ZCOLOR"](FakeGcmd(gcode, SLOT=1, HIDE=1))
        self.assertEqual(len(popups), 1)

    def test_zmod_popup_untouched_otherwise(self):
        plugin, gcode, popups = self._with_zmod_popup()
        gcode.commands["RUN_ZCOLOR"](FakeGcmd(gcode, SLOT=3, HIDE=1))
        gcode.commands["RUN_ZCOLOR"](FakeGcmd(gcode, SLOT=3, HEX="FFFFFF", TYPE="PLA"))
        self.assertEqual(len(popups), 2)

    def test_spool_title_drops_repeated_words(self):
        from ffnfc.spoolman import spool_title
        spool = {"id": 15, "filament": {"vendor": {"name": "Snapmaker"}, "material": "PLA",
                                        "name": "PLA SnapSpeed"}}
        self.assertEqual(spool_title(spool), "Snapmaker PLA SnapSpeed")

    def test_bad_vendor_key(self):
        with self.assertRaises(ConfigError):
            make_plugin({"bambu_key": "00" * 16})

    def test_no_filament_data(self):
        plugin, gcode, _ = make_plugin()
        plugin._handle_scan(plugin._process(type2_tag(b"")))
        self.assertIsNone(plugin.pending)
        self.assertIn("has no filament data", gcode.info[-1])

    def test_beep_on_scan(self):
        plugin, gcode, _ = make_plugin()
        beeps = []
        plugin.beeper.beep = lambda frequency, duration: beeps.append((frequency, duration))
        self.scan(plugin, "PETG:00FF00")
        plugin._handle_scan(plugin._process(type2_tag(b"")))
        gcode.commands["NFC_DISABLE"](FakeGcmd(gcode))
        self.scan(plugin, "PLA:FF0000")
        self.assertEqual(beeps, [(1500, 0.2), (600, 0.6)])
        self.assertEqual(gcode.scripts, [])

    def test_scan_gcode_replaces_beep(self):
        plugin, gcode, _ = make_plugin({"scan_gcode": "FLASH_OK", "scan_error_gcode": "FLASH_BAD"})
        beeps = []
        plugin.beeper.beep = lambda frequency, duration: beeps.append((frequency, duration))
        self.scan(plugin, "PETG:00FF00")
        plugin._handle_scan(plugin._process(type2_tag(b"")))
        self.assertEqual(gcode.scripts, ["FLASH_OK", "FLASH_BAD"])
        self.assertEqual(beeps, [])



class FakeProcess(object):
    def __init__(self, output, error=b""):
        self.output = output
        self.error = error
        self.killed = False

    def communicate(self, timeout=None):
        return self.output, self.error

    def kill(self):
        self.killed = True


class QrScanTest(unittest.TestCase):
    def camera_result(self, plugin, result, error=b""):
        process = FakeProcess(json.dumps(result).encode() + b"\n", error)
        plugin._qr_process = process
        plugin._qr_prompt = True
        plugin._qr_wait(process, 5)
        return process

    def spoolman(self, plugin, objects):
        objects["webhooks"]._remote_methods["spoolman_set_active_spool"] = True
        calls = []

        class FakeSpoolman(object):
            def get_spool(self, spool_id):
                calls.append(("get", spool_id))
                return {"id": spool_id, "filament": {"material": "PETG", "color_hex": "445566",
                                                     "name": "Blue"}, "extra": {}}

            def find_spool_by_uid(self, uid):
                calls.append(("uid", uid))
                return None

        plugin.spoolman = FakeSpoolman()
        return calls

    def test_spoolman_label(self):
        plugin, gcode, objects = make_plugin(afclite=True)
        calls = self.spoolman(plugin, objects)
        self.camera_result(plugin, {"text": "web+spoolman:s-12", "frames": 2})
        self.assertEqual(calls, [("get", 12)])   # no UID lookup for a QR code
        self.assertIsNone(plugin._qr_process)
        self.assertIn("// action:prompt_end", gcode.raw)
        self.assertTrue(gcode.raw[-1] == "// action:prompt_show" and plugin.pending is not None)
        self.assertTrue(any("Spoolman spool 12" in r and "Spoolman QR code" in r for r in gcode.raw))
        gcode.commands["NFC_ASSIGN"](FakeGcmd(gcode, SLOT=2))
        self.assertEqual(gcode.in_command, ["SET_SPOOL_ID LANE=E1 SPOOL_ID=12"])

    def test_text_label_without_spoolman(self):
        plugin, gcode, _ = make_plugin()
        self.camera_result(plugin, {"text": "PLA:FF8800"})
        gcode.commands["NFC_ASSIGN"](FakeGcmd(gcode, SLOT=3))
        self.assertEqual(gcode.in_command, ["CHANGE_ZCOLOR SLOT=3 HEX=FF8800 TYPE=PLA SILENT=1"])
        gcode.commands["NFC_DUMP"](FakeGcmd(gcode))
        self.assertIn("QR text: PLA:FF8800", gcode.info[-1])

    def test_nothing_found(self):
        plugin, gcode, _ = make_plugin()
        self.camera_result(plugin, {"error": "no QR code found", "frames": 9})
        self.assertEqual(gcode.info[-1], "NFC: no QR code found")
        self.assertIsNone(plugin.pending)
        # The screen says so too, with a retry.
        self.assertIn("// action:prompt_text No QR code found", gcode.raw)
        self.assertIn("// action:prompt_footer_button Try again|NFC_QR_SCAN|primary", gcode.raw)
        self.assertEqual(gcode.raw[-1], "// action:prompt_show")
        gcode.commands["NFC_QR_CANCEL"](FakeGcmd(gcode))
        self.assertEqual(gcode.raw[-1], "// action:prompt_end")
        self.assertFalse(plugin._qr_prompt)

    def test_camera_process_crash(self):
        plugin, gcode, _ = make_plugin()
        process = FakeProcess(b"", b"Traceback ...\nImportError: boom\n")
        plugin._qr_process = process
        plugin._qr_wait(process, 5)
        self.assertEqual(gcode.info[-1], "NFC: camera scan failed: ImportError: boom")

    def test_cancel(self):
        plugin, gcode, _ = make_plugin()
        process = FakeProcess(json.dumps({"text": "PLA:FF8800"}).encode())
        plugin._qr_process = process
        plugin._qr_prompt = True
        gcode.commands["NFC_QR_CANCEL"](FakeGcmd(gcode))
        self.assertTrue(process.killed)
        plugin._qr_wait(process, 5)              # the killed process still reports
        self.assertIsNone(plugin.pending)
        self.assertEqual(gcode.info[-1], "NFC: stopped looking for a QR label")

    def test_scan_command(self):
        plugin, gcode, _ = make_plugin({"qr_ffmpeg": "/opt/ffmpeg"})
        started = []
        release = threading.Event()

        class RunningProcess(FakeProcess):
            def communicate(self, timeout=None):
                release.wait(5)
                return self.output, self.error

        def popen(command, **kw):
            started.append((command, kw))
            return RunningProcess(b'{"error": "no QR code found"}')

        original = klipper_plugin.subprocess.Popen
        klipper_plugin.subprocess.Popen = popen
        try:
            gcode.commands["NFC_QR_SCAN"](FakeGcmd(gcode, TIME=8))
        finally:
            klipper_plugin.subprocess.Popen = original
        command, kw = started[0]
        self.assertEqual(command[1:3], ["-m", "ffnfc.camera"])
        self.assertIn("/opt/ffmpeg", command)
        self.assertEqual(command[command.index("--time") + 1], "8")
        self.assertEqual(kw["cwd"], klipper_plugin.PLUGIN_DIR)
        self.assertIn("NFC_QR_CANCEL", "".join(gcode.raw))
        with self.assertRaises(CommandError):
            gcode.commands["NFC_QR_SCAN"](FakeGcmd(gcode))   # still running
        release.set()

    def test_not_while_printing(self):
        plugin, gcode, objects = make_plugin()

        class Stats(object):
            def get_status(self, eventtime):
                return {"state": "printing"}

        objects["print_stats"] = Stats()
        with self.assertRaises(CommandError):
            gcode.commands["NFC_QR_SCAN"](FakeGcmd(gcode))


if __name__ == "__main__":
    unittest.main()
