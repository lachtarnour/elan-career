import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    objectName: "analysisPage"
    signal navigateRequested(string route, int selectedId)

    // Analysis owns its list and run history. Passive refreshes never replace
    // the offer list, selected dossier or note drafts in the Offers workspace.
    readonly property var workspace: AppBridge.analysisWorkspace || ({})
    readonly property var candidates: workspace.candidates || []
    readonly property var runs: workspace.runs || []
    readonly property bool loading: AppBridge.analysisWorkspaceLoading
    readonly property string operation: AppBridge.analysisWorkspaceOperation || ""
    readonly property bool launching: loading && operation === "start"
    readonly property bool immediateRunning: loading && operation === "immediate"
    readonly property string error: AppBridge.analysisWorkspaceError || ""
    readonly property bool controlsEnabled: (!loading || immediateRunning)
        && !AppBridge.shutdownRequested && !AppBridge.analysisArchiving
    readonly property int paneDividerWidth: 29
    property string offerView: "available"
    readonly property bool viewingLot: offerView === "lot" && Boolean(selectedRun.id)
    readonly property var listedOffers: viewingLot ? (selectedRun.items || []) : candidates
    property string query: ""
    property var selectedOffers: ({})
    property string selectedRunId: ""
    property bool launchCancelRequested: false
    property int listRevision: 0
    readonly property var filteredCandidates: {
        var needle = query.trim().toLowerCase()
        return needle ? listedOffers.filter(function(row) {
            return [row.title, row.company, row.location, row.id, row.state_label]
                .join(" ").toLowerCase().indexOf(needle) >= 0
        }) : listedOffers
    }
    readonly property var selectedIds: candidates.filter(function(row) {
        return Boolean(selectedOffers[String(row.id)])
    }).map(function(row) { return Number(row.id) })
    readonly property var reservedOfferIds: runs.filter(function(run) {
        return run.is_active || run.can_cancel || run.status === "cancelling"
    }).reduce(function(ids, run) {
        return ids.concat((run.items || []).map(function(row) { return Number(row.id) }))
    }, [])

    function canArchive(jobId) {
        return jobId > 0 && controlsEnabled
            && (AppBridge.busyJobIds || []).indexOf(jobId) < 0
            && reservedOfferIds.indexOf(jobId) < 0
    }

    function confirmArchive(ids) {
        if (!ids.length || !ids.every(canArchive)) return
        archiveConfirm.ids = ids.slice()
        archiveConfirm.open()
    }
    readonly property var selectedRun: {
        var target = selectedRunId || String(workspace.active_run_id || "")
        for (var i = 0; i < runs.length; ++i)
            if (String(runs[i].id) === target) return runs[i]
        return runs.length > 0 ? runs[0] : ({})
    }

    function setSelected(jobId, checked) {
        var next = Object.assign({}, selectedOffers)
        if (checked) next[String(jobId)] = true
        else delete next[String(jobId)]
        selectedOffers = next
    }

    function toggleVisible(checked) {
        var next = Object.assign({}, selectedOffers)
        if (viewingLot) return
        filteredCandidates.forEach(function(row) {
            if (checked) next[String(row.id)] = true
            else delete next[String(row.id)]
        })
        selectedOffers = next
    }

    function startBatch() {
        if (selectedIds.length === 0) return
        selectedRunId = ""
        AppBridge.startAnalysisBatch(selectedIds)
    }

    function startImmediate() {
        if (selectedIds.length === 0) return
        immediateConfirm.ids = selectedIds
        immediateConfirm.open()
    }

    function historyLabel(run) {
        var created = new Date(String(run.created_at || ""))
        var date = isNaN(created.getTime()) ? "" : Qt.formatDateTime(created, "dd/MM HH:mm") + " · "
        return "Analyse n°" + Number(run.sequence || 1) + " · " + date
            + Number(run.total || 0) + (Number(run.total || 0) === 1 ? " offre · " : " offres · ") + (run.status_label || run.status)
    }

    function preserveCandidateState() {
        var previousY = offerTable.contentY
        var revision = listRevision
        var next = ({})
        candidates.forEach(function(row) {
            if (selectedOffers[String(row.id)]) next[String(row.id)] = true
        })
        selectedOffers = next
        Qt.callLater(function() {
            if (revision === listRevision)
                offerTable.contentY = Math.min(previousY,
                    Math.max(0, offerTable.contentHeight - offerTable.viewportHeight))
        })
    }

    function openFirstOffer() {
        if (!Number(AppBridge.analysisOfferId || 0) && offerTable.sortedOffers.length > 0)
            AppBridge.selectAnalysisOffer(Number(offerTable.sortedOffers[0].id))
    }

    Component.onCompleted: openFirstOffer()
    onCandidatesChanged: {
        preserveCandidateState()
        Qt.callLater(openFirstOffer)
    }
    onLaunchingChanged: if (!launching) launchCancelRequested = false
    onQueryChanged: {
        listRevision += 1
        offerTable.contentY = 0
    }
    onOfferViewChanged: {
        listRevision += 1
        offerTable.contentY = 0
    }

    Connections {
        target: AppBridge
        function onAnalysisOffersArchived(ids) {
            var next = Object.assign({}, root.selectedOffers)
            ids.forEach(function(jobId) { delete next[String(jobId)] })
            root.selectedOffers = next
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 14

        Rectangle {
            visible: root.error.length > 0
            Layout.fillWidth: true
            implicitHeight: errorRow.implicitHeight + 24
            color: Theme.dangerSoft
            border.color: Theme.dangerLine
            radius: Theme.radiusMedium
            RowLayout {
                id: errorRow
                anchors.fill: parent
                anchors.margins: 12
                Text {
                    objectName: "analysisWorkspaceError"
                    Layout.fillWidth: true
                    text: root.error
                    textFormat: Text.PlainText
                    color: Theme.danger
                    font.pixelSize: 12
                    wrapMode: Text.WordWrap
                }
                AppButton {
                    text: "Réessayer"
                    refined: true
                    enabled: root.controlsEnabled
                    onClicked: AppBridge.refreshAnalysisWorkspace()
                }
            }
        }

        SplitView {
            id: readingWorkspace
            objectName: "analysisReadingWorkspace"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.leftMargin: 10
            Layout.rightMargin: 10
            Layout.topMargin: 10
            orientation: Qt.Horizontal
            handle: Item {
                implicitWidth: root.paneDividerWidth
                Rectangle {
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.top: parent.top
                    anchors.bottom: parent.bottom
                    anchors.topMargin: 8
                    anchors.bottomMargin: 8
                    width: 1
                    color: SplitHandle.hovered || SplitHandle.pressed ? Theme.accentBright : Theme.line
                }
            }

            ColumnLayout {
                objectName: "analysisQueuePanel"
                SplitView.minimumWidth: 500
                SplitView.preferredWidth: (readingWorkspace.width - root.paneDividerWidth) * 0.5
                spacing: 10

                Flow {
                    id: analysisToolbar
                    objectName: "analysisToolbar"
                    Layout.fillWidth: true
                    Layout.preferredHeight: childrenRect.height
                    Layout.bottomMargin: 6
                    spacing: 8
                    RowLayout {
                        id: analysisToolbarActions
                        objectName: "analysisToolbarActions"
                        visible: !root.viewingLot || root.immediateRunning
                        width: implicitWidth
                        height: visible ? 36 : 0
                        spacing: 8
                        AppButton {
                            objectName: "analysisCancelImmediateAction"
                            visible: root.immediateRunning
                            text: "Arrêter l’analyse"
                            iconSource: Theme.icon("x")
                            kind: "danger"
                            refined: true
                            onClicked: AppBridge.cancelImmediateAnalysis()
                        }
                        AppButton {
                            objectName: "analysisStartImmediateAction"
                            visible: !root.viewingLot && !root.immediateRunning
                            text: "Analyse immédiate"
                            fontPixelSize: 11
                            implicitHeight: 36
                            refined: true
                            enabled: root.controlsEnabled && root.selectedIds.length > 0
                            onClicked: root.startImmediate()
                        }
                        AppButton {
                            objectName: "analysisStartBatchAction"
                            visible: !root.viewingLot && !root.immediateRunning
                            text: "Analyse par Batch"
                            fontPixelSize: 11
                            implicitHeight: 36
                            iconSource: Theme.icon("analysis")
                            kind: "primary"
                            enabled: root.controlsEnabled && root.selectedIds.length > 0
                            onClicked: root.startBatch()
                        }
                        AppButton {
                            objectName: "analysisArchiveSelectionAction"
                            visible: !root.viewingLot
                            text: AppBridge.analysisArchiving ? "Archivage…" : "Archiver (" + root.selectedIds.length + ")"
                            iconSource: Theme.icon("archive")
                            kind: "warning"
                            refined: true
                            fontPixelSize: 11
                            implicitHeight: 36
                            enabled: root.selectedIds.length > 0 && root.selectedIds.every(root.canArchive)
                            onClicked: root.confirmArchive(root.selectedIds)
                        }
                    }
                    RowLayout {
                        id: primaryControls
                        width: implicitWidth
                        height: 36
                        spacing: 8
                        AppButton {
                            id: refreshAction
                            objectName: "analysisRefreshAction"
                            text: root.loading && !root.immediateRunning ? "Actualisation…" : "Actualiser"
                            iconSource: Theme.icon("refresh")
                            refined: true
                            quiet: true
                            enabled: root.controlsEnabled
                            onClicked: AppBridge.refreshAnalysisWorkspace()
                        }
                        Item {
                            id: searchControl
                            objectName: "analysisExpandableSearch"
                            property bool expanded: false
                            Layout.minimumWidth: 34
                            Layout.preferredWidth: expanded ? Math.min(230, Math.max(34,
                                analysisToolbar.width - refreshAction.implicitWidth
                                - primaryControls.spacing)) : 34
                            Layout.maximumWidth: Layout.preferredWidth
                            Layout.preferredHeight: 34
                            clip: true

                            function open() {
                                expanded = true
                                Qt.callLater(function() { searchField.forceActiveFocus() })
                            }

                            function close(clearQuery) {
                                if (clearQuery && searchField.text.length > 0)
                                    searchField.clear()
                                searchField.focus = false
                                expanded = false
                            }

                            Behavior on Layout.preferredWidth {
                                NumberAnimation { duration: 180; easing.type: Easing.OutCubic }
                            }

                            AppButton {
                                objectName: "analysisSearchToggle"
                                anchors.fill: parent
                                visible: opacity > 0
                                opacity: searchControl.expanded ? 0 : 1
                                enabled: !searchControl.expanded
                                text: ""
                                iconSource: Theme.icon("search")
                                refined: true
                                iconSize: 14
                                implicitWidth: 34
                                implicitHeight: 34
                                Accessible.name: "Rechercher dans les offres à analyser"
                                onClicked: searchControl.open()
                                Behavior on opacity { NumberAnimation { duration: 90 } }
                            }

                            AppField {
                                id: searchField
                                objectName: "analysisSearchField"
                                anchors.fill: parent
                                visible: opacity > 0
                                opacity: searchControl.expanded ? 1 : 0
                                enabled: searchControl.expanded
                                implicitHeight: 34
                                font.pixelSize: 12
                                iconSource: Theme.icon("search")
                                placeholderText: "Entreprise, poste, ville…"
                                onTextChanged: root.query = text
                                onActiveFocusChanged: {
                                    if (!activeFocus && text.length === 0)
                                        searchCollapseTimer.restart()
                                }
                                Keys.onEscapePressed: function(event) {
                                    searchControl.close(true)
                                    event.accepted = true
                                }
                                Behavior on opacity { NumberAnimation { duration: 120 } }
                            }

                            Timer {
                                id: searchCollapseTimer
                                interval: 120
                                onTriggered: {
                                    if (!searchField.activeFocus && searchField.text.length === 0)
                                        searchControl.close(false)
                                }
                            }
                        }
                    }
                }
                AnalysisOfferTable {
                    id: offerTable
                    objectName: "analysisOfferTable"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    offers: root.filteredCandidates
                    selectedOffers: root.selectedOffers
                    selectionEnabled: !root.viewingLot
                    loading: root.loading
                    focusedOfferId: Number(AppBridge.analysisOfferId || 0)
                    searchActive: Boolean(root.query)
                    onSelectionToggled: function(jobId, checked) { root.setSelected(jobId, checked) }
                    onVisibleSelectionToggled: function(checked) { root.toggleVisible(checked) }
                    onOfferOpened: function(jobId, status) {
                        AppBridge.selectAnalysisOffer(jobId)
                    }
                }

                RowLayout {
                    objectName: "analysisActionBar"
                    Layout.fillWidth: true
                    spacing: 10
                    BusyIndicator {
                        visible: root.immediateRunning
                        running: visible
                        Layout.preferredWidth: 24
                        Layout.preferredHeight: 24
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 3
                        Text {
                            objectName: "analysisCandidateCount"
                            Layout.fillWidth: true
                            text: root.immediateRunning ? "Analyse immédiate en cours…"
                                : !root.viewingLot && root.selectedIds.length > 0
                                    ? root.selectedIds.length + (root.selectedIds.length === 1
                                        ? " offre sélectionnée" : " offres sélectionnées")
                                    : root.filteredCandidates.length + (root.filteredCandidates.length === 1
                                        ? " offre affichée" : " offres affichées")
                            color: root.selectedIds.length > 0 || root.immediateRunning
                                ? Theme.ink : Theme.inkMuted
                            font.pixelSize: 12
                            font.weight: Font.Medium
                        }
                    }
                    AppButton {
                        objectName: "analysisClearSelection"
                        visible: !root.viewingLot && root.selectedIds.length > 0
                        text: ""
                        iconSource: Theme.icon("x")
                        implicitWidth: 28
                        implicitHeight: 32
                        refined: true
                        quiet: true
                        fontPixelSize: 11
                        onClicked: root.selectedOffers = ({})
                        Accessible.name: "Effacer la sélection"
                    }
                }
            }
            Item {
                objectName: "analysisDetailPanel"
                SplitView.minimumWidth: 500
                SplitView.preferredWidth: (readingWorkspace.width - root.paneDividerWidth) * 0.5
                SplitView.fillWidth: true
                AnalysisOfferDetail {
                    objectName: "analysisOfferDetail"
                    anchors.fill: parent
                    offer: AppBridge.analysisOffer || ({})
                    loading: AppBridge.analysisOfferLoading
                    archiveEnabled: !loading && offer.status !== "archived" && root.canArchive(offerId)
                    requestedId: Number(AppBridge.analysisOfferId || 0)
                    onRetryRequested: AppBridge.selectAnalysisOffer(requestedId)
                    pageWidth: root.width
                    onSourceRequested: function(url) { AppBridge.openUrl(url) }
                    onArchiveRequested: function(jobId) { root.confirmArchive([jobId]) }
                }
            }
        }

        // Keep tracking compact. Expanded details scroll independently so the
        // offer table and its action bar always remain available.
        Item {
            objectName: "analysisTrackingBand"
            visible: Boolean(root.selectedRun.id) || root.launching
            Layout.fillWidth: true
            Layout.topMargin: 8
            Layout.preferredHeight: Math.min(runContent.implicitHeight + 24, root.height * 0.38)
            Flickable {
                id: runViewport
                objectName: "analysisTrackingViewport"
                anchors.fill: parent
                anchors.leftMargin: 16
                anchors.rightMargin: 16
                anchors.topMargin: 12
                anchors.bottomMargin: 12
                contentWidth: width
                contentHeight: runContent.implicitHeight
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: AppScrollBar {
                    visible: runViewport.contentHeight > runViewport.height + 2
                }
                ColumnLayout {
                    id: runContent
                    width: parent.width - (runViewport.contentHeight > runViewport.height + 2
                        ? Theme.scrollGutter : 0)
                    spacing: 8
                    AnalysisRunCard {
                        objectName: "analysisRunCard"
                        visible: Boolean(root.selectedRun.id) && !root.launching
                        Layout.fillWidth: true
                        surfaceColor: "transparent"
                        strokeColor: "transparent"
                        run: root.selectedRun
                        activeRunCount: Number(root.workspace.active_run_count || 0)
                        historyOptions: root.runs.map(function(run) {
                            return {value: String(run.id), label: root.historyLabel(run)}
                        })
                        controlsEnabled: root.controlsEnabled
                        onRunSelected: function(runId) { root.selectedRunId = runId }
                        onRetryRequested: function(runId) { AppBridge.retryAnalysisBatch(runId) }
                        onCancelRequested: function(runId) {
                            cancelDialog.runId = runId
                            cancelDialog.blocked = root.selectedRun.status === "blocked"
                            cancelDialog.open()
                        }
                    }
                    Surface {
                        objectName: "analysisLaunchCard"
                        visible: root.launching
                        Layout.fillWidth: true
                        padding: 0
                        surfaceColor: "transparent"
                        strokeColor: "transparent"
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 12
                            BusyIndicator {
                                Layout.preferredWidth: 28
                                Layout.preferredHeight: 28
                                running: root.launching
                            }
                            Text {
                                Layout.fillWidth: true
                                text: root.launchCancelRequested ? "Annulation demandée…" : "Préparation de l’analyse"
                                color: Theme.ink
                                font.pixelSize: 15
                                font.weight: Font.DemiBold
                            }
                            AppButton {
                                objectName: "analysisCancelLaunchAction"
                                text: "Annuler le lancement"
                                kind: "danger"
                                refined: true
                                enabled: !root.launchCancelRequested
                                onClicked: {
                                    root.launchCancelRequested = true
                                    AppBridge.cancelBatchStart()
                                }
                            }
                        }
                        AnalysisTimeline {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 82
                            run: ({
                                id: "launching", phase: "extraction", status: "preparing",
                                is_active: true, phase_batches: [
                                    {phase: "extraction", status: "preparing"}
                                ]
                            })
                        }
                    }
                }
            }
        }


    }

    ConfirmDialog {
        id: archiveConfirm
        objectName: "analysisArchiveConfirm"
        property var ids: []
        parent: Overlay.overlay
        anchors.centerIn: parent
        heading: ids.length === 1 ? "Archiver cette offre ?" : "Archiver ces offres ?"
        message: ids.length + (ids.length === 1
            ? " offre sera archivée." : " offres seront archivées.")
            + " Les données et les analyses existantes seront conservées."
        confirmText: "Archiver"
        kind: "warning"
        iconSource: Theme.icon("archive")
        confirmEnabled: ids.length > 0 && ids.every(root.canArchive)
        onAccepted: AppBridge.archiveAnalysisOffers(ids)
    }

    ConfirmDialog {
        id: immediateConfirm
        property var ids: []
        parent: Overlay.overlay
        anchors.centerIn: parent
        heading: "Analyser maintenant ?"
        message: ids.length + (ids.length === 1
            ? " offre sera analysée immédiatement." : " offres seront analysées immédiatement.")
        confirmEnabled: root.controlsEnabled
        confirmText: "Lancer l’analyse"
        onAccepted: AppBridge.startImmediateAnalysis(ids)
    }

    ConfirmDialog {
        id: cancelDialog
        property string runId: ""
        property bool blocked: false
        parent: Overlay.overlay
        anchors.centerIn: parent
        heading: "Annuler ce Batch ?"
        message: blocked
            ? "Cette analyse sera annulée. Les offres restantes pourront être sélectionnées à nouveau."
            : "Les résultats déjà obtenus seront conservés. Les offres restantes redeviendront disponibles dès que l’annulation de ce Batch sera confirmée."
        dismissText: "Retour"
        confirmText: "Annuler ce Batch"
        kind: "danger"
        onAccepted: AppBridge.cancelAnalysisBatch(runId)
    }
}
