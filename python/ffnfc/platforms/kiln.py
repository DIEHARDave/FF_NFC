"""Kiln OS (Flashforge AD5X): slots come from Kiln's ifs and ifs_materials
objects and are set with IFS_SET_MATERIAL (type, exact color, nozzle
temperature). When Kiln's spoolman plugin is on, SPOOLMAN_SET_SLOT records
the slot's Spoolman spool, so it becomes the active spool whenever that
slot loads.

Kiln's kiln_lanes Moonraker component mirrors the slots into lane_data;
after an assignment the slot's record is written once more
(lane_data.KilnLaneData) so HelixScreen takes the scanned spool over a
color picked on its screen.

USB readers and the speaker need a root hook (root/ff-nfc-hook.sh), which
install.sh puts in place; NFC_STATUS says when it is missing or old."""

import os
import re

from ..lane_data import KilnLaneData
from ..spoolman import spool_uids
from ..tag import normalize_color

UNSET_MATERIALS = ("NONE", "N/A", "?", "")
# What IFS_SET_MATERIAL accepts as TYPE= (Kiln's parse_material).
KILN_MATERIAL_RE = re.compile(r"^[A-Za-z0-9+_.-]{1,16}$")
# The root hook install.sh puts in place (root/ff-nfc-hook.sh); keep in
# step with its FF_NFC_HOOK_VERSION.
HOOK_PATH = "/data/ad5x/ff-nfc-hook.sh"
HOOK_VERSION = 1
# Kiln's S64ad5x-camera starts one ustreamer per camera from port 8090.
QR_URL = "http://127.0.0.1:8090/?action=snapshot"
# Kiln's 1280x720 camera frames are then decoded at half size (draft
# mode): about 1 s per frame on the AD5X instead of 5 s at full size.
QR_WIDTH = 640
# The [ff_nfc] options only this platform reads.
OPTIONS = ("set_temperature",)


def kiln_material(material):
    """TYPE= for IFS_SET_MATERIAL: the material itself, upper case and
    reduced to the characters Kiln takes (PLA SILK -> PLA-SILK), or None.
    Kiln accepts any short name, so ASA stays ASA."""
    text = re.sub(r"[^A-Za-z0-9+_.-]+", "-", str(material or "").strip()).strip("-")
    text = text.upper()[:16]
    return text if text not in UNSET_MATERIALS and KILN_MATERIAL_RE.match(text) else None


def hook_problems(plugin_dir, hook=HOOK_PATH, proc="/proc"):
    """What is wrong with the USB root hook, as messages; [] when it is
    installed, current and its watcher runs."""
    fix = "run sh %s as root" % os.path.join(plugin_dir, "install.sh")
    try:
        with open(hook) as handle:
            text = handle.read()
    except (IOError, OSError):
        return ["USB readers need the root hook: %s" % fix]
    match = re.search(r"^FF_NFC_HOOK_VERSION=(\d+)", text, re.M)
    if match is None or int(match.group(1)) < HOOK_VERSION:
        return ["the USB hook is outdated: %s" % fix]
    # Pid files are root-only under Kiln's umask 077; command lines are not.
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return []
    for pid in pids:
        try:
            with open(os.path.join(proc, pid, "cmdline"), "rb") as handle:
                args = handle.read().split(b"\0")
        except (IOError, OSError):
            continue
        if b"watch" in args and any(a.endswith(b"ff-nfc-hook.sh") for a in args):
            return []
    return ["the USB watcher is not running: %s, or reboot" % fix]


class KilnPlatform(object):
    name = "kiln"
    lane_data_class = KilnLaneData
    qr_url = QR_URL
    qr_width = QR_WIDTH

    def __init__(self, core, config):
        self.core = core
        self.printer = core.printer
        self.set_temp = config.getboolean("set_temperature", True)
        self.ifs = None
        self.ifs_materials = None
        self.spoolman_slots = None   # Kiln's spoolman plugin

    def connect(self):
        pass

    def ready(self):
        self.ifs = self.printer.lookup_object("ifs", None)
        self.ifs_materials = self.printer.lookup_object("ifs_materials", None)
        self.spoolman_slots = self.printer.lookup_object("spoolman_slots", None)

    def slots(self):
        """{"toolchanger", "current", "presence",
        "slots": {n: {"material", "color", "loaded"}}} from Kiln's ifs and
        ifs_materials."""
        info = {"toolchanger": False, "current": 0, "presence": False, "slots": {}}
        if self.ifs is None or self.ifs_materials is None:
            info["slots"] = {1: {"material": "", "color": "", "loaded": False}}
            return info
        now = self.core.reactor.monotonic()
        status = self.ifs.get_status(now)
        materials = self.ifs_materials.get_status(now)
        count = int(materials.get("channel_count") or status.get("channel_count") or 4)
        info["current"] = int(status.get("channel") or 0)
        # Without the IFS link the lanes' sensors are unknown.
        info["presence"] = bool(status.get("connected"))
        loaded = set(status.get("loaded_channels") or [])
        by_slot = materials.get("slots") or {}
        for n in range(1, count + 1):
            slot = by_slot.get(str(n)) or {}
            info["slots"][n] = {
                "material": str(slot.get("type") or ""),
                "color": str(slot.get("color") or "").replace("#", "").upper()[:6],
                "loaded": n in loaded,
            }
        return info

    def slot_label(self, slots, n):
        return "Slot %d" % n

    def slot_word(self, slots):
        return "IFS slot"

    def assign(self, scan, slot_number, slots, label, respond, run_gcode):
        core = self.core
        spool = scan.spool
        current = slots["slots"][slot_number]
        material = kiln_material(scan.material())
        if material is None and scan.material():
            respond("NFC: %r is not a material name Kiln takes; %s keeps %s"
                    % (scan.material(), label, current["material"] or "its material"))
        color = normalize_color(scan.color())
        temp = scan.nozzle_temp() if self.set_temp else None
        if self.ifs_materials is not None and (material or color or temp):
            cmd = "IFS_SET_MATERIAL SLOT=%d" % slot_number
            if material:
                cmd += " TYPE=%s" % material
            if color:
                cmd += " COLOR=%s" % color
            if temp:
                cmd += " TEMP=%d" % temp
            run_gcode(cmd)
            info = core._lane_info(scan, material or current["material"],
                                   color or current["color"])
            if temp:
                info["nozzle_temp"] = temp
            core._write_lane_data(slot_number, info)
        if spool is None:
            what = (material or current["material"] or "?") + (" #" + color if color else "")
            respond("NFC: %s is now %s" % (label, what))
            return
        if self.spoolman_slots is not None:
            # Kiln's spoolman plugin keeps the slot's spool and makes it the
            # active one whenever the slot loads.
            run_gcode("SPOOLMAN_SET_SLOT SLOT=%d SPOOL=%d" % (slot_number, spool["id"]))
        elif slots["current"] in (0, slot_number) or len(slots["slots"]) == 1:
            core._set_active_spool(spool["id"])
        if core.bind_card_uid and scan.uid and scan.uid not in spool_uids(spool):
            core._in_background(core.spoolman.bind_uid, spool, scan.uid)
        respond("NFC: %s is now %s (Spoolman spool %d)" % (label, scan.title(), spool["id"]))
        if self.spoolman_slots is None:
            respond("NFC: turn on Kiln's spoolman plugin (INSTALL_PLUGIN NAME=spoolman"
                    " ENABLE=1) to keep each slot's spool")

    def status_lines(self):
        from ..klipper_plugin import PLUGIN_DIR
        return hook_problems(PLUGIN_DIR)

    def qr_args(self):
        return []
