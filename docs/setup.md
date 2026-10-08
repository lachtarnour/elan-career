# Setup and configuration

[Back to the README](../README.md)

Run the installation commands in the README first. The instructions below configure a development checkout; the packaged app uses its own runtime directory.

## Configure a profile

From the repository root, create your configuration and private profile:

```bash
cp .env.example .env
mkdir -p smartapply/profile/data
cp smartapply/profile/mock_profile/*.json smartapply/profile/data/
```

Edit the copied profile files with your experience, skills, and preferences. The supplied example defines the file format and contains placeholder data.

## Providers

Bulk offer analysis is available on the dedicated **Analyse** page. See
[Batch analysis](batch-analysis.md) for progress tracking, delays and recovery.

Set credentials in `.env` for the providers you use:

| Setting | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | AI analysis, document generation, and OpenAI embeddings |
| `FRANCETRAVAIL_CLIENT_ID`, `FRANCETRAVAIL_CLIENT_SECRET` | France Travail API access |
| `APIFY_TOKEN` | LinkedIn collection through Apify |
| `SERPAPI_API_KEY` | Google Jobs collection |
| `WTTJ_COOKIE` | Authenticated Welcome to the Jungle feed |
| `EMBEDDINGS_PROVIDER` | `openai`, `local`, or `mock` |

See [`.env.example`](../.env.example) for defaults and the [source guides](sources/README.md) for connector-specific setup.

To explore without an AI API key, use:

```dotenv
LLM_PROVIDER=mock
EMBEDDINGS_PROVIDER=mock
```

Mock providers return deterministic responses. They do not replace live job sources; add offers manually or configure a source to collect real listings.

## Run from source

The desktop launcher changes its working directory to the application runtime. Use absolute paths so the desktop app and CLI share the same development data. Run this block from the repository root in each new terminal session:

```bash
export ELAN_ENV_FILE="$PWD/.env"
export DATABASE_URL="sqlite:///$PWD/data/smartapply.db"
export PROFILE_DIR="$PWD/smartapply/profile/data"
export OUTPUT_DIR="$PWD/data/output"
export CACHE_DIR="$PWD/data/cache"

make init-db
make run-desktop
```

Environment variables take precedence over `.env`. Keep `.env`, the private profile, and generated data out of version control; these paths are already excluded by `.gitignore`.

## Build the macOS app

With the project `.env` and private profile configured:

```bash
make build
open dist/Elan.app
```

Quit Élan before running `make build`. The build stops with the active process ID
if `dist/Elan.app` is still open. PyInstaller loads Python modules lazily from its
embedded compressed archive; replacing that archive while the process is running
can otherwise make a later action fail with a zlib `incorrect header check`.

The first build provisions the runtime under `~/Library/Application Support/Elan`, copying the project configuration, profile, and database when available. Later builds preserve existing runtime data. When launching the packaged app, use its runtime configuration; the development environment overrides above are intended for source runs.

The build generates the `.icns` icon from the bundled SVG and runs the packaged
executable with a temporary `ELAN_HOME`, mock providers, the public mock profile,
and a temporary SQLite database. This smoke test never opens or modifies the
active user database. PyInstaller also uses a temporary configuration directory,
so a protected global cache cannot block the build.

Source credentials and collection options are reread from the active configuration
for each connection check and new search. Open **Réglages → Configuration** to
find that file (normally `~/Library/Application Support/Elan/.env`). Editing the
project `.env` and rebuilding does not overwrite this existing runtime file.
Connection checks run every minute, on entering Recherche or Réglages, and via
**Actualiser**. A successful check re-enables a recovered source automatically;
refreshing preserves source selections and the last checked availability while
the check runs. These checks do not launch paid searches. Source changes need no
rebuild or restart; database, profile paths, and model settings remain pinned to
the running application. Explicit process environment variables still take
precedence over the configuration file.

### Synchronize the project and app profiles

The two profile directories are not automatically synchronized by a rebuild.
From the repository root, compare them and then copy the project profile into Élan:

```bash
make check-profile-sync
make sync-profile
```

To copy changes made in Élan back into the project instead:

```bash
make check-profile-sync DIRECTION=from-app
make sync-profile DIRECTION=from-app
```

The source wins for each copied JSON file: lists such as projects and skills are
replaced, not merged. Choose the direction that contains the data you want to keep.
The command validates the source and resulting profile before writing, skips
unchanged JSON, and backs up the destination's profile files under
`<destination>/.sync-backups/<timestamp>/`. Optional files absent from the source
remain in the destination. API credentials, the database and generated documents
are not copied. A failed write rolls back the files already replaced.

The app destination defaults to `ELAN_HOME/profile`, or
`~/Library/Application Support/Elan/profile` when `ELAN_HOME` is unset; development
overrides of `PROFILE_DIR` do not redirect this command. For custom directories,
use `.venv/bin/elan sync-profile --project-profile /path/to/project/profile
--app-profile /path/to/app/profile --dry-run`, then omit `--dry-run` to apply.
No rebuild is needed after copying profile data. Existing CVs are not regenerated.

### Cache freshness after edits

The next desktop operation reloads changed profile JSON files (the in-memory
cache tracks each file's modification time and size). A running batch keeps the
profile it started with. Embedding cache keys include the model and the exact
text being embedded: changed profile or project text is computed on its next
use, while unchanged text remains reusable. `make refresh-embeddings` can
precompute the current profile and project vectors without clearing other entries.

Prompt builders read their system and user templates for each request, including
the shared summary rules. There is no process-wide snapshot of the prompt text.
The response-cache key includes the rendered system/user messages, model,
temperature and complete JSON schema, so changed instructions or supplied profile
content cannot reuse a response for the previous request.

New job analyses use two separate calls to `OPENAI_MODEL_CHEAP` (currently
`gpt-5.4-mini`). `job_offer_analysis` extracts offer facts and supporting excerpts
without any candidate profile. `job_fit_assessment` receives those facts and the
candidate's declared skills, all experiences and projects, education, languages
and actual preferences. Its strict output schema permits only the fit score,
matching reasons and candidate risks; it cannot overwrite extracted requirements.
The common rubric and score bands are in `job_fit/system.j2`; the model still
assigns a global score, without a new arithmetic weighting formula.

Extraction uncertainties remain visible alongside candidate risks. Unverifiable
supporting excerpts are excluded and reported as an offer-quality concern.
The existing exact-response cache stores the two calls separately, so an explicit
retry or rerun can reuse an unchanged offer extraction while reassessing a changed
profile. Reuse depends on matching inputs, instructions, model and the existing
cache lifetime. Both stages must succeed before the job is marked analyzed.
Already analyzed offers and existing application documents are not recalculated
automatically; historical analysis records remain compatible.

The standalone app reads the templates packaged in its own bundle. Editing the
repository's Python or prompt files therefore requires a rebuild and relaunch of
the app. Profile data remains in Application Support and is not overwritten by a
rebuild. Previously saved analyses, scores, CVs and letters are historical outputs,
not automatically refreshed caches: rerun the corresponding operation to update
them. None of these changes requires deleting the database or all caches.

## Adapt the profile and matching rules

Élan is primarily designed for AI Engineer profiles seeking employment. Using it for another role or field requires adapting the profile data, search role families, and static matching rules, including changes to the source code. The repository publishes a safe example profile; review the files below for your target role, country, and experience level.

### Profile files

[`smartapply/profile/mock_profile/`](../smartapply/profile/mock_profile/) is the published reference profile. It documents the expected JSON structure and contains no private candidate data.

Edit the files in `smartapply/profile/data/`:

| File | Content |
|---|---|
| `identity.json` | Name, contact details, title, location, and summary |
| `preferences.json` | Target roles, accepted contracts, remote policies, languages, domains, and deal-breakers |
| `skills.json` | Skills, matching keywords, evidence, and allowed claims |
| `experiences.json` | Work history and verifiable achievements |
| `projects.json`, `education.json`, `languages.json`, `certificates.json` | Optional supporting information |
| `style_guide.json`, `template_style.json` | Writing and document presentation preferences |

Set `PROFILE_DIR` to the absolute path of your private profile. Keep real personal information out of version control; `mock_profile` is the public template.

### Matching rules

The profile controls the main user preferences, but several deterministic vocabularies and safety gates are hard-coded for predictable filtering. Review these files when the default behavior does not match your domain:

| File | What to change |
|---|---|
| [`smartapply/filtering/rules.py`](../smartapply/filtering/rules.py) | Profile-derived inputs shared by the gates: target roles, accepted contracts, languages, experience ceiling, and seniority title markers |
| [`smartapply/pipeline/ingest/role_families.py`](../smartapply/pipeline/ingest/role_families.py) | Search role families and bilingual aliases used to expand search queries |
| [`smartapply/filtering/hard_reject/engine.py`](../smartapply/filtering/hard_reject/engine.py) | Coordinator for independent deterministic gates; survivors are routed to `pending_relevance` |
| [`smartapply/filtering/hard_reject/role_families.py`](../smartapply/filtering/hard_reject/role_families.py) | Explicit occupation families and their `hard reject` or `defer to relevance` policy |
| [`smartapply/filtering/experience_requirements.py`](../smartapply/filtering/experience_requirements.py) | Policy-neutral parsing of mandatory, optional, structured, and conflicting experience evidence, shared by rejection and ranking |
| [`smartapply/filtering/seniority.py`](../smartapply/filtering/seniority.py) | Policy-neutral assessment of explicit seniority, management duties, and ambiguous titles, shared by rejection and ranking |
| [`smartapply/filtering/hard_reject/`](../smartapply/filtering/hard_reject/) | Contract, qualification, language, and role gates plus thin experience/seniority adapters; this package can only return `rejected` or `pending_relevance` and never owns a score |
| [`smartapply/filtering/relevance.py`](../smartapply/filtering/relevance.py) | Bilingual role concepts, title patterns, technical concepts, and off-target role patterns |
| [`smartapply/filtering/role_signals.py`](../smartapply/filtering/role_signals.py) | Domain, analytics, engineering, and off-target signal vocabularies |
| [`smartapply/filtering/location_signals.py`](../smartapply/filtering/location_signals.py) | Location markers and foreign-location detection patterns |

Recommended order:

1. Update `preferences.json` first.
2. Adjust the search role families and shared inputs in `rules.py`.
3. Change a deterministic rejection policy only in its dedicated `hard_reject` gate and add gate-level tests.
4. Run the test suite after every filter change.

```bash
make test-fast
```

For changes to the deterministic rejection rules, also run both frozen audit
guards. The first protects the reviewed rejection cohort. The second evaluates
every offer that was previously classified `relevant` or `uncertain`:

```bash
.venv/bin/python scripts/replay_hard_reject_audit.py --strict \
  --output /private/tmp/hard-reject-replay.json
.venv/bin/python scripts/replay_hard_reject_positive_control.py --strict \
  --output /private/tmp/hard-reject-positive-control.json
```

For the former `relevant` cohort, the positive control uses the reviewed
ID-level decisions in its manifest: an unreviewed rejection and every reviewed
false or ambiguous rejection fail the guard. The former `uncertain` cohort is
also protected by its exhaustively reviewed rejected-ID allowlist and by an
explicit aggregate ceiling. `--strict` fails if a rules change rejects a former
`uncertain` offer outside that allowlist or raises the rejected count above the
ceiling. An allowed rejection may be released without failure. Any new rejected
ID must be reviewed before the manifest changes. Neither replay writes to the
frozen database.

The current deterministic filter stores only `rejected` and
`pending_relevance`. Relevance is evaluated separately after this first filter.
The Nouvelle class selector therefore offers only `Rejeté` and `À classer`,
plus `Toutes les classes`.

Ranking and analysis also validate the persisted deterministic decision, rather
than relying on phase timestamps. Imported legacy labels are re-evaluated before
ranking. Rejected offers cannot enter automatic ranking or LLM analysis; an
explicit manual keep remains recorded with the blocker it overrides. Repairing
protected history changes filter metadata without erasing prior applications,
analyses, scores or manual selections.

An offer explicitly added by the user (pasted text, structured manual input or
manual URL import) bypasses automatic rejection gates. This choice is stored as
`manual_filter_bypass` and persists through later ranking, analysis and metadata
repairs. The audit records the gates as skipped, not successfully evaluated.
Fit analysis can still report risks; duplicates, sent applications, active
reservations and manual archives remain protected. A source named `manual`
alone is not an exemption. Existing archived offers are not automatically reopened.

To migrate persisted legacy `uncertain` decisions, run the dedicated command
against the active database. It previews changes by default; add `--apply` to
create a backup and apply exactly the reviewed plan atomically:

```bash
.venv/bin/python -m scripts.migrate_legacy_filter \
  --database "/absolute/path/to/smartapply.db" \
  --review-manifest audit_manifests/classification-audit-20261005/positive-control-decisions.json \
  --output /private/tmp/legacy-filter-plan.json
```

Review the plan, then apply it. The command refuses any change to the scope,
offer inputs, decisions, or audit manifest:

```bash
.venv/bin/python -m scripts.migrate_legacy_filter \
  --database "/absolute/path/to/smartapply.db" \
  --review-manifest audit_manifests/classification-audit-20261005/positive-control-decisions.json \
  --apply --expected-plan /private/tmp/legacy-filter-plan.json
```

The migration preserves scores, analyses, documents, applications, and manual
or duplicate workflow decisions. Only untouched offers with a reviewed blocker
are automatically archived. Offers that pass the first filter become
`pending_relevance`; this does not start an LLM analysis. Migrating legacy
`relevant` decisions additionally requires the explicit `--include-relevant`
option. Repeating the preview after migration should report zero planned rows.

### CV template and writing style

The CV content comes from your profile. Presentation is configured separately for each export format:

Experience descriptions are static: every experience and bullet from `experiences.json`
is inserted verbatim, in profile order, for both CV-only and CV-plus-letter generation.
The LLM receives these facts as read-only context; its response schema has no experience
field. It personalizes the headline, summary, skills, project and certificate selections,
and writes the letter when requested. Generated-text length limits do not shorten
approved experience descriptions. To change an experience, edit the source profile
explicitly and generate a new CV; existing documents and history are retained.

Application generation reuses the stored offer analysis and uses three sequential
LLM stages. The existing Skills and project reconstruction rules remain in place:

1. Generate the CV personalization with the configured smart model, apply the
   reconstruction rules, and validate the assembled CV. The same call chooses
   zero to two relevant certificates; HTML/PDF and DOCX use only that finalized
   selection. See [project and certificate selection](project-selection.md).
2. Write a motivation-first letter with the smart model: explain why the
   candidate wants to join this organization and work on this role's priorities.
   The stored analysis's `company_context` and `offer_interest_points` appear in
   an explicit `motivation_context` block, checked against the full offer. They
   are derived context, not independent proof of company facts or the candidate's
   personal history. Missing company details lead to a focus on the stated
   mission, not invented culture, reputation or values. No new analysis call is
   needed, including when these fields are empty in an older analysis.
   Recruiter or source labels are not substituted for the hiring company: when
   the analysis cannot confirm that entity, documents use `Entreprise non
   communiquée` (or `Company not disclosed`) instead of guessing from the
   imported row.
   The finalized CV, declared Skills catalogue and experience/selected-project
   facts are references. Relevant projects or experiences personalize motivation
   when useful, with no separate quota on example count or detail beyond the
   letter's overall length; the letter is not a CV or technical-skills recap.
   Shared `letter_style.audience` and `letter_style.accessibility` instructions
   ask both writer and reviewer for language accessible to a non-specialist
   recruiter. Keep useful technical precision and explain its relevance when
   needed, without a glossary requirement or restrictions on example count or
   detail. Demonstrated readability problems are advisory style findings.
   Declared competence does not require a project citation. A claim that a tool
   was used at a specific employer or in a specific project does require that
   attribution in the corresponding source. Letter voice is first-person and
   separate from the CV style guide.
3. Independently review the CV title, summary and letter with the configured cheap model
   against these sources and an explicit reference date. The reviewer distinguishes
   declared competence, past realizations and prospective contributions; it does
   not infer past use of a tool from a competence or intention. Findings must
   identify an exact passage, a reason, valid source references and a category.
   Factual and language defects are blocking; style and structure suggestions
   remain advisory. Existing style hints are not evidence about the candidate.

The reviewer accepts conservative paraphrases and ordinary present motivation
without requiring biographical proof. It still checks claims of specific past
work, personal history and company facts, including unsupported amplification
of responsibility, scale, duration or impact. Generic praise or a skills recap
can receive an advisory style finding when it materially displaces motivation;
the reviewer cannot impose a preferred example or require exhaustive skills
coverage. The old lexical requirements to mention CV evidence and the regex
classifier conflating learning interest with self-deprecation are removed.
Language and source interpretation remain with the LLM, without offer-specific
word lists or exceptions.

Review findings retain non-empty source references. A contradiction or amplified
fact cites its specific record. For a candidate claim with no relevant specific
record, `candidate:provided_evidence` identifies all candidate sources actually
exposed to the reviewer through `member_source_ids`. This aggregate excludes
generated document text, offer/analysis content and unselected projects. It
supports an observation of missing support in the supplied evidence, not a claim
that the statement is false in real life or absent from the full private profile.
Unsupported company facts use the offer source instead of this candidate scope.

The CV title is checked as an English professional headline grounded in the
candidate's identity, declarations and relevant facts. A reasonable professional
positioning does not require an identical historical job title; however, the
offer alone cannot supply seniority, specialization or credentials. The reviewer
checks coherence with the summary and the letter's candidate claims without
requiring identical wording or confusing an application subject with an existing
role. The generated title is not independent evidence of those claims.

CV drafting and review share the summary editorial rules from
`llm/prompts/templates/cv_adaptation/summary_rules.j2`. They require concrete
positioning relevant to the offer, avoid a stack inventory and retain the
distinction between declared competence and specific realizations. The reviewer
receives these rules in `summary_style.editorial_rules` for both review attempts,
including corrections. Summary-specific presentation rules take precedence over
general guidance to display the technical stack; they do not apply to the letter.

Generated CV text, letter writing and review also share
`llm/prompts/templates/shared/recruiter_text_rules.j2`, supplied to the latter
two through `recruiter_text_style` (`style:recruiter_text`). Evidence metadata
guides claim strength internally; it must not become a qualifier such as
"verified work" or "source-grounded experience" in recruiter-facing prose.
Actual technical uses such as "verified model outputs", "source-grounded
answers" or "self-reported clinical data" remain legitimate.

A narrow Python check flags known metadata identifiers and experience qualifiers
in the generated title, summary, letter subject and body. These
`internal_evidence_metadata` warnings reach the reviewer and are recomputed on
the returned text, so a repaired phrase does not leave a stale warning. They are
editorial hints, not factual errors or automatic rewrites; unknown paraphrases
and ambiguous contexts remain the reviewer's responsibility. Static source
descriptions and Skills are not scanned or edited by this check.

The reviewer may propose exact passage replacements in the CV title, summary, subject or
letter body; the controller applies these local patches without semantic regex
rewrites or full-document regeneration. Experiences, Skills and
selected projects are read-only. One additional review checks a proposed revision
without permission to revise again. An unresolved blocking defect, invalid patch
or failed confirmation prevents the unconfirmed revision from replacing the
original. Advisory-only confirmation retains a valid correction. Objective
validators still enforce hard constraints, even when the reviewer reports no
blocking issue; optional style preferences cannot override these controls.
If an optional advisory patch fails hard objective checks or its confirmation
cannot complete, an original already reviewed without blocking defects remains
usable, with the discarded patch and failure retained in the trace. A demonstrated
factual or language defect found on confirmation still requires attention.
Word, paragraph and summary-length counts remain advisory before and after a
correction; a length deviation cannot silently undo a confirmed factual fix.

Unavailable reviews, unresolved factual or language defects, and objective
validation errors leave the documents available with `quality_rejected` status.
Style and structure suggestions are exposed as warnings without blocking an
otherwise valid application. The review trace, including model, reference date,
findings and decision, is retained in `generated_documents` as `document_review`.
This checks source consistency, not the real-world truth of the candidate's
declarations. Review instructions are general source and language rules; they do
not contain offer-specific examples, vocabulary blacklists or exceptions derived
from an evaluation sample.

The normal path has three LLM calls; a confirmed correction has four (excluding
provider-level transport retries), with at most two calls to the cheap reviewer.
There is no extra planner call or automatic escalation to another review model.
Actual cost depends on the configured models, token usage and cache hits; routing
the review to a cheaper model does not guarantee equal quality or a fixed saving.
The previous joint `application_draft` call and one-defect letter repair are no
longer used by the application pipeline.
CV-only generation remains available. No historical document or stored analysis
is migrated automatically when this generation code changes.

| What to customize | Where to edit |
| --- | --- |
| HTML/PDF layout, colors, typography, margins, and section order | [`smartapply/cv/templates/cv.html.j2`](../smartapply/cv/templates/cv.html.j2), using HTML/CSS and preserving the Jinja variables |
| DOCX fonts, colors, and text sizes | `template_style.json` in your private profile; see the [example settings](../smartapply/profile/mock_profile/template_style.json) |
| DOCX structure, margins, and section order | [`smartapply/cv/docx_generator.py`](../smartapply/cv/docx_generator.py) |
| Writing tone, instructions, and length limits | `style_guide.json` in your private profile; see the [example settings](../smartapply/profile/mock_profile/style_guide.json) |

`template_style.json` applies to DOCX output; HTML/PDF styling lives in the HTML template. After changing source templates or static filters, restart the source app or rebuild the packaged app with `make build`. Generate a sample application and review the exported CV before using it.

## Development

```bash
make check
.venv/bin/elan --help
```

The test suite uses fictional fixtures and a temporary runtime, with no private profile or API credentials required. Install the desktop dependencies to run the Qt interface tests.

See [Contributing](../CONTRIBUTING.md) for the project structure and development workflow, the [visual verification guide](desktop-design.md) for UI checks, and the [source documentation](sources/README.md) for connectors.
