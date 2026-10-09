import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    objectName: "settingsPage"
    signal navigateRequested(string route)
    property string selectedSection: "ai"
    property string callGroup: "analysis"
    property string expandedCall: ""
    property string keyProvider: "openai"
    property bool keyEdited: false
    property bool callsEdited: false
    property bool callsValid: true
    property string llmError: ""
    property string savingSection: ""
    property var callConfigurations: []
    property var appConfigurations: []
    property var appEdits: ({})
    readonly property bool configurationIncomplete: callConfigurations.some(function(call) { return Boolean(call.error) })
    readonly property var sections: [
        {key: "ai", label: "Modèles IA", shortLabel: "Modèles", icon: "sparkle"},
        {key: "connections", label: "Connexions", shortLabel: "Connexions", icon: "command"},
        {key: "app", label: "Application", shortLabel: "Application", icon: "sliders"},
        {key: "files", label: "Données locales", shortLabel: "Données", icon: "folder"}
    ]
    readonly property var sourceGroups: [
        {key: "serpapi", title: "Google Jobs", caption: "Recherche via SerpAPI", prefixes: ["SERPAPI_"]},
        {key: "francetravail", title: "France Travail", caption: "Connexion à l’API des offres", prefixes: ["FRANCETRAVAIL_"]},
        {key: "linkedin", title: "LinkedIn", caption: "Collecte via Apify", prefixes: ["APIFY_", "LINKEDIN_"]},
        {key: "welcometothejungle", title: "Welcome to the Jungle", caption: "Offres liées à votre compte", prefixes: ["WTTJ_"]}
    ]

    component SettingsNavItem: Rectangle {
        required property var section
        property bool compact: false
        readonly property bool selected: root.selectedSection === section.key
        signal chosen()
        objectName: "settingsNav_" + section.key + (compact ? "_compact" : "")
        implicitHeight: 44
        radius: Theme.radiusSmall
        color: selected ? Theme.accentSoft : navHover.hovered ? Theme.surfaceHover : "transparent"
        border.color: activeFocus ? Theme.accent : "transparent"
        activeFocusOnTab: true
        Accessible.role: Accessible.Button
        Accessible.name: section.label
        Accessible.onPressAction: chosen()
        RowLayout {
            anchors.fill: parent; anchors.leftMargin: 12; anchors.rightMargin: 12
            spacing: 10
            SvgIcon { source: Theme.icon(section.icon); color: selected ? Theme.accentBright : Theme.inkMuted; Layout.preferredWidth: 16; Layout.preferredHeight: 16 }
            Text { Layout.fillWidth: true; text: compact ? section.shortLabel : section.label; color: selected ? Theme.ink : Theme.inkMuted; font.pixelSize: 13; font.weight: selected ? Font.DemiBold : Font.Medium; elide: Text.ElideRight }
        }
        HoverHandler { id: navHover; cursorShape: Qt.PointingHandCursor }
        TapHandler { onTapped: chosen() }
        Keys.onReturnPressed: chosen()
        Keys.onEnterPressed: chosen()
        Keys.onSpacePressed: chosen()
    }

    function isAnalysis(purpose) {
        return ["job_scope_screening", "job_offer_extraction", "job_fit_assessment"].indexOf(purpose) >= 0
    }
    function callValues() {
        let values = []
        for (let index = 0; index < callRepeater.count; index++) {
            const editor = callRepeater.itemAt(index)
            if (editor && editor.dirty) values.push(editor.value())
        }
        return values
    }
    function callEdited() {
        callsEdited = true
        callsValid = true
        for (let index = 0; index < callRepeater.count; index++) {
            const editor = callRepeater.itemAt(index)
            if (editor && editor.dirty && !editor.valid) callsValid = false
        }
    }
    function editApplication(key, value) {
        let next = Object.assign({}, appEdits)
        next[key] = value
        appEdits = next
    }
    function fieldsForGroups(groups) {
        return appConfigurations.filter(function(field) { return groups.indexOf(field.group) >= 0 })
    }
    function fieldsForSource(prefixes) {
        return appConfigurations.filter(function(field) {
            return prefixes.some(function(prefix) { return field.key.indexOf(prefix) === 0 })
        })
    }
    function selectedKeyProvider() { return keyProvider }
    function chooseKeyProvider(provider) {
        if (keyProvider === provider) return
        keyProvider = provider
        apiKeyField.text = ""
        keyEdited = false
        llmError = ""
    }
    function applyLlmConfiguration(force) {
        const current = AppBridge.llmConfiguration || {}
        if (!callsEdited || force) {
            callConfigurations = (current.calls || []).map(function(call) { return Object.assign({}, call) })
            callsEdited = false
            callsValid = true
            const invalid = callConfigurations.find(function(call) { return Boolean(call.error) })
            if (invalid) {
                expandedCall = invalid.purpose
                callGroup = isAnalysis(invalid.purpose) ? "analysis" : "documents"
            }
        }
        if (Object.keys(appEdits).length === 0 || force) appConfigurations = current.app_fields || []
    }
    function sourceInfo(key) {
        return (AppBridge.diagnostics.sources || []).find(function(source) { return source.key === key }) || {}
    }
    function sourceStatusText(source) {
        if (source.state === "available") return "Disponible"
        if (source.state === "checking") return "Vérification…"
        if (source.state === "unavailable") return "Indisponible"
        return "À vérifier"
    }
    function sourceStatusTone(source) {
        if (source.state === "available") return "success"
        if (source.state === "unavailable") return "danger"
        return "neutral"
    }

    Component.onCompleted: { applyLlmConfiguration(false); AppBridge.refreshLlmConfiguration() }
    Connections {
        target: AppBridge
        function onLlmConfigurationChanged() { root.applyLlmConfiguration(false) }
        function onLlmConfigurationSaved() {
            if (root.savingSection === "calls") root.callsEdited = false
            else if (root.savingSection === "app") root.appEdits = ({})
            else { root.keyEdited = false; apiKeyField.text = "" }
            root.savingSection = ""
            root.llmError = ""
            root.applyLlmConfiguration(false)
        }
        function onLlmConfigurationSaveFailed(message) { root.llmError = message }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0
        PageHeader {
            Layout.fillWidth: true
            Layout.leftMargin: 28; Layout.rightMargin: 28; Layout.topMargin: 20; Layout.bottomMargin: 18
            title: "Réglages"
            AppButton {
                text: AppBridge.diagnosticsChecking ? "Vérification…" : "Actualiser"
                iconSource: Theme.icon("refresh")
                quiet: true
                enabled: !AppBridge.diagnosticsChecking
                onClicked: { AppBridge.refreshDiagnostics(); AppBridge.refreshLlmConfiguration() }
            }
        }
        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
        RowLayout {
            visible: root.width < 850
            Layout.fillWidth: true
            Layout.leftMargin: 20; Layout.rightMargin: 20; Layout.topMargin: 12; Layout.bottomMargin: 12
            spacing: 6
            Repeater {
                model: root.sections
                delegate: SettingsNavItem {
                    required property var modelData
                    section: modelData; compact: true
                    Layout.fillWidth: true; Layout.preferredWidth: 1
                    onChosen: root.selectedSection = section.key
                }
            }
        }
        RowLayout {
            Layout.fillWidth: true; Layout.fillHeight: true
            spacing: 0
            ColumnLayout {
                visible: root.width >= 850
                Layout.preferredWidth: 206; Layout.minimumWidth: 206; Layout.maximumWidth: 206
                Layout.fillHeight: true
                Layout.leftMargin: 20; Layout.rightMargin: 18; Layout.topMargin: 24
                spacing: 6
                Repeater {
                    model: root.sections
                    delegate: SettingsNavItem {
                        required property var modelData
                        section: modelData
                        Layout.fillWidth: true
                        onChosen: root.selectedSection = section.key
                    }
                }
                Item { Layout.fillHeight: true }
                Text { Layout.leftMargin: 12; Layout.bottomMargin: 24; text: "Élan · macOS"; color: Theme.inkFaint; font.pixelSize: 11 }
            }
            Rectangle { visible: root.width >= 850; Layout.preferredWidth: 1; Layout.fillHeight: true; color: Theme.line }
            ColumnLayout {
                Layout.fillWidth: true; Layout.fillHeight: true
                spacing: 0
                Flickable {
                    id: settingsScroll
                    objectName: "settingsScroll"
                    Layout.fillWidth: true; Layout.fillHeight: true
                    contentWidth: width
                    contentHeight: sectionContent.implicitHeight + 56
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: AppScrollBar { }
                    ColumnLayout {
                        id: sectionContent
                        width: Math.max(0, Math.min(920, parent.width - Theme.scrollGutter - 64))
                        x: (parent.width - Theme.scrollGutter - width) / 2
                        y: 28
                        spacing: 24

                        ColumnLayout {
                            objectName: "settingsAiSection"
                            visible: root.selectedSection === "ai"
                            Layout.fillWidth: true
                            spacing: 20
                            SectionTitle { title: "Modèles IA"; caption: "Un modèle pour chaque étape de votre candidature." }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 8
                                AppButton {
                                    objectName: "settingsCalls_analysis"
                                    text: "Analyse des offres"
                                    kind: root.callGroup === "analysis" ? "primary" : "secondary"
                                    refined: true
                                    quiet: root.callGroup !== "analysis"
                                    onClicked: root.callGroup = "analysis"
                                }
                                AppButton {
                                    objectName: "settingsCalls_documents"
                                    text: "CV et lettres"
                                    kind: root.callGroup === "documents" ? "primary" : "secondary"
                                    refined: true
                                    quiet: root.callGroup !== "documents"
                                    onClicked: root.callGroup = "documents"
                                }
                                Item { Layout.fillWidth: true }
                            }
                            Text {
                                visible: root.callGroup === "documents"
                                Layout.fillWidth: true
                                text: "Les corrections éventuelles conservent les mêmes paramètres que l’appel initial."
                                color: Theme.inkMuted; font.pixelSize: 12; wrapMode: Text.WordWrap
                            }
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 10
                                Repeater {
                                    id: callRepeater
                                    model: root.callConfigurations
                                    delegate: LlmCallSettings {
                                        required property var modelData
                                        configuration: modelData
                                        visible: root.isAnalysis(modelData.purpose) === (root.callGroup === "analysis")
                                        expanded: root.expandedCall === modelData.purpose
                                        onExpansionRequested: root.expandedCall = expanded ? "" : modelData.purpose
                                        onEdited: root.callEdited()
                                    }
                                }
                            }
                        }

                        ColumnLayout {
                            objectName: "settingsConnectionsSection"
                            visible: root.selectedSection === "connections"
                            Layout.fillWidth: true
                            spacing: 24
                            SectionTitle { title: "Connexions"; caption: "Les accès aux fournisseurs IA et aux sources d’offres." }
                            Surface {
                                Layout.fillWidth: true
                                padding: 20
                                radiusValue: Theme.radiusMedium
                                surfaceColor: Theme.canvasLift
                                RowLayout {
                                    Layout.fillWidth: true
                                    Text { Layout.fillWidth: true; text: "Fournisseurs IA"; color: Theme.ink; font.pixelSize: 14; font.weight: Font.DemiBold }
                                    Pill {
                                        text: ((AppBridge.llmConfiguration || {}).api_keys || {})[root.keyProvider] ? "Clé configurée" : "Clé manquante"
                                        tone: ((AppBridge.llmConfiguration || {}).api_keys || {})[root.keyProvider] ? "success" : "warning"
                                        compact: true
                                    }
                                }
                                RowLayout {
                                    spacing: 8
                                    AppButton { text: "OpenAI"; kind: root.keyProvider === "openai" ? "primary" : "secondary"; refined: true; quiet: root.keyProvider !== "openai"; enabled: !AppBridge.llmConfigurationSaving; onClicked: root.chooseKeyProvider("openai") }
                                    AppButton { text: "Claude"; kind: root.keyProvider === "anthropic" ? "primary" : "secondary"; refined: true; quiet: root.keyProvider !== "anthropic"; enabled: !AppBridge.llmConfigurationSaving; onClicked: root.chooseKeyProvider("anthropic") }
                                }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 7
                                    Text { text: "Nouvelle clé API"; color: Theme.inkSoft; font.pixelSize: 12 }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 12
                                        AppField {
                                            id: apiKeyField
                                            objectName: "llmApiKeyField"
                                            Layout.fillWidth: true
                                            echoMode: TextInput.Password
                                            placeholderText: ((AppBridge.llmConfiguration || {}).api_keys || {})[root.keyProvider] ? "Laisser vide pour conserver la clé actuelle" : "Ajouter une clé API"
                                            enabled: !AppBridge.llmConfigurationSaving
                                            Accessible.name: "Nouvelle clé API " + root.keyProvider
                                            onTextEdited: root.keyEdited = true
                                        }
                                        AppButton {
                                            objectName: "saveLlmApiKeyButton"
                                            text: AppBridge.llmConfigurationSaving && root.savingSection === "credentials" ? "Enregistrement…" : "Enregistrer la clé"
                                            kind: "primary"; refined: true
                                            enabled: root.keyEdited && apiKeyField.text.trim().length > 0 && !AppBridge.llmConfigurationSaving
                                            onClicked: { root.llmError = ""; root.savingSection = "credentials"; AppBridge.saveLlmApiKey(root.selectedKeyProvider(), apiKeyField.text) }
                                        }
                                    }
                                }
                                Text {
                                    Layout.fillWidth: true
                                    visible: root.llmError.length > 0 && root.savingSection === "credentials"
                                    text: root.llmError; color: Theme.danger; font.pixelSize: 12; wrapMode: Text.WordWrap
                                }
                            }
                            ColumnLayout {
                                objectName: "settingsSourcesSection"
                                Layout.fillWidth: true
                                spacing: 10
                                SectionTitle { title: "Sources d’offres" }
                                Repeater {
                                    model: root.sourceGroups
                                    delegate: SettingsGroup {
                                        required property var modelData
                                        readonly property var status: root.sourceInfo(modelData.key)
                                        title: modelData.title
                                        caption: status.state === "unavailable" && status.message ? status.message : modelData.caption
                                        collapsible: true
                                        statusLabel: root.sourceStatusText(status)
                                        statusTone: root.sourceStatusTone(status)
                                        fields: root.fieldsForSource(modelData.prefixes)
                                        drafts: root.appEdits
                                        onEdited: (key, value) => root.editApplication(key, value)
                                    }
                                }
                            }
                        }

                        ColumnLayout {
                            objectName: "settingsApplicationSection"
                            visible: root.selectedSection === "app"
                            Layout.fillWidth: true
                            spacing: 18
                            SectionTitle { title: "Application"; caption: "Traitements, documents et préférences techniques." }
                            SettingsGroup {
                                title: "Traitements et cache"
                                fields: root.fieldsForGroups(["Cache et pipeline"])
                                drafts: root.appEdits
                                onEdited: (key, value) => root.editApplication(key, value)
                            }
                            SettingsGroup {
                                title: "Présentation des CV"
                                fields: root.fieldsForGroups(["Documents"])
                                drafts: root.appEdits
                                onEdited: (key, value) => root.editApplication(key, value)
                            }
                            SettingsGroup {
                                title: "Paramètres avancés"
                                caption: "Fenêtres de contexte et journalisation."
                                collapsible: true
                                fields: root.fieldsForGroups(["Fenêtres de contexte", "Journalisation"])
                                drafts: root.appEdits
                                onEdited: (key, value) => root.editApplication(key, value)
                            }
                        }

                        ColumnLayout {
                            objectName: "settingsFilesSection"
                            visible: root.selectedSection === "files"
                            Layout.fillWidth: true
                            spacing: 18
                            SectionTitle { title: "Données locales"; caption: "Les fichiers utilisés par Élan, accessibles en un clic." }
                            Surface {
                                Layout.fillWidth: true
                                padding: 10; verticalPadding: 6
                                radiusValue: Theme.radiusMedium
                                surfaceColor: Theme.canvasLift
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 0
                                    Repeater {
                                        model: [
                                            {label: "Base de données", path: AppBridge.diagnostics.database_path || "", icon: "database"},
                                            {label: "CV et lettres", path: AppBridge.diagnostics.output_dir || "", icon: "files"},
                                            {label: "Profil", path: AppBridge.diagnostics.profile_dir || "", icon: "user"},
                                            {label: "Configuration de l’application", path: (AppBridge.llmConfiguration || {}).env_path || AppBridge.diagnostics.env_file || "", icon: "sliders"},
                                            {label: "Configuration des modèles", path: (AppBridge.llmConfiguration || {}).model_config_path || "", icon: "sparkle"}
                                        ]
                                        delegate: Item {
                                            required property var modelData
                                            Layout.fillWidth: true
                                            implicitHeight: 76
                                            enabled: Boolean(modelData.path)
                                            activeFocusOnTab: true
                                            Accessible.role: Accessible.Button
                                            Accessible.name: "Ouvrir " + modelData.label
                                            Accessible.description: modelData.path
                                            Accessible.onPressAction: AppBridge.openPath(modelData.path)
                                            Rectangle { anchors.fill: parent; radius: Theme.radiusSmall; color: pathHover.hovered || parent.activeFocus ? Theme.surfaceHover : "transparent" }
                                            RowLayout {
                                                anchors.fill: parent; anchors.leftMargin: 12; anchors.rightMargin: 12
                                                spacing: 14
                                                SvgIcon { source: Theme.icon(modelData.icon); color: Theme.inkMuted; Layout.preferredWidth: 18; Layout.preferredHeight: 18 }
                                                ColumnLayout {
                                                    Layout.fillWidth: true
                                                    spacing: 6
                                                    Text { text: modelData.label; color: Theme.ink; font.pixelSize: 13; font.weight: Font.DemiBold }
                                                    Text { Layout.fillWidth: true; text: modelData.path || "Non configuré"; color: Theme.inkMuted; font.pixelSize: 11; elide: Text.ElideMiddle }
                                                }
                                                SvgIcon { source: Theme.icon("arrow-up-right"); color: Theme.inkFaint; Layout.preferredWidth: 16; Layout.preferredHeight: 16 }
                                            }
                                            HoverHandler { id: pathHover; cursorShape: Qt.PointingHandCursor }
                                            TapHandler { onTapped: AppBridge.openPath(modelData.path) }
                                            Keys.onReturnPressed: AppBridge.openPath(modelData.path)
                                            Keys.onSpacePressed: AppBridge.openPath(modelData.path)
                                            ToolTip.visible: pathHover.hovered
                                            ToolTip.delay: 700
                                            ToolTip.text: modelData.path
                                        }
                                    }
                                }
                            }
                            SettingsGroup {
                                title: "Emplacement des données"
                                caption: "Les changements de chemins prennent effet au redémarrage."
                                collapsible: true
                                fields: root.fieldsForGroups(["Données"])
                                drafts: root.appEdits
                                onEdited: (key, value) => root.editApplication(key, value)
                            }
                        }
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: footerContent.implicitHeight + 32
                    color: Theme.chrome
                    Rectangle { anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; height: 1; color: Theme.line }
                    RowLayout {
                        id: footerContent
                        width: Math.max(0, Math.min(920, parent.width - Theme.scrollGutter - 64))
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 12
                        Text {
                            Layout.fillWidth: true
                            text: root.llmError.length > 0 && root.savingSection !== "credentials" ? root.llmError
                                : root.selectedSection === "ai" ? (root.callsEdited ? (root.callsValid ? "Modifications non enregistrées" : "Certains réglages sont incomplets") : root.configurationIncomplete ? "Configuration à compléter" : "Tous les réglages sont enregistrés")
                                : Object.keys(root.appEdits).length > 0 ? "Modifications non enregistrées" : "Tous les réglages sont enregistrés"
                            color: root.llmError.length > 0 || (root.selectedSection === "ai" && (!root.callsValid || (!root.callsEdited && root.configurationIncomplete))) ? Theme.danger
                                : (root.selectedSection === "ai" ? root.callsEdited : Object.keys(root.appEdits).length > 0) ? Theme.warning : Theme.inkFaint
                            font.pixelSize: 12
                            wrapMode: Text.WordWrap
                        }
                        AppButton {
                            objectName: "discardSettingsButton"
                            text: "Annuler"
                            quiet: true
                            visible: root.selectedSection === "ai" ? root.callsEdited : Object.keys(root.appEdits).length > 0
                            enabled: !AppBridge.llmConfigurationSaving
                            onClicked: {
                                root.llmError = ""
                                if (root.selectedSection === "ai") { root.callsEdited = false; root.applyLlmConfiguration(false) }
                                else root.appEdits = ({})
                            }
                        }
                        AppButton {
                            objectName: "saveLlmCallsButton"
                            visible: root.selectedSection === "ai"
                            text: AppBridge.llmConfigurationSaving && root.savingSection === "calls" ? "Enregistrement…" : "Enregistrer"
                            kind: "primary"
                            enabled: root.callsEdited && root.callsValid && !AppBridge.llmConfigurationSaving
                            onClicked: { root.llmError = ""; root.savingSection = "calls"; AppBridge.saveLlmCalls(root.callValues()) }
                        }
                        AppButton {
                            objectName: "saveApplicationSettingsButton"
                            visible: root.selectedSection !== "ai"
                            text: AppBridge.llmConfigurationSaving && root.savingSection === "app" ? "Enregistrement…" : "Enregistrer"
                            kind: "primary"
                            enabled: Object.keys(root.appEdits).length > 0 && !AppBridge.llmConfigurationSaving
                            onClicked: { root.llmError = ""; root.savingSection = "app"; AppBridge.saveApplicationSettings(root.appEdits) }
                        }
                    }
                }
            }
        }
    }

    onSelectedSectionChanged: settingsScroll.contentY = 0
    onCallGroupChanged: settingsScroll.contentY = 0
}
