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
- Zoom de l’offre : sans déplacement du titre, effet immédiat et valeur initiale de 1,15 à chaque lancement.
- Notifications : superposées en haut, fermables, sans modifier la géométrie du contenu.
- Profil, descriptions, diagnostics : retour à la ligne et hauteur adaptée au contenu.

Les tokens partagés sont dans [`Theme.qml`](../smartapply/desktop/qml/components/Theme.qml).

## Vérification reproductible

Depuis la racine du dépôt :

```sh
.venv/bin/python tools/desktop_visual_check.py --widths 1320,1480,1800,2524 --output /tmp/elan-visual-review
.venv/bin/pytest tests/test_desktop_services.py -q
.venv/bin/ruff check tools/desktop_visual_check.py
```

Le contrôle visuel utilise PySide6, le style Basic et un moteur de rendu logiciel.
Il crée une base temporaire contenant des offres fictives et utilise le profil d’exemple public.
Pour vérifier vos propres données, ajoutez `--database /chemin/vers/une-sauvegarde.db` : seule une copie temporaire est utilisée. Cette base doit contenir au moins deux offres prêtes à envoyer pour les interactions du tableau.
Il n’effectue ni vérification réseau des sources, ni génération de documents, ni envoi,
ni décision sur les doublons. Les états de sources connectées, de génération disponible,
de profil long et de comparaison de doublons sont des fixtures en mémoire.

Le dossier de sortie contient les captures et `diagnostics.json`, qui répertorie les
avertissements QML et les assertions. Un échec de contrôle produit un code de sortie non nul.

La recette couvre les huit pages en 1 320 × 820, 1 480 × 920, 1 800 × 1 000 et 2 524 × 1 000,
ainsi que la barre d’outils Offres en 1 559 et 1 560 px, de part et d’autre du
changement de largeur de la navigation. Les colonnes sont contrôlées pour détecter les chevauchements, les désalignements et le recouvrement par la barre de défilement. Elle exerce aussi le tri, la sélection et le filtrage des offres,
Échap, l’ordre de tabulation du formulaire, sa validation, le zoom, la navigation
au clavier et la fermeture des notifications.

La page Entreprises conserve les neuf champs du CSV importé dans la table SQLite
`companies`. La colonne `checked` est enregistrée dès le clic et reste inchangée
au redémarrage. Une nouvelle installation commence avec une liste vide ; les
entreprises sont ajoutées manuellement ou importées explicitement. Les noms en doublon
(casse et espaces) sont refusés. La recette vérifie aussi les filtres, les coches,
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
