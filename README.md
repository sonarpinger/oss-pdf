# oss-pdf

A self-hosted .NET 10 PDF API with ASP.NET Core Minimal API endpoints. Designed to run most business required flows while using software with permissible licensing. The ideal case being a central PDF API deployed internally at an organization. Optimized for throughput.

## Run

```sh
docker build -t oss-pdf .
docker run --rm --init -p 127.0.0.1:8080:8080 oss-pdf
```

## API

All routes accept `POST`. A successful response is a PDF attachment. Multipart parts must declare the expected media type and have a matching file signature.

| Route | Request |
| --- | --- |
| `/HtmlToPdf` | JSON with required `body` and optional `header`, `footer`, `fileName`, `marginTop`, `marginBottom`, `marginRight`, and `marginLeft`. |
| `/WordToPdf` | Multipart `file` containing a DOCX, with `application/vnd.openxmlformats-officedocument.wordprocessingml.document` or `application/zip`. |
| `/Compress` | Multipart `file` with `application/pdf` and integer `quality` from 1 to 100. qpdf optimizes PDF streams and eligible embedded images while retaining text, vector artwork, and image pixel dimensions. |
| `/Merge` | Two or more multipart `files` parts with `application/pdf`, in output order. |
| `/ImageToPdf` | Multipart `file` with `image/jpeg` or `image/png`. The image is oriented automatically and composited on white. The default embeds the original pixels using Ironpress. Optional `profile=small` embeds a downsampled JPEG in a PDF of the same physical page size. |

All four multipart endpoints accept an optional text field named `fileName`, such as `-F 'fileName=invoice'`. Like the JSON field on `/HtmlToPdf`, it sets the download name in `Content-Disposition`. The API trims surrounding whitespace and adds `.pdf` if the name lacks that extension.

Omitting `fileName` or leaving it blank uses `document.pdf` for HTML and Word, `compressed.pdf` for compression, `merged.pdf` for merges, and `image.pdf` for images.

HTML prints on Letter paper. Margins are in millimeters and default to 12 mm, or 20 mm on a side with a header or footer. Use inline styles and embed any assets in the supplied HTML. JavaScript and external resource loading are disabled. Use elements with class `pageNumber` and `totalPages` inside the HTML header or footer to insert page numbers. For example, `Page <span class="pageNumber"></span> of <span class="totalPages"></span>`.

Higher compression quality favors fidelity. For scans, `/Compress` preserves the pixel dimensions of embedded images. Some inputs can produce a larger output. To downsample a JPEG or PNG while creating a PDF, use `profile=small` on `/ImageToPdf`.

Errors use HTTP status codes and problem details where a response can be sent. Use `400` for invalid fields, `413` for oversized bodies, `415` for unsupported media types or mismatched signatures, and `422` for unsupported document contents or conversion limits. Busy converters respond with `503` and `Retry-After`. Conversion timeouts return `504`.

### curl examples

Run these commands from a directory containing the sample input files. The `-OJ` option saves each PDF in the current directory using the filename from the response header.

#### /HtmlToPdf

```sh
curl --fail-with-body -OJ \
  -H 'Content-Type: application/json' \
  --data-binary '{"body":"<h1>Invoice</h1>","fileName":"invoice"}' \
  http://127.0.0.1:8080/HtmlToPdf
```

#### /WordToPdf

```sh
curl --fail-with-body -OJ \
  -F 'file=@report.docx;type=application/vnd.openxmlformats-officedocument.wordprocessingml.document' \
  --form-string 'fileName=word-report' \
  http://127.0.0.1:8080/WordToPdf
```

#### /Compress

```sh
curl --fail-with-body -OJ \
  -F 'file=@input.pdf;type=application/pdf' \
  -F 'quality=65' \
  --form-string 'fileName=reduced.pdf' \
  http://127.0.0.1:8080/Compress
```

#### /Merge

```sh
curl --fail-with-body -OJ \
  -F 'files=@first.pdf;type=application/pdf' \
  -F 'files=@second.pdf;type=application/pdf' \
  --form-string 'fileName=combined' \
  http://127.0.0.1:8080/Merge
```

#### /ImageToPdf

Use `profile=small` with `targetBytes=250000` for a best-effort 250 KB target.

```sh
curl --fail-with-body -OJ \
  -F 'file=@photo.png;type=image/png' \
  -F 'profile=small' \
  -F 'targetBytes=250000' \
  --form-string 'fileName=photo-small' \
  http://127.0.0.1:8080/ImageToPdf
```

## Limits and validation

The default limits include 200 pages per PDF or merged output, up to 32 files per merge, 25 million pixels per decoded image, 512 MiB of monitored scratch space per job, and 128 MiB per output. Set `Pdf__HtmlConcurrency`, `Pdf__WordConcurrency`, `Pdf__PdfConcurrency`, and `Pdf__RasterConcurrency` to tune parallelism. Defaults are 2 HTML, 2 Word, 2 compression/merge, and 1 image-to-PDF job at a time.

Run the container smoke suite from a host with Python 3, pypdf, curl, and ImageMagick.

```sh
python3 tests/docker_smoke.py http://127.0.0.1:8080 --load
```

#### HtmlToPdf benchmark on a local container

Run the Ironpress benchmark from a Linux host with cgroup v2, k6, pypdf, and Pillow.

```sh
docker build -t oss-pdf:html-benchmark .
python3 tests/html_benchmark/run.py --fixture two-page-letter --fixture image-report
```

These results where recorded on a local container modeled off of a P1v3 web application container (`--cpus=2 --memory=8g`) 

| Fixture | Offered req/s | Valid PDFs/s | p95 | Valid responses |
| --- | ---: | ---: | ---: | ---: |
| One-page letter | 220 | 218.03 | 509 ms | 99.11% |
| Two-page letter with page numbers | 84 | 83.62 | 520 ms | 99.54% |
| Six-page report with 12 embedded images | 11 | 10.93 | 632 ms | 99.39% |

### Small image PDF profile

The profile defaults to a finished PDF target of 100,000 bytes. Add the multipart field `targetBytes` with a positive integer to choose another target. This field requires `profile=small`.

The profile preserves the default page size calculated from the oriented image at 150 DPI while reducing the number of embedded pixels. It starts with a longest side of up to 1,600 pixels and JPEG quality 76, then tries smaller dimensions with a floor of 640 pixels on the longest side, or the original size for smaller inputs. The final fallback uses JPEG quality 50. The target is best effort because the PDF can exceed it at those floors.

The benchmark compares one `profile=small` request with a default `/ImageToPdf` request followed by `/Compress`. Each job measures the full sequence from upload to final PDF.

```sh
docker build -t oss-pdf:small-image-profile .
python3 tests/html_benchmark/run_image.py --compare-small \
  --baseline oss-pdf:small-image-profile --candidate oss-pdf:small-image-profile \
  --round-trip-quality 10 --rates 0.25 --steady-seconds 80 \
  --output untracked/image-to-pdf-comparison
```

In an 80-second run on the local ARM64 Podman host, both paths returned 20 valid PDFs at 0.25 jobs/s using two CPUs, 8 GiB RAM, no swap, and one image conversion slot.

| Fixture | Default then `/Compress` at quality 10 | `profile=small` |
| --- | --- | --- |
| 22.05 MB transparent PNG | 615.5 KB PDF · 1,367 ms p95 | 93.7 KB PDF · 561 ms p95 |

## Licensing

The API source is available under the [MIT License](LICENSE). qpdf is [Apache 2.0 licensed](https://qpdf.readthedocs.io/en/stable/license.html) and ImageMagick uses the [ImageMagick License](https://imagemagick.org/license/). The image includes their notices and Ubuntu package notices under `/usr/local/share/doc` and `/usr/share/doc`. The Docker build repackages Ubuntu's LibreOffice core to omit its optional Poppler-based PDF importer. Writer still exports DOCX to PDF. Use the exact versions installed in a distributed image when making corresponding third-party source available, and provide recipients and service users access to the corresponding application source as required by the applicable licenses.

Ironpress and Html Agility Pack use the MIT License. Their notices are included under `/usr/local/share/doc/oss-pdf`. SkiaSharp's license and third-party notices are under `/usr/local/share/doc/skiasharp`. [Ironpress license](https://github.com/gastongouron/ironpress/blob/v1.7.0/LICENSE) · [Html Agility Pack license](https://github.com/zzzprojects/html-agility-pack/blob/v1.13.1/LICENSE)
