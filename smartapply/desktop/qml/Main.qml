import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "components"
import "pages"

ApplicationWindow {
    id: window
    width: 1480
    height: 920
    // The offers page contains a dense table plus a detail panel. Below this
    // width the fixed columns cannot remain readable without overlapping.
    minimumWidth: 1320
    minimumHeight: 820
    // macOS unified titlebar: keep the native traffic-light controls while
    // allowing the QML chrome/content to continue into the titlebar area.
    flags: Qt.Window | Qt.ExpandedClientAreaHint | Qt.NoTitleBarBackgroundHint
    // ApplicationWindow normally reserves the native safe area at the top.
    // The background already covers it, so do not add a second empty strip;
    // the rail and pages position their own content deliberately.
    topPadding: 0
    visible: true
    // Keep the native window controls without a visible brand label.
    title: ""
    color: Theme.canvas
    font.family: Theme.fontFamily

    property string currentRoute: "dashboard"
    property string pendingRoute: "dashboard"
    property string jobsFilterStatus: "scraped"
    property real pageOffset: 0
    property bool dashboardLoaded: true
    property bool searchLoaded: false
    property bool duplicatesLoaded: false
    property bool jobsLoaded: false
    property bool companiesLoaded: false
    property bool manualLoaded: false
    property bool profileLoaded: false
    property bool settingsLoaded: false
    // Keep the rail compact on a 14-inch display so the working area remains
    // wide enough for the list/detail layouts.
    readonly property bool expandedNavigation: width >= 1560
    property var actions: [
        {title: "Accueil", icon: Theme.icon("home"), route: "dashboard"},
        {title: "Recherche", icon: Theme.icon("search"), route: "search"},
        {title: "Doublons", icon: Theme.icon("files"), route: "duplicates"},
        {title: "Offres", icon: Theme.icon("briefcase"), route: "jobs"},
        {title: "Entreprises", icon: Theme.icon("database"), route: "companies"},
        {title: "Ajouter une offre", icon: Theme.icon("plus"), route: "manual"},
        {title: "Profil", icon: Theme.icon("user"), route: "profile"},
        {title: "Réglages", icon: Theme.icon("settings"), route: "settings"}
    ]

    function routeIndex(route) {
        var routes = ["dashboard", "search", "duplicates", "jobs", "companies", "manual", "profile", "settings"]
        var value = routes.indexOf(routePage(route))
        return value < 0 ? 0 : value
    }

    function routePage(route) {
        return String(route || "dashboard").split("?")[0]
    }

    function routeStatus(route) {
        var match = String(route || "").match(/[?&]status=([^&]+)/)
        if (!match) return ""
        var status = decodeURIComponent(match[1])
        // ``filtered`` is an internal persistence state, not a UI filter.
        return status === "filtered" ? "scraped" : status
    }

    function refreshRoute(route) {
        var page = routePage(route)
        if (page === "dashboard") AppBridge.refreshDashboard()
        if (page === "companies") AppBridge.loadCompanies()
        if (page === "duplicates") AppBridge.loadJobs("", "duplicate_review", "duplicate_confidence", false)
        if (page === "jobs") {
            var status = routeStatus(route) || jobsFilterStatus
            AppBridge.loadJobs("", status, "score", false)
        }
        if (page === "profile") AppBridge.refreshProfile()
        if (page === "settings") AppBridge.refreshDiagnostics()
    }

    function ensureRouteLoaded(route) {
        var page = routePage(route)
        if (page === "dashboard") dashboardLoaded = true
        else if (page === "search") searchLoaded = true
        else if (page === "duplicates") duplicatesLoaded = true
        else if (page === "jobs") jobsLoaded = true
        else if (page === "companies") companiesLoaded = true
        else if (page === "manual") manualLoaded = true
        else if (page === "profile") profileLoaded = true
        else if (page === "settings") settingsLoaded = true
    }

    function navigate(route) {
        var page = routePage(route)
        var status = routeStatus(route)
        if (page === "jobs" && status) jobsFilterStatus = status
        if (pageSwitch.running && pendingRoute === route) return
        if (page === currentRoute) {
            refreshRoute(route)
            return
        }
        pendingRoute = route
        pageSwitch.restart()
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0

        Rectangle {
            id: navigationRail
            Layout.preferredWidth: window.expandedNavigation ? 220 : 84
            Layout.fillHeight: true
            clip: true
            gradient: Gradient {
                orientation: Gradient.Vertical
                GradientStop { position: 0; color: Theme.chrome }
                GradientStop { position: 0.46; color: "#0B0A11" }
                GradientStop { position: 1; color: Theme.rail }
            }

            Rectangle {
                width: 280; height: 280; radius: 140
                x: navigationRail.width - 170; y: -190
                color: "#066B52FF"
            }
            Rectangle {
                width: 220; height: 220; radius: 110
                x: -150; y: navigationRail.height - 120
                color: "#03735CFF"
            }

            Rectangle {
                anchors.top: parent.top
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                width: 1
                color: Theme.line
            }

            Behavior on Layout.preferredWidth { NumberAnimation { duration: 190; easing.type: Easing.OutCubic } }

            ColumnLayout {
                anchors.fill: parent
                anchors.leftMargin: 12
                anchors.rightMargin: 12
                // Reserve only the native traffic-light safe area after the
                // client area is expanded into the macOS titlebar.
                anchors.topMargin: Math.max(32, window.SafeArea.margins.top + 8)
                anchors.bottomMargin: 16
                spacing: 5

                RailItem { Layout.fillWidth: window.expandedNavigation; Layout.alignment: Qt.AlignHCenter; expanded: window.expandedNavigation; text: "Accueil"; compactText: "Accueil"; iconSource: Theme.icon("home"); selected: window.currentRoute === "dashboard"; onClicked: window.navigate("dashboard") }
                RailItem { Layout.fillWidth: window.expandedNavigation; Layout.alignment: Qt.AlignHCenter; expanded: window.expandedNavigation; text: "Recherche"; compactText: "Recherche"; iconSource: Theme.icon("search"); selected: window.currentRoute === "search"; onClicked: window.navigate("search") }
                RailItem { Layout.fillWidth: window.expandedNavigation; Layout.alignment: Qt.AlignHCenter; expanded: window.expandedNavigation; text: "Doublons"; compactText: "Doublons"; iconSource: Theme.icon("files"); selected: window.currentRoute === "duplicates"; onClicked: window.navigate("duplicates") }
                RailItem { Layout.fillWidth: window.expandedNavigation; Layout.alignment: Qt.AlignHCenter; expanded: window.expandedNavigation; text: "Offres"; compactText: "Offres"; iconSource: Theme.icon("briefcase"); selected: window.currentRoute === "jobs"; onClicked: window.navigate("jobs") }
                RailItem { Layout.fillWidth: window.expandedNavigation; Layout.alignment: Qt.AlignHCenter; expanded: window.expandedNavigation; text: "Entreprises"; compactText: "Sociétés"; iconSource: Theme.icon("database"); selected: window.currentRoute === "companies"; onClicked: window.navigate("companies") }
                RailItem { Layout.fillWidth: window.expandedNavigation; Layout.alignment: Qt.AlignHCenter; expanded: window.expandedNavigation; text: "Ajouter une offre"; compactText: "Ajouter"; iconSource: Theme.icon("plus"); selected: window.currentRoute === "manual"; onClicked: window.navigate("manual") }

                Item { Layout.fillHeight: true }

                Item {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 22
                    Rectangle {
                        id: statusLight
                        anchors.centerIn: parent
                        width: 9; height: 9; radius: 5
                        color: AppBridge.busy ? Theme.warning : Theme.success
                        Rectangle { anchors.centerIn: parent; width: 18; height: 18; radius: 9; color: AppBridge.busy ? "#22EDB767" : "#2455D3AA"; z: -1 }
                    }
                }

                RailItem { Layout.fillWidth: window.expandedNavigation; Layout.alignment: Qt.AlignHCenter; expanded: window.expandedNavigation; text: "Profil"; compactText: "Profil"; iconSource: Theme.icon("user"); selected: window.currentRoute === "profile"; onClicked: window.navigate("profile") }
                RailItem { Layout.fillWidth: window.expandedNavigation; Layout.alignment: Qt.AlignHCenter; expanded: window.expandedNavigation; text: "Réglages"; compactText: "Réglages"; iconSource: Theme.icon("settings"); selected: window.currentRoute === "settings"; onClicked: window.navigate("settings") }
                Rectangle {
                    Layout.fillWidth: window.expandedNavigation
                    Layout.alignment: Qt.AlignHCenter
                    Layout.topMargin: 8
                    Layout.preferredWidth: window.expandedNavigation ? 196 : 38
                    Layout.preferredHeight: window.expandedNavigation ? 48 : 38
                    radius: window.expandedNavigation ? 13 : 12
                    gradient: Gradient {
                        orientation: Gradient.Horizontal
                        GradientStop { position: 0; color: avatarHover.hovered ? "#302B40" : "#25222F" }
                        GradientStop { position: 1; color: avatarHover.hovered ? "#24212D" : "#1B1A21" }
                    }
                    border.color: avatarHover.hovered ? Theme.accentLine : Theme.lineStrong
                    Rectangle {
                        width: 32; height: 32; radius: 10
                        x: window.expandedNavigation ? 9 : 3
                        anchors.verticalCenter: parent.verticalCenter
                        color: Theme.accentSoft
                        border.color: Theme.accentLine
                        Text { anchors.centerIn: parent; text: AppBridge.profile.initials || "É"; color: Theme.accentBright; font.pixelSize: 10; font.weight: Font.Bold }
                    }
                    Column {
                        visible: window.expandedNavigation
                        x: 52
                        anchors.verticalCenter: parent.verticalCenter
                        width: parent.width - 62
                        spacing: 0
                        Text { width: parent.width; text: AppBridge.profile.name || "Votre profil"; color: "#E7E5EC"; font.pixelSize: 11; font.weight: Font.DemiBold; elide: Text.ElideRight }
                    }
                    HoverHandler { id: avatarHover }
                    TapHandler { onTapped: window.navigate("profile") }
                }
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            Item {
                id: contentHost
                Layout.fillWidth: true
                Layout.fillHeight: true
                opacity: 1
                scale: 1
                AmbientBackdrop { anchors.fill: parent }
                StackLayout {
                    id: pages
                    objectName: "pages"
                    clip: true
                    anchors.fill: parent
                    anchors.leftMargin: 28
                    anchors.rightMargin: 28
                    // Keep the page content below the native macOS titlebar
                    // and add a small breathing space shared by every page.
                    // The window background still extends edge-to-edge.
                    anchors.topMargin: Math.max(32, window.SafeArea.margins.top + 8)
                    anchors.bottomMargin: 28
                    transform: Translate { x: window.pageOffset }
                    currentIndex: 0
                    Loader {
                        active: window.dashboardLoaded
                        asynchronous: true
                        sourceComponent: Component { DashboardPage { onNavigateRequested: function(route) { window.navigate(route) } } }
                    }
                    Loader {
                        active: window.searchLoaded
                        asynchronous: true
                        sourceComponent: Component { SearchPage { onNavigateRequested: function(route) { window.navigate(route) } } }
                    }
                    Loader {
                        active: window.duplicatesLoaded
                        asynchronous: true
                        sourceComponent: Component { DuplicatesPage { onNavigateRequested: function(route) { window.navigate(route) } } }
                    }
                    Loader {
                        active: window.jobsLoaded
                        asynchronous: true
                        sourceComponent: Component {
                            JobsPage {
                                requestedFilter: window.jobsFilterStatus
                                onFilterChanged: function(status) { window.jobsFilterStatus = status }
                                onNavigateRequested: function(route) { window.navigate(route) }
                            }
                        }
                    }
                    Loader {
                        active: window.companiesLoaded
                        asynchronous: true
                        sourceComponent: Component { CompaniesPage { } }
                    }
                    Loader {
                        active: window.manualLoaded
                        asynchronous: true
                        sourceComponent: Component { ManualPage { onNavigateRequested: function(route) { window.navigate(route) } } }
                    }
                    Loader {
                        active: window.profileLoaded
                        asynchronous: true
                        sourceComponent: Component { ProfilePage { onNavigateRequested: function(route) { window.navigate(route) } } }
                    }
                    Loader {
                        active: window.settingsLoaded
                        asynchronous: true
                        sourceComponent: Component { SettingsPage { onNavigateRequested: function(route) { window.navigate(route) } } }
                    }
                }

                // Notifications float above the page so their appearance
                // never changes the geometry of the current workspace.
                Rectangle {
                    id: toast
                    objectName: "toast"
                    z: 120
                    property string kind: "neutral"
                    property string titleText: ""
                    property string messageText: ""
                    anchors.top: parent.top
                    anchors.right: parent.right
                    anchors.topMargin: Math.max(32, window.SafeArea.margins.top + 8)
                    anchors.rightMargin: 28
                    width: Math.min(390, Math.max(260, parent.width - 56))
                    height: implicitHeight
                    implicitHeight: toastRow.implicitHeight + 28
                    radius: 14
                    color: Theme.surfaceRaised
                    border.color: kind === "success" ? "#286B57" : kind === "danger" ? "#7E3443" : kind === "warning" ? "#785827" : "#393548"
                    opacity: 0
                    visible: opacity > 0
                    transform: Translate { id: toastTranslate; x: 20; Behavior on x { NumberAnimation { duration: 200; easing.type: Easing.OutCubic } } }
                    RowLayout {
                        id: toastRow
                        anchors.fill: parent
                        anchors.margins: 14
                        anchors.rightMargin: 34
                        spacing: 12
                        Rectangle {
                            Layout.preferredWidth: 38; Layout.preferredHeight: 38; radius: 12
                            color: toast.kind === "success" ? "#203F36" : toast.kind === "danger" ? "#45262E" : toast.kind === "warning" ? "#443622" : "#302B4B"
                            Text { anchors.centerIn: parent; text: toast.kind === "success" ? "✓" : toast.kind === "danger" ? "×" : toast.kind === "warning" ? "!" : "i"; color: toast.kind === "success" ? "#62D6B0" : toast.kind === "danger" ? "#FF8CA0" : toast.kind === "warning" ? "#F4BB62" : "#A99FFF"; font.pixelSize: 17; font.weight: Font.Bold }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true; spacing: 2
                            Text { Layout.fillWidth: true; text: toast.titleText; color: "#F5F3F8"; font.pixelSize: 13; font.weight: Font.DemiBold; wrapMode: Text.WordWrap }
                            Text { visible: toast.messageText.length > 0; Layout.fillWidth: true; text: toast.messageText; color: "#A9A6B2"; font.pixelSize: 11; wrapMode: Text.WordWrap }
                        }
                    }
                    AppButton {
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 6
                        implicitWidth: 24
                        implicitHeight: 24
                        quiet: true
                        refined: true
                        iconSize: 12
                        iconSource: Theme.icon("x")
                        objectName: "toastDismiss"
                        Accessible.name: "Fermer la notification"
                        onClicked: { toast.opacity = 0; toastTimer.stop() }
                    }
                    HoverHandler { id: toastHover }
                    Behavior on opacity { NumberAnimation { duration: 180 } }
                    Timer { id: toastTimer; interval: 4600; onTriggered: { if (toastHover.hovered) restart(); else { toast.opacity = 0; toastTranslate.x = 20 } } }
                    function show(title, message, type) { titleText = title; messageText = message; kind = type; opacity = 1; toastTranslate.x = 0; toastTimer.restart() }
                }
            }
        }
        }

    // ExpandedClientAreaHint puts this strip inside the client area, so the
    // native titlebar no longer receives the drag automatically. Qt delegates
    // the move to macOS, preserving native snapping and window animations.
    Item {
        id: windowDragRegion
        x: navigationRail.width
        y: 0
        width: Math.max(0, window.width - navigationRail.width)
        height: Math.max(12, window.SafeArea.margins.top - 14)
        z: 50

        DragHandler {
            target: null
            acceptedButtons: Qt.LeftButton
            onActiveChanged: if (active) window.startSystemMove()
        }
    }

    SequentialAnimation {
        id: pageSwitch
        ParallelAnimation {
            NumberAnimation { target: pages; property: "opacity"; to: 0; duration: 55; easing.type: Easing.InQuad }
            NumberAnimation { target: window; property: "pageOffset"; to: -4; duration: 60; easing.type: Easing.InQuad }
        }
        ParallelAnimation {
            ScriptAction {
                    script: {
                    var page = window.routePage(window.pendingRoute)
                    window.ensureRouteLoaded(page)
                    window.currentRoute = page
                    pages.currentIndex = window.routeIndex(page)
                    window.refreshRoute(window.pendingRoute)
                    window.pageOffset = 8
                }
            }
            NumberAnimation { target: pages; property: "opacity"; from: 0; to: 1; duration: 175; easing.type: Easing.OutCubic }
            NumberAnimation { target: window; property: "pageOffset"; from: 8; to: 0; duration: 195; easing.type: Easing.OutCubic }
        }
    }

    Shortcut { sequences: ["Meta+K", "Ctrl+K"]; onActivated: commandPalette.open() }

    Popup {
        id: commandPalette
        objectName: "commandPalette"
        width: Math.min(640, window.width - 80)
        height: Math.min(520, window.height - 100)
        x: Math.round((window.width - width) / 2)
        y: 78
        z: 100
        modal: true
        focus: true
        padding: 0
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        onOpened: { commandSearch.text = ""; commandSearch.forceActiveFocus() }
        background: Item {
            Rectangle { anchors.fill: parent; anchors.topMargin: 12; anchors.bottomMargin: -12; radius: 26; color: "#85000000" }
            Rectangle {
                anchors.fill: parent
                radius: 24
                gradient: Gradient {
                    orientation: Gradient.Vertical
                    GradientStop { position: 0; color: Theme.surfaceRaised }
                    GradientStop { position: 1; color: Theme.surface }
                }
                border.color: Theme.accentLine
                border.width: Theme.lineWidth
                Rectangle { anchors.left: parent.left; anchors.right: parent.right; anchors.leftMargin: 24; anchors.rightMargin: 24; anchors.top: parent.top; height: 1; color: "#28FFFFFF" }
            }
        }
        Overlay.modal: Rectangle { color: Theme.scrim }
        contentItem: ColumnLayout {
            spacing: 0
            RowLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: 68
                Layout.leftMargin: 20; Layout.rightMargin: 20
                spacing: 12
                SvgIcon { source: Theme.icon("search"); color: Theme.accent; Layout.preferredWidth: 21; Layout.preferredHeight: 21 }
                TextField {
                    id: commandSearch
                    Layout.fillWidth: true
                    placeholderText: "Rechercher une page…"
                    color: Theme.ink
                    placeholderTextColor: Theme.inkMuted
                    font.pixelSize: 16
                    background: null
                    onTextChanged: commandList.currentIndex = commandList.count > 0 ? 0 : -1
                    Keys.onDownPressed: commandList.incrementCurrentIndex()
                    Keys.onUpPressed: commandList.decrementCurrentIndex()
                    Keys.onReturnPressed: {
                        if (commandList.currentIndex >= 0 && commandList.currentIndex < commandList.count) {
                            var action = commandList.model[commandList.currentIndex]
                            window.navigate(action.route)
                            commandPalette.close()
                        }
                    }
                }
            }
            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
            ListView {
                id: commandList
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.leftMargin: 10; Layout.rightMargin: 10; Layout.topMargin: 10; Layout.bottomMargin: 12
                clip: true
                spacing: 3
                model: window.actions.filter(function(item) { var q = commandSearch.text.toLowerCase(); return q.length === 0 || item.title.toLowerCase().indexOf(q) >= 0 })
                currentIndex: count > 0 ? 0 : -1
                Text {
                    anchors.centerIn: parent
                    visible: commandList.count === 0
                    text: "Aucune page trouvée"
                    color: Theme.inkMuted
                    font.pixelSize: Theme.bodySize
                }
                delegate: Rectangle {
                    required property var modelData
                    required property int index
                    width: commandList.width
                    height: 48
                    radius: 12
                    gradient: Gradient {
                        orientation: Gradient.Horizontal
                        GradientStop { position: 0; color: commandHover.hovered || commandList.currentIndex === index ? "#3B315F" : "transparent" }
                        GradientStop { position: 1; color: commandHover.hovered || commandList.currentIndex === index ? Theme.accentSoft : "transparent" }
                    }
                    border.color: commandHover.hovered || commandList.currentIndex === index ? Theme.accentLine : "transparent"
                    RowLayout {
                        anchors.fill: parent; anchors.leftMargin: 12; anchors.rightMargin: 12; spacing: 12
                        Rectangle { Layout.preferredWidth: 34; Layout.preferredHeight: 34; radius: 11; color: commandHover.hovered ? Theme.accentLine : Theme.neutralSoft; SvgIcon { anchors.centerIn: parent; source: modelData.icon; color: Theme.accentDark; width: 17; height: 17 } }
                        Text { Layout.fillWidth: true; text: modelData.title; color: Theme.ink; font.pixelSize: 13; font.weight: Font.DemiBold }
                    }
                    HoverHandler { id: commandHover }
                    TapHandler { onTapped: { window.navigate(modelData.route); commandPalette.close() } }
                }
            }
        }
    }

    Rectangle {
        id: busyPill
        z: 50
        visible: opacity > 0
        opacity: AppBridge.busy ? 1 : 0
        anchors.top: parent.top
        anchors.topMargin: 20
        anchors.horizontalCenter: parent.horizontalCenter
        width: Math.min(430, Math.max(220, busyRow.implicitWidth + 34))
        height: 44
        radius: 15
        color: Theme.surfaceRaised
        border.color: Theme.accentLine
        RowLayout {
            id: busyRow
            anchors.fill: parent
            anchors.margins: 12
            spacing: 10
            Item {
                Layout.preferredWidth: 18; Layout.preferredHeight: 18
                Rectangle { width: 6; height: 6; radius: 3; color: Theme.accentBright; anchors.top: parent.top; anchors.horizontalCenter: parent.horizontalCenter }
                RotationAnimation on rotation { running: AppBridge.busy; from: 0; to: 360; duration: 850; loops: Animation.Infinite }
            }
            Text { Layout.fillWidth: true; text: AppBridge.busyLabel || "Traitement en cours…"; color: Theme.inkSoft; font.pixelSize: 12; font.weight: Font.DemiBold; elide: Text.ElideRight }
        }
        Behavior on opacity { NumberAnimation { duration: 180 } }
    }

    Connections {
        target: AppBridge
        function onToastRequested(title, message, kind) { toast.show(title, message, kind) }
        function onNavigationRequested(route, selectedId) { window.navigate(route) }
    }

}
