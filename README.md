# oss-pdf

A self-hosted .NET 10 PDF API with Wolverine HTTP endpoints. See [Design.md](Design.md) for the original requirements.

## Run

```sh
docker build -t oss-pdf .
docker run --rm --init --shm-size=256m -p 127.0.0.1:8080:8080 oss-pdf
```

Chromium runs as a non-root user with its sandbox enabled. The container runtime must allow Chromium to create an unprivileged user namespace. On Docker hosts that block it by default, use a seccomp profile that allows it, as described in the [Playwright Docker guide](https://playwright.dev/dotnet/docs/docker#crawling-and-scraping).

The service is intended for an internal network or an authenticated gateway. Keep the gateway's request size limit aligned with the application's 60 MB limit (60,000,000 bytes for the entire request body). Keep worker scratch storage local and ephemeral. Requests can run for up to 120 seconds by default, including upload time once admitted.

## API

All routes accept `POST`. A successful response is a PDF attachment. Multipart parts must declare the expected media type and have a matching file signature.

| Route | Request |
| --- | --- |
| `/HtmlToPdf` | JSON with required `body` and optional `header`, `footer`, `fileName`, `marginTop`, `marginBottom`, `marginRight`, and `marginLeft`. |
| `/WordToPdf` | Multipart `file` containing a DOCX, with `application/vnd.openxmlformats-officedocument.wordprocessingml.document` or `application/zip`. |
| `/Compress` | Multipart `file` with `application/pdf` and integer `quality` from 1 to 100. qpdf optimizes PDF streams and eligible embedded images while retaining text, vector artwork, and image pixel dimensions. |
| `/ImageCompress` | Multipart `file` with `application/pdf` and integer `quality` from 1 to 100. Pages become images at 150 DPI. |
| `/Merge` | Two or more multipart `files` parts with `application/pdf`, in output order. |
| `/ImageToPdf` | Multipart `file` with `image/jpeg` or `image/png`. The image is oriented automatically and placed on one page at 150 DPI. |

HTML prints on Letter paper. Margins are in millimeters and default to 12 mm, or 20 mm on a side with a header or footer. Use inline styles and embed any assets in the supplied HTML. JavaScript and external resource loading are disabled. Higher compression quality favors fidelity. For scans, `/Compress` preserves the pixel dimensions of embedded images. Use `/ImageCompress` to render the full page at 150 DPI. Some inputs can produce a larger output.

```sh
curl --fail-with-body -o compressed.pdf \
  -F 'file=@input.pdf;type=application/pdf' -F 'quality=65' \
  http://127.0.0.1:8080/Compress
```

Errors use HTTP status codes and problem details where a response can be sent. Use `400` for invalid fields, `413` for oversized bodies, `415` for unsupported media types or mismatched signatures, and `422` for unsupported document contents or conversion limits. Busy converters respond with `503` and `Retry-After`. Conversion timeouts return `504`.

## Limits and validation

The default limits include 200 pages per PDF or merged output, up to 32 files per merge, 25 million pixels per decoded image, 200 million pixels per raster job, 512 MiB of monitored scratch space per job, and 128 MiB per output. Set `Pdf__HtmlConcurrency`, `Pdf__WordConcurrency`, `Pdf__PdfConcurrency`, and `Pdf__RasterConcurrency` to tune parallelism. Defaults are 2 HTML, 2 Word, 2 compression/merge, and 1 raster/image job at a time. The service logs queue and conversion timings.

Run the container smoke suite from a host with Python 3, pypdf, curl, and ImageMagick.

```sh
python3 tests/docker_smoke.py http://127.0.0.1:8080 --load
```

Use `--cpus=2 --memory=8g --memory-swap=8g` on the container for a local test with the CPU and RAM budget of a single Azure App Service P1v3 instance.

### HTML-to-PDF throughput

Run the k6 benchmark from a Linux host with cgroup v2, k6, and Python with `pypdf` and Pillow installed. The runner starts one container and renders only `/HtmlToPdf` for each of three generated fixtures. It uses two CPUs, 8 GiB of memory, no swap, and two HTML conversion slots.

```sh
docker build -t oss-pdf:html-benchmark .
python3 tests/html_benchmark/run.py --output untracked/html-to-pdf-benchmark
```

The runner measures the first conversion, warms up Chromium, probes fixed arrival rates, and repeats the highest passing rate before a final steady run. A run passes with at least 99% valid PDFs, zero dropped iterations, zero OOM kills, and no observed Chromium restart. Use `--p95-ms` to add a latency target and `--fixture` to run a subset. The defaults are 30-second warmups, two-minute probes, three five-minute confirmations, and a 15-minute steady run. The result directory includes Markdown and JSON reports, input fixtures, and individual k6 summaries.

#### Local P1v3-equivalent results

On November 15, 2026, k6 v2.4.0 tested each fixture separately against a local ARM64 container running on Podman with two CPUs, 8 GiB of RAM, swap disabled, and two HTML conversion slots. This run used a 30-second warmup, 12-second probes, three 20-second confirmation runs, and a 90-second final run per fixture.

| Fixture | Offered requests/sec | Valid PDFs/sec | p95 latency | Valid responses |
| --- | ---: | ---: | ---: | ---: |
| One-page letter | 40 | 39.69 | 545 ms | 99.22% |
| Eight-page report | 21 | 21.00 | 213 ms | 100% |
| Six-page report with 12 embedded images | 2 | 2.00 | 789 ms | 100% |

Each row shows the highest tested whole-number rate that passed the final run. Latency is measured for valid PDF responses, and k6 dropped zero iterations at these rates. The letter returned 28 HTTP 503 responses at 40 requests/sec. The image-heavy report returned 98.15% valid PDFs at 3 requests/sec, below the 99% threshold. At 2 requests/sec, it peaked at 5.46 GiB of container memory. Long-duration memory stability still needs a longer run.

### Scan compression comparison

To compare both compression endpoints against a baseline, install Pillow alongside pypdf and run the benchmark with both containers listening on separate ports. It generates three synthetic 300 DPI scans and measures output bytes and embedded image dimensions at three quality levels.

```sh
python3 tests/compare_scan_compression.py http://127.0.0.1:18081 http://127.0.0.1:18080
```

## Licensing

The API source is available under the [MIT License](LICENSE). qpdf is [Apache 2.0 licensed](https://qpdf.readthedocs.io/en/stable/license.html), PDFium is [BSD 3-Clause licensed](https://pdfium.googlesource.com/pdfium/+/main/LICENSE), and ImageMagick uses the [ImageMagick License](https://imagemagick.org/license/). The image includes their notices and Ubuntu package notices under `/usr/local/share/doc` and `/usr/share/doc`. The Docker build repackages Ubuntu's LibreOffice core to omit its optional Poppler-based PDF importer. Writer still exports DOCX to PDF. Use the exact versions installed in a distributed image when making corresponding third-party source available, and provide recipients and service users access to the corresponding application source as required by the applicable licenses.

The independently implemented upload and conversion flow was informed by [pdf2img](https://github.com/joanfabregat/pdf2img), an MIT-licensed reference.
