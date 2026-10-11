pragma Singleton
import QtQuick

QtObject {
    readonly property bool darkMode: true
    readonly property string fontFamily: ".AppleSystemUIFont"
    readonly property color canvas: "#09080F"
    readonly property color canvasLift: "#100E19"
    readonly property color chrome: "#0E0D14"
    readonly property color rail: "#07070C"
    readonly property color surface: "#16141F"
    readonly property color surfaceMuted: "#1B1925"
    readonly property color surfaceRaised: "#211E2D"
    readonly property color surfaceHover: "#272333"
    readonly property color surfacePressed: "#302A40"

    readonly property color ink: "#FCFAFF"
    readonly property color inkSoft: "#E0DCE9"
    readonly property color inkMuted: "#B0A9BF"
    readonly property color inkFaint: "#858095"

    readonly property color line: "#332F40"
    readonly property color lineStrong: "#5A526C"
    readonly property color accent: "#8E7CFF"
    readonly property color accentBright: "#B9AEFF"
    readonly property color accentDark: "#C8C0FF"
    readonly property color accentDeep: "#6555E8"
    readonly property color accentSoft: "#2B2448"
    readonly property color accentLine: "#5D518B"
    readonly property color accentGlow: "#765FFF"
    readonly property color success: "#55D3AA"
    readonly property color successSoft: "#10251F"
    readonly property color warning: "#EDB767"
    readonly property color warningSoft: "#261D12"
    readonly property color danger: "#FF7F96"
    readonly property color dangerSoft: "#40232D"
    readonly property color blue: "#82AAFF"
    readonly property color neutralSoft: "#24212E"
    readonly property color highlight: "#16FFFFFF"
    readonly property color shadow: "#28000000"
    readonly property color shadowSoft: "#36000000"
    readonly property color scrim: "#B8000000"

    readonly property color successLine: "#285B4D"
    readonly property color warningLine: "#684D2B"
    readonly property color dangerLine: "#673541"
    readonly property int controlHeight: 40
    readonly property int pageGap: 24
    readonly property int pageContentMaxWidth: 1320
    readonly property int sectionGap: 16
    readonly property int cardPadding: 22
    readonly property int scrollGutter: 14
    readonly property int pageTitleSize: 26
    readonly property int sectionTitleSize: 17
    readonly property int bodySize: 13
    readonly property int captionSize: 11

    // Keep strokes on whole or half logical pixels so they land cleanly on
    // both standard and Retina displays instead of being blended between
    // device pixels.
    readonly property real lineWidth: 1.0
    readonly property real lineWidthStrong: 1.5

    readonly property int radiusSmall: 10
    readonly property int radiusMedium: 11
    readonly property int radiusLarge: 18
    readonly property int radiusXLarge: 20

    readonly property int motionQuick: 90
    readonly property int motionFast: 130
    readonly property int motionMedium: 180
    readonly property int motionRelaxed: 240

    function icon(name) {
        return Qt.resolvedUrl("../../resources/icons/" + name + ".svg")
    }
}
