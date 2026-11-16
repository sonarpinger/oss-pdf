# oss-pdf

A self-hosted .NET 10 PDF API with Wolverine HTTP endpoints. See [Design.md](Design.md) for the original requirements.

## Run

```sh
docker build -t oss-pdf .
docker run --rm --init --shm-size=256m -p 127.0.0.1:8080:8080 oss-pdf
```

Ironpress is the default renderer for `/HtmlToPdf`. Set `Pdf__HtmlRenderer=Playwright` on the container to opt in to Chromium. When selected, Chromium runs as a non-root user with its sandbox enabled. The container runtime must allow Chromium to create an unprivileged user namespace. [Playwright Docker guide](https://playwright.dev/dotnet/docs/docker#crawling-and-scraping)

The service is intended for an internal network or an authenticated gateway. Keep the gateway's request size limit aligned with the application's 60 MB limit (60,000,000 bytes for the entire request body). Keep worker scratch storage local and ephemeral. Requests can run for up to 120 seconds by default, including upload time once admitted.

## API

All routes accept `POST`. A successful response is a PDF attachment. Multipart parts must declare the expected media type and have a matching file signature.

| Route | Request |
| --- | --- |
| `/HtmlToPdf` | JSON with required `body` and optional `header`, `footer`, `fileName`, `marginTop`, `marginBottom`, `marginRight`, and `marginLeft`. |
| `/WordToPdf` | Multipart `file` containing a DOCX, with `application/vnd.openxmlformats-officedocument.wordprocessingml.document` or `application/zip`. |
| `/Compress` | Multipart `file` with `application/pdf` and integer `quality` from 1 to 100. qpdf optimizes PDF streams and eligible embedded images while retaining text, vector artwork, and image pixel dimensions. |
| `/Merge` | Two or more multipart `files` parts with `application/pdf`, in output order. |
| `/ImageToPdf` | Multipart `file` with `image/jpeg` or `image/png`. The image is oriented automatically and composited on white. The default embeds the original pixels using Ironpress. Optional `profile=small` embeds a downsampled JPEG in a PDF of the same physical page size. |

All four multipart endpoints accept an optional text field named `fileName`, such as `-F 'fileName=invoice'`. Like the JSON field on `/HtmlToPdf`, it sets the download name in `Content-Disposition`. The API trims surrounding whitespace and adds `.pdf` if the name lacks that extension. Nonblank names are limited to 132 UTF-16 code units before trimming and cannot contain slashes, backslashes, or control characters.

Omitting `fileName` or leaving it blank uses `document.pdf` for HTML and Word, `compressed.pdf` for compression, `merged.pdf` for merges, and `image.pdf` for images.

HTML prints on Letter paper. Margins are in millimeters and default to 12 mm, or 20 mm on a side with a header or footer. Use inline styles and embed any assets in the supplied HTML. JavaScript and external resource loading are disabled. In either renderer, use elements with class `pageNumber` and `totalPages` inside the HTML header or footer to insert page numbers. For example, `Page <span class="pageNumber"></span> of <span class="totalPages"></span>`.

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

The default limits include 200 pages per PDF or merged output, up to 32 files per merge, 25 million pixels per decoded image, 512 MiB of monitored scratch space per job, and 128 MiB per output. Set `Pdf__HtmlConcurrency`, `Pdf__WordConcurrency`, `Pdf__PdfConcurrency`, and `Pdf__RasterConcurrency` to tune parallelism. Defaults are 2 HTML, 2 Word, 2 compression/merge, and 1 image-to-PDF job at a time. Ironpress uses reusable worker processes and terminates the active worker when a conversion is canceled or times out. The service logs queue and conversion timings.

Run the container smoke suite from a host with Python 3, pypdf, curl, and ImageMagick.

```sh
python3 tests/docker_smoke.py http://127.0.0.1:8080 --load
```

Use `--cpus=2 --memory=8g --memory-swap=8g` on the container for a local test with the CPU and RAM budget of a single Azure App Service P1v3 instance.

### HTML-to-PDF throughput

Run the k6 benchmark from a Linux host with cgroup v2, k6, and Python with `pypdf` and Pillow installed. The runner starts a fresh container for each fixture. It uses two CPUs, 8 GiB of memory, no swap, and two HTML conversion slots. The fixtures are a one-page letter, a two-page letter with page numbers, an eight-page report, and a six-page report with 12 embedded images.

```sh
docker build -t oss-pdf:html-benchmark .
for renderer in ironpress playwright; do
  python3 tests/html_benchmark/run.py --renderer "$renderer" \
    --fixture letter --fixture two-page-letter --probe-rps 40 --probe-rps 60 \
    --max-rps 180 --output "untracked/html-renderer-benchmark/$renderer-letters"
  python3 tests/html_benchmark/run.py --renderer "$renderer" \
    --fixture image-report --max-rps 16 \
    --output "untracked/html-renderer-benchmark/$renderer-image-report"
done
```

The runner measures the first conversion, validates page counts and fixture contents, warms up the selected renderer, and probes fixed arrival rates. It repeats the highest passing rate before a final steady run. A run passes with at least 99% valid PDFs, zero dropped iterations, zero OOM kills, and stable renderer processes in the samples. Use `--p95-ms` to add a latency target. The defaults are 30-second warmups, two-minute probes, three five-minute confirmations, and a 15-minute steady run. The result directory includes Markdown and JSON reports, input fixtures, and individual k6 summaries.

#### Ironpress and Playwright on the local P1v3-equivalent container

k6 v2.4.0 ran on an ARM64 Apple M1 Pro host with Podman. Each fixture used a 10-second warmup, 8-second probes, two 20-second confirmations, and a 60-second final run. The table shows the highest tested offered rate that passed the final run and p95 latency for valid responses.

| Fixture | Renderer | Offered req/s | Valid PDFs/s | p95 | Valid responses |
| --- | --- | ---: | ---: | ---: | ---: |
| One-page letter | Ironpress | 220 | 218.03 | 509 ms | 99.11% |
| One-page letter | Playwright | 41 | 40.72 | 543 ms | 99.31% |
| Two-page letter with page numbers | Ironpress | 84 | 83.62 | 520 ms | 99.54% |
| Two-page letter with page numbers | Playwright | 27 | 27.00 | 350 ms | 100% |
| Six-page report with 12 embedded images | Ironpress | 11 | 10.93 | 632 ms | 99.39% |
| Six-page report with 12 embedded images | Playwright | 2 | 2.00 | 482 ms | 100% |

Ironpress returned a valid PDF for every request in the 40 and 60 req/s probes for both letter fixtures. At 60 req/s, p95 was 19 ms for the one-page letter and 36 ms for the numbered two-page letter. The one-page letter also held 180 req/s at 17 ms p95 in a 60-second run. Additional probes at 280 and 360 req/s missed the 99% threshold. To reproduce the limit search, run the Ironpress letter fixture with `--probe-rps 280 --probe-rps 360 --max-rps 480`.

At a matched 2 req/s in the image-report probes, p95 was 259 ms for Ironpress and 509 ms for Playwright. The host ran out of memory during the first Playwright image-report final run. The table uses a fresh-container retry at 2 req/s with the same limits and durations.

#### Earlier Playwright baseline

On November 15, 2026, k6 v2.4.0 tested each fixture separately against a local ARM64 container running on Podman with two CPUs, 8 GiB of RAM, swap disabled, and two HTML conversion slots. This run used a 30-second warmup, 12-second probes, three 20-second confirmation runs, and a 90-second final run per fixture.

| Fixture | Offered requests/sec | Valid PDFs/sec | p95 latency | Valid responses |
| --- | ---: | ---: | ---: | ---: |
| One-page letter | 40 | 39.69 | 545 ms | 99.22% |
| Eight-page report | 21 | 21.00 | 213 ms | 100% |
| Six-page report with 12 embedded images | 2 | 2.00 | 789 ms | 100% |

Each row shows the highest tested whole-number rate that passed the final run. Latency is measured for valid PDF responses, and k6 dropped zero iterations at these rates. The letter returned 28 HTTP 503 responses at 40 requests/sec. The image-heavy report returned 98.15% valid PDFs at 3 requests/sec, below the 99% threshold. At 2 requests/sec, it peaked at 5.46 GiB of container memory. Long-duration memory stability still needs a longer run.

### Image-to-PDF throughput

The benchmark compares the original ImageMagick PDF writer with the Ironpress path. The Ironpress path uses ImageMagick to orient the image, composite transparency on white, and produce an RGB PNG for the PDF writer. Run on a Linux host with cgroup v2, k6, curl, `pdftoppm`, and Python with `pypdf` and Pillow installed.

```sh
git archive b06b5dde527d6d6d1a701dfdb29525574fb62ec7 | docker build -t oss-pdf:image-imagemagick -
docker build -t oss-pdf:image-ironpress .
python3 tests/html_benchmark/run_image.py \
  --baseline oss-pdf:image-imagemagick --candidate oss-pdf:image-ironpress
```

The runner uses two CPUs, 8 GiB of memory, zero swap, and one raster slot. It tests a rotated JPEG and a transparent PNG, checks one-page output, 150 DPI dimensions and rendered pixels, and confirms the highest passing rate from the selected load steps. Raw k6 results and sample PDFs are saved under `untracked/image-to-pdf-benchmark` by default.

On the local ARM64 Podman host, the highest passing load steps below each returned valid PDFs for all requests in the 30-second run. This comparison used 5-second warmups, 6-second probes, and `--rates 2 4 8 10 12 14 16 20 24 32 48 64`.

| Fixture | ImageMagick writer | Ironpress writer |
| --- | ---: | ---: |
| Rotated JPEG, 800 × 1200 px after rotation | 12 PDFs/s, 95 ms p95 | 8 PDFs/s, 154 ms p95 |
| Transparent PNG, 640 × 480 px | 10 PDFs/s, 130 ms p95 | 16 PDFs/s, 113 ms p95 |

At a matched 10 requests/s for the PNG, p95 was 128 ms with ImageMagick and 82 ms with Ironpress. The sample JPEG PDF grew from 390 KB to 1.26 MB because the Ironpress path stores the normalized pixels as PNG.

### Small image PDF profile

The profile defaults to a finished PDF target of 100,000 bytes. Add the multipart field `targetBytes` with a positive integer to choose another target. This field requires `profile=small`. [250 KB curl example](#imagetopdf)

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

### Scan compression comparison

To compare `/Compress` against a baseline, install Pillow alongside pypdf and run the benchmark with both containers listening on separate ports. It generates three synthetic 300 DPI scans and measures output bytes and embedded image dimensions at three quality levels.

```sh
python3 tests/compare_scan_compression.py http://127.0.0.1:18081 http://127.0.0.1:18080
```

## Licensing

The API source is available under the [MIT License](LICENSE). qpdf is [Apache 2.0 licensed](https://qpdf.readthedocs.io/en/stable/license.html) and ImageMagick uses the [ImageMagick License](https://imagemagick.org/license/). The image includes their notices and Ubuntu package notices under `/usr/local/share/doc` and `/usr/share/doc`. The Docker build repackages Ubuntu's LibreOffice core to omit its optional Poppler-based PDF importer. Writer still exports DOCX to PDF. Use the exact versions installed in a distributed image when making corresponding third-party source available, and provide recipients and service users access to the corresponding application source as required by the applicable licenses.

Ironpress and Html Agility Pack use the MIT License. Their notices are included under `/usr/local/share/doc/oss-pdf`. SkiaSharp's license and third-party notices are under `/usr/local/share/doc/skiasharp`. [Ironpress license](https://github.com/gastongouron/ironpress/blob/v1.7.0/LICENSE) · [Html Agility Pack license](https://github.com/zzzprojects/html-agility-pack/blob/v1.13.1/LICENSE)

The independently implemented upload and conversion flow was informed by [pdf2img](https://github.com/joanfabregat/pdf2img), an MIT-licensed reference.
