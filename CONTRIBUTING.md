# Contributing

## Set up and check your changes

Use Python 3.10+ on macOS 13+. From the repository root:

```bash
make install-desktop
make check
```

If several Python versions are installed, select one with `make install-desktop PY=python3.11`.

`make check` runs Ruff and the regression suite. Tests use a temporary runtime, a [fictional profile](tests/fixtures/README.md), and mock AI providers. They need no private profile or API credentials.

To run the app locally, follow the [setup guide](docs/setup.md). For focused changes, run a single module with `.venv/bin/pytest tests/test_profile.py`. GitHub Actions runs the same checks on macOS with Python 3.11 and verifies that the Python package includes all application files.

## Find the right module

| Location | Responsibility |
| --- | --- |
| `smartapply/desktop/` | Qt Quick interface and desktop services |
| `smartapply/scrapers/`, `smartapply/offers/` | Source connectors and offer normalization |
| `smartapply/filtering/`, `smartapply/ranking/`, `smartapply/dedup/` | Relevance, scoring, and duplicate detection |
| `smartapply/pipeline/`, `smartapply/jobsearch/` | Application workflow |
| `smartapply/profile/`, `smartapply/llm/`, `smartapply/cv/` | Candidate data, prompts, and document generation |
| `smartapply/database/` | Local storage |
| `tests/` | Application regression tests and interface checks |
| `docs/` | Setup, source connectors, and interface guidelines |

## Submit a change

- Keep each pull request focused. Explain the problem, the resulting behavior, and the checks you ran.
- Add regression coverage for changed behavior. Use deterministic fixtures and mock external services.
- For interface changes, include screenshots and follow the [visual verification guide](docs/desktop-design.md).
- Include only application code, necessary resources, behavior-focused tests, and maintained documentation. Private profiles, credentials, databases, generated documents, build outputs, audits, and local tools stay local and are ignored by Git. Keep test outputs in `artifacts/` or a temporary directory.

For bug reports, include reproduction steps, expected and actual behavior, macOS and Python versions, and relevant logs with credentials and personal data removed.
