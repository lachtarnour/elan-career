# Filtrage et état des offres

Le filtre déterministe enregistre uniquement `rejected` (**Rejeté**) ou
`pending_relevance` (**À classer**). La pertinence, l'état du dossier et les
doublons sont des décisions distinctes.

Les deux classes concernent uniquement l'étape de filtrage. Une analyse complète
ou un dossier généré termine cette étape : l'offre affiche **Analysée**, **Prête**,
**Envoyée**, ou son état de suivi, et ne conserve aucune classe active « À classer ».

## Source de vérité

`smartapply/jobsearch/offer_state.py` calcule l'état depuis les preuves enregistrées.
Une analyse présente compte même si son horodatage est absent ; un dossier généré
ou une preuve d'envoi prend priorité sur un ancien statut Nouvelle. Une réservation
vide ne constitue ni une analyse ni un dossier généré. L'archive reste prioritaire.

Les listes, le détail, les copies et l'admission à l'analyse utilisent cette règle.
Les relations de doublons sont conservées indépendamment de l'avancement.

`database/repository/offer_state.py` retire la classe active aux transitions et
conserve sa décision dans `completed_filter_routing`. La preuve déterministe
reste lisible pour une réanalyse explicitement demandée. La maintenance versionnée
`maintenance/offer_states.py` répare les anciens statuts et dates manquantes à
partir de leurs preuves, sans inventer d'analyse pour les dossiers générés seuls.

## Enregistrement et migration

- `smartapply/maintenance/filter_labels.py` complète les décisions absentes ou
  obsolètes avec le filtre déterministe actuel. Les classes déjà valides restent
  inchangées.
- L'enregistrement d'une nouvelle offre calcule cette décision, y compris pour
  les alias de doublons et les offres ajoutées manuellement. Le profil du
  pipeline est utilisé lorsque l'enregistrement passe par celui-ci.
- Une maintenance versionnée indépendante complète aussi les anciennes offres encore actives
  sans ligne de score. Elle ne relance pas les anciennes réparations.

La complétion des décisions manquantes modifie uniquement les composants du filtre. Les scores
numériques et leur date, les offres, leurs statuts et relations de doublons,
les analyses, candidatures et documents sont conservés. Les lignes de score
manquantes sont créées avec des valeurs numériques nulles.

Les choix explicites de conservation ou de restauration sont respectés. Les
motifs indépendants d'archivage manuel ou de rejet après analyse restent dans
leur espace d'origine. La migration conserve la précédente information du
filtre dans `filter_label_completion`.

## Interface

Les offres encore à filtrer affichent seulement **Rejeté** ou **À classer**.
Les offres terminées affichent leur état, sans ajout d'une classe fictive.
« Au moins une offre active »,
activé par défaut, sélectionne les paires dont au moins une offre n'est ni
rejetée ni archivée. Une offre déjà analysée ou préparée peut satisfaire ce
critère ; son rejet déterministe éventuel est lu dans l'historique enregistré.
La visibilité du couple ne décide pas le doublon et ne débloque pas son analyse.

La complétion des décisions applique uniquement les règles locales.

## Raisons d’archivage

Le détail affiche les motifs enregistrés dans `rejection_reasons`, ou les codes
des contrôles déterministes ayant réellement bloqué l’offre. Les notes positives
(`role_title_veto`, par exemple), les conflits non résolus et le contexte de
collecte ne constituent pas des raisons d’archivage. Les anciennes listes mixtes
de diagnostics sont utilisables uniquement si une décision de rejet est enregistrée.

Le regroupement d’un doublon qui récupère un dossier archivé conserve la cause
d’origine dans `archive_inheritance`, avec l’annonce source, le dossier et la
date d’archivage source. Cette provenance ne modifie pas le filtre de la nouvelle
annonce. Le détail explique le regroupement et le motif d’origine. La restauration
retire cet héritage actif et le conserve dans son audit ; un nouvel archivage
explicite remplace la cause active.

Pour un ancien regroupement sans provenance, le détail peut retrouver la cause
sur une unique annonce confirmée du groupe correspondant à l’URL d’envoi du dossier
et au même événement d’archivage (horodatages dans la seconde).
Cette lecture ne réécrit aucune donnée. Sans preuve suffisante, le détail indique
**Raison d’archivage non enregistrée**.
