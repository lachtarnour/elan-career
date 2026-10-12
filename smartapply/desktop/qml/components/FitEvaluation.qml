import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root
    property var offer: ({})
    property int fontPixelSize: 10
    property bool detailsAvailable: false
    property bool detailsExpanded: false
    signal toggleDetails()
    implicitHeight: evaluationContent.implicitHeight + 20
    radius: 12
    color: Theme.canvasLift
    border.color: Theme.line

    function axisLevel(axis) {
        if (!offer.fit_current) return -1
        var axes = offer.fit_axes || []
        for (var i = 0; i < axes.length; ++i) {
            if (axes[i].axis !== axis) continue
            var level = axes[i].level
            return typeof level === "number" && level >= 0 && level <= 4
                && Math.floor(level) === level ? level : -1
        }
        return -1
    }

    Rectangle {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.leftMargin: root.radius
        anchors.rightMargin: root.radius
        height: 1
        color: Theme.highlight
    }

    ColumnLayout {
        id: evaluationContent
        anchors.fill: parent
        anchors.margins: 10
        spacing: 6

        Text {
            objectName: "offerGlobalFitScore"
            visible: !(root.offer.fit_axes || []).length
            Layout.fillWidth: true
            text: "Score IA : " + (typeof root.offer.llm_score === "number"
                ? Math.round(root.offer.llm_score * 100) + " %" : "Indisponible")
            color: Theme.ink
            font.pixelSize: root.fontPixelSize
            font.weight: Font.DemiBold
        }

        Repeater {
            model: (root.offer.fit_axes || []).length ? [
                {axis: "missions", label: "Missions", accent: Theme.accentBright},
                {axis: "essentials", label: "Compétences essentielles", accent: Theme.blue},
                {axis: "responsibilities", label: "Responsabilités", accent: Theme.success}
            ] : []
            delegate: ColumnLayout {
                id: axisRow
                required property var modelData
                readonly property int level: root.axisLevel(modelData.axis)
                Layout.fillWidth: true
                spacing: 4

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 6
                    Rectangle {
                        Layout.preferredWidth: 2
                        Layout.preferredHeight: 10
                        radius: 2
                        color: axisRow.modelData.accent
                        opacity: axisRow.level < 0 ? 0.45 : 1
                    }
                    Text {
                        Layout.fillWidth: true
                        text: axisRow.modelData.label
                        textFormat: Text.PlainText
                        color: Theme.inkSoft
                        font.pixelSize: root.fontPixelSize
                        font.weight: Font.Medium
                        elide: Text.ElideRight
                    }
                    Text {
                        objectName: "offerFitAxis_" + axisRow.modelData.axis
                        text: axisRow.level < 0 ? "— / 4" : axisRow.level + " / 4"
                        textFormat: Text.PlainText
                        color: axisRow.level < 0 ? Theme.inkFaint : Theme.ink
                        font.pixelSize: root.fontPixelSize
                        font.weight: Font.DemiBold
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    Layout.leftMargin: 8
                    spacing: 3
                    Repeater {
                        model: 4
                        delegate: Rectangle {
                            objectName: "offerFitSegment_" + axisRow.modelData.axis + "_" + index
                            Layout.fillWidth: true
                            Layout.preferredHeight: 3
                            radius: 2
                            color: axisRow.level >= 0 && index < axisRow.level
                                ? axisRow.modelData.accent : Theme.neutralSoft
                        }
                    }
                }
            }
        }
        AppButton {
            objectName: "offerFitDetailsAction"
            visible: root.detailsAvailable
            Layout.alignment: Qt.AlignRight
            text: root.detailsExpanded ? "Réduire" : "Détails"
            iconSource: Theme.icon("chevron-right")
            iconSize: 10
            refined: true
            quiet: true
            fontPixelSize: root.fontPixelSize
            implicitHeight: 22
            implicitWidth: contentItem.implicitWidth + 12
            leftPadding: 6
            rightPadding: 6
            Accessible.name: root.detailsExpanded ? "Masquer les détails de l’adéquation" : "Afficher les détails de l’adéquation"
            onClicked: root.toggleDetails()
        }
    }
}
