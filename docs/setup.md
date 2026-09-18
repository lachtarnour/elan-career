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

The first build provisions the runtime under `~/Library/Application Support/Elan`, copying the project configuration, profile, and database when available. Later builds preserve existing runtime data. When launching the packaged app, use its runtime configuration; the development environment overrides above are intended for source runs.

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
| [`smartapply/filtering/rules.py`](../smartapply/filtering/rules.py) | Positive and negative title keywords, hard rejects, seniority gates, description penalties, blocked contracts, and score thresholds |
| [`smartapply/pipeline/ingest/role_families.py`](../smartapply/pipeline/ingest/role_families.py) | Search role families and bilingual aliases used to expand search queries |
| [`smartapply/filtering/relevance.py`](../smartapply/filtering/relevance.py) | Bilingual role concepts, title patterns, technical concepts, and off-target role patterns |
| [`smartapply/filtering/role_signals.py`](../smartapply/filtering/role_signals.py) | Domain, analytics, engineering, and off-target signal vocabularies |
| [`smartapply/filtering/contract_signals.py`](../smartapply/filtering/contract_signals.py) | Contract markers and contextual exceptions |
| [`smartapply/filtering/seniority.py`](../smartapply/filtering/seniority.py) | Seniority and people-management patterns |
| [`smartapply/filtering/location_signals.py`](../smartapply/filtering/location_signals.py) | Location markers and foreign-location detection patterns |

Recommended order:

1. Update `preferences.json` first.
2. Adjust `rules.py` and `role_families.py` for the new target.
3. Update the deeper signal files only when you need domain-specific vocabulary or different safety gates.
4. Run the test suite after every filter change.

```bash
make test-fast
```

### CV template and writing style

The CV content comes from your profile. Presentation is configured separately for each export format:

Experience descriptions are static: every experience and bullet from `experiences.json`
is inserted verbatim, in profile order, for both CV-only and CV-plus-letter generation.
The LLM receives these facts as read-only context; its response schema has no experience
field. It personalizes the headline, summary, skills selection and project selection,
and writes the letter when requested. Generated-text length limits do not shorten
approved experience descriptions. To change an experience, edit the source profile
explicitly and generate a new CV; existing documents and history are retained.

Application generation uses three sequential LLM stages:

1. Generate the CV personalization, apply the existing Skills and project
   reconstruction rules, and validate the assembled CV.
2. Write the letter from that finalized CV, the full offer text, the complete
   declared Skills catalogue and the facts of its experiences/selected projects.
   Declared competence does not require a project citation. A claim that a tool
   was used at a specific employer or in a specific project does require that
   attribution in the corresponding source. Letter voice is first-person and
   separate from the CV style guide.
3. Independently review the summary and letter against these sources. Findings
   must identify an exact passage, a reason and valid source references. The
   reviewer can propose replacements only for the summary, subject and body;
   it cannot change the headline, experiences, Skills or selected projects.

If a revision is proposed, one additional review checks it without permission
to revise again. An unconfirmed or structurally invalid revision is discarded.
Unavailable reviews and unresolved findings leave the documents available with
the `quality_rejected` status rather than marking them ready. The review attempts
are retained in `generated_documents` as `document_review`, and actionable issues
are also exposed in the application's validation messages. This checks source
consistency, not the real-world truth of the candidate's declarations.

The normal path has three LLM calls; a confirmed correction has four (excluding
provider-level transport retries). The previous joint `application_draft` call
and one-defect letter repair are no longer used by the application pipeline.
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

The test suite uses fictional fixtures and a temporary runtime, with no private profile or API credentials required. Tokenizer tests may download a public vocabulary on their first run.

See [Contributing](../CONTRIBUTING.md) for the project structure and development workflow, the [visual verification guide](desktop-design.md) for UI checks, and the [source documentation](sources/README.md) for connectors.
