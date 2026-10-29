"""The printer's speaker: a beep when a tag is read.

The AD5X and the Creator 5 Pro have a piezo on pc12, driven by the
Ingenic PWM driver (/dev/jz_pwm, no sysfs PWM on the stock kernel).
Flashforge's own cmd_pwm tool sets it up; Zmod keeps a working copy in
mod_data (its /usr/bin/cmd_pwm is /bin/echo, mounted over the real one
so the stock app stays quiet). Zmod's M300 macro does not work on the
AD5X, so this does not go through it.

The driver refuses new settings while it thinks the channel is running,
and disable_channels alone leaves it in that state, so every beep starts
and ends with a full disable. Beeps play in a thread, one at a time; a
beep asked for while another plays is dropped.

Kiln OS on its own kernel exposes the piezo through sysfs instead, played
by its ad5x-sound tool, which is used when present.
"""

import logging
import os
import subprocess
import threading
import time

LOGGER = logging.getLogger("zmod_nfc")

GPIO = "pc12"
# (cmd_pwm, library directory or None): Zmod, then Kiln OS with the
# printer's vendor libraries (Kiln keeps Zmod's mod_data in /data/config).
CMD_PWM = (
    ("/usr/data/config/mod_data/cmd_pwm", None),
    ("/data/ad5x/vendor/bin/cmd_pwm", "/data/ad5x/vendor/lib"),
    ("/data/config/mod_data/cmd_pwm", "/data/ad5x/vendor/lib"),
)
DEVICE = "/dev/jz_pwm"
# Kiln OS's player; its conf file exists only when it found the speaker.
AD5X_SOUND = "/usr/bin/ad5x-sound"
AD5X_SOUND_CONF = "/run/ad5x-sound.conf"
# Zmod's audio.py settings for the AD5X piezo.
SETUP = (
    ("config", GPIO, "freq=50000000", "max_level=300", "active_level=1",
     "accuracy_priority=freq"),
    ("set_level", GPIO, "100"),
    ("set_prescale", GPIO, "6"),
)
STEP_TIMEOUT = 5.


class Beeper(object):
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.method = None
        self.reason = "" if enabled else "beep: False"
        self._cmd = None
        self._env = None
        self._busy = threading.Lock()

    def find(self):
        """Look for the speaker; again on every beep until it is found
        (Kiln's root hook may grant access after Klipper started)."""
        if not self.enabled or self.method is not None:
            return self.method
        self._cmd = self._env = None
        if os.path.exists(AD5X_SOUND_CONF) and os.access(AD5X_SOUND, os.X_OK):
            self.method = "ad5x-sound"
            return self.method
        if not os.path.exists(DEVICE):
            self.reason = "no speaker (%s missing)" % DEVICE
            return None
        for cmd, libdir in CMD_PWM:
            if os.access(cmd, os.X_OK) and (libdir is None or os.path.isdir(libdir)):
                self._cmd = cmd
                # Klipper's own preloads are not for it (Zmod's audio
                # scripts drop them too).
                self._env = dict(os.environ)
                self._env.pop("LD_PRELOAD", None)
                if libdir is not None:
                    self._env["LD_LIBRARY_PATH"] = libdir
                break
        else:
            self.reason = "no cmd_pwm found (%s)" % ", ".join(c for c, _ in CMD_PWM)
            return None
        if not os.access(DEVICE, os.R_OK | os.W_OK):
            self.reason = "no access to %s" % DEVICE
            return None
        self.method = "cmd_pwm"
        return self.method

    def describe(self):
        if self.method == "cmd_pwm":
            return "speaker via %s" % self._cmd
        if self.method:
            return "speaker via %s" % self.method
        return "no beep: %s" % self.reason

    def beep(self, frequency, duration):
        """Play a tone in the background; False when there is no speaker or
        another beep is still playing."""
        if self.find() is None or frequency <= 0 or duration <= 0:
            return False
        if not self._busy.acquire(False):
            return False
        thread = threading.Thread(target=self._play, args=(frequency, duration),
                                  name="zmod_nfc beep")
        thread.daemon = True
        thread.start()
        return True

    def _play(self, frequency, duration):
        try:
            if self.method == "ad5x-sound":
                self._run(AD5X_SOUND, "tone", str(int(frequency)), str(int(duration * 1000)))
            else:
                self._tone(frequency, duration)
        except Exception as exc:
            LOGGER.warning("zmod_nfc: beep failed: %s", exc)
        finally:
            self._busy.release()

    def _tone(self, frequency, duration):
        # One period in ns, high for two thirds of it (as Zmod's audio.py).
        period = 1e9 / frequency
        high, low = int(round(period * 2 / 3)), int(round(period / 3))
        self._run(self._cmd, "disable", GPIO)
        try:
            for step in SETUP:
                self._run(self._cmd, *step)
            self._run(self._cmd, "set_wc", GPIO, str(high), str(low))
            self._run(self._cmd, "enable_channels", GPIO)
            time.sleep(duration)
        finally:
            self._run(self._cmd, "disable_channels", GPIO)
            self._run(self._cmd, "disable", GPIO)

    def _run(self, *args):
        result = subprocess.run(args, env=self._env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=STEP_TIMEOUT)
        output = result.stdout.decode("utf-8", "replace").strip()
        if result.returncode != 0 or "failed" in output:
            raise RuntimeError("%s %s: %s" % (os.path.basename(args[0]), args[1],
                                              output or "exit %d" % result.returncode))
