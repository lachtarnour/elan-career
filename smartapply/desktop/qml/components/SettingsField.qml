import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: root
    required property var configuration
    property var drafts: ({})
    signal edited(string key, string value)
    readonly property string key: configuration.key
    readonly property string value: Object.prototype.hasOwnProperty.call(drafts, key) ? String(drafts[key]) : String(configuration.value || "")
    readonly property bool booleanField: ["CV_MERGE_SPARSE_SECONDARY_SKILLS", "WTTJ_INCLUDE_COMPANY_PROFILE", "WTTJ_SKIP_FAILED_JOBS"].indexOf(key) >= 0
    readonly property bool multiline: key === "DECLARED_EXPERIENCE" || key === "WTTJ_ANALYZER_METADATA_FIELDS"
    readonly property bool wide: multiline || ["DATABASE_URL", "PROFILE_DIR", "OUTPUT_DIR", "CACHE_DIR", "WTTJ_COOKIE", "FRANCETRAVAIL_SCOPE", "SERPAPI_UDS"].indexOf(key) >= 0
    readonly property string label: ({
        DATABASE_URL: "Connexion à la base de données", PROFILE_DIR: "Dossier du profil", OUTPUT_DIR: "Dossier des CV et lettres", CACHE_DIR: "Dossier du cache",
        LLM_CACHE_TTL_DAYS: "Conservation du cache IA (jours)", LLM_MAX_CONCURRENT: "Appels IA simultanés", TOP_K_RANKED: "Nombre d’offres par traitement",
        DEDUP_TITLE_THRESHOLD: "Similarité des titres (%)", DEDUP_DESC_THRESHOLD: "Similarité des descriptions (%)", DECLARED_EXPERIENCE: "Expérience déclarée au modèle",
        CV_MERGE_SPARSE_SECONDARY_SKILLS: "Regrouper les petites catégories de compétences", CV_SECONDARY_SKILL_BLOCK_MIN_SIZE: "Minimum de compétences par catégorie",
        OPENAI_CONTEXT_WINDOW_TOKENS: "Fenêtre de contexte OpenAI (tokens)", ANTHROPIC_CONTEXT_WINDOW_TOKENS: "Fenêtre de contexte Claude (tokens)", LOG_LEVEL: "Niveau des journaux",
        SERPAPI_API_KEY: "Clé API SerpAPI", SERPAPI_GOOGLE_DOMAIN: "Domaine Google", SERPAPI_HL: "Langues de recherche", SERPAPI_GL: "Code du pays",
        SERPAPI_DEFAULT_LOCATION: "Localisation par défaut", SERPAPI_MAX_PAGES: "Pages maximum par recherche", SERPAPI_LOW_RESULT_FALLBACK_TARGET: "Seuil de résultats pour élargir la recherche",
        SERPAPI_DATE_POSTED: "Ancienneté des offres", SERPAPI_UDS: "Filtres Google Jobs personnalisés",
        FRANCETRAVAIL_CLIENT_ID: "Identifiant de l’application", FRANCETRAVAIL_CLIENT_SECRET: "Secret de l’application", FRANCETRAVAIL_SCOPE: "Autorisations API", FRANCETRAVAIL_TIMEOUT: "Délai réseau maximum (secondes)",
        APIFY_TOKEN: "Jeton API Apify", LINKEDIN_CONTRACT_TYPE: "Types de contrat", LINKEDIN_EXPERIENCE_LEVEL: "Niveaux d’expérience", LINKEDIN_REMOTE: "Modes de travail",
        LINKEDIN_DATE_POSTED: "Ancienneté des offres", LINKEDIN_HTTP_TIMEOUT: "Délai réseau maximum (secondes)", LINKEDIN_POLL_WAIT_SECONDS: "Intervalle de suivi (secondes)",
        LINKEDIN_READ_MAX_ATTEMPTS: "Tentatives de lecture maximum", LINKEDIN_READ_FAILURE_BUDGET_SECONDS: "Durée maximum des reprises (secondes)", LINKEDIN_MAX_CONCURRENT_RUNS: "Collectes simultanées", LINKEDIN_MAX_RESULTS: "Résultats maximum par requête",
        WTTJ_COOKIE: "Cookie de connexion", WTTJ_MAX_PAGES: "Pages maximum", WTTJ_PAGES: "Nombre de pages à parcourir", WTTJ_PER_PAGE: "Offres par page",
        WTTJ_INCLUDE_COMPANY_PROFILE: "Inclure le profil de l’entreprise", WTTJ_SKIP_FAILED_JOBS: "Ignorer les offres indisponibles", WTTJ_TIMEOUT: "Délai réseau maximum (secondes)",
        WTTJ_DELAY_SECONDS: "Pause entre les requêtes (secondes)", WTTJ_ANALYZER_METADATA_FIELDS: "Informations transmises à l’analyse"
    })[key] || configuration.label
    Layout.fillWidth: true
    Layout.preferredWidth: 1
    Layout.columnSpan: wide && parent.columns > 1 ? 2 : 1
    spacing: 7

    RowLayout {
        Layout.fillWidth: true
        Text { Layout.fillWidth: true; text: root.label; color: Theme.inkSoft; font.pixelSize: 12; wrapMode: Text.WordWrap }
        Switch {
            objectName: root.booleanField ? "appSetting_" + root.key : ""
            visible: root.booleanField
            checked: root.value.toLowerCase() === "true"
            enabled: !AppBridge.llmConfigurationSaving
            Accessible.name: root.label
            onToggled: root.edited(root.key, checked ? "True" : "False")
            indicator: Rectangle {
                implicitWidth: 38; implicitHeight: 22; radius: 11
                x: parent.leftPadding; y: (parent.height - height) / 2
                color: parent.checked ? Theme.accent : Theme.lineStrong
                border.color: parent.visualFocus ? Theme.accentBright : "transparent"
                border.width: 2
                Rectangle { x: parent.parent.checked ? 19 : 3; y: 3; width: 16; height: 16; radius: 8; color: Theme.ink; Behavior on x { NumberAnimation { duration: Theme.motionFast } } }
            }
        }
    }
    AppField {
        objectName: !root.booleanField && !root.multiline ? "appSetting_" + root.key : ""
        visible: !root.booleanField && !root.multiline
        Layout.fillWidth: true
        text: root.value
        echoMode: root.configuration.secret ? TextInput.Password : TextInput.Normal
        placeholderText: root.configuration.secret && root.configuration.configured ? "Configuré · laisser vide pour conserver" : root.key.indexOf("CONTEXT_WINDOW") >= 0 ? "Automatique" : "Valeur"
        enabled: !AppBridge.llmConfigurationSaving
        Accessible.name: root.label
        onTextEdited: root.edited(root.key, text)
    }
    TextArea {
        objectName: root.multiline ? "appSetting_" + root.key : ""
        visible: root.multiline
        Layout.fillWidth: true
        Layout.preferredHeight: 86
        text: root.value
        padding: 12
        color: Theme.ink; selectionColor: Theme.accentSoft; font.pixelSize: 12
        wrapMode: TextEdit.Wrap
        selectByMouse: true
        enabled: !AppBridge.llmConfigurationSaving
        Accessible.name: root.label
        background: Rectangle { radius: Theme.radiusSmall; color: Theme.surfaceMuted; border.color: parent.activeFocus ? Theme.accent : Theme.line }
        onTextChanged: if (activeFocus && text !== root.value) root.edited(root.key, text)
    }
}
