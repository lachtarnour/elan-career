import QtQuick
import QtQuick.Layouts

ColumnLayout {
    id: root
    property var offer: ({})
    property int fontPixelSize: 11
    property int labelFontPixelSize: 11
    property string fitObjectName: "offerFitEvaluation"
    property bool fitDetailsExpanded: false
    readonly property int offerId: Number(offer.id || 0)
    readonly property bool inlineEvaluation: width >= Math.round(360 * labelFontPixelSize / 11)
    readonly property var fitAxes: [
        {axis: "missions", label: "Missions", accent: Theme.accentBright},
        {axis: "essentials", label: "Compétences essentielles", accent: Theme.blue},
        {axis: "responsibilities", label: "Responsabilités", accent: Theme.success}
    ]
    readonly property bool hasFindings: fitAxes.some(function(axis) { return root.axisFindings(axis.axis).length > 0 })
    spacing: 14

    onOfferIdChanged: fitDetailsExpanded = false

    function axisFindings(axis) {
        var axes = offer.fit_axes || []
        for (var i = 0; i < axes.length; ++i)
            if (axes[i].axis === axis) return axes[i].findings || []
        return []
    }

    function seniorityLabel(value) {
        var labels = {junior: "Junior", mid: "Confirmé", senior: "Senior",
            lead: "Lead", manager: "Manager", mixed: "Plusieurs niveaux"}
        return labels[value] || (value && value !== "unknown" ? value : "Non précisée")
    }

    RowLayout {
        objectName: "offerAnalysisHeading"
        Layout.fillWidth: true
        spacing: 8
        Rectangle { Layout.preferredWidth: 3; Layout.preferredHeight: 13; radius: 2; color: Theme.accent }
        FormLabel { text: "ANALYSE DU POSTE"; fontPixelSize: root.labelFontPixelSize }
        Item { Layout.fillWidth: true }
    }

    GridLayout {
        objectName: "offerAnalysisOverview"
        Layout.fillWidth: true
        columns: root.inlineEvaluation ? 2 : 1
        columnSpacing: 16
        rowSpacing: 12
        ColumnLayout {
            objectName: "offerAnalysisMetadata"
            Layout.row: root.inlineEvaluation ? 0 : 1
            Layout.column: root.inlineEvaluation ? 1 : 0
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignTop
            spacing: 10
            Repeater {
                model: [
                    {key: "role", label: "Type de poste", value: root.offer.role_type || "Non précisé"},
                    {key: "seniority", label: "Séniorité", value: root.seniorityLabel(root.offer.seniority || "")},
                    {key: "domain", label: "Domaine", value: root.offer.domain || "Non précisé"}
                ]
                delegate: ColumnLayout {
                    required property var modelData
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignTop
                    spacing: 4
                    Text {
                        Layout.fillWidth: true
                        text: modelData.label
                        textFormat: Text.PlainText
                        color: Theme.inkMuted
                        font.pixelSize: root.labelFontPixelSize
                    }
                    Text {
                        objectName: "offerAnalysis_" + modelData.key
                        Layout.fillWidth: true
                        text: modelData.value
                        textFormat: Text.PlainText
                        color: Theme.ink
                        font.pixelSize: root.fontPixelSize
                        font.weight: Font.DemiBold
                        wrapMode: Text.WordWrap
                    }
                }
            }
        }
        FitEvaluation {
            objectName: root.fitObjectName
            Layout.row: 0
            Layout.column: 0
            Layout.fillWidth: !root.inlineEvaluation
            Layout.preferredWidth: Math.round(200 * root.labelFontPixelSize / 11)
            Layout.maximumWidth: root.width
            Layout.alignment: Qt.AlignTop | Qt.AlignLeft
            offer: root.offer
            fontPixelSize: Math.max(10, Math.round(root.labelFontPixelSize * 0.85))
            detailsAvailable: root.hasFindings
            detailsExpanded: root.fitDetailsExpanded
            onToggleDetails: root.fitDetailsExpanded = !root.fitDetailsExpanded
        }
    }

    Rectangle {
        objectName: "offerFitDetails"
        visible: root.fitDetailsExpanded && root.hasFindings
        Layout.fillWidth: true
        implicitHeight: findingsContent.implicitHeight + 24
        radius: 12
        color: Theme.canvasLift
        border.color: Theme.line
        ColumnLayout {
            id: findingsContent
            anchors.fill: parent
            anchors.margins: 12
            spacing: 12
            Repeater {
                model: root.fitAxes
                delegate: ColumnLayout {
                    required property var modelData
                    readonly property var findings: root.axisFindings(modelData.axis)
                    visible: findings.length > 0
                    Layout.fillWidth: true
                    spacing: 5
                    Text {
                        text: modelData.label
                        color: modelData.accent
                        font.pixelSize: root.labelFontPixelSize
                        font.weight: Font.DemiBold
                    }
                    Text {
                        objectName: "offerFitFindings_" + modelData.axis
                        Layout.fillWidth: true
                        text: "• " + findings.join("\n• ")
                        textFormat: Text.PlainText
                        color: Theme.inkSoft
                        font.pixelSize: root.labelFontPixelSize
                        wrapMode: Text.WordWrap
                        lineHeight: 1.25
                    }
                }
            }
        }
    }

    Repeater {
        model: [
            {key: "tasks", label: "Missions", entries: root.offer.main_tasks || [], bullets: true},
            {key: "requiredSkills", label: "Compétences requises", entries: root.offer.required_skills || [], bullets: false},
            {key: "niceToHave", label: "Compétences souhaitées", entries: root.offer.nice_to_have || [], bullets: false}
        ]
        delegate: ColumnLayout {
            required property var modelData
            Layout.fillWidth: true
            spacing: 5
            Text {
                Layout.fillWidth: true
                text: modelData.label
                textFormat: Text.PlainText
                color: Theme.inkSoft
                font.pixelSize: root.labelFontPixelSize + 1
                font.weight: Font.DemiBold
            }
            Text {
                objectName: "offerAnalysis_" + modelData.key
                Layout.fillWidth: true
                text: modelData.entries.length
                    ? (modelData.bullets ? "• " + modelData.entries.join("\n• ") : modelData.entries.join(" · "))
                    : "Non renseignées dans l’analyse."
                textFormat: Text.PlainText
                color: modelData.entries.length ? Theme.ink : Theme.inkMuted
                font.pixelSize: root.fontPixelSize
                wrapMode: Text.WordWrap
                lineHeight: 1.25
            }
        }
    }
}
