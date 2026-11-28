"""Kiln OS (Flashforge AD5X): slots come from Kiln's ifs and ifs_materials
objects and are set with IFS_SET_MATERIAL (type, exact color, nozzle
temperature). When Kiln's spoolman plugin is on, SPOOLMAN_SET_SLOT records
the slot's Spoolman spool, so it becomes the active spool whenever that
slot loads.

Kiln's kiln_lanes Moonraker component mirrors the slots into lane_data.
From Kiln plugin API 1.12 (Kiln OS v1.0.1, kiln-os issue #101)
IFS_SET_MATERIAL also keeps the slot's spool (SPOOL_ID= VENDOR= NAME=
FILAMENT_ID= BED_TEMP=) and kiln_lanes writes it into lane_data, so FF_NFC
sends everything there and kiln_lanes stays the only writer. On older Kiln
only type, color and temperature reach the slot; with lane_data: always
the record is written once more after Kiln's (lane_data.KilnLaneData).

With KILN_AFC (the full AFC on the IFS), a Spoolman spool goes to the
slot's AFC lane with SET_SPOOL_ID instead: AFC then keeps the lane's spool,
sets the active spool and writes lane_data, and its AFC_IFS unit passes
the spool's material and color on to Kiln. A tag without a spool still
uses IFS_SET_MATERIAL, which AFC_IFS copies into the lane.

USB readers and the speaker need the root part (root/, Kiln OS plugin API
1.6, enabled with ENABLE_PLUGIN NAME=ff_nfc ROOT=1); NFC_STATUS says when
it is missing or old."""

import os
import re

from ..lane_data import KilnLaneData
from ..spoolman import is_bound
from ..tag import normalize_color

UNSET_MATERIALS = ("NONE", "N/A", "?", "")
# What IFS_SET_MATERIAL accepts as TYPE= (Kiln's parse_material).
KILN_MATERIAL_RE = re.compile(r"^[A-Za-z0-9+_.-]{1,16}$")
# Root's copy of root/ff-nfc-hook.sh (Kiln OS plugin API 1.6); keep in
# step with its FF_NFC_HOOK_VERSION.
HOOK_PATH = "/data/ad5x/plugins-root/ff_nfc/ff-nfc-hook.sh"
HOOK_VERSION = 2
# The hook FF_NFC 0.2 installed by hand (root's install.sh removes it).
OLD_HOOK_PATH = "/data/ad5x/ff-nfc-hook.sh"
ENABLE = "ENABLE_PLUGIN NAME=ff_nfc ROOT=1"
# Kiln runs plugin boot scripts after S99ad5x-booted plus 15 s, a minute
# or two after Klipper is ready: no watcher before that is no problem yet.
BOOT_GRACE = 300.0
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
# Kiln plugin API that takes the slot's spool details on IFS_SET_MATERIAL
# (printer.ad5x_plugins.api_version; kiln-os issue #101).
SPOOL_DETAILS_API = (1, 12)
# Kiln refuses the whole IFS_SET_MATERIAL when VENDOR=, NAME= or
# FILAMENT_ID= holds one of these (ifs.py SPOOL_TEXT_BAD), or more than 64
# printable characters, and then keeps nothing, not even type and color.
SLOT_TEXT_BAD = frozenset('"\'\\;%')
SLOT_TEXT_MAX = 64
# [heater_bed] max_temp in Kiln's ad5x.cfg; BED_TEMP= above it is refused.
BED_TEMP_MAX = 130


def api_version(value):
    """(major, minor) from Kiln's "1.12", (0, 0) when missing or unreadable."""
    try:
        major, minor = str(value).strip().split(".")[:2]
        return int(major), int(minor)
    except (TypeError, ValueError):
        return 0, 0


def slot_text(value):
    """VENDOR=/NAME= text Kiln keeps: refused characters dropped, other
    unprintable ones and runs of spaces made one space, at most
    SLOT_TEXT_MAX characters; "" for nothing."""
    text = "".join(" " if not c.isprintable() else c
                   for c in str(value or "") if c not in SLOT_TEXT_BAD)
    return " ".join(text.split())[:SLOT_TEXT_MAX].strip()


def slot_filament_id(value):
    """FILAMENT_ID= text: OrcaSlicer's id exactly as given, or "" when it
    is not one Kiln can keep unchanged (Kiln then writes its own PKLN* id)."""
    text = str(value or "").strip()
    if (not text or len(text) > SLOT_TEXT_MAX or not text.isprintable()
            or " " in text or SLOT_TEXT_BAD.intersection(text)):
        return ""
    return text


def slot_bed_temp(value):
    """BED_TEMP= degrees 1..BED_TEMP_MAX, "" (clear) otherwise."""
    try:
        deg = int(round(float(value)))
    except (TypeError, ValueError):
        return ""
    return "%d" % deg if 0 < deg <= BED_TEMP_MAX else ""


def spool_name(scan):
    """NAME=: the filament's own name without the vendor, which goes in
    VENDOR= (Spoolman's filament name, else the tag's material, modifiers
    and name)."""
    if scan.spool is not None:
        name = (scan.spool.get("filament") or {}).get("name")
        if name:
            return name
    tag = scan.filament
    if tag is None:
        return scan.title()
    title = " ".join(str(w) for w in [tag.material] + list(tag.modifiers) if w)
    if tag.name and tag.name.lower() not in title.lower():
        title = (title + " " + tag.name).strip()
    return title or scan.title()


def spool_params(info):
    """The spool's details for IFS_SET_MATERIAL (plugin API 1.12). All
    five are sent, empty ones clear the slot's old value: a scan is a new
    spool. Text values are double-quoted (spaces and "#" need it)."""
    spool_id = info.get("spool_id")
    params = [" SPOOL_ID=%s" % ("%d" % spool_id if isinstance(spool_id, int)
                                and not isinstance(spool_id, bool) and spool_id > 0 else "")]
    for key, text in (("VENDOR", slot_text(info.get("vendor"))),
                      ("NAME", slot_text(info.get("name"))),
                      ("FILAMENT_ID", slot_filament_id(info.get("filament_id")))):
        params.append(' %s="%s"' % (key, text) if text else " %s=" % key)
    params.append(" BED_TEMP=%s" % slot_bed_temp(info.get("bed_temp")))
    return "".join(params)


def kiln_material(material):
    """TYPE= for IFS_SET_MATERIAL: the material itself, upper case and
    reduced to the characters Kiln takes (PLA SILK -> PLA-SILK), or None.
    Kiln accepts any short name, so ASA stays ASA."""
    text = re.sub(r"[^A-Za-z0-9+_.-]+", "-", str(material or "").strip()).strip("-")
    text = text.upper()[:16]
    return text if text not in UNSET_MATERIALS and KILN_MATERIAL_RE.match(text) else None


def uptime(proc="/proc"):
    try:
        with open(os.path.join(proc, "uptime")) as handle:
            return float(handle.read().split()[0])
    except (IOError, OSError, ValueError, IndexError):
        return BOOT_GRACE + 1


def hook_problems(plugin_dir, hook=HOOK_PATH, proc="/proc", old_hook=OLD_HOOK_PATH):
    """What is wrong with the USB root part, as messages; [] when it is
    installed, current and its watcher runs."""
    if os.path.exists(old_hook):
        return ["the old USB hook %s is still there: run %s" % (old_hook, ENABLE)]
    try:
        with open(hook) as handle:
            text = handle.read()
    except (IOError, OSError):
        return ["USB readers need the root part: run DISABLE_PLUGIN NAME=ff_nfc,"
                " then %s" % ENABLE]
    match = re.search(r"^FF_NFC_HOOK_VERSION=(\d+)", text, re.M)
    if match is None or int(match.group(1)) < HOOK_VERSION:
        return ["the USB root part is outdated: run %s" % ENABLE]
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
    if uptime(proc) <= BOOT_GRACE:
        return []
    return ["the USB watcher is not running: reboot, or run %s" % ENABLE]


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
        self.plugins = None          # Kiln's ad5x_plugins (api_version)
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
        self.plugins = self.printer.lookup_object("ad5x_plugins", None)

    def spool_details(self):
        """True when IFS_SET_MATERIAL takes the slot's spool details (Kiln
        plugin API 1.12 or newer). Older Kiln refuses them."""
        if self.plugins is None:
            return False
        try:
            status = self.plugins.get_status(self.core.reactor.monotonic())
        except Exception:
            return False
        return api_version(status.get("api_version")) >= SPOOL_DETAILS_API

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
            # lane_for_slot: the shared AFC_IFS unit (afc-kiln v0.3.0);
            # _lane_for_slot: older KILN_AFC.
            find = (getattr(unit, "lane_for_slot", None)
                    or getattr(unit, "_lane_for_slot", None))
            if find is None:
                continue
            lane = find(slot_number)
            if lane is not None:
                return unit, lane
        return None, None

    def afc_lane(self, slot_number):
        return self.afc_unit_lane(slot_number)[1]

    def lane_slot(self, lane):
        """The IFS slot of an AFC lane name (afc-kiln), or None: without AFC
        Kiln's kiln_lanes names slot n lane<n>, which the core maps."""
        if self.printer.lookup_object("AFC", None) is None:
            return None
        for n in self.slots()["slots"]:
            found = self.afc_lane(n)
            if found is not None and getattr(found, "name", None) == lane:
                return n
        return None

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
            # spool_slots: on the unit's Kiln backend since afc-kiln's shared
            # AFC_IFS unit (v0.3.0), on the unit itself before.
            backend = getattr(afc_unit, "backend", None)
            spool_slots = getattr(afc_unit, "spool_slots", None)
            if spool_slots is None:
                spool_slots = getattr(backend, "spool_slots", None)
            syncs = getattr(afc_unit, "sync_spools", False) is True and spool_slots is not None
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
        # Without AFC, Kiln 1.12+ keeps the spool's details with the slot and
        # kiln_lanes writes them into lane_data (kiln-os #101). With AFC,
        # AFC_IFS copies the slot into the lane and AFC keeps the spool.
        details = (lane is None and self.spool_details()
                   and (spool is not None or scan.filament is not None))
        if self.ifs_materials is not None and (material or color or temp or details):
            cmd = "IFS_SET_MATERIAL SLOT=%d" % slot_number
            if material:
                cmd += " TYPE=%s" % material
            if color:
                cmd += " COLOR=%s" % color
            if temp:
                cmd += " TEMP=%d" % temp
            info = core._lane_info(scan, material or current["material"],
                                   color or current["color"])
            if temp:
                info["nozzle_temp"] = temp
            if details:
                cmd += spool_params(dict(info, name=spool_name(scan)))
            run_gcode(cmd)
            if lane is None:
                # With AFC, AFC_IFS copies the slot into the lane and AFC
                # writes lane_data.
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

    def lane_data_producer(self):
        """AFC (afc-kiln) writes lane_data for its lanes; otherwise Kiln's
        kiln_lanes Moonraker component mirrors the IFS slots."""
        if self.printer.lookup_object("AFC", None) is not None:
            return "AFC"
        return "kiln_lanes"

    def status_lines(self):
        from ..klipper_plugin import PLUGIN_DIR
        lines = hook_problems(PLUGIN_DIR)
        if (self.ifs_materials is not None and self.plugins is not None
                and self.printer.lookup_object("AFC", None) is None
                and not self.spool_details()):
            lines.append("this Kiln OS keeps only type, color and temperature per slot;"
                         " the spool's Spoolman ID, vendor and name reach lane_data from"
                         " Kiln OS v1.0.1 (plugin API 1.12)")
        return lines

    def qr_args(self):
        return []
