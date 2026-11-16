import execution from 'k6/execution';
import http from 'k6/http';
import { Counter, Trend } from 'k6/metrics';

const uploadMime = __ENV.UPLOAD_MIME;
const fixture = uploadMime ? open(__ENV.FIXTURE, 'b') : open(__ENV.FIXTURE);
const route = uploadMime ? 'ImageToPdf' : 'HtmlToPdf';
const imageFlow = __ENV.IMAGE_PDF_FLOW;
const roundTripQuality = __ENV.ROUND_TRIP_QUALITY || '55';
const headers = uploadMime ? { Accept: 'application/pdf' } :
    { 'Content-Type': 'application/json', Accept: 'application/pdf' };
const rate = Number(__ENV.RATE);
const ratePeriod = Number(__ENV.RATE_PERIOD_SECONDS || 1);
const seconds = Number(__ENV.SECONDS);
const completed = new Counter('completed_requests');
const valid = new Counter('valid_pdfs');
const busy = new Counter('busy_responses');
const timedOut = new Counter('conversion_timeouts');
const httpErrors = new Counter('other_http_errors');
const transportErrors = new Counter('transport_errors');
const invalidPdfs = new Counter('invalid_pdfs');
const latency = new Trend('valid_pdf_latency', true);
const pdfBytes = new Trend('valid_pdf_bytes');
const quarters = Array.from({ length: 4 }, (_, index) =>
    new Trend(`valid_pdf_latency_q${index + 1}`, true));

if ([rate, ratePeriod, seconds].some(value => !Number.isInteger(value) || value < 1)) {
    throw new Error('RATE, RATE_PERIOD_SECONDS, and SECONDS must be positive integers.');
}

export const options = {
    scenarios: {
        html: {
            executor: 'constant-arrival-rate',
            rate,
            timeUnit: `${ratePeriod}s`,
            duration: `${seconds}s`,
            gracefulStop: '150s',
            preAllocatedVUs: Math.max(8, Math.ceil(rate / ratePeriod * 2)),
            maxVUs: Math.max(16, Math.ceil(rate / ratePeriod * 4)),
        },
    },
    summaryTrendStats: ['med', 'p(95)', 'p(99)'],
};

export default function () {
    // Exclude an iteration k6 may schedule at the end of the measurement window.
    if (execution.scenario.iterationInTest >= Math.ceil(rate * seconds / ratePeriod)) {
        return;
    }

    const start = Date.now();
    const body = uploadMime ? { file: http.file(fixture, __ENV.FIXTURE.split('/').pop(), uploadMime) } : fixture;
    if (imageFlow === 'small') {
        body.profile = 'small';
    }

    const url = __ENV.BASE_URL || 'http://127.0.0.1:8080';
    const requestOptions = { headers, responseType: 'binary', timeout: '150s', redirects: 0 };
    let response = http.post(`${url}/${route}`, body, requestOptions);
    if (imageFlow === 'round-trip' && isValidPdf(response)) {
        response = http.post(`${url}/Compress`, {
            file: http.file(response.body, 'intermediate.pdf', 'application/pdf'), quality: roundTripQuality,
        }, requestOptions);
    }

    const elapsed = Date.now() - start;
    const isPdf = isValidPdf(response);

    completed.add(1);
    valid.add(Number(isPdf));
    busy.add(Number(response.status === 503));
    timedOut.add(Number(response.status === 504));
    httpErrors.add(Number(response.status !== 0 && ![200, 503, 504].includes(response.status)));
    transportErrors.add(Number(response.status === 0));
    invalidPdfs.add(Number(response.status === 200 && !isPdf));

    if (isPdf) {
        latency.add(elapsed);
        pdfBytes.add(response.body.byteLength);
        const quarter = Math.min(3, Math.max(0,
            Math.floor((start - execution.scenario.startTime) * 4 / (seconds * 1000))));
        quarters[quarter].add(elapsed);
    }
}

function isValidPdf(response) {
    const bytes = new Uint8Array(response.body || new ArrayBuffer(0));
    return response.status === 200 &&
        /^application\/pdf(?:\s*;|$)/i.test(response.headers['Content-Type'] || '') &&
        bytes.length > 5 && bytes[0] === 37 && bytes[1] === 80 &&
        bytes[2] === 68 && bytes[3] === 70 && bytes[4] === 45;
}

export function handleSummary(data) {
    return { [__ENV.SUMMARY_PATH || 'html-summary.json']: JSON.stringify(data, null, 2) };
}
