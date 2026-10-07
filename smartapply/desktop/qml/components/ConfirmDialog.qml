import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: root
    objectName: "confirmDialog"
    property string heading: "Confirmer l’action"
    property string message: ""
    property bool confirmEnabled: true
    property string dismissText: "Annuler"
    property string confirmText: "Confirmer"
    property string kind: "primary"
    property url iconSource: Theme.icon(kind === "danger" ? "alert-circle" : "check")

    width: Math.min(460, parent ? parent.width - 48 : 460)
    implicitHeight: dialogContent.implicitHeight + padding * 2
    modal: true
    focus: true
    padding: 24
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    background: Rectangle {
        radius: Theme.radiusLarge
        color: Theme.surfaceRaised
        border.color: root.kind === "danger" ? Theme.dangerLine : Theme.lineStrong
    }
    Overlay.modal: Rectangle { color: Theme.scrim }
    contentItem: ColumnLayout {
        id: dialogContent
        Accessible.name: root.heading
        Accessible.role: Accessible.Dialog
        spacing: 20
        RowLayout {
            Layout.fillWidth: true
            spacing: 14
            Rectangle {
                Layout.preferredWidth: 40
                Layout.preferredHeight: 40
                radius: 12
                color: root.kind === "danger" ? Theme.dangerSoft : Theme.accentSoft
                SvgIcon { anchors.centerIn: parent; source: root.iconSource; color: root.kind === "danger" ? Theme.danger : Theme.accentBright; width: 20; height: 20 }
            }
            Text {
                Layout.fillWidth: true
                text: root.heading
                color: Theme.ink
                font.pixelSize: 19
                font.weight: Font.DemiBold
                wrapMode: Text.WordWrap
            }
        }
        Text {
            Layout.fillWidth: true
            visible: text.length > 0
            text: root.message
            color: Theme.inkSoft
            font.pixelSize: 13
            lineHeight: 1.35
            wrapMode: Text.WordWrap
        }
        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            Item { Layout.fillWidth: true }
            AppButton { text: root.dismissText; onClicked: root.reject() }
            AppButton { text: root.confirmText; kind: root.kind; enabled: root.confirmEnabled; onClicked: root.accept() }
        }
    }
    enter: Transition {
        ParallelAnimation {
            NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 150; easing.type: Easing.OutCubic }
            NumberAnimation { property: "scale"; from: 0.98; to: 1; duration: 150; easing.type: Easing.OutCubic }
        }
    }
    exit: Transition { NumberAnimation { property: "opacity"; from: 1; to: 0; duration: 100 } }
}
