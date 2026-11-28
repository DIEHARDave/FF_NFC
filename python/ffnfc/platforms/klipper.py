"""Any Klipper printer with Moonraker (Sovol, Voron, RatRig, ...): one
toolhead, or AFC lanes when the AFC-Klipper-Add-On runs.

One toolhead: a scanned spool is the spool on the printer, so there is no
slot prompt. The tag's Spoolman spool becomes Moonraker's active spool
(Spoolman tracks its usage), the tag's UID is linked to the spool in
Spoolman, and lane_data slot 1 gets its material and color for slicers and
screens that read it. A tag with no Spoolman spool clears the active spool
(option clear_active_spool), so usage isn't booked on the spool that was
loaded before.

With AFC, the slots are AFC's lanes in AFC's order: a scan asks which lane
(or goes to the lane the filament is inserted into), and a Spoolman spool
goes to that lane with SET_SPOOL_ID. AFC then keeps the lane's spool, makes
it the active spool when the lane loads and writes lane_data. A tag without
a spool sets the lane's material and color (SET_MATERIAL, SET_COLOR).

USB readers: the reader search never probes a Klipper MCU (USB ID
1d50:614e) or a port named by an [mcu] serial: option, and only probes the
USB serial chips readers use (readers/detect.py)."""

import re

from ..lane_data import LaneData
from ..spoolman import is_bound
from ..tag import normalize_color

# Crowsnest/ustreamer behind Mainsail's or Fluidd's nginx.
QR_URL = "http://127.0.0.1/webcam/?action=snapshot"
QR_WIDTH = 1024
# Seconds after SET_SPOOL_ID before the AFC lane is checked for the spool
# (AFC fetches it from Spoolman after the command returns).
AFC_CHECK_DELAY = 5.0
# The [ff_nfc] options only this platform reads.
OPTIONS = ("filament_sensor", "clear_active_spool")


def afc_material(material):
    """MATERIAL= for AFC's SET_MATERIAL: one word (PLA SILK -> PLA-SILK)."""
    text = re.sub(r"[^A-Za-z0-9+_.-]+", "-", str(material or "").strip()).strip("-")
    return text[:32] or None


class KlipperPlatform(object):
    name = "klipper"
    lane_data_class = LaneData
    qr_url = QR_URL
    qr_width = QR_WIDTH

    def __init__(self, core, config):
        self.core = core
        self.printer = core.printer
        self.sensor_name = config.get("filament_sensor", "filament_sensor")
        self.clear_active_spool = config.getboolean("clear_active_spool", True)
        self.sensor = None
        self.current = {"material": "", "color": ""}
        self._checks = []            # (lane name, spool ID, label) given to AFC
        self._check_timer = None

    def connect(self):
        pass

    def disconnect(self):
        if self._check_timer is not None:
            self.core.reactor.unregister_timer(self._check_timer)
            self._check_timer = None
        self._checks = []

    def ready(self):
        self.sensor = self.printer.lookup_object(
            "filament_switch_sensor %s" % self.sensor_name, None)

    # --- AFC ---------------------------------------------------------------

    def afc(self):
        return self.printer.lookup_object("AFC", None)

    def afc_lanes(self):
        """AFC's lanes in AFC's order, or [] without AFC."""
        lanes = getattr(self.afc(), "lanes", None)
        return list(lanes.values()) if isinstance(lanes, dict) else []

    def afc_lane(self, slot_number):
        lanes = self.afc_lanes()
        return lanes[slot_number - 1] if 0 < slot_number <= len(lanes) else None

    def lane_slot(self, lane):
        """The slot of an AFC lane name (NFC_ASSIGN LANE=), or None."""
        for n, found in enumerate(self.afc_lanes(), 1):
            if getattr(found, "name", None) == lane:
                return n
        return None

    def _afc_slots(self, lanes):
        current = None
        function = getattr(self.afc(), "function", None)
        try:
            current = function.get_current_lane() if function is not None else None
        except Exception:
            current = None
        info = {"toolchanger": False, "current": 0, "valid_types": [], "palette": [],
                "presence": True, "slots": {}}
        for n, lane in enumerate(lanes, 1):
            if lane.name == current:
                info["current"] = n
            info["slots"][n] = {
                "material": str(getattr(lane, "material", "") or ""),
                "color": str(getattr(lane, "color", "") or "").replace("#", "").upper()[:6],
                # A spool is in the lane (AFC's prep sensor).
                "loaded": bool(getattr(lane, "prep_state", False)),
            }
        return info

    def _check_later(self, lane_name, spool_id, label):
        """AFC fetches a spool from Spoolman after SET_SPOOL_ID returns and
        may refuse it then; say so if the lane does not have it
        AFC_CHECK_DELAY seconds later."""
        reactor = self.core.reactor
        self._checks.append((lane_name, spool_id, label))
        if self._check_timer is None:
            self._check_timer = reactor.register_timer(self._check_spools)
        reactor.update_timer(self._check_timer, reactor.monotonic() + AFC_CHECK_DELAY)

    def _check_spools(self, eventtime):
        checks, self._checks = self._checks, []
        lanes = dict((l.name, l) for l in self.afc_lanes())
        for lane_name, spool_id, label in checks:
            lane = lanes.get(lane_name)
            if lane is not None and getattr(lane, "spool_id", None) != spool_id:
                self.core.gcode.respond_info(
                    "NFC: AFC did not take Spoolman spool %d for %s; klippy.log has"
                    " AFC's reason" % (spool_id, label))
        return self.core.reactor.NEVER

    def _afc_assign(self, scan, lane, label, respond, run_gcode):
        core = self.core
        spool = scan.spool
        if spool is not None:
            weight = spool.get("remaining_weight", 1)
            if (not getattr(self.afc(), "disable_weight_check", False)
                    and (weight is None or weight <= 0)):
                # AFC refuses it and clears the lane: leave the lane alone.
                respond("NFC: Spoolman spool %d (%s) has no filament left, which AFC refuses;"
                        " %s is left as it was. Set the spool's remaining weight in Spoolman"
                        " and scan it again." % (spool["id"], scan.title(), label))
                return
            for other in self.afc_lanes():
                if other is not lane and getattr(other, "spool_id", None) == spool["id"]:
                    # AFC refuses a spool that is still on another lane: it
                    # was moved, so that lane loses it.
                    run_gcode("SET_SPOOL_ID LANE=%s SPOOL_ID=" % other.name)
                    respond("NFC: spool %d left %s" % (spool["id"], other.name))
            run_gcode("SET_SPOOL_ID LANE=%s SPOOL_ID=%d" % (lane.name, spool["id"]))
            self._check_later(lane.name, spool["id"], label)
            if core.bind_card_uid and scan.uid and core.spoolman is not None \
                    and not is_bound(spool, scan.uid):
                core._in_background(core.spoolman.bind_uid, spool, scan.uid, scan.tag_format)
            respond("NFC: %s is now %s (Spoolman spool %d)" % (label, scan.title(), spool["id"]))
            return
        if getattr(lane, "spool_id", None) is not None:
            # A tag without a Spoolman spool: the lane's old spool goes.
            run_gcode("SET_SPOOL_ID LANE=%s SPOOL_ID=" % lane.name)
        material = afc_material(scan.material())
        color = normalize_color(scan.color())
        if material:
            run_gcode("SET_MATERIAL LANE=%s MATERIAL=%s" % (lane.name, material))
        if color:
            run_gcode("SET_COLOR LANE=%s COLOR=%s" % (lane.name, color))
        what = " ".join(p for p in (material, "#" + color if color else "") if p) or scan.title()
        respond("NFC: %s is now %s (not in Spoolman)" % (label, what))

    # --- one toolhead ------------------------------------------------------

    def _loaded(self):
        helper = getattr(self.sensor, "runout_helper", None)
        return bool(getattr(helper, "filament_present", False))

    def slots(self):
        lanes = self.afc_lanes()
        if lanes:
            return self._afc_slots(lanes)
        return {"toolchanger": False, "current": 1, "valid_types": [], "palette": [],
                "presence": False,
                "slots": {1: {"material": self.current["material"],
                              "color": self.current["color"],
                              "loaded": self._loaded()}}}

    def slot_label(self, slots, n):
        lane = self.afc_lane(n)
        if lane is not None:
            return "Lane %s" % lane.name
        return "The toolhead"

    def slot_word(self, slots):
        return "AFC lane" if self.afc_lanes() else "toolhead"

    def assign(self, scan, slot_number, slots, label, respond, run_gcode):
        lane = self.afc_lane(slot_number)
        if lane is not None:
            self._afc_assign(scan, lane, label, respond, run_gcode)
            return
        core = self.core
        spool = scan.spool
        material = scan.material() or ""
        color = normalize_color(scan.color()) or ""
        self.current = {"material": material, "color": color}
        core._write_lane_data(1, core._lane_info(scan, material or None, color or None))
        if spool is not None:
            core._set_active_spool(spool["id"])
            if core.bind_card_uid and scan.uid and core.spoolman is not None \
                    and not is_bound(spool, scan.uid):
                core._in_background(core.spoolman.bind_uid, spool, scan.uid, scan.tag_format)
            respond("NFC: %s now has %s (Spoolman spool %d, now the active spool)"
                    % (label, scan.title(), spool["id"]))
            return
        text = " ".join(p for p in (material, "#" + color if color else "") if p) or scan.title()
        if self.clear_active_spool:
            core._set_active_spool(None)
            respond("NFC: %s now has %s; not in Spoolman, so no spool is active"
                    % (label, text))
        else:
            respond("NFC: %s now has %s (not in Spoolman)" % (label, text))

    def lane_data_producer(self):
        """AFC writes lane_data for its lanes; with one toolhead nothing
        does, and FF_NFC writes slot 1."""
        return "AFC" if self.afc() is not None else None

    def status_lines(self):
        lanes = self.afc_lanes()
        if lanes:
            return ["AFC lanes: %s" % ", ".join(l.name for l in lanes)]
        if self.sensor is None:
            return []
        return ["Filament sensor: %s" % ("filament present" if self._loaded() else "empty")]

    def qr_args(self):
        return []
