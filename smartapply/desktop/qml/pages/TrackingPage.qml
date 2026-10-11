import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    objectName: "trackingPage"
    property string tab: "generation"
    property bool initialTabSelected: false
    property string selectedAnalysisRunId: ""
    readonly property var analysisWorkspace: AppBridge.analysisWorkspace || ({})
    readonly property var generationWorkspace: AppBridge.generationWorkspace || ({})
    readonly property var analysisRuns: analysisWorkspace.runs || []
    readonly property bool analysisDesktopView: Boolean(analysisWorkspace.desktop_view)
    readonly property bool analysisLoading: AppBridge.analysisWorkspaceLoading
    readonly property string analysisOperation: AppBridge.analysisWorkspaceOperation || ""
    readonly property string analysisError: AppBridge.analysisWorkspaceError || ""
    readonly property bool immediateRunning: analysisLoading && analysisOperation === "immediate"
    readonly property bool analysisControlsEnabled: (!analysisLoading || immediateRunning)
        && !AppBridge.shutdownRequested
    readonly property int analysisHistoryPage: Math.max(1, Number(analysisWorkspace.history_page || 1))
    readonly property int analysisHistorySize: Math.max(1, Number(analysisWorkspace.history_page_size || 20))
    readonly property int analysisHistoryTotal: Number(analysisWorkspace.history_total_count || 0)
    readonly property var selectedAnalysisRun: {
        if (analysisDesktopView) return analysisWorkspace.selected_run || ({})
        var target = selectedAnalysisRunId || String(analysisWorkspace.active_run_id || "")
        for (var i = 0; i < analysisRuns.length; ++i)
            if (String(analysisRuns[i].id) === target) return analysisRuns[i]
        return analysisRuns.length ? analysisRuns[0] : ({})
    }
    property var analysisHistoryOptions: []
    property string analysisHistorySignature: ""

    function initializeTab() {
        if (initialTabSelected || !analysisWorkspace.desktop_view || !generationWorkspace.desktop_view) return
        if (Number(generationWorkspace.active_run_count || 0) > 0) tab = "generation"
        else if (Number(analysisWorkspace.active_run_count || 0) > 0) tab = "analysis"
        else if (!Number(generationWorkspace.history_total_count || 0)
                && Number(analysisWorkspace.history_total_count || 0)) tab = "analysis"
        initialTabSelected = true
    }

    function chooseTab(value) {
        initialTabSelected = true
        tab = value
    }

    function historyLabel(run) {
        var label = "Analyse n°" + Number(run.sequence || 1)
        var stamp = String(run.created_at || "")
        if (!stamp) return label
        var created = new Date(stamp)
        return label + (isNaN(created.getTime()) ? "" : " · " + Qt.formatDateTime(created, "dd/MM HH:mm"))
    }

    function updateAnalysisHistory() {
        var sources = (analysisRuns || []).map(function(run) {
            return {id: String(run.id), sequence: Number(run.sequence || 1),
                created_at: String(run.created_at || "")}
        })
        if (analysisDesktopView && selectedAnalysisRun && selectedAnalysisRun.id && !sources.some(function(run) {
                return run.id === String(root.selectedAnalysisRun.id)
            })) sources.unshift({id: String(selectedAnalysisRun.id),
                sequence: Number(selectedAnalysisRun.sequence || 1),
                created_at: String(selectedAnalysisRun.created_at || "")})
        var signature = JSON.stringify(sources)
        if (signature === analysisHistorySignature) return
        analysisHistorySignature = signature
        analysisHistoryOptions = sources.map(function(run) {
            return {value: run.id, label: root.historyLabel(run)}
        })
    }

    Component.onCompleted: {
        initializeTab()
        updateAnalysisHistory()
    }
    onAnalysisWorkspaceChanged: initializeTab()
    onGenerationWorkspaceChanged: initializeTab()
    onAnalysisRunsChanged: updateAnalysisHistory()
    onSelectedAnalysisRunChanged: updateAnalysisHistory()

    ColumnLayout {
        anchors.fill: parent
        spacing: 16
        PageHeader {
            Layout.fillWidth: true
            title: "Suivi"
        }
        Text {
            Layout.fillWidth: true
            text: "Les Batch envoyés continuent après fermeture. Le suivi se synchronise à la réouverture."
            color: Theme.inkMuted
            font.pixelSize: 12
            wrapMode: Text.WordWrap
        }
        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            AppButton {
                objectName: "trackingGenerationTab"
                text: "Créations"
                kind: root.tab === "generation" ? "primary" : "secondary"
                refined: true
                onClicked: root.chooseTab("generation")
            }
            AppButton {
                objectName: "trackingAnalysisTab"
                text: "Analyses"
                kind: root.tab === "analysis" ? "primary" : "secondary"
                refined: true
                onClicked: root.chooseTab("analysis")
            }
            Item { Layout.fillWidth: true }
        }
        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: root.tab === "analysis" ? 1 : 0
            Item {
                GenerationTracking {
                    id: generationTracking
                    anchors.fill: parent
                    embedded: true
                    expanded: true
                    workspace: root.generationWorkspace
                    loading: AppBridge.generationWorkspaceLoading
                    operation: AppBridge.generationWorkspaceOperation || ""
                    error: AppBridge.generationWorkspaceError || ""
                    controlsEnabled: !AppBridge.shutdownRequested
                    onRefreshRequested: AppBridge.refreshGenerationWorkspace()
                    onStopRequested: AppBridge.cancelImmediateGeneration()
                    onRetryRequested: function(runId) { AppBridge.retryGenerationRun(runId) }
                    onCancelRequested: function(runId) { AppBridge.cancelGenerationRun(runId) }
                    onAbandonRequested: function(runId) { AppBridge.abandonGenerationRun(runId) }
                    onHistoryPageRequested: function(page) { AppBridge.setGenerationHistoryPage(page) }
                    onRunSelected: function(runId) { AppBridge.selectGenerationRun(runId) }
                }
                ColumnLayout {
                    objectName: "trackingGenerationEmpty"
                    visible: !generationTracking.visible
                    anchors.centerIn: parent
                    spacing: 12
                    Text {
                        text: AppBridge.generationWorkspaceLoading ? "Lecture du suivi des créations…"
                            : "Aucune création enregistrée."
                        color: Theme.inkMuted
                        font.pixelSize: 13
                    }
                    AppButton {
                        text: "Actualiser"
                        enabled: !AppBridge.shutdownRequested && !AppBridge.generationWorkspaceLoading
                        onClicked: AppBridge.refreshGenerationWorkspace()
                    }
                }
            }
            ColumnLayout {
                spacing: 12
                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        Layout.fillWidth: true
                        text: root.immediateRunning ? "Analyse immédiate en cours…"
                            : root.analysisLoading && root.analysisOperation === "start" ? "Préparation du Batch…"
                            : root.analysisLoading ? "Actualisation du suivi…"
                            : root.analysisError ? "Dernier état enregistré"
                            : [root.selectedAnalysisRun.status_label || "", root.selectedAnalysisRun.phase_label || ""].filter(Boolean).join(" · ")
                        color: Theme.inkMuted
                        font.pixelSize: 12
                        elide: Text.ElideRight
                    }
                    AppButton {
                        objectName: "trackingAnalysisStopAction"
                        visible: root.analysisLoading && ["start", "immediate"].indexOf(root.analysisOperation) >= 0
                        text: "Arrêter"
                        kind: "danger"
                        refined: true
                        enabled: !AppBridge.shutdownRequested
                        onClicked: {
                            if (root.immediateRunning) AppBridge.cancelImmediateAnalysis()
                            else AppBridge.cancelBatchStart()
                        }
                    }
                    AppButton {
                        objectName: "trackingAnalysisRefreshAction"
                        text: "Actualiser"
                        refined: true
                        enabled: !AppBridge.shutdownRequested && !root.analysisLoading
                        onClicked: AppBridge.refreshAnalysisWorkspace()
                    }
                }
                Text {
                    objectName: "trackingAnalysisError"
                    visible: root.analysisError.length > 0
                    Layout.fillWidth: true
                    text: root.analysisError
                    textFormat: Text.PlainText
                    color: Theme.warning
                    font.pixelSize: 12
                    wrapMode: Text.WordWrap
                }
                Flickable {
                    objectName: "trackingAnalysisViewport"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    contentWidth: width
                    contentHeight: analysisContent.implicitHeight
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: AppScrollBar { }
                    ColumnLayout {
                        id: analysisContent
                        width: parent.width - Theme.scrollGutter
                        Text {
                            objectName: "trackingAnalysisEmpty"
                            visible: !Boolean(root.selectedAnalysisRun.id)
                            Layout.fillWidth: true
                            text: root.analysisLoading ? "Lecture du suivi des analyses…" : "Aucune analyse Batch enregistrée."
                            color: Theme.inkMuted
                            font.pixelSize: 13
                        }
                        AnalysisRunCard {
                            objectName: "trackingAnalysisRunCard"
                            visible: Boolean(root.selectedAnalysisRun.id)
                            Layout.fillWidth: true
                            run: root.selectedAnalysisRun
                            historyOptions: root.analysisHistoryOptions
                            activeRunCount: Number(root.analysisWorkspace.active_run_count || 0)
                            controlsEnabled: root.analysisControlsEnabled
                            retryEnabled: !root.analysisLoading
                            onRunSelected: function(runId) {
                                root.selectedAnalysisRunId = runId
                                if (root.analysisDesktopView) AppBridge.selectAnalysisRun(runId)
                            }
                            onRetryRequested: function(runId) { AppBridge.retryAnalysisBatch(runId) }
                            onCancelRequested: function(runId) {
                                cancelDialog.runId = runId
                                cancelDialog.blocked = root.selectedAnalysisRun.status === "blocked"
                                cancelDialog.open()
                            }
                        }
                    }
                }
                RowLayout {
                    objectName: "trackingAnalysisHistoryPagination"
                    visible: root.analysisDesktopView && root.analysisHistoryTotal > 0
                    Layout.fillWidth: true
                    Text {
                        Layout.fillWidth: true
                        text: root.analysisHistoryTotal + " analyses · page " + root.analysisHistoryPage
                            + " / " + Math.max(1, Math.ceil(root.analysisHistoryTotal / root.analysisHistorySize))
                        color: Theme.inkMuted
                        font.pixelSize: 11
                    }
                    AppButton {
                        objectName: "trackingAnalysisPreviousHistoryAction"
                        text: "‹"
                        Accessible.name: "Page précédente de l’historique des analyses"
                        refined: true
                        implicitWidth: 32
                        enabled: root.analysisHistoryPage > 1 && !root.analysisLoading && !AppBridge.shutdownRequested
                        onClicked: AppBridge.setAnalysisHistoryPage(root.analysisHistoryPage - 1)
                    }
                    AppButton {
                        objectName: "trackingAnalysisNextHistoryAction"
                        text: "›"
                        Accessible.name: "Page suivante de l’historique des analyses"
                        refined: true
                        implicitWidth: 32
                        enabled: root.analysisHistoryPage * root.analysisHistorySize < root.analysisHistoryTotal
                            && !root.analysisLoading && !AppBridge.shutdownRequested
                        onClicked: AppBridge.setAnalysisHistoryPage(root.analysisHistoryPage + 1)
                    }
                }
            }
        }
    }

    ConfirmDialog {
        id: cancelDialog
        objectName: "trackingAnalysisCancelConfirm"
        property string runId: ""
        property bool blocked: false
        parent: Overlay.overlay
        anchors.centerIn: parent
        heading: "Annuler ce Batch ?"
        message: blocked ? "Cette analyse sera annulée. Les offres restantes pourront être sélectionnées à nouveau."
            : "Les résultats déjà obtenus seront conservés. Les offres restantes redeviendront disponibles dès que l’annulation de ce Batch sera confirmée."
        dismissText: "Retour"
        confirmText: "Annuler ce Batch"
        confirmEnabled: root.analysisControlsEnabled
        kind: "danger"
        onAccepted: AppBridge.cancelAnalysisBatch(runId)
    }
}
