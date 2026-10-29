# Klipper extras entry point for FF_NFC on Zmod ([ff_nfc]).
#
# install.sh links this file into Klipper's klippy/extras directory. The
# code lives in the plugin's own repository (the ffnfc package under
# python/), so it is found through this file's real path. Kiln OS loads
# extras/ff_nfc.py instead.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os
import sys

_PACKAGE_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "python")
if _PACKAGE_DIR not in sys.path:
    sys.path.insert(0, _PACKAGE_DIR)


def load_config(config):
    # Klipper keeps imported modules across RESTART and FIRMWARE_RESTART, and
    # on the AD5X without the stock screen only a reboot restarts the Klipper
    # process. Drop the package each time so a plugin update takes effect on
    # the next RESTART. (Changes to this file itself still need a reboot.)
    for name in list(sys.modules):
        if name == "ffnfc" or name.startswith("ffnfc."):
            del sys.modules[name]
    from ffnfc import klipper_plugin
    return klipper_plugin.load_config(config, "zmod")
