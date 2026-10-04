French administrative reference used by offer deduplication

french_communes.json is an offline snapshot retrieved on 2026-10-03 from
the French government's API Découpage administratif (geo.api.gouv.fr).
It contains commune and municipal-arrondissement codes, official names,
department and region codes, postal codes, and department names.

Sources:
https://geo.api.gouv.fr/communes?fields=nom,code,codeDepartement,codeRegion,codesPostaux&format=json
https://geo.api.gouv.fr/communes?type=arrondissement-municipal&fields=nom,code,codeDepartement,codeRegion,codesPostaux&format=json
https://geo.api.gouv.fr/departements

License: Licence Ouverte / Open Licence 2.0.
Upstream source and license documentation:
https://github.com/datagouv/api-decoupage-administratif

The JSON stores the retrieval timestamp and source-response SHA-256 hashes.
Rows are sorted by code. No names or codes are synthesized from job offers.
The resolver reads this bundled resource locally; matching never calls the API.
Unknown or ambiguous locations remain neutral unless other reliable source
evidence establishes a geographic contradiction.

For a refresh, retrieve these same three endpoints, validate code uniqueness
and required fields, retain provenance, and rerun geographic regression tests.
A refresh is a maintenance operation, not an application-startup operation.
