// Fresh ephemeral Chromium profile; never attaches to a user's browser.
import {chromium, expect} from '@playwright/test';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
const origin = process.env.SANDBOX_ORIGIN || 'http://127.0.0.1:3080';
const url = new URL(origin);
const access = process.env.SANDBOX_ACCESS_FILE ? JSON.parse(await readFile(process.env.SANDBOX_ACCESS_FILE,'utf8')) : null;
const local = url.protocol === 'http:' && ['127.0.0.1','localhost'].includes(url.hostname);
const hosted = url.protocol === 'https:' && /^[a-z0-9]+\.cloudfront\.net$/.test(url.host);
if ((!local && !hosted) || url.origin !== origin || (hosted && !access) || (access && access.origin !== origin)) throw new Error('Use localhost or an explicit credential-bound CloudFront deployment');
const cases = JSON.parse(await readFile(new URL('../generated/cases.json', import.meta.url)));
const variants = JSON.parse(await readFile(new URL('../generated/variants.json', import.meta.url)));
const fixtureFiles = JSON.parse(await readFile(new URL('../generated/files.json', import.meta.url)));
const female = cases.filter(c => c.gender === 'female' && c.race === 'black or african american');
const femaleIds = new Set(female.map(c => c.case_id));
const counts = new Map();
for (const v of variants.filter(v=>femaleIds.has(v.case_id))) counts.set(v.symbol,(counts.get(v.symbol)||0)+1);
await mkdir('screenshots',{recursive:true});
const browser = await chromium.launch({headless:true});
const page = await browser.newPage({viewport:{width:1440,height:1000}, ...(access ? {httpCredentials:{origin, username:access.username,password:access.password}} : {})});
const existingCookie = 'normal-local-development-test-sentinel';
await page.context().addCookies([{name:'access_token', value:existingCookie, url:origin}]);
const failures=[];
page.on('pageerror',e=>failures.push(e.message));
page.on('request',r=>{if (/(^|\.)(google-analytics\.com|googletagmanager\.com|analytics\.google\.com|doubleclick\.net)$/.test(new URL(r.url()).hostname)) failures.push(`Unexpected tracking request: ${r.url()}`)});
page.on('response',r=>{if(r.url().startsWith(origin)&&r.status()>=400 && !(access && r.status()===401 && r.headers()['www-authenticate']?.startsWith('Basic realm="Synthetic Virtual Lab"'))) failures.push(`${r.status()} ${r.url()}`)});
async function open(app) { await page.goto(`${origin}/?app=${app}`,{waitUntil:'domcontentloaded'}); await expect(page.getByText('Synthetic sandbox — all data is fabricated and has no clinical meaning.',{exact:true})).toBeVisible(); }
async function capture(name){await page.mouse.move(0,0);await page.screenshot({path:`screenshots/${name}.png`,fullPage:true});}
try {
  await open('CohortBuilder');
  await expect(page.getByTestId('cookie-consent-banner')).toHaveCount(0);
  // Previously accepted production consent must not enable synthetic tracking.
  await page.evaluate(()=>localStorage.setItem('mmrf-cookie-consent','accepted'));
  await open('CohortBuilder');
  await expect(page.locator('body')).toContainText(new RegExp(`${cases.length.toLocaleString('en-US')}\\s*CASES`,'i'),{timeout:30000});
  await page.getByRole('tab',{name:'CGS Risk',exact:true}).click();
  for (const label of ['High risk','Standard risk', ...new Set(cases.flatMap(c=>c.cgs_risk_key_criteria.cgs_risk_criteria))]) {
    await expect(page.getByRole('checkbox',{name:label,exact:true})).toBeVisible();
  }
  await page.getByRole('checkbox',{name:'High risk',exact:true}).check();
  const highRisk = cases.filter(c=>c.cgs_risk_key_criteria.cgs_risk_category==='High risk');
  await expect(page.locator('body')).toContainText(new RegExp(`${highRisk.length}\\s*CASES`,'i'),{timeout:30000});
  await page.locator('[id="cgs_risk_key_criteria.other_criteria"]').getByText(/\d+ more/).click();
  await page.getByRole('checkbox',{name:'TP53 mutation: Present',exact:true}).check();
  const highRiskTp53 = highRisk.filter(c=>c.cgs_risk_key_criteria.other_criteria.includes('TP53 mutation: Present'));
  await expect(page.locator('body')).toContainText(new RegExp(`${highRiskTp53.length}\\s*CASES`,'i'),{timeout:30000});
  await capture('cgs-risk-labels');
  await page.getByRole('checkbox',{name:'TP53 mutation: Present',exact:true}).uncheck();
  await page.getByRole('checkbox',{name:'High risk',exact:true}).uncheck();
  await expect(page.locator('body')).toContainText(new RegExp(`${cases.length.toLocaleString('en-US')}\\s*CASES`,'i'),{timeout:30000});
  console.log(`PASS: CGS labels and combined filters select ${highRiskTp53.length}/${highRisk.length}/${cases.length} synthetic cases`);
  await page.getByRole('tab',{name:'Demographic',exact:true}).click();
  await page.getByRole('checkbox',{name:'female',exact:true}).check();
  await page.getByRole('checkbox',{name:'black or african american',exact:true}).check();
  await expect(page.locator('body')).toContainText(new RegExp(`${female.length}\\s*CASES`,'i'),{timeout:30000});
  await capture('cohort-filter');
  console.log(`PASS: browser cohort builder selects ${female.length}/${cases.length} synthetic cases`);
  await open('OncoMatrix');
  for (const [gene,count] of counts) await expect(page.getByText(`${gene} (${count})`,{exact:true})).toBeVisible({timeout:60000});
  const renderedSamples = await page.locator('svg text').allTextContents();
  const allowed = new Set(female.map(c=>c.sample_id));
  if(renderedSamples.filter(s=>s.startsWith('SYNTH-SAMPLE-')).some(s=>!allowed.has(s))) throw new Error('Matrix escaped selected cohort');
  await capture('oncomatrix');
  console.log('PASS: OncoMatrix selected-cohort mutation counts match generated source');
  await open('Repository');
  await expect(page.getByText(`${female[0].sample_id}.synthetic.fasta`,{exact:true})).toBeVisible({timeout:30000});
  await capture('repository');
  console.log('PASS: repository displays synthetic metadata');
  await page.getByRole('checkbox',{name:'BAM',exact:true}).check();
  const expectedFiles = fixtureFiles.filter(f=>femaleIds.has(f.cases[0].case_id)&&f.data_format==='BAM');
  for (const [button, idKey] of [['button-sample-sheet-files-table','file_id'], ['button-manifest-files-table','object_id'], ['button-metadata-files-table','file_id']]) {
    const pending = page.waitForEvent('download');
    await page.getByTestId(button).click();
    const download = await pending;
    const exported = JSON.parse(await readFile(await download.path(),'utf8'));
    expect(exported.map(f=>f[idKey]).sort()).toEqual(expectedFiles.map(f=>f.file_id).sort());
  }
  const fixture = expectedFiles[0];
  const pendingDetail = page.waitForEvent('popup');
  await page.getByText(fixture.file_name,{exact:true}).click();
  const filePage = await pendingDetail;
  filePage.on('pageerror',e=>failures.push(e.message));
  const localFile = filePage.locator('a[href*="/user/data/download/"]').first();
  const pendingFile = filePage.waitForEvent('download');
  await localFile.click();
  const downloadedFile = await pendingFile;
  expect(await readFile(await downloadedFile.path())).toEqual(await readFile(new URL(`../generated/${fixture.file_name}`,import.meta.url)));
  await filePage.close();
  await page.getByRole('checkbox',{name:'BAM',exact:true}).uncheck();
  console.log('PASS: website sample sheet, manifest, metadata and synthetic file downloads respect the selected BAM cohort');
  await open('Projects');
  for (const projectId of ['SYNTHETIC-MM','SYNTHETIC-RNA']) await expect(page.getByRole('link',{name:projectId,exact:true})).toBeVisible();
  await capture('projects');
  const pendingProject = page.waitForEvent('popup');
  await page.getByRole('link',{name:'SYNTHETIC-MM',exact:true}).click();
  const projectPage = await pendingProject;
  projectPage.on('pageerror',e=>failures.push(e.message));
  await expect(projectPage.getByTestId('text-file-count-project-summary')).toContainText('500');
  await projectPage.screenshot({path:'screenshots/project-summary.png',fullPage:true});
  await projectPage.getByTestId('button-view-files-in-repository').click();
  await expect(projectPage.getByText('SYNTH-SAMPLE-0001.synthetic.fasta',{exact:true})).toBeVisible();
  await projectPage.close();
  console.log('PASS: Projects list, project summary and View Files navigation');
  // Detail queries fail as a whole if Guppy lacks any field they name.
  const kras = variants.find(v=>v.symbol==='KRAS');
  for (const [path,heading,text] of [[`/cases/${cases[0].case_id}`,'Case Summary',cases[0].case_id],[`/genes/${kras.gene_id}`,'Gene Summary','Synthetic KRAS'],[`/ssms/${kras.ssm_id}`,'Mutation Summary',kras.ssm_id]]) {
    await page.goto(origin+path,{waitUntil:'domcontentloaded'});
    await expect(page.getByText(text,{exact:true}).first()).toBeVisible({timeout:30000});
    await expect(page.getByText(/Not Found$/)).toHaveCount(0);
    await capture(heading.toLowerCase().replace(' ','-'));
  }
  console.log('PASS: case, gene, and mutation detail pages render their records');
  const bulkResponse = page.waitForResponse(r => r.url().includes('/termdb/sampleScatter') && r.status() === 200);
  await open('Umap');
  const bulk = await (await bulkResponse).json();
  expect(Object.values(bulk.result).flatMap(group => group.samples).length).toBe(female.length);
  await expect(page.getByText('SAMPLE SCATTER',{exact:true})).toBeVisible();
  await page.waitForTimeout(1500);
  await capture('bulk-umap');
  await open('SC');
  await page.getByText('combined',{exact:true}).click();
  await expect(page.getByText('Meta analysis',{exact:true}).first()).toBeVisible({timeout:30000});
  await page.getByText('Meta analysis',{exact:true}).first().click();
  await expect(page.getByText(new RegExp(`${female.length * 80} cells`))).toBeVisible({timeout:60000});
  await page.waitForTimeout(1500);
  await capture('single-cell');
  console.log('PASS: bulk UMAP and single-cell sample selection');
  await open('Correlation');
  await expect(page.getByText('Correlation Input',{exact:true}).first()).toBeVisible({timeout:30000});
  await expect(page.getByText('Data Variables',{exact:true})).not.toBeVisible();
  await expect(page.getByText('Regression Analysis',{exact:true})).not.toBeVisible();
  await capture('correlation');
  await open('cDave');
  await expect(page.getByText('Overall Survival',{exact:true}).first()).toBeVisible({timeout:30000});
  await expect(page.locator('body')).toContainText(new RegExp(`${female.length}\\s*Cases\\s*with Survival Data`),{timeout:30000});
  await expect(page.getByText('Not enough survival data',{exact:true})).toHaveCount(0);
  await capture('clinical-survival');
  await open('GeneExpression');
  await page.getByRole('button',{name:'Submit',exact:true}).first().click();
  await expect(page.locator('body')).toContainText(new RegExp(`${female.length}\\s*Samples`),{timeout:60000});
  await expect(page.locator('body')).toContainText(/1000\s*Genes/);
  // With 1,000 rows ProteinPaint hides row labels, so check the row exists.
  await expect(page.locator('[data-testid="sjpp-matrix-label-left"]').filter({hasText:/^KRAS$/}).first()).toBeAttached();
  await capture('expression-clustering');
  await open('ProteinPaint');
  await page.getByRole('searchbox',{name:'Gene symbol, position, or alias'}).fill('c-Myc');
  await page.getByRole('searchbox',{name:'Gene symbol, position, or alias'}).press('Enter');
  const mycVariants = new Set(variants.filter(v=>femaleIds.has(v.case_id)&&v.symbol==='MYC').map(v=>v.ssm_id));
  await expect(page.getByText(`${mycVariants.size} variants`,{exact:true})).toBeVisible({timeout:60000});
  await capture('proteinpaint-gene');
  await open('GB');
  await page.getByRole('searchbox',{name:'Gene symbol, position, or alias'}).fill('chr12:10001-12000');
  await page.getByRole('searchbox',{name:'Gene symbol, position, or alias'}).press('Enter');
  const krasVariants = new Set(variants.filter(v=>femaleIds.has(v.case_id)&&v.symbol==='KRAS').map(v=>v.ssm_id));
  await expect(page.getByText(`${krasVariants.size} variants`,{exact:true})).toBeVisible({timeout:60000});
  await capture('genome-browser');
  await open('MutationFrequency');
  await expect(page.getByText('Synthetic KRAS',{exact:true})).toBeVisible({timeout:30000});
  await expect(page.locator('body')).not.toContainText('Not enough survival data');
  await capture('mutation-frequency');
  const [mutationSurvival] = await Promise.all([
    page.waitForResponse(r=>r.url().endsWith('/survival/compare_genomic')&&r.request().postDataJSON()?.type==='ssm'&&r.request().postDataJSON()?.symbol?.startsWith('SYNTH-SSM-')),
    page.getByTestId('button-mutations-tab').click(),
  ]);
  const mutationCurves = await mutationSurvival.json();
  expect(mutationCurves.results).toHaveLength(2);
  expect(mutationCurves.results.every(curve=>curve.donors.length>=10)).toBe(true);
  await expect(page.locator('body')).not.toContainText('Not enough survival data');
  await capture('mutation-survival');
  console.log('PASS: selected-cohort expression clustering, ProteinPaint gene view, and mutation frequency');
  // Keep the production age-unit regression covered in the synthetic launch path.
  await open('CohortBuilder');
  async function renameCohort(name) {
    await page.getByRole('button',{name:'Rename cohort',exact:true}).click();
    await page.getByPlaceholder('Enter new name').fill(name);
    await page.getByPlaceholder('Enter new name').press('Enter');
  }
  await renameCohort('Synthetic women');
  await page.getByRole('button',{name:'Add cohort',exact:true}).click();
  await renameCohort('Synthetic men');
  await page.getByRole('tab',{name:'Demographic',exact:true}).click();
  await page.getByRole('checkbox',{name:'male',exact:true}).check();
  await page.getByRole('checkbox',{name:'white',exact:true}).check();
  const men = cases.filter(c => c.gender === 'male' && c.race === 'white');
  await expect(page.locator('body')).toContainText(new RegExp(`${men.length}\\s*CASES`,'i'),{timeout:30000});
  await open('CohortComparison');
  await page.getByTestId('button-Synthetic women-cohort-comparison').check();
  const ageResponse = page.waitForResponse(r=>r.url().endsWith('/analysis/v0/compare/facets')&&r.request().method()==='POST');
  await page.getByTestId('button-run-cohort-comparison').click();
  const ageResult = await ageResponse;
  expect(ageResult.request().postDataJSON().interval).toEqual({'diagnoses.age_at_diagnosis':10});
  const ageCard = page.getByTestId('card-Age At Diagnosis-cohort-comparison');
  await expect(ageCard.getByTestId('text-analysis-40 to <50 years')).toBeVisible({timeout:30000});
  for (const row of await ageCard.locator('tbody tr').all()) {
    const values = await row.locator('td').allTextContents();
    const lower = parseFloat(values[0]);
    for (const [i, group] of [men, female].entries()) {
      const expected = group.filter(c=>c.diagnoses[0].age_at_diagnosis>=lower&&c.diagnoses[0].age_at_diagnosis<lower+10);
      expect(Number(values[1+i*2].replace(/[^0-9]/g,''))).toBe(expected.length);
    }
  }
  await capture('cohort-comparison');
  await ageCard.screenshot({path:'screenshots/diagnosis-age-buckets.png'});
  const membership = page.waitForResponse(r=>r.url().endsWith('/analysis/v0/cohorts/query')&&r.request().postData()?.includes('query Case_case'));
  await ageCard.locator('tbody tr').first().getByTestId('button-save-filtered-cohort').first().click();
  const members = (await (await membership).json()).data.CaseCentric_case_centric.map(c=>c.case_id).sort();
  expect(members).toEqual(men.filter(c=>c.diagnoses[0].age_at_diagnosis>=40&&c.diagnoses[0].age_at_diagnosis<50).map(c=>c.case_id).sort());
  await open('cDave');
  const ageControl = page.getByTestId('row-field-Age At Diagnosis-cdave');
  await expect(ageControl).toBeVisible({timeout:30000});
  await ageControl.getByRole('switch').check();
  const clinicalAge = page.getByTestId('Age At Diagnosis-card');
  await expect(clinicalAge).toContainText(/40 to < 50/,{timeout:30000});
  await expect(clinicalAge.getByText('Days',{exact:true})).toHaveCount(0);
  const expectedAgeBins = new Set(men.map(c=>Math.floor(c.diagnoses[0].age_at_diagnosis/10)*10));
  await expect(clinicalAge.locator('tbody tr')).toHaveCount(expectedAgeBins.size);
  let clinicalTotal = 0;
  for (const row of await clinicalAge.locator('tbody tr').all()) {
    const values = await row.locator('td').allTextContents();
    const lower = parseFloat(values[1]);
    const expected = men.filter(c=>c.diagnoses[0].age_at_diagnosis>=lower&&c.diagnoses[0].age_at_diagnosis<lower+10).length;
    await expect.poll(async()=>parseInt(await row.locator('td').nth(2).innerText(),10),{timeout:30000}).toBe(expected);
    clinicalTotal += expected;
  }
  expect(clinicalTotal).toBe(men.length);

  await clinicalAge.screenshot({path:'screenshots/diagnosis-age-clinical.png'});
  console.log('PASS: comparison age buckets, exact bucket cohort membership, and clinical age histogram use years');
  await expect(page.locator('#google-analytics-script, #google-analytics-init')).toHaveCount(0);
  expect((await page.context().cookies(origin)).filter(cookie=>/^_ga(?:_|$)/.test(cookie.name))).toEqual([]);
  console.log('PASS: no tracking modal, GA scripts, GA cookies, or tracking requests with prior consent accepted');
  await writeFile('screenshots/browser-errors.json', JSON.stringify(failures,null,2)+'\n');
  expect(failures).toEqual([]);
  expect((await page.context().cookies(origin)).find(cookie => cookie.name === 'access_token')?.value).toBe(existingCookie);
  console.log('PASS: sandbox preserves a pre-existing localhost login cookie');
} catch(error) {await capture('failure'); console.error(await page.locator('body').innerText()); console.error(failures); throw error;} finally {await browser.close();}
