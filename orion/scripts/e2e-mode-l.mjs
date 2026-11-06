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
//   ORION_E2E_MODEL=bedrock node orion/scripts/e2e-mode-l.mjs     against the Bedrock agent
//
// Scenarios: j1-open, j1-named (J1 both phrasings, Evidence Inspector), j2 (confirmation card,
// Create, cohort list, hand-off), j3 (pause with eligible counts, two saves, DE card), edit-stop
// (an edit at the save pause, then Stop during de_run), dont-save, e21 (Enter as the card
// appears), restore (reload at the pause and during de_run), follow (plan-card edit, E14, a
// follow-up chip, thread switching), edges (E7, E10, E8, E17), e18 (Unsupported, Open on this
// cohort). orion/notes/mode-l-status.md lists what each checks.
//
// ORION_E2E_MODEL names the agent's planner and composer: `stub` (the default; stage 1) or
// `bedrock` (stage 2, `orion/scripts/orion-local.sh up --bedrock`). Both modes run every
// structural check: statuses, plan rows and signatures, cards, chips, the four answer parts in
// order, record contents, no write before Create, focus and announcements. Observed and
// Limitations are code-written, so their wording is checked in both. With `bedrock`:
// - the record must name the bedrock planner and composer, with every model call on
//   us.anthropic.claude-opus-5-5 (or its refusal fallback us.anthropic.claude-opus-5);
// - the stub composer's own wording (its follow-up chip labels, and the plans those chips make)
//   is replaced by checks that the model's text exists, passed the verifier, cites facts of the
//   run and equals the record, and that the answer's first follow-up chip plans with no
//   clarifying question (composer rule 9: a chip stands alone) and runs to Complete;
// - every Complete run also checks "Writing interpretation" came after Observed and before the
//   Interpretation, waits are longer, and summary.json gets the browser's timings;
// - four scenarios run that only real models make meaningful: writing ("Writing interpretation"
//   between Observed and the Interpretation), stop-writing (Stop while the composer writes),
//   stop-planning (Stop while the planner plans), recompose (a plan-card edit after Complete
//   composes a new answer). With the stubs they are listed as skipped.
// Model calls cost money: a full bedrock run takes about 6 minutes and makes about 80 Bedrock calls,
// about 40 recorded in run records and about 40 more in the Assistant's warm-up turns (two calls per
// fresh tab, new thread or reload; not recorded). orion/notes/bedrock-status.md has the results.
//
// Each scenario runs in a fresh browser context (its own sessionStorage, localStorage and
// IndexedDB), so the saved cohorts of one never leak into another. Screenshots go to
// orion/notes/screenshots/e2e/ (gitignored), with summary.json (the latest run) and
// summary-<model>-<UTC stamp>.json (every run, never deleted by a later one). Exit code 1 when any
// scenario fails. All data is synthetic.
import {chromium, expect} from '@playwright/test';
import {mkdir, readdir, rm, writeFile} from 'node:fs/promises';

const origin = process.env.SANDBOX_ORIGIN || 'http://127.0.0.1:3080';
const url = new URL(origin);
if (url.protocol !== 'http:' || !['localhost', '127.0.0.1'].includes(url.hostname) || url.origin !== origin) {
  throw new Error('The mode L end-to-end run requires the local synthetic stack');
}
const shotsDir = new URL('../notes/screenshots/e2e/', import.meta.url);
// ProteinPaint's message for a viewer request that a reload or Back aborts on the old page (seen in `restore`).
const TEARDOWN_ABORT = /^Failed to fetch$/;
const filters = process.argv.slice(2).map(word => word.toLowerCase());
const MODEL = (process.env.ORION_E2E_MODEL || 'stub').trim().toLowerCase();
if (!['stub', 'bedrock'].includes(MODEL)) throw new Error('ORION_E2E_MODEL takes stub or bedrock');
const BEDROCK = MODEL === 'bedrock';
// A4: the inference profiles, never the base model ids (they fail on demand).
const MODEL_IDS = {primary: 'us.anthropic.claude-opus-5-5', fallback: 'us.anthropic.claude-opus-5'};

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
// The stub planner's plan signatures (compiler.signature: scope, steps, args, assumptions, writes)
// on the local stack, from its run records. NFR-04: the Bedrock planner signs the same plans, so
// both modes check them. A compiler change that moves one fails the stub run too: update it here.
const SIGNATURES = {
  j1: 'c0fd027216d535bf4873ea2fdd93b10b91fc25574d9384ed49c641a468c9a65e',
  // J1 scoped to the saved "Black or African American (ORION)" cohort (E14).
  j1Race: '97e8fd3050c451c7c493ede2b2d53176c5738e2b206c8d951357bb113f85d828',
  // J2 in a fresh profile: the race cohort is not saved yet, so the plan writes.
  j2: '8f75d253a5422a3264bfc80c6148ee3e33a485dea15220a5a8397d81e8115468',
  j3: '3841cfffc2782fa1ff43cde9f5aed23523a9763de0ef0733665eadb5f73f9fcb',
};
const DE_SORT_CAPTION = 'Table sorted by fold change. Sort by adjusted p-value to match the answer.';
// Bedrock: a plan takes 3-5 s (30 s when a first attempt stalls), J3's composer 12-22 s and a
// re-compose about 18 s more.
const RUN_TIMEOUT = BEDROCK ? 180_000 : 90_000;
const MOUNT_TIMEOUT = 75_000;
const STOPPED_WITHHELD = 'Interpretation withheld: you stopped the run';
// Words a run rests on: an end status, or a card that waits for the researcher.
const SETTLED_WORDS = [
  'Complete', 'Waiting for you', 'Question for you', 'Needs your choice', 'Partial: a step failed', 'Stopped by a limitation',
  'Not supported', 'Interpretation withheld', 'Stopped by you', 'Failed', 'Interrupted',
];

// ------------------------------------------------------------------------------------------ page
// Recorded in the page on every DOM change outside the viewers (a MutationObserver, so a state
// that shows for a single frame, such as "Stopping", is not missed): the latest run's card
// headers, its step words, the composer's Send/Stop button, the answer card's part headings,
// whether it shows "Writing interpretation", its Observed text, and the polite live region.
const timelineRecorder = () => {
  window.__orionTimeline = [];
  const t0 = performance.now();
  window.__orionNow = () => Math.round(performance.now() - t0);
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
    const answer = article
      ? [...article.querySelectorAll('section')].find(s => s.querySelector(':scope > header > h3')?.textContent.trim() === 'Answer')
      : undefined;
    const parts = answer ? [...answer.querySelectorAll('h4')].map(h => h.textContent.trim()) : [];
    const writing = answer ? [...answer.querySelectorAll('p')].some(p => p.textContent.trim() === 'Writing interpretation') : false;
    const observedHeading = answer ? [...answer.querySelectorAll('h4')].find(h => h.textContent.trim() === 'Observed') : undefined;
    const observed = observedHeading ? observedHeading.parentElement.innerText.replace(/\s+/g, ' ').trim() : '';
    const live = document.querySelector('[role=status][aria-live=polite].sr-only')?.textContent ?? '';
    const question = article?.getAttribute('aria-label') ?? null;
    const snapshot = JSON.stringify([question, headers, steps, button, parts, writing, observed, live]);
    if (snapshot !== last) {
      last = snapshot;
      window.__orionTimeline.push({t: Math.round(performance.now() - t0), question, headers, steps, button, parts, writing, observed, live});
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
    this.timings = [];
    // The warm-up turns (BFF POST with action "warmup"): one per thread, sent when the Assistant opens.
    this.warmups = [];
    this.markAt = 0;
    this.markWarm = false;
  }

  async open() {
    this.context = await browser.newContext({viewport: {width: 1600, height: 1100}});
    await this.context.addInitScript(timelineRecorder);
    this.page = await this.context.newPage();
    this.page.on('pageerror', error => this.errors.push(`pageerror: ${error.message}`));
    // From the scenario's own reload or Back until the next document commits, the old document is being torn
    // down, and ProteinPaint logs the viewer request that teardown aborts: console.error with the bare text
    // "Failed to fetch" (TEARDOWN_ABORT). Only that message is kept apart, as unloadErrors; every other console
    // error in that window (an ORION stream reader or store failing on unmount, say) still fails the scenario.
    this.unloading = false;
    this.unloadErrors = [];
    this.page.on('framenavigated', frame => {
      if (frame === this.page.mainFrame()) this.unloading = false;
    });
    this.page.on('console', message => {
      if (message.type() !== 'error') return;
      const {url: source, lineNumber} = message.location();
      const where = source ? ` [${source.replace(origin, '').split('?')[0].slice(-120)}:${lineNumber}]` : '';
      const teardown = this.unloading && TEARDOWN_ABORT.test(message.text()) && !/orion/i.test(source ?? '');
      (teardown ? this.unloadErrors : this.errors).push(`console: ${message.text().slice(0, 300)}${where}`);
    });
    this.page.on('request', request => {
      if (request.method() === 'POST' && request.url().endsWith('/api/orion/agui') && (request.postData() ?? '').includes('"action":"warmup"')) {
        this.warmups.push({request, start: Date.now(), end: null});
      }
    });
    const ended = request => {
      const warmup = this.warmups.find(w => w.request === request);
      if (warmup && warmup.end === null) warmup.end = Date.now();
    };
    this.page.on('requestfinished', ended);
    this.page.on('requestfailed', ended);
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

  /** The scenario's own reload and Back: the old document's teardown messages are not page errors. */
  async reload() {
    this.unloading = true;
    await this.page.reload({waitUntil: 'domcontentloaded'});
  }
  async goBack() {
    this.unloading = true;
    await this.page.goBack({waitUntil: 'domcontentloaded'});
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
  /** Clears the timeline and notes when the next turn starts (page clock), and whether a warm-up was in flight. */
  async mark() {
    await this.clearTimeline();
    this.markAt = await this.page.evaluate(() => window.__orionNow());
    this.markWarm = this.warmups.some(w => w.end === null);
  }
  async ask(text) {
    await this.composer.fill(text);
    await this.mark();
    await this.composer.press('Enter');
    await expect(this.page.getByRole('article', {name: text, exact: true})).toBeVisible();
  }
  async pickStarter(journey, phrasing, text) {
    await this.mark();
    await this.page.getByRole('button', {name: new RegExp(`^${journey} · ${phrasing}`)}).click();
    await expect(this.page.getByRole('article', {name: text, exact: true})).toBeVisible();
  }
  /** Waits until the warm-up turn the Assistant sent on opening has ended, so the next ask is a warm one. */
  async waitWarm(timeout = 60_000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      if (this.warmups.length > 0 && this.warmups.every(w => w.end !== null)) return this.warmups.at(-1).end - this.warmups.at(-1).start;
      await this.page.waitForTimeout(250);
    }
    throw new Error(`the warm-up turn did not end within ${timeout / 1000} s`);
  }
  /**
   * The browser's own timings for the turn since mark(), from the timeline: the planner's answer
   * on screen (the first card other than the "Planning" placeholder: the plan, or a question or
   * consent card), and the "Writing interpretation" wait (until the Interpretation shows).
   * Data only, for summary.json; no check.
   */
  async noteTiming(label, {question = null, version = null} = {}) {
    // The run asked since mark() (its question), or the plan version an edit made.
    let timeline = (await this.timeline()).filter(x => x.t >= this.markAt && (question === null || x.question === question));
    if (version !== null) {
      // The earlier version's answer shows until the edit applies.
      timeline = timeline.slice(Math.max(0, timeline.findIndex(x => x.headers.some(h => h.startsWith(`Plan, version ${version} `)))));
    }
    const first = predicate => timeline.find(predicate) ?? null;
    const planned = first(x => x.headers.length > 0 && x.headers.some(h => h !== 'Plan Planning'));
    const writing = first(x => x.writing);
    const interpretation = first(x => x.parts.includes('Interpretation'));
    const entry = {
      label,
      warmupInFlight: this.markWarm,
      planVisibleMs: planned === null ? null : planned.t - this.markAt,
      firstCard: planned?.headers[0] ?? null,
      writingMs: writing === null || interpretation === null ? null : interpretation.t - writing.t,
      answerMs: interpretation === null ? null : interpretation.t - this.markAt,
    };
    this.timings.push(entry);
    return entry;
  }
  /** The run's screen words on its cards' headers. */
  async statusWords() {
    return (await this.run.locator('section > header > span > span').allTextContents()).map(t => t.trim());
  }
  async waitAnyStatus(words, timeout = RUN_TIMEOUT) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const found = (await this.statusWords()).find(word => words.includes(word));
      if (found) return found;
      await this.page.waitForTimeout(250);
    }
    throw new Error(`no status of ${words.join(' | ')} within ${timeout / 1000} s; the run shows ${(await this.statusWords()).join(', ')}`);
  }
  async waitStatusGone(word, timeout = 30_000) {
    await expect(this.run.locator('section > header > span > span').filter({hasText: new RegExp(`^${escape(word)}$`)})).toHaveCount(0, {timeout});
  }
  /**
   * Drives a model-planned run to where it rests: Create on a save pause (make-active unticked),
   * the first option of a clarifying question. Returns the screen words on the way.
   */
  async settle(timeout = RUN_TIMEOUT) {
    const path = [];
    for (let turn = 0; turn < 4; turn += 1) {
      const word = await this.waitAnyStatus(SETTLED_WORDS, timeout);
      path.push(word);
      if (word === 'Waiting for you') {
        const card = this.run.locator('section').filter({has: this.page.getByRole('heading', {name: /^Save \d+ cohorts?$/, level: 3})});
        await card.getByRole('button', {name: 'Create', exact: true}).click();
      } else if (word === 'Question for you') {
        await this.run.getByRole('list', {name: 'Options'}).getByRole('button').first().click();
      } else {
        return path;
      }
      await this.waitStatusGone(word);
    }
    throw new Error(`the run did not settle: ${path.join(' → ')}`);
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
  async checkPlan(rows, scope = 'All cases', signature = null) {
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
    if (signature) {
      // NFR-04: the plan this planner made is the stub planner's plan (the view route's signature).
      const {runId, threadId} = await this.ids();
      expect((await this.view(runId, threadId)).plan.signature, 'the plan signature equals the stub planner\'s').toBe(signature);
    }
    this.pass(`plan card: scope line "${(await scopeLine.innerText()).replace(/\s+/g, ' ')}" and rows ${rows.length}${signature ? `, signature ${signature.slice(0, 8)}…` : ''}`);
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
    const {planner, composer} = record.environment;
    expect(record.environment, `the agent runs the ${planner} planner and the ${composer} composer; ORION_E2E_MODEL is ${MODEL}`)
      .toMatchObject({mode: 'L', planner: MODEL, composer: MODEL, toolTransport: 'in_process'});
    const calls = BEDROCK ? `, ${checkModelCalls(record)}` : '';
    this.pass(`run record ${runId.slice(0, 8)}… via the record route: ${record.status}, ${record.calls.length} calls, ${record.facts.length} facts, environment mode L, ${MODEL} planner and composer${calls}`);
    return record;
  }

  // ------------------------------------------------------------------------------ model text (bedrock)
  /** The answer's follow-up chips: the record's own, label on the chip and question in its title, at most 3. */
  async checkFollowUps(record, {min = 0} = {}) {
    const expected = record.runView.followUps;
    expect(expected.length, 'follow-up chips').toBeGreaterThanOrEqual(min);
    expect(expected.length).toBeLessThanOrEqual(3);
    const chips = this.answerCard.getByRole('list', {name: 'Follow-up questions'}).getByRole('button');
    expect((await chips.allTextContents()).map(t => t.trim())).toEqual(expected.map(f => f.label));
    expect(await chips.evaluateAll(nodes => nodes.map(node => node.getAttribute('title')))).toEqual(expected.map(f => f.question));
    for (const followUp of expected) {
      expect(followUp.label.length).toBeLessThanOrEqual(60);
      expect(followUp.question.length).toBeLessThanOrEqual(200);
    }
    return expected;
  }

  /**
   * The model's Interpretation and Hypotheses and next steps: each part has a sentence the
   * verifier kept; every kept sentence cites facts of this run; each cited number or name shows
   * with its chip; a digit outside a chip only in a term the contract allows (allowedDigitTokens);
   * and the screen equals the record. The chips and app links are the record's own.
   */
  async checkModelText(record) {
    const view = record.runView;
    expect(view.status).toBe('Complete');
    const facts = new Set(view.facts.map(f => f.id));
    const allowed = allowedDigitTokens(record);
    const summary = [];
    for (const [name, sentences] of [['Interpretation', view.interpretation], ['Hypotheses and next steps', view.hypotheses]]) {
      expect(Array.isArray(sentences), `${name} is in the record`).toBe(true);
      const kept = sentences.filter(sentence => !sentence.dropped);
      expect(kept.length, `${name}: a sentence that passed the verifier`).toBeGreaterThan(0);
      let cited = 0;
      for (const sentence of kept) {
        expect(sentence.cites.length, `${name}: the sentence cites a fact`).toBeGreaterThan(0);
        for (const id of sentence.cites) expect(facts.has(id), `${name} cites ${id}, a fact of this run`).toBe(true);
        for (const segment of sentence.segments) {
          if (segment.factId) {
            expect(facts.has(segment.factId), `${name} shows ${segment.factId}, a fact of this run`).toBe(true);
            cited += 1;
            continue;
          }
          const unexplained = digitTokens(segment.text).filter(token => !explained(token, allowed));
          expect(unexplained, `${name}: digits outside a fact chip`).toEqual([]);
        }
      }
      const screen = await this.section(name).locator('p').evaluateAll(nodes =>
        nodes.map(node => {
          if (node.classList.contains('italic')) return null; // "A sentence was removed by the verifier …"
          const clone = node.cloneNode(true);
          clone.querySelectorAll('button').forEach(button => button.remove());
          return clone.textContent.replace(/\s+/g, ' ').trim();
        }),
      );
      const stored = sentences.map(sentence =>
        sentence.dropped ? null : sentence.segments.map(segment => segment.text).join('').replace(/\s+/g, ' ').trim(),
      );
      expect(screen, `${name} on screen equals the record`).toEqual(stored);
      const chips = await this.section(name).locator('p').getByRole('button', {name: /^Row \d+ · s\d+ \w+$/}).count();
      expect(chips, `${name}: one chip per cited number or name`).toBe(cited);
      summary.push(`${name} ${kept.length} sentences${kept.length < sentences.length ? ` (${sentences.length - kept.length} dropped)` : ''} with ${chips} chips`);
    }
    await this.checkFollowUps(record);
    const links = this.answerCard.getByRole('list', {name: 'Other apps'}).getByRole('button');
    expect(await links.count(), 'one button per app link').toBe(view.openLinks.length);
    this.pass(`model text: ${summary.join('; ')}; every sentence cites facts of the run and equals the record; ${view.followUps.length} follow-up chips, ${view.openLinks.length} app links`);
  }

  /**
   * C-22 / F2 "Answering": since mark(), "Writing interpretation" showed with Observed (and
   * Limitations) and no Interpretation yet, Stop in place of Send, and the live region said so;
   * the Interpretation came after, and Observed did not change.
   */
  async checkWritingOrder({version = null} = {}) {
    let timeline = (await this.timeline()).filter(x => x.t >= this.markAt);
    if (version !== null) {
      // After an edit: from the first snapshot of that plan version on (the earlier answer shows until then).
      timeline = timeline.slice(Math.max(0, timeline.findIndex(x => x.headers.some(h => h.startsWith(`Plan, version ${version} `)))));
    }
    const at = timeline.findIndex(x => x.writing);
    expect(at, '"Writing interpretation" showed in the answer card').toBeGreaterThanOrEqual(0);
    const writing = timeline[at];
    expect(writing.headers.some(h => / Writing interpretation$/.test(h)), 'the plan card header read "Writing interpretation"').toBe(true);
    expect(writing.parts.filter(p => p !== 'Limitations'), 'while writing: Observed only, no Interpretation yet').toEqual(['Observed']);
    expect(writing.observed.length).toBeGreaterThan('Observed'.length);
    expect(writing.button, 'Stop in place of Send while writing').toBe('Stop');
    expect(timeline.slice(0, at).some(x => x.parts.includes('Interpretation')), 'no Interpretation before "Writing interpretation"').toBe(false);
    const done = timeline.findIndex(x => x.parts.includes('Interpretation'));
    expect(done, 'the Interpretation came after "Writing interpretation"').toBeGreaterThan(at);
    expect(timeline[done].parts).toEqual(ANSWER_PARTS);
    expect(timeline.some(x => /(^| )Writing interpretation\.( |$)/.test(x.live)), 'the live region said "Writing interpretation."').toBe(true);
    const observedNow = await this.section('Observed').evaluate(node => node.innerText.replace(/\s+/g, ' ').trim());
    expect(writing.observed, 'Observed did not change once the Interpretation came').toBe(observedNow);
    this.pass(`"Writing interpretation" at ${writing.t - this.markAt} ms with ${writing.parts.join(' and ')} on screen, then all four parts ${timeline[done].t - writing.t} ms later; Observed unchanged; announced`);
  }
}

// Contract "Numbers on screen", as the evals' cited-numbers gate reads it (orion_evals/checks.py
// unexplained_digits): outside a fact segment, a digit only in the contract's own terms, the
// question's words, scope and cohort names, and the plan's settings.
const CONTRACT_DIGIT_TOKENS = ['Gen3', 'log2FC', 'log2', 'six-gene', 'TP53', 'DIS3'];
const VIEWER_FOOTNOTE = 'p = 0';
const digitTokens = text =>
  [...text.replaceAll(VIEWER_FOOTNOTE, '').matchAll(/[A-Za-z0-9][A-Za-z0-9_.,+-]*/g)].map(m => m[0].replace(/^[.,]+|[.,]+$/g, ''));
function allowedDigitTokens(record) {
  const view = record.runView;
  const plan = view.plan ?? {};
  const texts = [record.question, plan.scope?.name ?? ''];
  for (const cohort of view.cohorts ?? []) texts.push(cohort.name ?? '', cohort.savedName ?? '');
  for (const editable of plan.editable ?? []) {
    const value = editable.value;
    texts.push(...(value && typeof value === 'object' ? Object.values(value).map(String) : [String(value)]));
  }
  return new Set([...CONTRACT_DIGIT_TOKENS, ...texts.flatMap(digitTokens)]);
}
function explained(token, allowed) {
  if (!/\d/.test(token) || allowed.has(token)) return true;
  const parts = token.split('-').filter(Boolean);
  return token.replace(/^-+|-+$/g, '').includes('-') && parts.every(part => !/\d/.test(part) || allowed.has(part));
}

/** Every model call on the A4 inference profiles; the run's last planner call ok; a Complete run's composer call ok. */
function checkModelCalls(record) {
  const calls = record.modelCalls;
  for (const call of calls) {
    expect([MODEL_IDS.primary, MODEL_IDS.fallback], `${call.callId} model`).toContain(call.model);
    expect(call.fallback, `${call.callId} fallback`).toBe(call.model === MODEL_IDS.fallback);
  }
  const planner = calls.filter(c => c.role === 'planner');
  expect(planner.length, 'a planner call').toBeGreaterThan(0);
  expect(planner.at(-1).outcome, 'the last planner call').toBe('ok');
  const composer = calls.filter(c => c.role === 'composer');
  if (record.status === 'Complete') {
    const version = record.planVersions.at(-1).version;
    const latest = composer.filter(c => c.callId.includes(`.v${version}.composer.`));
    expect(latest.length, `a composer call for plan version ${version}`).toBeGreaterThan(0);
    expect(latest.at(-1).outcome, 'the last composer call').toBe('ok');
  }
  const models = [...new Set(calls.map(c => c.model))].join(', ');
  return `${calls.length} model calls (${planner.length} planner, ${composer.length} composer: ${calls.map(c => c.outcome).join('/')}) on ${models}`;
}

const escape = text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

const skipped = [];

async function scenario(id, title, body, {bedrockOnly = false} = {}) {
  // bedrockOnly: needs real models (time to see "Planning" or "Writing interpretation", model text to re-compose).
  if (filters.length > 0 && !filters.some(word => id.includes(word))) return;
  if (bedrockOnly && !BEDROCK) {
    skipped.push(id);
    console.log(`- ${id}: ${title} ... skipped (needs the real models: ORION_E2E_MODEL=bedrock)`);
    return;
  }
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
    results.push({id, title, ok: true, ms: Date.now() - started, checks: s.checks, shots: s.shots, timings: s.timings, unloadErrors: s.unloadErrors});
    console.log(`PASS (${s.checks.length} checks, ${((Date.now() - started) / 1000).toFixed(1)} s)`);
  } catch (error) {
    if (s.page && !s.page.isClosed()) await s.shot('failure').catch(() => {});
    const timeline = s.page && !s.page.isClosed() ? await s.timeline().catch(() => []) : [];
    // Outside the Playwright runner an expect's own message is not in error.message: name the line.
    const at = (error.stack ?? '').split('\n').find(line => line.includes('e2e-mode-l.mjs'))?.trim().replace(/^at /, '') ?? '';
    results.push({id, title, ok: false, ms: Date.now() - started, checks: s.checks, shots: s.shots, timings: s.timings, at, unloadErrors: s.unloadErrors,
      error: error.message.split('\n').slice(0, 12).join('\n'), pageErrors: s.errors.slice(0, 10), timeline: timeline.slice(-12)});
    console.log(`FAIL after ${s.checks.length} checks${at ? ` (${at.replace(/^.*\//, '')})` : ''}\n${error.message.split('\n').slice(0, 12).map(l => `    ${l}`).join('\n')}`);
  } finally {
    await s.context?.close().catch(() => {});
  }
}

// ------------------------------------------------------------------------------------- journeys
async function j1(s, phrasing, question) {
  await expect(s.page.getByText('Try a reference question')).toBeVisible();
  await expect(s.page.getByText(/^Scope: All cases · /).first()).toBeVisible();
  await s.pickStarter('J1', phrasing, question);
  await s.checkPlan(J1_ROWS, 'All cases', SIGNATURES.j1);
  await s.shot('plan');
  await s.waitStatus('Complete');
  await s.noteTiming(`J1 ${phrasing}`, {question});
  await s.checkLiveSteps();
  if (BEDROCK) await s.checkWritingOrder();
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
  if (BEDROCK) await s.checkModelText(record);
  await s.checkVizReport(runId, threadId);
  return {card, runId, threadId, record};
}

/**
 * Bedrock: the answer's first follow-up chip (the composer's own) asks its question as the next
 * run of the thread, which the planner plans and which settles Complete (Create on a save pause,
 * the first option of a question), with all four parts and model text that passes the checks.
 */
async function modelFollowUp(s, record, runs) {
  const [first] = await s.checkFollowUps(record, {min: 1});
  await s.mark();
  await s.answerCard.getByRole('list', {name: 'Follow-up questions'}).getByRole('button', {name: first.label, exact: true}).click();
  await expect(s.page.locator('[role=log] > article')).toHaveCount(runs);
  await expect(s.page.getByRole('article', {name: first.question, exact: true})).toBeVisible();
  const path = await s.settle();
  expect(path.at(-1), `the follow-up "${first.question}" (${path.join(' → ')})`).toBe('Complete');
  // Composer rule 9: a chip stands alone, so the planner needs no clarifying question for it (FR-19).
  expect(path, `the chip "${first.question}" needed a clarifying question`).not.toContain('Question for you');
  await s.noteTiming(`follow-up "${first.label}"`, {question: first.question});
  await expect(s.planCard.getByRole('heading', {name: /^Plan, version \d+$/})).toBeVisible();
  const {runId, threadId} = await s.ids();
  const next = await s.checkRecord(runId, threadId, {status: 'Complete', question: first.question});
  await s.checkAnswer([]);
  await s.checkModelText(next);
  s.pass(`follow-up chip "${first.label}" asked "${first.question}" as run ${runs} of the thread: ${path.join(' → ')}`);
  return next;
}

await mkdir(shotsDir, {recursive: true});
for (const name of ['summary.json']) await rm(new URL(name, shotsDir), {force: true});
browser = await chromium.launch({headless: true});
try {
  await scenario('j1-open', 'J1, open-ended: plan, live steps, survival card, answer, record, Evidence Inspector', async s => {
    const {runId, threadId, record} = await j1(s, 'open-ended', PROMPTS.j1Open);
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
    if (BEDROCK) {
      await modelFollowUp(s, record, 2);
      return;
    }
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
    await s.checkPlan(J2_ROWS, 'All cases', SIGNATURES.j2);
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
    await s.noteTiming('J2 open-ended', {question: PROMPTS.j2Open});
    await s.checkLiveSteps();
    if (BEDROCK) await s.checkWritingOrder();
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
    if (BEDROCK) await s.checkModelText(record);
    await s.checkVizReport(runId, threadId);
    // Hand-off: the card's cohort is not active, so the button says it switches; same tab; Back restores.
    const viz = s.run.locator('[data-testid^="orion-viz-card-"]').first();
    await viz.getByRole('button', {name: 'Make active and open'}).click();
    await s.page.waitForURL(/[?&]app=OncoMatrix\b/, {timeout: 30_000});
    await expect(s.page.locator('input.mantine-Select-input').first()).toHaveValue('Black or African American (ORION)', {timeout: 30_000});
    await expect(s.page.getByText('Active cohort: Black or African American (ORION).')).toBeVisible();
    await s.shot('handoff');
    s.pass('Make active and open: OncoMatrix opened in the same tab with the saved cohort active, with an undo notice');
    await s.goBack();
    await expect(s.page.getByRole('article', {name: PROMPTS.j2Open, exact: true})).toBeVisible({timeout: 60_000});
    await s.waitStatus('Complete');
    await expect(s.answerCard.locator('h4')).toHaveText(ANSWER_PARTS);
    s.pass('browser Back returns to the thread, restored Complete with its answer');
    if (BEDROCK) {
      await modelFollowUp(s, record, 2);
    } else {
      // C-06: J2's own chip, "Survival by <top gene> mutation", compares that gene's mutated and other tested cases.
      await s.answerCard.getByRole('button', {name: /^Survival by [A-Z0-9]+ mutation$/}).click();
      await expect(s.page.locator('[role=log] > article')).toHaveCount(2);
      await s.waitStatus('Complete');
      await expect(s.planCard).toContainText('survival');
      s.pass('follow-up chip "Survival by <gene> mutation" ran as a second run in the thread, to Complete');
    }
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
    await s.checkPlan(J3_ROWS, 'All cases', SIGNATURES.j3);
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
    await s.noteTiming('J3 open-ended', {question: PROMPTS.j3Open});
    await s.checkLiveSteps();
    if (BEDROCK) await s.checkWritingOrder();
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
    if (BEDROCK) await s.checkModelText(record);
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
    if (BEDROCK) await s.checkModelText(record);
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
    await s.reload();
    await expect(s.page.getByRole('article', {name: PROMPTS.j3Open, exact: true})).toBeVisible({timeout: 60_000});
    const card = s.run.locator('section').filter({has: s.page.getByRole('heading', {name: 'Save 2 cohorts', level: 3})});
    await expect(card).toBeVisible({timeout: 30_000});
    await s.waitStatus('Waiting for you');
    expect((await s.ids()).runId).toBe(runId);
    s.pass('reload at the save pause: the thread, its plan and the confirmation card are restored');
    await s.shot('restored-pause');
    await card.getByRole('button', {name: 'Create', exact: true}).click();
    await expect(s.stepRow('s9 de_run').locator('span > span').first()).toHaveText(/^(Running|Done)/, {timeout: 30_000});
    await s.reload();
    await expect(s.page.getByRole('article', {name: PROMPTS.j3Open, exact: true})).toBeVisible({timeout: 60_000});
    await s.waitStatus('Complete');
    await s.checkAnswer(['Top-ranked genes by adjusted p-value: ']);
    await s.checkViz({title: 'Differential expression: KRAS mutated vs. KRAS not detected', caption: DE_SORT_CAPTION});
    const record = await s.checkRecord(runId, threadId, {status: 'Complete'});
    await s.checkObservedMatchesRecord(record);
    if (BEDROCK) await s.checkModelText(record);
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
    if (BEDROCK) await s.checkModelText(j2);
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
    expect(j1.runView.plan.signature, 'the plan signature equals the stub planner\'s').toBe(SIGNATURES.j1Race);
    await s.checkViz({title: 'Overall survival by CGS risk category'});
    s.pass(`E14: J1 scoped to the active race cohort (${cohortSize} cases): groups ${groups.join(' and ')}, signature ${SIGNATURES.j1Race.slice(0, 8)}…, the survival card live`);

    // A follow-up chip asks its question as a new run in the same thread.
    if (BEDROCK) {
      await s.noteTiming('J1 in the race cohort', {question: PROMPTS.j1Open});
      await s.checkRecord(e14.runId, e14.threadId, {status: 'Complete', question: PROMPTS.j1Open});
      await s.checkModelText(j1);
      await modelFollowUp(s, j1, 2);
    } else {
      await s.answerCard.getByRole('button', {name: 'Survival by gender'}).click();
      await expect(s.page.locator('[role=log] > article')).toHaveCount(2);
      await s.waitStatus('Complete');
      s.pass('follow-up chip "Survival by gender" ran as a second run in the thread, to Complete');
    }

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
    const offered = await options.count();
    await s.shot('clarify');
    await options.first().click();
    if (BEDROCK) {
      // The model plans the picked field; a save pause on the way is answered with Create.
      await s.waitStatusGone('Question for you');
      expect(await s.settle()).toContain('Complete');
    } else {
      await s.waitStatus('Complete');
    }
    ({runId, threadId} = await s.ids());
    record = await s.checkRecord(runId, threadId, {status: 'Complete', question: PROMPTS.e10});
    expect(record.actions.filter(a => a.action === 'clarify_answer').length).toBe(1);
    if (BEDROCK) await s.checkModelText(record);
    s.pass(`E10: one question with ${offered} field options; picking one planned and ran to Complete`);

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
    await s.goBack();
    await expect(s.page.getByRole('article', {name: PROMPTS.venn, exact: true})).toBeVisible({timeout: 60_000});
    await s.waitStatus('Not supported');
    s.pass('browser Back returns to the thread, restored');
  });

  // ------------------------------------------------------------------ real composer only (bedrock)
  const observedText = s => s.section('Observed').evaluate(node => node.innerText.replace(/\s+/g, ' ').trim());
  const topGenes = view => view.facts.filter(f => /^f\.s4\.top\.\d+\.gene$/.test(f.id)).length;

  await scenario('writing', '"Writing interpretation": the plan, then Observed and Limitations, then the Interpretation (J1 typed, after the warm-up)', async s => {
    const warmMs = await s.waitWarm();
    await s.composer.click();
    await s.ask(PROMPTS.j1Open);
    await s.checkPlan(J1_ROWS, 'All cases', SIGNATURES.j1);
    // C-22: while the composer writes, the answer card shows Observed and Limitations, the steps are done, Stop is live.
    await s.waitStatus('Writing interpretation');
    await expect(s.answerCard.locator('h4')).toHaveText(['Observed', 'Limitations']);
    await expect(s.answerCard.getByText('Writing interpretation', {exact: true})).toBeVisible();
    await expect(s.section('Observed')).toContainText('Log-rank p = ');
    for (const step of ['s2 cohort_count', 's3 cohort_count', 's4 survival_compare', 's5 derive_km_median']) {
      expect(await s.stepWord(step)).toMatch(/^Done/);
    }
    await expect(s.page.getByRole('button', {name: 'Stop', exact: true})).toBeEnabled();
    await expect(s.composer, 'focus stays in the composer while the answer is written').toBeFocused();
    await s.shot('writing');
    s.pass('while "Writing interpretation": Observed and Limitations on screen, no Interpretation yet, steps s2–s5 Done, Stop live, focus in the composer');
    await s.waitStatus('Complete');
    const timing = await s.noteTiming('J1 typed, warm', {question: PROMPTS.j1Open});
    const timeline = (await s.timeline()).filter(x => x.t >= s.markAt);
    const planning = timeline.findIndex(x => x.headers.includes('Plan Planning'));
    const planned = timeline.findIndex(x => x.headers.some(h => /^Plan, version 1 /.test(h)));
    expect(planning, 'the "Planning" placeholder card showed').toBeGreaterThanOrEqual(0);
    expect(planning, 'the placeholder came before the plan').toBeLessThan(planned);
    expect(timeline[planning].button, 'Stop while planning').toBe('Stop');
    await s.checkWritingOrder();
    expect(timeline.some(x => x.live.endsWith('Answer ready: Complete.')), 'the live region said "Answer ready: Complete."').toBe(true);
    await expect(s.composer, 'focus still in the composer').toBeFocused();
    const {runId, threadId} = await s.ids();
    const record = await s.checkRecord(runId, threadId, {status: 'Complete', question: PROMPTS.j1Open});
    await s.checkObservedMatchesRecord(record);
    await s.checkModelText(record);
    s.pass(`timings: the warm-up turn took ${warmMs} ms; then "Planning" for ${timing.planVisibleMs} ms until the plan, and "Writing interpretation" for ${timing.writingMs} ms`);
  }, {bedrockOnly: true});

  await scenario('stop-writing', 'Stop during "Writing interpretation": "Stopping", then Stopped by you, Observed kept, Interpretation withheld', async s => {
    await s.pickStarter('J1', 'open-ended', PROMPTS.j1Open);
    await s.checkPlan(J1_ROWS, 'All cases', SIGNATURES.j1);
    await s.waitStatus('Writing interpretation');
    await expect(s.answerCard.locator('h4')).toHaveText(['Observed', 'Limitations']);
    const observed = await observedText(s);
    await s.shot('writing');
    await s.page.getByRole('button', {name: 'Stop', exact: true}).click();
    await s.waitStatus('Stopped by you', 60_000);
    const timeline = (await s.timeline()).filter(x => x.t >= s.markAt);
    expect(timeline.some(x => x.button === 'Stopping' || x.headers.some(h => / Stopping$/.test(h))), '"Stopping" showed before the run ended').toBe(true);
    expect(timeline.some(x => x.parts.includes('Interpretation')), 'no Interpretation was ever shown').toBe(false);
    await expect(s.answerCard.locator('h4')).toHaveText(['Observed', 'Limitations']);
    await expect(s.answerCard).toContainText(STOPPED_WITHHELD);
    expect(await observedText(s), 'Observed kept as it was').toBe(observed);
    await expect(s.answerCard.getByRole('list', {name: 'Follow-up questions'})).toHaveCount(0);
    for (const step of ['s2 cohort_count', 's3 cohort_count', 's4 survival_compare', 's5 derive_km_median']) {
      expect(await s.stepWord(step)).toMatch(/^Done/);
    }
    expect(timeline.some(x => x.live.endsWith('Answer ready: Stopped by you.')), 'the live region said "Answer ready: Stopped by you."').toBe(true);
    await expect(s.page.getByRole('button', {name: 'Send', exact: true})).toBeVisible();
    s.pass('Stop while writing: "Stopping", then "Stopped by you"; Observed and Limitations kept unchanged, "Interpretation withheld: you stopped the run", no chips, s2–s5 Done, announced, Send is back');
    const {runId, threadId} = await s.ids();
    const record = await s.checkRecord(runId, threadId, {status: 'Cancelled', question: PROMPTS.j1Open});
    expect(record.runView.interpretation).toBeNull();
    expect(record.runView.hypotheses).toBeNull();
    expect(record.runView.followUps).toEqual([]);
    expect(record.runView.withheldReason).toBe('you stopped the run');
    expect(record.steps.filter(step => step.status !== 'Done').map(step => step.id), 'every step finished').toEqual([]);
    const composer = record.modelCalls.filter(c => c.role === 'composer');
    expect(composer.length, 'the aborted composer call is recorded').toBeGreaterThan(0);
    expect(composer.at(-1).outcome).toBe('cancelled');
    await s.checkObservedMatchesRecord(record);
    s.pass(`record: Cancelled, no Interpretation or follow-ups, withheld "you stopped the run"; the composer call ${composer.at(-1).callId.split('.').slice(1).join('.')} recorded "cancelled" after ${composer.at(-1).elapsedMs} ms`);
  }, {bedrockOnly: true});

  await scenario('stop-planning', 'Stop during "Planning": "Stopping", then Stopped by you, no plan and no tool call (J1 typed, after the warm-up)', async s => {
    await s.waitWarm();
    await s.ask(PROMPTS.j1Open);
    await s.waitStatus('Planning', 30_000);
    // After the warm-up the ask goes out at once; a second later its planner call (about 3 s) is in flight,
    // so Stop sends a cancel turn rather than withdrawing a question the agent never saw.
    await s.page.waitForTimeout(1000);
    await s.page.getByRole('button', {name: 'Stop', exact: true}).click();
    await s.waitStatus('Stopped by you', 60_000);
    const timeline = (await s.timeline()).filter(x => x.t >= s.markAt);
    expect(timeline.some(x => x.button === 'Stopping' || x.headers.some(h => / Stopping$/.test(h))), '"Stopping" showed before the run ended').toBe(true);
    expect(timeline.some(x => x.headers.some(h => /^Plan, version/.test(h))), 'no plan was ever shown').toBe(false);
    await expect(s.planCard).toContainText('No plan was made before the run ended.');
    await expect(s.answerCard).toHaveCount(0);
    s.pass('Stop while planning: "Stopping", then "Stopped by you"; no plan card, no answer');
    const {runId, threadId} = await s.ids();
    const record = await s.record(runId, threadId);
    expect(record.status).toBe('Cancelled');
    expect(record.environment).toMatchObject({mode: 'L', planner: MODEL, composer: MODEL});
    expect(record.calls.length, 'no tool call').toBe(0);
    expect(record.planVersions.length, 'no plan').toBe(0);
    const planner = record.modelCalls.filter(c => c.role === 'planner');
    expect(planner.length, 'the aborted planner call is recorded').toBeGreaterThan(0);
    expect(planner.at(-1).outcome).toBe('cancelled');
    for (const call of record.modelCalls) expect([MODEL_IDS.primary, MODEL_IDS.fallback]).toContain(call.model);
    s.pass(`record: Cancelled with no plan and no tool call; the planner call ${planner.at(-1).callId.split('.').slice(1).join('.')} recorded "cancelled" after ${planner.at(-1).elapsedMs} ms`);
  }, {bedrockOnly: true});

  await scenario('recompose', 'A plan-card edit after Complete re-composes: J2 Top N 3 → 5 makes version 2, and a new composer call writes its answer', async s => {
    await s.pickStarter('J2', 'open-ended', PROMPTS.j2Open);
    await s.checkPlan(J2_ROWS, 'All cases', SIGNATURES.j2);
    const card = s.run.locator('section').filter({has: s.page.getByRole('heading', {name: 'Save 1 cohort', exact: true, level: 3})});
    await expect(card).toBeVisible({timeout: RUN_TIMEOUT});
    await card.getByRole('button', {name: 'Create', exact: true}).click();
    await s.waitStatus('Complete');
    const {runId, threadId} = await s.ids();
    const v1 = await s.checkRecord(runId, threadId, {status: 'Complete', question: PROMPTS.j2Open});
    expect(topGenes(v1.runView)).toBe(3);
    await s.checkModelText(v1);
    const plannerCalls = v1.modelCalls.filter(c => c.role === 'planner').length;

    // The edit at rest: Edit settings, Top N 5, Apply. No planner call; version 2 runs and its own composer call answers.
    await s.mark();
    await s.planCard.getByRole('button', {name: 'Edit settings'}).click();
    await s.planCard.getByRole('spinbutton', {name: /Top N/}).fill('5');
    await s.planCard.getByRole('button', {name: 'Apply Top N'}).click();
    await expect(s.planCard.getByRole('heading', {name: 'Plan, version 2'})).toBeVisible({timeout: RUN_TIMEOUT});
    await expect(s.planCard).toContainText('2. Rank mutated genes: top 5');
    await expect(s.planCard).toContainText('Replaces version 1');
    await s.waitStatus('Complete');
    await s.noteTiming('J2 edit Top N 3 → 5', {version: 2});
    await s.checkWritingOrder({version: 2});
    expect(await s.stepWord('s2 cases_query')).toMatch(/^Reused · from version 1/);
    expect(await s.stepWord('s5 save_cohorts')).toMatch(/^(Reused|Done)/);
    await expect(s.run.locator('section').filter({has: s.page.getByRole('heading', {name: 'Save 1 cohort', level: 3})})).toHaveCount(0);
    await expect(s.answerCard.getByRole('note')).toContainText('This answer replaces the version 1 answer, which an edit superseded.');
    s.pass(`edit after Complete: plan version 2 (top 5), s2 reused, s5 "${(await s.stepWord('s5 save_cohorts')).trim()}" with no second save card; the answer says it replaces the version 1 answer`);
    const v2 = await s.checkRecord(runId, threadId, {status: 'Complete', question: PROMPTS.j2Open});
    expect(v2.planVersions.map(p => p.version)).toEqual([1, 2]);
    expect(v2.planHistory.at(-1)).toMatchObject({version: 2, cause: 'edit_plan'});
    expect(v2.modelCalls.filter(c => c.role === 'planner').length, 'the edit made no planner call').toBe(plannerCalls);
    const composed = v2.modelCalls.filter(c => c.role === 'composer' && c.outcome === 'ok').map(c => Number(/\.v(\d+)\.composer\./.exec(c.callId)[1]));
    expect([...new Set(composed)].sort(), 'a composer call answered each version').toEqual([1, 2]);
    expect(v2.confirmations.length, 'one save, made for version 1').toBe(1);
    expect(topGenes(v2.runView)).toBe(5);
    expect(v2.runView.superseded).toEqual([expect.objectContaining({planVersion: 1, hadAnswer: true})]);
    await s.checkObservedMatchesRecord(v2);
    await expect(s.section('Observed')).toContainText('The most frequently mutated genes are ');
    await s.checkModelText(v2);
    s.pass(`record: versions 1 and 2, ${plannerCalls} planner call, composer calls for both versions, five genes ranked, version 1's answer kept as superseded`);
  }, {bedrockOnly: true});
} finally {
  await browser.close();
}

// summary.json is the latest run in either mode; summary-<model>-<UTC stamp>.json keeps every run, so a stub run
// never overwrites the evidence of a Bedrock one (and the reverse).
const finishedAt = new Date().toISOString();
// skipped: the scenarios this mode did not run (the Bedrock-only ones in stub mode), so the file alone backs the count.
const summaryText = `${JSON.stringify({origin, model: MODEL, at: finishedAt, results, skipped: skipped.map(id => ({id, reason: 'needs ORION_E2E_MODEL=bedrock'}))}, null, 2)}\n`;
const keptSummary = `summary-${MODEL}-${finishedAt.replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z')}.json`;
await writeFile(new URL('summary.json', shotsDir), summaryText);
await writeFile(new URL(keptSummary, shotsDir), summaryText);
const failed = results.filter(r => !r.ok);
const skip = skipped.length > 0 ? `; ${skipped.length} skipped (${skipped.join(', ')}: they need ORION_E2E_MODEL=bedrock)` : '';
console.log(`\n${results.length - failed.length} of ${results.length} scenarios passed; ${results.reduce((n, r) => n + r.checks.length, 0)} checks${skip}. Model mode: ${MODEL}. Screenshots, summary.json and ${keptSummary} in ${shotsDir.pathname}`);
if (BEDROCK) {
  const seconds = ms => (ms === null ? '–' : `${(ms / 1000).toFixed(1)} s`);
  console.log('Browser timings (plan: the ask to the planner\'s card; writing: "Writing interpretation" to the Interpretation):');
  for (const r of results) {
    for (const t of r.timings ?? []) {
      const card = t.firstCard && !/^Plan, version/.test(t.firstCard) ? ` (${t.firstCard})` : '';
      console.log(`  ${r.id} · ${t.label}: plan ${seconds(t.planVisibleMs)}${card}${t.warmupInFlight ? ', warm-up in flight' : ''}; writing ${seconds(t.writingMs)}`);
    }
  }
}
if (results.length === 0) {
  console.log('No scenario matched the filter.');
  process.exitCode = 1;
}
if (failed.length > 0) process.exitCode = 1;
