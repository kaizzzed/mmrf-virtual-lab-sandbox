// ORION mode L end to end: drives the real Assistant app in headless Chromium against the local
// Compose stack (orion/compose.orion.yaml), the way a researcher would, and checks what the
// screen, the BFF routes and the run records show (RFA rev 25; contract v1.2).
//
// Prerequisites: the sandbox stack is up and mode L is running (orion/scripts/orion-local.sh up),
// the root devDependency @playwright/test 1.58.2 and its Chromium are installed (the same ones
// orion/scripts/golden-capture.mjs uses). Nothing else is installed.
//
//   node orion/scripts/e2e-mode-l.mjs                 every scenario
//   node orion/scripts/e2e-mode-l.mjs j1 stop         only scenarios whose id contains a word
//   SANDBOX_ORIGIN=http://localhost:3080 node orion/scripts/e2e-mode-l.mjs
//
// Scenarios: j1-open, j1-named (J1 both phrasings, Evidence Inspector), j2 (confirmation card,
// Create, cohort list, hand-off), j3 (pause with eligible counts, two saves, DE card), edit-stop
// (an edit at the save pause, then Stop during de_run), dont-save, e21 (Enter as the card
// appears), restore (reload at the pause and during de_run), follow (plan-card edit, E14, a
// follow-up chip, thread switching), edges (E7, E10, E8, E17), e18 (Unsupported, Open on this
// cohort). orion/notes/mode-l-status.md lists what each checks.
//
// Each scenario runs in a fresh browser context (its own sessionStorage, localStorage and
// IndexedDB), so the saved cohorts of one never leak into another. Screenshots go to
// orion/notes/screenshots/e2e/ (gitignored), with summary.json. Exit code 1 when any scenario
// fails. All data is synthetic.
import {chromium, expect} from '@playwright/test';
import {mkdir, readdir, rm, writeFile} from 'node:fs/promises';

const origin = process.env.SANDBOX_ORIGIN || 'http://127.0.0.1:3080';
const url = new URL(origin);
if (url.protocol !== 'http:' || !['localhost', '127.0.0.1'].includes(url.hostname) || url.origin !== origin) {
  throw new Error('The mode L end-to-end run requires the local synthetic stack');
}
const shotsDir = new URL('../notes/screenshots/e2e/', import.meta.url);
const filters = process.argv.slice(2).map(word => word.toLowerCase());

const SYNTHETIC_NOTICE = 'Synthetic sandbox — all data is fabricated and has no clinical meaning.';
const ANSWER_PARTS = ['Observed', 'Interpretation', 'Hypotheses and next steps', 'Limitations'];
const PROMPTS = {
  j1Named: 'Use Correlation Plot to compare Kaplan–Meier survival of CGS high-risk vs. CGS standard-risk patients',
  j1Open: 'Compare survival of CGS high-risk vs. CGS standard-risk patients',
  j2Open: 'Top 3 most frequently mutated genes in Black or African American patients',
  j3Open: 'Differential expression between patients with and without a KRAS mutation, top 10 genes',
  venn: 'Show a Venn diagram of the two KRAS cohorts',
  // orion/evals/prompts.json "edge".
  e7: 'Use OncoMatrix to plot survival by CGS risk category',
  e8: 'Differential expression between patients with and without an IDH1 mutation, top 10 genes',
  e10: 'Compare survival of high-risk patients with everyone else',
  e17: 'Which drug should a KRAS-mutated patient get?',
};
const J1_ROWS = ['1. Count both CGS risk groups', '2. Compare survival: Kaplan–Meier, split by CGS risk category'];
const J2_ROWS = ['1. Find Black or African American cases', '2. Rank mutated genes: top 3', '3. Save 1 cohort'];
const J3_ROWS = [
  '1. Find KRAS-mutated cases',
  '2. Find mutation-tested cases',
  '3. Subtract to get KRAS not detected',
  '4. Check RNA-seq-eligible group sizes',
  '5. Save 2 cohorts',
  '6. Run DE: Wilcoxon, rank by adjusted p',
];
const DE_SORT_CAPTION = 'Table sorted by fold change. Sort by adjusted p-value to match the answer.';
const RUN_TIMEOUT = 90_000;
const MOUNT_TIMEOUT = 75_000;

// ------------------------------------------------------------------------------------------ page
// Recorded in the page on every DOM change outside the viewers (a MutationObserver, so a state
// that shows for a single frame, such as "Stopping", is not missed): the latest run's card
// headers, its step words and the composer's Send/Stop button.
const timelineRecorder = () => {
  window.__orionTimeline = [];
  const t0 = performance.now();
  let last = '';
  const record = () => {
    const article = [...document.querySelectorAll('[role=log] > article')].pop();
    const headers = article
      ? [...article.querySelectorAll('section > header')].map(h => h.innerText.replace(/\s+/g, ' ').trim())
      : [];
    const steps = article
      ? [...article.querySelectorAll('ul[aria-label^="Steps in row"] > li')].map(li => {
          const name = li.querySelector('span.font-mono')?.textContent ?? '?';
          const word = li.querySelector('span > span')?.textContent ?? '?';
          return `${name}=${word.split(' · ')[0]}`;
        })
      : [];
    const button = [...document.querySelectorAll('button')]
      .map(b => b.textContent.trim())
      .find(text => text === 'Send' || text === 'Stop' || text === 'Stopping') ?? null;
    const snapshot = JSON.stringify([headers, steps, button]);
    if (snapshot !== last) {
      last = snapshot;
      window.__orionTimeline.push({t: Math.round(performance.now() - t0), headers, steps, button});
    }
  };
  const inViewer = node => (node.nodeType === 1 ? node : node.parentElement)?.closest?.('[data-orion-viz]');
  const observer = new MutationObserver(records => {
    if (records.some(r => !inViewer(r.target))) record();
  });
  const start = () => observer.observe(document.body, {subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ['disabled']});
  if (document.body) start();
  else document.addEventListener('DOMContentLoaded', start);
};

let browser;
const results = [];

class Scenario {
  constructor(id, title) {
    this.id = id;
    this.title = title;
    this.checks = [];
    this.shots = [];
    this.errors = [];
  }

  async open() {
    this.context = await browser.newContext({viewport: {width: 1600, height: 1100}});
    await this.context.addInitScript(timelineRecorder);
    this.page = await this.context.newPage();
    this.page.on('pageerror', error => this.errors.push(`pageerror: ${error.message}`));
    this.page.on('console', message => {
      if (message.type() === 'error') this.errors.push(`console: ${message.text().slice(0, 300)}`);
    });
    await this.gotoAssistant();
  }

  async gotoAssistant() {
    await this.page.goto(`${origin}/?app=Assistant`, {waitUntil: 'domcontentloaded'});
    await expect(this.page.getByText(SYNTHETIC_NOTICE, {exact: true}).first()).toBeVisible({timeout: 60_000});
    await expect(this.page.getByTestId('orion-assistant')).toBeVisible({timeout: 60_000});
  }

  pass(text) {
    this.checks.push(text);
  }

  async shot(name) {
    const file = `${this.id}-${String(this.shots.length + 1).padStart(2, '0')}-${name}.png`;
    await this.page.mouse.move(0, 0).catch(() => {});
    await this.page.screenshot({path: new URL(file, shotsDir).pathname, fullPage: true});
    this.shots.push(file);
  }

  // ------------------------------------------------------------------------------ locators
  get assistant() {
    return this.page.getByTestId('orion-assistant');
  }
  /** The thread's latest question and everything its run shows. */
  get run() {
    return this.page.locator('[role=log] > article').last();
  }
  get planCard() {
    return this.run.locator('section').filter({has: this.page.getByRole('heading', {name: /^Plan/})}).first();
  }
  get answerCard() {
    return this.run.locator('section').filter({has: this.page.getByRole('heading', {name: 'Answer', exact: true})});
  }
  get composer() {
    return this.page.getByRole('textbox', {name: 'Ask ORION'});
  }
  section(name) {
    return this.answerCard.locator('div').filter({has: this.page.getByRole('heading', {name, exact: true, level: 4})}).last();
  }

  // ------------------------------------------------------------------------------ actions
  async clearTimeline() {
    await this.page.evaluate(() => {
      window.__orionTimeline.length = 0;
    });
  }
  async timeline() {
    return this.page.evaluate(() => window.__orionTimeline.slice());
  }
  async ask(text) {
    await this.clearTimeline();
    await this.composer.fill(text);
    await this.composer.press('Enter');
    await expect(this.page.getByRole('article', {name: text, exact: true})).toBeVisible();
  }
  async pickStarter(journey, phrasing, text) {
    await this.clearTimeline();
    await this.page.getByRole('button', {name: new RegExp(`^${journey} · ${phrasing}`)}).click();
    await expect(this.page.getByRole('article', {name: text, exact: true})).toBeVisible();
  }
  /** The run's screen word, from the plan card or the card that carries it. */
  async waitStatus(word, timeout = RUN_TIMEOUT) {
    // Every card's header ends with a status label: an icon and the screen word in a span.
    await expect(this.run.locator('section > header > span > span').filter({hasText: new RegExp(`^${escape(word)}$`)}).first())
      .toBeVisible({timeout});
  }
  stepRow(name) {
    return this.run.locator('ul[aria-label^="Steps in row"] > li').filter({hasText: name});
  }
  async stepWord(name) {
    return (await this.stepRow(name).locator('span > span').first().textContent()) ?? '';
  }
  /** The tab's thread state (sessionStorage `orion:assistant:v1`): ids of the latest run. */
  async ids() {
    return this.page.evaluate(() => {
      const tab = JSON.parse(sessionStorage.getItem('orion:assistant:v1'));
      const thread = tab.threads[tab.activeThreadId];
      return {threadId: thread.threadId, runId: thread.runIds[thread.runIds.length - 1], runIds: thread.runIds};
    });
  }
  /** GET the record route as the page would (same cookies): the stored run record. */
  async record(runId, threadId) {
    const response = await this.page.request.get(`${origin}/api/orion/runs/${runId}/record?threadId=${threadId}`);
    expect(response.status(), `record route for ${runId}`).toBe(200);
    return response.json();
  }
  async view(runId, threadId) {
    const response = await this.page.request.get(`${origin}/api/orion/runs/${runId}/view?threadId=${threadId}`);
    expect(response.status(), `view route for ${runId}`).toBe(200);
    return response.json();
  }
  /** The names in the platform's own cohort list (the cohort bar's select). */
  async cohortList() {
    const select = this.page.locator('input.mantine-Select-input').first();
    await select.click();
    const listbox = this.page.getByRole('listbox');
    await expect(listbox).toBeVisible();
    const names = await listbox.getByRole('option').allInnerTexts();
    await this.page.keyboard.press('Escape');
    await expect(listbox).toBeHidden();
    return {names: names.map(n => n.trim()), active: await select.inputValue()};
  }
  async saveLedgerKeys() {
    return this.page.evaluate(() => Object.keys(localStorage).filter(key => key.startsWith('orion:save-ledger:')));
  }

  // ------------------------------------------------------------------------------ checks
  async checkPlan(rows, scope = 'All cases') {
    const plan = this.planCard;
    await expect(plan.getByRole('heading', {name: /^Plan, version \d+$/})).toBeVisible({timeout: RUN_TIMEOUT});
    // The scope line, its count a fact chip once a step sized the scope.
    const scopeLine = plan.locator('p').filter({hasText: /^Scope: /}).first();
    await expect(scopeLine).toContainText(`Scope: ${scope} · `);
    await expect(scopeLine.getByRole('button', {name: /^Row \d+ · s\d+ \w+$/})).toBeVisible();
    const labels = (await plan.locator('ol[aria-label="Plan rows"] > li > div > span.font-medium').allTextContents()).map(t => t.trim());
    expect(labels).toEqual(rows);
    // Each row names its tool and its steps (C-15, C-35).
    expect(await plan.locator('ol[aria-label="Plan rows"] > li').filter({hasText: /Tool: \w+ ·/}).count()).toBe(rows.length);
    this.pass(`plan card: scope line "${(await scopeLine.innerText()).replace(/\s+/g, ' ')}" and rows ${rows.length}`);
  }

  /** Steps went live: the plan was on screen before any step finished, and steps finished one by one. */
  async checkLiveSteps() {
    const timeline = await this.timeline();
    const firstPlan = timeline.find(s => s.headers.some(h => /^Plan, version/.test(h)));
    const firstDone = timeline.find(s => s.steps.some(step => step.endsWith('=Done')));
    expect(firstPlan, 'the plan card appeared').toBeTruthy();
    expect(firstDone, 'a step finished').toBeTruthy();
    expect(firstPlan.t, 'plan on screen before the first step finished').toBeLessThanOrEqual(firstDone.t);
    const partial = timeline.filter(s => s.steps.some(x => x.endsWith('=Done')) && s.steps.some(x => !/=(Done|Skipped|Reused|Empty)$/.test(x)));
    expect(partial.length, 'snapshots with some steps done and others not yet').toBeGreaterThan(0);
    const running = timeline.some(s => s.headers.some(h => / Running$/.test(h)));
    expect(running, 'the header read "Running"').toBe(true);
    const stop = timeline.some(s => s.button === 'Stop');
    expect(stop, 'Send turned into Stop while the run was active').toBe(true);
    this.pass(`live steps: plan at ${firstPlan.t} ms, first step done at ${firstDone.t} ms, ${partial.length} partial snapshots, Send became Stop`);
  }

  async checkAnswer(observedIncludes) {
    const answer = this.answerCard;
    await expect(answer).toBeVisible();
    const parts = (await answer.locator('h4').allTextContents()).map(t => t.trim());
    expect(parts).toEqual(ANSWER_PARTS);
    for (const text of observedIncludes) await expect(this.section('Observed')).toContainText(text);
    // Every number in Observed carries a chip "Row n · sN tool" (FR-16).
    const chips = await this.section('Observed').getByRole('button', {name: /^Row \d+ · s\d+ \w+$/}).count();
    expect(chips).toBeGreaterThan(0);
    await expect(answer.locator('p').last()).toHaveText(SYNTHETIC_NOTICE);
    this.pass(`answer: ${parts.join(', ')} in order; Observed has ${chips} chips; ends with the notice`);
  }

  /** Observed on screen (chips removed) equals the run record's own Observed sentences. */
  async checkObservedMatchesRecord(record) {
    const screen = await this.section('Observed').locator('p').evaluateAll(nodes =>
      nodes.map(node => {
        const clone = node.cloneNode(true);
        clone.querySelectorAll('button').forEach(button => button.remove());
        return clone.textContent.replace(/\s+/g, ' ').trim();
      }),
    );
    const stored = record.runView.observed.map(sentence =>
      sentence.segments.map(segment => segment.text).join('').replace(/\s+/g, ' ').trim(),
    );
    expect(screen).toEqual(stored);
    this.pass(`Observed on screen equals the record's ${stored.length} sentences`);
  }

  async checkViz({title, caption = null}) {
    const card = this.run.locator('[data-testid^="orion-viz-card-"]').filter({hasText: title}).first();
    await expect(card).toBeVisible({timeout: RUN_TIMEOUT});
    const viewer = card.locator('[data-phase]').first();
    await expect(viewer).toHaveAttribute('data-phase', 'mounted', {timeout: MOUNT_TIMEOUT});
    const status = await card.getByTestId('orion-viz-status').innerText();
    expect(status).toMatch(/Live · ready in/);
    const parity = card.getByTestId('orion-viz-parity');
    await expect(parity).toHaveAttribute('data-parity', 'match', {timeout: 30_000});
    const badge = (await parity.locator('span').first().textContent()).trim();
    expect(badge).toMatch(/^Parity: matches s\d+ \w+$/);
    if (caption) await expect(card).toContainText(caption);
    // The viewer drew into the card: ProteinPaint's own SVG is inside it.
    expect(await card.locator('svg').count()).toBeGreaterThan(2);
    this.pass(`viz "${title}": live (${status.split('\n')[0].trim()}), ${badge}${caption ? ', with the DE sort caption' : ''}`);
    return card;
  }

  /** The browser's viz report reached the record (on the next turn or standalone after 2 s). */
  async checkVizReport(runId, threadId) {
    let record;
    for (let attempt = 0; attempt < 20; attempt += 1) {
      record = await this.record(runId, threadId);
      if (record.vizReports.some(r => r.mounted === true)) break;
      await this.page.waitForTimeout(500);
    }
    const report = record.vizReports.find(r => r.mounted === true);
    expect(report, 'a mounted viz report in the record').toBeTruthy();
    expect(report.error).toBeNull();
    expect(report.pinnedHashVerified).toBe(true);
    this.pass(`record has the viz report: ${report.vizId} mounted, pinnedHashVerified true`);
  }

  async checkRecord(runId, threadId, {status, question}) {
    const record = await this.record(runId, threadId);
    expect(record.runId).toBe(runId);
    expect(record.threadId).toBe(threadId);
    expect(record.status).toBe(status);
    expect(record.runView.status).toBe(status);
    if (question) expect(record.question).toBe(question);
    expect(record.environment).toMatchObject({mode: 'L', planner: 'stub', composer: 'stub', toolTransport: 'in_process'});
    this.pass(`run record ${runId.slice(0, 8)}… via the record route: ${record.status}, ${record.calls.length} calls, ${record.facts.length} facts, environment mode L, stub planner and composer`);
    return record;
  }
}

const escape = text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

async function scenario(id, title, body) {
  if (filters.length > 0 && !filters.some(word => id.includes(word))) return;
  const s = new Scenario(id, title);
  for (const file of await readdir(shotsDir)) {
    if (file.startsWith(`${id}-`) && file.endsWith('.png')) await rm(new URL(file, shotsDir));
  }
  const started = Date.now();
  process.stdout.write(`- ${id}: ${title} ... `);
  try {
    await s.open();
    await body(s);
    await s.shot('end');
    const relevant = s.errors.filter(e => !/Failed to load resource|favicon/.test(e));
    expect(relevant, 'no page errors').toEqual([]);
    results.push({id, title, ok: true, ms: Date.now() - started, checks: s.checks, shots: s.shots});
    console.log(`PASS (${s.checks.length} checks, ${((Date.now() - started) / 1000).toFixed(1)} s)`);
  } catch (error) {
    if (s.page && !s.page.isClosed()) await s.shot('failure').catch(() => {});
    const timeline = s.page && !s.page.isClosed() ? await s.timeline().catch(() => []) : [];
    results.push({id, title, ok: false, ms: Date.now() - started, checks: s.checks, shots: s.shots,
      error: error.message.split('\n').slice(0, 12).join('\n'), pageErrors: s.errors.slice(0, 10), timeline: timeline.slice(-12)});
    console.log(`FAIL after ${s.checks.length} checks\n${error.message.split('\n').slice(0, 12).map(l => `    ${l}`).join('\n')}`);
  } finally {
    await s.context?.close().catch(() => {});
  }
}

// ------------------------------------------------------------------------------------- journeys
async function j1(s, phrasing, question) {
  await expect(s.page.getByText('Try a reference question')).toBeVisible();
  await expect(s.page.getByText(/^Scope: All cases · /).first()).toBeVisible();
  await s.pickStarter('J1', phrasing, question);
  await s.checkPlan(J1_ROWS);
  await s.shot('plan');
  await s.waitStatus('Complete');
  await s.checkLiveSteps();
  for (const [name, output] of [['s2 cohort_count', 'cases'], ['s3 cohort_count', 'cases'], ['s4 survival_compare', 'log-rank p reported'], ['s5 derive_km_median', '2 medians']]) {
    await expect(s.stepRow(name)).toContainText(output);
    expect(await s.stepWord(name)).toMatch(/^Done/);
  }
  s.pass('steps s2–s5 Done with their outputs');
  const card = await s.checkViz({title: 'Overall survival by CGS risk category'});
  await s.shot('survival-card');
  await s.checkAnswer(['High risk: ', 'Standard risk: ', 'Log-rank p = ', 'Median overall survival']);
  await expect(s.section('Limitations')).toContainText('p = 0');
  const {runId, threadId} = await s.ids();
  const record = await s.checkRecord(runId, threadId, {status: 'Complete', question});
  await s.checkObservedMatchesRecord(record);
  await s.checkVizReport(runId, threadId);
  return {card, runId, threadId, record};
}

await mkdir(shotsDir, {recursive: true});
for (const name of ['summary.json']) await rm(new URL(name, shotsDir), {force: true});
browser = await chromium.launch({headless: true});
try {
  await scenario('j1-open', 'J1, open-ended: plan, live steps, survival card, answer, record, Evidence Inspector', async s => {
    const {runId, threadId} = await j1(s, 'open-ended', PROMPTS.j1Open);
    // The Evidence Inspector from a chip: "Row n · sN tool", the call, the export.
    const chip = s.section('Observed').getByRole('button', {name: 'Row 1 · s2 cohort_count'}).first();
    await chip.click();
    const inspector = s.page.locator('aside').filter({has: s.page.getByRole('heading', {name: 'Evidence Inspector'})});
    await expect(inspector).toBeVisible();
    await expect(inspector.getByRole('heading', {name: 'Evidence Inspector'})).toBeFocused();
    await expect(inspector.getByRole('heading', {name: 'Row 1 · s2 cohort_count', exact: true})).toBeVisible();
    await expect(inspector.getByLabel('Fact', {exact: true})).toContainText('Value');
    await expect(inspector.getByLabel('Calls', {exact: true}).locator('ol > li').first()).toContainText('Attempt 1 · ok');
    await expect(inspector.getByLabel('Fact', {exact: true})).toContainText('In the call');
    const exportLink = inspector.getByRole('link', {name: 'Export the run record as JSON'});
    const href = await exportLink.getAttribute('href');
    const download = await s.page.request.get(new URL(href, origin).href);
    expect(download.status()).toBe(200);
    expect(download.headers()['content-disposition']).toContain(`orion-run-${runId}.json`);
    expect((await download.json()).runId).toBe(runId);
    await s.shot('inspector');
    await s.page.keyboard.press('Escape');
    await expect(inspector).toBeHidden();
    await expect(chip).toBeFocused();
    s.pass('Evidence Inspector: opened from the chip on "Row 1 · s2 cohort_count", call and fact shown, export downloads the record, Esc closes and returns focus');
    expect(threadId).toMatch(/^[0-9a-f]{32}$/);
    // C-06: J1's own chip asks a question the stub planner runs (top mutated genes in the scope), a new run to Complete.
    await s.answerCard.getByRole('button', {name: 'Top mutated genes in this cohort'}).click();
    await expect(s.page.locator('[role=log] > article')).toHaveCount(2);
    await s.waitStatus('Complete');
    await expect(s.planCard).toContainText('Rank mutated genes');
    s.pass('follow-up chip "Top mutated genes in this cohort" ran as a second run in the thread, to Complete');
  });

  await scenario('j1-named', 'J1, tool-named: the same plan, live steps, card and answer', async s => {
    const {record} = await j1(s, 'tool-named', PROMPTS.j1Named);
    expect(record.planHistory.length).toBeGreaterThan(0);
  });

  await scenario('j2', 'J2: plan, confirmation card, Create, cohort list, Complete, OncoMatrix card', async s => {
    const before = await s.cohortList();
    await s.pickStarter('J2', 'open-ended', PROMPTS.j2Open);
    await s.checkPlan(J2_ROWS);
    const card = s.run.locator('section').filter({has: s.page.getByRole('heading', {name: 'Save 1 cohort', exact: true, level: 3})});
    await expect(card).toBeVisible({timeout: RUN_TIMEOUT});
    await s.waitStatus('Waiting for you');
    await expect(card.getByRole('heading', {name: 'Save 1 cohort', level: 3})).toBeFocused();
    s.pass('confirmation card appeared with focus on its heading');
    await expect(card).toContainText('Black or African American (ORION)');
    await expect(card).toContainText('Race = black or african american');
    await expect(card.getByRole('button', {name: /^Row \d+ · s\d+ cases_query$/})).toBeVisible();
    await expect(card.getByRole('checkbox')).not.toBeChecked();
    // No default button: no form, every button is type="button", and Enter on the card saves nothing.
    expect(await card.locator('form').count()).toBe(0);
    expect(await s.assistant.locator('button:not([type="button"])').count()).toBe(0);
    await s.page.keyboard.press('Enter');
    await s.page.waitForTimeout(1000);
    await expect(card).toBeVisible();
    expect(await s.saveLedgerKeys()).toEqual([]);
    expect(await s.stepWord('s5 save_cohorts')).toMatch(/^Needs you/);
    s.pass('no default button: no form, every button type="button", Enter on the focused card saved nothing');
    await s.shot('confirmation');
    await card.getByRole('button', {name: 'Create', exact: true}).click();
    await s.waitStatus('Complete');
    await s.checkLiveSteps();
    await expect(s.stepRow('s5 save_cohorts')).toContainText('1 cohort saved and read back');
    const after = await s.cohortList();
    expect(after.names).toContain('Black or African American (ORION)');
    expect(before.names).not.toContain('Black or African American (ORION)');
    expect(after.active).toBe(before.active);
    s.pass(`cohort list: "Black or African American (ORION)" added (${after.names.length} cohorts), the active cohort unchanged ("${after.active}")`);
    await s.checkViz({title: 'Top mutated genes: Black or African American'});
    await s.shot('oncomatrix-card');
    await s.checkAnswer(['The cohort has ', 'The most frequently mutated genes are ']);
    await expect(s.section('Limitations')).toContainText('six-gene panel');
    const {runId, threadId} = await s.ids();
    const record = await s.checkRecord(runId, threadId, {status: 'Complete', question: PROMPTS.j2Open});
    expect(record.confirmations.length).toBe(1);
    await s.checkObservedMatchesRecord(record);
    await s.checkVizReport(runId, threadId);
    // Hand-off: the card's cohort is not active, so the button says it switches; same tab; Back restores.
    const viz = s.run.locator('[data-testid^="orion-viz-card-"]').first();
    await viz.getByRole('button', {name: 'Make active and open'}).click();
    await s.page.waitForURL(/[?&]app=OncoMatrix\b/, {timeout: 30_000});
    await expect(s.page.locator('input.mantine-Select-input').first()).toHaveValue('Black or African American (ORION)', {timeout: 30_000});
    await expect(s.page.getByText('Active cohort: Black or African American (ORION).')).toBeVisible();
    await s.shot('handoff');
    s.pass('Make active and open: OncoMatrix opened in the same tab with the saved cohort active, with an undo notice');
    await s.page.goBack({waitUntil: 'domcontentloaded'});
    await expect(s.page.getByRole('article', {name: PROMPTS.j2Open, exact: true})).toBeVisible({timeout: 60_000});
    await s.waitStatus('Complete');
    await expect(s.answerCard.locator('h4')).toHaveText(ANSWER_PARTS);
    s.pass('browser Back returns to the thread, restored Complete with its answer');
    // C-06: J2's own chip, "Survival by <top gene> mutation", compares that gene's mutated and other tested cases.
    await s.answerCard.getByRole('button', {name: /^Survival by [A-Z0-9]+ mutation$/}).click();
    await expect(s.page.locator('[role=log] > article')).toHaveCount(2);
    await s.waitStatus('Complete');
    await expect(s.planCard).toContainText('survival');
    s.pass('follow-up chip "Survival by <gene> mutation" ran as a second run in the thread, to Complete');
    // FR-09, E3/E4: the chat-made cohort opens in Cohort Builder (still active since the hand-off) with its filter and size.
    const size = record.facts.find(f => f.stepId === 's2' && f.unit === 'cases').value;
    await s.page.getByRole('link', {name: 'Cohort Builder', exact: true}).click();
    await s.page.waitForURL(/[?&]app=CohortBuilder\b/, {timeout: 30_000});
    await expect(s.page.locator('input.mantine-Select-input').first()).toHaveValue('Black or African American (ORION)', {timeout: 30_000});
    const filters = s.page.getByTestId('text-cohort-filters');
    await expect(filters).toContainText(/race/i, {timeout: 30_000});
    await expect(filters).toContainText('black or african american');
    await expect(s.page.getByText(new RegExp(`^${size.toLocaleString('en-US')} cases$`, 'i')).first()).toBeVisible({timeout: 30_000});
    await s.shot('cohort-builder');
    s.pass(`opens in Cohort Builder: "Black or African American (ORION)" with Race = black or african american and ${size} cases`);
  });

  await scenario('j3', 'J3: plan, pause with eligible counts, Create (two cohorts), DE card with the sort caption, Complete', async s => {
    await s.pickStarter('J3', 'open-ended', PROMPTS.j3Open);
    await s.checkPlan(J3_ROWS);
    const card = s.run.locator('section').filter({has: s.page.getByRole('heading', {name: 'Save 2 cohorts', exact: true, level: 3})});
    await expect(card).toBeVisible({timeout: RUN_TIMEOUT});
    await s.waitStatus('Waiting for you');
    await expect(card.getByRole('heading', {name: 'Save 2 cohorts', level: 3})).toBeFocused();
    for (const name of ['KRAS not detected (ORION)', 'KRAS mutated (ORION)']) {
      const item = card.locator('li').filter({has: s.page.getByRole('heading', {name, exact: true, level: 4})});
      await expect(item).toContainText(/\d[\d,]*\s?Row \d+ · s\d+ \w+ cases · \d[\d,]*\s?Row \d+ · s\d+ de_preanalysis RNA-seq eligible/);
    }
    s.pass('pause: two cohorts, each with its case count and RNA-seq-eligible count as chips, before Create');
    expect(await s.stepWord('s9 de_run')).toMatch(/^Next/);
    await s.shot('confirmation');
    await card.getByRole('button', {name: 'Create', exact: true}).click();
    await s.waitStatus('Complete');
    await s.checkLiveSteps();
    await expect(s.stepRow('s8 save_cohorts')).toContainText('2 cohorts saved and read back');
    const {names} = await s.cohortList();
    expect(names).toEqual(expect.arrayContaining(['KRAS not detected (ORION)', 'KRAS mutated (ORION)']));
    s.pass('cohort list: both KRAS cohorts saved');
    const de = await s.checkViz({title: 'Differential expression: KRAS mutated vs. KRAS not detected', caption: DE_SORT_CAPTION});
    // C-36: the viewer's own DE request reads the ProteinPaint cache entry s9 de_run made.
    const requests = de.getByTestId('orion-viz-requests');
    await requests.locator('summary').click();
    await expect(requests.locator('li').filter({hasText: 'POST /termdb/DE'})).toContainText('server cache: same entry as s9 de_run');
    s.pass('viewer requests: POST /termdb/DE read "server cache: same entry as s9 de_run"');
    await s.shot('de-card');
    await s.checkAnswer(['Mutation-tested cases: ', 'KRAS-mutated: ', 'KRAS not detected: ', 'Top-ranked genes by adjusted p-value: ']);
    const {runId, threadId} = await s.ids();
    const record = await s.checkRecord(runId, threadId, {status: 'Complete', question: PROMPTS.j3Open});
    expect(record.confirmations.length).toBe(1);
    await s.checkObservedMatchesRecord(record);
    await s.checkVizReport(runId, threadId);
  });

  await scenario('edit-stop', 'J3: an edit at the save pause (plan version 2), then Stop during de_run ("Stopping", then Stopped by you)', async s => {
    await s.pickStarter('J3', 'open-ended', PROMPTS.j3Open);
    const v1 = s.run.locator('section').filter({has: s.page.getByRole('heading', {name: 'Save 2 cohorts', level: 3})});
    await expect(v1).toBeVisible({timeout: RUN_TIMEOUT});
    const {runId, threadId} = await s.ids();
    const firstNonce = (await s.view(runId, threadId)).pending.nonce;
    // A unique name, so de_run misses ProteinPaint's cache and runs long enough to stop.
    const name = `KRAS mutated e2e ${Date.now().toString(36)}`;
    await v1.getByRole('button', {name: 'Rename KRAS mutated (ORION)'}).click();
    const field = v1.getByRole('textbox', {name: 'New name for KRAS mutated (ORION)'});
    await field.fill(name);
    await field.press('Enter');
    await s.page.waitForTimeout(500);
    await expect(s.planCard.getByRole('heading', {name: 'Plan, version 1'})).toBeVisible();
    s.pass('Enter in the rename field does nothing (no form)');
    await v1.getByRole('button', {name: 'Use this name'}).click();
    await expect(s.planCard.getByRole('heading', {name: 'Plan, version 2'})).toBeVisible({timeout: RUN_TIMEOUT});
    await expect(s.planCard).toContainText('Replaces version 1');
    const v2 = s.run.locator('section').filter({has: s.page.getByRole('heading', {name: 'Save 2 cohorts', level: 3})});
    await expect(v2.getByRole('heading', {name, level: 4})).toBeVisible({timeout: RUN_TIMEOUT});
    await s.waitStatus('Waiting for you');
    const view = await s.view(runId, threadId);
    expect(view.plan.version).toBe(2);
    expect(view.pending.nonce).not.toBe(firstNonce);
    expect(view.superseded.map(x => x.planVersion)).toEqual([1]);
    s.pass(`edit at the save pause: plan version 2 replaces version 1, the v1 save (nonce ${firstNonce.slice(0, 8)}…) is voided, a new card names "${name}"`);
    await s.shot('edited');

    await s.clearTimeline();
    await v2.getByRole('button', {name: 'Create', exact: true}).click();
    await expect(s.stepRow('s9 de_run').locator('span > span').first()).toHaveText(/^Running/, {timeout: 30_000});
    await s.page.getByRole('button', {name: 'Stop', exact: true}).click();
    await s.waitStatus('Stopped by you', 60_000);
    const timeline = await s.timeline();
    const stopping = timeline.some(x => x.button === 'Stopping' || x.headers.some(h => / Stopping$/.test(h)));
    expect(stopping, '"Stopping" showed before the run ended').toBe(true);
    // Finished steps are kept: the reads version 2 reused from version 1, and the save it ran.
    for (const step of ['s2 gene_cases', 's3 cases_query', 's5 set_operation', 's7 de_preanalysis']) {
      expect(await s.stepWord(step)).toMatch(/^(Done|Reused · from version 1)/);
    }
    expect(await s.stepWord('s8 save_cohorts')).toMatch(/^Done/);
    expect(await s.stepWord('s9 de_run')).not.toMatch(/^Done/);
    await expect(s.answerCard).toContainText('Interpretation withheld: you stopped the run');
    const record = await s.checkRecord(runId, threadId, {status: 'Cancelled'});
    const deCalls = record.calls.filter(c => c.tool === 'de_run');
    expect(deCalls.length, 'the de_run call that was in flight is recorded').toBe(1);
    expect(deCalls[0].status).toBe('error');
    expect(deCalls[0].error).toMatch(/^CANCELLED/);
    const s9 = record.steps.find(step => step.id === 's9');
    expect(s9.status).toBe('Skipped');
    expect(s9.durationMs, 'the aborted step keeps how long it ran').toBeGreaterThan(0);
    s.pass(`Stop during de_run: "Stopping", then "Stopped by you"; s1–s7 kept (Reused from version 1), s8 Done, s9 "${(await s.stepWord('s9 de_run')).trim()}"; Interpretation withheld`);
  });

  await scenario('dont-save', "J3: Don't save, DE on unsaved groups, hand-off disabled with the reason", async s => {
    const before = await s.cohortList();
    await s.pickStarter('J3', 'open-ended', PROMPTS.j3Open);
    const card = s.run.locator('section').filter({has: s.page.getByRole('heading', {name: 'Save 2 cohorts', level: 3})});
    await expect(card).toBeVisible({timeout: RUN_TIMEOUT});
    await card.getByRole('button', {name: "Don't save", exact: true}).click();
    await s.waitStatus('Complete');
    expect(await s.stepWord('s8 save_cohorts')).toMatch(/^Skipped/);
    await expect(s.stepRow('s8 save_cohorts')).toContainText("Don't save");
    await expect(s.section('Limitations')).toContainText("You chose Don't save");
    const viz = s.run.locator('[data-testid^="orion-viz-card-"]').first();
    const open = viz.getByRole('button', {name: 'Open with this cohort'});
    await expect(open).toBeDisabled();
    await expect(viz.getByTestId('orion-viz-handoff-reason')).toContainText("Don't save");
    const after = await s.cohortList();
    expect(after.names).toEqual(before.names);
    expect(await s.saveLedgerKeys()).toEqual([]);
    const {runId, threadId} = await s.ids();
    const record = await s.checkRecord(runId, threadId, {status: 'Complete'});
    expect(record.runView.limitations.map(l => l.code)).toContain('WRITE_SKIPPED');
    s.pass("Don't save: s8 Skipped, DE ran, WRITE_SKIPPED limitation, Open with this cohort disabled with the reason, cohort list unchanged");
  });

  await scenario('e21', 'E21: Enter in the composer as the confirmation card appears saves nothing', async s => {
    await s.composer.fill('And in all other cases?');
    await s.pickStarter('J2', 'open-ended', PROMPTS.j2Open);
    await s.composer.focus();
    const card = s.run.locator('section').filter({has: s.page.getByRole('heading', {name: 'Save 1 cohort', level: 3})});
    await expect(card).toBeVisible({timeout: RUN_TIMEOUT});
    // The card appeared while the composer held text: focus stays there, Enter goes to the composer.
    await expect(s.composer).toBeFocused();
    await s.composer.press('Enter');
    const prompt = s.page.getByRole('heading', {name: 'Cancel the waiting run and send this, or keep it waiting?'});
    await expect(prompt).toBeVisible();
    await expect(s.page.getByRole('status').filter({hasText: 'Waiting for you'})).toHaveCount(1);
    await s.page.waitForTimeout(1000);
    await s.waitStatus('Waiting for you');
    expect(await s.saveLedgerKeys()).toEqual([]);
    const {names} = await s.cohortList();
    expect(names).not.toContain('Black or African American (ORION)');
    await s.shot('pause-prompt');
    await s.page.getByRole('button', {name: 'Keep it waiting'}).click();
    await expect(prompt).toBeHidden();
    await expect(s.composer).toHaveValue('And in all other cases?');
    await expect(card).toBeVisible();
    const {runId, threadId} = await s.ids();
    const view = await s.view(runId, threadId);
    expect(view.status).toBe('AwaitingConfirmation');
    s.pass('E21: focus stayed in the composer, the card was announced, Enter opened the pause prompt, nothing was saved, Keep it waiting kept the text and the card');
  });

  await scenario('restore', 'Reload at the save pause and during de_run: the thread restores and the run finishes', async s => {
    await s.pickStarter('J3', 'open-ended', PROMPTS.j3Open);
    await expect(s.run.locator('section').filter({has: s.page.getByRole('heading', {name: 'Save 2 cohorts', level: 3})})).toBeVisible({timeout: RUN_TIMEOUT});
    const {runId, threadId} = await s.ids();
    await s.page.reload({waitUntil: 'domcontentloaded'});
    await expect(s.page.getByRole('article', {name: PROMPTS.j3Open, exact: true})).toBeVisible({timeout: 60_000});
    const card = s.run.locator('section').filter({has: s.page.getByRole('heading', {name: 'Save 2 cohorts', level: 3})});
    await expect(card).toBeVisible({timeout: 30_000});
    await s.waitStatus('Waiting for you');
    expect((await s.ids()).runId).toBe(runId);
    s.pass('reload at the save pause: the thread, its plan and the confirmation card are restored');
    await s.shot('restored-pause');
    await card.getByRole('button', {name: 'Create', exact: true}).click();
    await expect(s.stepRow('s9 de_run').locator('span > span').first()).toHaveText(/^(Running|Done)/, {timeout: 30_000});
    await s.page.reload({waitUntil: 'domcontentloaded'});
    await expect(s.page.getByRole('article', {name: PROMPTS.j3Open, exact: true})).toBeVisible({timeout: 60_000});
    await s.waitStatus('Complete');
    await s.checkAnswer(['Top-ranked genes by adjusted p-value: ']);
    await s.checkViz({title: 'Differential expression: KRAS mutated vs. KRAS not detected', caption: DE_SORT_CAPTION});
    const record = await s.checkRecord(runId, threadId, {status: 'Complete'});
    await s.checkObservedMatchesRecord(record);
    s.pass('reload during de_run: the run finished in its record and the reopened thread shows it Complete');
  });

  await scenario('follow', 'A plan-card edit (Top N 3 → 5), E14 in the browser, a follow-up chip, and switching threads', async s => {
    // J2 at its save pause: Edit settings → Top N 5 → Apply makes plan version 2, which ranks five genes.
    await s.pickStarter('J2', 'open-ended', PROMPTS.j2Open);
    await s.waitStatus('Waiting for you');
    await s.planCard.getByRole('button', {name: 'Edit settings'}).click();
    const topN = s.planCard.getByRole('spinbutton', {name: /Top N/});
    await topN.fill('5');
    await s.planCard.getByRole('button', {name: 'Apply Top N'}).click();
    await expect(s.planCard.getByRole('heading', {name: 'Plan, version 2'})).toBeVisible({timeout: RUN_TIMEOUT});
    await expect(s.planCard).toContainText('2. Rank mutated genes: top 5');
    await s.waitStatus('Waiting for you');
    expect(await s.stepWord('s2 cases_query')).toMatch(/^Reused · from version 1/);
    s.pass('plan-card edit: Top N 3 → 5 made plan version 2; s2 reused from version 1; a new save pause');
    const card = s.run.locator('section').filter({has: s.page.getByRole('heading', {name: 'Save 1 cohort', level: 3})});
    await card.getByRole('checkbox').check();
    await card.getByRole('button', {name: 'Create', exact: true}).click();
    await s.waitStatus('Complete');
    const first = await s.ids();
    const j2 = await s.record(first.runId, first.threadId);
    const cohortSize = j2.facts.find(f => f.stepId === 's2' && f.unit === 'cases').value;
    expect((await s.view(first.runId, first.threadId)).facts.filter(f => /^f\.s4\.top\.\d+\.gene$/.test(f.id)).length).toBe(5);
    await expect(s.page.locator('input.mantine-Select-input').first()).toHaveValue('Black or African American (ORION)');
    s.pass(`Create with "make active": five genes ranked, the ${cohortSize}-case race cohort is now the active cohort`);

    // E14 in the browser: J1 in a new thread uses the active race cohort as its scope.
    await s.page.getByRole('button', {name: 'New thread'}).click();
    await expect(s.page.getByText(`Scope: Black or African American (ORION) · ${cohortSize.toLocaleString('en-US')} cases`).first()).toBeVisible();
    await s.pickStarter('J1', 'open-ended', PROMPTS.j1Open);
    await s.waitStatus('Complete');
    await expect(s.planCard.locator('p').filter({hasText: /^Scope: /}).first()).toContainText(`Scope: Black or African American (ORION) · ${cohortSize.toLocaleString('en-US')}`);
    const e14 = await s.ids();
    const j1 = await s.record(e14.runId, e14.threadId);
    // The scope's own case lookup comes first (s2 cases_query); the two groups are cohort_count facts.
    const groups = j1.facts.filter(f => f.tool === 'cohort_count' && f.unit === 'cases').map(f => f.value);
    expect(groups.length).toBe(2);
    expect(groups.reduce((a, b) => a + b, 0)).toBeLessThanOrEqual(cohortSize);
    expect(j1.runView.plan.scope).toMatchObject({source: 'active_cohort', name: 'Black or African American (ORION)', caseCount: cohortSize});
    await s.checkViz({title: 'Overall survival by CGS risk category'});
    s.pass(`E14: J1 scoped to the active race cohort (${cohortSize} cases): groups ${groups.join(' and ')}, the survival card live`);

    // A follow-up chip asks its question as a new run in the same thread.
    await s.answerCard.getByRole('button', {name: 'Survival by gender'}).click();
    await expect(s.page.locator('[role=log] > article')).toHaveCount(2);
    await s.waitStatus('Complete');
    s.pass('follow-up chip "Survival by gender" ran as a second run in the thread, to Complete');

    // Switching threads: the first thread comes back as it was.
    await s.page.getByRole('navigation', {name: 'Threads in this tab'}).getByRole('button', {name: /^Top 3 most frequently/}).click();
    await expect(s.page.getByRole('article', {name: PROMPTS.j2Open, exact: true})).toBeVisible();
    await s.waitStatus('Complete');
    await expect(s.planCard.getByRole('heading', {name: 'Plan, version 2'})).toBeVisible();
    s.pass('switching back to the first thread shows its run, plan version 2, Complete');
  });

  await scenario('edges', 'E7 declined substitution, E10 one clarifying question, E8 and E17 limits: their cards, words and records', async s => {
    // E7 / E24: a named tool that does not fit asks first; Cancel ends "Stopped by you" with no data call.
    await s.ask(PROMPTS.e7);
    await s.waitStatus('Needs your choice');
    const consent = s.run.locator('section').filter({has: s.page.getByRole('heading', {name: 'Use Correlation Plot instead of OncoMatrix?'})});
    await expect(consent.getByRole('heading', {level: 3})).toBeFocused();
    await expect(consent).toContainText('Nothing has run yet.');
    await s.shot('consent');
    await consent.getByRole('button', {name: 'Cancel', exact: true}).click();
    await s.waitStatus('Stopped by you');
    let {runId, threadId} = await s.ids();
    let record = await s.checkRecord(runId, threadId, {status: 'Cancelled', question: PROMPTS.e7});
    expect(record.calls.length, 'no data call').toBe(0);
    s.pass('E7: "Use Correlation Plot instead of OncoMatrix?" with focus on its heading; Cancel ended "Stopped by you" with no call');

    // E10: exactly one clarifying question with field options; an option plans and runs.
    await s.ask(PROMPTS.e10);
    await s.waitStatus('Question for you');
    const options = s.run.getByRole('list', {name: 'Options'}).getByRole('button');
    expect(await options.count()).toBeGreaterThan(1);
    await s.shot('clarify');
    await options.first().click();
    await s.waitStatus('Complete');
    ({runId, threadId} = await s.ids());
    record = await s.checkRecord(runId, threadId, {status: 'Complete', question: PROMPTS.e10});
    expect(record.actions.filter(a => a.action === 'clarify_answer').length).toBe(1);
    s.pass(`E10: one question with ${await options.count()} field options; picking one planned and ran to Complete`);

    // E8: a gene outside the panel: a limitation card with no numbers and no data call.
    await s.ask(PROMPTS.e8);
    await s.waitStatus('Stopped by a limitation');
    const limited = s.run.locator('section').filter({has: s.page.getByRole('heading', {name: 'A limitation stopped this run'})});
    await expect(limited).toContainText('outside the six-gene mutation panel');
    ({runId, threadId} = await s.ids());
    record = await s.checkRecord(runId, threadId, {status: 'Limited', question: PROMPTS.e8});
    expect(record.calls.filter(c => c.tool === 'de_run').length, 'no de_run call').toBe(0);
    expect(record.runView.facts.length, 'no numbers').toBe(0);
    s.pass('E8: "Stopped by a limitation": the gene is outside the six-gene panel, what would unblock it, no fact and no de_run call');

    // E17: treatment advice is out of scope: Not supported, no data call, no clinical content.
    await s.ask(PROMPTS.e17);
    await s.waitStatus('Not supported');
    ({runId, threadId} = await s.ids());
    record = await s.checkRecord(runId, threadId, {status: 'Unsupported', question: PROMPTS.e17});
    expect(record.calls.length, 'no data call').toBe(0);
    await expect(s.run).not.toContainText(/\d{2,}/);
    s.pass('E17: "Not supported", no call and no numbers');
    await s.shot('limits');
  });

  await scenario('e18', 'E18: Unsupported, names Set Operations, Open on this cohort', async s => {
    await s.ask(PROMPTS.venn);
    await s.waitStatus('Not supported');
    const card = s.run.locator('section').filter({has: s.page.getByRole('heading', {name: 'This question is not supported'})});
    await expect(card).toBeVisible();
    await expect(card).toContainText('Nearest app: Set Operations');
    await expect(card.locator('p').last()).toHaveText(SYNTHETIC_NOTICE);
    const {runId, threadId} = await s.ids();
    const record = await s.checkRecord(runId, threadId, {status: 'Unsupported', question: PROMPTS.venn});
    expect(record.calls.length, 'no data call').toBe(0);
    await s.shot('unsupported');
    await card.getByRole('button', {name: 'Open on this cohort'}).click();
    await s.page.waitForURL(/[?&]app=SetOperations\b/, {timeout: 30_000});
    s.pass(`Open on this cohort opened ${new URL(s.page.url()).search} in the same tab`);
    await s.page.goBack({waitUntil: 'domcontentloaded'});
    await expect(s.page.getByRole('article', {name: PROMPTS.venn, exact: true})).toBeVisible({timeout: 60_000});
    await s.waitStatus('Not supported');
    s.pass('browser Back returns to the thread, restored');
  });
} finally {
  await browser.close();
}

await writeFile(new URL('summary.json', shotsDir), `${JSON.stringify({origin, at: new Date().toISOString(), results}, null, 2)}\n`);
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length} of ${results.length} scenarios passed; ${results.reduce((n, r) => n + r.checks.length, 0)} checks. Screenshots and summary.json in ${shotsDir.pathname}`);
if (results.length === 0) {
  console.log('No scenario matched the filter.');
  process.exitCode = 1;
}
if (failed.length > 0) process.exitCode = 1;
