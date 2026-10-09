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
- Offres : partage 50/50 dans une seule surface, avec un séparateur vertical discret entre tableau et détail. La liste n'a pas de cadre propre ; les contrôles gardent leurs dimensions compactes, leur marge de défilement et leur recherche extensible locale.
- Tableau Offres : grille commune aux en-têtes et aux cellules, proportions calculées sur la largeur du panneau, postes et lieux sur deux lignes au maximum. Scores IA/Match neutres sans contour, sélection légère et lignes sans séparateurs décoratifs.
- Dans la vue Nouvelle, la colonne `Décision` remplace IA/Match. Le rejet déterministe produit seulement `Rejeté` ou `À classer`; la future classification de pertinence reste une étape séparée. L'action `Analyser (N)` ne traite que la sélection sans créer de shortlist persistante.
- Le filtre de classe de Nouvelle propose `Toutes les classes`, `Rejeté` et `À classer`. Il s'applique à la liste complète du statut choisi, avant le tri. Le détail explique la décision enregistrée, sans recalculer la classification. Les anciennes décisions `Pertinent` et `Incertain` sont migrées vers ces deux états ; elles restent seulement dans l'historique d'audit.
- Doublons : le filtre `Au moins une offre active` conserve les paires dont au moins une offre n'est ni rejetée ni archivée, même si elle est déjà analysée ou possède un dossier. Le rejet déterministe est lu dans la décision enregistrée, y compris son historique ; le statut d'archive ou de refus tient aussi compte du dossier. Les couples dont les deux offres sont rejetées ou archivées sont masqués par ce filtre. Les doublons restent à décider avant l'analyse ; afficher une paire ne confirme pas son identité. Une republication confirmée devient principale, tandis que le dossier envoyé, les documents et l'historique des anciennes sources restent liés au groupe.
- Dans Doublons, la liste est sans cadre extérieur ni titre redondant ; chaque rapprochement en attente possède sa propre carte. Dans la comparaison, les deux offres restent côte à côte dans leurs cartes, en haut de la zone, sans cadre supplémentaire autour de la paire. Les descriptions montrent jusqu'à 1 200 caractères par offre ; la comparaison défile si nécessaire. Chaque décision indique les deux offres concernées. Voir [les règles de dédoublonnage](duplicate-review.md).
- Dans Recherche, le titre et une surface unique pour les critères et les sources forment un groupe centré. Les contrôles gardent leurs espacements naturels, avec un séparateur entre les deux sections ; sur une petite fenêtre, la page défile pour garder tous les contrôles accessibles.
- Analyse : le tableau et le détail partagent la largeur à parts égales, avec des marges, lignes et tailles de texte proches d’Offres. Le tableau garde sa propre carte, tandis que les deux panneaux qui l’entouraient sont sans cadre. La colonne « État » redondante est retirée au profit de la largeur du poste. Les commandes au-dessus du tableau suivent cet ordre : Analyse immédiate, Analyse par Batch, Archiver, Actualiser, Recherche. Elles passent à la ligne si nécessaire. Le suivi des Batch conserve son espace en bas, sans cadre extérieur ni carte intérieure pour ses détails. Les libellés utilisent « analyse en cours » et « Analyse n° ».
- Dans Analyse, « Archiver (N) » archive uniquement les offres sélectionnées après confirmation. « Archiver », à côté d’« Offre d’origine », agit uniquement sur l’offre ouverte. Les analyses et journaux existants sont conservés ; une offre réservée par une analyse immédiate ou un Batch ne peut pas être archivée par ces actions. L’archivage groupé est atomique, sans suppression ni archivage automatique des copies. La sélection restante, la recherche et le contexte de la page Offres sont conservés.
- Dans Offres > Archivée, un sous-libellé indique la provenance vérifiable : « Filtre automatique », « Missions hors cible » (après extraction, avant évaluation), « Archivage manuel » ou « Doublon confirmé ». Les nouveaux archivages par filtre, préfiltre ou action manuelle enregistrent `archive_origin` dans les composants d’audit du score ; un doublon confirmé est identifié par son lien vers l’offre principale. Une ancienne archive sans provenance exploitable conserve simplement le statut « Archivée ». Le sous-libellé est calculé à la lecture sans réécrire les archives historiques ni modifier les valeurs des scores.
- Le sélecteur « Origine de l’archivage », à côté du statut et visible uniquement pour « Archivée », filtre ces quatre provenances avant le comptage, la pagination et la sélection globale. « Tous les archivages » inclut aussi les archives historiques sans provenance. Le choix reste actif pendant la recherche, le tri et la pagination.
- « Désarchiver et créer » conserve un override `manual_rescue` qui protège durablement l’offre du filtre automatique et du préfiltre des missions. Un doublon en attente arrête toute analyse et génération ; un lien confirmé est rouvert pour vérification. Les offres associées apparaissent dans Doublons, avec l’ancien dossier conservé jusqu’à une décision puis une préparation explicites.
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
L’ajout manuel constitue un choix explicite de conserver l’offre : les filtres de
rejet automatiques ne sont pas exécutés, y compris lors des traitements suivants.
Le détail signale cette exemption sans affirmer que les critères ont été validés.
Les contrôles de doublons, d’envoi et de réservation restent inchangés.

Les changements enregistrés regroupent les actualisations sur 200 ms ; la progression
ne recharge pas les listes. Chaque vue conserve une seule lecture active et la dernière
demande suivante. Les listes affichent toutes les offres du statut choisi, sans plafond
de 500, et conservent leur sélection et leur défilement.
Une actualisation du même contexte garde la liste et le détail utilisables ; le masque
de chargement reste réservé au premier affichage et aux changements de requête ou
d’offre. Au retour sur Offres, la recherche, le filtre, le tri, les sélections et le
défilement sont conservés, y compris après consultation de Doublons. Une ouverture
explicite de dossier prend la priorité sur cette restauration.
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

## Choix du fournisseur IA

Réglages propose quatre rubriques : Modèles IA, Connexions, Application et Données
locales. Les appels IA sont séparés en deux vues, Analyse des offres et CV et
lettres. Chaque appel affiche d'abord son modèle, son niveau de raisonnement et
sa limite de sortie ; une seule ligne se déplie à la fois pour la modification.
CV et lettres expose trois appels : sélection, rédaction, relecture. Les reprises
de correction héritent des mêmes paramètres et n'ont pas de carte indépendante.
Le modèle possède un seul contrôle : une liste, ou un champ d'identifiant lorsque
« Autre modèle » est choisi. Les détails du dernier appel API se déplient à part.

Connexions rassemble les clés OpenAI/Claude et les paramètres des sources, repliés
par source avec leur état. Application regroupe les traitements et la présentation
des CV ; les fenêtres de contexte et la journalisation sont dans Paramètres
avancés. Données locales donne accès aux fichiers actifs et aux chemins configurés.
Les paramètres ont des libellés français et les booléens utilisent des interrupteurs.

La barre d'enregistrement reste accessible pendant le défilement. Les brouillons
sont conservés entre rubriques et entre les deux vues de modèles, y compris pendant
une actualisation. Les clés existantes restent masquées ; un champ vide conserve
la clé. La sauvegarde est asynchrone et préserve la sélection, les notes, les filtres
et les tâches en cours. Une erreur laisse les champs modifiés disponibles.

Une opération garde son fournisseur et ses modèles. Les cartes de suivi Batch
affichent le fournisseur et le modèle enregistrés dans leur journal, indépendamment
des réglages actuels. Les anciens journaux désignent OpenAI.
Les tests QML utilisent un runtime fictif isolé pour vérifier le panneau aux
quatre tailles de fenêtre indiquées ci-dessus.

## Avant distribution

Dans Offres, la colonne « Operation date » affiche la date d’achèvement de
l’analyse pour « Analysée », la date de la dernière préparation réussie pour
« Prête » et la date de soumission pour « Envoyée ». Les dates sont affichées en
heure locale. Un clic sur l’en-tête trie tous les résultats par jour d’opération,
puis par score IA décroissant dans chaque jour, avant pagination. Un second clic
inverse l’ordre des jours. La préparation utilise `applications.ready_at`, sans
dépendre des modifications de notes. Pour les anciens dossiers sans ce champ,
la date de création des documents sert de repère historique. L’envoi utilise
`applications.form_submitted_at` ; une date inconnue reste « — ».

Dans « Analysée », un curseur fin « IA min. » règle le seuil de 0 à 100 %, par
pas de 1 %. Le petit bouton de remise à zéro affiche « Aucun », qui conserve
aussi les offres sans score IA ; 0 % exige un score renseigné. Le seuil est
appliqué avant le tri, la pagination et la sélection de tous les résultats. Il
reste actif pendant les recherches, les tris, les actualisations et les
changements de vue, jusqu’à la remise à zéro.
La préférence est aussi restaurée au prochain lancement de l’application.

Compléter les contrôles automatiques par une recette de l’application macOS empaquetée :
rendu Metal/Retina, plein écran natif, lecteurs d’écran, signature/notarisation,
sources réellement connectées et parcours complets de génération et d’envoi.
