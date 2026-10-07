import QtQuick

Item {
    id: root
    property string description: ""
    property int fontPixelSize: 16
    implicitHeight: Math.max(180, body.implicitHeight + 6)

    function displayText(value) {
        var content = String(value || "").replace(/\r\n?/g, "\n").trim()
        if (!content) return "Description indisponible"
        var lines = content.split("\n")
        if (lines.length > 1) {
            var first = lines[0].trim().toLowerCase()
            if (["description", "description du poste", "job description"].indexOf(first) >= 0)
                lines.shift()
        }
        return lines.join("\n").replace(/\n[ \t]*\n[ \t]*\n+/g, "\n\n").trim()
    }

    Text {
        id: body
        objectName: "offerDescriptionText"
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.topMargin: 2
        text: root.displayText(root.description)
        color: Theme.inkSoft
        font.pixelSize: root.fontPixelSize
        wrapMode: Text.WordWrap
        lineHeight: 1.32
        textFormat: Text.PlainText
    }
}
