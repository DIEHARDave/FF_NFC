"""The speaker beeper against a fake cmd_pwm."""

import os
import shutil
import stat
import tempfile
import unittest

import helpers  # noqa: F401  (puts the plugin on sys.path)
from ffnfc import beeper

FAKE_CMD_PWM = """#!/bin/sh
echo "$@" >> "%s"
[ "$1" = config ] && [ -f "%s" ] && echo "pwm:ch 13 config failed Operation not permitted!"
exit 0
"""


class BeeperTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.log = os.path.join(self.dir, "calls")
        self.fail_flag = os.path.join(self.dir, "fail")
        self.cmd = os.path.join(self.dir, "cmd_pwm")
        with open(self.cmd, "w") as f:
            f.write(FAKE_CMD_PWM % (self.log, self.fail_flag))
        os.chmod(self.cmd, stat.S_IRWXU)
        self.device = os.path.join(self.dir, "jz_pwm")
        open(self.device, "w").close()
        self.saved = (beeper.CMD_PWM, beeper.DEVICE, beeper.AD5X_SOUND_CONF)
        beeper.CMD_PWM = ((os.path.join(self.dir, "missing"), None), (self.cmd, None))
        beeper.DEVICE = self.device
        beeper.AD5X_SOUND_CONF = os.path.join(self.dir, "no-ad5x-sound.conf")

    def tearDown(self):
        beeper.CMD_PWM, beeper.DEVICE, beeper.AD5X_SOUND_CONF = self.saved
        shutil.rmtree(self.dir)

    def calls(self):
        with open(self.log) as f:
            return [line.split()[0] for line in f]

    def test_tone_sequence(self):
        b = beeper.Beeper()
        self.assertEqual(b.find(), "cmd_pwm")
        self.assertIn(self.cmd, b.describe())
        b._busy.acquire()
        b._play(1500, 0.01)
        self.assertEqual(self.calls(), ["disable", "config", "set_level", "set_prescale", "set_wc",
                                        "enable_channels", "disable_channels", "disable"])
        with open(self.log) as f:
            self.assertIn("set_wc pc12 444444 222222", f.read())

    def test_failed_step_still_stops_the_channel(self):
        open(self.fail_flag, "w").close()
        b = beeper.Beeper()
        b.find()
        b._busy.acquire()
        b._play(1500, 0.01)
        self.assertEqual(self.calls(), ["disable", "config", "disable_channels", "disable"])
        self.assertTrue(b._busy.acquire(False))

    def test_no_speaker(self):
        os.remove(self.device)
        b = beeper.Beeper()
        self.assertIsNone(b.find())
        self.assertFalse(b.beep(1500, 0.2))
        self.assertIn("missing", b.describe())
        open(self.device, "w").close()
        self.assertEqual(b.find(), "cmd_pwm")

    def test_off(self):
        b = beeper.Beeper(False)
        self.assertFalse(b.beep(1500, 0.2))
        self.assertEqual(b.describe(), "no beep: beep: False")


if __name__ == "__main__":
    unittest.main()
