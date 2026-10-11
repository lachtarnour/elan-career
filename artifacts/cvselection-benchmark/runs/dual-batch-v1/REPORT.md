# Benchmark CVSelection — 40 offres, deux Batchs

Le dataset figé contient 40 analyses fraîches exploitables et 40 identifiants
distincts. Une requête `CVSelection` par offre a été soumise à chaque fournisseur.
Les 80 réponses sont reçues, conformes au schéma Pydantic `CVSelection` et sans
erreur fournisseur. Les sorties n'ont été ni corrigées ni enrichies.

| | OpenAI | Anthropic |
|---|---:|---:|
| Modèle demandé | `gpt-5.4` | `claude-haiku-5-5` |
| Batch ID | `batch_6aca775463548190b228c5f6985e21c3` | `msgbatch_01RD38AyHQqjWzBfHNbwARpF` |
| État final | `completed` | `ended` |
| Réponses valides | 40/40 | 40/40 |
| Entrée native, hors cache | 127 765 | 666 582 |
| Entrée lue du cache | 346 880 | 178 893 |
| Entrée écrite en cache | sans objet | 4 587 |
| Sortie totale | 22 174 | 230 425 |
| Raisonnement interne dans la sortie | 0 | 192 235 |
| Durée du Batch, création à fin | 171 s | 177,56 s |
| Coût Batch estimé | 0,36937 USD | 0,09212 USD |

Les deux modèles ont reçu exactement les mêmes textes système et utilisateur,
les mêmes 40 offres et le même schéma canonique. Le plafond de sortie est
32 000 tokens par requête. OpenAI a reçu `reasoning=none` et `temperature=0.3`.
Anthropic a reçu `effort=high` ; son adaptateur de production omet la température
sur ce modèle et traduit le schéma vers le format natif accepté par son API.

Les coûts sont calculés à partir des compteurs natifs, des tarifs enregistrés
dans `smartapply/llm/usage.py` et de la remise Batch de 50 %. Ce sont des
estimations, pas des montants facturés. Les durées sont celles des Batchs, pas
des latences par offre, que les réponses Batch ne donnent pas. La conformité
indiquée ici est celle du schéma structuré ; la pertinence du choix de projets
et de compétences n'a pas encore été évaluée.

`manifest.json` contient les empreintes du dataset, des prompts, du profil, du
schéma et des requêtes. `requests/` contient les entrées figées et le transport
Anthropic réellement envoyé. `raw/` conserve les réponses de soumission,
états, sorties et erreurs fournisseur. `results.jsonl` associe chaque réponse
structurée et usage natif à son identifiant d'offre. `summary.json` agrège les
compteurs ci-dessus. Aucun appel de rédaction du CV, de lettre, de résumé ou de
reviewer n'a été lancé.
