import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    objectName: "duplicatesPage"
    signal navigateRequested(string route)

    property string query: ""
    property int selectedId: Number(AppBridge.currentJob.id || 0)
    property var rows: AppBridge.jobs || []
    property var visibleRows: {
        var dependencyRows = rows
        var dependencyQuery = query
        var needle = String(query || "").trim().toLowerCase()
        if (!needle) return rows
        return rows.filter(function(row) {
            return [row.company, row.title, row.location, row.source]
                .join(" ").toLowerCase().indexOf(needle) >= 0
        })
    }
    property var selectedOffer: AppBridge.currentJob || ({})
    property var candidate: selectedOffer.duplicate_candidate || ({})
    readonly property bool selectedPairBusy: (AppBridge.busyJobIds || []).indexOf(Number(selectedId)) >= 0
        || (AppBridge.busyJobIds || []).indexOf(Number(candidate.id || 0)) >= 0
    property int displayedJobId: 0
    property bool hasSelection: Boolean(selectedOffer.id) && Boolean(candidate.id)

    function confidenceText(value) {
        if (value === undefined || value === null || value === "") return "—"
        return Math.round(Number(value) * 100) + "% de similarité"
    }

    function confidenceTone(value) {
        var score = Number(value || 0)
        return score >= 0.85 ? "warning" : "accent"
    }

    function sourceLabel(value) {
        var labels = {
            serpapi: "Google Jobs",
            francetravail: "France Travail",
            linkedin: "LinkedIn",
            welcometothejungle: "Welcome to the Jungle"
        }
        return labels[String(value || "")] || String(value || "Source inconnue")
    }

    function metadataText(offer) {
        var values = []
        if (offer.contract) values.push(offer.contract)
        if (offer.remote) values.push(offer.remote)
        return values.join(" · ")
    }

    function descriptionPreview(value) {
        var text = String(value || "").replace(/\s+/g, " ").trim()
        if (text.length > 560) return text.slice(0, 560) + "…"
        return text || "Description indisponible."
    }

    function selectRow(row) {
        if (row && row.id) AppBridge.selectJob(Number(row.id))
    }

    Connections {
        target: AppBridge
        function onCurrentJobChanged() {
            if (root.displayedJobId !== root.selectedId) {
                root.displayedJobId = root.selectedId
                compareScroll.contentY = 0
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: Theme.pageGap

        PageHeader {
            Layout.fillWidth: true
            title: "Doublons"
            Switch {
                id: relevantOnlySwitch
                objectName: "relevantDuplicatesOnlySwitch"
                text: "Pertinents"
                checked: AppBridge.duplicateRelevantOnly
                font.pixelSize: 12
                spacing: 8
                Accessible.name: "Afficher seulement les doublons dont les deux offres sont pertinentes"
                ToolTip.visible: hovered
                ToolTip.text: "Garde les paires dont les deux offres sont classées pertinentes par le filtre local."
                onToggled: AppBridge.setDuplicateRelevantOnly(checked)
                indicator: Rectangle {
                    implicitWidth: 40
                    implicitHeight: 22
                    x: relevantOnlySwitch.leftPadding
                    y: parent.height / 2 - height / 2
                    radius: height / 2
                    color: relevantOnlySwitch.checked ? Theme.accent : Theme.surfaceMuted
                    border.color: relevantOnlySwitch.checked ? Theme.accentBright : Theme.line
                    Rectangle {
                        width: 16
                        height: 16
                        radius: 8
                        x: relevantOnlySwitch.checked ? parent.width - width - 3 : 3
                        anchors.verticalCenter: parent.verticalCenter
                        color: Theme.ink
                    }
                }
                contentItem: Text {
                    text: relevantOnlySwitch.text
                    font: relevantOnlySwitch.font
                    color: Theme.inkSoft
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: relevantOnlySwitch.indicator.width + relevantOnlySwitch.spacing
                }
            }
            Pill {
                text: root.rows.length + " à traiter"
                tone: root.rows.length > 0 ? "warning" : "success"
            }
            AppButton {
                text: "Actualiser"
                iconSource: Theme.icon("refresh")
                enabled: !AppBridge.shutdownRequested && !AppBridge.jobsLoading
                onClicked: AppBridge.loadJobs("", "duplicate_review", "duplicate_confidence", false)
            }
        }

        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: root.rows.length === 0 && !AppBridge.jobsLoading
            EmptyState {
                anchors.centerIn: parent
                title: AppBridge.duplicateRelevantOnly ? "Aucune paire pertinente" : "Aucun doublon à vérifier"
                iconSource: Theme.icon("check")
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: root.rows.length > 0 || AppBridge.jobsLoading
            spacing: 16

            Surface {
                Layout.preferredWidth: Math.min(400, Math.max(320, root.width * 0.30))
                Layout.minimumWidth: 300
                Layout.fillHeight: true
                padding: 16
                surfaceEndColor: Theme.surface

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: 12
                    RowLayout {
                        Layout.fillWidth: true
                        Text {
                            Layout.fillWidth: true
                            text: "À vérifier"
                            color: Theme.inkSoft
                            font.pixelSize: 15
                            font.weight: Font.DemiBold
                        }
                        Text {
                            text: root.visibleRows.length + " / " + root.rows.length
                            color: Theme.inkMuted
                            font.pixelSize: 12
                        }
                    }
                    AppField {
                        Layout.fillWidth: true
                        implicitHeight: 40
                        iconSource: Theme.icon("search")
                        placeholderText: "Filtrer les rapprochements…"
                        onTextChanged: root.query = text
                    }
                    ListView {
                        id: duplicateList
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        spacing: 8
                        model: root.visibleRows
                        ScrollBar.vertical: AppScrollBar { }
                        delegate: Rectangle {
                            required property var modelData
                            width: duplicateList.width - Theme.scrollGutter
                            height: 100
                            activeFocusOnTab: true
                            Accessible.role: Accessible.Button
                            Accessible.name: modelData.company + " · " + modelData.title
                            Accessible.onPressAction: root.selectRow(modelData)
                            Keys.onReturnPressed: root.selectRow(modelData)
                            Keys.onSpacePressed: root.selectRow(modelData)
                            radius: 14
                            color: Number(modelData.id) === root.selectedId ? Theme.accentSoft : (rowHover.hovered ? Theme.surfaceHover : Theme.surfaceMuted)
                            border.color: activeFocus ? Theme.accent : (Number(modelData.id) === root.selectedId ? Theme.accentLine : Theme.line)
                            border.width: Number(modelData.id) === root.selectedId ? 1.5 : 1
                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: 12
                                spacing: 10
                                Rectangle {
                                    Layout.preferredWidth: 34
                                    Layout.preferredHeight: 34
                                    Layout.alignment: Qt.AlignTop
                                    radius: 11
                                    color: Theme.warningSoft
                                    Text {
                                        anchors.centerIn: parent
                                        text: String(modelData.company || "•").slice(0, 1).toUpperCase()
                                        color: Theme.warning
                                        font.pixelSize: 13
                                        font.weight: Font.Bold
                                    }
                                }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 3
                                    Text {
                                        Layout.fillWidth: true
                                        text: modelData.company || "Entreprise inconnue"
                                        color: Theme.inkSoft
                                        font.pixelSize: 12
                                        font.weight: Font.DemiBold
                                        elide: Text.ElideRight
                                    }
                                    Text {
                                        Layout.fillWidth: true
                                        text: modelData.title || "Titre indisponible"
                                        color: Theme.ink
                                        font.pixelSize: 12
                                        font.weight: Font.Medium
                                        elide: Text.ElideRight
                                    }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 7
                                        Text {
                                            Layout.fillWidth: true
                                            text: root.sourceLabel(modelData.source)
                                            color: Theme.inkFaint
                                            font.pixelSize: 12
                                            elide: Text.ElideRight
                                        }
                                        Pill {
                                            text: root.confidenceText(modelData.duplicate_confidence).replace(" de similarité", "")
                                            tone: root.confidenceTone(modelData.duplicate_confidence)
                                            compact: true
                                        }
                                    }
                                }
                            }
                            HoverHandler { id: rowHover; cursorShape: Qt.PointingHandCursor }
                            TapHandler { onTapped: root.selectRow(modelData) }
                        }
                        EmptyState {
                            anchors.centerIn: parent
                            visible: root.visibleRows.length === 0 && !AppBridge.jobsLoading
                            title: root.rows.length === 0
                                ? (AppBridge.duplicateRelevantOnly ? "Aucune paire pertinente" : "Tout est traité")
                                : "Aucun résultat"
                            iconSource: Theme.icon(root.rows.length === 0 ? "check" : "search")
                        }
                    }
                }
            }

            Surface {
                Layout.fillWidth: true
                Layout.fillHeight: true
                padding: 20
                surfaceEndColor: Theme.surface

                Flickable {
                    id: compareScroll
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    contentWidth: width
                    contentHeight: compareContent.implicitHeight
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: AppScrollBar { }

                    ColumnLayout {
                        id: compareContent
                        width: compareScroll.width - Theme.scrollGutter
                        spacing: 14
                        visible: root.hasSelection
                        opacity: AppBridge.jobDetailLoading ? 0.4 : 1
                        Behavior on opacity { NumberAnimation { duration: 120 } }

                        RowLayout {
                            Layout.fillWidth: true
                            Item { Layout.fillWidth: true }
                            Pill {
                                text: root.confidenceText(root.selectedOffer.duplicate_confidence)
                                tone: root.confidenceTone(root.selectedOffer.duplicate_confidence)
                                compact: true
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 12

                            Surface {
                                Layout.fillWidth: true
                                Layout.preferredWidth: 1
                                Layout.alignment: Qt.AlignTop
                                padding: 16
                                surfaceColor: Theme.surfaceMuted
                                surfaceEndColor: Theme.surfaceMuted
                                strokeColor: Theme.accentLine
                                SectionTitle {
                                    title: "Offre récente"
                                    caption: "#" + root.selectedOffer.id
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: root.selectedOffer.title || "Titre indisponible"
                                    color: Theme.ink
                                    font.pixelSize: 15
                                    font.weight: Font.DemiBold
                                    wrapMode: Text.WordWrap
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: (root.selectedOffer.company || "Entreprise inconnue") + (root.selectedOffer.location ? " · " + root.selectedOffer.location : "")
                                    color: Theme.inkSoft
                                    font.pixelSize: 12
                                    wrapMode: Text.WordWrap
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: root.metadataText(root.selectedOffer)
                                    visible: text.length > 0
                                    color: Theme.inkMuted
                                    font.pixelSize: 12
                                    wrapMode: Text.WordWrap
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    Text { Layout.fillWidth: true; text: root.sourceLabel(root.selectedOffer.source); color: Theme.accentBright; font.pixelSize: 12; font.weight: Font.DemiBold }
                                    AppButton {
                                        text: "Ouvrir"
                                        iconSource: Theme.icon("arrow-up-right")
                                        implicitHeight: 30
                                        enabled: Boolean(root.selectedOffer.url)
                                        onClicked: AppBridge.openUrl(root.selectedOffer.url)
                                    }
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: root.descriptionPreview(root.selectedOffer.description)
                                    color: Theme.inkSoft
                                    font.pixelSize: 13
                                    lineHeight: 1.3
                                    wrapMode: Text.WordWrap
                                }
                                Pill {
                                    visible: Boolean(root.selectedOffer.application && root.selectedOffer.application.id)
                                    text: root.selectedOffer.application ? (root.selectedOffer.application.status_label || "Candidature existante") : ""
                                    tone: root.selectedOffer.application && root.selectedOffer.application.status === "archived" ? "warning" : "neutral"
                                    compact: true
                                }
                            }

                            Surface {
                                Layout.fillWidth: true
                                Layout.preferredWidth: 1
                                Layout.alignment: Qt.AlignTop
                                padding: 16
                                surfaceColor: Theme.surfaceMuted
                                surfaceEndColor: Theme.surfaceMuted
                                strokeColor: Theme.warningLine
                                SectionTitle {
                                    title: "Offre déjà connue"
                                    caption: "#" + root.candidate.id
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: root.candidate.title || "Titre indisponible"
                                    color: Theme.ink
                                    font.pixelSize: 15
                                    font.weight: Font.DemiBold
                                    wrapMode: Text.WordWrap
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: (root.candidate.company || "Entreprise inconnue") + (root.candidate.location ? " · " + root.candidate.location : "")
                                    color: Theme.inkSoft
                                    font.pixelSize: 12
                                    wrapMode: Text.WordWrap
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: root.metadataText(root.candidate)
                                    visible: text.length > 0
                                    color: Theme.inkMuted
                                    font.pixelSize: 12
                                    wrapMode: Text.WordWrap
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    Text { Layout.fillWidth: true; text: root.sourceLabel(root.candidate.source); color: Theme.warning; font.pixelSize: 12; font.weight: Font.DemiBold }
                                    AppButton {
                                        text: "Ouvrir"
                                        iconSource: Theme.icon("arrow-up-right")
                                        implicitHeight: 30
                                        enabled: Boolean(root.candidate.url)
                                        onClicked: AppBridge.openUrl(root.candidate.url)
                                    }
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: root.descriptionPreview(root.candidate.description)
                                    color: Theme.inkSoft
                                    font.pixelSize: 13
                                    lineHeight: 1.3
                                    wrapMode: Text.WordWrap
                                }
                                Pill {
                                    visible: Boolean(root.candidate.application_id)
                                    text: root.candidate.application_status_label || "Candidature existante"
                                    tone: "success"
                                    compact: true
                                }
                            }
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            implicitHeight: decisionContent.implicitHeight + 28
                            radius: 16
                            color: Theme.warningSoft
                            border.color: Theme.warningLine
                            ColumnLayout {
                                id: decisionContent
                                anchors.fill: parent
                                anchors.margins: 14
                                spacing: 9
                                Text {
                                    visible: root.selectedPairBusy
                                    Layout.fillWidth: true
                                    text: "Une offre de cette paire est en cours de traitement. Décision disponible à sa fin."
                                    color: Theme.warning
                                    wrapMode: Text.WordWrap
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 9
                                    AppButton {
                                        Layout.fillWidth: true
                                        objectName: "duplicateConfirmAction"
                                        text: "Même offre"
                                        iconSource: Theme.icon("check")
                                        kind: "primary"
                                        implicitHeight: 42
                                        enabled: !AppBridge.shutdownRequested && !root.selectedPairBusy && !AppBridge.jobDetailLoading && !AppBridge.jobsLoading
                                        onClicked: AppBridge.resolveDuplicate(root.selectedId, true)
                                    }
                                    AppButton {
                                        Layout.fillWidth: true
                                        objectName: "duplicateDistinctAction"
                                        text: "Offres différentes"
                                        iconSource: Theme.icon("files")
                                        implicitHeight: 42
                                        enabled: !AppBridge.shutdownRequested && !root.selectedPairBusy && !AppBridge.jobDetailLoading && !AppBridge.jobsLoading
                                        onClicked: AppBridge.resolveDuplicate(root.selectedId, false)
                                    }
                                }
                            }
                        }
                    }

                    BusyIndicator {
                        parent: compareScroll
                        anchors.centerIn: parent
                        running: AppBridge.jobsLoading || AppBridge.jobDetailLoading
                        visible: running
                        palette.dark: Theme.accent
                    }
                    EmptyState {
                        parent: compareScroll
                        anchors.centerIn: parent
                        visible: !root.hasSelection && !AppBridge.jobsLoading && !AppBridge.jobDetailLoading
                        title: root.rows.length === 0
                            ? (AppBridge.duplicateRelevantOnly ? "Aucune paire pertinente" : "Aucun doublon en attente")
                            : "Aucun rapprochement sélectionné"
                        iconSource: Theme.icon(root.rows.length === 0 ? "check" : "files")
                    }
                }
            }
        }
    }
}
