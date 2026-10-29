# Klipper extras entry point for FF_NFC on Kiln OS ([ff_nfc]).
#
# Kiln OS loads a plugin's extras/*.py while the plugin is enabled, and
# puts its python/ directory (the ffnfc package) on sys.path. Zmod loads
# the ff_nfc.py at the top of the repository instead.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import sys


def load_config(config):
    # Klipper keeps imported modules across RESTART. Drop the package each
    # time so a plugin update takes effect on the next RESTART.
    for name in list(sys.modules):
        if name == "ffnfc" or name.startswith("ffnfc."):
            del sys.modules[name]
    from ffnfc import klipper_plugin
    return klipper_plugin.load_config(config, "kiln")
