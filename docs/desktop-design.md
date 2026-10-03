# Interface Élan — conventions et recette visuelle

[Retour au README](../README.md)

## Direction commune

Les huit pages utilisent le même thème sombre, une hiérarchie typographique commune,
des surfaces sobres et des couleurs sémantiques : violet pour les actions de recherche,
vert pour l’envoi, ambre pour l’archivage et les avertissements.

- Espacement extérieur : 28 px, avec 32 px au-dessus du contenu, sous la zone native de la fenêtre.
- Pages de formulaire : contenu centré, largeur maximale de 1 320 px, défilement vertical unique.
- Titres de page : 26 px ; titres de section : 17 px ; champs et textes courants : 13 px.
- Un seul titre principal par page, sans sous-titre explicatif. Les titres de section servent uniquement à distinguer les groupes de données.
- Pas de consignes permanentes, de confirmation anticipée ni de raccourcis redondants avec la navigation. Les erreurs de saisie sont contextuelles.
- Contrôles standard : 40 px ; contrôles compacts conservés dans la page Offres.
- Offres : partage 50/50, marge réservée au défilement, recherche extensible limitée à son panneau.
- Tableau Offres : grille commune aux en-têtes et aux cellules, proportions calculées sur la largeur du panneau, postes et lieux sur deux lignes au maximum. Scores IA/Match neutres sans contour, sélection légère et lignes sans séparateurs décoratifs.
- Dans la vue Nouvelle, la colonne `Filtre` remplace IA/Match, place les offres pertinentes avant les incertaines et l'action `Analyser (N)` ne traite que la sélection sans créer de shortlist persistante.
- Doublons : le filtre `Pertinents` conserve uniquement les paires dont les deux offres ont passé le filtre local. Une republication confirmée devient principale, tandis que le dossier envoyé, les documents et l'historique des anciennes sources restent liés au groupe.
- Zoom de l’offre : sans déplacement du titre, effet immédiat et valeur initiale de 1,15 à chaque lancement.
- Notifications : toasts superposés en haut et tâche active en bas à droite, sans modifier la géométrie du contenu.
- Profil, descriptions, diagnostics : retour à la ligne et hauteur adaptée au contenu.

Les tokens partagés sont dans [`Theme.qml`](../smartapply/desktop/qml/components/Theme.qml).

## Traitements et suivi

Une seule opération longue s’exécute à la fois ; les demandes suivantes attendent
leur tour. Les actions courtes passent par une file indépendante. Une offre et ses
alias sont protégés pendant leur traitement, avec un motif explicite en cas de
conflit. La navigation, les notes et les actions sur les autres offres restent disponibles.
L’ajout manuel enregistre d’abord l’offre, puis programme sa candidature. Une shortlist
non analysée reste une intention annulable jusqu’au succès de l’analyse.

Les changements enregistrés regroupent les actualisations sur 200 ms ; la progression
ne recharge pas les listes. Chaque vue conserve une seule lecture active et la dernière
demande suivante. Les listes conservent leur limite, leur sélection et leur défilement.
Les diagnostics des fournisseurs sont conservés cinq minutes et renouvelés sur demande
ou après modification de configuration.

Les notes restent des brouillons par candidature jusqu’à confirmation de sauvegarde.
Elles sont enregistrées après 500 ms, à la perte de focus, au changement d’offre et avant
fermeture. La fermeture attend les sauvegardes et le nettoyage des tâches sans bloquer
la fenêtre ; une sauvegarde échouée peut être réessayée. La maintenance historique est
versionnée et reprise au prochain lancement après interruption. Pendant cette vérification,
seules les opérations dépendant de la validation des doublons attendent.

## Vérification reproductible

Depuis la racine du dépôt :

```sh
.venv/bin/pytest tests/test_desktop_services.py tests/test_desktop_notifications.py tests/test_source_diagnostics_refresh.py tests/test_desktop_task_lifecycle.py tests/test_desktop_qml_responsiveness.py -q
```

Les tests Qt chargent réellement QML avec des services inertes et des données fictives.
Ils vérifient notamment les notifications et la conservation des choix de sources,
sans utiliser la base active ou les fournisseurs réels.

Compléter ces tests par une vérification interactive des pages en 1 320 × 820,
1 480 × 920, 1 800 × 1 000 et 2 524 × 1 000. Vérifier les chevauchements de colonnes,
le tri, la sélection, le filtrage, Échap, la navigation au clavier et le zoom.
Pour les scénarios avec données existantes, utiliser uniquement une copie temporaire
de la base et des fournisseurs simulés. Conserver les captures et rapports dans
`artifacts/` ou un dossier temporaire, hors Git.

La page Entreprises conserve les neuf champs du CSV importé dans la table SQLite
`companies`. La colonne `checked` est enregistrée dès le clic et reste inchangée
au redémarrage. Une nouvelle installation commence avec une liste vide ; les
entreprises sont ajoutées manuellement ou importées explicitement. Les noms en doublon
(casse et espaces) sont refusés. Vérifier aussi les filtres, les coches,
la validation du popup, l’ajout manuel et sa fermeture avec Échap.

La maintenance `make sync-companies` actualise les fiches non cochées depuis le
CSV privé `data/company-targeting/companies.csv` avec
sauvegarde, sans suppression et sans modifier les fiches checked.
Le tri de l’application utilise la priorité puis l’ordre ; après ajout manuel,
la vue se positionne sur la nouvelle fiche à son rang. Voir le
[référentiel et les règles de synchronisation](company-targeting.md).

## Avant distribution

Compléter les contrôles automatiques par une recette de l’application macOS empaquetée :
rendu Metal/Retina, plein écran natif, lecteurs d’écran, signature/notarisation,
sources réellement connectées et parcours complets de génération et d’envoi.
