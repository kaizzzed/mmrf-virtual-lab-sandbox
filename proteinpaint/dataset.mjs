import { readFileSync } from 'node:fs';
import { join } from 'node:path';
// Fresh configuration against the public ProteinPaint Mds3 contract.
// No private ppmmrf adapter code or deployment configuration is copied here.
export default function (_common, refs) {
  const cases = JSON.parse(readFileSync(join(refs.serverconfig.tpmasterdir, 'cases.json')));
  const sampleToCase = new Map(cases.flatMap((record, index) => [[record.sample_id, record.case_id], [index + 1, record.case_id]]));
  const variants = JSON.parse(readFileSync(join(refs.serverconfig.tpmasterdir, 'variants.json')));
  const kras = new Set(variants.filter(v=>v.symbol==='KRAS').map(v=>v.case_id));
  function value(record, id) {
    if (id === 'cgs_risk_category') return record.cgs_risk_key_criteria.cgs_risk_category;
    if (id === 'kras_status') return kras.has(record.case_id) ? 'Mutated' : 'No mutation';
    if (id === 't4_14_status') return record.cgs_risk_key_criteria.other_criteria.includes('t(4;14): Present') ? 'Present' : 'Absent';
    return record[id];
  }
  function selected(record, index, filter) {
    if (!filter) return true;
    if (typeof filter === 'string') return selected(record, index, JSON.parse(filter));
    if (filter.type === 'tvslst') {
      const results = filter.lst.map(item => selected(record, index, item));
      const match = filter.join === 'or' ? results.some(Boolean) : results.every(Boolean);
      return filter.in === false ? !match : match;
    }
    const tvs = filter.tvs;
    if (!tvs?.term) throw new Error('Unsupported synthetic ProteinPaint filter');
    let match;
    if (tvs.term.type === 'samplelst') {
      match = Object.values(tvs.term.values).some(group => group.list.some(sample => (sample.sampleId || sample.sample) === index + 1));
    } else if (tvs.term.type === 'categorical') {
      match = tvs.values.some(v => v.key === value(record, tvs.term.id));
    } else if (['integer', 'float'].includes(tvs.term.type)) {
      const value = record[tvs.term.id];
      match = tvs.ranges.some(r => (r.startunbounded || (r.startinclusive ? value >= r.start : value > r.start)) &&
        (r.stopunbounded || (r.stopinclusive ? value <= r.stop : value < r.stop)));
    } else throw new Error('Unsupported synthetic ProteinPaint filter type');
    return tvs.isnot ? !match : match;
  }
  return {
    isMds3: true,
    label: 'Synthetic MMRF',
    variant2samples: {
      variantkey: 'ssm_id',
      twLst: [{id: 'gender', q: {}}, {id: 'race', q: {}}],
    },
    // Public ProteinPaint presentation API: use the curated Virtual Lab menu.
    commonCharts: {summaryInput: true, survival: true, DA: true},
    cohort: {
      db: { file: 'termdb.sqlite' },
      termdb: {
        allowedTermTypes: ['geneVariant', 'cohort'],
        displaySampleIds: () => true,
        timeUnit: 'years',
        matrix: { appName: 'OncoMatrix' },
        convertSampleId: { get: inputs => Object.fromEntries(inputs.map(sample => [sample, sampleToCase.get(sample)])) },
      },
      scatterplots: { plots: [{name: 'Bulk RNA-seq UMAP', description: 'Artificial synthetic coordinates; no biological structure', dimension: 2,
        file: 'bulk-umap.tsv', coordsColumns: {x: 2, y: 3}, settings: {size: 2}}] },
    },
    queries: {
      topMutatedGenes: {
        get: async query => {
          const allowed = new Set(cases.flatMap((record, index) => selected(record, index, query.filter) ? [record.sample_id] : []));
          const counts = new Map();
          for (const variant of variants) {
            if (!allowed.has(variant.sample_id)) continue;
            if (!counts.has(variant.symbol)) counts.set(variant.symbol, new Set());
            counts.get(variant.symbol).add(variant.sample_id);
          }
          return [...counts].sort((a, b) => b[1].size - a[1].size).slice(0, Math.min(query.maxGenes || 50, 50))
            .map(([gene, samples]) => ({ gene, mutationStat: [{ dt: 1, count: samples.size }] }));
        },
      },
      snvindel: { forTrack: true, byrange: { bcffile: 'variants.bcf.gz' } },
      cnv: {type: 'log2ratio', file: 'cnv.tsv.gz'},
      geneExpression: {file: 'expression.h5', unit: 'Synthetic TPM'},
      rnaseqGeneCount: {file: 'counts.h5'},
      topVariablyExpressedGenes: {},
      singleCell: {
        samples: {},
        data: {
          settings: {svgw: 1000, svgh: 1000, sampleSizeThree: 0.01},
          plots: [{name: 'Meta analysis', folder: 'singlecell/meta', sampleId: 'combined', fileSuffix: '.tsv', isMetaResult: true,
            colorColumns: [{index: 5, name: 'Lineage Group'}, {index: 2, name: 'Subcluster'},
              {index: 1, name: 'Sample'}, {index: 3, name: 'Subcluster Compartment'}, {index: 4, name: 'Cell Type'}],
            cellTypeColumn: {index: 5},
            cellType2label: new Map(['Plasma', 'T', 'B', 'Myeloid'].map(key => [key, `Synthetic ${key} cells`])),
            coordsColumns: {x: 6, y: 7}}],
          addCellTypeFractionTerms(terms, ds) {
            // Native SQLite is read-only at runtime. Keep computed bin metadata in memory;
            // the assay generator already registered the dictionary entries and annotations.
            const nativeLookup = ds.cohort.termdb.q.termjsonByOneid;
            const computed = new Map(terms.map(term => [term.id, {...term, isleaf: true}]));
            ds.cohort.termdb.q.termjsonByOneid = id => computed.has(id) ? structuredClone(computed.get(id)) : nativeLookup(id);
          },
        },
        geneExpression: {folder: 'singlecell/expression', unit: 'log1p'},
        pseudobulk: {geneExpression: {'Cell Type': {folder: 'singlecell/pseudobulk', meanExt: '.mean.h5',
          percentExt: '.percent.h5', totalExt: '.total.h5',
          categories: Object.fromEntries(['Plasma', 'T', 'B', 'Myeloid'].map(key => [key, {label: `Synthetic ${key} cells`}])),
        }}},
      },
    },
  };
}
