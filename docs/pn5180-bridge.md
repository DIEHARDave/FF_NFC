# PN5180 USB bridge protocol (v1)

The PN5180 has no USB interface: it only has SPI plus the `BUSY` and `RST`
lines. To use it on a printer, a small microcontroller bridges it to USB
and appears as a USB serial port (CDC-ACM, or a USB-UART chip). zmod_nfc
runs the whole PN5180 command layer on the printer
([`zmodnfc/readers/pn5180.py`](../zmodnfc/readers/pn5180.py)), so the bridge
firmware only moves SPI frames.

**Status:** the host side is written and unit-tested. No bridge firmware
has been written yet, and no hardware has been chosen for it. Any board that
can do SPI and USB serial will do (RP2040, ESP32-S3, STM32, ATmega32U4 with
level shifting).

## Wiring

| PN5180 | Bridge MCU |
|--------|------------|
| `NSS`  | any GPIO (chip select, active low) |
| `MOSI`, `MISO`, `SCK` | SPI, mode 0, up to 7 MHz (1-2 MHz is plenty) |
| `BUSY` | any GPIO input |
| `RST`  | any GPIO output (active low) |
| `5V`   | 5 V (RF stage) |
| `3V3`  | 3.3 V (logic) |
| `GND`  | GND |

## Framing

Both directions use the same frame:

```
0xA5 | CMD | LEN_L | LEN_H | PAYLOAD (LEN bytes) | CRC8
```

* `CRC8` uses polynomial 0x07 with initial value 0 (CRC-8/SMBUS), computed
  over `CMD`, `LEN_L`, `LEN_H` and `PAYLOAD`.
* The bridge answers every request with `CMD | 0x80`. The first payload byte
  of the answer is a status: `0` for success, anything else for an error.
* Serial settings: 115200 8N1. A CDC-ACM bridge can ignore the baud rate.

## Commands

### 0x01 HELLO

Request payload: empty.
Answer payload: `00` followed by `PN5180BRIDGE` and the firmware version as
ASCII, e.g. `PN5180BRIDGE1.0`. zmod_nfc uses this to recognise the bridge
while probing serial ports.

### 0x02 RESET

Request payload: empty.
The bridge pulls `RST` low for at least 10 µs, releases it, and waits for
`BUSY` to go low (with a timeout of about 100 ms).
Answer payload: `00`.

### 0x03 EXCHANGE

Request payload: `RX_LEN_L RX_LEN_H TX...`

The bridge performs one PN5180 host-interface transaction:

1. Wait for `BUSY` low.
2. Assert `NSS`, clock out the `TX` bytes, release `NSS`.
3. Wait for `BUSY` low.
4. If `RX_LEN > 0`: assert `NSS`, clock out `RX_LEN` bytes of `0xFF` while
   reading `MISO`, release `NSS`, then wait for `BUSY` low again.

Answer payload: `00` followed by the `RX_LEN` bytes that were read.

Each `BUSY` wait times out after 1 s. On a timeout, answer with status `1`
and nothing else.

## Example

The host reads the PN5180 firmware version (EEPROM address 0x12, 2 bytes)
with `READ_EEPROM` (command 0x07):

```
host   -> A5 03 05 00 02 00 07 12 02 CRC
bridge <- A5 83 03 00 00 03 04 CRC        (firmware 4.3)
```
