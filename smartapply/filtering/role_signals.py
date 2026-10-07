"""Role-family and scope tokens used by the local filter."""

from __future__ import annotations

import re

REPORTING_BI_TOKENS = (
    "business intelligence",
    "dashboard",
    "dashboards",
    "ga4",
    "gtm",
    "looker",
    "lookml",
    "power bi",
    "power query",
    "qlik",
    "qliksense",
    "reporting",
    "tableau",
    "tableau de bord",
    "tableaux de bord",
    "informatique decisionnelle",
    "data visualization",
    "visualisation de donnees",
)

ANALYTICAL_OWNERSHIP_TOKENS = (
    "a/b",
    "ab testing",
    "aide a la decision",
    "analyse causale",
    "analyse exploratoire",
    "analyses ad hoc",
    "acquisition",
    "arima",
    "bigquery",
    "causal inference",
    "cohort",
    "cohorte",
    "conversion",
    "data warehouse",
    "dbt",
    "decision science",
    "econometrie",
    "engagement",
    "etl",
    "experimentation",
    "funnel",
    "forecasting",
    "hypothesis testing",
    "inference causale",
    "machine learning",
    "ml",
    "modele",
    "modeles",
    "modeling",
    "modelisation",
    "modélisation",
    "numpy",
    "pandas",
    "predictif",
    "predictifs",
    "predictive",
    "product analytics",
    "prevision",
    "prévision",
    "python",
    "pyspark",
    "qualite des donnees",
    "qualité des données",
    "recommandations actionnables",
    "restitution finale",
    "semantic layer",
    "segmentation",
    "spark",
    "sql",
    "statistical",
    "statistique",
    "statistics",
)

WEB_ANALYTICS_TRACKING_TOKENS = (
    "analyse web",
    "analytique web",
    "data layer",
    "ga4",
    "gestion des tags",
    "google analytics",
    "gtm",
    "marquage analytics",
    "mesure d audience",
    "plan de marquage",
    "suivi de conversion",
    "tagging plan",
    "tracking plan",
    "web analytics",
    "webanalyse",
)

CORE_DATA_TECH_TOKENS = (
    "dbt",
    "etl",
    "hugging face",
    "machine learning",
    "ml",
    "pandas",
    "python",
    "pyspark",
    "pytorch",
    "scikit-learn",
    "sklearn",
    "spark",
    "sql",
    "tensorflow",
)

NEGATED_CORE_DATA_TECH_TOKENS = (
    "no python",
    "without python",
    "sans python",
    "aucun python",
    "pas de python",
    "pas de developpement python",
    "pas de développement python",
    "no sql",
    "without sql",
    "sans sql",
    "aucun sql",
    "pas de sql",
    "no analytics ownership",
    "without analytics ownership",
    "sans ownership analytique",
    "sans responsabilite analytique",
    "sans responsabilité analytique",
)

DATA_ENGINEERING_PLATFORM_TOKENS = (
    "airflow",
    "data platform",
    "data warehouse",
    "databricks",
    "entrepot de donnees",
    "etl",
    "informatica",
    "integration de donnees",
    "orchestration de donnees",
    "plateforme data",
    "plateforme de donnees",
    "snowflake",
    "warehouse",
)

ML_ANALYTICS_SCOPE_TOKENS = (
    "agentic ai",
    "agents autonomes",
    "ai",
    "analytics",
    "apprentissage automatique",
    "apprentissage profond",
    "apprentissage statistique",
    "causal inference",
    "computer vision",
    "data science",
    "deep learning",
    "experimentation",
    "forecasting",
    "generative ai",
    "genai",
    "ia agentique",
    "ia generative",
    "ia générative",
    "inference causale",
    "intelligence artificielle",
    "llm",
    "machine learning",
    "ml",
    "modelisation statistique",
    "nlp",
    "python",
    "rag",
    "statistical learning",
    "statistical",
    "statistique",
    "statistics",
)

DATA_AI_ANCHOR_TOKENS = (
    "agentic ai",
    "agents autonomes",
    "ai",
    "artificial intelligence",
    "apprentissage automatique",
    "apprentissage statistique",
    "causal inference",
    "computer vision",
    "data pipeline",
    "data science",
    "deep learning",
    "genai",
    "ia agentique",
    "ia generative",
    "ia générative",
    "intelligence artificielle",
    "llm",
    "machine learning",
    "ml",
    "modelisation statistique",
    "nlp",
    "python",
    "rag",
    "statistical learning",
)

FINANCE_REPORTING_CONTEXT_TOKENS = (
    "budget",
    "budgeting",
    "controlling",
    "controle de gestion",
    "contrôle de gestion",
    "direction administrative et financière",
    "finance",
    "finance department",
    "financier",
    "financial planning",
    "management accounting",
    "fp&a",
)

_STRONG_DATA_AI_DEV_TITLE_RE = re.compile(
    r"(?<![a-z0-9])(?:ai|bi\s*/\s*etl|business\s+intelligence|data|"
    r"data\s+bi|donnees|données|etl|genai|ia|llm|machine\s+learning|"
    r"ml|power\s+bi|reporting\s+bi|talend)(?![a-z0-9])"
)
_SYSTEM_NETWORK_ADMIN_RE = re.compile(
    r"\badministrateur\b.{0,50}\b(?:systeme|système|reseau|réseau)\b"
)
_OFF_TARGET_TITLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("business", re.compile(r"\bbusiness\b")),
    (
        "qa/test",
        re.compile(
            r"\b(?:assurance\s+qualite|assurance\s+qualité|qa|quality assurance|"
            r"recetteur|testeur|testeuse)\b"
        ),
    ),
    ("cybersecurity", re.compile(r"\b(?:cybersecurite|cybersécurité)\b")),
    ("support", re.compile(r"\b(?:support|helpdesk|hotline|exploitation applicative)\b")),
    ("erp", re.compile(r"\b(?:erp|sage\s*x3)\b")),
    ("php", re.compile(r"\b(?:php|symfony|laravel|drupal|wordpress)\b")),
    ("cobol/as400", re.compile(r"\b(?:cobol|as\s*400|as400)\b")),
    ("frontend_backend", re.compile(r"\b(?:angular|react|node\.?js|spring)\b")),
    ("cplusplus", re.compile(r"\b(?:c\+\+|c#)\b")),
    ("embedded", re.compile(r"\b(?:embarque|embarqué|embarques|embarqués|fpga)\b")),
    ("system_network", re.compile(r"\b(?:systeme|système|reseau|réseau)\b")),
    ("administrator", re.compile(r"\badministrateur\b")),
    ("agility_it", re.compile(r"\b(?:agilite|agilité|operating model)\b")),
    ("automatisme", re.compile(r"\bautomatisme\b")),
    ("digital_learning", re.compile(r"\b(?:digital learning|pedagogique|pédagogique)\b")),
    ("actuarial", re.compile(r"\bactuariell?e?s?\b")),
    ("documentaliste", re.compile(r"\bdocumentaliste\b")),
    ("comptable", re.compile(r"\b(?:comptable|comptabilite|comptabilité)\b")),
    ("conducteur", re.compile(r"\bconducteur\b")),
    ("gestionnaire", re.compile(r"\bgestionnaire\b")),
)
_SOFTWARE_WITHOUT_DATA_AI_RE = re.compile(
    r"\b(?:developpeur|developpeuse|ingenieur logiciel|software engineer)\b"
)


def title_off_target_marker(title: str) -> str | None:
    if _SYSTEM_NETWORK_ADMIN_RE.search(title):
        return "system_network"
    for marker, pattern in _OFF_TARGET_TITLE_PATTERNS:
        if pattern.search(title):
            if marker == "system_network" and _STRONG_DATA_AI_DEV_TITLE_RE.search(title):
                continue
            if marker == "business" and re.search(
                r"\b(?:business data|data analyst|business intelligence|analytics)\b",
                title,
            ):
                continue
            return marker
    if _SOFTWARE_WITHOUT_DATA_AI_RE.search(title) and not _STRONG_DATA_AI_DEV_TITLE_RE.search(
        title
    ):
        return "software_engineering"
    return None


def should_skip_configured_title_hard_reject(title: str, blocker: str) -> bool:
    """Keep Data/AI dev titles from generic configured fullstack/dev blockers."""
    normalized = blocker.strip()
    if normalized in {"full stack", "full-stack", "fullstack"}:
        return bool(_STRONG_DATA_AI_DEV_TITLE_RE.search(title))
    if normalized == "expert":
        return bool(
            re.search(
                r"(?<![a-z0-9])(?:ai|data|genai|ia|intelligence\s+artificielle|llm|"
                r"machine\s+learning|ml)(?![a-z0-9])",
                title,
            )
        )
    return False


def has_analytics_title_scope(title: str) -> bool:
    return bool(
        re.search(
            r"(?<![a-z0-9])(?:analyste\s+(?:data|de\s+donnees|produit)|"
            r"analytics\s+engineer|business\s+analyst\s+data|data\s+analyst|"
            r"decision\s+scientist|ingenieur\s+analytics|product\s+analyst|"
            r"product\s+data\s+analyst)(?![a-z0-9])",
            title,
        )
    )
