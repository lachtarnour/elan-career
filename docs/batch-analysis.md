# Batch analysis

The **Analyse** page sits after **Doublons**. Its offer table follows the
**Offres → Nouvelle** layout: sortable company, title, experience, location,
publication date and analysis state columns, adapting to the available width.
**À analyser** opens first. Select individual rows or all displayed rows, then
use **Analyse par Batch** or **Analyse immédiate** for precisely that selection.
**Offres de l’analyse** keeps every offer in the selected run visible and searchable, with its
individual progress and a link to the full offer. Several Batch runs can progress at once on distinct offers,
alongside an immediate analysis. The run selector keeps their progress, retry
and cancellation independent. Offers must pass deterministic rejection,
be active and unanalysed, and have no application or unresolved duplicate review
in their group. Numeric ranking and the historical Top 25 do not limit analysis.
The two offer classes remain **Rejeté** and **À classer**.

Analysis uses the existing cheap OpenAI model and the same two structured calls:

1. **Extraction**: offer facts and requirements, independent of the profile.
2. **Évaluation**: comparison with the candidate profile.

In Économique mode, each phase is submitted through OpenAI Batch, with a completion window of up to
24 hours per phase. The second phase starts automatically after the first phase's
successful outputs have been validated. Batch processing has a 50% discount
relative to the corresponding standard token rates. It requires
`LLM_PROVIDER=openai` and a configured `OPENAI_API_KEY`.
Immédiate mode runs the same two calls per offer through the standard endpoint
and publishes each complete analysis without waiting for a Batch window.

## Progress and recovery

The upper workspace places the offer table on the left and the offer
reading pane on the right, with an adjustable divider. Clicking a row opens its full
description, classification reason and text zoom directly on this page;
checkboxes independently select offers for analysis. The description component
is shared with **Offres**, and only the opened offer body is read from SQLite.
Analysis preview reads use their own state and latest-request guard, preserving
the dossier and note drafts in **Offres**.
A transparent tracking area, separated by a single fine line below, follows the selected run on one three-step
timeline and shows its status, Batch count and analysed offer count. **Voir les détails** reveals the size and progress of
each Batch by phase, the last update and individual failures. Details scroll
independently and never hide the table or its bottom action bar. Selection,
search and sorting survive passive updates and opening or closing details.
The run selector, retry and stop actions remain directly accessible.
Customer messages explain service failures without displaying API permissions
or obsolete errors on completed or cancelled runs. Provider counters count requests, which can differ
from offer counts because exact copies share a request, cache hits need no request
and retries add attempts. There is no reliable queue position or exact finish time.

Élan checks every two minutes while a run is active, independently of the visible
page. **Actualiser** checks immediately. OpenAI continues submitted batches while
Élan is closed; local importing and launching the next phase resume when Élan
reopens. No extra background service is installed.

**Reprendre** sends only failed requests. Valid extractions from that run survive
an interrupted evaluation. A cancellation preserves already imported analyses,
requests cancellation from OpenAI and prevents another phase from being sent.
Partial successful provider results remain usable after expiration or cancellation.
The preparation screen also has an **Arrêter** action: a cancellation before a
provider submission creates no Batch; an already submitted Batch receives a
cancellation request and remains visible until OpenAI confirms its final state.

The current exact response cache and its retention stay unchanged. Only complete
analyses enter `job_analyses`; a Batch execution journal records intermediate
results for recovery. Repeated polling imports neither results nor usage twice.
Offer, profile and group history are checked before submission, before evaluation
and before publication. Obsolete results are skipped and explained on the page.

If a submission's outcome is uncertain, Élan searches OpenAI using the journaled
metadata instead of resending it. An unresolved submission stays visibly marked
**Envoi à vérifier**. Repeated checks cannot create another paid batch.

## Code responsibilities

| Module | Responsibility |
| --- | --- |
| `llm/batch.py` | JSONL requests, SDK transport, provider states and output parsing |
| `database/analysis_batches.py` | Durable runs, items and submission journal |
| `pipeline/process/analysis_admission.py` | Shared deterministic admission and history safeguards |
| `pipeline/batch_analysis/coordinator.py` | Two-phase transitions, recovery, retry and cancellation |
| `pipeline/batch_analysis/results.py` | Existing prompt/schema/cache contracts and atomic result import |
| `pipeline/batch_analysis/presentation.py` | Lightweight progress and eligible offer list |
| `desktop/analysis_services.py` | Desktop facade |
| `desktop/analysis_controller.py` | Worker scheduling, polling and completion notification |
| `desktop/analysis_offer_controller.py` | Independent, asynchronous offer preview |
| `desktop/qml/components/AnalysisOfferDetail.qml` | Offer reading pane and zoom |
| `desktop/qml/components/OfferDescription.qml` | Shared description rendering in Offres and Analyse |
| `desktop/qml/pages/AnalysisPage.qml` | Dedicated analysis workspace |

See the [OpenAI Batch guide](https://developers.openai.com/api/docs/guides/batch)
for provider behavior and limits.

## Interrupted analyses

A validated extraction awaiting evaluation is stored in `analysis_checkpoints`.
Both execution modes resume it only when the model, prompts, normalized source
input and schema match exactly. Cache expiry does not erase this checkpoint.
A complete published analysis removes it. Earlier Batch journals can be recovered
lazily through their persisted extraction request key.

Cancellation applies to the selected launched Batch parcours. While provider
confirmation is pending, that parcours remains visible and reserves its offers.
Once confirmed, it disappears from the page; the execution journal and received
results remain available for safe resumption. Other parcours are unaffected.
Batch monitoring and cancellation remain available during immediate analysis.
