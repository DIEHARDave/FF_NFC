"""Generate the lanetag-nfc logos from LaneTag's (the ESP32 Wi-Fi reader).

Usage: python make_logo.py LANETAG_LOGO_DIR Poppins-Bold.ttf OUTPUT_DIR

LANETAG_LOGO_DIR is LaneTag's docs/logo (lanetag-logo.svg,
lanetag-mark.svg). The wired reader keeps LaneTag's spool and reader card;
the ESP32 board becomes a USB-A plug at the same cable end, the orange
accent becomes teal, and a "USB" tag follows the wordmark. The tag is
Poppins Bold (SIL OFL), converted to outlines like LaneTag's wordmark.
Needs fontTools.
"""
import math
import os
import re
import sys

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

SRC, FONT, OUT = sys.argv[1], sys.argv[2], sys.argv[3]

INK = "#1b2632"
SLATE_D = "#243241"
GREY = "#c8d0d8"
WHITE = "#ffffff"
DARK_TEXT = "#eef2f6"
# LaneTag's orange -> teal for the wired reader
ACCENT = {"#f28a1e": "#14a3a3", "#c9650f": "#0b7676"}
TEAL = ACCENT["#f28a1e"]


def f(v):
    s = "%.2f" % v
    s = s.rstrip("0").rstrip(".")
    return "0" if s == "-0" else s


def recolor(svg):
    for old, new in ACCENT.items():
        svg = svg.replace(old, new)
    return svg


def usb_plug(ax, ay, deg=4):
    """USB-A plug hanging from the cable end (ax, ay)."""
    bw, bh = 50, 62          # overmold
    sw, sh = 40, 46          # metal shell
    x, y = ax - bw / 2, ay - 4
    cx, ty = ax, y + 12
    o = ['<g id="usb-plug" transform="rotate(%s %s %s)">' % (deg, f(ax), f(ay))]
    o.append('<rect x="%s" y="%s" width="18" height="16" rx="5" fill="%s" stroke="%s" stroke-width="3"/>'
             % (f(ax - 9), f(y - 10), SLATE_D, INK))
    o.append('<rect x="%s" y="%s" width="%d" height="%d" rx="10" fill="%s" stroke="%s" stroke-width="3"/>'
             % (f(x), f(y), bw, bh, SLATE_D, INK))
    # USB trident
    o.append('<g id="usb-symbol" fill="none" stroke="%s" stroke-width="3.2" stroke-linecap="round" '
             'stroke-linejoin="round">' % GREY)
    o.append('<path d="M%s %sV%s"/>' % (f(cx), f(ty + 34), f(ty + 4)))
    o.append('<path d="M%s %sL%s %sV%s"/>' % (f(cx), f(ty + 27), f(cx - 11), f(ty + 18), f(ty + 12)))
    o.append('<path d="M%s %sL%s %sV%s"/>' % (f(cx), f(ty + 22), f(cx + 11), f(ty + 14), f(ty + 9)))
    o.append("</g>")
    o.append('<path d="M%s %sL%s %sL%s %sZ" fill="%s"/>'
             % (f(cx - 5), f(ty + 6), f(cx), f(ty - 2), f(cx + 5), f(ty + 6), GREY))
    o.append('<circle cx="%s" cy="%s" r="3.4" fill="%s"/>' % (f(cx - 11), f(ty + 11), GREY))
    o.append('<rect x="%s" y="%s" width="6.8" height="6.8" fill="%s"/>' % (f(cx + 7.6), f(ty + 5.6), GREY))
    o.append('<circle cx="%s" cy="%s" r="3.6" fill="%s"/>' % (f(cx), f(ty + 36), GREY))
    # metal shell with its two slots
    sx, sy = ax - sw / 2, y + bh - 3
    o.append('<rect x="%s" y="%s" width="%d" height="%d" rx="3" fill="%s" stroke="%s" stroke-width="3"/>'
             % (f(sx), f(sy), sw, sh, GREY, INK))
    o.append('<rect x="%s" y="%s" width="9" height="7" rx="1" fill="%s"/>' % (f(sx + 7), f(sy + 8), INK))
    o.append('<rect x="%s" y="%s" width="9" height="7" rx="1" fill="%s"/>' % (f(sx + sw - 16), f(sy + 8), INK))
    o.append('<rect x="%s" y="%s" width="%d" height="16" rx="2" fill="%s" stroke="%s" stroke-width="2"/>'
             % (f(sx + 5), f(sy + 22), sw - 10, WHITE, INK))
    o.append("</g>")
    return "\n".join(o)


def text_path(text, x, baseline, size, color, path_id, tracking=0.02):
    """text in Poppins Bold as one outlined path; returns (svg, width)."""
    font = TTFont(FONT)
    gs, cmap = font.getGlyphSet(), font.getBestCmap()
    s = size / font["head"].unitsPerEm
    px, ds = x, []
    for ch in text:
        pen = SVGPathPen(gs, ntos=f)
        gs[cmap[ord(ch)]].draw(TransformPen(pen, (s, 0, 0, -s, px, baseline)))
        ds.append(pen.getCommands())
        px += gs[cmap[ord(ch)]].width * s + tracking * size
    return '<path id="%s" fill="%s" d="%s"/>' % (path_id, color, "".join(ds)), px - x - tracking * size


def usb_tag(x, baseline):
    """Teal "USB" tag after the wordmark, its bottom on the wordmark's baseline."""
    size, pad_x, h = 46, 18, 64
    glyphs, w = text_path("USB", 0, 0, size, WHITE, "word-usb")
    tw = w + 2 * pad_x
    top = baseline - h
    # cap height of Poppins Bold ~0.7 em: centre the letters in the tag
    text_base = top + h / 2 + 0.35 * size
    glyphs, _ = text_path("USB", x + pad_x, text_base, size, WHITE, "word-usb")
    rect = '<rect x="%s" y="%s" width="%s" height="%d" rx="14" fill="%s"/>' % (f(x), f(top), f(tw), h, TEAL)
    return '<g id="usb-tag">\n%s\n%s\n</g>' % (rect, glyphs), tw


def lockup(src_svg, lane_color):
    svg = recolor(src_svg)
    d = re.search(r'<g id="cable".*?<path d="([^"]+)"', svg, re.S).group(1)
    nums = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", d)]
    svg = re.sub(r'<g id="esp32".*?</g>\n', usb_plug(nums[-2], nums[-1]) + "\n", svg, count=1, flags=re.S)
    if 'id="usb-plug"' not in svg:
        raise SystemExit("no esp32 group in " + SRC)
    svg = re.sub(r'(<path id="word-lane" fill=")[^"]+', r"\g<1>" + lane_color, svg)
    width = float(re.search(r'viewBox="0 0 ([\d.]+) ', svg).group(1))
    # LaneTag's wordmark ends 20 units before the right edge, baseline 244
    tag, tw = usb_tag(width - 20 + 22, 244)
    new_w = math.ceil(width + 22 + tw)
    svg = svg.replace('viewBox="0 0 %s 400" width="%s"' % (f(width), f(width)),
                      'viewBox="0 0 %d 400" width="%d"' % (new_w, new_w))
    svg = svg.replace("<title>LaneTag</title>", "<title>LaneTag USB (lanetag-nfc)</title>")
    return svg.replace("</svg>", tag + "\n</svg>"), new_w


src = open(os.path.join(SRC, "lanetag-logo.svg"), encoding="utf-8").read()
for name, lane in (("lanetag-nfc-logo.svg", SLATE_D), ("lanetag-nfc-logo-dark.svg", DARK_TEXT)):
    svg, w = lockup(src, lane)
    open(os.path.join(OUT, name), "w", encoding="utf-8").write(svg)
mark = recolor(open(os.path.join(SRC, "lanetag-mark.svg"), encoding="utf-8").read())
mark = mark.replace("<title>LaneTag</title>", "<title>LaneTag USB (lanetag-nfc)</title>")
open(os.path.join(OUT, "lanetag-nfc-mark.svg"), "w", encoding="utf-8").write(mark)
print("width", w)
