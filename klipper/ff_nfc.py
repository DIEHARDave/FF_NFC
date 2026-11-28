# Klipper extras entry point for lanetag-nfc ([ff_nfc]) on any Klipper
# printer with Moonraker (Sovol, Voron, ...): the "klipper" platform.
#
# klipper/install.sh links this file into Klipper's klippy/extras
# directory. The code lives in the plugin's own repository (the ffnfc
# package under python/), found through this file's real path. Zmod loads
# the top-level ff_nfc.py, Kiln OS extras/ff_nfc.py.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os
import sys

_PACKAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                            "python")
if _PACKAGE_DIR not in sys.path:
    sys.path.insert(0, _PACKAGE_DIR)


def load_config(config):
    # Klipper keeps imported modules across RESTART. Drop the package each
    # time so a plugin update takes effect on the next RESTART. (Changes to
    # this file itself need a Klipper service restart.)
    for name in list(sys.modules):
        if name == "ffnfc" or name.startswith("ffnfc."):
            del sys.modules[name]
    from ffnfc import klipper_plugin
    return klipper_plugin.load_config(config, "klipper")
