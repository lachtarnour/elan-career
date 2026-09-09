# Test fixtures

`profile/` is a fictional candidate profile used by the regression suite. Names, contact details, employers, and academic institutions are examples. Evidence and IDs exercise document grounding, profile validation, skill selection, and prompt caching. These files are test data, not an application profile to publish or reuse for real applications.

All experience descriptions, achievements, and metrics must also be invented: replacing names alone does not make a real profile fictional. Keep fixtures deterministic and use `example.com` for links and addresses. Tests copy files before modifying them. Company ranking and import tests generate synthetic targets without reading a personal CSV or research audit.
