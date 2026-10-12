import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    property var selectedSourceKeys: ["serpapi", "francetravail"]
    readonly property bool searchRunning: AppBridge.searchRunning
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
        if (AppBridge.shutdownRequested || searchRunning || selectedSources().length === 0 || !queryField.text.trim()) return
        maxResults.commitInput()
        AppBridge.searchJobs(queryField.text, locationField.text, selectedSources(), maxResults.value, freshness.currentValue)
    }
    Flickable {
        id: searchScroll
        objectName: "searchScroll"
        anchors.fill: parent
        contentWidth: width
        contentHeight: Math.max(height, content.implicitHeight + 12)
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: AppScrollBar { }
        ColumnLayout {
            id: content
            width: Math.min(parent.width - Theme.scrollGutter, Theme.pageContentMaxWidth)
            height: implicitHeight
            x: (parent.width - Theme.scrollGutter - width) / 2
            spacing: Theme.pageGap
            PageHeader {
                Layout.fillWidth: true
                title: "Recherche"
            }
            Item {
                objectName: "searchCardsArea"
                Layout.fillWidth: true
                implicitHeight: searchCards.implicitHeight
                RowLayout {
                    id: searchCards
                    objectName: "searchCardsRow"
                    width: parent.width
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: Theme.sectionGap
                    Item {
                        objectName: "searchCriteriaPanel"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.preferredWidth: 3
                        implicitHeight: criteriaBody.implicitHeight + 44
                        ColumnLayout {
                            id: criteriaBody
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 22
                            height: implicitHeight
                            spacing: 16
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
                                    text: AppBridge.defaultSearchQuery
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
                                        objectName: "searchLocation"
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
                                Text {
                                    objectName: "searchTaskProgress"
                                    Layout.fillWidth: true
                                    text: root.searchRunning ? (AppBridge.searchLabel || "Recherche en cours…") : ""
                                    color: Theme.inkSoft
                                    font.pixelSize: 12
                                    elide: Text.ElideRight
                                }
                                AppButton {
                                    objectName: "searchCancelAction"
                                    visible: root.searchRunning && !AppBridge.shutdownRequested
                                    text: "Arrêter"
                                    quiet: true
                                    onClicked: AppBridge.cancelSearch()
                                }
                                AppButton {
                                    objectName: "searchStartAction"
                                    text: root.searchRunning ? "Recherche…" : "Rechercher"
                                    iconSource: Theme.icon("search")
                                    kind: "primary"
                                    enabled: !AppBridge.shutdownRequested && !root.searchRunning && root.selectedSources().length > 0 && queryField.text.trim().length > 0
                                    onClicked: root.runSearch()
                                }
                            }
                        }
                    }
                    Rectangle {
                        objectName: "searchWorkspaceDivider"
                        Layout.preferredWidth: 1
                        Layout.fillHeight: true
                        Layout.topMargin: 20
                        Layout.bottomMargin: 20
                        Layout.leftMargin: 6
                        Layout.rightMargin: 6
                        color: Theme.line
                    }
                    Item {
                        objectName: "searchSourcesPanel"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.preferredWidth: 2
                        implicitHeight: sourcesBody.implicitHeight + 44
                        ColumnLayout {
                            id: sourcesBody
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 22
                            height: implicitHeight
                            spacing: 16
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
                }
            }
        }
    }
}
