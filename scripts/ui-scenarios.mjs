// Run against the disposable local stack with a fresh Chromium profile.
import {chromium, expect} from '@playwright/test';
import {mkdir, readFile} from 'node:fs/promises';

const origin = process.env.SANDBOX_ORIGIN || 'http://127.0.0.1:3080';
const url = new URL(origin);
if (url.protocol !== 'http:' || !['localhost', '127.0.0.1'].includes(url.hostname) || url.origin !== origin) {
  throw new Error('Scenario checks require the local synthetic stack');
}
const variants = JSON.parse(await readFile(new URL('../generated/variants.json', import.meta.url)));
const cases = JSON.parse(await readFile(new URL('../generated/cases.json', import.meta.url)));
const proteins = JSON.parse(await readFile(new URL('../synthetic/public-proteins.json', import.meta.url)));
const featureBandColors = {
  Domain: '#ffc574',
  'DNA binding': '#8dd3c7',
  'Zinc finger': '#beaeda',
  Region: '#c8df86',
  Motif: '#91c4e3',
};
await mkdir('screenshots', {recursive: true});
let browser;
let page;
const errors = [];
async function open(app) {
  await page.goto(`${origin}/?app=${app}`, {waitUntil: 'domcontentloaded'});
  await expect(page.getByText('Synthetic sandbox — all data is fabricated and has no clinical meaning.', {exact: true})).toBeVisible();
}
async function capture(name) {
  // ProteinPaint animates SVG geometry after data has loaded.
  await page.waitForTimeout(1500);
  await page.screenshot({path: `screenshots/${name}.png`, fullPage: true});
}
async function expectInViewport(elements) {
  await expect(elements).not.toHaveCount(0);
  await elements.first().scrollIntoViewIfNeeded();
  for (const element of await elements.all()) {
    await expect(element).toBeInViewport();
  }
}
try {
  browser = await chromium.launch({headless: true});
  page = await browser.newPage({viewport: {width: 1800, height: 1100}});
  page.on('pageerror', error => errors.push(error.message));
  for (const symbol of ['KRAS', 'NRAS', 'TP53', 'BRAF', 'DIS3', 'MYC']) {
    await open('ProteinPaint');
    const search = page.getByRole('searchbox', {name: 'Gene symbol, position, or alias'});
    await search.fill(symbol);
    await search.press('Enter');
    const carriers = variants.filter(v => v.symbol === symbol);
    await expect(page.getByText(`${new Set(carriers.map(v => v.ssm_id)).size} variants`, {exact: true})).toBeVisible({timeout: 30000});
    await expect(page.getByText(`${new Set(carriers.map(v => v.case_id)).size} samples`, {exact: true})).toBeVisible({timeout: 30000});
    await expect(page.locator('svg rect[fill="#e4e9ee"]')).toBeVisible();
    await expectInViewport(page.locator('svg rect[fill="#e4e9ee"]'));
    await expect(page.locator(`a[href="${proteins[symbol].features[0].source}"]`).first()).toBeVisible();
    if (symbol === 'KRAS') {
      const residues = page.locator('svg text').filter({hasText: /^[ACDEFGHIKLMNPQRSTVWY]$/}).filter({visible: true});
      await expect(residues).toHaveCount(188);
      await expectInViewport(residues);
      expect((await residues.allTextContents()).join('')).toBe(proteins.KRAS.sequence);
      await expect(page.getByText('p.G12D', {exact: true})).toBeVisible();
      await expect(page.getByText('p.Q61H', {exact: true})).toBeVisible();
      await capture('kras-reference-sequence');
    }
  }
  console.log('PASS: all six native gene tracks match carrier counts; KRAS renders its 188 reference residues and hotspots');

  for (const width of [1280, 1440]) {
    await page.setViewportSize({width, height: 1000});
    for (const symbol of ['KRAS', 'NRAS', 'TP53', 'BRAF', 'DIS3', 'MYC']) {
      await open('ProteinPaint');
      const search = page.getByRole('searchbox', {name: 'Gene symbol, position, or alias'});
      await search.fill(symbol);
      await search.press('Enter');
      // The native viewer hides letters when they cannot fit. Both the full
      // reference band and real feature bands must still be drawn at that scale.
      await expect(page.locator('svg rect[fill="#e4e9ee"]')).toBeVisible({timeout: 30000});
      for (const type of new Set(proteins[symbol].features.map(feature => feature.type))) {
        const color = featureBandColors[type];
        expect(color, `Feature color for ${type}`).toBeDefined();
        const featureBands = page.locator(`svg rect[fill="${color}"]`).filter({visible: true});
        const expectedBands = proteins[symbol].features.filter(feature => feature.type === type).length;
        await expect(featureBands).toHaveCount(expectedBands);
        await expectInViewport(featureBands);
      }
      if (!['KRAS', 'MYC'].includes(symbol)) continue;
      await capture(`${symbol.toLowerCase()}-${width}-domains`);
      const residues = page.locator('svg text').filter({hasText: /^[ACDEFGHIKLMNPQRSTVWY]$/}).filter({visible: true});
      for (let zoom = 0; zoom < 3 && await residues.count() === 0; zoom++) {
        await page.getByText('In', {exact: true}).click();
        await page.waitForTimeout(700);
      }
      await expect(residues).not.toHaveCount(0);
      await expectInViewport(residues);
      const sequence = (await residues.allTextContents()).join('');
      expect(proteins[symbol].sequence.includes(sequence)).toBe(true);
      await capture(`${symbol.toLowerCase()}-${width}-sequence`);
    }
  }
  await page.setViewportSize({width: 1800, height: 1100});
  console.log('PASS: all six protein tracks retain feature bands at normal window sizes; KRAS and MYC letters appear on native zoom');

  for (const [term, worse, better, minimumGap, name] of [
    ['CGS risk category', 'High risk', 'Standard risk', .15, 'risk-survival'],
    ['KRAS mutation status', 'Mutated', 'No mutation', .10, 'kras-survival'],
  ]) {
    await open('Correlation');
    await page.locator('tr').filter({hasText: 'Primary variable'}).getByText('+', {exact: true}).click();
    await page.getByText('Synthetic overall survival', {exact: true}).click();
    // The native selector replaces its controls after choosing the primary term.
    await page.waitForTimeout(600);
    await page.getByTestId('sjpp-tw-addTermBtn-summaryInputControlTerm2surv').click();
    await page.getByText(term, {exact: true}).filter({visible: true}).click();
    await page.waitForTimeout(600);
    const response = page.waitForResponse(r => r.url().includes('/termdb/survival'));
    await page.getByRole('button', {name: 'Submit', exact: true}).click();
    const result = await (await response).json();
    const column = key => result.keys.indexOf(key);
    const initial = result.case.filter(row => row[column('time')] === 0);
    expect(initial.reduce((sum, row) => sum + row[column('nrisk')], 0)).toBe(cases.length);
    function survivalAtThreeYears(group) {
      return result.case.filter(row => row[column('seriesId')] === group && row[column('time')] <= 3).at(-1)[column('survival')];
    }
    expect(survivalAtThreeYears(better) - survivalAtThreeYears(worse)).toBeGreaterThan(minimumGap);
    await expect(page.getByText('Probability of Survival', {exact: true})).toBeVisible();
    await capture(name);
    console.log(`PASS: native survival curves show poorer outcomes for ${worse} versus ${better}`);
  }

  // "New group vs others" compares the selected group (group 1) with the rest;
  // the volcano plots log2(others / selected group).
  async function differentialExpression(term, value, count) {
    await open('DE');
    await page.getByText('Add group', {exact: true}).click();
    await page.getByText(term, {exact: true}).click();
    await page.locator('tr').filter({hasText: `${value} (n=${count})`}).getByRole('checkbox').check();
    await page.getByRole('button', {name: 'Apply', exact: true}).click();
    const preanalysis = page.waitForResponse(r => r.url().includes('/termdb/DE') && r.request().postDataJSON()?.preAnalysis);
    await page.getByText('Submit (New group vs others)', {exact: true}).click();
    const groups = (await (await preanalysis).json()).data;
    expect(groups['New group']).toBe(count);
    // edgeR on 10,000 genes and 1,000 samples takes about 40 seconds.
    const analysis = page.waitForResponse(r => r.url().includes('/termdb/DE') && !r.request().postDataJSON()?.preAnalysis, {timeout: 180000});
    await page.getByRole('button', {name: 'Run Differential Gene Expression Analysis', exact: true}).click();
    const result = (await (await analysis).json()).data;
    await expect(page.getByText('Volcano', {exact: true})).toBeVisible();
    await page.getByText('Show p-value table', {exact: true}).click();
    return result;
  }

  const krasCount = new Set(variants.filter(v => v.symbol === 'KRAS').map(v => v.case_id)).size;
  const kras = await differentialExpression('KRAS mutation status', 'Mutated', krasCount);
  const significant = kras.dots.filter(g => g.adjusted_p_value < .05 && Math.abs(g.fold_change) > 1);
  expect(significant.length).toBeGreaterThanOrEqual(10);
  expect(significant.some(g => g.fold_change > 1)).toBe(true);
  expect(significant.some(g => g.fold_change < -1)).toBe(true);
  await capture('kras-differential-expression');
  console.log(`PASS: native differential expression returns ${significant.length} significant genes for KRAS-mutated versus remaining cases`);

  const t414Count = cases.filter(c => c.cgs_risk_key_criteria.other_criteria.includes('t(4;14): Present')).length;
  const t414 = await differentialExpression('t(4;14) translocation', 'Present', t414Count);
  const nsd2 = t414.dots.find(g => g.gene_name === 'NSD2');
  // Higher in t(4;14) Present, so negative when Present is the selected group.
  // Wilcoxon p-values saturate; NSD2 shares the smallest value.
  expect(nsd2.fold_change).toBeLessThan(-2);
  expect(nsd2.adjusted_p_value).toBeLessThanOrEqual(Math.min(...t414.dots.map(g => g.adjusted_p_value)) * 1.01);
  expect(t414.dots.find(g => g.gene_name === 'FGFR3').fold_change).toBeLessThan(-3);
  await capture('t4-14-differential-expression');
  console.log(`PASS: t(4;14) Present versus others gives NSD2 the smallest p-value (log2FC others/Present ${nsd2.fold_change.toFixed(2)})`);
  expect(errors).toEqual([]);
} catch (error) {
  if (page && !page.isClosed()) {
    try {
      await capture('scenario-failure');
      console.error(await page.locator('body').innerText());
    } catch (diagnosticError) {
      console.error('Could not capture scenario failure details:', diagnosticError);
    }
  }
  throw error;
} finally {
  if (browser) {
    await browser.close().catch(error => {
      console.error('Could not close the scenario browser:', error);
      process.exitCode = 1;
    });
  }
}
