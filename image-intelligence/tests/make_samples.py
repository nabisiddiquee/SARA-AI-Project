"""Create test images in tests/samples/ (no real people involved).

* profile_card.png  : name, job title, company, @handle, email, phone, LinkedIn + a QR code
* exif_photo.jpg    : a plain photo-like image with camera EXIF (make, model, date)
* not_an_image.jpg  : random bytes, to check the "corrupted file" error

    python tests/make_samples.py
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent / "samples"


def font(size: int, bold: bool = False):
    for name in (("arialbd.ttf" if bold else "arial.ttf"), "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def profile_card() -> None:
    img = Image.new("RGB", (1200, 700), (245, 247, 250))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 1200, 120), fill=(14, 116, 144))
    d.text((60, 35), "Acme Technologies Pvt Ltd", font=font(46, True), fill=(255, 255, 255))
    lines = [
        ("John Smith", 64, True, (15, 23, 42)),
        ("Software Engineer", 40, False, (51, 65, 85)),
        ("@johnsmith", 36, False, (2, 132, 199)),
        ("linkedin.com/in/johnsmith", 36, False, (2, 132, 199)),
        ("john@example.com", 36, False, (51, 65, 85)),
        ("+91 98765 43210", 36, False, (51, 65, 85)),
    ]
    y = 170
    for text, size, bold, colour in lines:
        d.text((60, y), text, font=font(size, bold), fill=colour)
        y += size + 30
    qr = cv2.QRCodeEncoder.create().encode("https://linkedin.com/in/johnsmith")
    qr = cv2.resize(qr, (qr.shape[1] * 8, qr.shape[0] * 8), interpolation=cv2.INTER_NEAREST)
    qr = cv2.copyMakeBorder(qr, 24, 24, 24, 24, cv2.BORDER_CONSTANT, value=255)
    img.paste(Image.fromarray(qr).convert("RGB"), (850, 220))
    img.save(OUT / "profile_card.png")


def exif_photo() -> None:
    rng = np.random.default_rng(1)
    h, w = 600, 900
    sky = np.linspace([235, 180, 120], [250, 220, 170], h // 2).astype(np.uint8)       # BGR blue-ish sky
    grass = np.linspace([60, 160, 70], [40, 120, 50], h - h // 2).astype(np.uint8)    # green ground
    arr = np.concatenate([np.repeat(sky[:, None, :], w, 1), np.repeat(grass[:, None, :], w, 1)])
    arr = np.clip(arr + rng.integers(-8, 8, arr.shape), 0, 255).astype(np.uint8)
    img = Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB))
    exif = Image.Exif()
    exif[0x010F] = "Canon"                     # Make
    exif[0x0110] = "Canon EOS 200D"            # Model
    exif[0x0132] = "2025:08:15 10:30:00"       # DateTime
    exif[0x0112] = 1                            # Orientation
    img.save(OUT / "exif_photo.jpg", exif=exif, quality=90)


def broken_file() -> None:
    (OUT / "not_an_image.jpg").write_bytes(b"this is not really a jpeg" * 40)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    profile_card()
    exif_photo()
    broken_file()
    print("Samples written to", OUT)
