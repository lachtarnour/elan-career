# Dataset de référence CVSelection v1

`dataset.jsonl` contient 40 annonces distinctes tirées avec la graine `20261010`.
Les strates métier, langue et séniorité utilisent **uniquement les anciennes
analyses pour l'échantillonnage**. Elles ne sont pas présentées comme résultats
des nouveaux appels.

`build_dataset.py` appelle `Pipeline.analyze_jobs` sur une **copie SQLite** avec
`force_reanalyze=True`, une offre à la fois. Le cache LLM local, les points de
reprise d'analyse et les traces Batch de cette copie sont vidés avant la première
exécution. Chaque réponse native, requête, erreur, ligne d'usage et analyse
structurée est enregistrée sans correction. Un cas réussi reçoit les prompts
`CVSelection` construits par la fonction de production ; aucun appel
`CVSelection` n'est effectué.

## Reprise

Copier la base locale avec l'API SQLite `backup` dans
`/private/tmp/cvselection-dataset-v1/smartapply.db`, puis copier le profil dans
`/private/tmp/cvselection-dataset-v1/profile`. Lancer `auto_migrate()` seulement
sur cette copie. Définir `ELAN_HOME`, `DATABASE_URL`, `PROFILE_DIR` vers cette
copie et `ELAN_ENV_FILE` vers le `.env` du dépôt. Les empreintes du manifeste
doivent correspondre aux fichiers du profil, de configuration et des prompts.

```sh
PYTHONPATH="$PWD" .venv/bin/python artifacts/cvselection-benchmark/dataset-v1/build_dataset.py verify
PYTHONPATH="$PWD" .venv/bin/python artifacts/cvselection-benchmark/dataset-v1/build_dataset.py run
```

Le mode `run` reprend les lignes `pending`. Une erreur réseau initiale est
conservée dans `previous_attempts` lors d'une reprise explicite avec
`run 1 retry_network`. Les coûts sont les estimations calculées par Élan à
partir des compteurs du fournisseur ; les requêtes interrompues sans compteur
restent à coût inconnu.
