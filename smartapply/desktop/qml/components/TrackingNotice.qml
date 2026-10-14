import QtQuick
import QtQuick.Layouts

Surface {
    id: root
    property string heading: ""
    property string message: ""
    padding: 14
    verticalPadding: 12
    radiusValue: 12
    surfaceColor: Theme.warningSoft
    strokeColor: Theme.warningLine
    RowLayout {
        Layout.fillWidth: true
        spacing: 10
        SvgIcon {
            Layout.alignment: Qt.AlignTop
            Layout.topMargin: 2
            Layout.preferredWidth: 17
            Layout.preferredHeight: 17
            source: Theme.icon("alert-circle")
            color: Theme.warning
        }
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 4
            Text {
                visible: root.heading.length > 0
                Layout.fillWidth: true
                text: root.heading
                textFormat: Text.PlainText
                color: Theme.warning
                font.pixelSize: 12
                font.weight: Font.DemiBold
                wrapMode: Text.WordWrap
            }
            Text {
                Layout.fillWidth: true
                text: root.message
                textFormat: Text.PlainText
                color: Theme.inkMuted
                font.pixelSize: 12
                wrapMode: Text.WordWrap
            }
        }
    }
}
