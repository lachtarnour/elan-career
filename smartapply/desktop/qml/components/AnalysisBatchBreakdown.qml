import QtQuick
import QtQuick.Layouts

Item {
    id: root
    property var phase: ({})
    property string title: ""
    property bool blocked: false
    readonly property var batches: phase.batches || []

    visible: batches.length > 0
    Layout.fillWidth: true
    implicitHeight: content.implicitHeight + 4

    ColumnLayout {
        id: content
        anchors.fill: parent
        anchors.margins: 2
        spacing: 7

        RowLayout {
            Layout.fillWidth: true
            Text {
                Layout.fillWidth: true
                text: root.title
                color: Theme.ink
                font.pixelSize: 12
                font.weight: Font.DemiBold
            }
            Text {
                visible: root.batches.length > 1
                text: root.batches.length + " Batch"
                color: Theme.inkMuted
                font.pixelSize: 11
            }
        }

        Repeater {
            model: root.batches
            delegate: ColumnLayout {
                id: batchRow
                required property var modelData
                Layout.fillWidth: true
                spacing: 4
                readonly property int total: Number(modelData.total || 0)
                readonly property int processed: Math.min(total,
                    Number(modelData.completed || 0) + Number(modelData.failed || 0))
                readonly property string batchState: root.blocked && modelData.status === "prepared"
                    ? "Connexion à rétablir" : String(modelData.status_label || "")

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    Text {
                        text: "Batch " + modelData.number
                        color: Theme.inkSoft
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                    }
                    Text {
                        objectName: "analysisBatchState_" + root.phase.phase + "_" + modelData.number
                        Layout.fillWidth: true
                        visible: modelData.status !== "completed"
                        text: batchRow.batchState
                        color: root.blocked || Number(modelData.failed || 0) > 0
                            ? Theme.warning : Theme.inkMuted
                        font.pixelSize: 11
                        elide: Text.ElideRight
                    }
                    Text {
                        objectName: "analysisBatchProgress_" + root.phase.phase + "_" + modelData.number
                        text: batchRow.processed + " / " + batchRow.total
                        color: Theme.inkMuted
                        font.pixelSize: 11
                    }
                }
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 3
                    radius: 3
                    color: Theme.line
                    Rectangle {
                        width: parent.width * (batchRow.total > 0
                            ? batchRow.processed / batchRow.total : 0)
                        height: parent.height
                        radius: parent.radius
                        color: Number(modelData.failed || 0) > 0 ? Theme.warning : Theme.accentBright
                    }
                }
            }
        }
    }
}
