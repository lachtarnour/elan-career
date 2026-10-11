import QtQuick
import QtQuick.Layouts

Item {
    id: root
    property var run: ({})
    readonly property var phases: run.phase_batches || [{phase: "screening"}, {phase: "extraction"}, {phase: "evaluation"}]
    readonly property int stepCount: phases.length + 1
    readonly property int completedSteps: phases.filter(function(p) { return p.status === "completed" || p.status === "skipped" }).length
    readonly property bool hasRun: Boolean(run.id)
    readonly property bool finished: run.status === "completed"
    readonly property bool reachedOutcome: hasRun && run.phase === "done"
    implicitHeight: 82

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
        x: parent.width / (2 * root.stepCount)
        y: 12
        width: parent.width * (root.stepCount - 1) / root.stepCount
        height: 2
        color: Theme.lineStrong
    }
    Rectangle {
        objectName: "analysisTimelineCompletedTrack"
        x: parent.width / (2 * root.stepCount)
        y: 12
        width: parent.width / root.stepCount * root.completedSteps
        height: 2
        color: Theme.success
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
                number: index + 1
                phase: root.phaseData(modelData.phase)
                current: root.hasRun && root.run.is_active && root.run.status !== "attention"
                    && root.run.phase === modelData.phase
                complete: phase.status === "completed" || phase.status === "skipped"
                issue: ["failed", "expired", "attention", "blocked", "cancelled"].indexOf(phase.status) >= 0
                started: root.hasRun && (index === 0 || root.phases[index - 1].status === "completed" || root.run.phase === modelData.phase)
            }
        }
        AnalysisPhaseCard {
            Layout.fillWidth: true
            Layout.preferredWidth: 1
            number: root.stepCount
            phase: root.outcome
            complete: root.finished
            issue: root.terminalIssue
            started: root.reachedOutcome
        }
    }
}
