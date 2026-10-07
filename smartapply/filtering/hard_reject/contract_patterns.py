"""Regex vocabulary for candidate-owned contract context detection."""

from __future__ import annotations

import re

_NON_CONTRACT_SCIENTIFIC_PATTERNS = (
    r"\bapprentissage\s+(?:automatique|statistique|(?:en\s+)?continu|a\s+fond|"
    r"profond|federe|machine|incremental|avec\s+peu\s+d[' ]?exemples|"
    r"supervise|non\s+supervise|par\s+renforcement|social)\b",
    r"\b(?:methodes?|algorithmes?|modeles?|techniques?|paradigmes?|processus)\s+"
    r"(?:d[' ]?|de\s+l[' ]?)apprentissage\b",
    r"\bmachine\s+learning\s*(?:/|et)\s*apprentissage\b",
    r"\bcapacite\s+d[' ]?apprentissage\b",
    r"\bculture\s+de\s+l[' ]?apprentissage\b",
    r"\barticulant\s+travail\s+et\s+apprentissage\b",
)

_NON_CONTRACT_STAGE_TITLE_PATTERNS = (
    r"\b(?:first|second|third|upper|lower|single|multi|two|three|kick)[ -]stage\b",
    r"\bearly[- ]stage\b",
    r"\bstage[- ]gate\b",
    r"\bstage\s+(?:propulsion|separation|rocket|engine|compressor|turbine|"
    r"lighting|production|manager|management)\b",
    r"\b(?:rocket|launch\s+vehicle|propulsion|engine)\s+stage\b",
)

_NON_CONTRACT_HISTORICAL_PATTERNS = (
    r"\bexperiences?\b[^.!?;]{0,240}"
    r"\b(?:acquis(?:e|es)?|gained|obtained|effectuee?|realisee?)\b"
    r"[^.!?;]{0,180}\b(?:stage|alternance|internship|apprenticeship|"
    r"work[- ]study)\b",
    r"\bexperiences?\b[^.!?;()]{0,100}\(\s*"
    r"(?:cdi|cdd|stage|alternance|freelance|interim|internship|"
    r"apprenticeship|temporary|permanent|fixed[- ]term)\b"
    r"(?:\s*(?:,|/|-|\bou\b|\bor\b)\s*"
    r"(?:cdi|cdd|stage|alternance|freelance|interim|internship|"
    r"apprenticeship|temporary|permanent|fixed[- ]term)\b){1,5}\s*\)",
)

_NON_CONTRACT_ROTATION_PATTERNS = (
    r"\b(?:astreinte|permanence|garde|rotation|roulement)\b"
    r"[^.!?;]{0,120}\ben\s+alternance\s+avec\b",
)

_BLOCKED_TERM_RE = re.compile(
    r"\b(?:cdd|fixed[- ]term|temporary|temporaire|interim|stage|stagiaire|"
    r"internship|intern|alternance|alternant(?:e)?|apprentissage|apprenti(?:e)?|"
    r"professionnalisation|contrat\s+pro|apprenticeship|apprentice|work[- ]study|co[- ]op|"
    r"traineeship|freelance|"
    r"freelancer|contractor|independant(?:e)?|independent|self[- ]employed|"
    r"part[- ]time|temps\s+partiel|mi[- ]temps)\b"
)
_PART_TIME_RE = re.compile(r"\b(?:part[- ]time|temps\s+partiel|mi[- ]temps)\b")
_FULL_TIME_RE = re.compile(r"\b(?:full[- ]time|temps\s+plein)\b")
_PERMANENT_TERM_RE = re.compile(
    r"\b(?:cdi|permanent|open[- ]ended|open\s+ended|indefinite)\b"
)
_CANDIDATE_CONTRACT_FRAME_RE = re.compile(
    r"\b(?:(?:this|the)\s+(?:role|position|job|employment)\s+(?:is|will\s+be)|"
    r"(?:role|position|job|employment)\s*[:=-]|"
    r"(?:poste|emploi|offre|contrat|recrutement)\s*(?:est|sera|:|=|-|en|de)|"
    r"vous\s+serez\s+(?:en|un|une)|you\s+will\s+be\s+(?:a|an)|"
    r"we\s+(?:will\s+)?(?:hire|recruit)\s+you\s+as|"
    r"you\s+(?:are|will\s+be)\s+(?:hired|recruited)\s+as|"
    r"vous\s+serez\s+(?:recrutee?|embauchee?)\s+(?:comme|en\s+tant\s+que|en)|"
    r"nous\s+vous\s+(?:recrutons|embauchons)\s+(?:comme|en\s+tant\s+que|en))\b"
)
_CANDIDATE_CONTRACT_FRAME_GAP_RE = re.compile(
    r"\s*(?:(?:an?|the|un|une|en|de)\s+)?$"
)
_CANDIDATE_OFFER_FRAME_RE = re.compile(
    r"\b(?:"
    r"this\s+is\s+(?:an?|the)\s+|"
    r"(?:join|rejoindre|rejoignez|rejoindrez)\s+(?:(?:our|the|notre)\s+)?"
    r"(?:team|equipe)\b.{0,80}\b(?:as|for|en\s+tant\s+que|pour)\b|"
    r"(?:we|our\s+team|nous|notre\s+equipe)\b.{0,35}"
    r"(?:hire|hiring|recruit|recruiting|look(?:ing)?\s+for|"
    r"recrute|recrutons|recherche|recherchons)\b"
    r")"
)
_THIRD_PARTY_ENTITY_RE = re.compile(
    r"\b(?:team|equipe|staff|personnel|employees?|collaborateurs?|collegues?|"
    r"consultants?|techniciens?|managers?)\b"
)
_THIRD_PARTY_RELATION_RE = re.compile(
    r"\b(?:manage|manages|managing|administer|administers|administration|gerer|"
    r"gerez|gestion)\b.{0,80}\b(?:contracts?|contrats?)\b.{0,80}"
    r"\b(?:of|des?|du|de\s+la)\b.{0,30}"
    r"\b(?:employees?|collaborateurs?|consultants?|staff|personnel)\b"
)
_THIRD_PARTY_DUTY_RE = re.compile(
    r"\b(?:mentor|mentoring|supervise|supervising|manage|manages|managing|"
    r"lead|leading|coach|coaching|oversee|oversees|overseeing|coordinate|"
    r"coordinates|coordinating|administer|administers|administering|"
    r"responsible\s+for|in\s+charge\s+of|responsable\s+de|chargee?\s+de|"
    r"encadrer|encadrement|superviser|manager|accueillir|tutorat)\b"
    r".{0,100}\b(?:team|equipe|interns?|stagiaires?|alternants?|apprentices?|"
    r"apprentis?|trainees?|consultants?|collaborateurs?|"
    r"(?:internship|apprenticeship|work[- ]study|co[- ]op)\s+program(?:me)?)\b"
)
_THIRD_PARTY_ROLE_TERM_RE = re.compile(
    r"\b(?:stagiaire|internship|intern|alternant(?:e)?|apprenti(?:e)?|"
    r"apprenticeship|apprentice|contractor)\b"
)
_THIRD_PARTY_DUTY_VERB_RE = re.compile(
    r"\b(?:mentor|mentoring|supervise|supervising|manage|manages|managing|"
    r"lead|leading|coach|coaching|oversee|oversees|overseeing|coordinate|"
    r"coordinates|coordinating|administer|administers|administering|"
    r"responsible\s+for|in\s+charge\s+of|responsable\s+de|chargee?\s+de|"
    r"encadrer|encadrement|superviser|manager|accueillir|tutorat|includes?|"
    r"comprend|comporte)\b"
)
_HISTORICAL_RE = re.compile(
    r"\b(?:experiences?|background|previous|prior|past|precedent(?:e)?|"
    r"auparavant|deja|gained|acquired|demonstrated)\b"
)
_OPTIONAL_RE = re.compile(
    r"\b(?:optional|option|possible|accepted|on\s+request|"
    r"can\s+be\s+discussed|open\s+to|willing\s+to|depending\s+on|"
    r"according\s+to|au\s+choix|a\s+discuter|selon|suivant|"
    r"en\s+fonction\s+de|a\s+la\s+demande|preferences?|possibles?)\b"
)
_MANDATORY_RE = re.compile(
    r"\b(?:only|uniquement|exclusivement|must|required|mandatory|obligatoire)\b"
)
_ALTERNATIVE_CONNECTOR_RE = re.compile(
    r"(?:\s+(?:ou|or)\s+|\s*/\s*|\beither\b|"
    r"\bcomme(?:\s+aux?\s+profils?)?\b|\bas\s+well\s+as\b|\balongside\b)"
)
_LEVEL_HEADING_RE = re.compile(r"\b(?:niveau|level|seniority)\b")
_LEVEL_MENU_FRAME_RE = re.compile(
    r"\b(?:"
    r"profils?\s+(?:acceptes?|acceptees?|ouverts?|ouvertes?|eligibles?)|"
    r"(?:ouvert(?:e)?s?\s+aux?|open\s+to)\s+(?:profils?|profiles?|applicants?)|"
    r"(?:accepted|eligible)\s+(?:profiles?|applicants?)|"
    r"profils?\s+(?:junior|intermediaire|confirme|debutant)\s+"
    r"(?:bienvenus?|acceptes?|eligibles?)"
    r")\b"
)
_COMPATIBLE_LEVEL_RE = re.compile(
    r"\b(?:junior|intermediaire|intermediate|confirme|mid(?:dle)?|entry[- ]level)\b"
)
_CLAUSE_SPLIT_RE = re.compile(r"([.!?;\n]+)")

_COMPOSITIONAL_MARKER_RE = re.compile(
    r"(?P<fixed_term>\b(?:cdd|fixed[- ]term|temporary|temporaire|interim)\b)|"
    r"(?P<internship>\b(?:stage|stagiaire|internship|intern)\b)|"
    r"(?P<apprenticeship>\b(?:alternance|alternant(?:e)?|apprentissage|"
    r"apprenti(?:e)?|professionnalisation|contrat\s+pro|apprenticeship|apprentice|"
    r"work[- ]study|co[- ]op|traineeship)\b)|"
    r"(?P<freelance>\b(?:freelance|freelancer|contractor)\b)|"
    r"(?P<independent>\b(?:independant(?:e)?|independent|self[- ]employed|"
    r"auto[- ]entrepreneur|micro[- ]entrepreneur|portage\s+salarial)\b)"
)
_FIXED_DURATION_CONTRACT_RE = re.compile(
    r"\b(?:"
    r"contract\s+(?:role|position|job)\b[^.!?\n]{0,80}"
    r"(?:lasts?\s+for|lasting|for|duration\s*(?:of|:))?\s*"
    r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"\s*(?:months?|years?)|"
    r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"[- ](?:months?|years?)[- ]contract|"
    r"contract\s+(?:of|for)\s+"
    r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"\s*(?:months?|years?)"
    r")\b"
)
_GENERIC_CONTRACT_ROLE_RE = re.compile(r"\bcontract\s+(?:role|position|job)\b")

_OFFER_ATTRIBUTE_PREFIX_RE = re.compile(
    r"\b(?:this|the|ce|cet|cette|le|la)\s+"
    r"(?:role|position|job|opportunity|employment|opening|poste|emploi|offre|"
    r"mission|opportunite)\s+"
    r"(?:is|will\s+be|est|sera|reste|comes?\s+as)\s+"
    r"(?:(?:an?|the|un|une|en|de|a)\s+)?"
    r"(?:(?:paid|unpaid|summer|winter|off[- ]cycle|full[- ]time|research|"
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d{1,2})"
    r"[- ](?:month|year)s?)\s+){0,3}$"
)
_CURRENT_OFFER_ASSERTION_PREFIX_RE = re.compile(
    r"\b(?:this\s+is|c[' ]?est)\s+"
    r"(?:(?:an?|the|un|une)\s+)?"
    r"(?:(?:paid|unpaid|summer|winter|off[- ]cycle|full[- ]time|research|"
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d{1,2})"
    r"[- ](?:month|year)s?)\s+){0,3}$"
)
_CONTRACT_LABEL_PREFIX_RE = re.compile(
    r"\b(?:type\s+(?:de|d[' ])\s*(?:contrat|recrutement|emploi|poste|collaboration)|"
    r"(?:contract|employment|job|engagement)\s+type|"
    r"mode\s+de\s+collaboration|collaboration\s+mode|"
    r"(?:employment\s+)?status|statut(?:\s+(?:d[' ]?emploi|du\s+poste))?|"
    r"conditions?\s+(?:de\s+)?travail|duree|duration|contrat|poste|emploi|"
    r"offre|mission|"
    r"role|position|job)\s*[:=-]?\s*"
    r"(?:(?:est|sera|is|will\s+be)\s+)?"
    r"(?:(?:propose|proposee|contractuel|externe)\s*)?"
    r"(?:[(:=-]\s*)?"
    r"(?:(?:en|de|d[' ]?|a\s+duree)\s+)?$"
)
_CURRENT_ROLE_CONTRACT_PREFIX_RE = re.compile(
    r"\b(?:(?:this|the)\s+(?:role|position|job|employment|opening)|"
    r"(?:ce|cet|cette|le|la)\s+(?:role|poste|emploi|offre|mission)|"
    r"(?:poste|emploi|offre|mission))\b"
    r"[^.!?;\n]{0,100}\b(?:is|will\s+be|est|sera|as|in|en)\s*$"
)
_RECRUITMENT_CONTRACT_PREFIX_RE = re.compile(
    r"\brecrutement\s+(?:(?:est|sera)\s+)?(?:propose|offert)\s+en\s*$"
)
_ATS_CONTRACT_VALUE_PREFIX_RE = re.compile(
    r"^\s*(?:externe|contractuel)\s*[-:=(]?\s*$"
)
_CONTRACT_ALTERNATIVE_PREFIX_RE = re.compile(
    r"\b(?:cdi|permanent|open[- ]ended|indefinite|fonctionnaire|titulaire)\b"
    r"[^.!?;\n]{0,35}(?:ou|or|/)\s+(?:en\s+)?$"
)
_OFFER_ACTION_RE = re.compile(
    r"\b(?:"
    r"(?:we|our\s+team)\b.{0,70}\b"
    r"(?:offer|offers|hire|hiring|recruit|recruiting|look(?:ing)?\s+for)|"
    r"(?:nous|notre\s+equipe)\b.{0,70}\b"
    r"(?:propose|proposons|offre|offrons|recrute|recrutons|recherche|recherchons)"
    r")\b"
)
_OFFER_ACTION_GAP_RE = re.compile(
    r"\s+(?:(?:for|pour)\s+)?(?:(?:an?|un|une)\s+)?"
    r"(?:(?:personne|candidat(?:e)?)\s+(?:en|comme)\s+)?"
    r"(?:(?:paid|unpaid|summer|winter|off[- ]cycle|full[- ]time|"
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"[- ](?:month|year)s?|\d{1,2}[- ](?:month|year)s?|"
    r"\d{1,2}\s+(?:mois|ans?))\s+){0,3}$"
)
_CANDIDATE_ACTIVITY_PREFIX_RE = re.compile(
    r"\b(?:"
    r"(?:you\s+(?:will|would)\s+)?(?:work|join|serve|operate)\b.{0,55}"
    r"\b(?:as|in)|"
    r"(?:join|joining)\b.{0,70}\b(?:as|for)|"
    r"vous\s+(?:interviendrez|travaillerez|rejoindrez|exercerez)\b.{0,55}"
    r"\b(?:en\s+tant\s+que|comme|en)"
    r")\s+(?:(?:an?|un|une)\s+)?$"
)
_CURRENT_OFFER_REFERENCE_PREFIX_RE = re.compile(
    r"\b(?:about\s+(?:this|the)|during\s+(?:this|the)|throughout\s+(?:this|the)|"
    r"by\s+the\s+end\s+of\s+(?:this|the)|ce|cet|cette|off[- ]cycle)\s+$"
)
_OPPORTUNITY_FOR_CANDIDATE_PREFIX_RE = re.compile(
    r"\b(?:opportunity|opportunite|opening|role|position|job|mission|offre|poste)\s+"
    r"(?:for|pour)\s+(?:(?:an?|un|une)\s+)?$"
)
_CANDIDATE_OPENING_PREFIX_RE = re.compile(
    r"\b(?:opportunity|opportunite|opening|role|position|job|mission|offre|"
    r"poste|emploi)\s+"
    r"(?:available|open|opened|ouvert(?:e)?s?|disponible)\s+"
    r"(?:for|to|pour|a)\s+(?:(?:an?|un|une)\s+)?$"
)
_CANDIDATE_SEARCH_PREFIX_RE = re.compile(
    r"(?:^|\b)(?:recherche|recherchons|seeking|wanted|looking\s+for)\s+"
    r"(?:(?:an?|un|une)\s+)?$"
)
_SHORT_ROLE_CONTRACT_PREFIX_RE = re.compile(
    r"^\s*(?=.{2,80}$)(?!.*[.!?;:])"
    r"[a-z0-9][a-z0-9+&/'’-]*(?:\s+[a-z0-9+&/'’-]+)*\s+en\s+$"
)
_NON_HEADLINE_STATUS_PREFIX_RE = re.compile(
    r"\b(?:our|their|nos|leurs?|l[' ]?equipe|the\s+team|staff|employees?|"
    r"collaborateurs?|consultants)\b|"
    r"\b(?:is|are|was|were|works?|includes?|est|sera|sont|seront|"
    r"travaille(?:nt)?|comprend|comprennent|inclut|incluent)\b"
)
_CANDIDATE_CONTRACT_HEADLINE_RE = re.compile(
    r"\b(?:opportunity|opening|role|position|job|mission|offre|poste)\s+"
    r"(?:for|pour)\s+(?:(?:an?|un|une)\s+)?(?P<marker>contract)\b"
    r"(?=\s+[a-z])"
)
_INDEPENDENT_STATUS_MARKER_RE = re.compile(
    r"(?:self[- ]employed|auto[- ]entrepreneur|micro[- ]entrepreneur|"
    r"portage\s+salarial)"
)
_INDEPENDENT_STATUS_PREFIX_RE = re.compile(
    r"\b(?:"
    r"consultant(?:e)?|contractor|freelancer|"
    r"travailleur|travailleuse|"
    r"mission|contrat|contract|"
    r"(?:employment\s+)?status|statut(?:\s+(?:d[' ]?emploi|du\s+poste))?|"
    r"contract\s+type|type\s+de\s+contrat"
    r")\s*(?:(?:as|en|de|d[' ])\s+)?[:=-]?\s*$"
)
_INDEPENDENT_STATUS_SUFFIX_RE = re.compile(
    r"^\s+(?:consultant|contractor|freelancer|worker|professional|status)\b"
)
_INDEPENDENT_NOUN_SUFFIX_RE = re.compile(
    r"^\s*(?:$|[.,;:()/-]|(?:pour|to|afin\s+de|who|qui)\b)"
)
_STAGE_PHASE_SUFFIX_RE = re.compile(r"^\s*(?:[- ]gate\b|of\b)")
_OFFER_NOUN_SUFFIX_RE = re.compile(
    r"^\s*(?:role|position|job|opportunity|opening|mission|contract)\b"
    r"(?!\s+(?:experience|background)\b)"
)
_CONTRACT_DETAIL_SUFFIX_RE = re.compile(
    r"^\s*(?:conventionne|de\s+fin\s+d[' ]?etudes|pfe|"
    r"(?:de|for|lasting)\s+(?:\d{1,2}|one|two|three|four|five|six|seven|"
    r"eight|nine|ten|eleven|twelve)\s*(?:mois|ans?|months?|years?))"
)
_DURATION_SUFFIX_RE = re.compile(
    r"^\s*(?:[-,(]\s*)?"
    r"(?:(?:de|for|lasting)\s+|d[' ]?)?"
    r"(?:\d{1,2}|un|une|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve)\s*(?:mois|ans?|months?|years?)\b"
)
_FIXED_TERM_ROLE_SUFFIX_RE = re.compile(
    r"^\s+(?:(?:(?:phd|doctoral|research)\s+)?contract\b"
    r"(?!\s+(?:experience|background)\b)|doctorant\b)"
)
_SEASONAL_CONTRACT_SUFFIX_RE = re.compile(
    r"^\s*(?:-\s*)?(?:pour\s+la\s+)?saison\b"
)
_OFFER_STATUS_SUFFIX_RE = re.compile(
    r"^\s+(?:(?:is|will\s+be|est|sera)\s+"
    r"(?:available|offered|proposed|disponible|proposee?|offerte?)|"
    r"(?:available|disponible)(?:\s+(?:now|immediately|maintenant))?)\b"
)
_EXCLUSIVE_CONTRACT_SUFFIX_RE = re.compile(
    r"^\s+(?:only|uniquement|exclusivement)\b"
)
_SELF_IDENTIFICATION_PREFIX_RE = re.compile(
    r"\b(?:as\s+(?:an?|the)|en\s+tant\s+que)\s*$"
)
_SELF_IDENTIFICATION_SUFFIX_RE = re.compile(
    r"^\s*,?\s*(?:you|vous)\s+"
    r"(?:will|would|are|allez|serez|developperez|interviendrez|rejoindrez|"
    r"travaillerez|exercerez)\b"
)
_SHORT_HEADING_PREFIX_RE = re.compile(
    r"^\s*[:=-]?\s*(?:(?:an?|the|un|une|paid|unpaid|summer|winter|off[- ]cycle|"
    r"full[- ]time|research|remuneree?|gratified?)\s+){0,4}$"
)
_SHORT_HEADING_SUFFIX_RE = re.compile(
    r"^\s*(?:"
    r",?\s*(?:paid|unpaid|remuneree?|gratified?)\s+"
    r"(?:(?:de|for|lasting)\s+)?"
    r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"\s*(?:mois|ans?|months?|years?)|"
    r",?\s*(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve)\s*(?:mois|ans?|months?|years?)|"
    r"(?:in|en|a|at)\s+(?!(?:experience|background|program(?:me)?|manager|"
    r"marketplace|culture)\b)[a-z][a-z0-9 '’-]{1,50}|"
    r"(?:-|\|)\s*(?!(?:experience|background|program(?:me)?|manager|"
    r"marketplace|culture)\b)[a-z][a-z0-9 '’-]{1,50}"
    r")\s*$"
)
_HISTORICAL_TOKEN_RE = re.compile(
    r"\b(?:previous|prior|past|precedent(?:e)?|auparavant|deja|experiences?|"
    r"background)\b"
)
_HISTORICAL_MARKER_SUFFIX_RE = re.compile(
    r"^\s*(?:experience|background|preferred|appreciated|welcome|accepted|"
    r"acceptee?|appreciee?|valorisee?)\b"
)
_OPTIONAL_MARKER_CONTEXT_RE = re.compile(
    r"\b(?:optional|option|on\s+request|can\s+be\s+"
    r"discussed|open\s+to(?:\s+conversations?\s+about)?|au\s+choix|"
    r"a\s+discuter|a\s+la\s+demande|selon|suivant|"
    r"preferences?|(?<!des\sque\s)(?<!as\ssoon\sas\s)possibles?)\b"
)
_NEGATED_MARKER_PREFIX_RE = re.compile(
    r"(?:\b(?:no|without|sans|aucun(?:e)?|pas\s+de)\s+|"
    r"\b(?:do|does|will)\s+not\s+(?:hire|recruit|accept)\s+"
    r"(?:(?:an?|any)\s+)?|"
    r"\bnot\s+(?:looking\s+for|hiring|recruiting)\s+(?:(?:an?|any)\s+)?|"
    r"\bne\s+(?:recrutons|recherchons|acceptons)\s+pas\s+(?:de\s+)?)$"
)
_NEGATED_MARKER_SUFFIX_RE = re.compile(
    r"^\s+(?:"
    r"(?:is|are)\s+not\s+(?:available|offered|accepted)|"
    r"(?:arrangements?|status|option)\b[^.!?;\n]{0,35}"
    r"(?:not\s+available|unavailable|excluded|not\s+offered)"
    r")\b"
)
_GENERIC_LISTING_RE = re.compile(
    r"\b(?:careers?\s+page|career\s+opportunities|nos\s+offres|"
    r"all\s+our\s+opportunities|types?\s+of\s+opportunities)\b"
)
_JOB_SEARCH_BOILERPLATE_RE = re.compile(
    r"\b(?:agences?|cabinets?)\b.{0,100}\b(?:interim|recrutement)\b|"
    r"\b(?:vous\s+)?accompagn(?:e|ons|ent)\b.{0,160}"
    r"\b(?:recherches?\s+(?:de|d[' ]?)\s*(?:mission|emploi)|"
    r"mission\s+d[' ]?interim)\b"
)
_RECRUITMENT_SERVICE_CONTEXT_RE = re.compile(
    r"\b(?:agence|cabinet|societe|entreprise|groupe|acteur|branche)\b"
    r"[^.!?;\n]{0,140}\b(?:recrutement|travail\s+temporaire)\b"
    r"[^.!?;\n]{0,160}\b(?:cdi|cdd|interim)\b|"
    r"\b(?:specialise|specialisee|specialises|specialisees|acteur|expert)\b"
    r"[^.!?;\n]{0,80}\brecrutement\b[^.!?;\n]{0,100}"
    r"\b(?:cdi|cdd|interim|travail\s+temporaire)\b|"
    r"\brecrutement\s+(?:en\s+)?(?:cdi|cdd|interim)\b"
    r"[^.!?;\n]{0,80}\b(?:cdi|cdd|interim)\b|"
    r"\b(?:propose|proposons|offers?)\b[^.!?;\n]{0,80}"
    r"\boffres?\s+d[' ]?emploi\b[^.!?;\n]{0,80}"
    r"\b(?:cdi|cdd|interim)\b[^.!?;\n]{0,30}(?:/|ou|or|,)"
    r"[^.!?;\n]{0,30}\b(?:cdi|cdd|interim)\b"
)
_ORGANIZATION_INDEPENDENCE_PREFIX_RE = re.compile(
    r"\b(?:company|subsidiary|group|firm|agency|organisation|organization|"
    r"societe|entreprise|filiale|groupe|acteur|cabinet|agence)\b"
    r"[^.!?;\n]{0,100}$"
)
_ORGANIZATION_INDEPENDENCE_SUFFIX_RE = re.compile(
    r"^\s+(?:company|subsidiary|group|firm|agency|organisation|organization|"
    r"societe|entreprise|filiale|groupe|acteur|cabinet|agence)\b"
)
_PAST_CANDIDATE_ACTION_RE = re.compile(
    r"\b(?:"
    r"you\s+have\s+(?:already\s+)?(?:built|delivered|created|completed)|"
    r"vous\s+avez\s+(?:deja\s+)?(?:concu|livre|realise|effectue)"
    r")\b"
)
_PROGRAM_RESPONSIBILITY_SUFFIX_RE = re.compile(
    r"^\s+program(?:me)?\s+(?:manager|management|coordinator|lead|leader|"
    r"director|owner|administrator|responsable|coordinateur|coordinatrice|"
    r"directeur|directrice|gestionnaire)\b"
)
_THIRD_PARTY_MANAGER_SUFFIX_RE = re.compile(
    r"^\s*(?:\(\s*)?"
    r"(?:qui\s+sera|who\s+(?:is|will\s+be))\s+"
    r"(?:ton|ta|votre|your)\s+manager\b(?:\s*\))?"
)
_EXPLICIT_OFFER_SUBJECT_PATTERN = (
    r"(?:we|nous|you|vous)\b|(?:our\s+team|notre\s+equipe)\b|"
    r"(?:this|the)\s+(?:role|position|job|opportunity|employment|opening)\b|"
    r"(?:ce|cet|cette|le|la)\s+(?:role|poste|emploi|offre|mission|opportunite)\b"
)
_SHARED_SUBJECT_PREDICATE_PATTERN = (
    r"(?:is|are|will|est|sera|offers?|propose|proposons|offre|offrons|"
    r"hire|hiring|recruit|recruiting|recrute|recrutons|work|join|serve|"
    r"operate|interviendrez|travaillerez|rejoindrez|exercerez)\b"
)
_PREDICATE_BOUNDARY_RE = re.compile(
    rf"(?:"
    rf"(?:,\s*)?\b(?:and|but|et|mais)\b\s+"
    rf"(?=(?:{_EXPLICIT_OFFER_SUBJECT_PATTERN}|"
    rf"{_SHARED_SUBJECT_PREDICATE_PATTERN}))|"
    rf"(?:,\s+|:\s+|\s+(?:--?|\||\*)\s+)"
    rf"(?=(?:{_EXPLICIT_OFFER_SUBJECT_PATTERN}))|"
    rf"(?:,\s*)?\b(?:while|whereas|tandis\s+que|alors\s+que|however|yet|"
    rf"cependant|toutefois)\b\s+"
    rf"(?=(?:{_EXPLICIT_OFFER_SUBJECT_PATTERN}))"
    rf")"
)
_COORDINATED_ATTRIBUTE_PREFIX_RE = re.compile(
    r"^\s*(?:is|will\s+be|est|sera)\s+"
    r"(?:(?:an?|the|un|une|en|de|a)\s+)?"
    r"(?:(?:paid|unpaid|summer|winter|off[- ]cycle|full[- ]time|research|"
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d{1,2})"
    r"[- ](?:month|year)s?)\s+){0,3}$"
)
_COORDINATED_OFFER_ACTION_RE = re.compile(
    r"\b(?:offers?|hire|hiring|recruit|recruiting|look(?:ing)?\s+for|"
    r"propose|proposons|offre|offrons|recrute|recrutons|recherche|recherchons)\b"
)
_COORDINATED_CANDIDATE_ACTIVITY_RE = re.compile(
    r"^\s*(?:"
    r"(?:work|join|serve|operate)\b.{0,55}\b(?:as|in)|"
    r"(?:interviendrez|travaillerez|rejoindrez|exercerez)\b.{0,55}"
    r"\b(?:en\s+tant\s+que|comme|en)"
    r")\s+(?:(?:an?|un|une)\s+)?$"
)
