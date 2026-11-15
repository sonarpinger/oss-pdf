import execution from 'k6/execution';
import http from 'k6/http';
import { Counter, Trend } from 'k6/metrics';

const fixture = open(__ENV.FIXTURE);
const rate = Number(__ENV.RATE);
const seconds = Number(__ENV.SECONDS);
const completed = new Counter('completed_requests');
const valid = new Counter('valid_pdfs');
const busy = new Counter('busy_responses');
const timedOut = new Counter('conversion_timeouts');
const httpErrors = new Counter('other_http_errors');
const transportErrors = new Counter('transport_errors');
const invalidPdfs = new Counter('invalid_pdfs');
const latency = new Trend('valid_pdf_latency', true);
const quarters = Array.from({ length: 4 }, (_, index) =>
    new Trend(`valid_pdf_latency_q${index + 1}`, true));

if (!Number.isInteger(rate) || rate < 1 || !Number.isInteger(seconds) || seconds < 1) {
    throw new Error('RATE and SECONDS must be positive integers.');
}

export const options = {
    scenarios: {
        html: {
            executor: 'constant-arrival-rate',
            rate,
            timeUnit: '1s',
            duration: `${seconds}s`,
            gracefulStop: '150s',
            preAllocatedVUs: Math.max(8, Math.ceil(rate * 2)),
            maxVUs: Math.max(16, Math.ceil(rate * 4)),
        },
    },
    summaryTrendStats: ['med', 'p(95)', 'p(99)'],
};

export default function () {
    // Exclude an iteration k6 may schedule at the end of the measurement window.
    if (execution.scenario.iterationInTest >= rate * seconds) {
        return;
    }

    const start = Date.now();
    const response = http.post(`${__ENV.BASE_URL || 'http://127.0.0.1:8080'}/HtmlToPdf`, fixture, {
        headers: { 'Content-Type': 'application/json', Accept: 'application/pdf' },
        responseType: 'binary',
        timeout: '150s',
        redirects: 0,
    });
    const elapsed = Date.now() - start;
    const bytes = new Uint8Array(response.body || new ArrayBuffer(0));
    const isPdf = response.status === 200 &&
        /^application\/pdf(?:\s*;|$)/i.test(response.headers['Content-Type'] || '') &&
        bytes.length > 5 && bytes[0] === 37 && bytes[1] === 80 &&
        bytes[2] === 68 && bytes[3] === 70 && bytes[4] === 45;

    completed.add(1);
    valid.add(Number(isPdf));
    busy.add(Number(response.status === 503));
    timedOut.add(Number(response.status === 504));
    httpErrors.add(Number(response.status !== 0 && ![200, 503, 504].includes(response.status)));
    transportErrors.add(Number(response.status === 0));
    invalidPdfs.add(Number(response.status === 200 && !isPdf));

    if (isPdf) {
        latency.add(elapsed);
        const quarter = Math.min(3, Math.max(0,
            Math.floor((start - execution.scenario.startTime) * 4 / (seconds * 1000))));
        quarters[quarter].add(elapsed);
    }
}

export function handleSummary(data) {
    return { [__ENV.SUMMARY_PATH || 'html-summary.json']: JSON.stringify(data, null, 2) };
}
