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

from zmodnfc.klipper_plugin import load_config  # noqa: E402,F401
