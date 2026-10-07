# Audit de la page Analyse — 6 octobre 2026

## Parcours livré

- Tableau à gauche, description complète à droite, séparation ajustable.
- Suivi compact en bas : Extraction → Évaluation → Bilan.
- Sélection individuelle ou de toutes les offres affichées ; recherche et tri.
- Analyse immédiate ou Batch sur la sélection, sans plafond ni classement Top 25.
- Plusieurs parcours Batch distincts ; historique, taille et avancement par étape.
- Annulation du parcours sélectionné, avec confirmation et retour visuel.
- Un parcours annulé disparaît après confirmation du fournisseur. Son journal
  et les résultats reçus restent enregistrés ; les offres restantes sont libérées.
- Colonne **État** : À extraire, À évaluer, En extraction, En évaluation,
  Analysée ou échec selon le parcours. Ces états de traitement ne créent aucune
  nouvelle classe d’offre : Rejeté et À classer restent les seules classes.

## Problèmes constatés et corrigés

| Problème | Correction | Vérification |
| --- | --- | --- |
| Une extraction réussie pouvait être repayée après expiration du cache ou changement de mode | Point de reprise indépendant du cache ; reprise directement à l’évaluation | Cache supprimé, Batch vers immédiat et Batch vers Batch |
| Une sélection immédiate obsolète pouvait viser une offre déjà réservée par un Batch | Contrôle des réservations à la sélection et avant les appels/publication | Aucun appel pour une offre réservée |
| Un traitement immédiat bloquait le suivi et l’annulation des Batch | Worker de contrôle distinct pendant l’analyse immédiate | Actualisation et annulation concurrentes, arrêt et fermeture |
| Une annulation pendant une actualisation pouvait être ignorée | Annulation mise en attente et dédoublonnée | Demande concurrente exécutée exactement une fois |
| L’historique affichait encore les parcours annulés | Masquage après confirmation, conservation du journal | Un parcours annulé, un autre toujours actif |
| La colonne Décision répétait À classer | États d’exécution exploitables dans les deux vues | Présentation et interface réelle |
| Le premier aperçu pouvait différer de la première ligne affichée | Ouverture selon l’ordre effectif du tableau | Test QML avec le bridge réel |
| Le tri de l’expérience était textuel | Tri numérique des années | Relecture du tri et tests de la table |
| Les confirmations produisaient un avertissement d’accessibilité Qt | Libellé accessible attaché au contenu du dialogue | Chargement QML et tests de dialogue |
| Le compteur confondait demandes programmées et Batch envoyés | Compteurs séparés selon les identifiants fournisseur persistés | Présentation, suivi et capture isolée |
| Une vague préparée abandonnée pouvait sembler terminée | État Non exécutée pour une vague sans envoi ni résultats | Présentation des phases |

## Contrat de reprise

`database/analysis_checkpoints.py` stocke uniquement une extraction validée
qui attend son évaluation. `pipeline/process/analysis_checkpoint.py` contrôle
l’identité exacte : modèle, prompts, schéma et entrée normalisée de l’offre.
Un profil modifié peut réutiliser l’extraction ; une offre, un prompt, un schéma
ou un modèle modifié déclenche une nouvelle extraction. L’analyse complète
publiée supprime le point de reprise. Le cache existant conserve sa politique.
Les anciens journaux Batch sont récupérés à la demande grâce à la clé de
requête enregistrée, sans reconstruire artificiellement leur ancien contrat.

Une extraction terminée pendant un arrêt est conservée, puis l’arrêt est
respecté avant l’évaluation. Une offre dont l’analyse complète existe ne
repasse pas dans la file normale. Les reprises internes explicitement forcées
restent possibles pour les autres fonctions existantes de l’application.

## Vérifications visuelles et fonctionnelles

Tests QML aux largeurs 1 100, 1 320, 1 700 et 2 400 pixels : tableau et barre
d’actions accessibles, suivi sous les panneaux, détails déroulants dans leur
propre espace. Vérification des sélections, recherche, tri, aperçu indépendant,
zoom, parcours multiples, attente, erreur, reprise, annulation et résultats.
Les actualisations préservent la sélection, le tri, la recherche, le défilement
et le contexte de l’espace Offres. La présentation ne charge pas les corps
d’offres, profils, prompts ni résultats lourds pour remplir le tableau.

Les tests de transport remplacent le fournisseur par un service déterministe :
extraction complète avant évaluation automatique, résultats partiels, expiration,
reprise, coût compté une seule fois, envoi incertain récupéré sans double POST.
Aucun nouvel appel payant n’a été effectué pour cet audit. Le délai réel du
fournisseur dépend de sa file ; une annulation distante reste visible jusqu’à
sa confirmation. Élan doit être ouvert pour importer les résultats et lancer
la seconde étape ; les demandes déjà envoyées continuent chez le fournisseur
pendant sa fermeture.
