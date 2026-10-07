import QtQuick
import QtQuick.Layouts

Item {
    id: root
    property var run: ({})
    readonly property var phases: run.phase_batches || []
    readonly property bool hasRun: Boolean(run.id)
    readonly property bool finished: run.status === "completed"
    readonly property bool reachedOutcome: hasRun && run.phase === "done"
    implicitHeight: 116

    function phaseData(name) {
        for (var i = 0; i < phases.length; ++i)
            if (phases[i].phase === name) {
                if (["attention", "blocked"].indexOf(run.status) >= 0 && run.phase === name)
                    return Object.assign({}, phases[i], {
                        status: run.status, status_label: run.status_label
                    })
                return phases[i]
            }
        return ({status: "waiting"})
    }

    readonly property var extraction: phaseData("extraction")
    readonly property var evaluation: phaseData("evaluation")
    readonly property bool extractionDone: extraction.status === "completed"
    readonly property bool evaluationDone: evaluation.status === "completed"
    readonly property bool terminalIssue: reachedOutcome && !finished
    readonly property var outcome: ({
        status: reachedOutcome ? run.status : "waiting",
        status_label: reachedOutcome ? run.status_label : ""
    })

    Rectangle {
        objectName: "analysisTimelineTrack"
        x: parent.width / 6
        y: 18
        width: parent.width * 2 / 3
        height: 2
        color: Theme.lineStrong
    }
    Rectangle {
        objectName: "analysisTimelineCompletedTrack"
        x: parent.width / 6
        y: 18
        width: parent.width / 3 * (root.evaluationDone ? 2 : root.extractionDone ? 1 : 0)
        height: 2
        color: Theme.success
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0
        AnalysisPhaseCard {
            Layout.fillWidth: true
            Layout.preferredWidth: 1
            number: 1
            phase: root.extraction
            current: root.hasRun && root.run.is_active && root.run.status !== "attention"
                && root.run.phase === "extraction"
            complete: root.extractionDone
            issue: ["failed", "expired", "attention", "blocked", "cancelled"].indexOf(root.extraction.status) >= 0
            started: root.hasRun
        }
        AnalysisPhaseCard {
            Layout.fillWidth: true
            Layout.preferredWidth: 1
            number: 2
            phase: root.evaluation
            current: root.hasRun && root.run.is_active && root.run.status !== "attention"
                && root.run.phase === "evaluation"
            complete: root.evaluationDone
            issue: ["failed", "expired", "attention", "blocked", "cancelled"].indexOf(root.evaluation.status) >= 0
            started: root.extractionDone || (root.hasRun && root.run.phase === "evaluation")
        }
        AnalysisPhaseCard {
            Layout.fillWidth: true
            Layout.preferredWidth: 1
            number: 3
            phase: root.outcome
            complete: root.finished
            issue: root.terminalIssue
            started: root.reachedOutcome
        }
    }
}
