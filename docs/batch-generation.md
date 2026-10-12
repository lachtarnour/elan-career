# Création des candidatures par Batch ou en immédiat

[Documentation](README.md) · [Analyse par Batch](batch-analysis.md)

Dans **Offres**, **Créer** propose **Batch** et **Immédiat** pour les offres
déjà analysées : offre ouverte ou offres cochées. Le suivi
reprend les composants de l'analyse avec les étapes propres aux documents.

## Audit du parcours existant

La création passait par `DesktopService.generate_application(s)`, puis
`Pipeline.apply_to` et `Applier.apply`. Elle conservait le dossier final,
sans journal durable de toutes les réponses intermédiaires. Le cache local
ne suffisait pas à garantir la reprise, notamment pour la relecture qui
utilise `use_cache=False`.

Le parcours comporte des branches et des traitements Python indispensables :

1. Sélection du CV, éventuellement corrigée si les projets ou certificats
   ne satisfont pas les règles.
2. Assemblage, normalisation des compétences, contrat de rôle, validation et
   corrections déterministes du CV.
3. Rédaction du résumé et de la lettre à partir du CV finalisé.
4. Relecture, correction et confirmation si nécessaire. Les contrôles Python
   peuvent rejeter une correction et conserver les textes précédents.
5. Validation finale, rendu DOCX/HTML/PDF et publication atomique.

Les contrôles Python de contenu sont consultatifs : citations, sélection de
projets et de certificats, structure du CV, longueur des textes et nom de
l'employeur. Après la tentative de correction prévue, les problèmes restants
sont enregistrés et affichés parmi les points à vérifier dans **Prêt**. Ils
n'interrompent pas la génération du dossier, en Batch comme en immédiat.

Les transports Batch existants sont réutilisés. Le coordinateur d'analyse
est lié à ses trois phases ; la création possède donc un journal distinct
et rejoue le même parcours `Applier` derrière un fournisseur de réponses
durables. Les transformations et validations ne sont pas dupliquées.

## Exécution, arrêt et reprise

**Immédiat** sauvegarde chaque réponse structurée avant l'étape suivante.
**Batch** rassemble les prochains appels disponibles dans des lots OpenAI
ou Anthropic, selon les routes configurées. Après réception, Élan reprend
automatiquement les traitements Python et prépare les appels suivants.

**Suivi**, dans la navigation principale, reste accessible après réouverture,
quel que soit le filtre des offres. Il regroupe l'historique des créations et
des analyses, avec les étapes, les lots, les dossiers publiés et les erreurs,
sans remplacer la sélection, les notes ou le détail ouvert. Le journal local
est affiché avant la synchronisation avec les fournisseurs ; une erreur de
synchronisation laisse cet état consultable. Les Batch sont vérifiés au
démarrage puis toutes les deux minutes, même hors de la page Offres.
**Actualiser** vérifie immédiatement. Les lots soumis continuent
chez le fournisseur après fermeture ; les traitements locaux et les étapes
suivantes reprennent à la réouverture. Un parcours immédiat interrompu attend
**Reprendre**.

Fermer l'application n'annule pas un Batch. Si son envoi initial est encore
en cours, Élan attend l'enregistrement de cet envoi avant de fermer.

**Arrêter** conserve le dossier réservé et les réponses reçues. Les lots
soumis sont annulés auprès du fournisseur ; leurs succès disponibles sont
importés avant la pause. L'arrêt empêche de nouveaux appels. **Reprendre**
réutilise les réponses validées et relance uniquement l'appel échoué ou
manquant. Les opérations contradictoires sur l'offre et ses alias sont
bloquées pendant la réservation ; les notes restent éditables.

La reprise est **par offre**, même dans un autre parcours ou un autre mode.
Une offre arrêtée propose **Reprendre** et le choix Batch/Immédiat : ses
réponses déjà acquises sont réutilisées. On peut reprendre une seule offre
d'un parcours multiple, ou réunir des offres de plusieurs parcours arrêtés.
Les autres offres conservent leur progression. Le parcours précédent garde
son historique et indique les offres reprises ailleurs.

Pour renoncer à un parcours arrêté, **Abandonner** libère ses offres après
confirmation de l'arrêt fournisseur. Les réponses acquises restent dans le
journal. Une reprise ultérieure exige que les offres soient encore disponibles.

Le profil, les routes et la date de référence sont figés au premier démarrage
de chaque offre et conservés lors de ses reprises, même si la configuration
courante a changé. Des offres reprises ensemble peuvent garder des contextes
d'origine différents.
L'identité d'une réponse comprend le fournisseur et le corps complet de la
requête : prompts, schéma, effort, température et limite de sortie. La
reprise ne dépend pas de l'expiration du cache local. L'assemblage et les
validations Python sont réexécutés avec les réponses sauvegardées. Une
modification de l'offre, de l'analyse ou d'une requête acquise arrête le
parcours avant de payer une nouvelle étape.

Une confirmation d'envoi perdue apparaît **Envoi à vérifier**. Le Batch est
recherché grâce au journal, sans renvoi automatique. Un appel immédiat dont
l'acceptation est inconnue bloque le renvoi : le transport synchrone ne
permet pas cette récupération. Les retries internes de connexion sont
désactivés pour ces appels de création immédiate.
Changer de mode ou de parcours ne contourne pas cette protection : tant
qu'un envoi est en cours ou incertain, l'offre attend sa résolution avant
tout nouvel appel.

La publication du dossier et le résultat final du journal partagent la même
transaction. L'import Batch et son usage sont atomiques et idempotents.
Les entrées manuelles et la régénération existantes restent disponibles.

## Repères du code

| Module | Rôle |
| --- | --- |
| `database/generation_runs.py` | Parcours, offres, appels et lots durables |
| `pipeline/generation/replay.py` | Réponses sauvegardées et suspension |
| `pipeline/generation/coordinator.py` | Enchaînement, arrêt et reprise |
| `pipeline/generation/offer_resume.py` | Reprise par offre et contexte d'origine |
| `pipeline/generation/transport.py` | Envoi, récupération et import Batch |
| `pipeline/generation/presentation.py` | Résumés sans charger les prompts |
| `desktop/generation_controller.py` | Suivi en arrière-plan |

Les tests emploient des fournisseurs simulés et des bases temporaires :
génération réelle, reprise, erreurs, envois ambigus, réservations,
suppression et interface QML.
