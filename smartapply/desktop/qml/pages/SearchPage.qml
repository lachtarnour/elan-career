import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    signal navigateRequested(string route)
    property var selectedSourceKeys: ["serpapi", "francetravail"]
    function selectedSources() {
        var readiness = AppBridge.diagnostics.source_ready || ({})
        return selectedSourceKeys.filter(function(key) { return Boolean(readiness[key]) })
    }
    function setSourceSelected(key, selected) {
        var next = selectedSourceKeys.slice()
        var index = next.indexOf(key)
        if (selected && index < 0) next.push(key)
        if (!selected && index >= 0) next.splice(index, 1)
        selectedSourceKeys = next
    }
    function runSearch() {
        if (AppBridge.shutdownRequested || selectedSources().length === 0 || !queryField.text.trim()) return
        maxResults.commitInput()
        AppBridge.searchJobs(queryField.text, locationField.text, selectedSources(), maxResults.value, freshness.currentValue)
    }
    Flickable {
        anchors.fill: parent
        contentWidth: width
        contentHeight: content.implicitHeight + 12
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: AppScrollBar { }
        ColumnLayout {
            id: content
            width: Math.min(parent.width - Theme.scrollGutter, 1320)
            x: (parent.width - Theme.scrollGutter - width) / 2
            spacing: Theme.pageGap
            PageHeader {
                Layout.fillWidth: true
                title: "Recherche"
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.sectionGap
                Surface {
                    Layout.fillWidth: true
                    Layout.preferredWidth: 3
                    Layout.alignment: Qt.AlignTop
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 7
                        FormLabel { text: "POSTES RECHERCHÉS" }
                        AppField {
                            id: queryField
                            objectName: "searchQuery"
                            Layout.fillWidth: true
                            iconSource: Theme.icon("search")
                            placeholderText: "Data Scientist, ML Engineer…"
                            text: (AppBridge.profile.target_roles || []).join(" OR ")
                            Component.onCompleted: cursorPosition = 0
                            onAccepted: root.runSearch()
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 14
                        ColumnLayout {
                            Layout.fillWidth: true
                            Layout.preferredWidth: 3
                            spacing: 7
                            FormLabel { text: "LOCALISATION" }
                            AppField {
                                id: locationField
                                Layout.fillWidth: true
                                iconSource: Theme.icon("map-pin")
                                placeholderText: "Ville, région ou pays"
                                onAccepted: root.runSearch()
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            Layout.preferredWidth: 2
                            spacing: 7
                            FormLabel { text: "PUBLICATION" }
                            AppSelect {
                                id: freshness
                                Layout.fillWidth: true
                                textRole: "text"
                                valueRole: "value"
                                model: [
                                    {text: "Aujourd’hui", value: "today"},
                                    {text: "3 derniers jours", value: "3days"},
                                    {text: "7 derniers jours", value: "week"},
                                    {text: "30 derniers jours", value: "month"}
                                ]
                                currentIndex: 2
                                Accessible.name: "Date de publication"
                            }
                        }
                    }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 12
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 4
                            Text { Layout.fillWidth: true; text: "Résultats par source"; color: Theme.inkSoft; font.pixelSize: 13; font.weight: Font.DemiBold }
                            Text {
                                Layout.fillWidth: true
                                text: "LinkedIn (Apify) et France Travail : limite par requête, sans plafond total."
                                color: Theme.inkSoft
                                font.pixelSize: 12
                                wrapMode: Text.WordWrap
                            }
                        }
                        AppSpinBox { id: maxResults; from: 1; to: 100; value: 20; Layout.preferredWidth: 132 }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 12
                        Item { Layout.fillWidth: true }
                        AppButton {
                            text: "Rechercher"
                            iconSource: Theme.icon("search")
                            kind: "primary"
                            enabled: !AppBridge.shutdownRequested && root.selectedSources().length > 0 && queryField.text.trim().length > 0
                            onClicked: root.runSearch()
                        }
                    }
                }
                Surface {
                    Layout.fillWidth: true
                    Layout.preferredWidth: 2
                    Layout.alignment: Qt.AlignTop
                    RowLayout {
                        Layout.fillWidth: true
                        SectionTitle { Layout.fillWidth: true; title: "Sources" }
                        AppButton {
                            objectName: "refreshSourcesButton"
                            text: AppBridge.diagnosticsChecking ? "Vérification…" : "Actualiser"
                            iconSource: Theme.icon("refresh")
                            enabled: !AppBridge.diagnosticsChecking
                            onClicked: AppBridge.refreshDiagnostics()
                        }
                    }
                    Repeater {
                        model: [
                            {label: "Google Jobs", symbol: "G", key: "serpapi"},
                            {label: "France Travail", symbol: "F", key: "francetravail"},
                            {label: "LinkedIn", symbol: "in", key: "linkedin"},
                            {label: "Welcome to the Jungle", symbol: "W", key: "welcometothejungle"}
                        ]
                        delegate: ColumnLayout {
                            required property var modelData
                            readonly property bool available: Boolean((AppBridge.diagnostics.source_ready || ({}))[modelData.key])
                            readonly property var health: (AppBridge.diagnostics.source_health || ({}))[modelData.key] || ({})
                            Layout.fillWidth: true
                            spacing: 4
                            ChoiceChip {
                                objectName: "sourceChip_" + modelData.key
                                Layout.fillWidth: true
                                text: modelData.label
                                symbol: modelData.symbol
                                enabled: parent.available
                                checked: root.selectedSourceKeys.indexOf(modelData.key) >= 0
                                onToggled: root.setSourceSelected(modelData.key, checked)
                            }
                            Text {
                                Layout.fillWidth: true
                                text: parent.health.message || "Vérification en cours…"
                                color: Theme.inkMuted
                                font.pixelSize: 11
                                wrapMode: Text.Wrap
                            }
                        }
                    }
                }
            }
            Surface {
                Layout.fillWidth: true
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 20
                    Repeater {
                        model: [
                            {label: "Offres", value: AppBridge.dashboard.jobs || 0},
                            {label: "À analyser", value: AppBridge.dashboard.pending || 0},
                            {label: "Dossiers prêts", value: AppBridge.dashboard.ready || 0}
                        ]
                        delegate: ColumnLayout {
                            required property var modelData
                            Layout.fillWidth: true
                            spacing: 4
                            Text { Layout.fillWidth: true; text: String(modelData.value); color: Theme.ink; font.pixelSize: 24; font.weight: Font.DemiBold }
                            Text { Layout.fillWidth: true; text: modelData.label; color: Theme.inkMuted; font.pixelSize: 12 }
                        }
                    }
                    Text { text: "Offres à retenir"; color: Theme.inkMuted; font.pixelSize: 12 }
                    AppSpinBox { id: topK; from: 1; to: 100; value: AppBridge.analysisTopK; Layout.preferredWidth: 132; onUserValueModified: function(value) { AppBridge.setAnalysisTopK(Number(value)) } }
                    AppButton { text: "Analyser"; iconSource: Theme.icon("sparkle"); kind: "primary"; enabled: !AppBridge.shutdownRequested; onClicked: { topK.commitInput(); AppBridge.processPending(topK.value) } }
                }
            }
        }
    }
}
