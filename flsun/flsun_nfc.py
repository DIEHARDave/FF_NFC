# Klipper extras entry point for lanetag-nfc on the FLSUN T1 / T1 Pro
# ([flsun_nfc], the "flsun" platform). Formerly FLSUN_NFC; the section name
# stays, so existing settings keep working.
#
# flsun/install.sh links this file into Klipper's klippy/extras directory.
# The code lives in the repository (the ffnfc package under python/), found
# through this file's real path.
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
    # time so an update takes effect on the next RESTART.
    for name in list(sys.modules):
        if name == "ffnfc" or name.startswith("ffnfc."):
            del sys.modules[name]
    from ffnfc import klipper_plugin
    return klipper_plugin.load_config(config, "flsun")
