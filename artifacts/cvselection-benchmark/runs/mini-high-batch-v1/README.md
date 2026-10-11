# CVSelection : gpt-5.4-mini, reasoning high

Ce run ajoute un troisième modèle au benchmark des mêmes 40 offres, sans
modifier les deux Batchs archivés dans `../dual-batch-v1/`. `manifest.json`
contient les empreintes des entrées et leurs identifiants ; `requests/openai.jsonl`
conserve les 40 requêtes réellement téléversées. Les prompts système et
utilisateur ainsi que le schéma `CVSelection` sont identiques au Batch OpenAI
précédent. Le modèle est `gpt-5.4-mini`, le raisonnement `high` et le plafond
de sortie 32 000 tokens. La température est omise par le transport de production
pour les modèles GPT-5.4 avec raisonnement activé.

`state.json` trace les étapes avant les appels réseau pour empêcher toute
seconde soumission après une réponse inconnue. `raw/` garde la réponse native
de téléversement, de soumission, les états et les sorties ou erreurs. Dès la
fin du Batch, `results.jsonl` enregistre une ligne par offre, et `summary.json`
agrège les usages et coûts estimés. Aucun appel de correction, rédaction ou
reviewer n'est exécuté. Les coûts estimés ne sont pas la facture fournisseur.

Pour consulter le Batch déjà soumis et récupérer ses résultats :

```sh
PYTHONPATH="$PWD" .venv/bin/python artifacts/cvselection-benchmark/runs/mini-high-batch-v1/run.py poll
```

Pour recalculer les résultats exclusivement à partir des fichiers locaux,
remplacer `poll` par `summarize`.
