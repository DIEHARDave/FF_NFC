"""Klipper side of zmod_nfc: the [zmod_nfc] config section.

A worker thread owns the reader: it polls for tags, decodes them and looks
them up in Spoolman, then hands each scan to the reactor. The reactor
shows a prompt (Mainsail, Fluidd and HelixScreen render Klipper's
action:prompt) asking which IFS slot or toolhead the spool goes to, and
writes the choice into Zmod - through zmod_afclite's SET_SPOOL_ID when that
plugin is loaded and the spool is in Spoolman, otherwise with Zmod's own
CHANGE_ZCOLOR. Inserting the filament into a free slot while the prompt is
open picks that slot, which also works with screens that cannot show
prompts.

NFC_QR_SCAN reads a QR label (Spoolman, OpenSpool JSON, text) through the
printer camera instead. The camera module does that in a child process;
its text then takes the same path as a tag's.

Network readers (LaneTag) send their raw tag reads to webhooks
endpoints that Moonraker publishes (zmod_nfc/scan, ...; remote.py); those
take the same path too.

After an assignment the slot's Moonraker lane_data entry gets the spool's
details (lane_data.py), for OrcaSlicer."""

import json
import logging
import os
import subprocess
import sys
import threading
import time

from . import decoders
from .decoders.crypto import key_matches
from .decoders import vendors
from .readers.base import ReaderError
from .readers.detect import READER_TYPES, open_reader
from .spoolman import Spoolman, SpoolmanError, normalize_uid, spool_color, spool_title, spool_uids
from .camera import DECODERS as QR_DECODERS, DEFAULT_CHROOT, DEFAULT_URL, DEFAULT_WIDTH
from .decoders.ndef import text_message, type2_image
from .lane_data import LaneData
from .remote import API_VERSION, RemoteError, tag_from_json
from .tag import KIND_QR, KIND_TYPE2, TECH_CAMERA, TECH_ISO14443A, Tag, normalize_color

LOGGER = logging.getLogger("zmod_nfc")
# The directory holding the zmodnfc package, for the camera child process.
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The last camera scan's frames, to see why a label was not read.
QR_FRAMES_DIR = "/tmp/zmod_nfc_qr"

SPOOLMAN_REMOTE_METHOD = "spoolman_set_active_spool"
SLOTS_PER_IFS = 4
UNSET_MATERIALS = ("NONE", "N/A", "?", "")
SPOOL_VARIABLE = "zmod_nfc_slot%d_spool"
REMOTE_ENDPOINTS = ("scan", "assign", "cancel", "info")
INSERT_POLL = 1.0
# Larger "spool IDs" on tags are not Spoolman's (Spoolman counts from 1).
MAX_SPOOLMAN_ID = 10 ** 7


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


class Scan(object):
    """One tag presented to the reader, decoded and looked up."""

    def __init__(self, tag, filament, records):
        self.tag = tag
        self.filament = filament
        self.records = records
        self.spool = None            # Spoolman spool dict
        self.spoolman_note = ""      # why there is no spool, for the prompt
        self.time = time.time()

    @property
    def uid(self):
        return self.tag.uid_hex

    @property
    def is_qr(self):
        return self.tag.kind == KIND_QR

    def source(self):
        """What was scanned, for messages: "tag 04A1..." or "QR code"."""
        return "QR code" if self.is_qr else "tag " + self.uid

    def title(self):
        if self.spool is not None:
            return spool_title(self.spool)
        if self.filament is not None:
            return self.filament.title()
        return self.source()[0].upper() + self.source()[1:]

    def material(self):
        if self.spool is not None:
            return (self.spool.get("filament") or {}).get("material")
        return self.filament.material if self.filament else None

    def color(self):
        if self.spool is not None:
            return spool_color(self.spool)
        return self.filament.color if self.filament else None

    def has_filament_data(self):
        return bool(self.material() or self.color())

    def summary(self):
        data = {
            "uid": self.uid,
            "tag": self.tag.kind,
            "format": self.filament.fmt if self.filament else None,
            "title": self.title(),
            "material": self.material(),
            "color": self.color(),
            "spool_id": self.spool["id"] if self.spool else None,
            "time": self.time,
        }
        if self.filament is not None:
            data["filament"] = self.filament.as_dict()
        return data


class ZmodNfc(object):
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.webhooks = self.printer.lookup_object("webhooks")

        self.reader_type = config.getchoice(
            "reader", {t: t for t in READER_TYPES}, "auto")
        self.device = config.get("device", "")
        self.baud = config.getint("baud", 115200)
        self.ccid_match = config.get("ccid_reader", "")
        self.poll_interval = config.getfloat("poll_interval", 0.3, minval=0.05)
        self.retry_interval = config.getfloat("retry_interval", 10., minval=1.)
        self.prompt_timeout = config.getfloat("prompt_timeout", 60., minval=0.)
        self.assign_on_insert = config.getboolean("assign_on_insert", True)
        self.scan_while_printing = config.getboolean("scan_while_printing", False)
        self.use_spoolman = config.getboolean("spoolman", True)
        self.auto_create = config.getboolean("spoolman_auto_create", False)
        self.bind_card_uid = config.getboolean("bind_card_uid", True)
        self.match_filament = config.getboolean("spoolman_match_filament", True)
        self.skip_zmod_prompt = config.getboolean("skip_zmod_color_prompt", True)
        self.skip_window = config.getfloat("skip_zmod_color_window", 300., minval=0.)
        self.disabled_formats = [f.strip() for f in config.get("disabled_formats", "").split(",")
                                 if f.strip()]
        self.qr_url = config.get("qr_snapshot_url", DEFAULT_URL)
        self.qr_decoder = config.getchoice("qr_decoder", {d: d for d in QR_DECODERS}, "auto")
        self.qr_chroot = config.get("qr_chroot", DEFAULT_CHROOT)
        self.qr_ffmpeg = config.get("qr_ffmpeg", "")
        self.qr_scan_time = config.getfloat("qr_scan_time", 20., minval=1., maxval=120.)
        self.qr_width = config.getint("qr_width", DEFAULT_WIDTH, minval=320, maxval=4096)
        moonraker_url = config.get("moonraker_url", "http://127.0.0.1:7125")
        self.spoolman = Spoolman(moonraker_url)
        self.lane_data = LaneData(moonraker_url) if config.getboolean("lane_data", True) else None
        self.remote_result = None     # outcome of the last network scan
        self.settings = self._vendor_settings(config)
        self.excluded_ports = []
        for mcu in config.get_prefix_sections("mcu"):
            serial = mcu.get("serial", None)
            if serial:
                self.excluded_ports.append(serial)

        self.zmod_color = None
        self.enabled = True
        self.reader_name = ""
        self.reader_state = "starting"
        self.reader_error = ""
        self.last = None             # last Scan
        self.pending = None          # Scan waiting for a slot
        self.prompt_open = False
        self._prompt_deadline = None
        self._slots_before = {}
        self._timer = None
        self._thread = None
        self._stop = threading.Event()
        self._rescan = threading.Event()
        self._assigned = {}          # slot -> reactor time of the NFC assignment
        self._zmod_run_zcolor = None
        self._qr_process = None      # camera child process while scanning
        self._qr_prompt = False      # the "hold the label up" prompt is shown

        for cmd, desc in (
                ("NFC_STATUS", "Show the NFC reader state and the last scanned tag"),
                ("NFC_ASSIGN", "Assign the scanned spool to a slot: NFC_ASSIGN SLOT=<n>"),
                ("NFC_CANCEL", "Dismiss the NFC slot prompt"),
                ("NFC_PROMPT", "Show the slot prompt for the last scanned tag again"),
                ("NFC_CREATE_SPOOL", "Add the scanned tag's filament to Spoolman"),
                ("NFC_DUMP", "Print everything read from the last tag (RAW=1 adds memory)"),
                ("NFC_RESCAN", "Read the tag on the reader again"),
                ("NFC_TEST_SCAN", "Simulate a scan without a reader: TEXT=PLA:FF0000, "
                                  "SPOOL=<id> or UID=<hex>"),
                ("NFC_QR_SCAN", "Read a spool's QR label with the printer camera [TIME=<s>]"),
                ("NFC_QR_CANCEL", "Stop looking for a QR label"),
                ("NFC_ENABLE", "Resume handling scanned tags"),
                ("NFC_DISABLE", "Ignore scanned tags until NFC_ENABLE")):
            self.gcode.register_command(cmd, getattr(self, "cmd_" + cmd), desc=desc)
        for name in REMOTE_ENDPOINTS:
            self.webhooks.register_endpoint("zmod_nfc/" + name, self._endpoint(name))
        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler("klippy:disconnect", self._handle_disconnect)

    # Configuration --------------------------------------------------------

    def _vendor_settings(self, config):
        settings = {}
        for option, name, digest, length in (
                ("bambu_key", "bambu_key", vendors.BAMBU_KEY_SHA256, None),
                ("creality_key", "creality_key", vendors.CREALITY_KEY_SHA256, 16),
                ("creality_encryption_key", "creality_aes_key", vendors.CREALITY_AES_KEY_SHA256, 16),
                ("snapmaker_key", "snapmaker_key", vendors.SNAPMAKER_KEY_SHA256, None)):
            text = config.get(option, "").strip().replace(" ", "")
            if not text:
                continue
            try:
                key = bytes.fromhex(text)
            except ValueError:
                raise config.error("%s must be hex" % option)
            if not key_matches(key, digest):
                raise config.error("%s is not the expected key (SHA-256 mismatch)" % option)
            settings[name] = key
        keys = []
        for text in config.get("mifare_keys", "").split(","):
            text = text.strip().replace(" ", "")
            if not text:
                continue
            try:
                key = bytes.fromhex(text)
            except ValueError:
                key = b""
            if len(key) != 6:
                raise config.error("mifare_keys entries must be 12 hex digits")
            keys.append(key)
        settings["extra_mifare_keys"] = keys
        return settings

    # Lifecycle ------------------------------------------------------------

    def _handle_connect(self):
        # After a filament insert (AD5X IFS auto-load, Creator 5 head sensor)
        # Zmod runs RUN_ZCOLOR SLOT=<n> HIDE=1, a popup to pick the slot's
        # color and material. Wrap it so slots set from a tag skip it.
        if self.skip_zmod_prompt and "RUN_ZCOLOR" in self.gcode.ready_gcode_handlers:
            self._zmod_run_zcolor = self.gcode.register_command("RUN_ZCOLOR", None)
            self.gcode.register_command(
                "RUN_ZCOLOR", self.cmd_RUN_ZCOLOR,
                desc="Zmod slot color popup (skipped for slots set by zmod_nfc)")

    def cmd_RUN_ZCOLOR(self, gcmd):
        if gcmd.get_int("HIDE", 0) == 1:
            slot = gcmd.get_int("SLOT", 0)
            # The tag's prompt is still open: the inserted slot is its answer.
            if self.pending is not None and slot in self._slots()["slots"]:
                scan = self.pending
                self._close_prompt()
                self._assign(scan, slot, gcmd.respond_info)
                self._assigned.pop(slot, None)   # this was the popup to skip
                return
            assigned = self._assigned.pop(slot, None)
            if assigned is not None and self.reactor.monotonic() - assigned <= self.skip_window:
                gcmd.respond_info("NFC: slot %d was set from its tag; skipping the color selection" % slot)
                return
        self._zmod_run_zcolor(gcmd)

    def _handle_ready(self):
        self.zmod_color = self.printer.lookup_object("zmod_color", None)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="zmod_nfc reader")
        self._thread.daemon = True
        self._thread.start()
        self._timer = self.reactor.register_timer(self._tick, self.reactor.NEVER)

    def _handle_disconnect(self):
        # RESTART builds a new instance in the same process; this one's
        # thread must release the reader first.
        self._stop.set()
        self._rescan.set()
        self._kill_qr()
        if self._thread is not None:
            self._thread.join(3.)
            self._thread = None
        if self._timer is not None:
            self.reactor.unregister_timer(self._timer)
            self._timer = None

    # Reader thread --------------------------------------------------------

    def _post(self, callback, *args):
        self.reactor.register_async_callback(lambda eventtime: callback(*args))

    def _run(self):
        reader = None
        last_error = None
        while not self._stop.is_set():
            if reader is None:
                try:
                    reader = open_reader(
                        self.reader_type, self.device, self.baud, self.ccid_match,
                        lambda uid: decoders.mifare_keysets(uid, self.settings),
                        self.excluded_ports)
                except ReaderError as exc:
                    if str(exc) != last_error:
                        last_error = str(exc)
                        self._post(self._reader_changed, "searching", "", last_error)
                    self._stop.wait(self.retry_interval)
                    continue
                except Exception as exc:
                    LOGGER.exception("zmod_nfc: unexpected error opening the reader")
                    self._post(self._reader_changed, "error", "", str(exc))
                    self._stop.wait(self.retry_interval)
                    continue
                last_error = None
                self._post(self._reader_changed, "connected", reader.describe(), "")
            try:
                if self._rescan.is_set():
                    self._rescan.clear()
                    reader.forget()
                tag = reader.poll()
            except ReaderError as exc:
                LOGGER.warning("zmod_nfc: reader lost: %s", exc)
                reader.close()
                reader = None
                self._post(self._reader_changed, "searching", "", str(exc))
                continue
            except Exception:
                LOGGER.exception("zmod_nfc: reader poll failed")
                reader.close()
                reader = None
                self._stop.wait(self.retry_interval)
                continue
            if tag is not None and tag.incomplete:
                # Partial data would set a slot without weight or temperatures.
                self._post(self.gcode.respond_info,
                           "NFC: the tag left the reader too soon (%s). Hold the spool"
                           " on the reader until it beeps." % tag.describe())
            elif tag is not None:
                scan = self._process(tag)
                self._post(self._handle_scan, scan)
            self._rescan.wait(self.poll_interval)
        if reader is not None:
            reader.close()

    def _spoolman_available(self):
        methods = getattr(self.webhooks, "_remote_methods", None) or {}
        return self.use_spoolman and SPOOLMAN_REMOTE_METHOD in methods

    def _process(self, tag):
        """Decode a tag and find its spool (worker thread)."""
        filament, records = decoders.decode(tag, self.settings, self.disabled_formats)
        scan = Scan(tag, filament, records)
        LOGGER.info("zmod_nfc: read %s -> %s", tag.describe(),
                    filament.as_dict() if filament else "no filament data")
        if not self._spoolman_available():
            scan.spoolman_note = "" if not self.use_spoolman else "Spoolman not configured"
            return scan
        try:
            # A spool ID on the tag may come from another app (OpenSpool
            # phone apps write timestamps there), so a miss falls back to
            # the tag's UID.
            spool_id = filament.spool_id if filament is not None else None
            if spool_id and 0 < spool_id < MAX_SPOOLMAN_ID:
                try:
                    scan.spool = self.spoolman.get_spool(spool_id)
                except SpoolmanError as exc:
                    LOGGER.info("zmod_nfc: tag spool ID %s: %s", spool_id, exc)
            # QR codes have no UID to look up.
            if scan.spool is None and tag.uid:
                scan.spool = self.spoolman.find_spool_by_uid(tag.uid_hex)
            if scan.spool is None and self.match_filament and filament is not None:
                # Only a single match is taken; bind_card_uid then records
                # this tag on it, so the next scan finds it by UID.
                matches = self.spoolman.find_spools_by_filament(filament)
                if len(matches) == 1:
                    scan.spool = matches[0]
                elif matches:
                    scan.spoolman_note = "%d spools match (%s); pick it in Spoolman" % (
                        len(matches), ", ".join(str(s["id"]) for s in matches))
                    return scan
            if scan.spool is None and self.auto_create and filament is not None \
                    and filament.material:
                scan.spool = self.spoolman.create_spool(filament, tag.uid_hex)
                scan.spoolman_note = "added to Spoolman"
            elif scan.spool is None:
                scan.spoolman_note = "not in Spoolman"
        except SpoolmanError as exc:
            scan.spoolman_note = str(exc)
            LOGGER.warning("zmod_nfc: %s", exc)
        return scan

    # Reactor side ---------------------------------------------------------

    def _reader_changed(self, state, name, error):
        previous = self.reader_state
        self.reader_state = state
        self.reader_name = name or self.reader_name
        self.reader_error = error
        if state == "connected":
            self.gcode.respond_info("NFC: %s connected" % name)
        elif previous == "connected":
            self.gcode.respond_info("NFC: reader disconnected (%s)" % error)
        else:
            LOGGER.info("zmod_nfc: %s", error)

    def _printing(self):
        stats = self.printer.lookup_object("print_stats", None)
        if stats is None:
            return False
        state = stats.get_status(self.reactor.monotonic()).get("state")
        return state in ("printing", "paused")

    def _handle_scan(self, scan, run_gcode=None, slot=None, messages=None):
        """Show the prompt for a scan, or assign it to slot straight away
        when the reader names one (a reader fixed to a slot). run_gcode is
        given when called from a G-code command (NFC_TEST_SCAN), where
        run_script would deadlock. Returns what happened: "prompt",
        "assigned", "disabled", "printing", "no_data", "no_slot" or
        "error"; messages collects what a direct assignment reported."""
        self.last = scan
        if not self.enabled:
            return "disabled"
        if self._printing() and not self.scan_while_printing:
            self.gcode.respond_info("NFC: read %s (ignored while printing)" % scan.title())
            return "printing"
        if not scan.has_filament_data() and scan.spool is None:
            self.gcode.respond_info(
                "NFC: %s has no filament data%s. NFC_DUMP shows what was read."
                % (scan.source(), (" (Spoolman: %s)" % scan.spoolman_note) if scan.spoolman_note else ""))
            return "no_data"
        slots = self._slots()
        if slot is not None:
            return self._assign_direct(scan, slot, slots, run_gcode, messages)
        if len(slots["slots"]) <= 1:
            self._assign_outside_command(scan, 1, run_gcode)
            return "assigned"
        self.pending = scan
        self._slots_before = {n: s["loaded"] for n, s in slots["slots"].items()}
        self._show_prompt(scan, slots)
        return "prompt"

    # Zmod slots -----------------------------------------------------------

    def _slots(self):
        """{"toolchanger", "current", "valid_types", "palette", "presence",
        "slots": {n: {"material", "color", "loaded"}}} from zmod_color."""
        info = {"toolchanger": False, "current": 0, "valid_types": [], "palette": [],
                "presence": False, "slots": {}}
        if self.zmod_color is None:
            info["slots"] = {1: {"material": "", "color": "", "loaded": False}}
            return info
        status = self.zmod_color.get_status(self.reactor.monotonic())
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

    def _slot_label(self, slots, n):
        return ("T%d" % (n - 1)) if slots["toolchanger"] else ("Slot %d" % n)

    # Prompt ---------------------------------------------------------------

    def _prompt(self, line):
        self.gcode.respond_raw("// action:" + line.replace("\n", " "))

    def _show_prompt(self, scan, slots=None):
        slots = slots or self._slots()
        clean = lambda text: str(text).replace("|", "/")
        self._prompt("prompt_begin NFC: %s" % clean(scan.title()))
        details = []
        color = scan.color()
        if color:
            details.append("#" + color)
        if scan.filament is not None and scan.filament.nozzle_temp:
            details.append("%d C" % scan.filament.nozzle_temp)
        if scan.spool is not None:
            details.append("Spoolman spool %d" % scan.spool["id"])
        elif scan.spoolman_note:
            details.append(scan.spoolman_note)
        if scan.filament is not None:
            details.append(scan.filament.fmt + (" QR code" if scan.is_qr else " tag"))
        self._qr_prompt = False      # this prompt replaces the camera one
        self._prompt("prompt_text %s" % clean(" - ".join(details)))
        hint = "Choose the %s" % ("toolhead" if slots["toolchanger"] else "IFS slot")
        if self.assign_on_insert and slots["presence"]:
            hint += ", or insert the filament into an empty one"
        self._prompt("prompt_text %s" % hint)
        numbers = sorted(slots["slots"])
        for index, n in enumerate(numbers):
            if index % 4 == 0:
                self._prompt("prompt_button_group_start")
            slot = slots["slots"][n]
            label = self._slot_label(slots, n)
            if slot["material"] and slot["material"].upper() not in UNSET_MATERIALS:
                label += ": " + slot["material"]
            style = "primary" if not slot["loaded"] else "secondary"
            self._prompt("prompt_button %s|NFC_ASSIGN SLOT=%d|%s" % (clean(label), n, style))
            if index % 4 == 3 or index == len(numbers) - 1:
                self._prompt("prompt_button_group_end")
        if scan.spool is None and scan.filament is not None and scan.filament.material \
                and self._spoolman_available():
            self._prompt("prompt_footer_button Add to Spoolman|NFC_CREATE_SPOOL|info")
        self._prompt("prompt_footer_button Cancel|NFC_CANCEL|error")
        self._prompt("prompt_show")
        self.prompt_open = True
        self._prompt_deadline = (self.reactor.monotonic() + self.prompt_timeout
                                 if self.prompt_timeout else None)
        if self._timer is not None:
            self.reactor.update_timer(self._timer, self.reactor.monotonic() + INSERT_POLL)

    def _close_prompt(self):
        if self.prompt_open:
            self._prompt("prompt_end")
        self.prompt_open = False
        self.pending = None
        self._prompt_deadline = None
        if self._timer is not None:
            self.reactor.update_timer(self._timer, self.reactor.NEVER)

    def _tick(self, eventtime):
        """While a prompt is open: time it out, and assign the spool to a
        slot that filament was just inserted into."""
        if self.pending is None:
            return self.reactor.NEVER
        if self._prompt_deadline is not None and eventtime > self._prompt_deadline:
            self.gcode.respond_info("NFC: no slot chosen for %s" % self.pending.title())
            self._close_prompt()
            return self.reactor.NEVER
        if self.assign_on_insert:
            slots = self._slots()
            if slots["presence"]:
                for n, slot in sorted(slots["slots"].items()):
                    if slot["loaded"] and not self._slots_before.get(n, True):
                        scan = self.pending
                        self._close_prompt()
                        self._assign_outside_command(scan, n)
                        return self.reactor.NEVER
                self._slots_before = {n: s["loaded"] for n, s in slots["slots"].items()}
        return eventtime + INSERT_POLL

    # Assignment -----------------------------------------------------------

    def _assign_direct(self, scan, slot_number, slots, run_gcode=None, messages=None):
        """Assign without a prompt, for a reader fixed to one slot."""
        messages = messages if messages is not None else []

        def respond(message):
            messages.append(message)
            self.gcode.respond_info(message)

        if slot_number not in slots["slots"]:
            respond("NFC: %s is for %s %d, which this printer does not have"
                    % (scan.source(), "toolhead" if slots["toolchanger"] else "slot",
                       slot_number))
            return "no_slot"
        try:
            self._assign(scan, slot_number, respond, run_gcode or self.gcode.run_script)
        except Exception as exc:
            respond("NFC: %s" % exc)
            return "error"
        return "assigned"

    def _assign_outside_command(self, scan, slot_number, run_gcode=None):
        """Assign from a reactor callback rather than a G-code command."""
        try:
            self._assign(scan, slot_number, self.gcode.respond_info,
                         run_gcode or self.gcode.run_script)
        except Exception as exc:
            self.gcode.respond_info("NFC: %s" % exc)

    def _assign(self, scan, slot_number, respond, run_gcode=None):
        """Write a scanned spool into a slot. run_gcode is
        run_script_from_command inside G-code commands and run_script from
        reactor callbacks."""
        run_gcode = run_gcode or self.gcode.run_script_from_command
        slots = self._slots()
        if slot_number not in slots["slots"]:
            raise self.printer.command_error("NFC: there is no slot %d" % slot_number)
        label = self._slot_label(slots, slot_number)
        # Zmod's own color popup for this slot's next insert is skipped.
        self._assigned[slot_number] = self.reactor.monotonic()
        spool = scan.spool
        afclite = self.printer.lookup_object("zmod_afclite", None)
        if spool is not None and afclite is not None:
            # zmod_afclite tracks per-lane spools, writes the spool's color
            # and material into Zmod and keeps Moonraker's active spool on
            # the loaded lane.
            cmd = "SET_SPOOL_ID LANE=E%d SPOOL_ID=%d" % (slot_number - 1, spool["id"])
            if self.bind_card_uid and scan.uid and scan.uid not in spool_uids(spool):
                cmd += " CARD_UID=%s" % scan.uid
            run_gcode(cmd)
            respond("NFC: %s is now Spoolman spool %d (%s)" % (label, spool["id"], scan.title()))
            self._write_lane_data(scan, slot_number,
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
        self._write_lane_data(scan, slot_number, material, color, slots["palette"])
        if spool is not None:
            if self.printer.lookup_object("save_variables", None) is not None:
                run_gcode("SAVE_VARIABLE VARIABLE=%s VALUE=%d"
                                % (SPOOL_VARIABLE % slot_number, spool["id"]))
            if slots["current"] in (0, slot_number) or len(slots["slots"]) == 1:
                self._set_active_spool(spool["id"])
            if self.bind_card_uid and scan.uid and scan.uid not in spool_uids(spool):
                self._in_background(self.spoolman.bind_uid, spool, scan.uid)
            respond("NFC: %s is now %s (Spoolman spool %d)" % (label, scan.title(), spool["id"]))
        else:
            respond("NFC: %s is now %s #%s" % (label, material or "?", zmod_color))

    def _write_lane_data(self, scan, slot_number, material, color, palette=None):
        if self.lane_data is None:
            return
        info = {"material": material, "color": color, "name": scan.title(),
                "uid": scan.uid if scan.tag.uid else ""}
        if color and palette:
            # The palette color Zmod gets for this one (zmod_afclite snaps
            # the same way), so its lane_data mirror keeps the exact color.
            info["zmod_color"] = color if color in palette else nearest_palette_color(color, palette)
        if scan.spool is not None:
            filament = scan.spool.get("filament") or {}
            info["spool_id"] = scan.spool["id"]
            info["vendor"] = (filament.get("vendor") or {}).get("name")
            info["nozzle_temp"] = filament.get("settings_extruder_temp")
            info["bed_temp"] = filament.get("settings_bed_temp")
        if scan.filament is not None:
            info["vendor"] = info.get("vendor") or scan.filament.brand
            info["nozzle_temp"] = info.get("nozzle_temp") or scan.filament.nozzle_temp
            info["bed_temp"] = info.get("bed_temp") or scan.filament.bed_temp
        self.lane_data.write_later(slot_number, info)

    def _set_active_spool(self, spool_id):
        try:
            self.webhooks.call_remote_method(SPOOLMAN_REMOTE_METHOD, spool_id=spool_id)
        except Exception as exc:
            LOGGER.info("zmod_nfc: could not set the active spool: %s", exc)

    def _in_background(self, fn, *args):
        def run():
            try:
                fn(*args)
            except SpoolmanError as exc:
                LOGGER.warning("zmod_nfc: %s", exc)
        thread = threading.Thread(target=run, name="zmod_nfc spoolman")
        thread.daemon = True
        thread.start()

    def _wait_background(self, fn, *args):
        """Run a blocking Spoolman call on a thread while the reactor keeps
        running, and return its result (inside a G-code command)."""
        completion = self.reactor.completion()

        def run():
            try:
                result = (fn(*args), None)
            except Exception as exc:
                result = (None, exc)
            self.reactor.async_complete(completion, result)

        thread = threading.Thread(target=run, name="zmod_nfc spoolman")
        thread.daemon = True
        thread.start()
        result, error = completion.wait(self.reactor.monotonic() + 40.,
                                        (None, SpoolmanError("Spoolman did not answer")))
        if error is not None:
            raise self.printer.command_error("NFC: %s" % error)
        return result

    # G-code commands ------------------------------------------------------

    def cmd_NFC_ASSIGN(self, gcmd):
        scan = self.pending
        if scan is None:
            raise gcmd.error("NFC: no scanned tag is waiting for a slot; scan it again")
        slot = gcmd.get_int("SLOT", minval=1)
        self._close_prompt()
        self._assign(scan, slot, gcmd.respond_info)

    def cmd_NFC_CANCEL(self, gcmd):
        self._close_prompt()

    def cmd_NFC_PROMPT(self, gcmd):
        if self.last is None:
            raise gcmd.error("NFC: no tag has been scanned yet")
        self.pending = self.last
        slots = self._slots()
        self._slots_before = {n: s["loaded"] for n, s in slots["slots"].items()}
        self._show_prompt(self.last, slots)

    def cmd_NFC_CREATE_SPOOL(self, gcmd):
        scan = self.pending or self.last
        if scan is None or scan.filament is None or not scan.filament.material:
            raise gcmd.error("NFC: the last tag has no filament data to add")
        if scan.spool is not None:
            raise gcmd.error("NFC: the tag is already Spoolman spool %d" % scan.spool["id"])
        if not self._spoolman_available():
            raise gcmd.error("NFC: Spoolman is not configured in Moonraker")
        scan.spool = self._wait_background(self.spoolman.create_spool, scan.filament, scan.uid)
        scan.spoolman_note = ""
        gcmd.respond_info("NFC: added Spoolman spool %d (%s)" % (scan.spool["id"], scan.title()))
        if self.pending is scan:
            self._show_prompt(scan)

    def cmd_NFC_STATUS(self, gcmd):
        lines = ["NFC reader: %s%s" % (self.reader_name or self.reader_state,
                                       "" if self.enabled else " (disabled)")]
        if self.reader_state != "connected" and self.reader_error:
            lines.append("  %s" % self.reader_error)
        if self.last is not None:
            age = time.time() - self.last.time
            lines.append("Last scan (%s), %ds ago: %s" % (self.last.source(), age, self.last.title()))
        if self._qr_process is not None:
            lines.append("Looking for a QR label with the camera")
        if self.pending is not None:
            lines.append("Waiting for a slot for %s" % self.pending.title())
        lines.append("Formats: " + ", ".join(
            name for name in decoders.FORMAT_NAMES if name not in self.disabled_formats))
        keys = ["%s (%s)" % (k.split("_")[0].title(), "config" if k in self.settings else "built in")
                for k in ("bambu_key", "creality_key", "snapmaker_key")]
        lines.append("Vendor keys: " + ", ".join(keys))
        gcmd.respond_info("\n".join(lines))

    def cmd_NFC_DUMP(self, gcmd):
        scan = self.last
        if scan is None:
            raise gcmd.error("NFC: no tag has been scanned yet")
        lines = ["Tag: %s (%s)" % (scan.tag.describe(), scan.tag.reader)]
        if scan.tag.sector_keys:
            lines.append("Sectors opened: " + ", ".join(
                "%d=%s" % item for item in sorted(scan.tag.sector_keys.items())))
        if scan.is_qr:
            lines.append("QR text: %s" % scan.tag.memory.decode("utf-8", "replace")[:300])
        for record in [] if scan.is_qr else scan.records:
            if record.is_uri():
                lines.append("NDEF URI: %s" % record.uri())
            elif record.is_text() or record.mime in ("application/json", "text/plain"):
                lines.append("NDEF %s: %s" % (record.mime or "text", record.text()[:200]))
            else:
                lines.append("NDEF %r: %d bytes" % (record.mime or record.type, len(record.payload)))
        if scan.filament is not None:
            for key, value in sorted(scan.filament.as_dict().items()):
                lines.append("  %s: %s" % (key, value))
        else:
            lines.append("No known filament format")
        if scan.spool is not None:
            lines.append("Spoolman spool %d: %s" % (scan.spool["id"], spool_title(scan.spool)))
        elif scan.spoolman_note:
            lines.append("Spoolman: %s" % scan.spoolman_note)
        if gcmd.get_int("RAW", 0):
            memory = scan.tag.memory or scan.tag.ndef or b""
            for offset in range(0, len(memory), 16):
                lines.append("%04X: %s" % (offset, memory[offset:offset + 16].hex(" ")))
        gcmd.respond_info("\n".join(lines))

    def cmd_NFC_RESCAN(self, gcmd):
        self._rescan.set()
        gcmd.respond_info("NFC: reading the tag on the reader again")

    def cmd_NFC_TEST_SCAN(self, gcmd):
        """Run a made-up tag through decoding, Spoolman lookup and the prompt,
        to try the plugin before a reader is attached."""
        uid_text = gcmd.get("UID", "04A1B2C3D4E5F6")
        uid = bytes.fromhex(normalize_uid(uid_text) or "04A1B2C3D4E5F6")
        text = gcmd.get("TEXT", "")
        spool = gcmd.get_int("SPOOL", 0)
        if spool:
            text = "SPOOL:%d" % spool
        message = text_message(text) if text else b""
        tag = Tag(uid, TECH_ISO14443A, KIND_TYPE2, type2_image(message, uid),
                  sak=0x00, reader="NFC_TEST_SCAN")
        scan = self._wait_background(self._process, tag)
        self._handle_scan(scan, self.gcode.run_script_from_command)

    # Camera QR labels ----------------------------------------------------

    def cmd_NFC_QR_SCAN(self, gcmd):
        if self._qr_process is not None:
            raise gcmd.error("NFC: already looking for a QR label")
        if self._printing() and not self.scan_while_printing:
            raise gcmd.error("NFC: QR scanning is off while printing (scan_while_printing)")
        seconds = gcmd.get_float("TIME", self.qr_scan_time, minval=1., maxval=120.)
        command = [sys.executable, "-m", "zmodnfc.camera", "--url", self.qr_url,
                   "--time", "%g" % seconds, "--width", str(self.qr_width), "--nice", "19",
                   "--verbose", "--save-dir", QR_FRAMES_DIR,
                   "--decoder", self.qr_decoder, "--chroot", self.qr_chroot]
        if self.qr_ffmpeg:
            command += ["--ffmpeg", self.qr_ffmpeg]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in (PLUGIN_DIR, env.get("PYTHONPATH")) if p)
        try:
            process = subprocess.Popen(command, cwd=PLUGIN_DIR, env=env,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
        except OSError as exc:
            raise gcmd.error("NFC: could not start the camera scan: %s" % exc)
        self._qr_process = process
        self._prompt("prompt_begin NFC: QR label")
        self._prompt("prompt_text Hold the spool's QR label up to the camera (%d s)" % seconds)
        self._prompt("prompt_footer_button Cancel|NFC_QR_CANCEL|error")
        self._prompt("prompt_show")
        self._qr_prompt = True
        gcmd.respond_info("NFC: looking for a QR label for %d s" % seconds)
        thread = threading.Thread(target=self._qr_wait, args=(process, seconds),
                                  name="zmod_nfc camera")
        thread.daemon = True
        thread.start()

    def _qr_wait(self, process, seconds):
        """Wait for the camera process, then decode and look up its text
        like a tag (worker thread)."""
        try:
            out, err = process.communicate(timeout=seconds + 60.)
        except subprocess.TimeoutExpired:
            process.kill()
            out, err = process.communicate()
        result = {}
        for line in reversed(out.decode("utf-8", "replace").splitlines()):
            try:
                result = json.loads(line)
                break
            except ValueError:
                continue
        for line in err.decode("utf-8", "replace").splitlines():
            if line.startswith(("frame ", "decoder: ")):
                LOGGER.info("zmod_nfc: camera %s", line)
        if not result:
            message = err.decode("utf-8", "replace").strip().splitlines()
            LOGGER.warning("zmod_nfc: camera scan failed: %s", err.decode("utf-8", "replace"))
            result = {"error": "camera scan failed%s" % (": " + message[-1] if message else "")}
        scan = None
        if "text" in result:
            tag = Tag(b"", TECH_CAMERA, KIND_QR, result["text"].encode("utf-8"), reader="camera")
            scan = self._process(tag)
        self._post(self._qr_done, process, scan, result.get("error"))

    def _qr_done(self, process, scan, error):
        if process is not self._qr_process:
            return                   # cancelled
        self._qr_process = None
        if self._qr_prompt:
            self._prompt("prompt_end")
            self._qr_prompt = False
        if scan is not None:
            self._handle_scan(scan)
            return
        self.gcode.respond_info("NFC: %s" % error)
        # Say so on the screen too, where the scan was started.
        self._prompt("prompt_begin NFC: QR label")
        self._prompt("prompt_text %s" % (error[:1].upper() + error[1:]).replace("|", "/"))
        self._prompt("prompt_text Hold the label still, close to the camera and well lit")
        self._prompt("prompt_footer_button Try again|NFC_QR_SCAN|primary")
        self._prompt("prompt_footer_button Close|NFC_QR_CANCEL|secondary")
        self._prompt("prompt_show")
        self._qr_prompt = True

    def _kill_qr(self):
        process, self._qr_process = self._qr_process, None
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass

    def cmd_NFC_QR_CANCEL(self, gcmd):
        if self._qr_process is None:
            if self._qr_prompt:          # the "nothing found" prompt
                self._prompt("prompt_end")
                self._qr_prompt = False
            else:
                gcmd.respond_info("NFC: not looking for a QR label")
            return
        self._kill_qr()
        if self._qr_prompt:
            self._prompt("prompt_end")
            self._qr_prompt = False
        gcmd.respond_info("NFC: stopped looking for a QR label")

    def cmd_NFC_ENABLE(self, gcmd):
        self.enabled = True
        gcmd.respond_info("NFC: scanning enabled")

    def cmd_NFC_DISABLE(self, gcmd):
        self.enabled = False
        self._close_prompt()
        gcmd.respond_info("NFC: scanning disabled")

    # Network readers ----------------------------------------------------
    #
    # Klipper webhooks endpoints, which Moonraker publishes as
    # http://<printer>:7125/printer/zmod_nfc/<name>. Handlers run on the reactor and
    # must answer at once; an exception other than command_error would shut
    # Klipper down.

    def _endpoint(self, name):
        handler = getattr(self, "_ep_" + name)

        def run(web_request):
            try:
                result = handler(web_request)
            except self.printer.command_error:
                raise
            except Exception as exc:
                LOGGER.exception("zmod_nfc: zmod_nfc/%s failed", name)
                raise self.printer.command_error("NFC: %s" % exc)
            web_request.send(result)
        return run

    def _slots_summary(self):
        slots = self._slots()
        return {
            "toolchanger": slots["toolchanger"],
            "current": slots["current"],
            "slots": [dict(slots["slots"][n], slot=n, label=self._slot_label(slots, n))
                      for n in sorted(slots["slots"])],
        }

    def _ep_info(self, web_request):
        data = {"plugin": "zmod_nfc", "api": API_VERSION, "enabled": self.enabled,
                "printing": self._printing(), "remote": self.remote_result,
                "pending": self.pending.summary() if self.pending is not None else None}
        data.update(self._slots_summary())
        return data

    def _ep_scan(self, web_request):
        """A tag read by a network reader. Decoding and the Spoolman lookup
        run on a thread, like a USB reader's; the outcome appears as
        printer.zmod_nfc.remote under the returned id."""
        try:
            tag = tag_from_json(web_request.params)
        except RemoteError as exc:
            raise self.printer.command_error("NFC: %s" % exc)
        # A reader fixed to a slot names it; the scan then skips the prompt.
        slot = web_request.params.get("slot") or None
        if slot is not None and (not isinstance(slot, int) or slot < 1):
            raise self.printer.command_error("NFC: slot must be a slot number")
        if tag.incomplete:
            return {"accepted": False, "outcome": "incomplete"}
        scan_id = os.urandom(4).hex()
        LOGGER.info("zmod_nfc: tag %s from %s (%s)", tag.uid_hex, tag.reader, scan_id)

        def run():
            try:
                scan = self._process(tag)
            except Exception as exc:
                LOGGER.exception("zmod_nfc: network scan failed")
                self._post(self._remote_failed, scan_id, str(exc))
                return
            self._post(self._remote_done, scan_id, scan, slot)
        thread = threading.Thread(target=run, name="zmod_nfc network scan")
        thread.daemon = True
        thread.start()
        return {"accepted": True, "id": scan_id}

    def _remote_done(self, scan_id, scan, slot=None):
        messages = []
        outcome = self._handle_scan(scan, slot=slot, messages=messages)
        self.remote_result = {"id": scan_id, "outcome": outcome, "scan": scan.summary(),
                              "spoolman": scan.spoolman_note, "slot": slot,
                              "message": " ".join(messages)}

    def _remote_failed(self, scan_id, error):
        self.remote_result = {"id": scan_id, "outcome": "error", "error": error}

    def _ep_assign(self, web_request):
        slot = web_request.get_int("slot")
        scan = self.pending
        if scan is None:
            raise self.printer.command_error("NFC: no scanned tag is waiting for a slot")
        messages = []
        self._close_prompt()
        self._assign(scan, slot, messages.append, self.gcode.run_script)
        for message in messages:
            self.gcode.respond_info(message)
        return {"message": " ".join(messages), "scan": scan.summary()}

    def _ep_cancel(self, web_request):
        self._close_prompt()
        return {}

    def get_status(self, eventtime):
        return {
            "enabled": self.enabled,
            "remote": self.remote_result,
            "reader": self.reader_name,
            "state": self.reader_state,
            "error": self.reader_error,
            "pending": self.pending is not None,
            "qr_scanning": self._qr_process is not None,
            "last": self.last.summary() if self.last is not None else None,
        }


def load_config(config):
    return ZmodNfc(config)
