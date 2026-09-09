import QtQuick
import QtQuick.Shapes

Item {
    id: root
    property url source
    property color color: Theme.inkSoft
    property real iconOpacity: 1
    property string iconName: {
        var value = source.toString()
        var file = value.substring(value.lastIndexOf("/") + 1)
        return file.replace(".svg", "")
    }
    property string pathData: {
        switch (iconName) {
        case "home": return "M3.5 10.6 L12 3.6 L20.5 10.6 M5.5 9.2 L5.5 19.5 L18.5 19.5 L18.5 9.2 M9.5 19.5 L9.5 13.5 L14.5 13.5 L14.5 19.5"
        case "search": return "M17.6 10.8 A6.8 6.8 0 1 1 4 10.8 A6.8 6.8 0 0 1 17.6 10.8 M16 16 L20 20"
        case "briefcase": return "M5.5 6.5 L18.5 6.5 Q21 6.5 21 9 L21 17 Q21 19.5 18.5 19.5 L5.5 19.5 Q3 19.5 3 17 L3 9 Q3 6.5 5.5 6.5 M8.5 6.5 L8.5 4.8 Q8.5 3 10.3 3 L13.7 3 Q15.5 3 15.5 4.8 L15.5 6.5 M3 11.5 Q12 15.5 21 11.5 M10 12.8 L14 12.8"
        case "files": return "M7 3.5 L14 3.5 L18 7.5 L18 19 Q18 21 16 21 L7 21 Q5 21 5 19 L5 5.5 Q5 3.5 7 3.5 M14 3.5 L14 7.5 L18 7.5 M8.5 12 L14.5 12 M8.5 16 L14.5 16"
        case "copy": return "M10 8 L18 8 Q20 8 20 10 L20 18 Q20 20 18 20 L10 20 Q8 20 8 18 L8 10 Q8 8 10 8 Z M16 8 L16 4 Q16 2 14 2 L4 2 Q2 2 2 4 L2 14 Q2 16 4 16 L8 16"
        case "plus": return "M12 5 L12 19 M5 12 L19 12"
        case "minus": return "M5 12 L19 12"
        case "x": return "M6 6 L18 18 M18 6 L6 18"
        case "user": return "M15.5 8 A3.5 3.5 0 1 1 8.5 8 A3.5 3.5 0 0 1 15.5 8 M5 20 Q5.7 14 12 14 Q18.3 14 19 20"
        case "settings": return "M15 12 A3 3 0 1 1 9 12 A3 3 0 0 1 15 12 M19.4 15 Q19 16 19.8 17 L17 19.8 Q16 19 15 19.4 Q14 19.8 14 21 L14 21.2 L10 21.2 L10 21 Q10 19.8 9 19.4 Q8 19 7 19.8 L4.2 17 Q5 16 4.6 15 Q4.2 14 3 14 L2.8 14 L2.8 10 L3 10 Q4.2 10 4.6 9 Q5 8 4.2 7 L7 4.2 Q8 5 9 4.6 Q10 4.2 10 3 L10 2.8 L14 2.8 L14 3 Q14 4.2 15 4.6 Q16 5 17 4.2 L19.8 7 Q19 8 19.4 9 Q19.8 10 21 10 L21.2 10 L21.2 14 L21 14 Q19.8 14 19.4 15"
        case "command": return "M9 7 L9 5.5 A2.5 2.5 0 1 0 6.5 8 L18 8 M15 17 L15 18.5 A2.5 2.5 0 1 0 17.5 16 L6 16 M7 9 L7 15 M17 9 L17 15"
        case "chevron-left": return "M15 5 L8 12 L15 19"
        case "chevron-right": return "M9 5 L16 12 L9 19"
        case "arrow-up-right": return "M6 18 L18 6 M8 6 L18 6 L18 16"
        case "check": return "M5 12.5 L9.2 16.7 L19 7"
        case "alert-circle": return "M21 12 A9 9 0 1 1 3 12 A9 9 0 0 1 21 12 M12 7.5 L12 13 M12 16.5 L12.01 16.5"
        case "sparkle": return "M12 2.8 Q12.6 10.4 20.2 11 Q12.6 11.6 12 19.2 Q11.4 11.6 3.8 11 Q11.4 10.4 12 2.8 M19 3 L19 6 M17.5 4.5 L20.5 4.5"
        case "folder": return "M5 5 L10 5 L12 7 L19 7 Q21 7 21 9 L21 17.5 Q21 19.5 19 19.5 L5 19.5 Q3 19.5 3 17.5 L3 7 Q3 5 5 5"
        case "database": return "M20 5.5 A8 3 0 1 1 4 5.5 A8 3 0 0 1 20 5.5 M4 5.5 L4 11.5 Q4 14.5 12 14.5 Q20 14.5 20 11.5 L20 5.5 M4 11.5 L4 17.5 Q4 20.5 12 20.5 Q20 20.5 20 17.5 L20 11.5"
        case "document": return "M6 3 L14 3 L18 7 L18 21 L6 21 Q4 21 4 19 L4 5 Q4 3 6 3 M14 3 L14 8 L18 8 M8 12 L14 12 M8 16 L15 16"
        case "map-pin": return "M19 10 Q19 15.3 12 21 Q5 15.3 5 10 A7 7 0 1 1 19 10 M14.4 10 A2.4 2.4 0 1 1 9.6 10 A2.4 2.4 0 0 1 14.4 10"
        case "refresh": return "M20 6 L20 11 L15 11 M4 18 L4 13 L9 13 M6.1 8.2 Q12 1.8 18.6 7 L20 11 M4 13 L5.4 17 Q12 22.2 18 15.8"
        case "sliders": return "M4 7 L8 7 M12 7 L20 7 M4 17 L12 17 M16 17 L20 17 M12 7 A2 2 0 1 1 8 7 A2 2 0 0 1 12 7 M16 17 A2 2 0 1 1 12 17 A2 2 0 0 1 16 17"
        default: return "M5 12 L19 12"
        }
    }

    implicitWidth: 20
    implicitHeight: 20
    readonly property bool softwareRenderer: GraphicsInfo.api === GraphicsInfo.Software
    opacity: iconOpacity

    Behavior on color { ColorAnimation { duration: Theme.motionFast; easing.type: Easing.OutCubic } }
    Behavior on iconOpacity { NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutCubic } }

    // SVG images use the same paths and colours without depending on the
    // Shape renderer's software clipping and teardown behaviour.
    Image {
        anchors.fill: parent
        visible: root.softwareRenderer
        source: root.softwareRenderer ? "data:image/svg+xml;utf8," + encodeURIComponent(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="' + root.pathData
            + '" fill="none" stroke="' + root.color + '" stroke-width="2.35" stroke-linecap="round" stroke-linejoin="round"/></svg>') : ""
        sourceSize.width: Math.ceil(width * Screen.devicePixelRatio)
        sourceSize.height: Math.ceil(height * Screen.devicePixelRatio)
        smooth: true
    }

    Shape {
        id: iconShape
        visible: !root.softwareRenderer
        width: 24
        height: 24
        anchors.centerIn: parent
        scale: Math.min(root.width / 24, root.height / 24)
        // CurveRenderer keeps small curved strokes clean after the icon is
        // fitted into compact controls such as the navigation rail.
        preferredRendererType: Shape.CurveRenderer
        ShapePath {
            strokeColor: root.color
            strokeWidth: 2.35
            fillColor: "transparent"
            capStyle: ShapePath.RoundCap
            joinStyle: ShapePath.RoundJoin
            PathSvg { path: root.pathData }
        }
    }
}
