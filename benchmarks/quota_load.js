// Run with k6; no remote imports or external feature calls.
import http from 'k6/http';
import { check } from 'k6';
import crypto from 'k6/crypto';
import exec from 'k6/execution';
import { SharedArray } from 'k6/data';
import { Counter, Trend } from 'k6/metrics';

const fixturePath = __ENV.FIXTURE || './results/fixture.json';
const orgs = new SharedArray('quota organizations', () => JSON.parse(open(fixturePath)).organizations);
const metadata = new SharedArray('quota fixture metadata', () => {
  const document = JSON.parse(open(fixturePath));
  delete document.organizations;
  return [document];
});
const manifest = metadata[0];
const baseURL = (__ENV.BASE_URL || 'http://127.0.0.1:18001').replace(/\/$/, '');
const workload = __ENV.WORKLOAD || 'distributed';
const rate = Number(__ENV.RATE || 20);
const vus = Number(__ENV.VUS || 20);
const maxVUs = Number(__ENV.MAX_VUS || 200);
const routesPerBatch = Number(__ENV.BATCH_SIZE || 7);
const quotaTarget = Number(__ENV.QUOTA_P95_MS || 10);
const fixedIterations = __ENV.ITERATIONS ? Number(__ENV.ITERATIONS) : null;
const modes = ['distributed', 'hot', 'batch', 'retry', 'failure', 'exhaustion'];
if (!modes.includes(workload) || !Number.isInteger(rate) || rate < 1 ||
    !Number.isInteger(vus) || vus < 1 || !Number.isInteger(maxVUs) || maxVUs < vus ||
    !Number.isInteger(routesPerBatch) || routesPerBatch < 1 || routesPerBatch > 1000 ||
    !Number.isFinite(quotaTarget) || quotaTarget <= 0 ||
    (fixedIterations !== null && (!Number.isInteger(fixedIterations) || fixedIterations < 1)) ||
    !orgs.length || manifest.feature !== 'sailing-schedule') {
  throw new Error('Invalid workload, fixture, rate, VUs, or batch size');
}

const newPaid = new Counter('new_paid_completed');
const paidUnits = new Counter('paid_units');
const replays = new Counter('replay_completed');
const failures = new Counter('expected_failures');
const denials = new Counter('quota_denials');
const unexpected = new Counter('unexpected_responses');
const paidHTTP = new Trend('new_paid_http_ms');
const replayHTTP = new Trend('replay_http_ms');
const admission = new Trend('quota_admission_ms');
const finalize = new Trend('quota_finalize_ms');
const release = new Trend('quota_release_ms');
// One sample per NEW paid request: admission + finalization, not a sum of percentiles.
const totalQuota = new Trend('quota_total_ms');

export const options = {
  scenarios: {
    quota: fixedIterations !== null ? {
      executor: 'shared-iterations',
      vus,
      iterations: fixedIterations,
      maxDuration: __ENV.DURATION || '30s',
      gracefulStop: '35s',
    } : {
      executor: 'constant-arrival-rate',
      rate,
      timeUnit: '1s',
      duration: __ENV.DURATION || '10s',
      preAllocatedVUs: vus,
      maxVUs,
      gracefulStop: '35s',
    },
  },
  // Never tag thousands of org IDs/keys/URLs: group by endpoint + workload + kind.
  systemTags: ['status', 'method', 'name', 'scenario', 'expected_response'],
  summaryTrendStats: ['avg', 'min', 'med', 'p(95)', 'p(99)', 'max'],
  thresholds: {
    checks: ['rate==1'],
    unexpected_responses: ['count==0'],
    dropped_iterations: ['count==0'],
    ...(workload !== 'failure' ? {
      quota_total_ms: [`p(95)<${quotaTarget}`],
      new_paid_completed: ['count>0'],
    } : {}),
    ...(workload === 'retry' ? { replay_completed: ['count>0'] } : {}),
    ...(workload === 'failure' ? { expected_failures: ['count>0'] } : {}),
    ...(workload === 'exhaustion' ? { quota_denials: ['count>0'] } : {}),
  },
};

function uuid() {
  const bytes = new Uint8Array(crypto.randomBytes(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function timings(response) {
  const result = {};
  const header = response.headers['Server-Timing'] || '';
  for (const part of header.split(',')) {
    const match = part.trim().match(/^(quota_admission|quota_finalize|quota_release);dur=([0-9.]+);desc="([a-z_]+)"$/);
    if (match) result[match[1]] = { duration: Number(match[2]), outcome: match[3] };
  }
  return result;
}

function send(org, key, payload, kind, prior) {
  const expectedStatus = kind === 'failure' ? (payload.demo_behavior === 'fail' ? 502 : 504) : 200;
  const response = http.post(`${baseURL}/orgs/${org.id}/schedule-searches`, JSON.stringify(payload), {
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
    tags: { name: 'POST /orgs/:org_id/schedule-searches', workload, kind },
    timeout: '30s',
    responseCallback: workload === 'exhaustion' ? http.expectedStatuses(200, 429) : http.expectedStatuses(expectedStatus),
  });
  const spans = timings(response);
  const denied = workload === 'exhaustion' && response.status === 429;
  let body;
  try { body = response.json(); } catch (_) { body = null; }
  const validResult = body && body.status === 'DONE' && Number.isInteger(body.operation_id) &&
    Array.isArray(body.results) && body.results.length === payload.routes.length &&
    body.results.every((r, i) => r.route === payload.routes[i] &&
      JSON.stringify(r.sailings) === JSON.stringify(['demo-sailing-001']));
  const validTimings = spans.quota_admission && Number.isFinite(spans.quota_admission.duration) &&
    (denied ? spans.quota_admission.outcome === 'error' : spans.quota_admission.outcome === 'ok') &&
    (kind === 'new' && !denied ? spans.quota_finalize && Number.isFinite(spans.quota_finalize.duration) && spans.quota_finalize.outcome === 'ok' : true) &&
    (kind === 'failure' ? spans.quota_release && Number.isFinite(spans.quota_release.duration) && spans.quota_release.outcome === 'ok' : true) &&
    (kind === 'replay' ? !spans.quota_finalize && !spans.quota_release : true);
  const valid = check(response, {
    'expected HTTP status': r => r.status === expectedStatus || denied,
    'valid result or explicit rejection': () => response.status === 200 ? Boolean(validResult) :
      Boolean(body && typeof body.detail === 'string'),
    'quota timing phases present': () => Boolean(validTimings),
    // Compare JSON values, not object-property serialization order.
    'replay preserves saved result': () => kind !== 'replay' || Boolean(body && prior &&
      body.operation_id === prior.operation_id && body.status === prior.status &&
      Array.isArray(body.results) && body.results.length === prior.results.length &&
      body.results.every((r, i) => r.route === prior.results[i].route &&
        JSON.stringify(r.sailings) === JSON.stringify(prior.results[i].sailings))),
  }, { workload, kind });
  unexpected.add(valid ? 0 : 1);
  if (spans.quota_admission) admission.add(spans.quota_admission.duration, { workload, kind, outcome: spans.quota_admission.outcome });
  if (spans.quota_finalize) finalize.add(spans.quota_finalize.duration, { workload, outcome: spans.quota_finalize.outcome });
  if (spans.quota_release) release.add(spans.quota_release.duration, { workload, outcome: spans.quota_release.outcome });
  if (!valid) return null;
  if (denied) denials.add(1);
  else if (kind === 'failure') failures.add(1);
  else if (kind === 'replay') {
    replays.add(1);
    replayHTTP.add(response.timings.duration);
  }
  else {
    newPaid.add(1);
    paidUnits.add(payload.routes.length * manifest.unit_cost);
    paidHTTP.add(response.timings.duration);
    totalQuota.add(spans.quota_admission.duration + spans.quota_finalize.duration, { workload });
  }
  return response.status === 200 ? body : null;
}

export default function () {
  const index = exec.scenario.iterationInTest;
  const org = orgs[(workload === 'hot' || workload === 'exhaustion') ? 0 : index % orgs.length];
  const key = uuid();
  const quantity = workload === 'batch' || workload === 'exhaustion' ? routesPerBatch : 1;
  const payload = {
    routes: Array.from({ length: quantity }, (_, i) => `DEMO-ROUTE-${i}`),
    demo_behavior: workload === 'failure' ? (index % 2 === 0 ? 'fail' : 'timeout') : 'success',
  };
  const result = send(org, key, payload, workload === 'failure' ? 'failure' : 'new');
  if (workload === 'retry' && result) send(org, key, payload, 'replay', result);
}

export function handleSummary(data) {
  const output = __ENV.SUMMARY || './benchmarks/results/summary.json';
  const record = {
    fixture_run_id: manifest.run_id,
    workload,
    rate_iterations_per_second: fixedIterations === null ? rate : null,
    fixed_iterations: fixedIterations,
    requests_per_iteration: workload === 'retry' ? 'up to 2 (new then completed replay)' : 1,
    duration: __ENV.DURATION || (fixedIterations === null ? '10s' : '30s maximum'),
    preallocated_vus: vus,
    max_vus: maxVUs,
    batch_size: workload === 'batch' || workload === 'exhaustion' ? routesPerBatch : 1,
    quota_p95_target_ms: quotaTarget,
    k6_version: __ENV.K6_VERSION || 'unspecified; record k6 version separately',
    summary: data,
  };
  return {
    [output]: JSON.stringify(record, null, 2),
    stdout: JSON.stringify({ workload, metrics: Object.fromEntries(
      ['http_reqs', 'iterations', 'dropped_iterations', 'new_paid_completed', 'paid_units',
        'replay_completed', 'expected_failures', 'quota_denials', 'unexpected_responses',
        'checks', 'http_req_duration', 'new_paid_http_ms', 'replay_http_ms', 'quota_admission_ms', 'quota_finalize_ms',
        'quota_release_ms', 'quota_total_ms'].filter(name => data.metrics[name])
        .map(name => [name, data.metrics[name].values])) }, null, 2) + '\n',
  };
}
