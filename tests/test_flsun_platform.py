"""The "flsun" platform (FLSUN T1): the klipper platform plus the T1's
mainboard guard. Ported from FLSUN_NFC's tests."""
import unittest

import test_klipper_platform as base
from ffnfc import klipper_plugin
from ffnfc.platforms import flsun as platform


def make_plugin(options=None):
    gcode, webhooks = base.FakeGcode(), base.FakeWebhooks()
    objects = {"gcode": gcode, "webhooks": webhooks,
               "filament_switch_sensor filament_sensor": base.FakeSensor()}
    printer = base.FakePrinter(objects)
    options = dict({"lane_data": False, "spoolman": False}, **(options or {}))
    plugin = klipper_plugin.load_config(base.FakeConfig(printer, options), "flsun")
    plugin.platform.ready()
    return plugin, gcode, webhooks


class FlsunPlatformTest(unittest.TestCase):
    def fake_ports(self, mainboard_present):
        originals = (platform.usb_serial_ports, platform.os.path.exists)
        platform.usb_serial_ports = lambda: [("/dev/ttyUSB0", "1a86", "7523", "USB Serial"),
                                             ("/dev/ttyUSB1", "1a86", "7523", "USB Serial"),
                                             ("/dev/ttyUSB2", "10c4", "ea60", "CP2102")]
        platform.os.path.exists = lambda path: mainboard_present

        def restore():
            platform.usb_serial_ports, platform.os.path.exists = originals
        self.addCleanup(restore)

    def test_only_the_mainboard_port_is_excluded(self):
        plugin, _, _ = make_plugin()
        self.fake_ports(True)
        self.assertEqual(list(plugin.excluded_ports),
                         [platform.MAINBOARD_PATH, platform.MAINBOARD_ID])

    def test_unknown_layout_excludes_every_ch340(self):
        plugin, _, _ = make_plugin()
        self.fake_ports(False)
        self.assertEqual(list(plugin.excluded_ports), ["/dev/ttyUSB0", "/dev/ttyUSB1"])

    def test_named_device_is_not_excluded(self):
        plugin, _, _ = make_plugin({"device": "/dev/ttyUSB1"})
        self.assertEqual(list(plugin.excluded_ports), [])

    def test_one_toolhead_like_the_klipper_platform(self):
        plugin, gcode, webhooks = make_plugin()
        plugin._write_lane_data = lambda slot, info: None
        scan = plugin._process(base.text_tag("PETG:00FF00"))
        self.assertEqual(plugin._handle_scan(scan), "assigned")
        self.assertEqual(webhooks.calls, [("spoolman_set_active_spool", {"spool_id": None})])
        self.assertEqual(plugin.platform.name, "flsun")


if __name__ == "__main__":
    unittest.main()
