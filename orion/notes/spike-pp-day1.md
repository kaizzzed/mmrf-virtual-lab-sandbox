# Spike: pinned ProteinPaint viewers in a card (Day 1 AM)

RFA rev 25: F3 "How results are drawn", "How many viewers stay live", "How DE runs"; Risks row 1;
Assumption A-2; Open questions 9 and 13. Run on 2026-11-04 against the local `mmrf-synthetic`
stack (synthetic data, seed 42), `@sjcrh/proteinpaint-client` 2.216.0 (installed in the
`orion-fe-node-modules` volume) and ProteinPaint server 2.216.0.

**Result: A-2 holds.** All four mounts render from pinned state inside an Assistant card, with no
`updateArgs`, so no card follows the live cohort. The direct DE mount runs Wilcoxon with no clicks.
The DEinput fallback works too, but only after one fix to the seeded group shape (below).
Open question 9 is **no**: no pinned setting changes the p-value table's sort, so the caption stays.

## Where the code is (frontend fork; uncommitted at the spike, since committed in `cc89ca7`)

| File | What it does |
| --- | --- |
| `src/features/orion/viz/ppPinnedArgs.ts` | Pure builders from a `PinnedViz` to launcher arguments, and `pinnedHash`: sha256 of the canonical pinned document |
| `src/features/orion/viz/OrionPpCard.tsx` | Calls `bindProteinPaint` once per live session with pinned `initArgs` and a fresh root. It keeps at most two cards live, and Re-open live remounts a frozen card. Teardown aborts, calls `destroy()` where it exists, empties the root and cleans up body menus |
| `src/features/orion/viz/liveViz.ts` | The two-live registry, used through `useSyncExternalStore` |
| `src/features/orion/viz/bodyMenus.ts` | Claims the `.sja_menu_div` nodes a session adds to `<body>` and removes them with their body listeners |
| `src/features/orion/viz/spike/PpSpike.tsx`, `krasGroups.ts` | The spike page: `/?app=Assistant&spike=pp` (a lazy import in `AssistantApp.tsx`) |

Cards are added one at a time, the way answers add them to a thread. Each card sits inside
`data-orion-viz` and exposes `data-phase`, `data-mount-ms` and `data-pinned-hash` for tests.

## Inputs used

- **Survival:** Correlation launcher with `state.plots = [{chartType: 'survival', term: {id: 'overall_survival'}, term2: {id: 'cgs_risk_category'}}]` and `filter0: null` (all cases).
- **OncoMatrix:** `launchGdcMatrix` with `genes` KRAS, NRAS, DIS3, MYC, BRAF and TP53, `settings.matrix.maxGenes` 50 (the wrapper's default), and `filter0` set to the KRAS-mutated cases as a case-ID list: `{"and":[{"in":{"case_id":[...480 ids]}}]}`.
- **DE groups:** the J3 KRAS groups as ProteinPaint integer sample ids. "KRAS no mutation" (control) has 520 samples and "KRAS mutated" (case) has 480. They come from the `samplelst` of `orion/golden/proteinpaint/termdb-DE-wilcoxon.json`. Both lists were checked as identical to live `POST /protein-paint/termdb` `getsamplelist` calls for "KRAS mutation status". The sandbox maps sample n to case `SYNTH-CASE-000n`; I checked all 1,000 entries in the identity container's `cases.json`.
- **DE direct:** `launchGdcDE`, `filter0: null`, and `state.plots` set to the `differentialAnalysis` config that DEinput itself dispatches after Run (`mass/groups.js` `renderPreAnalysisData`). That config is `termType: 'geneExpression'`, `samplelst.groups`, and `tw` (a samplelst term, with values that carry DEinput's own group colors `#1b9e77` and `#d95f02`). On top it pins `settings.volcano = {method: 'wilcoxon', showPValueTable: true}`.
- **DEinput fallback:** `launchGdcDE` and `state.plots = [{chartType: 'DEinput', groups, autoSubmit: true}]`. Each group filter is one saved-cohort tvs, as ProteinPaint's Cohort picker builds it: `term: {id, name, type: 'cohort', filter0: <case-ID GQL>, isleaf: true}` plus `values: [{key: id, label: name}]`.
- **Open question 9 probe:** the DE direct card again, with `settings.volcano.rankBy: 'adjustedPValue'` pinned.

## Result per mount

Timings come from the final Playwright run (run 4). "Ready" runs from the Add or Re-open click to
the viewer's content being visible. Server-side caches were warm from earlier runs; the one
cold-path number is listed separately.

| Mount | Outcome | Evidence | Ready | Requests |
| --- | --- | --- | --- | --- |
| Survival (Correlation launcher) | **Works** | Plot "Synthetic overall survival" with High risk (339) and Standard risk (661) curves, numbers at risk, and the log-rank table, whose p-value shows **0**. Ids alone hydrate (`termsbyids` ×2). Screenshot `spike-pp-survival.png` | 1.34 s first; 1.91 s on each re-open (×4) | `genomes`, `termdb/config`, `termsbyids` ×2, `termdb/cohorts` (first mount only, each ≤16 ms), then `GET termdb/survival` at 0.94–1.16 s on every mount |
| OncoMatrix, fixed genes | **Works** | Six rows in pinned order, "480 Samples". KRAS (480) is mutated in every sample, which proves the case-ID `filter0` crossed the gateway bridge. NRAS (175) compares with 355 in all cases. No `topMutatedGenes` call (0 seen). Screenshot `spike-pp-oncomatrix.png` | 0.30 s; ~0.39 s on re-open | `POST termdb/matrix` 216 ms |
| DE, direct `differentialAnalysis` | **Works with caveats** (internal shape, Open question 13) | Volcano "KRAS no mutation vs KRAS mutated", "16 DE genes (8 up / 8 down)" and the p-value table, with no clicks. Screenshot `spike-pp-de-direct.png` | 0.75 s (server cache warm); ~0.05 s on re-open | `POST termdb/DE` (Wilcoxon run) 669 ms. Cold compute, measured by `curl` with fresh group names: **1.58 s**; the same body again took 0.70 s |
| DEinput fallback, seeded cohorts and `autoSubmit` | **Works with caveats** | Both groups seeded (pills "COHORT IS KRAS no mutation / KRAS mutated", 520 and 480 samples). autoSubmit ran pre-analysis and stopped at the method choice with **edgeR preselected**. Screenshots `spike-pp-de-input-preanalysis.png` and `spike-pp-de-input-run.png` | 0.12 s to the method choice; 0.07 s after Wilcoxon and Run | `getsamplecount` ×2 and `getsamplelist` ×2 (dofetch3 turned these GETs into POSTs because the URLs were long; 24–58 ms each), `termdb/DE` pre-analysis 6 ms. The run itself made **no network call** (see "Same body, client cache") |
| DE, `rankBy` pinned (Open question 9 probe) | Renders; the setting has no effect | Same table order as DE direct. Screenshot `spike-pp-de-rankby.png` | 0.12 s | none (client cache) |

### Fallback decision per viewer

- **Survival:** keep the pinned Correlation launcher. Its shape is the public `state.plots` path that `gdc/correlation.ts` documents. The Gen3 survival plot and a static image stay as fallbacks, but neither is needed. The chart shows p = 0, so the planned `VIEWER_P_ROUNDED` footnote and Gen3 Analysis p are still needed (Open question 16).
- **OncoMatrix:** keep the pinned `genes` and `filter0`. No fallback is needed.
- **DE:** use the **direct mount** as the primary path and keep DEinput as the fallback (cut order 2). The direct mount needs 0 clicks. The DEinput fallback needs **3**: Wilcoxon, then Run, then "Show p-value table". DEinput's own `plot_create` sends no `showPValueTable`, so the table starts hidden, and the design's "two clicks" undercounts by one for anyone who wants the table. No DEinput config can preselect Wilcoxon: the radio order comes only from `termdbConfig.queries.rnaseqGeneCount.defaultMethod` (`mass/groups.js`), which is the rejected "Wilcoxon for all users" option.
- **DEinput needed a fix:** a seeded cohort tvs without `values` throws `TypeError: Cannot read properties of undefined (reading 'length')` in `filter/tvs.cohort.js` `get_pill_label` (seen twice per mount in run 3). The fix is `values: [{key: cohortId, label: name}]`, the shape the Cohort picker's `fillMenu` produces. The earlier code map's DEinput shape omitted it.

## Open question 9: can pinned state set the DE p-value table's sort? **No.**

- **Code (client 2.216.0):** `plots/volcano/viewModel/VolcanoViewModel.ts` always sorts `pValueTable.rows` by the bar-plot column (signed log2 fold change), descending (`rows.sort((a, b) => b[fc].value - a[fc].value)`). The view passes `header: {allowSort: true}` to `renderTable`, which only adds click-to-sort buttons; nothing reads a default sort. `rankBy` is set in `getDefaultVolcanoSettings` but read nowhere (only a commented-out control refers to it).
- **Runtime:** the direct card, the `rankBy`-pinned card and the DEinput card list the same 16 genes in the same order: MAPK1, DIS3, ETV5, TP53, KRAS, FOS, DUSP6, MKI67, PCNA, JUN, ETV4, BRAF, NRAS, MAPK3, SPRY2, MYC. That is exactly the `fold_change`-descending order of the `termdb/DE` response's `dots` (2.17 down to −1.83).
- **Header click:** clicking "Adjusted p-value" re-sorts in the DOM to BRAF, ETV4, JUN, DUSP6, FOS, TP53, … (adjusted p ascending). Nothing reaches state, and a remount restores fold-change order.
- **Consequence for C-21:** keep the caption "Table sorted by fold change. Sort by adjusted p-value to match the answer." Also note that the table lists only threshold-passing genes: 16 of 7,684 tested here, at pValue 1.3 (adjusted) and foldChangeCutoff 0.3.

## Open question 13: what the spike means for the direct mount

- **It works on 2.216.0 with no extra plumbing.** `differentialAnalysis.getPlotConfig` validates and then `copyMerge`s the opts, so the pinned `settings.volcano.method` wins over the session default. `showPValueTable` is honoured.
- **The request matches the native viewer.** The direct card's `termdb/DE` run body was diffed against the golden native Wilcoxon body. The `samplelst` members are the same; only the group names (golden "New group"/"New group 1") and `filter0` (golden `{"and":[]}`, card `null`) differ. Everything else, `volcanoRender` included, is identical once the card sends DEinput's group colors. Before I added the colors, `colorSignificantUp/Down` differed, and so did the volcano image.
- **Same body, client cache.** With equal group names and samples, the DEinput fallback's run body is **byte-identical** to the direct mount's. In the same page it was then served from ProteinPaint's client cache: `dofetch3`'s module-level `defaultServerDataCache`, keyed by URL and body, capped at 360 entries. So re-opening a DE card costs no DE call. Across pages, the server DE cache (`deKeyInputs`: names, sorted samples, method, `filter`, `filter0`, …) applies; there, a repeat call took 0.70 s against 1.58 s cold.
- **Implication:** the click-free J3 needs only MMRF and the ProteinPaint maintainers to accept an unpublished shape. The technical risk is version drift. Mitigations: the exact pin (the lock resolves 2.216.0, but `package.json` says `^2.216.0`; not changed here), the shape locked by `ppPinnedArgs.unit.test.ts`, and the golden DE body diff above as a contract check. If the answer is no, the fallback costs 3 clicks, as described above.

## Teardown and leaks

Body-level state after each step of run 4 (baseline: 10 body children, 0 menus, 0 d3 body listeners):

| Step | `.sja_menu_div` on body (2 are headtips) | Body children | d3 menu listeners on body | Other d3 body listeners |
| --- | --- | --- | --- | --- |
| After survival | 25 | 35 | 25 | 3 |
| After OncoMatrix | 35 | 45 | 35 | 4 |
| After DE direct (survival froze) | 18 | 28 | 18 | 4 |
| After 3 rotations of 3 re-opens each | 31 (flat) | 41 (flat) | 31 (flat) | 23 |
| After "Remove all cards" | **2** | **12** | **2** | 23 |

- **Menus need cleanup.** A Correlation mount alone appends 23 Menu divs, plus the 2 headtips (z-index 5555, created once and kept). Each Menu also registers a `mousedown.menu<id>` listener on `<body>`. Correlation and DE expose no `destroy()`, and OncoMatrix's `destroy()` does not destroy menus.
- **Cleaning only the divs is not enough.** Run 1 removed only the divs: they stayed flat, but body listeners grew by about 37 per rotation and **199** were left after removing all cards. The card now also removes each removed menu's body listener, exactly as `Menu.destroy()` does. The div and the body listener share the d3 name `menu<id>`, which d3-selection keeps in `__on`. Both now return to baseline.
- **Some listeners still leak.** Body listeners from the filter UI (`mousedown.sja_filter_N`, 3 per Correlation mount and 4 per DEinput mount) and from OncoMatrix (`mousedown.matrixZoom-…`, 1 per mount) are not attributable from the DOM and stay. That was 23 after 14 mounts. The growth is bounded but real; Day 2 can attribute them by time window, as the menus are, or accept the growth.
- **A card that freezes while its viewer is still starting waits for `bindProteinPaint` to settle**, then aborts, destroys, empties its root and releases its menus. That way, menus created during startup are still claimed.
- **The two-live cap holds.** After every add, at most two cards were mounted and at most two `.sja_root_holder`s were live. Re-opening survival froze the oldest live card (DE input) and kept the newest (DE `rankBy`). Each re-open uses a new root element, because `bindProteinPaint` keys instances by `rootElem` in a WeakMap and never unbinds.
- **The cleanup depends on 2.216.0 internals:** the `sja_menu_div` class, the headtip's z-index 5555 and d3's `__on`. A client bump must re-run this spike.

## The FE shell's first real render (`/?app=Assistant&orion=fixture`)

The fixture replay works. I clicked the starter "Use Correlation Plot to compare Kaplan–Meier survival…"
and reached the end state ("Interpretation withheld") after 6.9 s at 250 ms per frame. The page showed the plan with
scope "All cases · 1,000 cases", rows s1–s5 marked Done, the viz placeholder, Observed with citation chips, the
limitation and the synthetic notice. The fixture page logged **no console errors or warnings**. See
`fixture-shell-start.png` and `fixture-shell-end.png`.

These shell styling bugs are outside the spike's paths and are left for the shell owner:

- **Card headings are invisible.** `cards/Card.tsx`'s `<h3>` computes to `rgb(244, 243, 241)` on a white Paper, so titles such as "Plan, version 1", "Answer" and every viz card title are unreadable. Visible in every screenshot.
- **The New thread button has no visible label.** Its text and background are both `rgb(139, 0, 83)`.

The spike page's only console messages are expected or fixed:

- the warm-up's 404 at `/api/orion/agui` (no BFF yet);
- the `get_pill_label` TypeError, fixed by the cohort tvs `values`.

## What Day 1 PM and Day 2 viz work must know

1. **`VizSpec` carries no pinned inputs.** It has only `vizId`, `appId`, `launcher`, `citesSteps` and `pinnedHash`, but a card needs the inputs themselves: sample ids, genes, terms and `filter0`. Either add a `pinned` object to `VizSpec` or have the FE read the cited step's request from the run record. I recommend the first; it is small except for DE sample lists. J3's 1,000 ids are about 4 KB as plain integers, but the 4,000-sample DE cap would be about 20 KB of the 32 KB snapshot limit.
2. **Parity hash definition used here:** sha256 over canonical JSON of `{schema: "orion.viz-pinned/v1", ...PinnedViz}` with sample ids sorted. Canonical JSON means object keys sorted at every level, array order kept, no whitespace, non-ASCII not escaped (Python: `sort_keys=True, separators=(",", ":"), ensure_ascii=False`). Known vector: the survival spec above hashes to `660a67ad…f3d7f5de`. The agent must hash the same document from the step's request, or the definition must move into the contract.
3. **"Mounted" means different things per launcher.** The `bindProteinPaint` promise resolves after the first render for Correlation (1.29 s) and DE (0.70 s), but for OncoMatrix it resolves after **13 ms**, before the matrix request. `viz_report` must not use it as "rendered"; use ProteinPaint's `postRender` callbacks or a DOM check per launcher.
4. **Width matters.** At a 1,440 px viewport the card is about 1,150 px wide, and the DE p-value table's "Adjusted p-value" column is clipped behind the card's horizontal scroll. Give DE cards the full thread width, or use Expand.
5. **DE group colors:** send DEinput's colors (`DE_GROUP_COLORS`) so the volcano image and the request body match the native tool.
6. **Survival has no client cache.** Every mount re-requests `termdb/survival` (about 1 s) because the term wrapper `$id` is random, so the client cache key changes. DE and identical DEinput runs are served from the client cache.
7. **Frozen cards show a placeholder only.** The design's frozen output (the DE volcano image or the cited table) is not built. `ComponentApi.getChartImages()` exists in 2.216.0 and is the candidate for capturing an image before teardown.
8. **The shell's warm-up always fires.** The spike rides inside `AssistantApp`, so on `spike=pp` without `orion=fixture` the warm-up 404s. The only visible effect is one console error; no card shows (`spike-pp-after-remove-all.png`).

## Reproducing

- `docker compose build frontend && docker compose up -d --wait frontend` in the sandbox. Each build took about 2.5 min, and Next's type check passed.
- The driver is a throwaway Playwright script, kept outside the repo in this session's scratchpad as `spike-pp.mjs`. It uses the root `@playwright/test` 1.58.2 in headless Chromium at 1,440 × 1,000. It adds the cards in the order survival, OncoMatrix, DE direct, DEinput (then Wilcoxon, Run, Show table), DE `rankBy`. It then re-opens survival, runs 3 rotations of re-opens, removes all cards, and loads the fixture shell. Screenshots go to `orion/notes/screenshots/` (gitignored).
- The cold DE timing: `POST /protein-paint/termdb/DE` with the golden Wilcoxon body, unique group names and `filter0: null`, posted twice with `curl` (1.58 s, then 0.70 s).
