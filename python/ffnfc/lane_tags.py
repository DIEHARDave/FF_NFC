"""The Klipper object `lane_tags ff_nfc` (lane-status-spec, SPEC.md section
2.4): what FF_NFC's reader sees, for a UI that shows tag readers beside the
lanes. FF_NFC is a tag reader, not the producer of the lanes, so it
publishes the spec's `tags` object in an object of its own.

    {"spec_version": "0.1", "version": "<FF_NFC>",
     "tags": {"readers": {"ff_nfc": {"type", "alive", "lanes", "port"}},
              "lanes": {},
              "pending": {"uid", "spool"} or None},
     "assign": "NFC_ASSIGN", "cancel": "NFC_CANCEL"}

FF_NFC has one shared reader that tags are held up to, so it serves no
lane (`lanes` []) and `tags.lanes` stays empty: the tag of a spool that
went to a lane is that lane's record's business (section 6). `pending` is
a scan still waiting for a lane: the tag's UID (None for a QR label) and
its Spoolman spool ({"tracker": "spoolman", "id": "<id>"}, section 6) or
None. `assign` takes LANE= (the producer's lane name) and `cancel` dismisses
the scan. Every key is there from the first status (section 2.2)."""

from . import __version__

OBJECT_NAME = "lane_tags ff_nfc"
SPEC_VERSION = "0.1"
READER_NAME = "ff_nfc"


def reader_port(reader):
    """Where the reader is attached (a serial or USB path, a bridge), or None."""
    if reader is None:
        return None
    path = getattr(reader, "path", None)
    if path:
        return str(path)
    transport = getattr(reader, "transport", None)
    path = getattr(transport, "path", None)
    if path:
        return str(path)
    if transport is not None:
        return "bridge"
    device = getattr(reader, "device", None)
    ident = getattr(device, "ident", None)
    return str(ident) if ident else None


def reader_details(reader):
    """{"type", "port"} of an open reader."""
    return {"type": getattr(reader, "chip_type", None), "port": reader_port(reader)}


def pending_status(scan):
    if scan is None:
        return None
    spool = getattr(scan, "spool", None)
    spool_id = spool.get("id") if isinstance(spool, dict) else None
    uid = getattr(scan, "uid", None) if not scan.is_qr else None
    return {"uid": uid or None,
            "spool": {"tracker": "spoolman", "id": str(spool_id)} if spool_id else None}


class LaneTags(object):
    def __init__(self, core):
        self.core = core

    def get_status(self, eventtime):
        core = self.core
        details = core.reader_details or {}
        reader = {
            "type": details.get("type"),
            "alive": core.reader_state == "connected",
            "lanes": [],
            "port": details.get("port"),
        }
        # New dicts on every call: Klipper sends a field when it differs
        # from the one it sent last.
        return {
            "spec_version": SPEC_VERSION,
            "version": __version__,
            "tags": {"readers": {READER_NAME: reader},
                     "lanes": {},
                     "pending": pending_status(core.pending)},
            "assign": "NFC_ASSIGN",
            "cancel": "NFC_CANCEL",
        }
