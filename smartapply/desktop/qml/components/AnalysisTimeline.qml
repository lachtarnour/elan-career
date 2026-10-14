import QtQuick
import QtQuick.Layouts

Item {
    id: root
    property var run: ({})
    property bool compactStages: false
    property bool productStyle: false
    readonly property bool generation: run.kind === "generation"
    readonly property var sourcePhases: run.phase_batches || []
    readonly property var phases: {
        if (!compactStages) return run.phase_batches || [{phase: "screening"},
            {phase: "extraction"}, {phase: "evaluation"}]
        if (generation) return [
            displayPhase("selection", "Sélection des éléments pour le CV",
                ["cv_adaptation", "cv_selection_correction"]),
            displayPhase("construction", "Construction du CV", ["assembly"]),
            displayPhase("texts", "Génération du résumé et de la lettre", ["application_texts"]),
            displayPhase("review", "Relecture", ["document_review"]),
            displayPhase("correction", "Correction", ["document_correction", "motivation_letter_repair"]),
            displayPhase("confirmation", "Confirmation", ["confirmation", "rendering"])
        ]
        return [
            displayPhase("screening", "Préfiltre", ["screening"]),
            displayPhase("extraction", "Extraction", ["extraction"]),
            displayPhase("evaluation", "Évaluation", ["evaluation"])
        ]
    }
    readonly property int stepCount: phases.length + (compactStages ? 0 : 1)
    readonly property int completedSteps: {
        var count = 0
        for (var i = 0; i < phases.length; ++i) {
            if (phases[i].status !== "completed" && phases[i].status !== "skipped") break
            ++count
        }
        return count
    }
    readonly property bool hasRun: Boolean(run.id)
    readonly property bool finished: run.status === "completed"
    readonly property bool reachedOutcome: hasRun && run.phase === "done"
    readonly property bool terminalIssue: reachedOutcome && !finished
    readonly property var outcome: ({status: reachedOutcome ? run.status : "waiting",
        status_label: reachedOutcome ? run.status_label : ""})
    implicitHeight: compactStages ? (generation ? 100 : 82)
        : phases.some(function(phase) { return Boolean(phase.label) }) ? 100 : 82

    function phaseData(name) {
        for (var i = 0; i < phases.length; ++i)
            if (phases[i].phase === name) {
                if (["attention", "blocked", "paused", "interrupted"].indexOf(run.status) >= 0
                        && run.phase === name)
                    return Object.assign({}, phases[i], {status: run.status, status_label: run.status_label})
                return phases[i]
            }
        return ({status: "waiting"})
    }

    function displayPhase(name, label, sourceNames) {
        var members = sourcePhases.filter(function(phase) {
            return sourceNames.indexOf(phase.phase) >= 0
        })
        var active = sourceNames.indexOf(run.phase) >= 0
        var status = "waiting"
        var statusLabel = ""
        if (members.length === 0) {
            var correctionPassed = run.status === "completed"
                || ["confirmation", "rendering", "done"].indexOf(run.phase) >= 0
            if (name === "correction") {
                status = correctionPassed ? "skipped" : "waiting"
                statusLabel = correctionPassed ? "Non nécessaire" : ""
            } else if (!generation && name === "screening" && sourcePhases.length > 0) {
                status = "skipped"
                statusLabel = "Non exécuté"
            } else {
                status = sourcePhases.length > 0 ? "unrecorded" : "waiting"
                statusLabel = sourcePhases.length > 0 ? "Non documenté" : ""
            }
        } else {
            var issue = members.find(function(phase) {
                return ["failed", "expired", "attention", "blocked", "cancelled", "paused",
                    "interrupted", "partial"].indexOf(phase.status) >= 0
            })
            var moving = members.find(function(phase) {
                return ["preparing", "submitting", "in_progress", "finalizing", "importing",
                    "validating"].indexOf(phase.status) >= 0
            })
            if (issue) {
                status = issue.status
                statusLabel = issue.status_label || ""
            } else if (moving) {
                status = moving.status
                statusLabel = moving.status_label || ""
            } else if (members.every(function(phase) {
                return phase.status === "completed" || phase.status === "skipped"
            })) {
                status = members.some(function(phase) { return phase.status === "completed" })
                    ? "completed" : "skipped"
            }
        }
        if (active && ["attention", "blocked", "paused", "interrupted"].indexOf(run.status) >= 0) {
            status = run.status
            statusLabel = run.status_label || ""
        } else if (active && run.is_active && ["waiting", "unrecorded"].indexOf(status) >= 0) {
            status = "in_progress"
            statusLabel = ""
        }
        return {phase: name, label: label, sourcePhases: sourceNames, status: status,
            status_label: statusLabel}
    }

    Rectangle {
        objectName: "analysisTimelineTrack"
        x: parent.width / (2 * root.stepCount)
        y: root.productStyle ? 14 : 12
        width: parent.width * (root.stepCount - 1) / root.stepCount
        height: 2
        color: Theme.lineStrong
    }
    Rectangle {
        objectName: "analysisTimelineCompletedTrack"
        x: parent.width / (2 * root.stepCount)
        y: root.productStyle ? 14 : 12
        width: parent.width / root.stepCount * (root.compactStages
            ? Math.max(0, root.completedSteps - 1) : root.completedSteps)
        height: 2
        color: root.productStyle ? Theme.lineStrong : Theme.success
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0
        Repeater {
            model: root.phases
            delegate: AnalysisPhaseCard {
                required property var modelData
                required property int index
                Layout.fillWidth: true
                Layout.preferredWidth: 1
                Layout.alignment: Qt.AlignTop
                number: index + 1
                phase: root.compactStages ? modelData : root.phaseData(modelData.phase)
                simpleTitle: root.compactStages
                productStyle: root.productStyle
                current: root.hasRun && Boolean(root.run.is_active) && root.run.status !== "attention"
                    && (!root.productStyle || ["blocked", "paused", "interrupted", "failed",
                        "cancelled", "partial", "expired"].indexOf(root.run.status) < 0)
                    && (root.compactStages ? modelData.sourcePhases.indexOf(root.run.phase) >= 0
                        : root.run.phase === modelData.phase)
                complete: phase.status === "completed" || phase.status === "skipped"
                issue: ["failed", "expired", "attention", "blocked", "cancelled", "paused",
                    "interrupted", "partial"].indexOf(phase.status) >= 0
                started: root.hasRun && (index === 0 || root.phases[index - 1].status === "completed"
                    || (root.compactStages && root.phases[index - 1].status === "skipped") || current)
            }
        }
        Repeater {
            model: root.compactStages ? 0 : 1
            delegate: AnalysisPhaseCard {
                Layout.fillWidth: true
                Layout.preferredWidth: 1
                Layout.alignment: Qt.AlignTop
                number: root.stepCount
                phase: root.outcome
                complete: root.finished
                issue: root.terminalIssue
                started: root.reachedOutcome
            }
        }
    }
}
