#!/usr/bin/env python3
from io import BytesIO
from pathlib import Path
import struct

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
FONT = "/System/Library/Fonts/Supplemental/Bradley Hand Bold.ttf"

canvas = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
draw = ImageDraw.Draw(canvas)
draw.rounded_rectangle((76, 76, 948, 948), radius=198, fill="#08080a", outline="#29292c", width=6)

font = ImageFont.truetype(FONT, 560)
box = draw.textbbox((0, 0), "Ro", font=font, stroke_width=0)
width, height = box[2] - box[0], box[3] - box[1]
draw.text(
    ((1024 - width) / 2 - box[0] - 8, (1024 - height) / 2 - box[1] - 10),
    "Ro",
    font=font,
    fill="white",
)

canvas.save(ASSETS / "app-icon.png")
canvas.save(ASSETS / "app-icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

chunks = []
for kind, icon_size in ((b"ic10", 1024), (b"ic09", 512), (b"ic08", 256), (b"ic07", 128), (b"icp6", 64), (b"icp5", 32), (b"icp4", 16)):
    output = BytesIO()
    canvas.resize((icon_size, icon_size), Image.Resampling.LANCZOS).save(output, format="PNG")
    payload = output.getvalue()
    chunks.append(kind + struct.pack(">I", len(payload) + 8) + payload)
body = b"".join(chunks)
(ASSETS / "app-icon.icns").write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)
