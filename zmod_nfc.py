# Klipper extras entry point for zmod_nfc.
#
# install.sh links this file into Klipper's klippy/extras directory. The
# code lives next to it, in the plugin's own repository (the zmodnfc
# package), so it is found through this file's real path.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.realpath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)


def load_config(config):
    # Klipper keeps imported modules across RESTART and FIRMWARE_RESTART, and
    # on the AD5X without the stock screen only a reboot restarts the Klipper
    # process. Drop the package each time so a plugin update takes effect on
    # the next RESTART. (Changes to this file itself still need a reboot.)
    for name in list(sys.modules):
        if name == "zmodnfc" or name.startswith("zmodnfc."):
            del sys.modules[name]
    from zmodnfc import klipper_plugin
    return klipper_plugin.load_config(config)
