"""Create deterministic HTML request payloads for the benchmark."""

import base64
import io
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps


def letter():
    return {
        "body": """
            <main style="font:12pt 'Liberation Sans',sans-serif;line-height:1.45">
              <h1 style="font-size:23pt">Service confirmation</h1>
              <p>November 15, 2026</p>
              <p>Dear Operations Team,</p>
              <p>This letter confirms that the conversion service processed your
              quarterly report. Please keep this confirmation with your records.</p>
              <p>Reference: OPS-2701</p><p>Regards,<br>Operations Support</p>
            </main>""",
        "fileName": "letter.pdf",
    }


def two_page_letter():
    first_page = letter()["body"]
    return {
        "body": first_page + """
            <main style="break-before:page;font:12pt 'Liberation Sans',sans-serif;line-height:1.45">
              <h1 style="font-size:23pt">Processing details</h1>
              <p>Reference: OPS-2701</p>
              <p>This page accompanies the service confirmation. The operations team
              received the quarterly report and recorded its completion.</p>
              <p>Retain both pages together in your operations records.</p>
              <p>Operations Support</p>
            </main>""",
        "footer": (
            '<div style="margin-left:40px;font:10px sans-serif">Page '
            '<span class="pageNumber"></span> of <span class="totalPages"></span></div>'
        ),
        "fileName": "two-page-letter.pdf",
    }


def report():
    sections = []
    for page in range(1, 9):
        rows = "".join(
            f"<tr><td>{page:02d}-{row:02d}</td><td>Processing queue {row}</td>"
            f"<td>{page * row * 17:,}</td><td>Complete</td></tr>"
            for row in range(1, 17)
        )
        sections.append(
            f"<section><h1>Operations report {page}</h1><p>Monthly utilization, "
            f"exception tracking, and completion figures for group {page}.</p>"
            f"<table><thead><tr><th>Ref</th><th>Queue</th><th>Jobs</th>"
            f"<th>Status</th></tr></thead><tbody>{rows}</tbody></table>"
            f"<p>Prepared for the operations review. Figures reflect the reporting interval.</p></section>"
        )

    return {
        "body": """
            <style>
            body {font:11pt 'Liberation Sans',sans-serif;color:#172c3b}
            section:not(:last-child) {break-after:page}
            table {border-collapse:collapse;width:100%}
            th, td {padding:8pt;border-bottom:1px solid #b4c3ce;text-align:left}
            th {background:#e8f0f5}
            </style>""" + "".join(sections),
        "header": '<div style="margin-left:40px;font:10px sans-serif">Operations summary</div>',
        "footer": (
            '<div style="margin-left:40px;font:10px sans-serif">Page '
            '<span class="pageNumber"></span> of <span class="totalPages"></span></div>'
        ),
        "marginTop": 18,
        "marginBottom": 18,
        "fileName": "report.pdf",
    }


def embedded_image(seed):
    size = (1200, 800)
    noise = Image.frombytes("L", size, random.Random(seed).randbytes(size[0] * size[1]))
    shade = Image.linear_gradient("L").resize(size)
    texture = Image.blend(shade, noise, 0.34)
    palette = (("#13283e", "#dbd09a"), ("#27322d", "#c4dcf0"), ("#381e37", "#f5e2c6"))
    image = ImageOps.colorize(texture, *palette[seed % len(palette)])
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((80 + seed * 12, 160, 485 + seed * 12, 600), radius=36, outline="#f4eee0", width=10)
    encoded = io.BytesIO()
    image.save(encoded, format="JPEG", quality=88, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(encoded.getvalue()).decode("ascii")


def image_report():
    sections = []
    for page in range(1, 7):
        images = "".join(
            f'<img alt="Inspection image {seed}" src="{embedded_image(seed)}">' for seed in (page * 2, page * 2 + 1)
        )
        sections.append(
            f"<section><h1>Inspection record {page}</h1><p>Attached images and observations.</p>{images}</section>"
        )

    return {
        "body": """
            <style>
            body {font:11pt 'Liberation Sans',sans-serif}
            section:not(:last-child) {break-after:page}
            img {display:block;width:4.4in;margin:0 auto 10pt}
            </style>""" + "".join(sections),
        "fileName": "image-report.pdf",
    }


FIXTURES = {
    "letter": (letter, 1, "Service confirmation", 0),
    "two-page-letter": (two_page_letter, 2, "Processing details", 0),
    "report": (report, 8, "Operations report 8", 0),
    "image-report": (image_report, 6, "Inspection record 6", 12),
}


def write_fixture(directory: Path, name: str):
    create, _, _, _ = FIXTURES[name]
    path = directory / f"{name}.json"
    path.write_text(json.dumps(create(), separators=(",", ":")), encoding="utf-8")
    return path
