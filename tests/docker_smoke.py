#!/usr/bin/env python3
"""Exercise a running container using curl, pypdf, and ImageMagick."""

import argparse
import concurrent.futures
import json
import math
import re
import struct
import subprocess
import tempfile
import time
import unicodedata
import zipfile
import zlib
from pathlib import Path
from urllib.parse import unquote

from pypdf import PdfReader, PdfWriter


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
    return [(float(page.mediabox.width), float(page.mediabox.height)) for page in PdfReader(path).pages]


def text(path):
    return unicodedata.normalize("NFKC", "\n".join(page.extract_text() for page in PdfReader(path).pages))


def image_count(path):
    return sum(len(page.images) for page in PdfReader(path).pages)


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

        def request(route, name, *arguments, expected=200, download_name=None):
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
                response_headers = headers.read_text()
                assert "content-type: application/pdf" in response_headers.lower(), route
                if download_name is not None:
                    match = re.search(r"^Content-Disposition: attachment;[^\n]*\bfilename\*=UTF-8''([^\s;]+)",
                                      response_headers, re.IGNORECASE | re.MULTILINE)
                    assert match and unquote(match[1]) == download_name, (route, download_name, response_headers)
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

        html = request("HtmlToPdf", "html.pdf", *html_args, download_name="custom.pdf")
        html_text = text(html)
        assert all(label in html_text for label in ("Generated HTML fixture", "Header fixture", "Footer fixture"))
        assert "JS-EXECUTED" not in html_text
        numbered_payload = json.dumps({
            "body": '<p>First sheet</p><p style="break-before:page">Second sheet</p>',
            "header": '<div style="font:10px sans-serif">Sheet <strong class="another pageNumber">x</strong></div>',
            "footer": '<div style="font:10px sans-serif">Page <span class="pageNumber"></span>'
                      ' of <span class="totalPages"></span></div>',
        })
        numbered = request("HtmlToPdf", "numbered.pdf", "-H", "Content-Type: application/json",
                           "--data-binary", numbered_payload, download_name="document.pdf")
        assert pages(numbered) == [(612.0, 792.0)] * 2
        for number, page in enumerate(PdfReader(numbered).pages, 1):
            page_text = " ".join(page.extract_text().split())
            assert f"Sheet {number}" in page_text and f"Page {number} of 2" in page_text, page_text
            assert f"{['First', 'Second'][number - 1]} sheet" in page_text, page_text
        word = request("WordToPdf", "word.pdf", *word_args, download_name="document.pdf")
        assert "Generated Word fixture" in text(word)
        compressed = request("Compress", "compressed.pdf", *compress_args, download_name="compressed.pdf")
        assert "VECTOR ONE" in text(compressed) and "VECTOR TWO" in text(compressed)
        # The source contains a path and text, with no images to carry a flattened page.
        assert image_count(compressed) == 0
        request("ImageCompress", "retired-route.json", *compress_args, expected=404)
        merge_args = ("-F", f"files=@{word};type=application/pdf", "-F", f"files=@{pdf};type=application/pdf")
        merged = request("Merge", "merged.pdf", *merge_args, download_name="merged.pdf")
        assert len(pages(merged)) == 3
        merged_text = text(merged)
        assert merged_text.index("Generated Word fixture") < merged_text.index("VECTOR ONE") < merged_text.index("VECTOR TWO")
        png_args = ("-F", f"file=@{png};type=image/png")
        png_pdf = request("ImageToPdf", "png.pdf", *png_args, download_name="image.pdf")
        jpeg_pdf = request("ImageToPdf", "jpeg.pdf", "-F", f"file=@{jpeg};type=image/jpeg")
        assert pages(png_pdf) == [(144.0, 57.6)], pages(png_pdf)
        assert pages(jpeg_pdf) == [(57.6, 144.0)], pages(jpeg_pdf)
        for source, mime, original in ((png, "image/png", png_pdf), (jpeg, "image/jpeg", jpeg_pdf)):
            small = request("ImageToPdf", f"small-{source.stem}{source.suffix}.pdf",
                            "-F", "profile=small", "-F", f"file=@{source};type={mime}", download_name="image.pdf")
            assert pages(small) == pages(original)
            assert small.stat().st_size <= 100_000
            assert image_count(small) == 1
        request("ImageToPdf", "invalid-profile.json", "-F", f"file=@{png};type=image/png",
                "-F", "profile=other", expected=400)
        request("ImageToPdf", "duplicate-profile.json", "-F", f"file=@{png};type=image/png",
                "-F", "profile=small", "-F", "profile=small", expected=400)
        larger = request("ImageToPdf", "larger-target.pdf", "-F", "targetBytes=250000",
                         "-F", f"file=@{jpeg};type=image/jpeg", "-F", "profile=small")
        assert pages(larger) == pages(jpeg_pdf)
        assert larger.stat().st_size <= 250_000
        best_effort = request("ImageToPdf", "tiny-target.pdf", "-F", f"file=@{png};type=image/png",
                              "-F", "profile=small", "-F", "targetBytes=1")
        assert pages(best_effort) == pages(png_pdf)
        assert best_effort.stat().st_size > 1
        request("ImageToPdf", "target-without-profile.json", "-F", f"file=@{png};type=image/png",
                "-F", "targetBytes=250000", expected=400)
        for invalid in ("", "0", "-1", "2.5", "9223372036854775808"):
            request("ImageToPdf", "invalid-target.json", "-F", f"file=@{png};type=image/png",
                    "-F", "profile=small", "-F", f"targetBytes={invalid}", expected=400)
        request("ImageToPdf", "duplicate-target.json", "-F", f"file=@{png};type=image/png",
                "-F", "profile=small", "-F", "targetBytes=100000", "-F", "targetBytes=250000", expected=400)

        for route, arguments, supplied, expected in (
            ("WordToPdf", word_args, "  Quarterly report  ", "Quarterly report.pdf"),
            ("Compress", compress_args, "reduced.PDF", "reduced.PDF"),
            ("Merge", merge_args, "Résumé 合本", "Résumé 合本.pdf"),
            ("ImageToPdf", (*png_args, "-F", "profile=small", "-F", "targetBytes=250000"), "photo", "photo.pdf"),
        ):
            request(route, f"{route}-named.pdf", "--form-string", f"fileName={supplied}", *arguments,
                    download_name=expected)
            request(route, f"{route}-invalid-name.json", *arguments, "--form-string", "fileName=../invalid", expected=400)
        for blank in ("", "\t  ", " " * 133):
            request("ImageToPdf", "blank-name.pdf", *png_args, "--form-string", f"fileName={blank}",
                    download_name="image.pdf")
        for invalid in ("bad\\name", "bad\tname", "a" * 133):
            request("ImageToPdf", "invalid-name.json", *png_args, "--form-string", f"fileName={invalid}", expected=400)
        max_name = "é" * 132
        request("ImageToPdf", "max-name.pdf", *png_args, "--form-string", f"fileName={max_name}",
                download_name=max_name + ".pdf")
        request("ImageToPdf", "duplicate-name.json", *png_args, "--form-string", "fileName=",
                "--form-string", "fileName=second", expected=400)

        request("Compress", "wrong-type.json", "-F", f"file=@{pdf};type=text/plain", "-F", "quality=55", expected=415)
        request("Compress", "non-multipart.json", "-H", "Content-Type: application/json",
                "--data-binary", "{}", expected=415)
        for header in ("multipart/form-data", 'multipart/form-data; boundary=""',
                       "multipart/form-data; boundary=" + "x" * 129):
            request("Compress", "invalid-boundary.json", "-H", f"Content-Type: {header}",
                    "--data-binary", "", expected=400)
        boundary = "x" * 128
        multipart = root / "multipart.bin"
        multipart.write_bytes(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="sample.pdf"\r\n'
            'Content-Type: application/pdf\r\n\r\n'.encode()
            + pdf.read_bytes()
            + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="quality"\r\n\r\n55\r\n--{boundary}--\r\n'.encode()
        )
        request("Compress", "quoted-boundary.pdf", "-H", f'Content-Type: multipart/form-data; boundary="{boundary}"',
                "--data-binary", f"@{multipart}", download_name="compressed.pdf")
        request("ImageToPdf", "wrong-magic.json", "-F", f"file=@{png};type=image/jpeg", expected=415)
        request("Compress", "quality.json", "-F", f"file=@{pdf};type=application/pdf", "-F", "quality=101", expected=400)
        encrypted = root / "encrypted.pdf"
        writer = PdfWriter(clone_from=pdf)
        writer.encrypt("", "test owner", algorithm="RC4-128")
        writer.write(encrypted)
        request("Compress", "encrypted.json", "-F", f"file=@{encrypted};type=application/pdf",
                "-F", "quality=55", expected=422)
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
        print("All five routes passed PDF output, validation, and request-size checks. /ImageCompress returned 404.")
        print("All five routes returned the expected default and custom download names.")

        if args.load:
            mix = [
                ("HtmlToPdf", html_args), ("WordToPdf", word_args),
                ("Compress", compress_args),
                ("Merge", ("-F", f"files=@{word};type=application/pdf", "-F", f"files=@{pdf};type=application/pdf")),
                ("ImageToPdf", ("-F", f"file=@{png};type=image/png", "-F", "profile=small")),
            ]

            def workload(index):
                route, arguments = mix[index]
                start = time.monotonic()
                request(route, f"mixed-load-{index}.pdf", *arguments)
                return time.monotonic() - start

            start = time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                latencies = sorted(pool.map(workload, range(len(mix))))
            elapsed = time.monotonic() - start
            p95 = latencies[math.ceil(len(latencies) * 0.95) - 1]
            print(f"Mixed concurrency smoke test passed at {len(latencies) / elapsed:.2f} requests/sec, p95 {p95:.2f}s.")


if __name__ == "__main__":
    main()
