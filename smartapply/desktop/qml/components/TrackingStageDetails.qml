import QtQuick
import QtQuick.Layouts

Surface {
    id: root
    property var stage: ({})
    property var sourcePhases: []
    property int number: 1
    property bool current: false
    readonly property var members: sourcePhases.filter(function(phase) {
        return (stage.sourcePhases || []).indexOf(phase.phase) >= 0
    })
    readonly property bool issue: ["failed", "partial", "attention", "blocked", "paused",
        "interrupted", "expired", "cancelled"].indexOf(stage.status) >= 0
    readonly property bool complete: stage.status === "completed"
    readonly property color stateColor: issue ? Theme.warning : complete ? Theme.inkMuted
        : current ? Theme.accentBright : Theme.inkFaint
    readonly property var rows: {
        var result = []
        members.forEach(function(phase) {
            var batches = phase.batches || []
            if (batches.length) batches.forEach(function(batch) {
                result.push({phase: phase, data: batch, label: (members.length > 1
                    ? root.sourceLabel(phase.phase) + " · " : "") + "Batch " + Number(batch.number || 1)})
            })
            else if (Number(phase.total || 0) > 0)
                result.push({phase: phase, data: phase, label: phase.execution_kind === "python"
                    ? (members.length > 1 ? root.sourceLabel(phase.phase) : "Traitement local")
                    : members.length > 1 ? root.sourceLabel(phase.phase) : "Demandes"})
        })
        return result
    }
    padding: 12
    verticalPadding: 10
    radiusValue: 10
    surfaceColor: Theme.canvasLift
    strokeColor: issue ? Theme.warningLine : current ? "#514574" : "#2E293A"

    function sourceLabel(name) {
        return ({cv_adaptation: "Sélection", cv_selection_correction: "Vérification",
            assembly: "Assemblage", application_texts: "Textes de candidature",
            document_review: "Relecture", document_correction: "Correction",
            motivation_letter_repair: "Ajustement de la lettre",
            confirmation: "Vérification finale", rendering: "Documents finalisés"})[name] || "Demandes"
    }

    function statusLabel(status, label) {
        var labels = {waiting: "En attente", prepared: "Prêt à démarrer", preparing: "Préparation",
            submitting: "Envoi en cours", submitted: "En cours", in_progress: "En cours",
            validating: "Validation", finalizing: "Finalisation", importing: "Enregistrement",
            completed: "Terminé", skipped: "Non nécessaire", unrecorded: "Non documenté",
            partial: "À reprendre", failed: "Échec", attention: "À vérifier", blocked: "À reprendre",
            paused: "En pause", interrupted: "Interrompu", expired: "Délai dépassé",
            cancelling: "Arrêt en cours", cancelled: "Arrêté", ended: "Terminé", active: "En cours"}
        return label && label !== status ? String(label) : labels[status] || "En attente"
    }

    ColumnLayout {
        Layout.fillWidth: true
        Layout.alignment: Qt.AlignTop
        spacing: 8
        RowLayout {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignTop
            spacing: 8
            Rectangle {
                Layout.preferredWidth: 24
                Layout.preferredHeight: 24
                radius: 7
                color: root.current ? Theme.accentSoft : Theme.surface
                border.color: root.current ? Theme.accentLine : Theme.line
                Text {
                    anchors.centerIn: parent
                    text: root.complete ? "✓" : String(root.number)
                    color: root.stateColor
                    font.pixelSize: 12
                    font.weight: Font.DemiBold
                }
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 3
                Text {
                    objectName: "trackingStageDetailTitle_" + root.number
                    Layout.fillWidth: true
                    text: root.stage.label || ""
                    color: Theme.inkSoft
                    font.pixelSize: 12
                    font.weight: Font.DemiBold
                    wrapMode: Text.WordWrap
                }
                Text {
                    Layout.fillWidth: true
                    text: root.statusLabel(root.stage.status, root.stage.status_label)
                    color: root.stateColor
                    font.pixelSize: 11
                    elide: Text.ElideRight
                }
            }
        }
        Text {
            visible: root.rows.length === 0
            Layout.fillWidth: true
            text: root.stage.status === "skipped" ? (root.stage.phase === "correction"
                ? "Aucune correction nécessaire." : root.members.length > 0
                    ? "Aucune demande à traiter pour cette étape."
                    : "Cette étape n’a pas été exécutée pour ce traitement.")
                : root.stage.status === "unrecorded" ? "Aucun détail enregistré pour cette étape."
                : "Les détails apparaîtront au démarrage de cette étape."
            color: Theme.inkFaint
            font.pixelSize: 11
            wrapMode: Text.WordWrap
        }
        Repeater {
            model: root.rows
            delegate: ColumnLayout {
                id: batchRow
                required property var modelData
                Layout.fillWidth: true
                spacing: 4
                readonly property var batchData: modelData.data
                readonly property int total: Number(batchData.total || 0)
                readonly property int failed: Number(batchData.failed || 0)
                readonly property int processed: Math.min(total, Number(batchData.completed || 0) + failed)
                readonly property string state: String(batchData.status || "waiting")
                readonly property color barColor: failed > 0 ? Theme.warning : Theme.blue
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 10
                    Text {
                        Layout.fillWidth: true
                        text: modelData.label
                        color: Theme.inkMuted
                        font.pixelSize: 11
                        elide: Text.ElideRight
                    }
                    Text {
                        objectName: "analysisBatchState_" + modelData.phase.phase + "_" + Number(batchRow.batchData.number || 1)
                        text: root.statusLabel(batchRow.state, batchRow.batchData.status_label)
                        color: batchRow.failed > 0 ? Theme.warning : Theme.inkFaint
                        font.pixelSize: 10
                    }
                    Text {
                        objectName: "analysisBatchProgress_" + modelData.phase.phase + "_" + Number(batchRow.batchData.number || 1)
                        text: batchRow.processed + " / " + batchRow.total
                        color: Theme.inkSoft
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                    }
                }
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 3
                    radius: 2
                    color: "#2A2536"
                    Rectangle {
                        width: parent.width * (batchRow.total > 0 ? batchRow.processed / batchRow.total : 0)
                        height: parent.height
                        radius: 2
                        color: batchRow.barColor
                    }
                }
                Text {
                    visible: batchRow.failed > 0
                    text: batchRow.failed + (batchRow.failed === 1 ? " demande en échec" : " demandes en échec")
                    color: Theme.warning
                    font.pixelSize: 10
                }
            }
        }
    }
}
