# Sélection des projets

Le profil déclare une politique stable par projet dans `projects.json` :

```json
"selection_policy": {
  "tier": "specialized",
  "rationale": "Projet pédagogique utile pour démontrer une tâche précise.",
  "activation_conditions": ["Extraction d'entités nommées explicitement demandée"]
}
```

- `primary` : projet prioritaire par pertinence directe ou compétences transférables.
- `specialized` : complément activable pour un besoin explicite de l'offre ; au plus un.
- `disabled` : conservé dans le profil et l'historique, exclu des nouvelles sélections.

Les anciens profils sans politique restent compatibles et utilisent `primary`.
La politique ne modifie ni les descriptions ni les preuves du projet. Elle est
personnelle et reste dans le profil privé, sans identifiants codés dans le moteur.

## Parcours de génération

L'appel de personnalisation existant choisit exactement trois projets distincts :
le minimum est de trois et le maximum reste de trois. Il retient d'abord les
meilleures correspondances directes, puis les projets prioritaires démontrant les
compétences transférables les plus utiles pour compléter la sélection. Il écrit
ensuite un résumé fondé sur cette sélection. Chaque choix comporte une
justification et un extrait exact des tâches, compétences demandées ou du texte de
l'offre, y compris lorsque le lien repose sur des compétences transférables. La
justification doit décrire ce lien honnêtement, sans inventer une expérience du
domaine demandé.

Un projet spécialisé doit apporter une contribution distincte et répondre à un
besoin explicite ; la simple présence de Python ou PyTorch ne suffit pas à
l'activer. Le minimum de trois n'autorise ni l'assouplissement de cette règle ni
la sélection de projets désactivés. Si le catalogue ne permet pas trois choix
admissibles, la génération signale cette limite dans le dossier et continue.

Le même appel choisit de zéro à deux certificats pertinents parmi les
identifiants de `certificates.json`. Une liste vide signifie qu'aucun certificat
n'améliore cette candidature. Le code signale les identifiants inconnus, les
doublons et plus de deux choix, puis utilise la même sélection et le
même ordre dans les exports HTML/PDF et DOCX. Aucun appel LLM supplémentaire
n'est ajouté. Les anciens snapshots, créés avant cette sélection, restent
compatibles et continuent d'afficher les certificats historiques.

Le modèle juge la pertinence et la complémentarité. Le code contrôle les
identifiants, doublons, catégories, minimum et maximum de trois projets, la limite
de certificats, la présence des justifications et l'existence des extraits cités.
Ce dernier contrôle ne prouve pas la pertinence sémantique d'une justification.
Ces contrôles Python sont consultatifs : leurs résultats sont conservés dans le
dossier et affichés parmi les points à vérifier dans **Prêt**. Ils ne bloquent
ni la rédaction de la lettre ni la publication des documents.

La recherche de l'extrait applique la même normalisation typographique à la
citation et aux sources : Unicode, casse, espaces et retours à la ligne,
apostrophes droites/courbes et traits d'union typographiques. Une coupure telle
que `d’` suivie d'un retour à la ligne puis `APIs` correspond donc à `d'APIs`.
Les textes d'origine restent inchangés. Les mots, leur ordre, les accents,
les chiffres et la ponctuation significative restent nécessaires ; aucune
recherche approximative ni validation supplémentaire par LLM n'est ajoutée.
Cette normalisation ne sert pas à localiser les passages à remplacer lors de
la relecture, qui conserve ses contrôles exacts.

Une sélection invalide déclenche une seule nouvelle tentative de personnalisation
complète. Après cette correction, deux défauts de citation peuvent être réparés
localement : un point final ajouté à une mission complète, ou une liste séparée
par des virgules dont chaque élément correspond exactement à une compétence
requise. Dans ce dernier cas, la citation devient la première compétence de la
liste, reprise telle quelle depuis l'offre. La réponse IA originale reste dans
le journal ; le snapshot CV conserve la citation réparée et un avertissement
`auto_fixed:project_offer_evidence`. Aucun projet, justification ou compétence
candidat n'est modifié. Les autres erreurs sont signalées, sans appel IA
supplémentaire. Le contrôle initial reste identique pour pouvoir rejouer les
demandes de correction déjà sauvegardées.

Si la sélection reste invalide, la génération conserve cette sélection et ses
diagnostics, puis continue vers le résumé, la lettre et le rendu. Le dossier
terminé est **Prêt à candidater**, avec les points à vérifier. Un catalogue de
moins de trois projets éligibles est également signalé sans interrompre la
création. Aucun correctif automatique ne retire ou n'ajoute un projet sous un résumé existant.
Le parcours normal n'ajoute aucun appel LLM.

La lettre reçoit le CV final et les preuves des seuls projets retenus. HTML,
PDF et DOCX suivent les identifiants dans le même ordre et n'ajoutent jamais de
projet lors du rendu. La sélection provient de l'appel de personnalisation, qui
reçoit le profil complet et ses règles d'éligibilité.

Les raisons sont conservées dans `project_selection_reasons` du snapshot CV pour
audit, mais exclues des documents et des sources de relecture. Les anciens
snapshots sans raisons, y compris avec moins de trois ou plus de trois projets,
restent lisibles. Le minimum s'applique aux nouvelles générations ; les dossiers
existants nécessitent une régénération pour le respecter.
