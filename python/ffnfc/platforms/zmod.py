"""Zmod (Flashforge AD5X, Creator 5 Pro): slots come from Zmod's zmod_color
object and are set with CHANGE_ZCOLOR, or through zmod_afclite's
SET_SPOOL_ID when that plugin is loaded and the spool is in Spoolman.

After a filament insert Zmod runs RUN_ZCOLOR SLOT=<n> HIDE=1, a popup to
pick the slot's color and material; it is wrapped so slots just set from a
tag skip it, and so an insert while the tag's prompt is open answers it."""

from ..camera import DEFAULT_CHROOT
from ..lane_data import LaneData
from ..spoolman import is_bound, spool_uids
from ..tag import normalize_color

SLOTS_PER_IFS = 4
UNSET_MATERIALS = ("NONE", "N/A", "?", "")
SPOOL_VARIABLE = "ff_nfc_slot%d_spool"
# Both camera streamers Zmod uses (ustreamer on the AD5X, mjpg-streamer on
# the Creator 5) answer mjpg-streamer's snapshot URL.
QR_URL = "http://127.0.0.1:8080/?action=snapshot"
QR_WIDTH = 1024
# The [ff_nfc] options only this platform reads.
OPTIONS = ("skip_zmod_color_prompt", "skip_zmod_color_window", "qr_chroot")


def resolve_material(material, valid_types):
    """Zmod material type for a tag's or Spoolman's material: exact match,
    then the base without filler or modifiers, then the same fallbacks
    zmod_afclite and SpoolSync use (ASA -> ABS, PLA blends -> PLA)."""
    material = str(material or "").strip().upper()
    valid = [str(t).upper() for t in valid_types if str(t).upper() not in UNSET_MATERIALS]
    if not valid:
        return material or None
    candidates = [material, material.split("-")[0], material.split(" ")[0]]
    for candidate in candidates:
        if candidate in valid:
            return candidate
    if material.startswith("ASA"):
        fallback = "ABS"
    elif material in ("COPE", "CPE") or "PLA" in material:
        fallback = "PLA"
    elif material.startswith(("PCTG", "PET")):
        fallback = "PETG"
    else:
        return None
    return fallback if fallback in valid else None


def nearest_palette_color(color, palette):
    """Closest palette entry by the "redmean" weighted RGB distance."""
    def rgb(value):
        return [int(value[i:i + 2], 16) for i in (0, 2, 4)]

    def distance(entry):
        (r1, g1, b1), (r2, g2, b2) = rgb(color), rgb(entry)
        mean = (r1 + r2) / 2
        return ((2 + mean / 256) * (r1 - r2) ** 2 + 4 * (g1 - g2) ** 2
                + (2 + (255 - mean) / 256) * (b1 - b2) ** 2)

    return min(palette, key=distance)


class ZmodPlatform(object):
    name = "zmod"
    lane_data_class = LaneData
    qr_url = QR_URL
    qr_width = QR_WIDTH

    def __init__(self, core, config):
        self.core = core
        self.printer = core.printer
        self.skip_zmod_prompt = config.getboolean("skip_zmod_color_prompt", True)
        self.skip_window = config.getfloat("skip_zmod_color_window", 300., minval=0.)
        self.qr_chroot = config.get("qr_chroot", DEFAULT_CHROOT)
        self.zmod_color = None
        self._assigned = {}          # slot -> reactor time of the NFC assignment
        self._zmod_run_zcolor = None

    def connect(self):
        gcode = self.core.gcode
        if self.skip_zmod_prompt and "RUN_ZCOLOR" in gcode.ready_gcode_handlers:
            self._zmod_run_zcolor = gcode.register_command("RUN_ZCOLOR", None)
            gcode.register_command(
                "RUN_ZCOLOR", self.cmd_RUN_ZCOLOR,
                desc="Zmod slot color popup (skipped for slots set by ff_nfc)")

    def cmd_RUN_ZCOLOR(self, gcmd):
        core = self.core
        if gcmd.get_int("HIDE", 0) == 1:
            slot = gcmd.get_int("SLOT", 0)
            # The tag's prompt is still open: the inserted slot is its answer.
            if core.pending is not None and slot in self.slots()["slots"]:
                scan = core.pending
                core._close_prompt()
                core._assign(scan, slot, gcmd.respond_info)
                self._assigned.pop(slot, None)   # this was the popup to skip
                return
            assigned = self._assigned.pop(slot, None)
            if assigned is not None and core.reactor.monotonic() - assigned <= self.skip_window:
                gcmd.respond_info("NFC: slot %d was set from its tag; skipping the color selection" % slot)
                return
        self._zmod_run_zcolor(gcmd)

    def ready(self):
        self.zmod_color = self.printer.lookup_object("zmod_color", None)

    def slots(self):
        """{"toolchanger", "current", "valid_types", "palette", "presence",
        "slots": {n: {"material", "color", "loaded"}}} from zmod_color."""
        info = {"toolchanger": False, "current": 0, "valid_types": [], "palette": [],
                "presence": False, "slots": {}}
        if self.zmod_color is None:
            info["slots"] = {1: {"material": "", "color": "", "loaded": False}}
            return info
        status = self.zmod_color.get_status(self.core.reactor.monotonic())
        info["toolchanger"] = int(status.get("total_tools", 1) or 1) > 1
        count = max(1, int(status.get("color_limit", SLOTS_PER_IFS) or SLOTS_PER_IFS))
        if self.printer.lookup_object("ifs_jacker", None) is None:
            count = min(count, SLOTS_PER_IFS)
        if info["toolchanger"]:
            info["current"] = max(int(status.get("active_tool_id", -1)), -1) + 1
        else:
            info["current"] = int(status.get("channel", 0) or 0)
        info["valid_types"] = list(status.get("valid_types") or [])
        info["palette"] = [str(p).replace("#", "").upper() for p in status.get("palette") or []
                           if len(str(p).replace("#", "")) == 6]
        presence = info["toolchanger"] or not status.get("display")
        if "filament_known" in status:
            presence = info["toolchanger"] or bool(status["filament_known"])
        info["presence"] = presence
        by_id = {}
        for slot in status.get("slots") or []:
            try:
                by_id[int(slot.get("ID"))] = slot
            except (TypeError, ValueError):
                continue
        for n in range(1, count + 1):
            slot = by_id.get(n, {})
            info["slots"][n] = {
                "material": str(slot.get("Material", "") or ""),
                "color": str(slot.get("HEX", "") or "").replace("#", "").upper()[:6],
                "loaded": bool(slot.get("hasFilament")),
            }
        return info

    def slot_label(self, slots, n):
        return ("T%d" % (n - 1)) if slots["toolchanger"] else ("Slot %d" % n)

    def slot_word(self, slots):
        return "toolhead" if slots["toolchanger"] else "IFS slot"

    def assign(self, scan, slot_number, slots, label, respond, run_gcode):
        core = self.core
        # Zmod's own color popup for this slot's next insert is skipped.
        self._assigned[slot_number] = core.reactor.monotonic()
        spool = scan.spool
        afclite = self.printer.lookup_object("zmod_afclite", None)
        if spool is not None and afclite is not None:
            # zmod_afclite tracks per-lane spools, writes the spool's color
            # and material into Zmod and keeps Moonraker's active spool on
            # the loaded lane.
            cmd = "SET_SPOOL_ID LANE=E%d SPOOL_ID=%d" % (slot_number - 1, spool["id"])
            if core.bind_card_uid and scan.uid and scan.uid not in spool_uids(spool):
                cmd += " CARD_UID=%s" % scan.uid
            run_gcode(cmd)
            # zmod_afclite only writes card_uids; Spoolman's own tag list
            # is linked here.
            if core.bind_card_uid and scan.uid and core.spoolman is not None:
                core._in_background(core.spoolman.link_tag, spool, scan.uid, scan.tag_format)
            respond("NFC: %s is now Spoolman spool %d (%s)" % (label, spool["id"], scan.title()))
            self.write_lane_data(scan, slot_number,
                                 resolve_material(scan.material(), slots["valid_types"]),
                                 normalize_color(scan.color()), slots["palette"])
            return

        material = resolve_material(scan.material(), slots["valid_types"])
        current = slots["slots"][slot_number]
        if material is None:
            material = current["material"] if current["material"].upper() not in UNSET_MATERIALS else None
            respond("NFC: %r is not a material this printer knows; %s keeps %s"
                    % (scan.material(), label, material or "its material"))
        color = normalize_color(scan.color()) or current["color"] or "FFFFFF"
        # Zmod only stores its palette colors; lane_data (OrcaSlicer,
        # HelixScreen) gets the spool's own color.
        zmod_color = color
        if slots["palette"] and color not in slots["palette"]:
            zmod_color = nearest_palette_color(color, slots["palette"])
            respond("NFC: #%s is stored as the closest palette color #%s" % (color, zmod_color))
        if self.zmod_color is not None and material:
            # SILENT=1 keeps Zmod from opening its color picker.
            run_gcode("CHANGE_ZCOLOR SLOT=%d HEX=%s TYPE=%s SILENT=1"
                      % (slot_number, zmod_color, material))
        self.write_lane_data(scan, slot_number, material, color, slots["palette"])
        if spool is not None:
            if self.printer.lookup_object("save_variables", None) is not None:
                run_gcode("SAVE_VARIABLE VARIABLE=%s VALUE=%d"
                          % (SPOOL_VARIABLE % slot_number, spool["id"]))
            if slots["current"] in (0, slot_number) or len(slots["slots"]) == 1:
                core._set_active_spool(spool["id"])
            if core.bind_card_uid and scan.uid and not is_bound(spool, scan.uid):
                core._in_background(core.spoolman.bind_uid, spool, scan.uid, scan.tag_format)
            respond("NFC: %s is now %s (Spoolman spool %d)" % (label, scan.title(), spool["id"]))
        else:
            respond("NFC: %s is now %s #%s" % (label, material or "?", zmod_color))

    def write_lane_data(self, scan, slot_number, material, color, palette=None):
        info = self.core._lane_info(scan, material, color)
        if color and palette:
            # The palette color Zmod gets for this one (zmod_afclite snaps
            # the same way), so its lane_data mirror keeps the exact color.
            info["zmod_color"] = color if color in palette else nearest_palette_color(color, palette)
        self.core._write_lane_data(slot_number, info)

    def status_lines(self):
        return []

    def qr_args(self):
        return ["--chroot", self.qr_chroot]
