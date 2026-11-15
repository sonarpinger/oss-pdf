#!/usr/bin/env python3
"""Run the /HtmlToPdf benchmark against one freshly started container at a time."""

import argparse
import hashlib
import io
import json
import math
import platform
import re
import statistics
import subprocess
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader

from fixtures import FIXTURES, write_fixture

SCRIPT = Path(__file__).with_name("html_to_pdf.js")
COUNTERS = (
    "completed_requests", "valid_pdfs", "busy_responses", "conversion_timeouts",
    "other_http_errors", "transport_errors", "invalid_pdfs", "dropped_iterations",
)
RUN_COLUMNS = {
    "offered_rps": "Offered req/s",
    "valid_pdf_per_sec": "Valid PDFs/s",
    "valid_percent": "Valid %",
    "p95_ms": "p95 (ms)",
    "queue_p95_ms": "Queue p95 (ms)",
    "busy_responses": "503",
    "conversion_timeouts": "504",
    "dropped_iterations": "Dropped",
    "peak_memory_mib": "Peak memory (MiB)",
}
LOG_TIMINGS = re.compile(r"/HtmlToPdf queue wait (\d+) ms, processing (\d+) ms")


def command(*arguments):
    return subprocess.check_output(arguments, text=True).strip()


def cgroup_path(pid):
    for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
        if line.startswith("0::/"):
            return Path("/sys/fs/cgroup") / line.removeprefix("0::/")
    raise RuntimeError("This benchmark requires cgroup v2 resource counters.")


def read_counters(path):
    return {key: int(value) for key, value in (line.split() for line in path.read_text().splitlines())}


def sample_resources(path):
    pids = set()
    for pid in (path / "cgroup.procs").read_text().split():
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if argv[0].endswith(b"/chrome-headless-shell") and not any(arg.startswith(b"--type=") for arg in argv):
            pids.add(int(pid))
    return int((path / "memory.current").read_text()), pids


def validate_fixture(url, payload, fixture):
    started = time.perf_counter()
    request = urllib.request.Request(
        f"{url}/HtmlToPdf", data=payload.read_bytes(), headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=150) as response:
        if response.status != 200 or response.headers.get_content_type() != "application/pdf":
            raise RuntimeError(f"{fixture} returned an invalid HTTP response")
        content = response.read()
    elapsed = (time.perf_counter() - started) * 1000

    if not content.startswith(b"%PDF-"):
        raise RuntimeError(f"{fixture} did not return a PDF")
    _, expected_pages, marker, expected_images = FIXTURES[fixture]
    pages = PdfReader(io.BytesIO(content)).pages
    extracted = unicodedata.normalize("NFKC", "\n".join(page.extract_text() for page in pages))
    images = sum(len(page.images) for page in pages)
    if len(pages) != expected_pages or marker not in extracted or images < expected_images:
        raise RuntimeError(f"{fixture}: {len(pages)} pages, {images} images, marker found={marker in extracted}")
    return round(elapsed, 1), len(content), len(pages), images


def await_http(url, engine, container):
    for _ in range(120):
        try:
            urllib.request.urlopen(url, timeout=1).close()
            return
        except urllib.error.HTTPError:
            return
        except OSError:
            time.sleep(0.25)
    logs = subprocess.run([engine, "logs", container], capture_output=True, text=True)
    raise RuntimeError(f"Container did not start: {logs.stdout}\n{logs.stderr}")


def percentile95(values):
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * 0.95) - 1] if ordered else None


def run_stage(args, fixture, url, directory, container, cgroup, name, rate, duration):
    summary_path = directory / f"{name}.json"
    started = datetime.now(timezone.utc).isoformat()
    before_cpu = read_counters(cgroup / "cpu.stat")
    before_memory = read_counters(cgroup / "memory.events")
    start_usage, browsers = sample_resources(cgroup)
    samples = [start_usage]
    with (directory / f"{name}.log").open("w") as log:
        start_time = time.monotonic()
        with subprocess.Popen(
            [args.k6, "run", "--quiet", "--no-color",
             "-e", f"BASE_URL={url}", "-e", f"FIXTURE={fixture}", "-e", f"RATE={rate}",
             "-e", f"SECONDS={duration}", "-e", f"SUMMARY_PATH={summary_path}", str(SCRIPT)],
            stdout=log, stderr=subprocess.STDOUT,
        ) as process:
            while True:
                try:
                    result = process.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    usage, pids = sample_resources(cgroup)
                    samples.append(usage)
                    browsers.update(pids)
        elapsed = time.monotonic() - start_time
    if result != 0:
        raise RuntimeError(f"k6 exited with status {result}; see {directory / (name + '.log')}")

    end_usage, pids = sample_resources(cgroup)
    browsers.update(pids)
    after_cpu = read_counters(cgroup / "cpu.stat")
    after_memory = read_counters(cgroup / "memory.events")
    metrics = json.loads(summary_path.read_text())["metrics"]
    counts = {key: int(metrics.get(key, {}).get("values", {}).get("count", 0)) for key in COUNTERS}
    latency = metrics.get("valid_pdf_latency", {}).get("values", {})
    valid_pdfs = counts["valid_pdfs"]
    expected = rate * duration
    if counts["completed_requests"] > expected:
        raise RuntimeError(f"Unexpected k6 request count for {name}: {counts}")
    successful = valid_pdfs >= 0.99 * expected and counts["dropped_iterations"] == 0
    if args.p95_ms is not None:
        successful &= 0 < latency.get("p(95)", 0) <= args.p95_ms
    oom_kills = after_memory.get("oom_kill", 0) - before_memory.get("oom_kill", 0)
    successful &= oom_kills == 0 and len(browsers) <= 1
    container_log = subprocess.run(
        [args.engine, "logs", "--since", started, container], capture_output=True, text=True, check=True,
    )
    timings = [tuple(map(int, pair)) for pair in LOG_TIMINGS.findall(container_log.stdout + container_log.stderr)]
    stage = {
        "stage": name, "scheduled_seconds": duration, "elapsed_seconds": round(elapsed, 3),
        "offered_rps": rate, "expected_requests": expected, "passed": bool(successful),
        "valid_pdf_per_sec": round(valid_pdfs / duration, 4),
        "valid_percent": round(valid_pdfs * 100 / expected, 4),
        "p50_ms": latency.get("med"), "p95_ms": latency.get("p(95)"), "p99_ms": latency.get("p(99)"),
        "p95_ms_by_quarter": [
            metrics.get(f"valid_pdf_latency_q{quarter}", {}).get("values", {}).get("p(95)")
            for quarter in range(1, 5)
        ],
        "queue_p95_ms": percentile95(wait for wait, _ in timings),
        "processing_p95_ms": percentile95(processing for _, processing in timings),
        "cpu_average_cores": round((after_cpu["usage_usec"] - before_cpu["usage_usec"]) / (elapsed * 1e6), 3),
        "cpu_throttled_periods": after_cpu["nr_throttled"] - before_cpu["nr_throttled"],
        "memory_start_mib": round(start_usage / 2**20, 1),
        "peak_memory_mib": round(max(*samples, end_usage) / 2**20, 1),
        "memory_end_mib": round(end_usage / 2**20, 1),
        "oom_kills": oom_kills, "chromium_browser_pids": sorted(browsers),
        **counts,
    }
    print(
        f"{directory.name} {name}: {stage['valid_pdf_per_sec']:.2f} PDFs/s at {rate} req/s, "
        f"p95 {round(stage['p95_ms'], 1) if stage['p95_ms'] is not None else 'n/a'} ms, "
        f"{stage['valid_percent']:.2f}% valid, "
        f"{counts['busy_responses']} busy, {counts['dropped_iterations']} dropped",
        flush=True,
    )
    return stage


def benchmark_fixture(args, name, destination, publish):
    directory = destination / name
    directory.mkdir()
    fixture = write_fixture(directory, name)
    container = f"oss-pdf-html-{uuid.uuid4().hex[:8]}"
    url = f"http://127.0.0.1:{args.port}"
    run_args = [
        args.engine, "run", "--rm", "-d", "--name", container, "--init", "--shm-size=256m",
        "--cpus=2", "--memory=8g", "--memory-swap=8g", "-p", f"127.0.0.1:{args.port}:8080",
        "-e", "Pdf__HtmlConcurrency=2", "-e", "Pdf__QueueWaitMilliseconds=500",
        "-e", "Pdf__ChromiumSandbox=true",
    ]
    if args.app_cores:
        run_args += ["--entrypoint", "/usr/bin/taskset", args.image, "-c", args.app_cores, "dotnet", "OssPdf.dll"]
    else:
        run_args.append(args.image)
    command(*run_args)
    try:
        pid = command(args.engine, "inspect", "--format", "{{.State.Pid}}", container)
        cgroup = cgroup_path(pid)
        limits = {key: (cgroup / key).read_text().strip() for key in ("cpu.max", "memory.max", "memory.swap.max")}
        quota, period = map(int, limits["cpu.max"].split())
        if quota != 2 * period or limits["memory.max"] != str(8 * 2**30) or limits["memory.swap.max"] != "0":
            raise RuntimeError(f"Unexpected container resource limits: {limits}")
        await_http(url, args.engine, container)
        cold_ms, pdf_bytes, pages, images = validate_fixture(url, fixture, name)
        data = {
            "limits": limits, "request_bytes": fixture.stat().st_size,
            "request_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
            "sample_pdf_bytes": pdf_bytes, "pages": pages, "images": images,
            "first_request_ms": cold_ms, "stages": [],
        }

        def measure(label, rate, duration):
            stage = run_stage(args, fixture, url, directory, container, cgroup, label, rate, duration)
            data["stages"].append(stage)
            publish(name, data)
            return stage

        measure("warmup", 1, args.warmup_seconds)
        low, high = 0, args.max_rps + 1
        rate = 1
        while rate <= args.max_rps:
            if measure(f"probe-{rate}", rate, args.probe_seconds)["passed"]:
                low = rate
            else:
                high = rate
                break
            rate *= 2
        if low < args.max_rps < rate and high > args.max_rps:
            if measure(f"probe-{args.max_rps}", args.max_rps, args.probe_seconds)["passed"]:
                low = args.max_rps
            else:
                high = args.max_rps
        while high - low > 1:
            rate = (low + high) // 2
            if measure(f"probe-{rate}", rate, args.probe_seconds)["passed"]:
                low = rate
            else:
                high = rate

        data["probe_ceiling"] = args.max_rps
        data["first_failing_probe_rps"] = high if high <= args.max_rps else None
        while low > 0:
            confirmations = []
            for trial in range(1, args.repeats + 1):
                confirmation = measure(f"confirm-{low}-{trial}", low, args.repeat_seconds)
                confirmations.append(confirmation)
                if not confirmation["passed"]:
                    break
            if all(stage["passed"] for stage in confirmations):
                soak = measure(f"soak-{low}", low, args.soak_seconds)
                if soak["passed"]:
                    validate_fixture(url, fixture, name)
                    data["sustainable"] = soak
                    data["confirmation_p95_ms_median"] = statistics.median(stage["p95_ms"] for stage in confirmations)
                    break
            low -= 1

        publish(name, data)
        if "sustainable" not in data:
            raise RuntimeError(f"No offered rate met the benchmark criteria for {name}.")
    finally:
        subprocess.run([args.engine, "stop", "--time", "5", container], check=True, stdout=subprocess.DEVNULL)


def save_results(results, destination):
    (destination / "results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# HTML-to-PDF throughput", "",
        f"- Image ID: `{results['image_id']}`",
        f"- Host: {results['host']}",
        f"- k6: {results['k6']}",
        f"- Container CPU affinity: {results['configuration']['app_cores'] or 'host scheduler'}",
        "- Container budget: 2 CPUs, 8 GiB, swap disabled; two HTML rendering slots.",
        f"- Durations: {results['configuration']['warmup_seconds']} s warmup, "
        f"{results['configuration']['probe_seconds']} s probes, "
        f"{results['configuration']['repeats']} × {results['configuration']['repeat_seconds']} s confirmations, "
        f"{results['configuration']['soak_seconds']} s final steady run per fixture.",
        "- Pass criteria: ≥99% valid PDFs, zero dropped iterations, zero OOM kills and no observed browser restart.",
        "- Successful PDF rate is valid responses for requests scheduled in the run divided by scheduled seconds.",
        "",
        "| Fixture | Pages | Offered req/s | Valid PDFs/s | Valid % | p95 (ms) | Cold (ms) | Peak memory (MiB) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    if results["configuration"]["p95_ms"] is not None:
        lines.insert(9, f"- Additional passing p95 limit: {results['configuration']['p95_ms']} ms.")
    for fixture, data in results["fixtures"].items():
        if "sustainable" not in data:
            continue
        stage = data["sustainable"]
        lines.append(
            f"| {fixture} | {data['pages']} | {stage['offered_rps']} | {stage['valid_pdf_per_sec']:.2f} "
            f"| {stage['valid_percent']:.2f} | {stage['p95_ms']:.1f} | {data['first_request_ms']:.1f} "
            f"| {stage['peak_memory_mib']:.1f} |"
        )
    for fixture, data in results["fixtures"].items():
        lines += [
            "", f"## {fixture}", "",
            f"Request: {data['request_bytes']:,} bytes. Sample PDF: {data['sample_pdf_bytes']:,} bytes, "
            f"{data['pages']} pages and {data['images']} image objects.",
            "", "| Stage | " + " | ".join(RUN_COLUMNS.values()) + " |",
            "| --- | " + " | ".join("---:" for _ in RUN_COLUMNS) + " |",
        ]
        for stage in data["stages"]:
            values = [stage[column] for column in RUN_COLUMNS]
            cells = [f"{value:.2f}" if isinstance(value, float) else str(value) if value is not None else "—"
                     for value in values]
            lines.append(f"| {stage['stage']} | " + " | ".join(cells) + " |")
    (destination / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="oss-pdf:html-benchmark")
    parser.add_argument("--engine", default="docker")
    parser.add_argument("--k6", default="k6")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--output", type=Path, default=Path("untracked/html-to-pdf-benchmark"))
    parser.add_argument("--app-cores", help="optional Linux CPU affinity, e.g. 4,5")
    parser.add_argument("--fixture", action="append", choices=FIXTURES)
    parser.add_argument("--warmup-seconds", type=int, default=30)
    parser.add_argument("--probe-seconds", type=int, default=120)
    parser.add_argument("--repeat-seconds", type=int, default=300)
    parser.add_argument("--soak-seconds", type=int, default=900)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-rps", type=int, default=64)
    parser.add_argument("--p95-ms", type=float, help="optional latency target for passing a rate")
    args = parser.parse_args()
    durations = (args.warmup_seconds, args.probe_seconds, args.repeat_seconds, args.soak_seconds)
    if min(*durations, args.repeats, args.max_rps) < 1 or args.p95_ms is not None and args.p95_ms <= 0:
        parser.error("durations, repetitions, rate limit, and p95 target must be positive")
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    results = {
        "image_id": command(args.engine, "image", "inspect", "--format", "{{.Id}}", args.image),
        "host": f"{platform.system()} {platform.release()} {platform.machine()}",
        "k6": command(args.k6, "version"),
        "configuration": {key: value for key, value in vars(args).items() if key != "output"},
        "fixtures": {},
    }

    def publish(name, data):
        results["fixtures"][name] = data
        save_results(results, destination)

    for name in args.fixture or FIXTURES:
        benchmark_fixture(args, name, destination, publish)


if __name__ == "__main__":
    main()
