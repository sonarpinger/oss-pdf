#!/usr/bin/env python3
"""Compare image endpoints on two container images using the same k6 workload."""

import argparse
import base64
import json
import math
import random
import shutil
import subprocess
import uuid
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps
from pypdf import PdfReader

from fixtures import embedded_image
from run import await_http, cgroup_path, command, image_draws, run_stage

COLOR_POINTS = ((0.05, 0.05), (0.95, 0.05), (0.5, 0.5), (0.05, 0.95), (0.95, 0.95))


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


def write_profile_fixtures(directory):
    jpeg = directory / "rotated-photo.jpg"
    png = directory / "large-transparent.png"
    photo = Image.open(BytesIO(base64.b64decode(embedded_image(4).split(",", 1)[1])))
    size = (2960, 2160)
    art = Image.frombytes("RGB", size, random.Random(42).randbytes(math.prod(size) * 3)).convert("RGBA")
    markers = ((224, 28, 58, 0), (42, 72, 211, 128), (20, 151, 80, 255),
               (84, 51, 145, 255), (194, 125, 19, 255))
    for image in (photo, art):
        draw = ImageDraw.Draw(image)
        for (x, y), color in zip(COLOR_POINTS, markers):
            cx, cy = int(x * image.width), int(y * image.height)
            draw.rectangle((cx - 28, cy - 28, cx + 28, cy + 28),
                           fill=color if image.mode == "RGBA" else color[:3])
    exif = Image.Exif()
    exif[274] = 6
    photo.save(jpeg, quality=90, exif=exif)
    art.save(png, compress_level=1)
    return (png, "image/png"), (jpeg, "image/jpeg")


def request_pdf(url, route, fixture, mime, destination, *form):
    subprocess.run(["curl", "--fail-with-body", "--silent", "--show-error", "--max-time", "120",
                    "--form", f"file=@{fixture};type={mime}", *form, "--output", str(destination), f"{url}/{route}"],
                   check=True)


def verify_pdf(url, fixture, mime, destination):
    request_pdf(url, "ImageToPdf", fixture, mime, destination)
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


def verify_profile_pdf(url, fixture, mime, destination, flow, quality, check_fixture_colors):
    if flow == "small":
        request_pdf(url, "ImageToPdf", fixture, mime, destination, "--form", "profile=small")
    else:
        intermediate = destination.with_name("intermediate.pdf")
        request_pdf(url, "ImageToPdf", fixture, mime, intermediate)
        request_pdf(url, "Compress", intermediate, "application/pdf", destination, "--form", f"quality={quality}")

    pages = PdfReader(destination).pages
    assert len(pages) == 1 and image_draws(pages[0]) == 1, destination
    bounds = pages[0].mediabox
    with Image.open(fixture) as image:
        original = ImageOps.exif_transpose(image).convert("RGBA")
        assert all(math.isclose(float(pts), pixels * 72 / 150, abs_tol=0.01)
                   for pts, pixels in zip((bounds.width, bounds.height), original.size)), bounds
        if check_fixture_colors:
            raster = subprocess.check_output(["pdftoppm", "-scale-to", "640", "-png", "-singlefile", str(destination)])
            with Image.open(BytesIO(raster)) as result:
                rendered = result.convert("RGB")
            for x, y in COLOR_POINTS:
                *rgb, alpha = original.getpixel((int(x * original.width), int(y * original.height)))
                expected = [round((channel * alpha + 255 * (255 - alpha)) / 255) for channel in rgb]
                actual = rendered.getpixel((int(x * rendered.width), int(y * rendered.height)))
                assert max(abs(a - b) for a, b in zip(actual, expected)) <= 18, (destination, x, y, actual, expected)
    if flow == "small" and check_fixture_colors:
        assert destination.stat().st_size <= 100_000, destination
    return list(pages[0].images[0].image.size)


def benchmark(args, image, fixture, mime):
    directory = args.output / args.renderer / fixture.stem
    directory.mkdir(parents=True)
    container = f"oss-image-bench-{uuid.uuid4().hex[:8]}"
    run = [args.engine, "run", "-d", "--name", container, "--init", "--cpus=2", "--memory=8g",
           "--memory-swap=8g", "-e", "Pdf__RasterConcurrency=1",
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
        def verify():
            if args.compare_small:
                check_colors = not args.profile_fixture and (args.renderer == "small" or args.round_trip_quality >= 55)
                return verify_profile_pdf(url, fixture, mime, pdf, args.renderer, args.round_trip_quality, check_colors)
            return verify_pdf(url, fixture, mime, pdf)

        pixels = verify()
        data = {"limits": limits, "fixture_bytes": fixture.stat().st_size, "sample_pdf_bytes": pdf.stat().st_size,
                "output_pixels": pixels, "stages": []}
        data["fixture_sha256"] = sha256(fixture.read_bytes()).hexdigest()
        if args.compare_small and args.renderer == "round-trip":
            data["intermediate_pdf_bytes"] = pdf.with_name("intermediate.pdf").stat().st_size

        def measure(name, rate, duration):
            flow = args.renderer if args.compare_small else None
            stage = run_stage(args, fixture, url, directory, container, cgroup, name, rate, duration,
                              upload_mime=mime, image_flow=flow)
            data["stages"].append(stage)
            (directory / "results.json").write_text(json.dumps(data, indent=2) + "\n")
            return stage

        measure("warmup", min(1, args.rates[0]), args.warmup_seconds)
        passing = []
        for rate in args.rates:
            if not measure(f"probe-{rate:g}", rate, args.probe_seconds)["passed"]:
                break
            passing.append(rate)
        for rate in reversed(passing):
            steady = measure(f"steady-{rate:g}", rate, args.steady_seconds)
            if steady["passed"]:
                verify()
                return steady
        raise RuntimeError(f"No tested rate passed for {args.renderer} {fixture.name}.")
    finally:
        subprocess.run([args.engine, "stop", "--time", "5", container], capture_output=True)
        subprocess.run([args.engine, "rm", "--force", container], check=True, stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="baseline container image")
    parser.add_argument("--candidate", required=True, help="candidate container image")
    parser.add_argument("--compare-small", action="store_true",
                        help="compare default /ImageToPdf followed by /Compress with /ImageToPdf profile=small")
    parser.add_argument("--profile-fixture", type=Path, help="use this exact JPEG or PNG with --compare-small")
    parser.add_argument("--round-trip-quality", type=int, default=55)
    parser.add_argument("--engine", default="docker")
    parser.add_argument("--k6", default="k6")
    parser.add_argument("--app-cores")
    parser.add_argument("--rates", nargs="+", type=float, help="offered requests per second")
    parser.add_argument("--warmup-seconds", type=int, default=6)
    parser.add_argument("--probe-seconds", type=int, default=8)
    parser.add_argument("--steady-seconds", type=int, default=30)
    parser.add_argument("--p95-ms", type=float, help="optional latency target for passing a rate")
    parser.add_argument("--output", type=Path, default=Path("untracked/image-to-pdf-benchmark"))
    args = parser.parse_args()
    if args.profile_fixture and (not args.compare_small or args.profile_fixture.suffix.lower() not in (".png", ".jpg", ".jpeg")):
        parser.error("--profile-fixture requires --compare-small and a JPEG or PNG file")
    if not 1 <= args.round_trip_quality <= 100:
        parser.error("--round-trip-quality must be between 1 and 100")
    if args.rates is None:
        args.rates = ([0.25, 0.5, 0.75, 1, 2, 4, 6, 8, 12, 16, 24, 32] if args.compare_small
                      else [4, 8, 16, 24, 32, 48, 64, 96, 128])
    if any(not math.isfinite(rate) or rate <= 0 for rate in args.rates) or (
        min(args.warmup_seconds, args.probe_seconds, args.steady_seconds) < 1
    ) or (
        args.p95_ms is not None and args.p95_ms <= 0
    ):
        parser.error("rates, durations, and p95 target must be positive")
    args.rates = sorted(set(args.rates))
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    if args.compare_small:
        if args.profile_fixture:
            mime = "image/png" if args.profile_fixture.suffix.lower() == ".png" else "image/jpeg"
            fixtures = [(Path(shutil.copyfile(args.profile_fixture, args.output / args.profile_fixture.name)), mime)]
        else:
            fixtures = write_profile_fixtures(args.output)
        engines = ("round-trip", "small")
    else:
        fixtures = write_fixtures(args.output)
        engines = ("imagemagick", "ironpress")
    rate_ladder = ", ".join(f"{rate:g}" for rate in args.rates)
    report = ["# /ImageToPdf container benchmark", "",
              f"- k6: {command(args.k6, 'version')}",
              "- Limits: two CPUs, 8 GiB RAM, swap disabled, one raster slot.",
              f"- CPU affinity: {args.app_cores or 'host scheduler'}.",
              f"- Stages: {args.warmup_seconds}s warmup, {args.probe_seconds}s probes, {args.steady_seconds}s steady.",
              f"- Rate ladder: {rate_ladder} req/s.",
              "- Passing stages returned valid PDFs for ≥99% of requests with zero drops and OOM kills.",
              "", "| Path | Fixture | Offered jobs/s | Valid PDFs/s | p95 (ms) | Output bytes p95 |",
              "| --- | --- | ---: | ---: | ---: | ---: |"]
    if args.p95_ms is not None:
        report.insert(8, f"- Passing p95 limit: {args.p95_ms} ms.")
    if args.compare_small:
        report.insert(7, f"- Round trip: default /ImageToPdf then /Compress at quality {args.round_trip_quality}.")
    for args.renderer, image in zip(engines, (args.baseline, args.candidate)):
        image_id = command(args.engine, "image", "inspect", "--format", "{{.Id}}", image)
        report.insert(2, f"- {args.renderer} image: `{image_id}`.")
        for fixture, mime in fixtures:
            stage = benchmark(args, image, fixture, mime)
            report.append(f"| {args.renderer} | [{fixture.name}]({args.renderer}/{fixture.stem}/results.json) | "
                          f"{stage['offered_rps']:g} | {stage['valid_pdf_per_sec']:.2f} | {stage['p95_ms']:.1f} "
                          f"| {stage['pdf_bytes_p95']:,.0f} |")
            (args.output / "results.md").write_text("\n".join(report) + "\n")


if __name__ == "__main__":
    main()
