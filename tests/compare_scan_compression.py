#!/usr/bin/env python3
"""Compare compression endpoints on deterministic, synthetic 300 DPI scans."""

import argparse
import random
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps
from pypdf import PdfReader


def write_scans(directory):
    size = (2550, 3300)
    text = Image.new("1", size, 1)
    draw = ImageDraw.Draw(text)
    draw.text((230, 205), "Scanned report", font=ImageFont.load_default(size=90))
    body = ImageFont.load_default(size=40)
    for row in range(38):
        draw.text(
            (230, 405 + 65 * row),
            f"{row + 1:02d}    Synthetic line of text for the PDF compression comparison",
            font=body,
        )
    draw.rectangle((225, 2950, 2325, 3070), outline=0, width=4)

    noise = Image.frombytes("L", size, random.Random(42).randbytes(size[0] * size[1]))
    paper = noise.point(lambda value: 222 + value // 8)
    grayscale = ImageChops.multiply(ImageOps.autocontrast(text.convert("L"), cutoff=1), paper)
    color = ImageOps.colorize(grayscale, "#161317", "#fef2d0")
    draw = ImageDraw.Draw(color)
    draw.rounded_rectangle((1620, 245, 2310, 350), radius=26, fill="#56769f")
    draw.rectangle((230, 2950, 1275, 3070), fill="#bc7752")
    draw.rectangle((1275, 2950, 2325, 3070), fill="#518c83")

    for name, image in (("bilevel", text), ("grayscale", grayscale), ("color", color)):
        path = directory / f"{name}.pdf"
        jpeg_options = {} if image.mode == "1" else {"quality": 92}
        image.save(path, resolution=300, **jpeg_options)
        yield name, path


def compress(url, route, quality, source, output):
    subprocess.run(
        ["curl", "--fail-with-body", "--silent", "--show-error", "--max-time", "150",
         "-o", output, "-F", f"file=@{source};type=application/pdf", "-F", f"quality={quality}",
         f"{url.rstrip('/')}/{route}"],
        check=True,
    )
    pages = PdfReader(output).pages
    assert len(pages) == 1, output
    page = pages[0]
    assert (float(page.mediabox.width), float(page.mediabox.height)) == (612.0, 792.0), output
    image = page.images[0].image
    return output.stat().st_size, f"{image.width}x{image.height}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline_url")
    parser.add_argument("candidate_url")
    args = parser.parse_args()

    print("scan,endpoint,quality,input_bytes,baseline_bytes,candidate_bytes,baseline_image,candidate_image")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name, scan in write_scans(root):
            for route in ("Compress", "ImageCompress"):
                for quality in (25, 55, 85):
                    old_size, old_dims = compress(args.baseline_url, route, quality, scan, root / "baseline.pdf")
                    new_size, new_dims = compress(args.candidate_url, route, quality, scan, root / "candidate.pdf")
                    print(f"{name},{route},{quality},{scan.stat().st_size},{old_size},{new_size},{old_dims},{new_dims}")


if __name__ == "__main__":
    main()
