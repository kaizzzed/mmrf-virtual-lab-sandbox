# Synthetic data

The default seed (`42`) creates 1,000 fabricated cases. Reusing the seed and
generator version reproduces the same records. No patient dataset is used.

## Demonstration scenarios

- **Survival:** High risk and KRAS-mutated groups have shorter simulated event
  times. Independent censoring produces overlapping curves rather than perfect
  separation. Effect sizes are arbitrary, not fitted clinical estimates.
- **Expression:** RNA-Seq counts cover 10,000 public gene symbols. The original
  36 genes keep their uneven latent groups and artificial KRAS shifts, giving
  KRAS differential expression a useful top-10 result. See
  [expression model](#expression-model). Bulk TPM derives from the counts;
  downloadable RNA-Seq TSVs match the count matrix.
- **Mutations:** Six genes have recurring public protein-change examples plus
  invented background variants. Carrier frequencies and hotspot weights are
  arbitrary. TP53 variants match the corresponding CGS marker.

CGS markers follow approximate frequencies reported for newly diagnosed
myeloma: t(4;14) 15%, t(14;16) 4%, t(14;20) 1.5%, 1q gain about 35%
(about 60% with an IgH translocation), del(17p) 9%, TP53 mutation about 10%
(about half with del(17p)), and del(1p32) 11% (1.5% biallelic). High risk follows
the [IMS/IMWG consensus criteria](https://doi.org/10.1200/JCO-24-01893) and
covers about a third of cases. TP53 is slightly above its usual frequency so
every demographic subgroup has at least 10 carriers for comparisons.

The KRAS survival effect is a test scenario, not a claim that every KRAS mutation
predicts poor survival in myeloma. Expression signatures and background variant
annotations are also demonstration constructs.

## Expression model

`synthetic/public-genes.tsv` lists the 36 original genes, the named genes
below, and a deterministic sample of HGNC protein-coding genes; see
`synthetic/build_gene_panel.py`. Symbols, Ensembl gene IDs, and cytogenetic
locations come from the [HGNC complete set](https://www.genenames.org/download/)
(retrieved 2026-11-02, [CC0](https://www.genenames.org/about/license/)).
Transcript lengths used for TPM are artificial.

Each gene has a baseline level and residual variation. About 30% of unnamed
genes are barely expressed, and the differential expression count filter removes
some of them. Counts are Poisson draws from each sample's library size, which
averages about 30 million reads. The named patterns follow public descriptions of myeloma
biology, but **all magnitudes and frequencies are illustrative**. No values
are fitted to patient data.

| Pattern | Effect in the fixtures |
| --- | --- |
| Plasma-cell identity and housekeeping | SDC1, CD38, TNFRSF17, XBP1, MZB1, JCHAIN, B2M, ACTB, and ribosomal genes are highly expressed |
| Clonal immunoglobulin | Each sample has one dominant light chain (kappa 63%, lambda 37%) and, in most samples, one dominant heavy chain; immunoglobulin reads total about 30% |
| Tumor purity | Lower purity raises myeloid, erythroid, and T-cell genes (LYZ, S100A8/9, HBB, CD3E, and others) |
| Proliferation | A continuous score shifts MKI67, TOP2A, CCNB1, and about 170 other genes together |
| Sex | XIST is expressed in female samples; RPS4Y1, DDX3Y, and other Y genes in male samples |
| t(4;14) | **NSD2 up about 7-fold in every carrier**; FGFR3 strongly up in 72% of carriers; CCND2 and MAF up, CCND1 down; 350 secondary genes shift modestly |
| t(14;16), t(14;20) | MAF or MAFB up, with CCND2, ITGB7 (and CX3CR1 for MAF); shared 200-gene secondary program |
| Copy number | 1q gain raises 1q genes about 1.5-fold; del(17p) and del(1p32) lower genes in those regions; biallelic del(1p32) silences CDKN2C and FAF1 |

Translocation and copy-number states come from each case's CGS markers, so a
cohort filtered on `t(4;14): Present` contains the overexpressing samples.
NSD2 and FGFR3 are excluded from the original latent groups so that t(4;14)
drives them. The secondary program genes are chosen at random for each seed;
they demonstrate the scale of a real comparison, not specific targets.
Effects vary between patients as clones do: secondary programs and driver
shifts (except NSD2) scale per patient, and copy-number shifts scale with a
clonal fraction between 50% and 100%.

Sources for the named patterns: t(4;14) increases FGFR3 expression
([Chesi et al., 1997](https://pubmed.ncbi.nlm.nih.gov/9207791/)) and
dysregulates NSD2/MMSET ([Chesi et al., 1998](https://pubmed.ncbi.nlm.nih.gov/9787135/));
NSD2 transcripts are overexpressed in all t(4;14) cases
([Keats et al., 2005](https://pubmed.ncbi.nlm.nih.gov/15677557/)), while a
subset lacks FGFR3 expression ([Santra et al., 2003](https://pubmed.ncbi.nlm.nih.gov/12433679/)).
MAF is overexpressed in about half of myelomas, including many without a MAF
translocation, and its targets include CCND2 and ITGB7
([Hurt et al., 2004](https://pubmed.ncbi.nlm.nih.gov/14998494/));
translocation groups differ in D-type cyclin expression
([Bergsagel et al., 2005](https://pubmed.ncbi.nlm.nih.gov/15755896/)).

### Comparing t(4;14) in differential expression

Use **t(4;14) translocation** in ProteinPaint, or Virtual Lab cohorts filtered on
the CGS marker `t(4;14): Present` versus `t(4;14): Absent`. NSD2 is higher in
the **Present** group. ProteinPaint plots log2(group 2 / group 1). With
**Present vs others** or Present as group 1, NSD2 and FGFR3 appear at the
left (negative); put Absent first to show them on the right. Only the original
36 genes have gene models, protein tracks, and single-cell values. Other genes
appear in bulk expression and differential expression.

## Reference annotations

`synthetic/public-proteins.json` records the UniProt accession, source URL, and
protein sequence for each expression gene. Sequences were retrieved on
2026-10-28. Attribution: **The UniProt Consortium**, [UniProt](https://www.uniprot.org/),
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), per its
[data license](https://www.uniprot.org/help/license).

Reference proteins are retained unchanged. Coding DNA is a deterministic
back-translation; chromosome coordinates, single-exon models, and flanking DNA
are artificial. They are **not GRCh38 coordinates**, although the frontend uses
the `hg38` dataset key. Protein-change labels must not be interpreted as real
genomic HGVS annotations.

Public variant examples:

| Genes | Examples and source |
| --- | --- |
| KRAS, NRAS, BRAF | RAS codons 12, 13, 61 and BRAF V600/D594; [myeloma sequencing study](https://pmc.ncbi.nlm.nih.gov/articles/PMC6097138/) |
| TP53 | R175, Y220, R248, R273, R282; [TP53 hotspot study](https://pubmed.ncbi.nlm.nih.gov/41595714/) |
| DIS3 | S477R, G766R, R780K; [functional myeloma study](https://pubmed.ncbi.nlm.nih.gov/24150935/) |
| MYC | T58A and T58I; [lymphoma study](https://pmc.ncbi.nlm.nih.gov/articles/PMC8657136/) |

MYC coding examples come from lymphoma research, not common myeloma coding
hotspots. Its reference isoform is UniProt P01106-1, which uses the conventional
T58 numbering; KRAS uses the 188-amino-acid P01116-2 isoform.

The six mutation-panel proteins also include public UniProt feature annotations
(retrieved 2026-10-29), projected to the selected isoforms. MYC's bHLH feature
therefore uses positions 354–406 in P01106-1. The KRAS/NRAS Ras-domain interval
comes from [Pfam PF00071 through InterPro](https://www.ebi.ac.uk/interpro/entry/pfam/PF00071/),
whose data are [CC0](https://interpro-documentation.readthedocs.io/en/latest/license.html).
Each stored feature records its source and original interval; KRAS's conserved
core maps without coordinate-changing insertions or deletions.

ProteinPaint shows these feature bands and a labeled reference-sequence
backdrop at the full-protein scale. Amino-acid letters appear once zoomed far
enough to fit; use **In** above the track. This preserves the native viewer's
behavior on smaller screens without leaving a blank protein bar.
