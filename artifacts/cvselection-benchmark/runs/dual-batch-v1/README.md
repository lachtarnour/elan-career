# CVSelection : deux Batchs sur les mêmes 40 offres

`manifest.json` fixe les 40 identifiants, les empreintes du dataset, du profil, des
prompts de production et du schéma canonique `CVSelection`. Les deux fichiers
`requests/*.jsonl` contiennent une requête par offre et par modèle. Leurs
prompts et schémas canoniques sont identiques ; les champs propres au fournisseur
sont différents. `requests/anthropic_wire.json` conserve les requêtes réellement
adaptées à Message Batches. Haiku 5.5 ne reçoit pas de température, conformément
à l'adaptateur de production ; OpenAI reçoit `temperature=0.3`.

Les analyses, prompts et profil proviennent du dataset de référence v1. Son
vérificateur historique compare aussi la configuration de l'analyse initiale :
il s'arrête aujourd'hui car le plafond *fit* est passé de 32 768 à 32 000 après
la création du dataset. `run.py prepare` vérifie séparément l'empreinte du
dataset, les 40 résultats, les réponses natives, le profil, tous les fichiers
de prompts et la reconstruction exacte des 40 prompts CVSelection. Il accepte
uniquement cette modification de configuration entre les deux dates.

`state.json` trace les tentatives avant tout appel réseau. Une soumission dont
la réponse est inconnue n'est jamais relancée automatiquement. `raw/` conserve
les réponses natives de soumission, les états observés, les sorties et erreurs.
Une fois un fournisseur terminé, `results.jsonl` enregistre les 40 correspondances
avec l'offre, l'usage natif, la validation structurée ou l'erreur. Aucun appel de
correction, de résumé, de lettre ou de reviewer n'est lancé. Les coûts sont des
estimations à partir des compteurs natifs et des tarifs Batch, pas la facture.

Pour reprendre la lecture des deux lots, sans nouvelle soumission :

```sh
PYTHONPATH="$PWD" .venv/bin/python artifacts/cvselection-benchmark/runs/dual-batch-v1/run.py poll
```

Pour recalculer `results.jsonl` et `summary.json` uniquement à partir des
réponses locales, sans appel réseau : remplacer `poll` par `summarize`.
`REPORT.md` résume les deux Batchs terminés et distingue coût estimé et facture.
