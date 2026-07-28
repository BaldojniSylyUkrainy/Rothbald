#!/usr/bin/env python3
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

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

iconset_sizes = {
    "icon_16x16.png": 16,
    "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32,
    "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128,
    "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256,
    "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512,
    "icon_512x512@2x.png": 1024,
}

with TemporaryDirectory() as temporary_directory:
    iconset = Path(temporary_directory) / "app-icon.iconset"
    iconset.mkdir()
    for filename, icon_size in iconset_sizes.items():
        canvas.resize((icon_size, icon_size), Image.Resampling.LANCZOS).save(iconset / filename)

    subprocess.run(
        ["/usr/bin/iconutil", "--convert", "icns", str(iconset), "--output", str(ASSETS / "app-icon.icns")],
        check=True,
    )
