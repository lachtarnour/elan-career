import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Surface {
    id: root
    property var run: ({})
    property bool controlsEnabled: true
    property bool detailsExpanded: false
    property bool errorsExpanded: false
    readonly property string runId: String(run.id || "")
    readonly property var errors: run.errors || []
    readonly property int total: Number(run.total || 0)
    readonly property int analyzed: Number(run.analyzed || 0)
    readonly property int failedCount: Number(run.failed || 0) + Number(run.skipped || 0)
    readonly property bool needsAction: ["blocked", "attention", "failed"].indexOf(run.status) >= 0
    readonly property int batchCount: (run.phase_batches || []).reduce(function(sum, phase) {
        return sum + Number(phase.batch_count || (phase.batches || []).length)
    }, 0)
    readonly property int submittedBatchCount: (run.phase_batches || []).reduce(function(sum, phase) {
        return sum + Number(phase.submitted_count !== undefined ? phase.submitted_count : phase.batch_count || 0)
    }, 0)
    signal retryRequested(string runId)
    signal cancelRequested(string runId)
    padding: 0
    surfaceColor: "transparent"
    strokeColor: "transparent"
    onRunIdChanged: {
        detailsExpanded = false
        errorsExpanded = false
    }

    function tone() {
        if (run.status === "completed") return "success"
        if (run.status === "failed") return "danger"
        if (["attention", "blocked", "partial", "expired"].indexOf(run.status) >= 0) return "warning"
        if (run.status === "cancelled") return "neutral"
        return "accent"
    }

    function dateLabel(value) {
        if (!value) return ""
        var date = new Date(String(value))
        return isNaN(date.getTime()) ? String(value) : Qt.formatDateTime(date, "dd/MM à HH:mm")
    }

    function actionMessage() {
        if (run.status === "blocked")
            return "Le service d’analyse est indisponible. Les offres sont conservées pour la reprise."
        if (run.status === "attention")
            return "L’envoi doit être vérifié avant de poursuivre. Les résultats enregistrés sont conservés."
        return "Le traitement a été interrompu. Vous pouvez reprendre les demandes restantes."
    }

    RowLayout {
        Layout.fillWidth: true
        spacing: 12
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 5
            Text {
                objectName: "analysisRunTitle"
                Layout.fillWidth: true
                text: "Analyse de " + root.total + (root.total === 1 ? " offre" : " offres")
                color: Theme.ink
                font.pixelSize: 16
                font.weight: Font.DemiBold
                elide: Text.ElideRight
            }
            Text {
                objectName: "analysisRunSummary"
                Layout.fillWidth: true
                text: "Analyse n°" + Number(root.run.sequence || 1)
                    + " · " + root.submittedBatchCount + (root.submittedBatchCount <= 1 ? " Batch envoyé" : " Batch envoyés")
                    + (root.batchCount > root.submittedBatchCount
                        ? " · " + (root.batchCount - root.submittedBatchCount) + " programmés" : "")
                    + " · " + root.analyzed + " / " + root.total
                    + (root.total === 1 ? " offre analysée" : " offres analysées")
                color: Theme.inkMuted
                font.pixelSize: 11
                elide: Text.ElideRight
            }
        }
        Pill {
            objectName: "analysisRunStatus"
            text: root.run.status_label || String(root.run.status || "")
            tone: root.tone()
            compact: true
        }
        AppButton {
            objectName: "analysisRetryAction"
            visible: Boolean(root.run.can_retry)
            text: "Reprendre"
            iconSource: Theme.icon("refresh")
            refined: true
            enabled: root.controlsEnabled
            onClicked: root.retryRequested(root.runId)
        }
        AppButton {
            objectName: "analysisCancelAction"
            visible: Boolean(root.run.can_cancel)
            text: "Annuler ce Batch"
            kind: "danger"
            refined: true
            quiet: true
            enabled: root.controlsEnabled
            onClicked: root.cancelRequested(root.runId)
        }
        AppButton {
            objectName: "analysisToggleDetails"
            text: root.detailsExpanded ? "Masquer les détails" : "Voir les détails"
            iconSource: Theme.icon("sliders")
            refined: true
            quiet: true
            onClicked: root.detailsExpanded = !root.detailsExpanded
            Accessible.name: text
        }
    }

    Rectangle {
        visible: root.needsAction
        Layout.fillWidth: true
        implicitHeight: actionRow.implicitHeight + 8
        color: "transparent"
        RowLayout {
            id: actionRow
            anchors.fill: parent
            anchors.margins: 4
            spacing: 9
            SvgIcon {
                Layout.preferredWidth: 15
                Layout.preferredHeight: 15
                source: Theme.icon("alert-circle")
                color: Theme.warning
            }
            Text {
                objectName: "analysisRunError"
                Layout.fillWidth: true
                text: root.actionMessage()
                color: Theme.warning
                font.pixelSize: 11
                wrapMode: Text.WordWrap
            }
        }
    }

    AnalysisTimeline {
        objectName: "analysisTimeline"
        Layout.fillWidth: true
        Layout.preferredHeight: 100
        run: root.run
    }

    ColumnLayout {
        objectName: "analysisRunDetails"
        visible: root.detailsExpanded
        Layout.fillWidth: true
        spacing: 12

        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
        GridLayout {
            Layout.fillWidth: true
            columns: root.width >= 1000 ? 2 : 1
            columnSpacing: 12
            rowSpacing: 12
            AnalysisBatchBreakdown {
                objectName: "analysisExtractionBatches"
                Layout.preferredWidth: 1
                Layout.alignment: Qt.AlignTop
                phase: (root.run.phase_batches || [])[0] || ({})
                title: "Extraction des offres"
                blocked: root.run.status === "blocked" && root.run.phase === "extraction"
            }
            AnalysisBatchBreakdown {
                objectName: "analysisEvaluationBatches"
                Layout.preferredWidth: 1
                Layout.alignment: Qt.AlignTop
                phase: (root.run.phase_batches || [])[1] || ({})
                title: "Évaluation de la pertinence"
                blocked: root.run.status === "blocked" && root.run.phase === "evaluation"
            }
        }
        Text {
            objectName: "analysisRunResult"
            visible: root.failedCount > 0
            Layout.fillWidth: true
            text: root.analyzed + " / " + root.total
                + (root.total === 1 ? " analyse enregistrée" : " analyses enregistrées")
                + (Number(root.run.failed || 0) > 0 ? " · " + root.run.failed + " en échec" : "")
                + (Number(root.run.skipped || 0) > 0 ? " · " + root.run.skipped + " ignorées" : "")
            color: Theme.warning
            font.pixelSize: 11
            wrapMode: Text.WordWrap
        }
        Text {
            objectName: "analysisLastCheck"
            Layout.fillWidth: true
            text: "Lancée le " + root.dateLabel(root.run.created_at)
                + (root.run.updated_at ? " · Dernière mise à jour le " + root.dateLabel(root.run.updated_at) : "")
            color: Theme.inkFaint
            font.pixelSize: 11
            wrapMode: Text.WordWrap
        }
        ColumnLayout {
            visible: root.errors.length > 0
            Layout.fillWidth: true
            spacing: 8
            RowLayout {
                Layout.fillWidth: true
                Text {
                    Layout.fillWidth: true
                    text: root.errors.length + (root.errors.length === 1
                        ? " offre à vérifier" : " offres à vérifier")
                    color: Theme.warning
                    font.pixelSize: 12
                    font.weight: Font.DemiBold
                    wrapMode: Text.WordWrap
                }
                AppButton {
                    visible: root.errors.length > 3
                    text: root.errorsExpanded ? "Réduire" : "Tout afficher"
                    refined: true
                    quiet: true
                    implicitHeight: 28
                    fontPixelSize: 11
                    onClicked: root.errorsExpanded = !root.errorsExpanded
                }
            }
            ListView {
                id: errorList
                objectName: "analysisFailedOffers"
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(190, count * 65)
                model: root.errorsExpanded ? root.errors : root.errors.slice(0, 3)
                clip: true
                reuseItems: true
                spacing: 8
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: AppScrollBar { }
                delegate: ColumnLayout {
                    required property var modelData
                    width: errorList.width - Theme.scrollGutter
                    spacing: 4
                    Text {
                        Layout.fillWidth: true
                        text: "#" + modelData.job_id + (modelData.title ? " · " + modelData.title : "")
                        textFormat: Text.PlainText
                        color: Theme.inkSoft
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                        wrapMode: Text.WordWrap
                    }
                    Text {
                        Layout.fillWidth: true
                        text: String(modelData.message || "")
                        textFormat: Text.PlainText
                        color: Theme.inkMuted
                        font.pixelSize: 11
                        wrapMode: Text.WordWrap
                    }
                }
            }
        }

    }
}
