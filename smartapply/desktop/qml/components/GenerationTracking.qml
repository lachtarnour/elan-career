import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    objectName: "generationTrackingBand"
    property var workspace: ({})
    property bool loading: false
    property string operation: ""
    property string error: ""
    property bool controlsEnabled: true
    property bool expanded: false
    property bool embedded: false
    property string selectedRunId: ""
    readonly property bool desktopView: Boolean(workspace.desktop_view)
    readonly property var runs: workspace.runs || []
    readonly property int historyPage: Math.max(1, Number(workspace.history_page || 1))
    readonly property int historyPageSize: Math.max(1, Number(workspace.history_page_size || 20))
    readonly property int historyTotal: desktopView ? Number(workspace.history_total_count || 0) : runs.length
    readonly property bool commandRunning: ["start", "immediate", "retry"].indexOf(operation) >= 0
    readonly property var selectedRun: {
        if (desktopView) return workspace.selected_run || ({})
        var target = selectedRunId || String(workspace.active_run_id || "")
        for (var i = 0; i < runs.length; ++i)
            if (String(runs[i].id) === target) return runs[i]
        return runs.length ? runs[0] : ({})
    }
    signal refreshRequested()
    signal stopRequested()
    signal retryRequested(string runId)
    signal cancelRequested(string runId)
    signal abandonRequested(string runId)
    signal historyPageRequested(int page)
    signal runSelected(string runId)
    property var historyOptions: []
    property string historyOptionsSignature: ""

    function updateHistoryOptions() {
        var sources = (runs || []).map(function(run) {
            return {id: String(run.id), sequence: Number(run.sequence || 1),
                created_at: String(run.created_at || "")}
        })
        if (desktopView && selectedRun && selectedRun.id && !sources.some(function(source) {
                return source.id === String(root.selectedRun.id)
            })) sources.unshift({id: String(selectedRun.id), sequence: Number(selectedRun.sequence || 1),
                created_at: String(selectedRun.created_at || "")})
        var signature = JSON.stringify(sources)
        if (signature === historyOptionsSignature) return
        historyOptionsSignature = signature
        historyOptions = sources.map(function(run) {
            return {value: run.id, label: root.historyLabel(run)}
        })
    }
    Component.onCompleted: updateHistoryOptions()
    onRunsChanged: updateHistoryOptions()
    onSelectedRunChanged: updateHistoryOptions()
    visible: runs.length > 0 || Boolean(selectedRun.id) || commandRunning || error.length > 0
    implicitHeight: visible ? (expanded ? (embedded ? content.implicitHeight + 56
        : Math.min(content.implicitHeight + 56, 240)) : 34) : 0

    function historyLabel(run) {
        var stamp = String(run.created_at || "")
        if (!stamp) return "Création n°" + Number(run.sequence || 1)
        var created = new Date(stamp)
        var date = isNaN(created.getTime()) ? "" : " · " + Qt.formatDateTime(created, "dd/MM HH:mm")
        return "Création n°" + Number(run.sequence || 1) + date
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 6
        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 30
            spacing: 8
            AppButton {
                objectName: "generationTrackingToggle"
                visible: !root.embedded
                text: root.expanded ? "Réduire le suivi" : "Suivi des créations"
                iconSource: Theme.icon("analysis")
                refined: true
                quiet: true
                implicitHeight: 30
                fontPixelSize: 11
                onClicked: root.expanded = !root.expanded
            }
            Text {
                objectName: "generationTrackingStatus"
                Layout.fillWidth: true
                text: root.commandRunning ? "Création en cours…"
                    : root.operation === "refresh" ? "Actualisation du suivi…"
                    : root.embedded && root.error ? "Dernier état enregistré"
                    : [root.selectedRun.status_label || "", root.selectedRun.phase_label || ""].filter(Boolean).join(" · ")
                color: ["paused", "partial", "failed", "blocked", "attention"].indexOf(root.selectedRun.status) >= 0
                    ? Theme.warning : Theme.inkMuted
                font.pixelSize: 11
                elide: Text.ElideRight
            }
            AppButton {
                objectName: "generationStopImmediateAction"
                visible: root.commandRunning
                text: "Arrêter"
                kind: "danger"
                refined: true
                implicitHeight: 30
                enabled: root.controlsEnabled
                onClicked: root.stopRequested()
            }
            AppButton {
                objectName: "generationRefreshAction"
                text: ""
                iconSource: Theme.icon("refresh")
                refined: true
                quiet: true
                implicitWidth: 30
                implicitHeight: 30
                Accessible.name: "Actualiser le suivi des créations"
                enabled: root.controlsEnabled && !root.loading
                onClicked: root.refreshRequested()
            }
        }
        Flickable {
            id: viewport
            objectName: "generationTrackingViewport"
            visible: root.expanded
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: width
            contentHeight: content.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: AppScrollBar { visible: viewport.contentHeight > viewport.height + 2 }
            ColumnLayout {
                id: content
                width: viewport.width - (viewport.contentHeight > viewport.height + 2 ? Theme.scrollGutter : 0)
                spacing: 8
                Text {
                    objectName: "generationWorkspaceError"
                    visible: root.error.length > 0
                    Layout.fillWidth: true
                    text: root.error
                    textFormat: Text.PlainText
                    color: Theme.warning
                    font.pixelSize: 12
                    wrapMode: Text.WordWrap
                }
                AnalysisRunCard {
                    objectName: "generationRunCard"
                    visible: Boolean(root.selectedRun.id)
                    Layout.fillWidth: true
                    run: root.selectedRun
                    activeRunCount: Number(root.workspace.active_run_count || 0)
                    historyOptions: root.historyOptions
                    controlsEnabled: root.controlsEnabled && !root.commandRunning
                    retryEnabled: !root.loading
                    onRunSelected: function(runId) {
                        root.selectedRunId = runId
                        if (root.desktopView) root.runSelected(runId)
                    }
                    onRetryRequested: function(runId) { root.retryRequested(runId) }
                    onCancelRequested: function(runId) { root.cancelRequested(runId) }
                    onAbandonRequested: function(runId) {
                        abandonConfirm.runId = runId
                        abandonConfirm.open()
                    }
                }
                RowLayout {
                    objectName: "generationHistoryPagination"
                    visible: root.desktopView
                    Layout.fillWidth: true
                    Text {
                        Layout.fillWidth: true
                        text: root.historyTotal + " créations · page " + root.historyPage
                            + " / " + Math.max(1, Math.ceil(root.historyTotal / root.historyPageSize))
                        color: Theme.inkMuted
                        font.pixelSize: 11
                    }
                    AppButton {
                        objectName: "generationPreviousHistoryPageAction"
                        text: "‹"
                        Accessible.name: "Page précédente de l’historique des créations"
                        refined: true
                        implicitWidth: 32
                        implicitHeight: 30
                        enabled: root.historyPage > 1 && !root.loading && root.controlsEnabled
                        onClicked: root.historyPageRequested(root.historyPage - 1)
                    }
                    AppButton {
                        objectName: "generationNextHistoryPageAction"
                        text: "›"
                        Accessible.name: "Page suivante de l’historique des créations"
                        refined: true
                        implicitWidth: 32
                        implicitHeight: 30
                        enabled: root.historyPage * root.historyPageSize < root.historyTotal
                            && !root.loading && root.controlsEnabled
                        onClicked: root.historyPageRequested(root.historyPage + 1)
                    }
                }
            }
        }
    }

    ConfirmDialog {
        id: abandonConfirm
        objectName: "generationAbandonConfirm"
        property string runId: ""
        parent: Overlay.overlay
        anchors.centerIn: parent
        heading: "Abandonner cette création ?"
        message: "Les offres restantes seront libérées. Les étapes enregistrées et les candidatures créées seront conservées."
        dismissText: "Retour"
        confirmText: "Libérer les offres"
        kind: "warning"
        confirmEnabled: root.controlsEnabled && !root.commandRunning
        onAccepted: root.abandonRequested(runId)
    }
}
