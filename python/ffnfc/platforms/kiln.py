"""Kiln OS (Flashforge AD5X): slots come from Kiln's ifs and ifs_materials
objects and are set with IFS_SET_MATERIAL (type, exact color, nozzle
temperature). When Kiln's spoolman plugin is on, SPOOLMAN_SET_SLOT records
the slot's Spoolman spool, so it becomes the active spool whenever that
slot loads.

Kiln's kiln_lanes Moonraker component mirrors the slots into lane_data;
after an assignment the slot's record is written once more
(lane_data.KilnLaneData) so HelixScreen takes the scanned spool over a
color picked on its screen.

With KILN_AFC (the full AFC on the IFS), a Spoolman spool goes to the
slot's AFC lane with SET_SPOOL_ID instead: AFC then keeps the lane's spool,
sets the active spool and writes lane_data, and its AFC_IFS unit passes
the spool's material and color on to Kiln. A tag without a spool still
uses IFS_SET_MATERIAL, which AFC_IFS copies into the lane.

USB readers and the speaker need a root hook (root/ff-nfc-hook.sh), which
install.sh puts in place; NFC_STATUS says when it is missing or old."""

import os
import re

from ..lane_data import KilnLaneData
from ..spoolman import is_bound
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
# Seconds after SET_SPOOL_ID before the AFC lane is checked for the spool;
# AFC's Spoolman lookup took well under a second on the AD5X.
AFC_CHECK_DELAY = 5.0
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
        self._checks = []            # (slot, spool ID, label) given to AFC
        self._check_timer = None

    def connect(self):
        pass

    def disconnect(self):
        if self._check_timer is not None:
            self.core.reactor.unregister_timer(self._check_timer)
            self._check_timer = None
        self._checks = []

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

    def afc_unit_lane(self, slot_number):
        """(AFC_IFS unit, AFC lane) on this IFS slot when KILN_AFC (AFC with
        an AFC_IFS unit) runs, else (None, None)."""
        afc = self.printer.lookup_object("AFC", None)
        units = getattr(afc, "units", None)
        if not isinstance(units, dict):
            return None, None
        for unit in units.values():
            find = getattr(unit, "_lane_for_slot", None)
            if find is None:
                continue
            lane = find(slot_number)
            if lane is not None:
                return unit, lane
        return None, None

    def afc_lane(self, slot_number):
        return self.afc_unit_lane(slot_number)[1]

    def _check_later(self, slot_number, spool_id, label):
        """AFC fetches a spool from Spoolman after SET_SPOOL_ID returns and
        may refuse it then; say so if the lane does not have it AFC_CHECK_DELAY
        seconds later."""
        reactor = self.core.reactor
        self._checks.append((slot_number, spool_id, label))
        if self._check_timer is None:
            self._check_timer = reactor.register_timer(self._check_spools)
        reactor.update_timer(self._check_timer, reactor.monotonic() + AFC_CHECK_DELAY)

    def _check_spools(self, eventtime):
        checks, self._checks = self._checks, []
        for slot_number, spool_id, label in checks:
            lane = self.afc_lane(slot_number)
            if lane is not None and getattr(lane, "spool_id", None) != spool_id:
                self.core.gcode.respond_info(
                    "NFC: AFC did not take Spoolman spool %d for %s (%s); klippy.log has"
                    " AFC's reason" % (spool_id, label, lane.name))
        return self.core.reactor.NEVER

    def afc_lanes(self):
        afc = self.printer.lookup_object("AFC", None)
        lanes = getattr(afc, "lanes", None)
        return list(lanes.values()) if isinstance(lanes, dict) else []

    def assign(self, scan, slot_number, slots, label, respond, run_gcode):
        core = self.core
        spool = scan.spool
        afc_unit, afc_lane = self.afc_unit_lane(slot_number)
        lane = afc_lane.name if afc_lane is not None else None
        if lane is not None and spool is not None:
            # AFC takes the spool's material, color and weight from Spoolman,
            # makes it the active spool when the lane loads and writes
            # lane_data; AFC_IFS passes material and color on to Kiln.
            weight = spool.get("remaining_weight", 1)
            afc = self.printer.lookup_object("AFC", None)
            if (not getattr(afc, "disable_weight_check", False)
                    and (weight is None or weight <= 0)):
                # AFC refuses it and clears the lane: leave the lane alone.
                respond("NFC: Spoolman spool %d (%s) has no filament left, which AFC refuses;"
                        " %s is left as it was. Set the spool's remaining weight in Spoolman"
                        " and scan it again." % (spool["id"], scan.title(), label))
                return
            for other in self.afc_lanes():
                if other is not afc_lane and getattr(other, "spool_id", None) == spool["id"]:
                    # AFC refuses a spool that is still on another lane: it
                    # was moved, so that lane loses it.
                    run_gcode("SET_SPOOL_ID LANE=%s SPOOL_ID=" % other.name)
                    respond("NFC: spool %d left %s" % (spool["id"], other.name))
            run_gcode("SET_SPOOL_ID LANE=%s SPOOL_ID=%d" % (lane, spool["id"]))
            self._check_later(slot_number, spool["id"], label)
            if core.bind_card_uid and scan.uid and not is_bound(spool, scan.uid):
                core._in_background(core.spoolman.bind_uid, spool, scan.uid, scan.tag_format)
            respond("NFC: %s (%s) is now %s (Spoolman spool %d)"
                    % (label, lane, scan.title(), spool["id"]))
            syncs = (getattr(afc_unit, "sync_spools", False) is True
                     and getattr(afc_unit, "spool_slots", None) is not None)
            if self.spoolman_slots is not None and not syncs:
                # KILN_AFC before its sync_spools, or with it off: both set
                # the active spool, from lists that drift apart.
                respond("NFC: Kiln's spoolman plugin and AFC both set the active spool;"
                        " update KILN_AFC (it keeps them in step) or turn the plugin off"
                        " (INSTALL_PLUGIN NAME=spoolman ENABLE=0). Spoolman tracking stays"
                        " on through Moonraker either way.")
            return
        if lane is not None and getattr(afc_lane, "spool_id", None) is not None:
            # A tag without a Spoolman spool: the lane's old spool goes.
            run_gcode("SET_SPOOL_ID LANE=%s SPOOL_ID=" % lane)
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
            if lane is None:
                # With AFC, AFC_IFS copies the slot into the lane and AFC
                # writes lane_data.
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
        if core.bind_card_uid and scan.uid and not is_bound(spool, scan.uid):
            core._in_background(core.spoolman.bind_uid, spool, scan.uid, scan.tag_format)
        respond("NFC: %s is now %s (Spoolman spool %d)" % (label, scan.title(), spool["id"]))
        if self.spoolman_slots is None:
            respond("NFC: turn on Kiln's spoolman plugin (INSTALL_PLUGIN NAME=spoolman"
                    " ENABLE=1) to keep each slot's spool")

    def status_lines(self):
        from ..klipper_plugin import PLUGIN_DIR
        return hook_problems(PLUGIN_DIR)

    def qr_args(self):
        return []
