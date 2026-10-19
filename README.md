# zmod_nfc

NFC/RFID filament tags for **Zmod** on the **Flashforge AD5X** (four-slot IFS)
and the **Flashforge Creator 5** (four toolheads).

Hold a tagged spool to a USB reader plugged into the printer. A prompt asks
which IFS slot or toolhead the spool goes in. You pick one, or simply insert
the filament into an empty slot, and the slot gets the spool's material and
color. If Spoolman is set up, the slot also gets the Spoolman spool.

No reader? Spoolman's printed QR labels can be read with the printer's own
camera instead, on both printers (see [Camera QR labels](#camera-qr-labels)).

```
scan tag ──► decode (Bambu, Creality, OpenSpool, OpenPrintTag, ...)
         ──► look up in Spoolman (by spool ID on the tag or by tag UID)
         ──► prompt: [Slot 1] [Slot 2] [Slot 3] [Slot 4]  or insert filament
         ──► Zmod slot color + material  (+ Spoolman spool via zmod_afclite)
```

> **Status: early release.** Tested end to end on an **AD5X** and a
> **Creator 5** with an **ACR122U** reader: scan, prompt, slot assignment and
> filament insert. The decoders, the Klipper integration and the USB plumbing
> also have unit tests, which pass under Klipper's Python 3.8. The PN532,
> ACR1552U and PN5180 drivers have **not yet been run against physical
> readers**. Reports from owners of those readers are very welcome (see
> [Reporting a tag](#reporting-a-tag)).

## Readers

| Reader | Connection | Tags | Notes |
|--------|------------|------|-------|
| **ACR1552U** | USB (CCID) | ISO 14443A + ISO 15693 | Plug in. Driven directly over usbfs; no pcscd needed. |
| **ACR122U** | USB (CCID) | ISO 14443A | Tested. MIFARE Classic is read through its internal PN532. |
| Other PC/SC readers: ACR1252U, ACR1255U, Identiv, ... | USB (CCID) | depends on the reader | Same driver: any reader with the PC/SC Part 3 storage-card commands. |
| **PN532** | USB-serial adapter (CH340, CP210x, PL2303, FTDI) | ISO 14443A | Set the board to **HSU/UART** mode (both switches off on the common red and blue boards). Cannot read ISO 15693 (OpenPrintTag). |
| **PN5180** | USB through a bridge MCU | ISO 14443A + ISO 15693 | The PN5180 has no USB of its own. The driver is written; the bridge firmware is planned. See [docs/pn5180-bridge.md](docs/pn5180-bridge.md). |

`reader: auto` (the default) uses the first CCID reader it finds, then probes
USB-serial adapters for a PN532 or a PN5180 bridge. It never probes serial
ports that a Klipper MCU uses, or ports from unknown USB vendors.

Everything uses the Python standard library: no pyserial, libusb, pcscd or
pyscard. Zmod's Python ships none of them, and building them for the AD5X's
MIPS CPU is impractical.

## Tag formats

| Format | Tag | Readers | Notes |
|--------|-----|---------|-------|
| [OpenPrintTag](https://specs.openprinttag.org) (Prusa) | ICODE SLIX2 (ISO 15693) | ACR1552U, PN5180 | CBOR. Remaining weight from the aux region. |
| [OpenSpool](https://openspool.io) | NTAG21x | all | JSON NDEF record. |
| [OpenTag3D](https://opentag3d.info) | NTAG21x / ISO 15693 | all | Spec 2.x, and the older 0.012 layout. |
| [TigerTag](https://tigertag.io) | NTAG21x | all | Offline ID database. |
| Bambu Lab | MIFARE Classic 1K | ACR1552U, PN532, PN5180 | Works out of the box. |
| Creality (CFS) | MIFARE Classic 1K | ACR1552U, PN532, PN5180 | Works out of the box, encrypted tags included. |
| Snapmaker (U1) | MIFARE Classic 1K | ACR1552U, PN532, PN5180 | Works out of the box: the key derivation is [published](https://github.com/DnG-Crafts/U1-RFID/blob/main/docs/U1%20Tag%20Protocol.md). The tag's RSA signature is not checked, so re-written tags read too. |
| Anycubic (ACE) | NTAG21x | all | |
| Elegoo | NTAG21x | all | |
| Qidi | MIFARE Classic 1K | ACR1552U, PN532, PN5180 | |
| [SpoolEase](https://spoolease.io) | NTAG21x | all | URL record. |
| Spoolman references | any NDEF tag | all | `web+spoolman:s-<id>` / `.../spool/show/<id>` links, and [nfc2klipper](https://github.com/bofh69/nfc2klipper) `SPOOL:<id>` text. |
| Plain text | any NDEF tag | all | `PLA:FF0000`, `PETG #00FF00`, `PLA Silk;123456;Brand`. |
| NDEF on MIFARE Classic, Type 4 (DESFire) | | ACR1552U, PN532 | All of the NDEF formats above. |
| Any other tag | | all | Its UID is still matched against Spoolman spools. |

**Vendor keys:** the Bambu, Creality and Snapmaker keys are built in, from
their public write-ups (the
[Bambu Research Group RFID guide](https://github.com/Bambu-Research-Group/RFID-Tag-Guide),
the Creality CFS RFID research and the
[DnG-Crafts U1 Tag Protocol](https://github.com/DnG-Crafts/U1-RFID/blob/main/docs/U1%20Tag%20Protocol.md)),
so these tags read without any setup. The `bambu_key`, `creality_key`,
`creality_encryption_key` and `snapmaker_key` options can override them; an
override is checked against the key's known SHA-256 at startup.

## Install

1. Add the section in
   [`zmod_nfc.moonraker.conf.example`](zmod_nfc.moonraker.conf.example) to
   `mod_data/user.moonraker.conf`, then restart Moonraker. Moonraker's update
   manager now keeps the plugin up to date.
2. Run:

   ```gcode
   ENABLE_PLUGIN name=zmod_nfc
   ```

Zmod clones the repository into `mod_data/plugins/zmod_nfc` and runs
`install.sh`, which links `zmod_nfc.py` into Klipper's extras directory
(native Klipper and Klipper 13). Zmod then restarts Klipper. The plugin works
with its defaults.

To remove it, run `DISABLE_PLUGIN name=zmod_nfc`.

## Configuration

Zmod includes [`zmod_nfc.cfg`](zmod_nfc.cfg), which lists every option.
Plugin updates replace that file, so put your settings in `mod_data/user.cfg`:

```ini
[zmod_nfc]
prompt_timeout: 90
spoolman_auto_create: True
```

Klipper merges both `[zmod_nfc]` sections, and yours wins.

## Using it

A Snapmaker spool scanned for the first time, shown on HelixScreen. It is not
in Spoolman yet, so the prompt offers **Add to Spoolman**:

![NFC prompt for a Snapmaker PLA SnapSpeed tag, with slot buttons and an Add to Spoolman button](docs/images/prompt-new-spool.png)

The same spool after adding it, now matched to Spoolman spool 15:

![NFC prompt for the same tag showing Spoolman spool 15 and the slot buttons](docs/images/prompt-spoolman-spool.png)

1. Hold a tag to the reader. A prompt appears in Mainsail, Fluidd and
   HelixScreen, showing the filament and one button per IFS slot (or `T0`-`T3`
   on the Creator 5). Empty slots are highlighted.
2. Tap a slot, **or insert the filament into an empty slot** within the
   prompt timeout. Inserting also works on screens that cannot show prompts,
   such as the stock Flashforge screen.
3. The slot gets the material and color. On the Creator 5, the color is
   snapped to the closest color in its palette.

Zmod normally asks for the color and material after you insert filament
(after the AD5X IFS loads it, or when a Creator 5 head sensor sees it). For a
slot set from a tag, zmod_nfc skips that popup. If the tag's prompt is still
open when the popup would appear, zmod_nfc assigns the spool to that slot
instead. Set `skip_zmod_color_prompt: False` to keep Zmod's popup.

Tags are ignored while printing unless `scan_while_printing: True`.

### With Spoolman

When Moonraker has a `[spoolman]` section:

* A tag that names a spool (a Spoolman link, nfc2klipper `SPOOL:`, OpenSpool
  `spool_id`) selects that spool.
* Otherwise the tag's UID is looked up in the spools' `card_uids` field (the
  same field [zmod_afclite](https://github.com/DIEHARDave/zmod_afclite) uses)
  or nfc2klipper's `nfc_id` field.
* An unknown tag that carries filament data can be added with the prompt's
  **Add to Spoolman** button, or automatically with
  `spoolman_auto_create: True`.
* With `bind_card_uid: True` (the default), assigning a spool records the
  tag's UID on it, so the same tag is recognised next time even if it has no
  data of its own.

If **zmod_afclite** is installed, assigning a Spoolman spool goes through its
`SET_SPOOL_ID`. Each IFS lane then tracks its own spool, and Moonraker's
active spool follows whichever lane is loaded. Without zmod_afclite, zmod_nfc
writes the color and material itself, saves the spool ID per slot in
`save_variables`, and sets the active spool when the slot is the one loaded.

Two Snapmaker spools assigned from their tags to IFS lanes E2 and E3, in
zmod_afclite's AFC panel in Mainsail. Hovering over a lane's spool shows its
Spoolman details:

![AFC panel with four IFS lanes; lanes E2 and E3 hold Snapmaker PLA SnapSpeed spools, with a tooltip showing Spoolman spool 14](docs/images/afclite-lanes.png)

Without Spoolman, only the tag's material and color are used.

### Camera QR labels

Spools with a QR label can be scanned with the printer camera instead of an
NFC reader. Run `NFC_QR_SCAN`, or tap the **SCAN_QR_LABEL** macro in
Mainsail, Fluidd or HelixScreen, then hold the label up to the camera or lay
it on the bed. The usual slot prompt follows as soon as the code is read.

It reads:

* **Spoolman labels**: `web+spoolman:s-<id>` (Spoolman's default QR code)
  and links to `.../spool/show/<id>`. The spool is selected in Spoolman.
* **OpenSpool JSON** and the plain-text formats above (`PLA:FF0000`, ...).

The scan looks for up to 20 seconds (`qr_scan_time`, or
`NFC_QR_SCAN TIME=<s>`). If it finds nothing, the prompt says so and offers
**Try again**. Codes up to version 10 (271 bytes) are read. Scanning is off
while printing unless `scan_while_printing: True`.

| Printer | Camera | Time per frame |
|---------|--------|----------------|
| AD5X | 2048x1536, above and in front of the bed | about 2 s |
| Creator 5 | 1280x720, low, looking across the bed | about 2.3 s |

The code's modules (its small squares) need about 2 to 3 camera pixels each,
so print labels large enough and bring them close. A label held up facing
the camera reads best. Lying flat on the bed works too, even when the camera
sees it squashed to a third of its height, but it then needs to be larger.

> **Status: new.** Tested with a printed Spoolman label on an AD5X (assigned
> to an IFS slot) and on a Creator 5 (assigned to a toolhead), both through
> zmod_afclite; each read the label within about 5 seconds. The QR reader
> also has tests against reference codes and camera-like images. Reports
> from other setups are welcome.

How it works: the plugin starts a low-priority child process that fetches
snapshots from Zmod's camera streamer (`http://127.0.0.1:8080/?action=snapshot`
on both printers), turns each into a grayscale frame with libturbojpeg in
Zmod's chroot (or with ffmpeg where its build can; the Creator 5's cannot),
and decodes it with a QR reader written in pure Python (`zmodnfc/qr.py`).
Klipper itself does none of the image work. The frames of the last scan are
kept in `/tmp/zmod_nfc_qr/`, and the per-frame timings go to the Klipper
log. The camera URL, decoder and frame size are configurable; see the `qr_`
options in [`zmod_nfc.cfg`](zmod_nfc.cfg). To try a saved snapshot:

```sh
python3 -m zmodnfc.camera --image label.jpg --verbose   # from the plugin directory
```

## Commands

| Command | |
|---------|--|
| `NFC_STATUS` | Reader state, the last tag, and enabled formats and keys. |
| `NFC_ASSIGN SLOT=<n>` | Assign the scanned spool to a slot (the prompt's buttons run this). |
| `NFC_CANCEL` | Close the prompt. |
| `NFC_PROMPT` | Show the prompt for the last tag again. |
| `NFC_CREATE_SPOOL` | Add the scanned filament to Spoolman. |
| `NFC_DUMP [RAW=1]` | Everything read from the last tag; `RAW=1` adds a memory dump. |
| `NFC_RESCAN` | Read the tag on the reader again. |
| `NFC_TEST_SCAN TEXT=PLA:FF0000` / `SPOOL=<id>` / `UID=<hex>` | Simulate a scan, to try the prompt and Spoolman flow without a reader. |
| `NFC_QR_SCAN [TIME=<s>]` | Read a QR label with the printer camera (the `SCAN_QR_LABEL` macro runs this). |
| `NFC_QR_CANCEL` | Stop looking for a QR label. |
| `NFC_ENABLE` / `NFC_DISABLE` | Resume or ignore scans. |

`printer.zmod_nfc` reports `state`, `reader`, `pending`, `qr_scanning` and
the `last` tag (UID, format, material, color, spool ID) for macros and other
clients.

## Testing a reader without Klipper

[`tools/nfc_scan.py`](tools/nfc_scan.py) reads tags and prints what it finds.
Stop the plugin from holding the reader first, for example by running this
before `ENABLE_PLUGIN`:

```sh
# AD5X, over SSH
chroot /usr/data/.mod/.zmod python3 /opt/config/mod_data/plugins/zmod_nfc/tools/nfc_scan.py --list
chroot /usr/data/.mod/.zmod python3 /opt/config/mod_data/plugins/zmod_nfc/tools/nfc_scan.py --raw
```

## Reporting a tag

If a tag isn't recognised, scan it and run `NFC_DUMP RAW=1` (or
`tools/nfc_scan.py --raw --json`). Then open an issue with the output and
what the spool is. Remove anything private first.

## Development

```sh
cd tests
python3 -m unittest test_decoders test_readers test_plugin test_qr
```

The reader tests need Linux (`fcntl`, `termios`). Keep the code compatible
with Python 3.8, which is what Klipper runs under on the AD5X.

## Credits

* [OpenRFID](https://github.com/suchmememanyskill/OpenRFID) (GPL-3.0). The
  Bambu, Creality, Snapmaker, Qidi, Anycubic, Elegoo, TigerTag, OpenSpool,
  OpenTag3D and SpoolEase decoders are adapted from it, as is the bundled
  TigerTag ID database. OpenRFID builds on the
  [Bambu Research Group RFID guide](https://github.com/Bambu-Research-Group/RFID-Tag-Guide)
  and [DnG-Crafts ACE-RFID](https://github.com/DnG-Crafts/ACE-RFID).
* Snapmaker U1 tag research by
  [DnG-Crafts](https://github.com/DnG-Crafts/U1-RFID),
  [SnapmakerResearchGroup](https://github.com/SnapmakerResearchGroup/RFID) and the
  [Proxmark3](https://github.com/RfidResearchGroup/proxmark3) key generator.
* [OpenPrintTag](https://github.com/prusa3d/OpenPrintTag) by Prusa Research.
  The test vectors in `tests/vectors/openprinttag` are MIT licensed; see the
  LICENSE file there.
* [nfc2klipper](https://github.com/bofh69/nfc2klipper) and
  [klipper-nfc-daemon](https://github.com/DIEHARDave/klipper-nfc-daemon) for
  the Spoolman conventions and the scan-then-pick-a-slot flow.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
