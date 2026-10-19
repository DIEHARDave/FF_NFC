"""QR codes from the printer camera.

The plugin runs this as a child process (python -m zmodnfc.camera) at a
low priority, so a slow decode never holds Klipper's interpreter and a
hung download or ffmpeg cannot block it. It fetches camera snapshots,
has ffmpeg turn each JPEG into a small grayscale frame (decoding a JPEG
in Python is out of reach on the AD5X, and libjpeg calls exit() on bad
data, so it is not loaded in-process), and looks for a QR code until it
finds one or the time is up. The result is one JSON line on stdout:
{"text": ...} or {"error": ...}.

Also usable by hand, e.g. with a saved snapshot:
    python3 -m zmodnfc.camera --image label.jpg
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

from . import qr

DEFAULT_URL = "http://127.0.0.1:8080/snapshot"
DEFAULT_WIDTH = 1024
MAX_SAVED_FRAMES = 20


class CameraError(Exception):
    pass


def find_ffmpeg(configured=""):
    """The ffmpeg to use: the configured one, one on PATH, or the one the
    Flashforge firmware ships under /usr/prog."""
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    found = shutil.which("ffmpeg")
    if found:
        return found
    for path in sorted(glob.glob("/usr/prog/ffmpeg*/bin/ffmpeg")):
        if os.access(path, os.X_OK):
            return path
    return None


def jpeg_size(data):
    """(width, height) from a JPEG's frame header."""
    if data[:2] != b"\xff\xd8":
        raise CameraError("the camera did not send a JPEG image")
    pos = 2
    while pos + 9 < len(data):
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[pos + 5:pos + 7], "big")
            width = int.from_bytes(data[pos + 7:pos + 9], "big")
            return width, height
        pos += 2 + int.from_bytes(data[pos + 2:pos + 4], "big")
    raise CameraError("no frame header in the camera's JPEG")


def gray_frame(jpeg, ffmpeg, max_width=DEFAULT_WIDTH, crop=None):
    """(pixels, width, height): the JPEG as 8-bit grayscale, at most
    max_width wide, or the crop box (left, top, right, bottom) of it at full
    resolution. ffmpeg's -lowres decodes big frames at 1/2 or 1/4 size
    directly, which is much faster than decoding and then scaling."""
    width, height = jpeg_size(jpeg)
    lowres = 0
    if crop is not None:
        # Even offsets and sizes, so chroma subsampling cannot shift them.
        left, top = int(crop[0]) & ~1, int(crop[1]) & ~1
        out_width = (min(width, int(crop[2])) - left) & ~1
        out_height = (min(height, int(crop[3])) - top) & ~1
        if out_width < 16 or out_height < 16:
            raise CameraError("crop box too small")
        video_filter = "crop=%d:%d:%d:%d" % (out_width, out_height, left, top)
    else:
        while lowres < 2 and (width >> (lowres + 1)) >= max_width:
            lowres += 1
        out_width = min(max_width, width)
        out_height = max(1, height * out_width // width)
        video_filter = "scale=%d:%d" % (out_width, out_height)
    command = [ffmpeg, "-hide_banner", "-loglevel", "error",
               "-lowres", str(lowres), "-f", "mjpeg", "-i", "pipe:0",
               "-frames:v", "1", "-vf", video_filter,
               "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1"]
    try:
        result = subprocess.run(command, input=jpeg, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CameraError("ffmpeg failed: %s" % exc)
    if len(result.stdout) != out_width * out_height:
        message = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise CameraError("ffmpeg could not convert the image%s"
                          % (": " + message[-1] if message else ""))
    return result.stdout, out_width, out_height


def decode_jpeg(jpeg, ffmpeg, max_width=DEFAULT_WIDTH):
    """Text of a QR code in a JPEG, or None. The frame is decoded scaled
    down first; a code whose modules came out too small to read is tried
    again from a full-resolution crop around it."""
    pixels, width, height = gray_frame(jpeg, ffmpeg, max_width)
    small = []
    text = qr.decode_image(pixels, width, height, small=small)
    if text is not None or not small:
        return text
    scale = jpeg_size(jpeg)[0] / float(width)
    if scale < 1.5:
        return None
    for box in small[:3]:
        crop = [value * scale for value in box]
        text = qr.decode_image(*gray_frame(jpeg, ffmpeg, crop=crop))
        if text is not None:
            return text
    return None


def snapshot(url, timeout=5.):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read()
    except Exception as exc:
        raise CameraError("no camera image from %s: %s" % (url, exc))


def _save_frames(directory):
    """A function that stores each frame of this scan in directory (emptied
    first), for looking at why a label was not read."""
    try:
        os.makedirs(directory, exist_ok=True)
        for name in os.listdir(directory):
            if name.startswith("frame") and name.endswith(".jpg"):
                os.remove(os.path.join(directory, name))
    except OSError:
        return None

    def save(number, jpeg):
        if number <= MAX_SAVED_FRAMES:
            try:
                with open(os.path.join(directory, "frame%02d.jpg" % number), "wb") as handle:
                    handle.write(jpeg)
            except OSError:
                pass
    return save


def scan(url, ffmpeg, seconds, max_width=DEFAULT_WIDTH, log=None, save_dir=None):
    """Text of the first QR code the camera sees within seconds."""
    deadline = time.time() + seconds
    frames = 0
    save = _save_frames(save_dir) if save_dir else None
    while True:
        started = time.time()
        jpeg = snapshot(url)
        if save:
            save(frames + 1, jpeg)
        fetched = time.time()
        text = decode_jpeg(jpeg, ffmpeg, max_width)
        frames += 1
        if log:
            log("frame %d: fetch %.2fs, decode %.2fs%s"
                % (frames, fetched - started, time.time() - fetched,
                   ", found" if text is not None else ""))
        if text is not None:
            return text, frames
        if time.time() >= deadline:
            return None, frames


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read a QR code from the printer camera.")
    parser.add_argument("--url", default=DEFAULT_URL, help="camera snapshot URL")
    parser.add_argument("--image", help="decode this JPEG file instead of the camera")
    parser.add_argument("--ffmpeg", default="", help="path to ffmpeg")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH,
                        help="scale frames down to this width")
    parser.add_argument("--time", type=float, default=15., help="seconds to keep looking")
    parser.add_argument("--nice", type=int, default=0, help="lower this process's priority")
    parser.add_argument("--verbose", action="store_true", help="timings on stderr")
    parser.add_argument("--save-dir", help="keep this scan's camera frames in this directory")
    args = parser.parse_args(argv)
    if args.nice:
        try:
            os.nice(args.nice)
        except OSError:
            pass
    log = (lambda message: print(message, file=sys.stderr)) if args.verbose else None
    result = {}
    try:
        ffmpeg = find_ffmpeg(args.ffmpeg)
        if ffmpeg is None:
            raise CameraError("ffmpeg not found; set qr_ffmpeg to its path")
        if args.image:
            with open(args.image, "rb") as handle:
                text, frames = decode_jpeg(handle.read(), ffmpeg, args.width), 1
        else:
            text, frames = scan(args.url, ffmpeg, args.time, args.width, log, args.save_dir)
        result["frames"] = frames
        if text is None:
            result["error"] = "no QR code found"
        else:
            result["text"] = text
    except CameraError as exc:
        result["error"] = str(exc)
    print(json.dumps(result))
    return 0 if "text" in result else 1


if __name__ == "__main__":
    sys.exit(main())
