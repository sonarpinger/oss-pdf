#!/usr/bin/env python3
"""Exercise a running container using curl, Poppler, and ImageMagick."""

import argparse
import concurrent.futures
import json
import math
import re
import struct
import subprocess
import tempfile
import time
import zipfile
import zlib
from pathlib import Path


def command(*args):
    return subprocess.check_output(args, text=True)


def write_pdf(path):
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 600] /CropBox [10 20 390 580] "
        b"/Rotate 90 /Resources << /Font << /F1 7 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 240 360] "
        b"/Resources << /Font << /F1 7 0 R >> >> /Contents 6 0 R >>",
    ]
    for label in (b"VECTOR ONE", b"VECTOR TWO"):
        contents = b"12 35 120 60 re S BT /F1 15 Tf 30 110 Td (" + label + b") Tj ET"
        objects.append(f"<< /Length {len(contents)} >>\nstream\n".encode() + contents + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    pdf = bytearray(b"%PDF-1.7\n")
    offsets = [0]
    for index, data in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode() + data + b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(pdf)


def write_docx(path):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", """
          <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
            <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
            <Default Extension="xml" ContentType="application/xml"/>
            <Override PartName="/word/document.xml"
              ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
          </Types>""")
        docx.writestr("_rels/.rels", """
          <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
            <Relationship Id="rId1" Target="word/document.xml"
              Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>
          </Relationships>""")
        docx.writestr("word/document.xml", """
          <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
            <w:body><w:p><w:r><w:t>Generated Word fixture</w:t></w:r></w:p></w:body>
          </w:document>""")


def write_images(png, jpeg):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    width, height = 300, 120
    pixels = (b"\0" + b"\x0a\x40\xcd\x80" * width) * height
    png.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")
    )
    subprocess.run(["convert", png, "-background", "white", "-alpha", "remove", jpeg], check=True)
    exif = b"Exif\0\0" + b"II" + struct.pack("<HIHHHIHHI", 42, 8, 1, 0x112, 3, 1, 6, 0, 0)
    data = jpeg.read_bytes()
    jpeg.write_bytes(data[:2] + b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif + data[2:])


def pages(path):
    return [(float(w), float(h)) for w, h in re.findall(
        r"^Page\s+\d+ size:\s+([\d.]+) x ([\d.]+) pts",
        command("pdfinfo", "-f", "1", "-l", "20", path),
        re.M,
    )]


def text(path):
    return command("pdftotext", path, "-")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url", nargs="?", default="http://127.0.0.1:8080")
    parser.add_argument("--load", action="store_true", help="also exercise concurrent mixed conversions")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        pdf, docx, png, jpeg = (root / filename for filename in ("mixed.pdf", "sample.docx", "sample.png", "sample.jpg"))
        write_pdf(pdf)
        write_docx(docx)
        write_images(png, jpeg)

        def request(route, name, *arguments, expected=200):
            output = root / name
            headers = root / f"{name}.headers"
            status = command(
                "curl", "--silent", "--show-error", "--max-time", "150", "--output", output,
                "--dump-header", headers, "--write-out", "%{http_code}", *arguments, f"{args.url}/{route}",
            )
            assert status == str(expected), (route, status, output.read_bytes()[:500])
            if expected == 200:
                with output.open("rb") as response:
                    assert response.read(5) == b"%PDF-", route
                assert "content-type: application/pdf" in headers.read_text().lower(), route
            return output

        payload = json.dumps({
            "body": "<h1>Generated HTML fixture</h1><script>document.write('JS-EXECUTED')</script>",
            "header": '<div style="font-size:10px">Header fixture</div>',
            "footer": '<div style="font-size:10px">Footer fixture</div>',
            "fileName": "custom", "marginTop": 25, "marginBottom": 20,
        })
        html_args = ("-H", "Content-Type: application/json", "--data-binary", payload)
        word_args = ("-F", f"file=@{docx};type=application/zip")
        compress_args = ("-F", f"file=@{pdf};type=application/pdf", "-F", "quality=55")

        html = request("HtmlToPdf", "html.pdf", *html_args)
        html_text = text(html)
        assert all(label in html_text for label in ("Generated HTML fixture", "Header fixture", "Footer fixture"))
        assert "JS-EXECUTED" not in html_text
        assert "custom.pdf" in (root / "html.pdf.headers").read_text()
        word = request("WordToPdf", "word.pdf", *word_args)
        assert "Generated Word fixture" in text(word)
        compressed = request("Compress", "compressed.pdf", *compress_args)
        assert "VECTOR ONE" in text(compressed) and "VECTOR TWO" in text(compressed)
        # The source contains a path and text, with no images to carry a flattened page.
        assert len(command("pdfimages", "-list", compressed).strip().splitlines()) == 2
        raster = request("ImageCompress", "raster.pdf", *compress_args)
        assert not text(raster).strip()
        assert len(command("pdfimages", "-list", raster).strip().splitlines()) == 4
        assert pages(raster) == [(560.0, 380.0), (240.0, 360.0)], pages(raster)
        merged = request("Merge", "merged.pdf",
                         "-F", f"files=@{word};type=application/pdf", "-F", f"files=@{pdf};type=application/pdf")
        assert len(pages(merged)) == 3
        merged_text = text(merged)
        assert merged_text.index("Generated Word fixture") < merged_text.index("VECTOR ONE") < merged_text.index("VECTOR TWO")
        png_pdf = request("ImageToPdf", "png.pdf", "-F", f"file=@{png};type=image/png")
        jpeg_pdf = request("ImageToPdf", "jpeg.pdf", "-F", f"file=@{jpeg};type=image/jpeg")
        assert pages(png_pdf) == [(144.0, 57.6)], pages(png_pdf)
        assert pages(jpeg_pdf) == [(57.6, 144.0)], pages(jpeg_pdf)

        request("Compress", "wrong-type.json", "-F", f"file=@{pdf};type=text/plain", "-F", "quality=55", expected=415)
        request("ImageToPdf", "wrong-magic.json", "-F", f"file=@{png};type=image/jpeg", expected=415)
        request("Compress", "quality.json", "-F", f"file=@{pdf};type=application/pdf", "-F", "quality=101", expected=400)
        request("Merge", "too-few.json", "-F", f"files=@{pdf};type=application/pdf", expected=400)
        generic_zip = root / "generic.zip"
        with zipfile.ZipFile(generic_zip, "w") as archive:
            archive.writestr("readme.txt", "Not a Word document")
        request("WordToPdf", "wrong-package.json", "-F", f"file=@{generic_zip};type=application/zip", expected=422)
        oversized = root / "oversized.pdf"
        with oversized.open("wb") as large_file:
            large_file.write(b"%PDF-")
            large_file.truncate(60_000_001)
        request("Compress", "large.json", "-F", f"file=@{oversized};type=application/pdf", "-F", "quality=55", expected=413)
        request("Compress", "chunked.json", "-H", "Transfer-Encoding: chunked",
                "-F", f"file=@{oversized};type=application/pdf", "-F", "quality=55", expected=413)
        print("All six routes passed PDF output, validation, and request-size checks.")

        if args.load:
            mix = [
                ("HtmlToPdf", html_args), ("WordToPdf", word_args),
                ("ImageCompress", compress_args),
                ("Merge", ("-F", f"files=@{word};type=application/pdf", "-F", f"files=@{pdf};type=application/pdf")),
            ]

            def workload(index):
                route, arguments = mix[index % len(mix)]
                start = time.monotonic()
                request(route, f"mixed-load-{index}.pdf", *arguments)
                return time.monotonic() - start

            start = time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                latencies = sorted(pool.map(workload, range(4)))
            elapsed = time.monotonic() - start
            p95 = latencies[math.ceil(len(latencies) * 0.95) - 1]
            print(f"Mixed concurrency smoke test passed at {len(latencies) / elapsed:.2f} requests/sec, p95 {p95:.2f}s.")


if __name__ == "__main__":
    main()
