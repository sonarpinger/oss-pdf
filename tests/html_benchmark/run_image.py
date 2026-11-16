#!/usr/bin/env python3
"""Compare /ImageToPdf on two container images using the same k6 workload."""

import argparse
import base64
import json
import math
import subprocess
import uuid
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps
from pypdf import PdfReader

from fixtures import embedded_image
from run import await_http, cgroup_path, command, image_draws, run_stage


def write_fixtures(directory):
    photo = Image.open(BytesIO(base64.b64decode(embedded_image(4).split(",", 1)[1])))
    draw = ImageDraw.Draw(photo)
    draw.rectangle((0, 0, 180, 180), fill="#d02040")
    draw.rectangle((1020, 620, 1199, 799), fill="#2040d0")
    exif = Image.Exif()
    exif[274] = 6
    jpeg = directory / "oriented-photo.jpg"
    photo.save(jpeg, quality=90, exif=exif)

    art = Image.new("RGBA", (640, 480))
    layer = photo.resize((560, 400)).convert("RGBA")
    layer.putalpha(128)
    art.paste(layer, (40, 40))
    png = directory / "transparent-art.png"
    art.save(png)
    return (jpeg, "image/jpeg"), (png, "image/png")


def verify_pdf(url, fixture, mime, destination):
    subprocess.run(["curl", "--fail-with-body", "--silent", "--show-error", "--max-time", "120",
                    "--form", f"file=@{fixture};type={mime}", "--output", str(destination), f"{url}/ImageToPdf"],
                   check=True)
    pages = PdfReader(destination).pages
    assert len(pages) == 1 and image_draws(pages[0]) == 1, destination
    with Image.open(fixture) as image:
        oriented = ImageOps.exif_transpose(image).convert("RGBA")
    expected = Image.alpha_composite(Image.new("RGBA", oriented.size, "white"), oriented).convert("RGB")
    bounds = pages[0].mediabox
    assert all(math.isclose(float(pts), pixels * 72 / 150, abs_tol=0.01)
               for pts, pixels in zip((bounds.width, bounds.height), expected.size)), bounds
    assert len(pages[0].images) == 1 and pages[0].images[0].image.size == expected.size, destination

    # Rasterize the full page so the checks also cover rotation and image placement.
    width, height = expected.size
    raster = subprocess.check_output(["pdftoppm", "-scale-to-x", str(width), "-scale-to-y", str(height),
                                      "-png", "-singlefile", str(destination)])
    with Image.open(BytesIO(raster)) as image:
        rendered = image.convert("RGB")
    assert rendered.size == expected.size
    for x, y in ((0.025, 0.025), (0.95, 0.025), (0.5, 0.5), (0.025, 0.95), (0.95, 0.95)):
        point = (int(x * width), int(y * height))
        actual, reference = rendered.getpixel(point), expected.getpixel(point)
        assert max(abs(a - b) for a, b in zip(actual, reference)) <= 16, (destination, point, actual, reference)
    return list(expected.size)


def benchmark(args, image, fixture, mime):
    directory = args.output / args.renderer / fixture.stem
    directory.mkdir(parents=True)
    container = f"oss-image-bench-{uuid.uuid4().hex[:8]}"
    run = [args.engine, "run", "-d", "--name", container, "--init", "--cpus=2", "--memory=8g",
           "--memory-swap=8g", "--shm-size=256m", "-e", "Pdf__RasterConcurrency=1",
           "-e", "Pdf__QueueWaitMilliseconds=500", "-p", "127.0.0.1::8080"]
    if args.app_cores:
        run += ["--entrypoint", "/usr/bin/taskset", image, "-c", args.app_cores, "dotnet", "OssPdf.dll"]
    else:
        run.append(image)
    command(*run)
    try:
        cgroup = cgroup_path(command(args.engine, "inspect", "--format", "{{.State.Pid}}", container))
        limits = {key: (cgroup / key).read_text().strip() for key in ("cpu.max", "memory.max", "memory.swap.max")}
        quota, period = map(int, limits["cpu.max"].split())
        if quota != 2 * period or limits["memory.max"] != str(8 * 2**30) or limits["memory.swap.max"] != "0":
            raise RuntimeError(f"Unexpected container limits: {limits}")
        url = "http://" + command(args.engine, "port", container, "8080/tcp")
        await_http(url, args.engine, container)
        pdf = directory / "sample.pdf"
        pixels = verify_pdf(url, fixture, mime, pdf)
        data = {"limits": limits, "fixture_bytes": fixture.stat().st_size, "sample_pdf_bytes": pdf.stat().st_size,
                "oriented_pixels": pixels, "stages": []}

        def measure(name, rate, duration):
            stage = run_stage(args, fixture, url, directory, container, cgroup, name, rate, duration, mime)
            data["stages"].append(stage)
            (directory / "results.json").write_text(json.dumps(data, indent=2) + "\n")
            return stage

        measure("warmup", 1, args.warmup_seconds)
        passing = []
        for rate in args.rates:
            if not measure(f"probe-{rate}", rate, args.probe_seconds)["passed"]:
                break
            passing.append(rate)
        for rate in reversed(passing):
            steady = measure(f"steady-{rate}", rate, args.steady_seconds)
            if steady["passed"]:
                verify_pdf(url, fixture, mime, pdf)
                return steady
        raise RuntimeError(f"No tested rate passed for {args.renderer} {fixture.name}.")
    finally:
        subprocess.run([args.engine, "stop", "--time", "5", container], capture_output=True)
        subprocess.run([args.engine, "rm", "--force", container], check=True, stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="container image using ImageMagick for PDF output")
    parser.add_argument("--candidate", required=True, help="container image using Ironpress for PDF output")
    parser.add_argument("--engine", default="docker")
    parser.add_argument("--k6", default="k6")
    parser.add_argument("--app-cores")
    parser.add_argument("--rates", nargs="+", type=int, default=[4, 8, 16, 24, 32, 48, 64, 96, 128])
    parser.add_argument("--warmup-seconds", type=int, default=6)
    parser.add_argument("--probe-seconds", type=int, default=8)
    parser.add_argument("--steady-seconds", type=int, default=30)
    parser.add_argument("--p95-ms", type=float, help="optional latency target for passing a rate")
    parser.add_argument("--output", type=Path, default=Path("untracked/image-to-pdf-benchmark"))
    args = parser.parse_args()
    if min(*args.rates, args.warmup_seconds, args.probe_seconds, args.steady_seconds) < 1 or (
        args.p95_ms is not None and args.p95_ms <= 0
    ):
        parser.error("rates, durations, and p95 target must be positive")
    args.rates = sorted(set(args.rates))
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    fixtures = write_fixtures(args.output)
    report = ["# Image-to-PDF container benchmark", "",
              f"- k6: {command(args.k6, 'version')}",
              "- Limits: two CPUs, 8 GiB RAM, swap disabled, one raster slot.",
              f"- CPU affinity: {args.app_cores or 'host scheduler'}.",
              f"- Stages: {args.warmup_seconds}s warmup, {args.probe_seconds}s probes, {args.steady_seconds}s steady.",
              f"- Rate ladder: {args.rates} req/s.",
              "- Passing stages returned valid PDFs for ≥99% of requests with zero drops and OOM kills.",
              "", "| Engine | Fixture | Offered req/s | Valid PDFs/s | p95 (ms) |",
              "| --- | --- | ---: | ---: | ---: |"]
    if args.p95_ms is not None:
        report.insert(8, f"- Passing p95 limit: {args.p95_ms} ms.")
    for args.renderer, image in (("imagemagick", args.baseline), ("ironpress", args.candidate)):
        image_id = command(args.engine, "image", "inspect", "--format", "{{.Id}}", image)
        report.insert(2, f"- {args.renderer} image: `{image_id}`.")
        for fixture, mime in fixtures:
            stage = benchmark(args, image, fixture, mime)
            report.append(f"| {args.renderer} | [{fixture.name}]({args.renderer}/{fixture.stem}/results.json) | "
                          f"{stage['offered_rps']} | {stage['valid_pdf_per_sec']:.2f} | {stage['p95_ms']:.1f} |")
            (args.output / "results.md").write_text("\n".join(report) + "\n")


if __name__ == "__main__":
    main()
