# Doublons : preuves, copies et décisions

Le dédoublonnage est indépendant du rejet déterministe et de la future
classification de pertinence. Aucun appel LLM n'est utilisé dans ce traitement.

## Trois responsabilités

| Module | Responsabilité |
|---|---|
| `smartapply/dedup/review_policy.py` | Identités techniques validées et séparées par fournisseur |
| `smartapply/dedup/copy_groups.py` | Empreinte de contenu, groupes de copies et clé de classification future |
| `smartapply/dedup/automatic_copies.py` | Preuve de copie stricte et protection des choix manuels et dossiers |
| `smartapply/dedup/review_decisions.py` | Plan automatique, contrôles des groupes et protection de l'historique |
| `smartapply/maintenance/duplicate_reviews.py` | Application atomique, audit et annulation contrôlée |

## Confirmation par identité technique

Les identifiants et URL d'annonce LinkedIn et France Travail ont des extracteurs
spécifiques. Une URL générique `/jobs/...` ou un score de similarité ne suffit
pas. Des identifiants contradictoires dans une même offre annulent la preuve.

Une nouvelle version peut devenir un alias confirmé si son groupe respecte la
fenêtre inclusive de 30 jours et ne présente aucune contradiction explicite de
titre, entreprise, localisation, contrat ou télétravail. L'alias doit être sans
score, analyse, candidature, archive ou choix manuel. Le dossier de la référence
reste à sa place. Les chaînes en attente ne servent jamais de preuve d'identité.

## Traitement automatique des copies certaines

Une copie peut aussi être confirmée automatiquement avec des identifiants ou
des URL différents. La preuve exige :

- le même titre et la même entreprise, en conservant les qualificatifs du poste ;
- la même commune officielle, un contrat connu identique et le même télétravail ;
- la description originale intégralement identique, après normalisation de la
  casse et des espaces uniquement ;
- une description d'au moins 200 caractères et aucune référence employeur divergente ;
- au plus 30 jours d'écart entre chaque paire de versions du groupe.

Les lieux inconnus ne sont pas une preuve de copie. Les nombres, accents,
ponctuation, mots junior/senior et informations de contrat restent significatifs.

Le score et le filtrage automatiques ne bloquent plus cette confirmation. Ils
restent sur la ligne d'origine. Une candidature, une analyse, une sélection,
une réactivation, une décision manuelle ou une archive sans motif automatique
protègent la version concernée. Les groupes liés sont vérifiés intégralement,
y compris leurs autres titres, contrats, lieux, télétravail et dates.

La version portant l'historique reste principale ; sinon une version active,
puis la publication la plus récente, est conservée. Les autres copies éligibles
deviennent des alias confirmés et sortent de la file de décision. Une version
active n'est jamais absorbée par une référence archivée sur la seule preuve de
contenu. Un rapprochement amont encore ambigu reste en attente. Les copies
archivées ne sont traitées que si leur motif prouve un rejet par le filtre local.

Aucune ligne d'offre, score, analyse, candidature ou document n'est supprimée.
Chaque changement de référence ou confirmation est audité. Les futures offres
sont recherchées par empreinte exacte avant la similarité ; la résolution se
termine après leur enregistrement, avant l'analyse de pertinence.

## Présentation des cas encore en attente

Chaque rapprochement encore en attente possède sa propre carte. Chaque décision
concerne la paire affichée.

Après une décision enregistrée, le couple suivant s'ouvre automatiquement dans
la liste filtrée. Les relations retirées
par la décision sont ignorées. À la fin de la liste, les couples précédents encore
en attente sont proposés ; quand aucun couple visible ne reste, la comparaison
se vide. Une erreur conserve le couple affiché. Un changement manuel de sélection,
de filtre ou de page prend priorité sur cette navigation automatique.

`strict_copy_classification_key` prépare la réutilisation d'un résultat futur.
Elle exige les versions du profil, du classifieur, du prompt et du modèle. Elle
ne lance aucune analyse et ne partage actuellement aucun résultat LLM.

## Retrait des relations invalides

La maintenance vérifie les références, les preuves textuelles existantes, les
lieux précis et les dates. Elle retire d'abord les liens sans preuve, puis
recalcule le graphe avant de vérifier ses contraintes : un faux lien ne doit pas
faire retirer d'autres liens valides.

Retirer un rapprochement modifie uniquement ses métadonnées. Cela n'archive,
ne réactive et ne rejette pas l'offre. Les scores, analyses, shortlist,
candidatures et documents sont préservés. L'offre peut être rapprochée plus tard
d'une autre référence.

Chaque décision automatique enregistre sa règle, sa version et ses états avant
et après dans `duplicate_review_events`, dans la même transaction. L'annulation
est refusée si la relation ou l'historique a changé depuis la décision.

La maintenance possède sa propre version : une mise à jour de ces règles ne
relance pas les anciennes réparations historiques. Elle s'exécute au premier
lancement concerné et avant l'enregistrement d'un nouveau lot d'offres.
