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
    property var visibleRows: []
    function filteredRows() {
        var needle = String(query || "").trim().toLowerCase()
        if (!needle) return rows
        return rows.filter(function(row) {
            return [row.company, row.title, row.location, row.source, row.id,
                row.possible_duplicate_of_id].join(" ").toLowerCase().indexOf(needle) >= 0
        })
    }
    readonly property int visibleRelationCount: visibleRows.length
    property var selectedOffer: AppBridge.currentJob || ({})
    property var candidate: selectedOffer.duplicate_candidate || ({})
    readonly property bool selectedPairBusy: (AppBridge.busyJobIds || []).indexOf(Number(selectedId)) >= 0
        || (AppBridge.busyJobIds || []).indexOf(Number(candidate.id || 0)) >= 0
    property int displayedJobId: 0
    property bool hasSelection: Boolean(selectedOffer.id) && Boolean(candidate.id)
    property int decisionJobId: 0
    property var decisionOrder: []
    property bool decisionSaved: false

    function visiblePairIds() {
        return visibleRows.map(function(row) { return Number(row.id) })
    }

    function cancelAdvancement() {
        decisionJobId = 0
        decisionOrder = []
        decisionSaved = false
    }

    function resolveSelectedPair(sameOffer) {
        if (decisionJobId || !hasSelection) return
        var ids = visiblePairIds()
        var index = ids.indexOf(selectedId)
        decisionOrder = index < 0 ? ids : ids.slice(index + 1).concat(ids.slice(0, index))
        decisionJobId = selectedId
        decisionSaved = false
        AppBridge.resolveDuplicate(selectedId, sameOffer)
    }

    function advanceAfterDecision() {
        if (!decisionSaved || !visible || selectedId !== decisionJobId) return
        // The refreshed list is the authority: a confirmation alone is not
        // enough, and resolving a pair can remove other pending relations.
        if (rows.some(function(row) { return Number(row.id) === decisionJobId })) return
        var remaining = visiblePairIds()
        var nextId = decisionOrder.find(function(id) { return remaining.indexOf(id) >= 0 })
        if (nextId === undefined) nextId = remaining.length ? remaining[0] : 0
        cancelAdvancement()
        AppBridge.selectJob(nextId)
        Qt.callLater(function() {
            var index = visibleRows.findIndex(function(row) { return Number(row.id) === nextId })
            if (index >= 0) duplicateList.positionViewAtIndex(index, ListView.Contain)
        })
    }

    function updateVisibleRows() {
        var previousY = duplicateList ? duplicateList.contentY : 0
        visibleRows = filteredRows()
        Qt.callLater(function() {
            duplicateList.contentY = Math.min(previousY,
                Math.max(0, duplicateList.contentHeight - duplicateList.height))
        })
    }

    onRowsChanged: {
        updateVisibleRows()
        Qt.callLater(advanceAfterDecision)
    }
    onQueryChanged: {
        cancelAdvancement()
        updateVisibleRows()
    }
    onVisibleChanged: if (!visible) cancelAdvancement()
    onSelectedIdChanged: if (decisionJobId && selectedId !== decisionJobId) cancelAdvancement()

    function confidenceText(value) {
        if (value === undefined || value === null || value === "") return "—"
        return Math.round(Number(value) * 100) + "%"
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
        if (text.length > 1200) return text.slice(0, 1200).replace(/\s+\S*$/, "") + "…"
        return text || "Description indisponible."
    }

    function selectRow(row) {
        cancelAdvancement()
        if (row && row.id) AppBridge.selectJob(Number(row.id))
    }

    Connections {
        target: AppBridge
        function onDuplicateResolutionFinished(jobId, resolved) {
            if (jobId !== root.decisionJobId) return
            if (!resolved) root.cancelAdvancement()
            else {
                root.decisionSaved = true
                Qt.callLater(root.advanceAfterDecision)
            }
        }
        function onDuplicatePendingOnlyChanged() { root.cancelAdvancement() }
        function onCurrentJobChanged() {
            if (root.displayedJobId !== root.selectedId) {
                root.displayedJobId = root.selectedId
                recentDescriptionScroll.contentY = 0
                knownDescriptionScroll.contentY = 0
            }
        }
    }
    Component.onCompleted: {
        updateVisibleRows()
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.bottomMargin: 12
        spacing: Theme.pageGap

        PageHeader {
            Layout.fillWidth: true
            Layout.leftMargin: Math.max(0, (root.width - Theme.scrollGutter - Theme.pageContentMaxWidth) / 2)
            Layout.rightMargin: Layout.leftMargin + Theme.scrollGutter
            title: "Doublons"
            Switch {
                id: pendingOnlySwitch
                objectName: "pendingDuplicatesOnlySwitch"
                text: "Au moins une offre active"
                checked: AppBridge.duplicatePendingOnly
                font.pixelSize: 12
                spacing: 8
                Accessible.name: "Afficher les doublons dont au moins une offre n’est ni rejetée ni archivée"
                ToolTip.visible: hovered
                ToolTip.text: "Garde les paires dont au moins une offre n’est ni rejetée ni archivée, même si elle est déjà analysée ou préparée."
                onToggled: AppBridge.setDuplicatePendingOnly(checked)
                indicator: Rectangle {
                    implicitWidth: 40
                    implicitHeight: 22
                    x: pendingOnlySwitch.leftPadding
                    y: parent.height / 2 - height / 2
                    radius: height / 2
                    color: pendingOnlySwitch.checked ? Theme.accent : Theme.surfaceMuted
                    border.color: pendingOnlySwitch.checked ? Theme.accentBright : Theme.line
                    Rectangle {
                        width: 16
                        height: 16
                        radius: 8
                        x: pendingOnlySwitch.checked ? parent.width - width - 3 : 3
                        anchors.verticalCenter: parent.verticalCenter
                        color: Theme.ink
                    }
                }
                contentItem: Text {
                    text: pendingOnlySwitch.text
                    font: pendingOnlySwitch.font
                    color: Theme.inkSoft
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: pendingOnlySwitch.indicator.width + pendingOnlySwitch.spacing
                }
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
                title: AppBridge.duplicatePendingOnly ? "Aucune paire avec une offre active" : "Aucun doublon"
                iconSource: Theme.icon("check")
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: root.rows.length > 0 || AppBridge.jobsLoading
            spacing: Theme.pageGap

            Item {
                objectName: "duplicateListPanel"
                Layout.preferredWidth: Math.min(400, Math.max(320, root.width * 0.30))
                Layout.minimumWidth: 300
                Layout.fillHeight: true

                ColumnLayout {
                    anchors.fill: parent
                    spacing: Theme.sectionGap
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        AppField {
                            Layout.fillWidth: true
                            implicitHeight: 40
                            iconSource: Theme.icon("search")
                            placeholderText: "Filtrer les rapprochements…"
                            onTextChanged: root.query = text
                        }
                        Text {
                            objectName: "duplicateRelationCount"
                            text: root.visibleRelationCount + " / " + root.rows.length
                            color: Theme.inkMuted
                            font.pixelSize: 12
                        }
                    }
                    ListView {
                        id: duplicateList
                        objectName: "duplicatePairList"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        spacing: 4
                        model: root.visibleRows
                        ScrollBar.vertical: AppScrollBar { }
                        delegate: Rectangle {
                            id: pairCard
                            objectName: "duplicatePairAction_" + modelData.id
                            required property var modelData
                            signal clicked()
                            onClicked: root.selectRow(modelData)
                            readonly property bool selected: Number(modelData.id) === root.selectedId
                            width: duplicateList.width - Theme.scrollGutter
                            height: pairContent.implicitHeight + 20
                            radius: 10
                            color: selected ? Theme.accentSoft : (pairHover.hovered ? Theme.surfaceHover : "transparent")
                            activeFocusOnTab: true
                            Accessible.role: Accessible.Button
                            Accessible.name: (modelData.company || "Entreprise inconnue") + " — "
                                + (modelData.title || "Titre indisponible") + ", comparer #"
                                + modelData.id + " avec #" + modelData.possible_duplicate_of_id
                            Accessible.onPressAction: pairCard.clicked()
                            Keys.onReturnPressed: pairCard.clicked()
                            Keys.onSpacePressed: pairCard.clicked()
                            HoverHandler { id: pairHover; cursorShape: Qt.PointingHandCursor }
                            TapHandler { onTapped: pairCard.clicked() }
                            Rectangle {
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.bottom: parent.bottom
                                height: 1
                                color: Theme.line
                                visible: !pairCard.selected
                            }
                            ColumnLayout {
                                id: pairContent
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.top: parent.top
                                anchors.margins: 10
                                spacing: 7
                                Text {
                                    Layout.fillWidth: true
                                    text: pairCard.modelData.company || "Entreprise inconnue"
                                    color: Theme.inkSoft
                                    font.pixelSize: 12
                                    font.weight: Font.DemiBold
                                    elide: Text.ElideRight
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: pairCard.modelData.title || "Titre indisponible"
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
                                        text: root.sourceLabel(pairCard.modelData.source)
                                        color: Theme.inkFaint
                                        font.pixelSize: 12
                                        elide: Text.ElideRight
                                    }
                                    Text {
                                        text: root.confidenceText(pairCard.modelData.duplicate_confidence)
                                        color: Theme.warning
                                        font.pixelSize: 12
                                        font.weight: Font.DemiBold
                                    }
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: "Comparer #" + pairCard.modelData.id + " avec #" + pairCard.modelData.possible_duplicate_of_id
                                    color: pairCard.selected ? Theme.accentBright : Theme.inkMuted
                                    font.pixelSize: 11
                                    font.weight: Font.DemiBold
                                }
                            }
                        }
                        EmptyState {
                            anchors.centerIn: parent
                            visible: root.visibleRows.length === 0 && !AppBridge.jobsLoading
                            title: root.rows.length === 0
                                ? (AppBridge.duplicatePendingOnly ? "Aucune paire avec une offre active" : "Tout est traité")
                                : "Aucun résultat"
                            iconSource: Theme.icon(root.rows.length === 0 ? "check" : "search")
                        }
                    }
                }
            }

            Item {
                objectName: "duplicateComparisonPanel"
                Layout.fillWidth: true
                Layout.fillHeight: true

                ColumnLayout {
                    id: compareContent
                    objectName: "duplicateComparisonContent"
                    anchors.fill: parent
                    anchors.rightMargin: Theme.scrollGutter
                    spacing: 16
                    visible: root.hasSelection
                    opacity: AppBridge.jobDetailLoading ? 0.4 : 1
                    Behavior on opacity { NumberAnimation { duration: 120 } }

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.maximumHeight: root.height
                        spacing: 12

                        Surface {
                            objectName: "duplicateRecentOfferCard"
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            Layout.preferredWidth: 1
                            Layout.minimumHeight: 0
                            Layout.maximumHeight: 600
                            Layout.alignment: Qt.AlignVCenter
                            Layout.margins: 20
                            padding: 16
                            verticalPadding: 11
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
                            Flickable {
                                id: recentDescriptionScroll
                                objectName: "duplicateRecentDescriptionScroll"
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                Layout.minimumHeight: 0
                                clip: true
                                contentWidth: width
                                contentHeight: recentDescription.implicitHeight
                                boundsBehavior: Flickable.StopAtBounds
                                ScrollBar.vertical: AppScrollBar { }
                                Text {
                                    id: recentDescription
                                    width: recentDescriptionScroll.width - Theme.scrollGutter
                                    text: root.descriptionPreview(root.selectedOffer.description)
                                    color: Theme.inkSoft
                                    font.pixelSize: 13
                                    lineHeight: 1.3
                                    wrapMode: Text.WordWrap
                                }
                            }
                            Pill {
                                visible: Boolean(root.selectedOffer.application && root.selectedOffer.application.id)
                                text: root.selectedOffer.application ? (root.selectedOffer.application.status_label || "Candidature existante") : ""
                                tone: root.selectedOffer.application && root.selectedOffer.application.status === "archived" ? "warning" : "neutral"
                                compact: true
                            }
                        }

                        Surface {
                            objectName: "duplicateKnownOfferCard"
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            Layout.preferredWidth: 1
                            Layout.minimumHeight: 0
                            Layout.maximumHeight: 600
                            Layout.alignment: Qt.AlignVCenter
                            Layout.margins: 20
                            padding: 16
                            verticalPadding: 11
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
                            Flickable {
                                id: knownDescriptionScroll
                                objectName: "duplicateKnownDescriptionScroll"
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                Layout.minimumHeight: 0
                                clip: true
                                contentWidth: width
                                contentHeight: knownDescription.implicitHeight
                                boundsBehavior: Flickable.StopAtBounds
                                ScrollBar.vertical: AppScrollBar { }
                                Text {
                                    id: knownDescription
                                    width: knownDescriptionScroll.width - Theme.scrollGutter
                                    text: root.descriptionPreview(root.candidate.description)
                                    color: Theme.inkSoft
                                    font.pixelSize: 13
                                    lineHeight: 1.3
                                    wrapMode: Text.WordWrap
                                }
                            }
                            Pill {
                                visible: Boolean(root.candidate.application_id)
                                text: root.candidate.application_status_label || "Candidature existante"
                                tone: "success"
                                compact: true
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 9
                        AppButton {
                            Layout.fillWidth: true
                            Layout.preferredWidth: 1
                            objectName: "duplicateConfirmAction"
                            text: "Même offre"
                            iconSource: Theme.icon("check")
                            kind: "primary"
                            implicitHeight: 42
                            enabled: !AppBridge.shutdownRequested && !root.selectedPairBusy && !root.decisionJobId && !AppBridge.jobDetailLoading && !AppBridge.jobsLoading
                            onClicked: root.resolveSelectedPair(true)
                        }
                        AppButton {
                            Layout.fillWidth: true
                            Layout.preferredWidth: 1
                            objectName: "duplicateDistinctAction"
                            text: "Offres différentes"
                            iconSource: Theme.icon("files")
                            implicitHeight: 42
                            enabled: !AppBridge.shutdownRequested && !root.selectedPairBusy && !root.decisionJobId && !AppBridge.jobDetailLoading && !AppBridge.jobsLoading
                            onClicked: root.resolveSelectedPair(false)
                        }
                    }
                }

                BusyIndicator {
                    anchors.centerIn: parent
                    running: AppBridge.jobsLoading || AppBridge.jobDetailLoading
                    visible: running
                    palette.dark: Theme.accent
                }
                EmptyState {
                    anchors.centerIn: parent
                    visible: !root.hasSelection && !AppBridge.jobsLoading && !AppBridge.jobDetailLoading
                    title: root.visibleRelationCount === 0
                        ? (AppBridge.duplicatePendingOnly ? "Aucune paire avec une offre active" : "Aucun doublon en attente")
                        : "Aucun rapprochement sélectionné"
                    iconSource: Theme.icon(root.visibleRelationCount === 0 ? "check" : "files")
                }
            }
        }
    }
}
