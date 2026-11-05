// Capture the request bodies the native Virtual Lab viewers send, so ORION's
// server tools and parity badge can be contract-tested against them.
// Run against the disposable local stack with a fresh Chromium profile:
//   node orion/scripts/golden-capture.mjs          write orion/golden/
//   node orion/scripts/golden-capture.mjs --check  exit 1 if a golden drifted
import {chromium, expect} from '@playwright/test';
import {createHash} from 'node:crypto';
import {mkdir, mkdtemp, readFile, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

const origin = process.env.SANDBOX_ORIGIN || 'http://127.0.0.1:3080';
const url = new URL(origin);
if (url.protocol !== 'http:' || !['localhost', '127.0.0.1'].includes(url.hostname) || url.origin !== origin) {
  throw new Error('Golden capture requires the local synthetic stack');
}
const checkOnly = process.argv.includes('--check');
const goldenDir = new URL('../golden/', import.meta.url);
const tracked = /^\/(protein-paint|analysis)\//;
// dofetch3 sends location.hostname, which differs between 127.0.0.1, localhost and CloudFront.
const embedderPlaceholder = '<page hostname>';

const kinds = [
  {
    file: 'proteinpaint/termdb-survival.json',
    viewer: 'Correlation Plot (/?app=Correlation)',
    flow: 'J1 by hand: primary variable "Synthetic overall survival", overlay "CGS risk category", Submit',
    count: 1,
    match: ({path}) => path === '/protein-paint/termdb/survival',
  },
  {
    file: 'proteinpaint/termdb-topMutatedGenes.json',
    viewer: 'OncoMatrix (/?app=OncoMatrix)',
    flow: 'Default view on all cases',
    count: 1,
    match: ({path}) => path === '/protein-paint/termdb/topMutatedGenes',
  },
  {
    file: 'proteinpaint/termdb-matrix.json',
    viewer: 'OncoMatrix (/?app=OncoMatrix)',
    flow: 'Default view on all cases, after topMutatedGenes',
    count: 1,
    match: ({path}) => path === '/protein-paint/termdb/matrix',
  },
  {
    file: 'proteinpaint/termdb-getsamplecount.json',
    viewer: 'Differential Gene Expression (/?app=DE)',
    flow: 'Add group "KRAS mutation status" is "No mutation", then Add group "KRAS mutation status" is "Mutated"',
    count: 2,
    match: ({path, payload}) => path === '/protein-paint/termdb' && payload.getsamplecount !== undefined,
  },
  {
    file: 'proteinpaint/termdb-getsamplelist.json',
    viewer: 'Differential Gene Expression (/?app=DE)',
    flow: 'Submit (New group vs New group 1): one sample list per group, in group order',
    count: 2,
    match: ({path, payload}) => path === '/protein-paint/termdb' && payload.getsamplelist !== undefined,
  },
  {
    file: 'proteinpaint/termdb-DE-preAnalysis.json',
    viewer: 'Differential Gene Expression (/?app=DE)',
    flow: 'Submit (New group vs New group 1); group 1 is control (No mutation), group 2 is case (Mutated)',
    count: 1,
    match: ({path, payload}) => path === '/protein-paint/termdb/DE' && payload.preAnalysis,
  },
  {
    file: 'proteinpaint/termdb-DE-wilcoxon.json',
    viewer: 'Differential Gene Expression (/?app=DE)',
    flow: 'Method switched from the edgeR default to Wilcoxon, then Run Differential Gene Expression Analysis',
    count: 1,
    match: ({path, payload}) => path === '/protein-paint/termdb/DE' && !payload.preAnalysis,
  },
  {
    file: 'analysis/survival.json',
    viewer: 'Clinical Data Analysis (/?app=cDave)',
    flow: 'Default Overall Survival card on all cases, then the Sex card survival toggle (its top two values)',
    count: 2,
    match: ({path}) => path === '/analysis/v0/survival/',
  },
];

const sha256 = data => createHash('sha256').update(data).digest('hex');
function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}
// Same parse as identity/server.mjs before it bridges filter0, filter and samplelst.
function parseQuery(searchParams) {
  return Object.fromEntries([...searchParams].map(([key, value]) => {
    try {
      return [key, JSON.parse(value)];
    } catch {
      return [key, value];
    }
  }));
}
// A golden stores parsed query values, so they must re-encode to the exact bytes dofetch3 sent.
function assertQueryRoundTrips(path, search) {
  const searchParams = new URLSearchParams(search);
  const query = parseQuery(searchParams);
  if (Object.keys(query).length !== [...searchParams.keys()].length) throw new Error(`${path}: repeated query parameter`);
  for (const [key, raw] of searchParams) {
    const value = query[key];
    const encoded = value !== null && typeof value === 'object' ? JSON.stringify(value) : String(value);
    if (encoded !== raw) throw new Error(`${path}: query parameter ${key} does not round-trip`);
  }
}

function readRequest(request) {
  const requestUrl = new URL(request.url());
  const postData = request.postData();
  const query = requestUrl.search ? parseQuery(requestUrl.searchParams) : null;
  const body = postData ? JSON.parse(postData) : null;
  if (query && body) throw new Error(`${requestUrl.pathname}: request has both a query and a body`);
  return {method: request.method(), path: requestUrl.pathname, search: requestUrl.search, query, body, payload: body ?? query ?? {}};
}

function withDeadline(promise, ms, message) {
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), ms);
  });
  return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
}

async function readResponse(request) {
  const response = await request.response();
  if (!response) return {status: null, failure: request.failure()?.errorText ?? 'no response'};
  return {status: response.status(), body: await response.body()};
}

// Term wrappers created in the page get `TwBase_${n}_${E}`, where E is
// `_${Math.random()}_${Date.now()}` digits fixed per page load. n is creation order.
const sessionTermId = /^(TwBase_\d+)__[^_]+_[^_]+$/;
function normalizeTermIds(value, path, fields, termIds) {
  if (Array.isArray(value)) {
    for (const item of value) normalizeTermIds(item, `${path}[*]`, fields, termIds);
  } else if (value !== null && typeof value === 'object') {
    for (const [key, child] of Object.entries(value)) {
      if (key === '$id' && typeof child === 'string' && sessionTermId.test(child)) {
        value[key] = child.replace(sessionTermId, '$1_<page load>');
        termIds.set(child, value[key]);
        fields.add(`${path}.$id`);
      } else {
        normalizeTermIds(child, `${path}.${key}`, fields, termIds);
      }
    }
  }
}

function normalizeRequest({method, path, search, query, body}, fields, termIds) {
  if (query) assertQueryRoundTrips(path, search);
  for (const [where, payload] of [['query', query], ['body', body]]) {
    if (payload?.embedder !== undefined) {
      payload.embedder = embedderPlaceholder;
      fields.add(`${where}.embedder`);
    }
    normalizeTermIds(payload, where, fields, termIds);
  }
  return {method, path, query, body};
}

// The matrix response streams one [path, value] JSON line per sample, keyed by
// each term's shortId. The server numbers terms in the order its per-gene
// queries finish, so renumber them by the term's position in the request.
function renumberMatrixTerms(text, terms) {
  const position = new Map(terms.map((tw, index) => [tw.$id, index + 1]));
  const lines = text.split('\n').map(line => (line ? JSON.parse(line) : null));
  const refs = lines.find(line => line?.[0].length === 1 && line[0][0] === 'refs')?.[1];
  if (!refs?.byTermId) throw new Error('Matrix response has no refs.byTermId line');
  const shortIds = new Map();
  for (const [id, term] of Object.entries(refs.byTermId)) {
    if (!position.has(id)) throw new Error(`Matrix response names unrequested term ${id}`);
    shortIds.set(String(term.shortId), String(position.get(id)));
    term.shortId = position.get(id);
  }
  return lines.map(line => {
    if (!line) return '';
    const [path, value] = line;
    if (path.length !== 2 || path[0] !== 'samples') return JSON.stringify(line);
    const renumbered = Object.entries(value).map(([shortId, data]) => {
      if (!shortIds.has(shortId)) throw new Error(`Matrix sample ${path[1]} uses unknown shortId ${shortId}`);
      return [shortIds.get(shortId), data];
    });
    return JSON.stringify([path, Object.fromEntries(renumbered)]);
  }).join('\n');
}

function normalizeResponse(request, body, fields, termIds) {
  let text = body.toString('utf8');
  const original = text;
  // ProteinPaint keys matrix data by the request's term-wrapper ids.
  for (const [from, to] of termIds) {
    if (text.includes(`"${from}"`)) {
      text = text.replaceAll(`"${from}"`, `"${to}"`);
      fields.add('response.<echoed term-wrapper $id>');
    }
  }
  if (request.path === '/protein-paint/termdb/matrix') {
    text = renumberMatrixTerms(text, request.body.terms);
    fields.add('response.<matrix term shortId>');
  }
  // Gen3 Analysis labels each curve with a Python id(), a memory address.
  if (request.path === '/analysis/v0/survival/') {
    text = text.replace(/"meta":\{"id":\d+\}/g, () => {
      fields.add('response.results[*].meta.id');
      return '"meta":{"id":"<python object id>"}';
    });
  }
  return text === original ? body : Buffer.from(text, 'utf8');
}

let browser;
let page;
let step = 'start';
const seen = [];
const errors = [];
async function open(app) {
  await page.goto(`${origin}/?app=${app}`, {waitUntil: 'domcontentloaded'});
  await expect(page.getByText('Synthetic sandbox — all data is fabricated and has no clinical meaning.', {exact: true})).toBeVisible();
}
function responseTo(predicate, timeout = 120000) {
  return page.waitForResponse(response => {
    if (!tracked.test(new URL(response.url()).pathname)) return false;
    try {
      return predicate(readRequest(response.request()));
    } catch {
      return false;
    }
  }, {timeout});
}
// Waits until every tracked request has its response and none has started for quietMs.
async function settle(quietMs = 2000, timeoutMs = 120000) {
  const message = `Viewer requests did not settle during ${step}`;
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const started = seen.length;
    await withDeadline(Promise.all(seen.map(entry => entry.response)), Math.max(deadline - Date.now(), 0), message);
    await page.waitForTimeout(quietMs);
    if (seen.length === started) return;
    if (Date.now() > deadline) throw new Error(message);
  }
}

try {
  const health = await (await fetch(`${origin}/protein-paint/healthcheck`)).json();
  const proteinpaintServer = health.versionInfo?.deps?.['@sjcrh/proteinpaint-server']?.installed;
  if (!proteinpaintServer) throw new Error('ProteinPaint healthcheck has no server version');

  browser = await chromium.launch({headless: true});
  page = await browser.newPage({viewport: {width: 1800, height: 1100}});
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    const requestUrl = new URL(request.url());
    if (requestUrl.origin !== origin || !tracked.test(requestUrl.pathname)) return;
    try {
      // Read the body as soon as it arrives: navigating away discards it.
      const response = readResponse(request).catch(error => ({status: null, failure: error.message}));
      seen.push({step, request: readRequest(request), response});
    } catch (error) {
      errors.push(`Unreadable request ${request.method()} ${requestUrl.pathname}: ${error.message}`);
    }
  });

  step = 'correlation-survival';
  await open('Correlation');
  await page.locator('tr').filter({hasText: 'Primary variable'}).getByText('+', {exact: true}).click();
  await page.getByText('Synthetic overall survival', {exact: true}).click();
  // The native selector replaces its controls after choosing the primary term.
  await page.waitForTimeout(600);
  await page.getByTestId('sjpp-tw-addTermBtn-summaryInputControlTerm2surv').click();
  await page.getByText('CGS risk category', {exact: true}).filter({visible: true}).click();
  await page.waitForTimeout(600);
  const survival = responseTo(({path}) => path === '/protein-paint/termdb/survival');
  await page.getByRole('button', {name: 'Submit', exact: true}).click();
  expect((await survival).ok()).toBe(true);
  await expect(page.getByText('Probability of Survival', {exact: true})).toBeVisible();
  await settle();

  step = 'oncomatrix-default';
  const matrix = responseTo(({path}) => path === '/protein-paint/termdb/matrix');
  await open('OncoMatrix');
  expect((await matrix).ok()).toBe(true);
  await settle();

  step = 'de-add-groups';
  await open('DE');
  for (const value of ['No mutation', 'Mutated']) {
    await page.getByText('Add group', {exact: true}).click();
    // Scope to the open menu: from the second group on, the term also appears in the groups table.
    await page.locator('.sja_menu_div').filter({visible: true}).getByText('KRAS mutation status', {exact: true}).click();
    await page.locator('tr').filter({hasText: new RegExp(`^${value} \\(n=\\d+\\)`)}).getByRole('checkbox').check();
    const count = responseTo(({path, payload}) => path === '/protein-paint/termdb' && payload.getsamplecount !== undefined);
    await page.getByRole('button', {name: 'Apply', exact: true}).click();
    expect((await count).ok()).toBe(true);
  }
  await settle();

  step = 'de-preanalysis';
  const preAnalysis = responseTo(({path, payload}) => path === '/protein-paint/termdb/DE' && payload.preAnalysis);
  await page.getByText('Submit (New group vs New group 1)', {exact: true}).click();
  const groupSizes = (await (await preAnalysis).json()).data;
  expect(Object.keys(groupSizes)).toEqual(['New group', 'New group 1']);
  await expect(page.getByLabel('edgeR', {exact: true})).toBeChecked();
  await settle();

  step = 'de-run-wilcoxon';
  await page.getByText('Wilcoxon', {exact: true}).click();
  const run = responseTo(({path, payload}) => path === '/protein-paint/termdb/DE' && !payload.preAnalysis, 180000);
  await page.getByRole('button', {name: 'Run Differential Gene Expression Analysis', exact: true}).click();
  const result = await (await run).json();
  expect(result.error).toBeUndefined();
  expect(result.data.totalRows).toBeGreaterThan(0);
  await expect(page.getByText('Volcano', {exact: true})).toBeVisible({timeout: 60000});
  await settle();

  step = 'cdave-overall-survival';
  const overall = responseTo(({path}) => path === '/analysis/v0/survival/');
  await open('cDave');
  expect((await overall).ok()).toBe(true);
  await settle();

  step = 'cdave-sex-survival';
  const split = responseTo(({path, payload}) => path === '/analysis/v0/survival/' && payload.filters?.length === 2);
  await page.getByRole('button', {name: 'Select Sex survival plot', exact: true}).click();
  expect((await split).ok()).toBe(true);
  await settle();
  expect(errors).toEqual([]);

  const kept = new Set();
  const results = [];
  for (const kind of kinds) {
    const entries = seen.filter(entry => kind.match(entry.request));
    if (entries.length !== kind.count) {
      throw new Error(`${kind.file}: expected ${kind.count} request(s), saw ${entries.length} (${entries.map(e => e.step).join(', ')})`);
    }
    const normalized = new Set();
    const requests = [];
    for (const entry of entries) {
      if (kept.has(entry)) throw new Error(`${kind.file}: ${entry.request.path} already belongs to another golden`);
      kept.add(entry);
      const response = await entry.response;
      if (response.status !== 200) throw new Error(`${kind.file}: ${entry.request.path} returned ${response.status ?? response.failure}`);
      const termIds = new Map();
      const request = normalizeRequest(entry.request, normalized, termIds);
      const body = normalizeResponse(request, response.body, normalized, termIds);
      requests.push({
        step: entry.step,
        ...request,
        payloadSha256: sha256(canonicalJson(request.body ?? request.query ?? {})),
        response: {status: response.status, bytes: body.length, sha256: sha256(body)},
      });
    }
    const golden = {
      schema: 'orion.golden/v1',
      kind: kind.file.replace(/\.json$/, ''),
      viewer: kind.viewer,
      flow: kind.flow,
      stage: 'pre-bridge',
      proteinpaintServer,
      normalized: [...normalized].sort(),
      requests,
    };
    const text = `${JSON.stringify(golden, null, 2)}\n`;
    const target = new URL(kind.file, goldenDir);
    const previous = await readFile(target, 'utf8').catch(error => {
      if (error.code === 'ENOENT') return null;
      throw error;
    });
    const status = previous === null ? 'new' : previous === text ? 'unchanged' : 'changed';
    if (!checkOnly && status !== 'unchanged') {
      await mkdir(new URL('.', target), {recursive: true});
      await writeFile(target, text);
    }
    results.push({kind, golden, status});
  }

  console.log(`Golden capture from ${origin} (ProteinPaint server ${proteinpaintServer}, pre-bridge bodies)`);
  for (const {kind, golden, status} of results) {
    for (const request of golden.requests) {
      console.log([
        status.padEnd(9), kind.file.padEnd(40), request.method.padEnd(4), request.path.padEnd(32),
        `payload ${request.payloadSha256.slice(0, 12)}`, `response ${request.response.status} ${request.response.bytes} B ${request.response.sha256.slice(0, 12)}`,
      ].join('  '));
    }
  }
  const context = {};
  for (const entry of seen.filter(entry => !kept.has(entry))) {
    const key = `${entry.request.method} ${entry.request.path}`;
    context[key] = (context[key] ?? 0) + 1;
  }
  console.log(`Not kept (viewer setup): ${Object.entries(context).map(([key, n]) => `${key} x${n}`).join(', ')}`);
  console.log(`DE groups (preAnalysis): ${JSON.stringify(groupSizes)}; Wilcoxon run: ${result.data.totalRows} genes`);
  const drifted = results.filter(r => r.status !== 'unchanged');
  if (checkOnly && drifted.length) {
    console.error(`FAIL: ${drifted.length} golden file(s) differ from orion/golden: ${drifted.map(r => r.kind.file).join(', ')}`);
    process.exitCode = 1;
  } else if (checkOnly) {
    console.log(`PASS: all ${results.length} golden files match orion/golden`);
  } else {
    console.log(`PASS: ${results.length} golden files captured; ${drifted.length} written, ${results.length - drifted.length} unchanged`);
  }
} catch (error) {
  const dir = await mkdtemp(join(tmpdir(), 'orion-golden-capture-'));
  await writeFile(join(dir, 'requests.json'), JSON.stringify(seen.map(({step, request}) => ({step, ...request})), null, 2));
  if (page && !page.isClosed()) await page.screenshot({path: join(dir, 'failure.png'), fullPage: true}).catch(() => {});
  console.error(`Golden capture failed during ${step}; diagnostics in ${dir}`);
  throw error;
} finally {
  if (browser) {
    await browser.close().catch(error => {
      console.error('Could not close the capture browser:', error);
      process.exitCode = 1;
    });
  }
}
