import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root
    required property var configuration
    objectName: "llmCall_" + configuration.purpose
    property bool expanded: false
    property string provider: configuration.provider || ""
    property string modelName: configuration.model || ""
    property string reasoning: configuration.reasoning || ""
    property string maxOutputTokens: String(configuration.max_output_tokens || "")
    property var levels: AppBridge.llmReasoningOptions(provider, modelName)
    property var modelOptions: provider === "openai"
        ? ["gpt-5.4-mini", "gpt-5.4", "gpt-5.4-pro"]
        : provider === "anthropic" ? ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-4-6"] : []
    property bool customModel: modelName.length > 0 && modelOptions.indexOf(modelName) < 0
    property bool requestDetails: false
    property bool dirty: false
    readonly property bool modelIdValid: provider === "openai"
        ? /^(gpt-[\w.-]+|o[134][\w.-]*|ft:gpt-[\w.:-]+)$/.test(modelName.trim())
        : provider === "anthropic" && /^claude-[\w.-]+$/.test(modelName.trim())
    readonly property bool valid: (provider === "openai" || provider === "anthropic")
        && modelIdValid
        && /^\d+$/.test(maxOutputTokens) && Number(maxOutputTokens) >= 256 && Number(maxOutputTokens) <= 100000
        && (levels.length > 1 ? reasoning !== "auto" && levels.indexOf(reasoning) > 0 : reasoning === "auto")
    readonly property string description: ({
        job_scope_screening: "Vérifie que les missions entrent dans votre recherche.",
        job_offer_extraction: "Identifie les missions, compétences et contraintes de l’offre.",
        job_fit_assessment: "Compare les exigences de l’offre aux éléments de votre profil.",
        cv_adaptation: "Sélectionne les projets, compétences et certificats du CV.",
        application_texts: "Rédige le résumé du CV et la lettre de motivation.",
        document_review: "Vérifie les affirmations et la cohérence des documents."
    })[configuration.purpose] || ""
    signal edited()
    signal expansionRequested()
    Layout.fillWidth: true
    implicitHeight: body.implicitHeight + 2
    radius: Theme.radiusMedium
    color: expanded ? Theme.surface : Theme.canvasLift
    border.color: expanded ? Theme.accentLine : cardHover.hovered ? Theme.lineStrong : Theme.line

    function value() {
        return {purpose: configuration.purpose, provider: provider, model: modelName,
            reasoning: reasoning, max_output_tokens: maxOutputTokens}
    }
    function markEdited() { dirty = true; edited() }
    function effortLabel(value) {
        return ({auto: "Automatique", none: "Sans", minimal: "Minimal", low: "Faible", medium: "Moyen",
            high: "Élevé", xhigh: "Très élevé", max: "Maximum"})[value] || value || "À choisir"
    }
    function modelLabel(value) {
        return ({"gpt-5.4-mini": "GPT-5.4 mini", "gpt-5.4": "GPT-5.4", "gpt-5.4-pro": "GPT-5.4 Pro",
            "claude-haiku-5-5": "Claude Haiku 5.5", "claude-sonnet-5-5": "Claude Sonnet 5.5",
            "claude-opus-4-6": "Claude Opus 4.6"})[value] || value || "Modèle à choisir"
    }
    function modelChoices() {
        let choices = modelOptions.map(function(value) { return {label: modelLabel(value), value: value} })
        choices.push({label: "Autre modèle…", value: "custom"})
        return choices
    }
    function selectModel(index) {
        if (index === modelOptions.length) {
            customModel = true
            modelName = ""; reasoning = ""
            customModelField.forceActiveFocus()
        } else {
            customModel = false
            modelName = modelOptions[index]
            applyRecommendedSettings()
        }
        markEdited()
    }
    function applyRecommendedSettings() {
        const purpose = configuration.purpose
        if (purpose === "job_scope_screening" && provider === "anthropic" && modelName === "claude-haiku-5-5") {
            reasoning = "high"; maxOutputTokens = "16000"
        } else if (purpose === "job_offer_extraction" && provider === "openai" && modelName === "gpt-5.4-mini") {
            reasoning = "medium"; maxOutputTokens = "16000"
        } else if (purpose === "job_fit_assessment" && provider === "openai" && modelName === "gpt-5.4-mini") {
            reasoning = "medium"; maxOutputTokens = "32000"
        } else if (purpose === "job_fit_assessment" && provider === "anthropic" && modelName === "claude-haiku-5-5") {
            reasoning = "high"; maxOutputTokens = "60000"
        } else if (["cv_adaptation", "application_texts", "document_review"].indexOf(purpose) >= 0
                && provider === "openai" && modelName === "gpt-5.4") {
            reasoning = purpose === "cv_adaptation" || purpose === "application_texts" ? "medium" : "high"
            maxOutputTokens = "32000"
        } else { reasoning = "" }
    }

    HoverHandler { id: cardHover }
    ColumnLayout {
        id: body
        width: parent.width - 2
        x: 1; y: 1
        spacing: 0
        Item {
            objectName: "llmCallHeader_" + root.configuration.purpose
            Layout.fillWidth: true
            implicitHeight: headerContent.implicitHeight + 32
            activeFocusOnTab: true
            Accessible.role: Accessible.Button
            Accessible.name: root.configuration.label + (root.expanded ? " · Replier" : " · Modifier")
            Accessible.onPressAction: root.expansionRequested()
            Rectangle { anchors.fill: parent; radius: Theme.radiusMedium; color: parent.activeFocus ? Theme.accentSoft : "transparent" }
            RowLayout {
                id: headerContent
                anchors.left: parent.left; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                anchors.margins: 18
                spacing: 14
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 5
                    Text {
                        Layout.fillWidth: true
                        text: root.configuration.label
                        color: Theme.ink; font.pixelSize: 14; font.weight: Font.DemiBold
                        wrapMode: Text.WordWrap
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.expanded ? root.description : root.modelLabel(root.modelName)
                            + "  ·  " + root.effortLabel(root.reasoning)
                            + "  ·  " + Number(root.maxOutputTokens || 0).toLocaleString(Qt.locale("fr_FR"), "f", 0) + " tokens"
                        color: Theme.inkMuted; font.pixelSize: 12; wrapMode: Text.WordWrap
                    }
                }
                Text {
                    visible: root.dirty || Boolean(root.configuration.error)
                    text: root.dirty ? "Modifié" : "À configurer"
                    color: root.dirty ? Theme.warning : Theme.danger
                    font.pixelSize: 11
                }
                SvgIcon {
                    source: Theme.icon("chevron-right")
                    color: root.expanded ? Theme.accentBright : Theme.inkFaint
                    rotation: root.expanded ? 90 : 0
                    Layout.preferredWidth: 16; Layout.preferredHeight: 16
                }
            }
            HoverHandler { cursorShape: Qt.PointingHandCursor }
            TapHandler { onTapped: root.expansionRequested() }
            Keys.onReturnPressed: root.expansionRequested()
            Keys.onEnterPressed: root.expansionRequested()
            Keys.onSpacePressed: root.expansionRequested()
        }
        ColumnLayout {
            visible: root.expanded
            Layout.fillWidth: true
            Layout.leftMargin: 18; Layout.rightMargin: 18; Layout.bottomMargin: 18
            spacing: 12
            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
            GridLayout {
                Layout.fillWidth: true
                columns: width >= 620 ? 2 : 1
                uniformCellWidths: true
                columnSpacing: 18; rowSpacing: 16
                ColumnLayout {
                    Layout.fillWidth: true; Layout.preferredWidth: 1; spacing: 6
                    Text { text: "Fournisseur"; color: Theme.inkSoft; font.pixelSize: 12 }
                    AppSelect {
                        objectName: "llmProvider_" + root.configuration.purpose
                        Layout.fillWidth: true
                        model: [{label: "OpenAI", value: "openai"}, {label: "Claude · Anthropic", value: "anthropic"}]
                        textRole: "label"
                        currentIndex: root.provider === "openai" ? 0 : root.provider === "anthropic" ? 1 : -1
                        displayText: currentIndex >= 0 ? currentText : "Choisir un fournisseur"
                        enabled: !AppBridge.llmConfigurationSaving
                        Accessible.name: "Fournisseur · " + root.configuration.label
                        onActivated: {
                            root.provider = model[currentIndex].value
                            root.modelName = ""; root.reasoning = ""; root.customModel = false
                            root.markEdited()
                        }
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true; Layout.preferredWidth: 1; spacing: 6
                    RowLayout {
                        Layout.fillWidth: true
                        Text { Layout.fillWidth: true; text: root.customModel ? "Identifiant du modèle" : "Modèle"; color: Theme.inkSoft; font.pixelSize: 12 }
                        Text {
                            visible: root.customModel
                            text: "Voir la liste"
                            color: Theme.accentBright; font.pixelSize: 11
                            activeFocusOnTab: visible
                            Accessible.role: Accessible.Button
                            Accessible.name: "Revenir à la liste des modèles"
                            Accessible.onPressAction: root.customModel = false
                            HoverHandler { cursorShape: Qt.PointingHandCursor }
                            TapHandler { onTapped: root.customModel = false }
                            Keys.onReturnPressed: root.customModel = false
                            Keys.onSpacePressed: root.customModel = false
                        }
                    }
                    AppSelect {
                        objectName: "llmModelSelect_" + root.configuration.purpose
                        visible: !root.customModel
                        Layout.fillWidth: true
                        model: root.modelChoices()
                        textRole: "label"
                        currentIndex: root.modelOptions.indexOf(root.modelName)
                        displayText: currentIndex >= 0 ? currentText : root.modelName || "Choisir un modèle"
                        enabled: root.provider.length > 0 && !AppBridge.llmConfigurationSaving
                        Accessible.name: "Modèle · " + root.configuration.label
                        onActivated: root.selectModel(currentIndex)
                    }
                    AppField {
                        id: customModelField
                        objectName: "llmModel_" + root.configuration.purpose
                        visible: root.customModel
                        Layout.fillWidth: true
                        text: root.modelName
                        placeholderText: "Identifiant fourni par l’API"
                        enabled: !AppBridge.llmConfigurationSaving
                        Accessible.name: "Identifiant personnalisé · " + root.configuration.label
                        onTextEdited: { root.modelName = text; root.applyRecommendedSettings(); root.markEdited() }
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true; Layout.preferredWidth: 1; spacing: 6
                    Text { text: "Raisonnement"; color: Theme.inkSoft; font.pixelSize: 12 }
                    AppSelect {
                        objectName: "llmReasoning_" + root.configuration.purpose
                        Layout.fillWidth: true
                        model: root.levels.map(function(value) { return {label: root.effortLabel(value), value: value} })
                        textRole: "label"
                        currentIndex: root.levels.indexOf(root.reasoning)
                        displayText: currentIndex >= 0 ? currentText : root.reasoning ? "Niveau incompatible" : "Choisir un niveau"
                        enabled: root.modelName.length > 0 && !AppBridge.llmConfigurationSaving
                        Accessible.name: "Raisonnement · " + root.configuration.label
                        onActivated: { root.reasoning = root.levels[currentIndex]; root.markEdited() }
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true; Layout.preferredWidth: 1; spacing: 6
                    Text { text: "Limite de sortie (tokens)"; color: Theme.inkSoft; font.pixelSize: 12 }
                    AppField {
                        objectName: "llmMaxOutputTokens_" + root.configuration.purpose
                        Layout.fillWidth: true
                        text: root.maxOutputTokens
                        inputMethodHints: Qt.ImhDigitsOnly
                        placeholderText: "256 à 100 000"
                        enabled: !AppBridge.llmConfigurationSaving
                        Accessible.name: "Limite de sortie · " + root.configuration.label
                        onTextEdited: { root.maxOutputTokens = text; root.markEdited() }
                    }
                }
            }
            Text { text: "La limite inclut le raisonnement et la réponse."; color: Theme.inkFaint; font.pixelSize: 11; Layout.fillWidth: true; wrapMode: Text.WordWrap }
            Text {
                Layout.fillWidth: true
                objectName: "llmConfigurationError_" + root.configuration.purpose
                visible: !root.dirty && Boolean(root.configuration.error)
                text: root.configuration.error || ""
                color: Theme.danger; font.pixelSize: 12; wrapMode: Text.WordWrap
            }
            Text {
                Layout.fillWidth: true
                visible: !root.valid && (root.dirty || !root.configuration.error)
                text: root.modelName.length > 0 && !root.modelIdValid
                    ? "L’identifiant du modèle ne correspond pas au fournisseur choisi."
                    : "Complétez les quatre réglages pour enregistrer cet appel."
                color: Theme.danger; font.pixelSize: 12; wrapMode: Text.WordWrap
            }
            AppButton {
                visible: Boolean(root.configuration.last_request)
                text: root.requestDetails ? "Masquer le dernier appel" : "Dernier appel API"
                quiet: true; refined: true
                iconSource: Theme.icon("analysis")
                onClicked: root.requestDetails = !root.requestDetails
            }
            Text {
                Layout.fillWidth: true
                visible: root.requestDetails && Boolean(root.configuration.last_request)
                text: {
                    const request = root.configuration.last_request
                    if (!request) return ""
                    const effort = request.configuration_recorded ? (request.reasoning || "défaut de l’API") : "non enregistré"
                    return "Modèle retourné : " + request.model
                        + (request.requested_model ? " · demandé : " + request.requested_model : "")
                        + "\nRaisonnement envoyé : " + effort + "\n" + request.at
                }
                color: Theme.inkMuted; font.pixelSize: 11; wrapMode: Text.WordWrap
            }
        }
    }
}
