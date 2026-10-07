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

    implicitHeight: 108

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
        width: 38
        height: 38
        radius: 19
        anchors.centerIn: marker
        color: "#33765FFF"
        opacity: 0.58
        SequentialAnimation on opacity {
            running: root.current && root.visible
            loops: Animation.Infinite
            NumberAnimation { to: 0.22; duration: 1050; easing.type: Easing.InOutSine }
            NumberAnimation { to: 0.7; duration: 1050; easing.type: Easing.InOutSine }
        }
    }

    Rectangle {
        id: marker
        objectName: "analysisPhaseMarker_" + root.number
        property bool active: root.current
        property bool finished: root.complete
        width: 18
        height: 18
        radius: 9
        anchors.horizontalCenter: parent.horizontalCenter
        y: 10
        color: root.complete ? Theme.success : root.current ? Theme.accent : Theme.surfaceRaised
        border.width: root.current ? 2 : 1
        border.color: root.markerColor
        z: 1
        Text {
            anchors.centerIn: parent
            visible: root.complete
            text: "✓"
            color: Theme.canvas
            font.pixelSize: 11
            font.weight: Font.Bold
        }
        Text {
            anchors.centerIn: parent
            visible: root.issue && !root.complete && !root.current
            text: "!"
            color: Theme.warning
            font.pixelSize: 12
            font.weight: Font.Bold
        }
        Rectangle {
            visible: root.current
            width: 6
            height: 6
            radius: 3
            anchors.centerIn: parent
            color: Theme.ink
        }
    }

    ColumnLayout {
        anchors.top: marker.bottom
        anchors.topMargin: 12
        anchors.left: parent.left
        anchors.right: parent.right
        spacing: 4
        Text {
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignHCenter
            text: root.number === 3 ? "Bilan"
                : (root.number === 1 ? "Extraction" : "Évaluation") + " · "
                    + Number(root.phase.batch_count || 0) + " Batch"
            color: root.started ? Theme.ink : Theme.inkFaint
            font.pixelSize: 12
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
            font.pixelSize: 11
            elide: Text.ElideRight
        }
        Text {
            objectName: "analysisPhaseCounts_" + root.number
            visible: root.number < 3 && root.total > 0
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignHCenter
            text: root.countLabel()
            color: root.failed > 0 ? Theme.warning : Theme.inkMuted
            font.pixelSize: 10
            wrapMode: Text.WordWrap
        }
    }
}
