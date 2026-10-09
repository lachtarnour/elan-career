import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root
    property var offer: ({})
    property int fontPixelSize: 12
    property int smallFontPixelSize: 11
    property bool warningsExpanded: false
    readonly property var warnings: offer.fit_warnings || []
    implicitHeight: evaluationContent.implicitHeight + 24
    radius: 16
    color: Theme.surfaceMuted
    border.color: Theme.line

    ColumnLayout {
        id: evaluationContent
        anchors.fill: parent
        anchors.margins: 12
        spacing: 10
        RowLayout {
            Layout.fillWidth: true
            Text {
                Layout.fillWidth: true
                text: "Indice technique"
                color: Theme.ink
                font.pixelSize: root.fontPixelSize
                font.weight: Font.Bold
            }
            Text {
                objectName: "offerTechnicalScore"
                text: root.offer.llm_score === null || root.offer.llm_score === undefined
                    ? "—" : Math.round(Number(root.offer.llm_score) * 100) + " / 100"
                color: Theme.ink
                font.pixelSize: root.fontPixelSize + 3
                font.weight: Font.DemiBold
            }
        }
        Text {
            objectName: "offerFitStatus"
            Layout.fillWidth: true
            text: root.offer.fit_status_label || ""
            textFormat: Text.PlainText
            color: root.offer.fit_current ? Theme.inkMuted : Theme.warning
            font.pixelSize: root.smallFontPixelSize
            wrapMode: Text.WordWrap
        }
        Repeater {
            model: root.offer.fit_current ? (root.offer.fit_axes || []) : []
            delegate: RowLayout {
                required property var modelData
                Layout.fillWidth: true
                Text {
                    Layout.fillWidth: true
                    text: modelData.label
                    wrapMode: Text.WordWrap
                    color: Theme.inkSoft
                    font.pixelSize: root.fontPixelSize
                }
                Text {
                    objectName: "offerFitAxis_" + modelData.axis
                    text: modelData.decision_label
                        + (modelData.level === null || modelData.level === undefined
                            ? "" : " · " + modelData.level + " / 4")
                    color: Theme.inkSoft
                    font.pixelSize: root.fontPixelSize
                }
            }
        }
        Text {
            objectName: "offerFitMethod"
            Layout.fillWidth: true
            text: root.offer.fit_current
                ? "Indice de classement issu de trois niveaux d’adéquation, pondérés à 50 / 30 / 20."
                : "Cette évaluation utilise une méthode historique non comparable. Une réévaluation explicite est nécessaire pour utiliser l’indice actuel."
            color: Theme.inkMuted
            font.pixelSize: root.smallFontPixelSize
            wrapMode: Text.WordWrap
        }
        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
        Text {
            Layout.fillWidth: true
            text: root.offer.candidacy_label || "Admissibilité"
            color: Theme.ink
            font.pixelSize: root.fontPixelSize
            font.weight: Font.Bold
            wrapMode: Text.WordWrap
        }
        Repeater {
            model: root.offer.fit_admissibility || []
            delegate: ColumnLayout {
                required property var modelData
                Layout.fillWidth: true
                spacing: 4
                Text {
                    objectName: "offerAdmissibility_" + modelData.dimension
                    Layout.fillWidth: true
                    text: modelData.label + " · " + modelData.status_label
                    textFormat: Text.PlainText
                    color: modelData.status === "incompatible" ? Theme.warning : Theme.inkSoft
                    font.pixelSize: root.fontPixelSize
                    wrapMode: Text.WordWrap
                }
                Text {
                    visible: (modelData.findings || []).length > 0
                    Layout.fillWidth: true
                    text: (modelData.findings || []).join("\n")
                    textFormat: Text.PlainText
                    color: Theme.inkMuted
                    font.pixelSize: root.smallFontPixelSize
                    wrapMode: Text.WordWrap
                }
            }
        }
        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
        Text {
            Layout.fillWidth: true
            text: "Fiabilité de l’évaluation"
            color: Theme.ink
            font.pixelSize: root.fontPixelSize
            font.weight: Font.Bold
        }
        Repeater {
            model: root.offer.fit_reliability || []
            delegate: Text {
                required property var modelData
                Layout.fillWidth: true
                text: modelData.label + " · " + modelData.text
                textFormat: Text.PlainText
                color: Theme.inkMuted
                font.pixelSize: root.smallFontPixelSize
                wrapMode: Text.WordWrap
            }
        }
        AppButton {
            objectName: "offerFitWarningsAction"
            visible: root.warnings.length > 0
            text: root.warnings.length + (root.warnings.length === 1
                ? " avertissement informatif" : " avertissements informatifs")
            quiet: true
            refined: true
            fontPixelSize: root.smallFontPixelSize
            onClicked: root.warningsExpanded = !root.warningsExpanded
        }
        Text {
            objectName: "offerFitWarnings"
            visible: root.warningsExpanded && root.warnings.length > 0
            Layout.fillWidth: true
            text: "• " + root.warnings.join("\n• ")
            textFormat: Text.PlainText
            color: Theme.inkMuted
            font.pixelSize: root.smallFontPixelSize
            wrapMode: Text.Wrap
        }
    }
}
