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
    readonly property int total: Number(phase.total || 0)
    readonly property int completed: Number(phase.completed || 0)
    readonly property int failed: Number(phase.failed || 0)
    readonly property string state: String(phase.status || "waiting")
    readonly property color markerColor: issue ? Theme.warning
        : complete ? Theme.success : current ? Theme.accentBright : Theme.lineStrong

    implicitHeight: 82

    function statusLabel(value) {
        var labels = {
            waiting: "En attente", preparing: "Préparation", submitting: "Envoi",
            validating: "Validation", in_progress: "En cours",
            finalizing: "Finalisation", importing: "Enregistrement",
            completed: "Terminé", partial: "À reprendre", failed: "Échec",
            expired: "Délai dépassé", cancelling: "Annulation",
            cancelled: "Annulé", attention: "À vérifier", blocked: "Connexion à rétablir"
        }
        return value === "waiting" ? labels.waiting
            : String(phase.status_label || labels[value] || value)
    }

    function countLabel() {
        var singular = total === 1
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
        height: 30
        radius: 15
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
        width: 14
        height: 14
        radius: 7
        anchors.horizontalCenter: parent.horizontalCenter
        y: 5
        color: root.complete ? Theme.success : root.current ? Theme.accent : Theme.surfaceRaised
        border.width: root.current ? 2 : 1
        border.color: root.markerColor
        z: 1
        Text {
            anchors.centerIn: parent
            visible: root.complete
            text: "✓"
            color: Theme.canvas
            font.pixelSize: 9
            font.weight: Font.Bold
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
            width: 4
            height: 4
            radius: 2
            anchors.centerIn: parent
            color: Theme.ink
        }
    }

    ColumnLayout {
        anchors.top: marker.bottom
        anchors.topMargin: 7
        anchors.left: parent.left
        anchors.right: parent.right
        spacing: 2
        Text {
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignHCenter
            text: !root.phase.phase ? "Bilan"
                : (root.phase.phase === "screening" ? "Préfiltre" : root.phase.phase === "extraction" ? "Extraction" : "Évaluation") + " · "
                    + Number(root.phase.batch_count || 0) + " Batch"
            color: root.started ? Theme.ink : Theme.inkFaint
            font.pixelSize: 11
            font.weight: Font.DemiBold
            elide: Text.ElideRight
        }
        Text {
            objectName: "analysisPhaseStatus_" + root.number
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignHCenter
            text: root.statusLabel(root.state)
            textFormat: Text.PlainText
            color: root.issue ? Theme.warning : root.current ? Theme.accentBright : Theme.inkMuted
            font.pixelSize: 10
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
