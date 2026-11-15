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
| `/Compress` | Multipart `file` with `application/pdf` and integer `quality` from 1 to 100. Text and vector artwork are retained. |
| `/ImageCompress` | Multipart `file` with `application/pdf` and integer `quality` from 1 to 100. Pages become images at 150 DPI. |
| `/Merge` | Two or more multipart `files` parts with `application/pdf`, in output order. |
| `/ImageToPdf` | Multipart `file` with `image/jpeg` or `image/png`. The image is oriented automatically and placed on one page at 150 DPI. |

HTML prints on Letter paper. Margins are in millimeters and default to 12 mm, or 20 mm on a side with a header or footer. Use inline styles and embed any assets in the supplied HTML. JavaScript and external resource loading are disabled. Higher compression quality favors fidelity. Some inputs can produce a larger output.

```sh
curl --fail-with-body -o compressed.pdf \
  -F 'file=@input.pdf;type=application/pdf' -F 'quality=65' \
  http://127.0.0.1:8080/Compress
```

Errors use HTTP status codes and problem details where a response can be sent. Use `400` for invalid fields, `413` for oversized bodies, `415` for unsupported media types or mismatched signatures, and `422` for unsupported document contents or conversion limits. Busy converters respond with `503` and `Retry-After`. Conversion timeouts return `504`.

## Limits and validation

The default limits include 200 pages per PDF or merged output, up to 32 files per merge, 25 million pixels per decoded image, 200 million pixels per raster job, 512 MiB of monitored scratch space per job, and 128 MiB per output. Set `Pdf__HtmlConcurrency`, `Pdf__WordConcurrency`, `Pdf__PdfConcurrency`, and `Pdf__RasterConcurrency` to tune parallelism. Defaults are 2 HTML, 2 Word, 2 compression/merge, and 1 raster/image job at a time. The service logs queue and conversion timings.

Run the container smoke suite from a host with Python 3, curl, Poppler utilities, and ImageMagick.

```sh
python3 tests/docker_smoke.py http://127.0.0.1:8080 --load
```

Use `--cpus=2 --memory=8g --memory-swap=8g` on the container for a local test with the CPU and RAM budget of a single Azure App Service P1v3 instance.

## Licensing

The API source is available under the [MIT License](LICENSE). Ghostscript is used under its AGPL v3 terms. Review [Artifex's licensing terms](https://artifex.com/licensing) when distributing or hosting a derivative. ImageMagick is distributed under the [ImageMagick License](https://imagemagick.org/license/). The image retains Ubuntu package copyright notices under `/usr/share/doc`. Use the exact Ubuntu package versions installed in a distributed image when making corresponding third-party source available, and provide recipients and service users access to the corresponding application source as required by the applicable licenses.

The independently implemented upload and conversion flow was informed by [pdf2img](https://github.com/joanfabregat/pdf2img), an MIT-licensed reference.
