"""QR codes from the printer camera.

The plugin runs this as a child process (python -m zmodnfc.camera) at a
low priority, so a slow decode never holds Klipper's interpreter and a
hung download or decoder cannot block it. It fetches camera snapshots,
turns each JPEG into a grayscale frame, and looks for a QR code until it
finds one or the time is up. The result is one JSON line on stdout:
{"text": ...} or {"error": ...}.

Decoding a JPEG in Python is out of reach on these printers, and the
host's libjpeg calls exit() on bad data, so a JPEG decoder runs as one
more process per frame:
  turbojpeg  libturbojpeg through Python in Zmod's chroot, which both the
             AD5X and the Creator 5 have. Decodes at 1/2 or 1/4 size
             directly when the frame is large.
  ffmpeg     the Flashforge firmware's ffmpeg, where it has the mjpeg
             demuxer and rawvideo output (the AD5X's does, the Creator 5's
             stripped-down build does not).

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

# Both camera streamers Zmod uses (ustreamer on the AD5X, mjpg-streamer on
# the Creator 5) answer mjpg-streamer's snapshot URL.
DEFAULT_URL = "http://127.0.0.1:8080/?action=snapshot"
DEFAULT_WIDTH = 1024
DEFAULT_CHROOT = "/usr/data/.mod/.zmod"
DECODERS = ("auto", "turbojpeg", "ffmpeg")
MAX_SAVED_FRAMES = 20

# Runs inside the chroot, with its own Python and libraries: JPEG on stdin,
# "width height" line and 8-bit gray pixels on stdout. argv[1] is the scale
# divisor (1, 2 or 4).
_TURBOJPEG_HELPER = r'''
import ctypes, sys
factor = int(sys.argv[1])
tj = ctypes.CDLL("libturbojpeg.so.0")
tj.tjInitDecompress.restype = ctypes.c_void_p
tj.tjDecompressHeader3.argtypes = ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_ulong]
                                   + [ctypes.POINTER(ctypes.c_int)] * 4)
tj.tjDecompress2.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p,
                             ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
tj.tjGetErrorStr.restype = ctypes.c_char_p
data = sys.stdin.buffer.read()
handle = tj.tjInitDecompress()
w, h, sub, space = (ctypes.c_int() for _ in range(4))
if tj.tjDecompressHeader3(handle, data, len(data), ctypes.byref(w), ctypes.byref(h),
                          ctypes.byref(sub), ctypes.byref(space)):
    sys.exit(tj.tjGetErrorStr().decode())
width, height = (w.value + factor - 1) // factor, (h.value + factor - 1) // factor
pixels = ctypes.create_string_buffer(width * height)
if tj.tjDecompress2(handle, data, len(data), pixels, width, 0, height, 6, 0):  # TJPF_GRAY
    sys.exit(tj.tjGetErrorStr().decode())
sys.stdout.buffer.write(b"%d %d\n" % (width, height) + pixels.raw)
'''


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


def _crop_box(crop, width, height):
    """Integer (left, top, right, bottom) inside the frame, even-aligned so
    chroma subsampling cannot shift it."""
    left, top = max(0, int(crop[0])) & ~1, max(0, int(crop[1])) & ~1
    right = left + ((min(width, int(crop[2])) - left) & ~1)
    bottom = top + ((min(height, int(crop[3])) - top) & ~1)
    if right - left < 16 or bottom - top < 16:
        raise CameraError("crop box too small")
    return left, top, right, bottom


class TurboJpegDecoder(object):
    name = "turbojpeg"

    def __init__(self, root=DEFAULT_CHROOT):
        self.root = root
        self.chroot = shutil.which("chroot") or "/usr/sbin/chroot"
        # The host's library paths mean nothing inside the chroot.
        self.env = {key: value for key, value in os.environ.items()
                    if key not in ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONPATH", "PYTHONHOME")}
        self.env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"

    @staticmethod
    def available(root=DEFAULT_CHROOT):
        return (os.path.exists(os.path.join(root, "usr/lib/libturbojpeg.so.0"))
                and os.path.exists(os.path.join(root, "usr/bin/python3")))

    def gray(self, jpeg, max_width=DEFAULT_WIDTH, crop=None):
        """(pixels, width, height): the JPEG as 8-bit grayscale, scaled down
        by 2 or 4 while it stays at least max_width wide, or the crop box
        (left, top, right, bottom) of it at full resolution."""
        width, height = jpeg_size(jpeg)
        factor = 1
        if crop is None:
            while factor < 4 and width // (factor * 2) >= max_width:
                factor *= 2
        command = [self.chroot, self.root, "python3", "-c", _TURBOJPEG_HELPER, str(factor)]
        try:
            result = subprocess.run(command, input=jpeg, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=self.env, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CameraError("JPEG decoder failed: %s" % exc)
        header, _, pixels = result.stdout.partition(b"\n")
        try:
            out_width, out_height = [int(v) for v in header.split()]
        except ValueError:
            out_width = out_height = 0
        if result.returncode or not out_width or len(pixels) != out_width * out_height:
            message = result.stderr.decode("utf-8", "replace").strip().splitlines()
            raise CameraError("could not decode the camera image%s"
                              % (": " + message[-1] if message else ""))
        if crop is not None:
            left, top, right, bottom = _crop_box(crop, out_width, out_height)
            pixels = b"".join(pixels[y * out_width + left:y * out_width + right]
                              for y in range(top, bottom))
            out_width, out_height = right - left, bottom - top
        return pixels, out_width, out_height


class FfmpegDecoder(object):
    name = "ffmpeg"

    def __init__(self, path):
        self.path = path

    def gray(self, jpeg, max_width=DEFAULT_WIDTH, crop=None):
        """Like TurboJpegDecoder.gray, scaled to exactly max_width. ffmpeg's
        -lowres decodes big frames at 1/2 or 1/4 size directly, which is
        much faster than decoding and then scaling."""
        width, height = jpeg_size(jpeg)
        lowres = 0
        if crop is not None:
            left, top, right, bottom = _crop_box(crop, width, height)
            out_width, out_height = right - left, bottom - top
            video_filter = "crop=%d:%d:%d:%d" % (out_width, out_height, left, top)
        else:
            while lowres < 2 and (width >> (lowres + 1)) >= max_width:
                lowres += 1
            out_width = min(max_width, width)
            out_height = max(1, height * out_width // width)
            video_filter = "scale=%d:%d" % (out_width, out_height)
        command = [self.path, "-hide_banner", "-loglevel", "error",
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


def make_decoder(kind="auto", chroot=DEFAULT_CHROOT, ffmpeg=""):
    """The JPEG decoder to use: turbojpeg in the chroot when it is there,
    otherwise ffmpeg."""
    if kind in ("auto", "turbojpeg") and TurboJpegDecoder.available(chroot):
        return TurboJpegDecoder(chroot)
    if kind == "turbojpeg":
        raise CameraError("no libturbojpeg and python3 in %s (qr_chroot)" % chroot)
    path = find_ffmpeg(ffmpeg)
    if path is None:
        raise CameraError("no JPEG decoder: neither libturbojpeg in %s nor ffmpeg" % chroot)
    return FfmpegDecoder(path)


def decode_jpeg(jpeg, decoder, max_width=DEFAULT_WIDTH):
    """Text of a QR code in a JPEG, or None. The frame is decoded scaled
    down first; a code whose modules came out too small to read is tried
    again from a full-resolution crop around it."""
    pixels, width, height = decoder.gray(jpeg, max_width)
    small = []
    text = qr.decode_image(pixels, width, height, small=small)
    if text is not None or not small:
        return text
    scale = jpeg_size(jpeg)[0] / float(width)
    if scale < 1.5:
        return None
    for box in small[:3]:
        crop = [value * scale for value in box]
        text = qr.decode_image(*decoder.gray(jpeg, crop=crop))
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


def scan(url, decoder, seconds, max_width=DEFAULT_WIDTH, log=None, save_dir=None):
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
        text = decode_jpeg(jpeg, decoder, max_width)
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
    parser.add_argument("--decoder", choices=DECODERS, default="auto", help="JPEG decoder")
    parser.add_argument("--chroot", default=DEFAULT_CHROOT, help="chroot with libturbojpeg")
    parser.add_argument("--ffmpeg", default="", help="path to ffmpeg")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH,
                        help="scale frames down towards this width")
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
        decoder = make_decoder(args.decoder, args.chroot, args.ffmpeg)
        if log:
            log("decoder: %s" % decoder.name)
        if args.image:
            with open(args.image, "rb") as handle:
                text, frames = decode_jpeg(handle.read(), decoder, args.width), 1
        else:
            text, frames = scan(args.url, decoder, args.time, args.width, log, args.save_dir)
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
