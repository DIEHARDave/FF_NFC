"""Moonraker lane_data: what OrcaSlicer (and HelixScreen) read per slot.

zmod_afclite mirrors each slot's color and material into lane_data, but
not which spool is in it. After a tag assignment zmod_nfc adds the spool's
details to the slot's entry - Spoolman spool ID, vendor, temperatures -
keeping every other key, so afclite's and other writers' fields survive.

Entries are found by their "lane" field ("0" for the first slot), because
the key differs between printers (AD5X: lane1..lane4, Creator 5: T0..T3)."""

import datetime
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request

LOGGER = logging.getLogger("zmod_nfc")

NAMESPACE = "lane_data"
TIMEOUT = 10.0
# Written at once, for HelixScreen, which re-reads lane_data 2 s after the
# slot's firmware color changes, and again a little after the assignment,
# once zmod_afclite has mirrored the slot's new color.
DELAY = 5.0


def entry_key(entries, slot):
    """Key of the lane_data entry for a 1-based slot."""
    lane = str(slot - 1)
    for key, value in sorted(entries.items()):
        if isinstance(value, dict) and str(value.get("lane")) == lane:
            return key
    return "lane%d" % slot


# Keys other writers (HelixScreen) keep about the spool in a slot. They
# describe the previous spool once a different one is assigned; HelixScreen
# prefers its own (spool_name, helix_material) over the shared ones.
# HelixScreen takes an entry holding "vendor", "spool_name" or a helix_ key
# for its own and ignores it when it re-reads lane_data, so this module
# writes only the shared spellings (vendor_name, name).
SPOOL_KEYS = ("name", "spool_name", "vendor", "color_name", "spoolman_vendor_id",
              "scan_time", "remaining_weight_g", "total_weight_g")


def same_spool(existing, info):
    if existing.get("spool_id") and info.get("spool_id"):
        return existing["spool_id"] == info["spool_id"]
    return bool(info.get("uid")) and existing.get("zmod_nfc_uid") == info["uid"]


def lane_value(existing, slot, info):
    """The new entry: existing keys kept, the spool's details set. info has
    material, color (RRGGBB), and optionally spool_id, vendor, name,
    nozzle_temp, bed_temp, uid. For a different spool than before, other
    writers' details about the old one are dropped."""
    value = dict(existing) if isinstance(existing, dict) else {}
    if not same_spool(value, info):
        for key in list(value):
            if key in SPOOL_KEYS or key.startswith("helix_"):
                del value[key]
        if info.get("name"):
            value["name"] = info["name"]
    elif "spool_name" not in value and not any(k.startswith("helix_") for k in value):
        value.pop("vendor", None)     # written by older versions of this module
    value["lane"] = str(slot - 1)
    if info.get("material"):
        value["material"] = info["material"]
    if info.get("color"):
        value["color"] = "#" + info["color"].lstrip("#").upper()
    if info.get("spool_id"):
        value["spool_id"] = info["spool_id"]
    else:
        value.pop("spool_id", None)       # the previous spool's
    for key in ("nozzle_temp", "bed_temp"):
        if info.get(key):
            value[key] = info[key]
        else:
            value.pop(key, None)
    if info.get("vendor"):
        value["vendor_name"] = info["vendor"]
    else:
        value.pop("vendor_name", None)
    value["zmod_nfc_name"] = info.get("name") or ""
    value["zmod_nfc_uid"] = info.get("uid") or ""
    if info.get("zmod_color"):
        # zmod_afclite's note of the Zmod color it mirrored: with Zmod's new
        # color already noted, it keeps this entry's exact color.
        value["zmod_afclite_color"] = "#" + info["zmod_color"].lstrip("#").upper()
    if info.get("scan_time"):
        # When the spool was set, so HelixScreen can order this record
        # against an edit made on its screen.
        value["scan_time"] = info["scan_time"]
    return value


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LaneData(object):
    def __init__(self, moonraker_url):
        self.url = moonraker_url.rstrip("/") + "/server/database/item"

    def _request(self, method, query=None, body=None):
        url = self.url + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response)

    def entries(self):
        try:
            reply = self._request("GET", {"namespace": NAMESPACE})
        except urllib.error.HTTPError as exc:
            if exc.code == 404:            # the namespace does not exist yet
                return {}
            raise
        return (reply.get("result") or {}).get("value") or {}

    def write(self, slot, info):
        entries = self.entries()
        key = entry_key(entries, slot)
        value = lane_value(entries.get(key), slot, info)
        if value != entries.get(key):
            self._request("POST", body={"namespace": NAMESPACE, "key": key, "value": value})
        return key

    def write_later(self, slot, info, delay=DELAY):
        """write() at once and again after delay, on timer threads; failures
        are only logged."""
        info = dict(info, scan_time=info.get("scan_time") or utc_now())

        def run():
            try:
                key = self.write(slot, info)
                LOGGER.info("zmod_nfc: lane_data %s set for slot %d", key, slot)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                LOGGER.warning("zmod_nfc: could not write lane_data for slot %d: %s", slot, exc)
        timers = [threading.Timer(0, run), threading.Timer(delay, run)]
        for timer in timers:
            timer.daemon = True
            timer.start()
        return timers
