"""What differs between the firmwares FF_NFC runs on: how slots are read and
set, lane_data, camera defaults and status checks. Each entry file names
its platform (ff_nfc.py: zmod, extras/ff_nfc.py: kiln)."""

from . import kiln, zmod

MODULES = {"zmod": zmod, "kiln": kiln}
CLASSES = {"zmod": zmod.ZmodPlatform, "kiln": kiln.KilnPlatform}


def make_platform(name, core, config):
    """The platform object. The other platforms' [ff_nfc] options are read
    too (and ignored), so one config works on every printer."""
    platform = CLASSES[name](core, config)
    for other, module in MODULES.items():
        if other != name:
            for option in module.OPTIONS:
                config.get(option, None)
    return platform
