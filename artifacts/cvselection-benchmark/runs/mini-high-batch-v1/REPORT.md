# Troisième Batch CVSelection : gpt-5.4-mini, high

Le Batch `batch_6aca79343b488190aab6de266a63252f` est terminé : 40 réponses
sur 40, toutes conformes au schéma structuré `CVSelection`, sans erreur
fournisseur ni arrêt au plafond de tokens. Le modèle retourné est
`gpt-5.4-mini-2026-03-17`. Aucune sortie n'a été corrigée ou enrichie.

| Compteur natif | Total pour 40 offres |
|---|---:|
| Tokens d'entrée | 474 645 |
| Dont entrée lue du cache | 377 856 |
| Tokens de sortie, raisonnement inclus | 626 376 |
| Dont tokens de raisonnement | 602 266 |
| Sortie hors raisonnement | 24 110 |
| Durée du Batch, création à fin | 441 s |
| Coût Batch estimé | 1,45981 USD |

Les 40 prompts système et utilisateur, les identifiants des offres et le schéma
canonique sont exactement ceux du premier Batch OpenAI. Le plafond est identique
(32 000 tokens par requête) ; la température a été omise pour rendre le niveau
`high` compatible avec GPT-5.4. Les compteurs de sortie incluent le raisonnement
interne. Le coût est calculé à partir de l'usage natif et du tarif Batch enregistré
dans `smartapply/llm/usage.py` ; ce n'est pas la facture réelle.

Pour situer ce résultat, les deux premiers Batchs avaient aussi 40/40 réponses
structurées valides : `gpt-5.4` avec raisonnement `none` a utilisé 22 174 tokens
de sortie, dont 0 de raisonnement, pour 0,36937 USD estimés ;
`claude-haiku-5-5` avec raisonnement `high` a utilisé 230 425 tokens de sortie,
dont 192 235 de raisonnement, pour 0,09212 USD estimés. Ces chiffres ne mesurent
pas la pertinence des projets ou compétences sélectionnés.

`manifest.json` et `requests/openai.jsonl` fixent les entrées ; `raw/` conserve
les réponses natives et états ; `results.jsonl` relie chaque réponse et son usage
à l'offre ; `summary.json` contient les totaux et empreintes des sorties.
