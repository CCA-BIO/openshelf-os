from __future__ import annotations

import os
from functools import cache
from typing import Literal

import barcode
import segno
from barcode.writer import ImageWriter
from PIL import Image, ImageDraw, ImageFont

ESC = b"\x1b"
GS = b"\x1d"

LABEL_WIDTH_DOTS = 384
LABEL_HEIGHT_DOTS = 203
LEFT_MARGIN_DOTS = 6
RIGHT_MARGIN_DOTS = 24
# The raster starts at the label's top edge give or take ~8 dots per label, so the content is
# centred with equal margins that absorb that jitter.
TOP_MARGIN_DOTS = 20
BOTTOM_MARGIN_DOTS = 20
TEXT_GAP_DOTS = 4
TEXT_MAX_SIZE = 28
TEXT_MIN_SIZE = 14
FONT_PATH = os.path.join(os.path.dirname(barcode.__file__), "fonts", "DejaVuSansMono.ttf")

# Kept in the job for compatibility with the printer, although its effective
# darkness is stored in non-volatile printer configuration.
HEATING_DOTS = 9
HEATING_TIME = 80
HEATING_INTERVAL = 2

Symbology = Literal["code128", "qr"]
# 2 modules of quiet zone instead of the spec's 4: the label edge and the text gap add the rest,
# and every saved module lets the code grow.
QR_BORDER_MODULES = 2
QR_TEXT_GAP_DOTS = 8
QR_DEFAULT_MODULE_DOTS = 4


@cache
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_PATH, size)


def _fit_text(text: str, width: int) -> tuple[str, ImageFont.FreeTypeFont]:
    """Shrink the font to fit ``width``, then truncate with an ellipsis at the floor size."""
    for size in range(TEXT_MAX_SIZE, TEXT_MIN_SIZE - 1, -1):
        font = _font(size)
        if font.getlength(text) <= width:
            return text, font
    while len(text) > 1 and font.getlength(text + "…") > width:
        text = text[:-1]
    return text + "…", font


def render_label(payload: str, location: str | None = None) -> Image.Image:
    """Render a Code 128 barcode using the layout tuned on the T9E-US.

    The payload is printed as a human-readable line above the bars, joined by a mid dot to
    the storage location when given (bin labels).
    """
    code = barcode.get("code128", payload, writer=ImageWriter())
    barcode_image = code.render(
        writer_options={
            "module_height": 12.0,
            "quiet_zone": 1,
            "write_text": False,
            "font_size": 0,
            "text_distance": 3,
        }
    ).convert("L")

    available_width = LABEL_WIDTH_DOTS - LEFT_MARGIN_DOTS - RIGHT_MARGIN_DOTS
    # One dot narrower: the text is double-struck one dot to the right.
    text, font = _fit_text(f"{payload} · {location}" if location else payload, available_width - 1)
    text_height = font.getbbox("Ag")[3] + TEXT_GAP_DOTS
    available_height = LABEL_HEIGHT_DOTS - TOP_MARGIN_DOTS - BOTTOM_MARGIN_DOTS - text_height
    source_width, source_height = barcode_image.size
    rendered_height = min(
        available_height,
        round(source_height * (available_width / source_width)),
    )
    barcode_image = barcode_image.resize(
        (available_width, rendered_height),
        Image.Resampling.NEAREST,
    )

    label = Image.new("L", (LABEL_WIDTH_DOTS, LABEL_HEIGHT_DOTS), 255)
    x = LEFT_MARGIN_DOTS + (available_width - barcode_image.width) // 2
    y = TOP_MARGIN_DOTS + text_height + (available_height - barcode_image.height) // 2
    label.paste(barcode_image, (x, y))

    # Text sits above the bars so it stays visible over the back lip of a shelf.
    # Double-striking one dot apart thickens the strokes without closing the zeros' counters.
    text_x = LEFT_MARGIN_DOTS + (available_width - 1 - font.getlength(text)) / 2
    draw = ImageDraw.Draw(label)
    for dx in (0, 1):
        draw.text((text_x + dx, TOP_MARGIN_DOTS), text, font=font, fill=0)
    return label


def _wrap_text(text: str, width: int, height: int) -> tuple[list[str], ImageFont.FreeTypeFont]:
    """Break ``text`` at any character into lines of the largest font that fits ``width`` x ``height``.

    At the floor size the lines that do not fit are dropped and the last kept line gets an ellipsis.
    """
    for size in range(TEXT_MAX_SIZE, TEXT_MIN_SIZE - 1, -1):
        font = _font(size)
        lines: list[str] = []
        for char in text:
            if lines and font.getlength(lines[-1] + char) <= width:
                lines[-1] += char
            else:
                lines.append(char)
        line_height = font.getbbox("Ag")[3] + TEXT_GAP_DOTS
        max_lines = max(1, height // line_height)
        if len(lines) <= max_lines:
            return lines, font
    lines = lines[:max_lines]
    last = lines[-1]
    while len(last) > 1 and font.getlength(last + "…") > width:
        last = last[:-1]
    lines[-1] = last + "…"
    return lines, font


def render_qr_label(
    payload: str, location: str | None = None, *, module_dots: int = QR_DEFAULT_MODULE_DOTS
) -> Image.Image:
    """Render a QR code at the label's left edge with the payload wrapped beside it.

    The code sits flush left so the label can be trimmed to just the code for small items.
    ``module_dots`` is shrunk when the code would not fit the label height.
    """
    qr = segno.make_qr(payload, error="m", boost_error=False)
    modules = qr.symbol_size(scale=1, border=QR_BORDER_MODULES)[0]
    available_height = LABEL_HEIGHT_DOTS - TOP_MARGIN_DOTS - BOTTOM_MARGIN_DOTS
    module_dots = min(module_dots, available_height // modules)
    if module_dots < 1:
        raise ValueError("payload too long for a QR label")
    side = modules * module_dots

    label = Image.new("L", (LABEL_WIDTH_DOTS, LABEL_HEIGHT_DOTS), 255)
    code_x = LEFT_MARGIN_DOTS
    code_y = TOP_MARGIN_DOTS + (available_height - side) // 2
    draw = ImageDraw.Draw(label)
    for row_idx, row in enumerate(qr.matrix_iter(scale=1, border=QR_BORDER_MODULES)):
        y = code_y + row_idx * module_dots
        for col_idx, dark in enumerate(row):
            if dark:
                x = code_x + col_idx * module_dots
                draw.rectangle((x, y, x + module_dots - 1, y + module_dots - 1), fill=0)

    text_x = code_x + side + QR_TEXT_GAP_DOTS
    # One dot narrower: the text is double-struck one dot to the right.
    text_width = LABEL_WIDTH_DOTS - RIGHT_MARGIN_DOTS - text_x - 1
    lines, font = _wrap_text(f"{payload} · {location}" if location else payload, text_width, available_height)
    line_height = font.getbbox("Ag")[3] + TEXT_GAP_DOTS
    text_y = TOP_MARGIN_DOTS + (available_height - len(lines) * line_height + TEXT_GAP_DOTS) // 2
    for idx, line in enumerate(lines):
        for dx in (0, 1):
            draw.text((text_x + dx, text_y + idx * line_height), line, font=font, fill=0)
    return label


# The T9E-US ignores a feed command after a raster job. Configurable blank
# raster rows advance the finished label far enough to clear the enclosure.
def _add_feed_rows(label: Image.Image, feed_dots: int) -> Image.Image:
    if feed_dots < 0:
        raise ValueError("feed_dots must be non-negative")
    raster = Image.new(
        "L",
        (label.width, label.height + feed_dots),
        255,
    )
    raster.paste(label, (0, 0))
    return raster


def _pack_raster(image: Image.Image, *, threshold: int = 128) -> tuple[bytes, int, int]:
    grayscale = image.convert("L")
    width, height = grayscale.size
    bytes_per_row = (width + 7) // 8
    pixels = grayscale.load()
    if pixels is None:
        raise RuntimeError("unable to access label raster pixels")
    packed = bytearray(bytes_per_row * height)

    for y in range(height):
        row_offset = y * bytes_per_row
        for x in range(width):
            pixel = pixels[x, y]
            if not isinstance(pixel, (int, float)):
                raise TypeError("expected a grayscale label raster")
            if pixel < threshold:
                packed[row_offset + x // 8] |= 0x80 >> (x % 8)

    return bytes(packed), bytes_per_row, height


def build_label_job(
    payload: str,
    *,
    feed_dots: int,
    location: str | None = None,
    symbology: Symbology = "code128",
    qr_module_dots: int = QR_DEFAULT_MODULE_DOTS,
) -> bytes:
    """Build the complete ESC/POS raster job for one label without doing I/O."""
    if symbology == "qr":
        label = render_qr_label(payload, location, module_dots=qr_module_dots)
    else:
        label = render_label(payload, location)
    packed, bytes_per_row, height = _pack_raster(_add_feed_rows(label, feed_dots))
    raster_header = (
        GS
        + b"v0"
        + bytes(
            [
                0,
                bytes_per_row & 0xFF,
                bytes_per_row >> 8,
                height & 0xFF,
                height >> 8,
            ]
        )
    )
    density = ESC + b"7" + bytes([HEATING_DOTS, HEATING_TIME, HEATING_INTERVAL])
    return ESC + b"@" + density + raster_header + packed
