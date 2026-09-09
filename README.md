# Élan

**A native macOS job-search workspace for AI Engineers.**

Find relevant roles, assess your fit, prepare tailored CVs and cover letters, and track applications in one place. Built with Python, Qt Quick, and SQLite.

Élan is primarily designed for **AI Engineer profiles**. Using it for other roles or fields requires adapting the candidate profile, search keywords, static filters, and matching rules, including changes to the source code. See [Make it your own](#make-it-your-own).

[![macOS 13+](https://img.shields.io/badge/macOS-13%2B-111017?logo=apple&logoColor=white)](#getting-started)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![MIT License](https://img.shields.io/badge/License-MIT-55bd92)](LICENSE)
[![CI](https://github.com/lachtarnour/elan-career/actions/workflows/ci.yml/badge.svg)](https://github.com/lachtarnour/elan-career/actions/workflows/ci.yml)

## Demo

45 seconds · silent · French annotations. Fictional data with simulated collection and AI responses.

https://github.com/user-attachments/assets/b357b04e-eba4-49aa-a6ed-851d0a94e167

## What it does

- **Find opportunities** across France Travail, LinkedIn, Welcome to the Jungle, and Google Jobs, or add an offer manually.
- **Review your fit** with ranked offers, match explanations, and a comparison view for suspected duplicates.
- **Prepare applications** with CVs and cover letters grounded in your profile.
- **Track progress** with application statuses, documents, and a dashboard of recent activity.
- **Review target companies** on the Entreprises page with search, priority filters, persistent checkboxes, and manual additions. [Import or synchronize your private CSV](docs/company-targeting.md) with `make sync-companies` while preserving checked records.

Profile data and application history are stored locally. Connected providers process the data needed for search and AI features. You review documents and submit applications yourself.

## Getting started

Requires **macOS 13+** and **Python 3.10+**.

```bash
git clone https://github.com/lachtarnour/elan-career.git
cd elan-career
make install-desktop
```

Follow the [setup guide](docs/setup.md) to configure your profile and providers, then launch:

```bash
make run-desktop
```

The guide also covers running with mock providers and building a standalone macOS app. Source credentials are configured separately for each [connector](docs/sources/README.md).

## Make it your own

Default filters are tuned for AI Engineer and related data/AI roles, with junior-to-mid experience. Follow these steps to adapt Élan to your profile and target position:

1. **Profile information** — Copy the [example profile](smartapply/profile/mock_profile/) into your private profile directory, following the [setup guide](docs/setup.md#configure-a-profile). Replace the identity, experience, skills, projects, and education with your own information, then set your search preferences in `preferences.json`.
2. **Static filters** — Review [filter rules](smartapply/filtering/rules.py) and [search role families](smartapply/pipeline/ingest/role_families.py) for your target roles, exclusions, and seniority. Changing your profile does not automatically update these rules; the [matching guide](docs/setup.md#matching-rules) also covers contract and location filters.
3. **CV template** — Edit [the HTML/CSS template](smartapply/cv/templates/cv.html.j2) for HTML/PDF layout, or `template_style.json` in your private profile for DOCX fonts and colors. See [CV customization](docs/setup.md#cv-template-and-writing-style) for layout and writing options.

## Development

```bash
make check
```

Tests use fictional data and mock AI providers. See [Contributing](CONTRIBUTING.md) for the project structure, development workflow, and bug reports.

[Setup](docs/setup.md) · [Source connectors](docs/sources/README.md) · [Interface guidelines](docs/desktop-design.md) · [Contributing](CONTRIBUTING.md)
