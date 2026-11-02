from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import gzip
import hashlib
import math
import re
import statistics
from collections import Counter

spec = importlib.util.spec_from_file_location('generator', Path(__file__).resolve().parents[1] / 'synthetic/generate.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


def read_counts(out):
    """Return sample IDs, Ensembl IDs and {symbol: counts} from the gene matrix."""
    with gzip.open(Path(out) / 'counts.tsv.gz', 'rt') as handle:
        samples = handle.readline().rstrip('\n').split('\t')[3:]
        rows = [line.rstrip('\n').split('\t') for line in handle]
    return samples, [row[1] for row in rows], {row[0]: [int(v) for v in row[3:]] for row in rows}


def log_cpm(counts):
    libraries = [sum(column) for column in zip(*counts.values())]
    return {gene: [math.log2(v / size * 1e6 + 1) for v, size in zip(values, libraries)]
            for gene, values in counts.items()}


class SyntheticIntegrity(unittest.TestCase):
    def test_reference_models_reject_overlaps_and_out_of_bounds_sequences(self):
        generator.validate_reference_models()
        with patch.dict(generator.PUBLIC_PROTEINS, MKI67={'sequence': 'M' * 4000}):
            with self.assertRaisesRegex(ValueError, 'MKI67 overlaps PCNA'):
                generator.validate_reference_models()
        with patch.object(generator, 'REFERENCE_LENGTH', 100):
            with self.assertRaisesRegex(ValueError, 'outside the synthetic reference'):
                generator.validate_reference_models()

    def test_expression_panel_uses_unique_public_gene_ids(self):
        panel = generator.load_gene_panel()
        self.assertTrue(set(generator.NAMED_EXPRESSION_GENES) <= {symbol for symbol, *_ in panel})
        self.assertTrue(all(re.fullmatch(r'ENSG\d{11}', gid) for _, gid, _ in panel))
        self.assertEqual(len({gid for _, gid, _ in panel}), len(panel))
        locations = dict((symbol, location) for symbol, _, location in panel)
        self.assertEqual((locations['NSD2'], locations['FGFR3']), ('4p16.3', '4p16.3'))

    def test_demo_scenarios_support_survival_and_differential_expression(self):
        for seed in (42, 7, 103):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory() as out:
                cases, files, variants = generator.generate(out, seed=seed)
                kras = {v['case_id'] for v in variants if v['symbol'] == 'KRAS'}

                # No simulated censoring precedes three years, so the observed
                # event-free fraction equals Kaplan-Meier survival at this time.
                def survival(group):
                    return statistics.mean(c['vital_status'] == 'Alive' or c['days_to_death'] > 1096 for c in group)

                high = [c for c in cases if c['cgs_risk_key_criteria']['cgs_risk_category'] == 'High risk']
                standard = [c for c in cases if c not in high]
                mutated = [c for c in cases if c['case_id'] in kras]
                wildtype = [c for c in cases if c['case_id'] not in kras]
                self.assertGreater(survival(standard) - survival(high), .15)
                self.assertGreater(survival(wildtype) - survival(mutated), .10)
                self.assertGreater(survival(high), .1)
                self.assertLess(survival(standard), .98)
                for case in cases:
                    self.assertEqual(case['outcomes']['surivival_time_os'], case['days_to_last_follow_up'])
                    self.assertEqual(case['outcomes']['censor_os'], int(case['vital_status'] == 'Dead'))
                    self.assertEqual(case['demographic']['vital_status'], case['vital_status'])

                samples, gene_ids, counts = read_counts(out)
                self.assertEqual(samples, [c['sample_id'] for c in cases])
                self.assertEqual(len(counts), generator.EXPRESSION_GENE_COUNT)
                self.assertTrue(all(v >= 0 for values in counts.values() for v in values))
                index = {c['case_id']: i for i, c in enumerate(cases)}
                cpm = log_cpm(counts)

                def difference(gene, first, second):
                    return (statistics.mean(cpm[gene][index[c['case_id']]] for c in first)
                            - statistics.mean(cpm[gene][index[c['case_id']]] for c in second))
                effects = [difference(gene, mutated, wildtype) for gene in generator.EXPRESSION_GENES]
                effects = [v for v in effects if abs(v) > 1]
                self.assertGreaterEqual(len(effects), 10)
                self.assertTrue(any(v > 1 for v in effects) and any(v < -1 for v in effects))

                # t(4;14) raises NSD2 in every carrier and FGFR3 in most carriers.
                translocated = [c for c in cases if 't(4;14)' in generator.present_markers(c)]
                others = [c for c in cases if c not in translocated]
                self.assertGreaterEqual(len(translocated), 50)
                self.assertGreater(difference('NSD2', translocated, others), 2)
                others_nsd2 = sorted(cpm['NSD2'][index[c['case_id']]] for c in others)
                self.assertGreater(min(cpm['NSD2'][index[c['case_id']]] for c in translocated),
                                   others_nsd2[len(others_nsd2) // 2])
                fgfr3_ceiling = max(cpm['FGFR3'][index[c['case_id']]] for c in others) + 2
                fgfr3_high = statistics.mean(cpm['FGFR3'][index[c['case_id']]] > fgfr3_ceiling for c in translocated)
                self.assertTrue(.55 < fgfr3_high < .9, fgfr3_high)
                self.assertLess(difference('CCND1', translocated, others), -1)
                # Other comparisons do not inherit the NSD2 signal.
                female = [c for c in cases if c['gender'] == 'female']
                male = [c for c in cases if c['gender'] == 'male']
                self.assertLess(abs(difference('NSD2', female, male)), .3)
                self.assertGreater(difference('XIST', female, male), 4)
                self.assertLess(difference('RPS4Y1', female, male), -4)

                # Downloaded RNA counts match the matrix used for native DE.
                tsv = next((c, f) for c, f in zip(cases, files) if f['data_format'] == 'TSV')
                rows = (Path(out) / tsv[1]['file_name']).read_text().splitlines()[1:]
                self.assertEqual(len(rows), len(counts))
                by_id = dict(zip(gene_ids, counts.values()))
                for row in rows:
                    gene_id, sample, count = row.split('\t')
                    self.assertEqual(sample, tsv[0]['sample_id'])
                    self.assertEqual(int(count), by_id[gene_id][index[tsv[0]['case_id']]])
                with closing(sqlite3.connect(Path(out) / 'termdb.sqlite')) as db:
                    for number, case in enumerate(cases, 1):
                        actual = db.execute('select value from anno_categorical where sample=? and term_id=?', (number, 'kras_status')).fetchone()[0]
                        self.assertEqual(actual, 'Mutated' if case['case_id'] in kras else 'No mutation')
                        actual = db.execute('select value from anno_categorical where sample=? and term_id=?', (number, 't4_14_status')).fetchone()[0]
                        self.assertEqual(actual, 'Present' if case in translocated else 'Absent')
                        days, dead = db.execute('select tte, exit_code from survival where sample=?', (number,)).fetchone()
                        self.assertAlmostEqual(days * 365.25, case['days_to_last_follow_up'])
                        self.assertEqual(dead, case['outcomes']['censor_os'])

    def test_protein_models_and_public_hotspots_share_reference_residues(self):
        with tempfile.TemporaryDirectory() as out:
            generator.generate(out, count=20)
            with closing(sqlite3.connect(Path(out) / 'genes.sqlite')) as db:
                for symbol, gid, chrom, start in generator.GENES:
                    model = json.loads(db.execute('select genemodel from genes where name=?', (symbol,)).fetchone()[0])
                    protein = generator.PUBLIC_PROTEINS[symbol]['sequence']
                    self.assertEqual(model['aaseq'], protein)
                    self.assertGreaterEqual(len(model['pdomains']), 2, symbol)
                    self.assertEqual(model['pdomains'][0]['name'], 'Reference sequence')
                    self.assertEqual((model['pdomains'][0]['start'], model['pdomains'][0]['stop']), (1, len(protein)))
                    for feature in model['pdomains']:
                        self.assertGreaterEqual(feature['start'], 1)
                        self.assertLessEqual(feature['stop'], len(protein))
                        self.assertLessEqual(feature['start'], feature['stop'])
                        self.assertRegex(feature['color'], r'^#[0-9a-fA-F]{6}$')
                    for feature in model['pdomains'][1:]:
                        self.assertTrue(feature['url'].startswith(('https://rest.uniprot.org/', 'https://www.ebi.ac.uk/interpro/')))
                    self.assertEqual(model['cdslen'], 3 * len(protein))
                    self.assertEqual(len(model['genomicseq']), model['codingstop'] - model['codingstart'])
                    for variant in generator.variant_templates(symbol, gid, chrom, start):
                        ref, alt = variant['ref'], variant['alt']
                        if len(ref) == len(alt) > 1:
                            self.assertNotEqual(ref[0], alt[0])
                            self.assertNotEqual(ref[-1], alt[-1])
                        if variant['class'] == 'S':
                            self.assertEqual((len(ref), len(alt)), (1, 1))
                        if variant['public_protein_example']:
                            self.assertEqual(protein[variant['aa_position'] - 1], variant['mname'][2])
                kras = json.loads(db.execute("select genemodel from genes where name='KRAS'").fetchone()[0])
                self.assertEqual(len(kras['aaseq']), 188)
                ras = next(d for d in kras['pdomains'] if d['name'] == 'Ras')
                self.assertEqual((ras['start'], ras['stop']), (5, 164))
                self.assertEqual(ras['url'], 'https://www.ebi.ac.uk/interpro/api/entry/pfam/PF00071/protein/uniprot/P01116/')
                myc = json.loads(db.execute("select genemodel from genes where name='MYC'").fetchone()[0])
                bhlh = next(d for d in myc['pdomains'] if d['name'] == 'bHLH')
                self.assertEqual((bhlh['start'], bhlh['stop']), (354, 406))

    def test_all_id_spaces_and_reproducibility(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            cases, files, variants = generator.generate(first, count=20)
            generator.generate(second, count=20)
            paths = sorted(p.name for p in Path(first).iterdir())
            self.assertEqual(paths, sorted(p.name for p in Path(second).iterdir()))
            for name in paths:
                self.assertEqual((Path(first) / name).read_bytes(), (Path(second) / name).read_bytes(), name)
            ids = {c['case_id'] for c in cases}
            samples = {c['sample_id'] for c in cases}
            self.assertTrue(all(c['synthetic'] for c in cases))
            self.assertTrue(all(v['case_id'] in ids and v['sample_id'] in samples for v in variants))
            self.assertTrue(all(f['cases'][0]['case_id'] in ids for f in files))
            with closing(sqlite3.connect(Path(first) / 'termdb.sqlite')) as db:
                self.assertEqual(samples, {r[0] for r in db.execute('select name from sampleidmap')})
                self.assertEqual(len(cases), db.execute('select count(*) from survival').fetchone()[0])
            with closing(sqlite3.connect(Path(first) / 'genes.sqlite')) as db:
                for alias in ('c-Myc', 'ENSG00000136997', 'ENST00000621592', 'NM_002467'):
                    self.assertEqual('MYC', db.execute('SELECT name FROM genealias WHERE alias=?', (alias,)).fetchone()[0])
                # ProteinPaint rejects expression genes absent from both tables.
                known = {name for (name,) in db.execute('SELECT name FROM genes UNION SELECT alias FROM genealias')}
                for symbol, gid, _ in generator.load_gene_panel():
                    self.assertIn(symbol, known)
                    self.assertIn(gid, known)
                self.assertEqual('XIST', db.execute("SELECT name FROM genealias WHERE alias='ENSG00000229807'").fetchone()[0])
            header = next(line for line in (Path(first) / 'variants.vcf').read_text().splitlines() if line.startswith('#CHROM'))
            self.assertEqual(samples, set(header.split('\t')[9:]))
            reference = {}
            for line in (Path(first) / 'synthetic.fa').read_text().splitlines():
                if line.startswith('>'):
                    chrom = line[1:]
                    reference[chrom] = ''
                else:
                    reference[chrom] += line
            for variant in variants:
                self.assertEqual(reference[variant['chr']][variant['pos'] - 1:variant['pos'] - 1 + len(variant['ref'])], variant['ref'])
            for line in (Path(first) / 'variants.vcf').read_text().splitlines():
                if not line.startswith('#'):
                    self.assertEqual(len(line.split('\t')[7].removeprefix('CSQ=').split('|')), 12)
            for f in files:
                payload = (Path(first) / f['file_name']).read_bytes()
                self.assertEqual(f['file_size'], len(payload))
                self.assertEqual(f['md5sum'], hashlib.md5(payload, usedforsecurity=False).hexdigest())
                if f['data_format'] == 'FASTA':
                    self.assertEqual(set(payload.decode().splitlines()[1]), set('ACGT'))
                if f['data_format'] == 'BAM':
                    self.assertTrue(gzip.decompress(payload).startswith(b'BAM\x01'))

    def test_demographic_groups_support_survival_and_gene_comparisons(self):
        with tempfile.TemporaryDirectory() as out:
            cases, files, variants = generator.generate(out)
            self.assertEqual(len({c['project_id'] for c in cases}), 2)
            self.assertNotRegex(json.dumps(cases), r'Synthetic [AB]\b')
            self.assertEqual({c['cgs_risk_key_criteria']['cgs_risk_category'] for c in cases}, {'High risk', 'Standard risk'})
            risk_counts = Counter(c['cgs_risk_key_criteria']['cgs_risk_category'] for c in cases)
            self.assertLess(risk_counts['High risk'], risk_counts['Standard risk'])
            self.assertEqual({label for c in cases for label in c['cgs_risk_key_criteria']['cgs_risk_criteria']}, set(generator.CGS_CRITERIA))
            for case in cases:
                risk = case['cgs_risk_key_criteria']
                self.assertEqual(risk['cgs_risk_category'] == 'High risk', bool(risk['cgs_risk_criteria']))
                regimen = case['administered_regimen_lines'][0]
                self.assertEqual(dict(generator.REGIMENS)[regimen['regimen_short_name']], regimen['regimen_name'])
                self.assertEqual(regimen['first_line'] == 'Yes', regimen['line'] == 1)
            self.assertEqual({v['class'] for v in variants}, {'M', 'N', 'P', 'F', 'D', 'I', 'S'})
            for field in ('data_format', 'data_type', 'data_category', 'experimental_strategy'):
                self.assertGreaterEqual(len({f[field] for f in files}), 3)
            self.assertGreaterEqual(len({f['downstream_analyses'][0]['workflow_type'] for f in files}), 3)
            # One shared ID and one VCF row per variant, with every carrier on it.
            carriers = {}
            for variant in variants:
                carriers.setdefault(variant['ssm_id'], set()).add(variant['sample_id'])
            rows = [line.split('\t') for line in (Path(out) / 'variants.vcf').read_text().splitlines() if not line.startswith('#')]
            self.assertEqual(len(rows), len(carriers))
            self.assertTrue(all({c['sample_id'] for c, gt in zip(cases, row[9:]) if gt == '0/1'} == carriers[row[2]] for row in rows))
            for gender in ('male', 'female'):
                for race in ('white', 'black or african american', 'asian'):
                    selected = {c['case_id'] for c in cases if c['gender'] == gender and c['race'] == race}
                    self.assertGreaterEqual(len(selected), 150)
                    for risk in ('High risk', 'Standard risk'):
                        self.assertGreaterEqual(sum(c['case_id'] in selected and c['cgs_risk_key_criteria']['cgs_risk_category'] == risk for c in cases), 10)
                    for symbol, *_ in generator.GENES:
                        mutated = {v['case_id'] for v in variants if v['symbol'] == symbol and v['case_id'] in selected}
                        self.assertGreaterEqual(len(mutated), 10, (gender, race, symbol))
                        self.assertGreaterEqual(len(selected - mutated), 10, (gender, race, symbol))
                        recurrent = Counter(v['ssm_id'] for v in variants if v['symbol'] == symbol and v['case_id'] in selected)
                        self.assertGreaterEqual(recurrent.most_common(1)[0][1], 3, (gender, race, symbol))


if __name__ == '__main__':
    unittest.main()
