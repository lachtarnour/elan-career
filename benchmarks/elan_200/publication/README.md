# Élan benchmarks — public evidence index

This release records seven historical Batch runs migrated into the current benchmark layout. It is a metadata index: it contains no job posting text, candidate profile, prompts, response bodies, provider account IDs, or local file paths. Hashes in `runs.json` let an authorized holder check the original evidence files.

The [benchmark overview](benchmark_overview.html) summarizes all four stages of the pipeline, including newer extraction comparisons and a separate CV selection benchmark. It distinguishes provider completion from semantic review.

## Two benchmark setups

| Corpus | Offers | Scope |
| --- | ---: | --- |
| `corpus_1` | 200 | The intact existing corpus used by the current benchmarks, including the 200-offer extraction V2/V3 comparisons. It contains 100 historical and 100 newer offers. |
| `corpus_2` | 100 | The lost historical extraction setup. Its exact effective offer bodies and rendered prompts were recovered from the saved native Batch requests for two old GPT-5.4 mini extraction runs. |

The original frozen SQLite database for `corpus_2` is missing. Its other original database fields have not been recovered. The 100 extraction prompts match historical rows within `corpus_1` exactly, but the two setups and their identifiers remain separate. The historical 100 rows inside `corpus_1` came from a later user database snapshot; they are not a copy of that missing SQLite database. The newer 100 rows came from a verified frozen `benchmark2` SQLite database.

The two imported extraction runs use `corpus_2` and its `EB1-*` identifiers. Two scope screening runs and three fit runs use historical identifiers inside `corpus_1`. The fit runs cover subset B (80 of those 100 historical identifiers); exact equivalence between their original fit input text and the reconstructed offer descriptions is unverified.

| Evidence | Requests | Native responses | Other outcomes |
| --- | ---: | ---: | ---: |
| Scope screening: GPT-6 Luna and Claude Haiku | 100 each | 100 each | 0 |
| Offer extraction: GPT-5.4 mini medium and high | 100 each | 100 each | 0 |
| Fit: GPT-5.4 mini and Claude Haiku | 80 each | 80 each | 0 |
| Fit: GPT-6 Luna | 80 | 76 | 4 not completed after Batch cancellation |

The Anthropic fit request files were reconstructed and matched to SHA-256 hashes recorded in the original run state. Their native result file was recovered. A native provider response does not establish schema validity, factual accuracy, or benchmark quality. No new provider request or semantic evaluation was performed during migration; this release contains no score or model ranking.

## Files and verification

- `corpora.json`: setup identity, provenance limits, counts, and corpus hashes.
- `runs.json`: sent model settings, Batch status, outcome counts, and hashes of native evidence held locally.
- `coverage.jsonl`: canonical offer ID and provider outcome for each of 640 offer-run pairs.
- `benchmark_overview.html`: readable summary of all four stages, with separate corpus and review limits.
- `checksums.json`: hashes of the three public data files and the HTML overview.

Run `python3 verify.py` from this folder. It needs no API access or private data. The local benchmark has a separate verifier that compares the corpus and native evidence with independent backups.

This index alone does not enable reproduction or semantic comparison. That requires the underlying postings, candidate profile where applicable, exact requests, native outputs, and a documented evaluation protocol.
