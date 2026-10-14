import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    objectName: "trackingPage"
    signal navigateRequested(string route)
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
    readonly property string displayedAnalysisRunId: String(selectedAnalysisRun.id || "")
    onDisplayedAnalysisRunIdChanged: if (analysisViewport) analysisViewport.contentY = 0
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
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.horizontalCenter: parent.horizontalCenter
        width: Math.min(parent.width, Theme.pageContentMaxWidth)
        spacing: 12
        PageHeader {
            Layout.fillWidth: true
            title: "Suivi"
            Rectangle {
                Layout.preferredWidth: 304
                Layout.preferredHeight: 40
                radius: 10
                color: Theme.canvasLift
                border.color: Theme.line
                RowLayout {
                    anchors.fill: parent
                    anchors.margins: 4
                    spacing: 4
                    AppButton {
                        objectName: "trackingGenerationTab"
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                        Layout.fillHeight: true
                        text: "Génération"
                        iconSource: Theme.icon("document")
                        kind: root.tab === "generation" ? "primary" : "secondary"
                        refined: true
                        Accessible.role: Accessible.PageTab
                        Accessible.selected: root.tab === "generation"
                        background: Rectangle {
                            radius: 9
                            color: root.tab === "generation" ? Theme.accentSoft : parent.hovered ? Theme.surface : "transparent"
                            border.color: parent.visualFocus ? Theme.accentBright
                                : root.tab === "generation" ? Theme.accentLine : "transparent"
                        }
                        onClicked: root.chooseTab("generation")
                    }
                    AppButton {
                        objectName: "trackingAnalysisTab"
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                        Layout.fillHeight: true
                        text: "Analyse"
                        iconSource: Theme.icon("analysis")
                        kind: root.tab === "analysis" ? "primary" : "secondary"
                        refined: true
                        Accessible.role: Accessible.PageTab
                        Accessible.selected: root.tab === "analysis"
                        background: Rectangle {
                            radius: 9
                            color: root.tab === "analysis" ? Theme.accentSoft : parent.hovered ? Theme.surface : "transparent"
                            border.color: parent.visualFocus ? Theme.accentBright
                                : root.tab === "analysis" ? Theme.accentLine : "transparent"
                        }
                        onClicked: root.chooseTab("analysis")
                    }
                }
            }
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
                Surface {
                    objectName: "trackingGenerationEmpty"
                    visible: !generationTracking.visible
                    anchors.fill: parent
                    anchors.rightMargin: Theme.scrollGutter
                    Item { Layout.fillHeight: true }
                    EmptyState {
                        iconSource: Theme.icon("document")
                        title: AppBridge.generationWorkspaceLoading ? "Lecture du suivi…" : "Vos générations apparaîtront ici"
                        message: "Lancez la génération d’un dossier depuis Offres, puis suivez ici chaque étape."
                    }
                    RowLayout {
                        Layout.alignment: Qt.AlignHCenter
                        spacing: 8
                        AppButton {
                            objectName: "trackingGenerationOffersAction"
                            text: "Voir les offres"
                            kind: "primary"
                            enabled: !AppBridge.shutdownRequested
                            onClicked: root.navigateRequested("jobs")
                        }
                        AppButton {
                            objectName: "trackingGenerationEmptyRefreshAction"
                            text: "Actualiser"
                            iconSource: Theme.icon("refresh")
                            enabled: !AppBridge.shutdownRequested && !AppBridge.generationWorkspaceLoading
                            onClicked: AppBridge.refreshGenerationWorkspace()
                        }
                    }
                    Item { Layout.fillHeight: true }
                }
            }
            ColumnLayout {
                spacing: 12
                RowLayout {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 30
                    Text {
                        Layout.fillWidth: true
                        text: root.immediateRunning ? "Analyse immédiate en cours…"
                            : root.analysisLoading && root.analysisOperation === "start" ? "Préparation du Batch…"
                            : root.analysisLoading ? "Actualisation du suivi…"
                            : root.analysisError ? "Dernier état enregistré"
                            : "Historique des analyses"
                        color: Theme.inkMuted
                        font.pixelSize: 11
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
                        iconSource: Theme.icon("refresh")
                        refined: true
                        implicitHeight: 30
                        enabled: !AppBridge.shutdownRequested && !root.analysisLoading
                        onClicked: AppBridge.refreshAnalysisWorkspace()
                    }
                }
                TrackingNotice {
                    objectName: "trackingAnalysisError"
                    visible: root.analysisError.length > 0
                    Layout.fillWidth: true
                    heading: "La synchronisation n’a pas abouti"
                    message: root.analysisError
                }
                Flickable {
                    id: analysisViewport
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
                        Surface {
                            objectName: "trackingAnalysisEmpty"
                            visible: !Boolean(root.selectedAnalysisRun.id)
                            Layout.fillWidth: true
                            Layout.preferredHeight: Math.max(340, analysisViewport.height)
                            Item { Layout.fillHeight: true }
                            EmptyState {
                                iconSource: Theme.icon("analysis")
                                title: root.analysisLoading ? "Lecture du suivi…" : "Vos analyses apparaîtront ici"
                                message: "Lancez une analyse Batch depuis les offres à analyser pour retrouver son avancement ici."
                            }
                            AppButton {
                                objectName: "trackingAnalysisOffersAction"
                                Layout.alignment: Qt.AlignHCenter
                                text: "Voir les offres à analyser"
                                kind: "primary"
                                enabled: !AppBridge.shutdownRequested
                                onClicked: root.navigateRequested("analysis")
                            }
                            Item { Layout.fillHeight: true }
                        }
                        TrackingRunCard {
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
                    Layout.rightMargin: Theme.scrollGutter
                    spacing: 8
                    Text {
                        Layout.fillWidth: true
                        text: root.analysisHistoryTotal + (root.analysisHistoryTotal === 1 ? " analyse enregistrée" : " analyses enregistrées")
                        color: Theme.inkFaint
                        font.pixelSize: 11
                    }
                    Text {
                        text: "Page " + root.analysisHistoryPage + " / " + Math.max(1, Math.ceil(root.analysisHistoryTotal / root.analysisHistorySize))
                        color: Theme.inkMuted
                        font.pixelSize: 11
                    }
                    AppButton {
                        objectName: "trackingAnalysisPreviousHistoryAction"
                        text: ""
                        iconSource: Theme.icon("chevron-right")
                        contentItem.rotation: 180
                        Accessible.name: "Page précédente de l’historique des analyses"
                        refined: true
                        implicitWidth: 30
                        implicitHeight: 30
                        enabled: root.analysisHistoryPage > 1 && !root.analysisLoading && !AppBridge.shutdownRequested
                        onClicked: AppBridge.setAnalysisHistoryPage(root.analysisHistoryPage - 1)
                    }
                    AppButton {
                        objectName: "trackingAnalysisNextHistoryAction"
                        text: ""
                        iconSource: Theme.icon("chevron-right")
                        Accessible.name: "Page suivante de l’historique des analyses"
                        refined: true
                        implicitWidth: 30
                        implicitHeight: 30
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
