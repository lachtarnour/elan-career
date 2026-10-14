import QtQuick
import QtQuick.Layouts

Item {
    id: root
    property var phase: ({})
    property int number: 1
    property bool current: false
    property bool complete: false
    property bool issue: false
    property bool started: false
    property bool simpleTitle: false
    property bool productStyle: false
    readonly property int total: Number(phase.total || 0)
    readonly property int completed: Number(phase.completed || 0)
    readonly property int failed: Number(phase.failed || 0)
    readonly property string state: String(phase.status || "waiting")
    readonly property color markerColor: productStyle && !issue && !current ? Theme.lineStrong : issue ? Theme.warning
        : complete ? Theme.success : current ? Theme.accentBright : Theme.lineStrong

    implicitHeight: phase.label ? 100 : 82

    function statusLabel(value) {
        var labels = {
            waiting: "En attente", preparing: "Préparation", submitting: "Envoi",
            validating: "Validation", in_progress: "En cours",
            finalizing: "Finalisation", importing: "Enregistrement",
            completed: "Terminé", skipped: "Non nécessaire", partial: "À reprendre", failed: "Échec",
            expired: "Délai dépassé", cancelling: "Annulation",
            cancelled: "Annulé", attention: "À vérifier", blocked: "Connexion à rétablir",
            paused: "Arrêté · reprise possible", interrupted: "Reprise possible"
        }
        if (productStyle && ["paused", "interrupted", "blocked"].indexOf(value) >= 0)
            return value === "paused" ? "En pause" : value === "blocked" ? "À reprendre" : "Interrompu"
        return value === "waiting" ? labels.waiting
            : String(phase.status_label || labels[value] || value)
    }

    function countLabel() {
        var singular = total === 1
        if (phase.execution_kind === "python")
            return completed + " / " + total + (singular ? " offre traitée" : " offres traitées")
        if (state === "blocked")
            return total + (singular ? " demande conservée" : " demandes conservées")
        if (["preparing", "prepared", "submitting"].indexOf(state) >= 0)
            return total + (singular ? " demande préparée" : " demandes préparées")
        var text = state === "completed"
            ? completed + " / " + total + (singular ? " demande réussie" : " demandes réussies")
            : (completed + failed) + " / " + total + (singular ? " demande traitée" : " demandes traitées")
        return failed > 0 ? text + " · " + failed + " en échec" : text
    }

    Rectangle {
        id: outerGlow
        objectName: "analysisPhaseGlow_" + root.number
        visible: root.current
        width: 30
        height: width
        radius: width / 2
        anchors.centerIn: marker
        color: "#33765FFF"
        opacity: 0.4
        SequentialAnimation on opacity {
            running: root.current && root.visible
            loops: Animation.Infinite
            NumberAnimation { to: 0.18; duration: 1050; easing.type: Easing.InOutSine }
            NumberAnimation { to: 0.48; duration: 1050; easing.type: Easing.InOutSine }
        }
    }

    Rectangle {
        id: marker
        objectName: "analysisPhaseMarker_" + root.number
        property bool active: root.current
        property bool finished: root.complete
        width: root.productStyle ? 20 : 14
        height: width
        radius: width / 2
        anchors.horizontalCenter: parent.horizontalCenter
        y: 5
        color: root.productStyle && !root.current ? Theme.surfaceRaised
            : root.complete ? Theme.success : root.current ? Theme.accent : Theme.surfaceRaised
        border.width: root.current ? 2 : 1
        border.color: root.markerColor
        z: 1
        Text {
            anchors.centerIn: parent
            visible: root.complete && (!root.productStyle || root.state !== "skipped")
            text: "✓"
            color: root.productStyle ? Theme.inkSoft : Theme.canvas
            font.pixelSize: root.productStyle ? 11 : 9
            font.weight: Font.Bold
        }
        Text {
            visible: root.productStyle && !root.current && !root.issue
                && (!root.complete || root.state === "skipped")
            anchors.centerIn: parent
            text: root.state === "skipped" ? "–" : String(root.number)
            color: Theme.inkMuted
            font.pixelSize: 11
            font.weight: Font.DemiBold
        }
        Text {
            anchors.centerIn: parent
            visible: root.issue && !root.complete && !root.current
            text: "!"
            color: Theme.warning
            font.pixelSize: 10
            font.weight: Font.Bold
        }
        Rectangle {
            visible: root.current
            width: root.productStyle ? 6 : 4
            height: width
            radius: width / 2
            anchors.centerIn: parent
            color: Theme.ink
        }
    }

    ColumnLayout {
        anchors.top: marker.bottom
        anchors.topMargin: 7
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.leftMargin: root.productStyle ? 8 : 0
        anchors.rightMargin: root.productStyle ? 8 : 0
        spacing: 2
        Text {
            objectName: "analysisPhaseTitle_" + root.number
            Layout.fillWidth: true
            Layout.minimumHeight: root.productStyle ? 32 : implicitHeight
            Layout.preferredHeight: root.productStyle ? 32 : implicitHeight
            Layout.maximumHeight: root.productStyle ? 32 : implicitHeight
            horizontalAlignment: Text.AlignHCenter
            text: root.simpleTitle ? String(root.phase.label || "")
                : !root.phase.phase ? "Bilan"
                : root.phase.label ? String(root.phase.label)
                    + (Number(root.phase.batch_count || 0) > 0 ? " · " + root.phase.batch_count + " Batch" : "")
                : (root.phase.phase === "screening" ? "Préfiltre" : root.phase.phase === "extraction" ? "Extraction" : "Évaluation") + " · "
                    + Number(root.phase.batch_count || 0) + " Batch"
            color: root.productStyle ? (root.current ? Theme.accentBright : root.started ? Theme.inkSoft : Theme.inkMuted)
                : root.started ? Theme.ink : Theme.inkFaint
            font.pixelSize: root.productStyle ? 12 : 11
            font.weight: Font.DemiBold
            maximumLineCount: root.productStyle ? 2 : root.phase.label ? 2 : 1
            wrapMode: root.phase.label ? Text.WordWrap : Text.NoWrap
            elide: Text.ElideRight
        }
        Text {
            objectName: "analysisPhaseStatus_" + root.number
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignHCenter
            text: root.statusLabel(root.state)
            textFormat: Text.PlainText
            color: root.issue ? Theme.warning : root.current ? Theme.accentBright : Theme.inkMuted
            font.pixelSize: root.productStyle ? 11 : 10
            elide: Text.ElideRight
        }
        Text {
            objectName: "analysisPhaseCounts_" + root.number
            visible: Boolean(root.phase.phase) && root.total > 0
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignHCenter
            text: root.countLabel()
            color: root.failed > 0 ? Theme.warning : Theme.inkMuted
            font.pixelSize: 10
            wrapMode: Text.WordWrap
        }
    }
}
