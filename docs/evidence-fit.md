# Score IA global

Sur cette branche, le LLM retourne directement `fit_score` (0 à 1), `match_reasons`
et `risks`. Le barème et les règles de preuve viennent du parent du merge PR #15
(`49fb156`). Python valide le contrat et conserve la note sans pondération,
arrondi, plafond ou moyenne avec le matching. Le profil complet et les faits
extraits de l’annonce restent les entrées de l’évaluation.

La Top sélection trie les offres admissibles au workflow par `fit_score`
décroissant, puis par identifiant en cas d’égalité. Le nombre demandé et le seuil
IA restent applicables. Les notes historiques ne sont pas mélangées avec les
nouvelles notes globales : elles restent affichées et peuvent être réévaluées
explicitement. Les sélections manuelles sont conservées.

Les analyses immédiates, Batch et réévaluations utilisent le même schéma strict
`JobFitAssessment`, avec un contexte signé et les réglages fournisseur figés.
Les lots soumis avec l’ancien contrat ne peuvent pas publier sous le nouveau
contrat ; leur journal et leurs réponses restent lisibles. Aucune réévaluation
payante n’est lancée lors de la migration ou d’un changement de branche.

## Base compatible et retour aux trois évaluations

Les tables et colonnes existantes sont conservées. `job_fit_history` est une
addition ; les versions précédentes de l’application peuvent ignorer cette table.
À chaque remplacement d’analyse ou réévaluation, l’analyse précédente entière
(note, raisons, risques, réponse brute, trois axes, preuves, contexte et modèle)
est archivée dans la même transaction. Un échec annule l’archive et le remplacement.
Les candidatures, documents et statuts ne sont pas modifiés par la restauration.

Initialiser avec `elan init-db`, ou utiliser `elan fit-schema` pour ajouter
uniquement les éléments du schéma fit sur une base existante. Puis :

```sh
elan fit-history --job-id 123
elan fit-history --job-id 123 --restore 456
```

La deuxième commande restaure l’analyse enregistrée sous l’identifiant 456,
contrôle son empreinte et sauvegarde à son tour l’évaluation déplacée. Pour revenir
à V4 : restaurer les évaluations souhaitées **avant** de changer de branche,
puis utiliser la branche V4/main contenant la politique trois axes. Les offres
réévaluées uniquement en global devront être réévaluées à trois axes si elles
n’ont aucune ancienne évaluation correspondante. Un changement de branche seul
ne transforme pas les notes déjà enregistrées.

Le calcul historique 35/45/20 et ses lecteurs restent testés. Voir
[le contrat historique](three-axis-fit.md). La base de production n’est pas
modifiée par cette implémentation.
