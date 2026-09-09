import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    objectName: "jobsPage"
    signal navigateRequested(string route)
    signal filterChanged(string status)

    property int selectedId: AppBridge.currentJob.id || 0
    // A generated application is displayed as part of its offer. This keeps
    // the offer list and the application tracking state in one workspace.
    property var applicationData: AppBridge.currentJob.application || ({})
    property string sortKey: "score"
    property bool sortAscending: false
    property string filterStatus: "scraped"
    property string requestedFilter: "scraped"
    property var selectedJobs: ({})
    readonly property int tableScrollGutter: Theme.scrollGutter
    // Header and rows share one measured content width and the same grid.
    readonly property real tableContentWidth: Math.max(0, tableSurface.width - 24 - tableScrollGutter)
    readonly property real tableScale: Math.min(1, Math.max(0, (tableContentWidth - 540) / 440))
    readonly property int columnGap: Math.round(6 + 4 * tableScale)
    readonly property int selectWidth: 24
    readonly property int experienceWidth: Math.round(52 + 12 * tableScale)
    readonly property int scoreWidth: Math.round(42 + 6 * tableScale)
    readonly property int llmScoreWidth: scoreWidth
    readonly property real textColumnsWidth: Math.max(0, tableContentWidth - selectWidth - experienceWidth - scoreWidth - llmScoreWidth - columnGap * 6)
    readonly property int companyWidth: Math.round(textColumnsWidth * 0.26)
    readonly property int locationWidth: Math.round(textColumnsWidth * 0.30)
    readonly property int tableRowHeight: Math.round(68 + 4 * tableScale)
    readonly property int tableTextSize: Math.round(12 + 2 * tableScale)
    // Local-only zoom: deliberately not persisted between application runs.
    property real detailTextZoom: 1.15
    // Scale the detail continuously as the window grows, including normal
    // manual resizing before the full-screen breakpoint is reached.
    readonly property real detailFontScale: Math.min(1.25, Math.max(1.0, root.width / 1400)) * detailTextZoom
    readonly property int detailBodyFontSize: Math.round(14 * detailFontScale)
    readonly property int detailMetaFontSize: Math.round(11 * detailFontScale)
    readonly property int detailSmallFontSize: Math.round(10 * detailFontScale)
    readonly property int detailButtonFontSize: Math.round(11 * detailFontScale)
    readonly property int detailActionButtonWidth: root.width < 1300 ? 92 : 104
    readonly property int detailActionButtonHeight: 30
    readonly property color successCardSurface: Theme.successSoft
    readonly property color successCardBorder: Theme.successLine
    readonly property color successIconSurface: "#193B32"
    readonly property color warningCardSurface: Theme.warningSoft
    readonly property color warningCardBorder: Theme.warningLine
    readonly property color warningIconSurface: "#3A2A18"
    readonly property bool hasMatchInsights: (AppBridge.currentJob.match_reasons || []).length > 0
    readonly property bool hasRiskInsights: (AppBridge.currentJob.risks || []).length > 0
    readonly property bool hasDuplicateInsight: AppBridge.currentJob.duplicate_review_status === "pending"
    readonly property bool hasArchiveInsight: AppBridge.currentJob.status === "archived"
        && (AppBridge.currentJob.archive_reasons || []).length > 0
    readonly property bool hasInsightCards: hasMatchInsights || hasRiskInsights || hasDuplicateInsight || hasArchiveInsight

    function companyX() { return selectWidth + columnGap }
    function titleX() { return companyX() + companyWidth + columnGap }
    function scoreX(contentWidth) { return contentWidth - scoreWidth }
    function llmScoreX(contentWidth) { return scoreX(contentWidth) - columnGap - llmScoreWidth }
    function locationX(contentWidth) { return llmScoreX(contentWidth) - columnGap - locationWidth }
    function experienceX(contentWidth) { return locationX(contentWidth) - columnGap - experienceWidth }
    function titleWidthFor(contentWidth) { return Math.max(0, experienceX(contentWidth) - columnGap - titleX()) }
    function adjustDetailTextZoom(delta) {
        var next = Math.round((detailTextZoom + delta) * 100) / 100
        detailTextZoom = Math.max(0.85, Math.min(1.55, next))
    }
    function resetDetailTextZoom() { detailTextZoom = 1.15 }

    function sourceLabel(source) {
        var raw = String(source || "").trim()
        var key = raw.toLowerCase().replace(/[\s_-]+/g, "")
        if (key.indexOf("welcometothejungle") >= 0 || key === "wttj") return "WTTJ"
        if (key.indexOf("francetravail") >= 0) return "FRANCE TRAVAIL"
        if (key.indexOf("linkedin") >= 0) return "LINKEDIN"
        if (key.indexOf("serpapi") >= 0) return "WEB"
        return raw.length > 0 ? raw.replace(/[_-]+/g, " ").toUpperCase() : "SOURCE INCONNUE"
    }

    function experienceLabel(experience) {
        var raw = String(experience || "").trim()
        var key = raw.toLowerCase().replace(/[\s_-]+/g, "")
        if (key === "junior" || key === "entrylevel") return "Junior"
        if (key === "mid" || key === "midlevel" || key === "intermediate") return "Confirmé"
        if (key === "senior" || key === "seniorlevel") return "Senior"
        return raw.length > 0 ? raw : "—"
    }

    function descriptionForDisplay(description) {
        var content = String(description || "").replace(/\r\n?/g, "\n").trim()
        if (content.length === 0) return "Description indisponible"
        var lines = content.split("\n")
        if (lines.length > 1) {
            var firstLine = lines[0].trim().toLowerCase()
            if (firstLine === "description" || firstLine === "description du poste" || firstLine === "job description")
                lines.shift()
        }
        return lines.join("\n").replace(/\n[ \t]*\n[ \t]*\n+/g, "\n\n").trim()
    }

    property var sortedJobs: {
        var rowsDependency = AppBridge.jobs
        var keyDependency = sortKey
        var directionDependency = sortAscending
        return (AppBridge.jobs || []).slice()
    }
    property var selectedRows: {
        var selectionDependency = selectedJobs
        var rows = AppBridge.jobs || []
        var result = []
        for (var i = 0; i < rows.length; ++i) {
            if (selectionDependency[String(rows[i].id)]) result.push(rows[i])
        }
        return result
    }
    property var selectedIds: selectedRows.map(function(row) { return Number(row.id) })
    property var topLabelIds: selectedRows.filter(function(row) { return row.status !== "archived" && !row.application_id && !row.shortlisted }).map(function(row) { return Number(row.id) })
    property var generationIds: selectedRows.filter(function(row) { return Boolean(row.can_generate) }).map(function(row) { return Number(row.id) })
    property var rescueIds: selectedRows.filter(function(row) { return row.status === "archived" }).map(function(row) { return Number(row.id) })
    property int selectedCount: selectedIds.length
    property bool allVisibleSelected: {
        if (sortedJobs.length === 0) return false
        for (var i = 0; i < sortedJobs.length; ++i) {
            if (!selectedJobs[String(sortedJobs[i].id)]) return false
        }
        return true
    }

    function isSelected(jobId) { return Boolean(selectedJobs[String(jobId)]) }
    function setSelected(jobId, checked) {
        var next = {}
        for (var key in selectedJobs) next[key] = selectedJobs[key]
        if (checked) next[String(jobId)] = true
        else delete next[String(jobId)]
        selectedJobs = next
    }
    function clearSelection() { selectedJobs = ({}) }
    function selectedIndex() {
        var rows = sortedJobs || []
        for (var i = 0; i < rows.length; ++i) {
            if (Number(rows[i].id) === Number(selectedId)) return i
        }
        return -1
    }
    readonly property int selectedPosition: root.selectedIndex()
    readonly property bool canGoPrevious: root.selectedPosition > 0
    readonly property bool canGoNext: root.selectedPosition >= 0 && root.selectedPosition < root.sortedJobs.length - 1
    function moveSelection(delta) {
        var rows = sortedJobs || []
        if (rows.length === 0) return
        var index = root.selectedIndex()
        if (index < 0) index = delta > 0 ? -1 : rows.length
        var next = Math.max(0, Math.min(rows.length - 1, index + delta))
        AppBridge.selectJob(Number(rows[next].id))
    }
    function selectAllVisible() {
        var next = {}
        for (var key in selectedJobs) next[key] = selectedJobs[key]
        for (var i = 0; i < sortedJobs.length; ++i) next[String(sortedJobs[i].id)] = true
        selectedJobs = next
    }
    function sortBy(key) {
        if (sortKey === key) sortAscending = !sortAscending
        else {
            sortKey = key
            sortAscending = key === "score" ? false : true
        }
        AppBridge.loadJobs(searchField.text, statusFilter.currentValue || "", sortKey, sortAscending)
    }

    function applyRequestedFilter() {
        if (!statusFilter) return
        var index = statusFilter.indexOfValue(requestedFilter)
        if (index < 0) return
        filterStatus = requestedFilter
        statusFilter.currentIndex = index
    }

    onRequestedFilterChanged: applyRequestedFilter()
    Component.onCompleted: applyRequestedFilter()
    Timer {
        id: searchTimer
        interval: 260
        onTriggered: {
            root.clearSelection()
            AppBridge.loadJobs(searchField.text, statusFilter.currentValue || "", root.sortKey, root.sortAscending)
        }
    }

    Timer {
        id: applicationNotesSaveTimer
        interval: 500
        onTriggered: {
            if (root.applicationData.id && applicationNotes.activeFocus)
                AppBridge.updateApplication(root.applicationData.id, "", applicationNotes.text, false)
        }
    }

    Connections {
        target: AppBridge
        function onJobSelectionClearRequested() { root.clearSelection() }
        function onCurrentJobChanged() {
            detailViewport.contentY = 0
            if (applicationNotes)
                applicationNotes.text = root.applicationData.notes || ""
            var index = root.selectedIndex()
            if (index >= 0) jobsTable.positionViewAtIndex(index, ListView.Contain)
        }
    }

    RowLayout {
        anchors.fill: parent
        spacing: 12

        ColumnLayout {
            Layout.fillWidth: true
            Layout.preferredWidth: 1
            Layout.minimumWidth: 560
            Layout.fillHeight: true
            spacing: 10

            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: 38
                RowLayout {
                    id: toolbarRow
                    objectName: "offerToolbar"
                    anchors.fill: parent
                    spacing: 8
                    AppSelect {
                        id: statusFilter
                        objectName: "offerStatusFilter"
                        Layout.minimumWidth: 112
                        Layout.preferredWidth: 124
                        implicitHeight: 34
                        leftPadding: 12
                        rightPadding: 34
                        fontPixelSize: 12
                        indicatorSize: 14
                        indicatorMargin: 12
                        emphasized: true
                        textRole: "label"
                        valueRole: "value"
                        model: AppBridge.jobStatuses
                        currentIndex: Math.max(0, indexOfValue(root.filterStatus))
                        onActivated: {
                            root.filterStatus = currentValue
                            root.filterChanged(currentValue)
                            root.clearSelection()
                            AppBridge.loadJobs(searchField.text, currentValue, root.sortKey, root.sortAscending)
                        }
                    }
                    Item {
                        id: searchControl
                        objectName: "expandableSearch"
                        property bool expanded: false
                        Layout.minimumWidth: 34
                        // Reserve the actions' actual width before expanding the field.
                        // The toolbar must never push into the adjacent offer.
                        Layout.preferredWidth: expanded ? Math.min(260, Math.max(34,
                            toolbarRow.width - statusFilter.Layout.preferredWidth - shortlistControls.Layout.preferredWidth
                            - (generationAction.visible ? generationAction.implicitWidth + toolbarRow.spacing : 0)
                            - toolbarRow.spacing * 3)) : 34
                        Layout.maximumWidth: Layout.preferredWidth
                        Layout.preferredHeight: 34
                        clip: true

                        function open() {
                            expanded = true
                            Qt.callLater(function() { searchField.forceActiveFocus() })
                        }

                        function close(clearQuery) {
                            if (clearQuery && searchField.text.length > 0)
                                searchField.clear()
                            searchField.focus = false
                            expanded = false
                        }

                        Behavior on Layout.preferredWidth {
                            NumberAnimation { duration: 180; easing.type: Easing.OutCubic }
                        }

                        AppButton {
                            objectName: "offerSearchToggle"
                            anchors.fill: parent
                            visible: opacity > 0
                            opacity: searchControl.expanded ? 0 : 1
                            enabled: !searchControl.expanded
                            text: ""
                            iconSource: Theme.icon("search")
                            refined: true
                            iconSize: 14
                            implicitWidth: 34
                            implicitHeight: 34
                            Accessible.name: "Rechercher dans les offres"
                            onClicked: searchControl.open()
                            Behavior on opacity { NumberAnimation { duration: 90 } }
                        }

                        AppField {
                            id: searchField
                            objectName: "offerSearchField"
                            anchors.fill: parent
                            visible: opacity > 0
                            opacity: searchControl.expanded ? 1 : 0
                            enabled: searchControl.expanded
                            implicitHeight: 34
                            font.pixelSize: 12
                            iconSource: Theme.icon("search")
                            placeholderText: "Entreprise, poste, ville…"
                            onTextChanged: searchTimer.restart()
                            onActiveFocusChanged: {
                                if (!activeFocus && text.length === 0)
                                    searchCollapseTimer.restart()
                            }
                            Keys.onEscapePressed: function(event) {
                                searchControl.close(true)
                                event.accepted = true
                            }
                            Behavior on opacity { NumberAnimation { duration: 120 } }
                        }

                        Timer {
                            id: searchCollapseTimer
                            interval: 120
                            onTriggered: {
                                if (!searchField.activeFocus && searchField.text.length === 0)
                                    searchControl.close(false)
                            }
                        }
                    }
                    Item { Layout.fillWidth: true }
                    AppButton {
                        id: generationAction
                        visible: (AppBridge.shortlist.ready_to_generate || 0) > 0
                        text: "Générer (" + AppBridge.shortlist.ready_to_generate + ")"
                        iconSource: Theme.icon("sparkle")
                        kind: "primary"
                        refined: true
                        iconSize: 14
                        fontPixelSize: 11
                        implicitHeight: 34
                        enabled: !AppBridge.busy
                        onClicked: AppBridge.generateShortlistedApplications()
                    }
                    RowLayout {
                        id: shortlistControls
                        objectName: "offerShortlistControls"
                        Layout.minimumWidth: 192
                        Layout.preferredWidth: 192
                        Layout.maximumWidth: 192
                        spacing: 6
                        AppSpinBox {
                            id: topK
                            from: 1
                            to: 100
                            value: AppBridge.shortlistTopK
                            Layout.minimumWidth: 90
                            Layout.preferredWidth: 90
                            Layout.maximumWidth: 90
                            implicitHeight: 34
                            leftPadding: 34
                            rightPadding: 34
                            fontPixelSize: 12
                            indicatorSize: 13
                            indicatorWidth: 28
                            indicatorMargin: 4
                            // This is an independent preference; background
                            // searches must not disable its +/- controls.
                            onUserValueModified: function(nextValue) { AppBridge.setShortlistTopK(Number(nextValue)) }
                        }
                        AppButton {
                            Layout.minimumWidth: 96
                            Layout.preferredWidth: 96
                            Layout.maximumWidth: 96
                            text: "Shortlist"
                            iconSource: Theme.icon("sparkle")
                            kind: "primary"
                            refined: true
                            iconSize: 14
                            fontPixelSize: 11
                            implicitHeight: 34
                            enabled: !AppBridge.busy
                            onClicked: {
                                topK.commitInput()
                                AppBridge.updateShortlist(topK.value)
                            }
                        }
                    }
                }
            }

            Rectangle {
                visible: root.selectedCount > 0
                Layout.fillWidth: true
                Layout.preferredHeight: visible ? 48 : 0
                radius: 13
                gradient: Gradient {
                    orientation: Gradient.Horizontal
                    GradientStop { position: 0; color: "#252039" }
                    GradientStop { position: 1; color: Theme.surfaceMuted }
                }
                border.color: Theme.accentLine
                Rectangle { anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter; width: 3; height: 24; radius: 2; color: Theme.accentBright }
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 12
                    anchors.rightMargin: 8
                    spacing: 8
                    Rectangle {
                        Layout.preferredWidth: 28; Layout.preferredHeight: 28; radius: 9
                        color: Theme.accentSoft; border.color: Theme.accentLine
                        Text { anchors.centerIn: parent; text: root.selectedCount; color: Theme.accentDark; font.pixelSize: 11; font.weight: Font.Bold }
                    }
                    ColumnLayout {
                        spacing: 1
                        Text { text: root.selectedCount === 1 ? "1 offre sélectionnée" : root.selectedCount + " offres sélectionnées"; color: Theme.ink; font.pixelSize: 12; font.weight: Font.DemiBold }
                    }
                    Item { Layout.fillWidth: true }
                    AppButton {
                        text: "Effacer"
                        iconSource: Theme.icon("x")
                        refined: true
                        quiet: true
                        iconSize: 13
                        fontPixelSize: 11
                        implicitHeight: 30
                        onClicked: root.clearSelection()
                    }
                    AppButton {
                        visible: root.topLabelIds.length > 0
                        text: "Ajouter (" + root.topLabelIds.length + ")"
                        iconSource: Theme.icon("plus")
                        refined: true
                        iconSize: 13
                        fontPixelSize: 11
                        implicitHeight: 30
                        enabled: root.topLabelIds.length > 0 && !AppBridge.busy
                        onClicked: AppBridge.labelJobsAsTop(root.topLabelIds)
                    }
                    AppButton {
                        visible: root.rescueIds.length > 0
                        text: "Désarchiver (" + root.rescueIds.length + ")"
                        iconSource: Theme.icon("refresh")
                        refined: true
                        iconSize: 13
                        fontPixelSize: 11
                        implicitHeight: 30
                        enabled: root.rescueIds.length > 0 && !AppBridge.busy
                        onClicked: AppBridge.rescueJobs(root.rescueIds)
                    }
                    AppButton {
                        visible: root.generationIds.length > 0
                        text: "Créer (" + root.generationIds.length + ")"
                        iconSource: Theme.icon("sparkle")
                        kind: "primary"
                        refined: true
                        iconSize: 13
                        fontPixelSize: 11
                        implicitHeight: 30
                        enabled: root.generationIds.length > 0 && !AppBridge.busy
                        onClicked: AppBridge.generateApplications(root.generationIds)
                    }
                }
            }

            Surface {
                id: tableSurface
                objectName: "offerTable"
                Layout.fillWidth: true
                Layout.fillHeight: true
                padding: 0
                elevated: false
                radiusValue: 16
                surfaceColor: "#121119"
                surfaceEndColor: surfaceColor
                strokeColor: "#2C2836"
                Item {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 0

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 42
                            color: "#191720"
                            radius: tableSurface.radiusValue
                            Rectangle { anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom; height: 18; color: parent.color }
                            Item {
                                anchors.fill: parent
                                anchors.leftMargin: 12
                                anchors.rightMargin: 12 + root.tableScrollGutter
                                SelectionBox {
                                    x: 0
                                    width: root.selectWidth
                                    anchors.verticalCenter: parent.verticalCenter
                                    checked: root.allVisibleSelected
                                    partial: root.selectedCount > 0 && !root.allVisibleSelected
                                    onToggled: function(checked) { if (checked) root.selectAllVisible(); else root.clearSelection() }
                                }
                                SortHeader { objectName: "offerCompanyHeader"; x: root.companyX(); width: root.companyWidth; anchors.verticalCenter: parent.verticalCenter; title: "Entreprise"; sortKey: "company"; active: root.sortKey === sortKey; ascending: root.sortAscending; onSortRequested: function(key) { root.sortBy(key) } }
                                SortHeader { objectName: "offerTitleHeader"; x: root.titleX(); width: root.titleWidthFor(parent.width); anchors.verticalCenter: parent.verticalCenter; title: "Poste"; sortKey: "title"; active: root.sortKey === sortKey; ascending: root.sortAscending; onSortRequested: function(key) { root.sortBy(key) } }
                                SortHeader { x: root.experienceX(parent.width); width: root.experienceWidth; anchors.verticalCenter: parent.verticalCenter; title: "Exp."; sortKey: "experience"; active: root.sortKey === sortKey; ascending: root.sortAscending; onSortRequested: function(key) { root.sortBy(key) } }
                                SortHeader { x: root.locationX(parent.width); width: root.locationWidth; anchors.verticalCenter: parent.verticalCenter; title: "Lieu"; sortKey: "location"; active: root.sortKey === sortKey; ascending: root.sortAscending; onSortRequested: function(key) { root.sortBy(key) } }
                                SortHeader { x: root.llmScoreX(parent.width); width: root.llmScoreWidth; anchors.verticalCenter: parent.verticalCenter; title: "IA"; sortKey: "llm_score"; active: root.sortKey === sortKey; ascending: root.sortAscending; centered: true; onSortRequested: function(key) { root.sortBy(key) } }
                                SortHeader { objectName: "offerMatchHeader"; x: root.scoreX(parent.width); width: root.scoreWidth; anchors.verticalCenter: parent.verticalCenter; title: "Match"; sortKey: "score"; active: root.sortKey === sortKey; ascending: root.sortAscending; centered: true; onSortRequested: function(key) { root.sortBy(key) } }
                            }
                        }
                        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: "#2C2836" }

                        Item {
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            EmptyState {
                                visible: AppBridge.jobs.length === 0
                                anchors.centerIn: parent
                                iconSource: Theme.icon("search")
                                title: "Aucun résultat"
                            }
                            ListView {
                                id: jobsTable
                                anchors.fill: parent
                                anchors.margins: 4
                                visible: AppBridge.jobs.length > 0
                                clip: true
                                spacing: 2
                                reuseItems: true
                                cacheBuffer: 420
                                boundsBehavior: Flickable.StopAtBounds
                                model: root.sortedJobs
                                ScrollBar.vertical: AppScrollBar { objectName: "offerTableScrollBar" }
                                delegate: Rectangle {
                                    id: jobRow
                                    required property var modelData
                                    readonly property bool current: root.selectedId === modelData.id
                                    width: Math.max(0, jobsTable.width - root.tableScrollGutter)
                                    height: root.tableRowHeight
                                    radius: 9
                                    color: current ? "#252034" : rowHover.hovered ? "#1D1B26" : root.isSelected(modelData.id) ? "#1C1927" : "transparent"
                                    border.color: current ? "#49405F" : "transparent"
                                    border.width: Theme.lineWidth
                                    Rectangle {
                                        visible: parent.current
                                        anchors.left: parent.left
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: 2
                                        height: 24
                                        radius: 2
                                        color: Theme.accentBright
                                    }
                                    Item {
                                        anchors.fill: parent
                                        anchors.leftMargin: 8
                                        anchors.rightMargin: 8
                                        SelectionBox {
                                            objectName: "offerSelection-" + modelData.id
                                            z: 2
                                            x: 0
                                            width: root.selectWidth
                                            anchors.verticalCenter: parent.verticalCenter
                                            checked: root.isSelected(modelData.id)
                                            onToggled: function(checked) { root.setSelected(modelData.id, checked) }
                                        }
                                        Column {
                                            objectName: "offerCompany-" + modelData.id
                                            x: root.companyX(); width: root.companyWidth; anchors.verticalCenter: parent.verticalCenter; spacing: 4
                                            Text { width: parent.width; text: modelData.company; color: Theme.inkSoft; font.pixelSize: root.tableTextSize - 1; font.weight: Font.Medium; wrapMode: Text.WordWrap; maximumLineCount: 2; elide: Text.ElideRight; lineHeight: 1.15 }
                                            Text { width: parent.width; text: root.sourceLabel(modelData.source); color: Theme.inkFaint; font.pixelSize: 9; font.letterSpacing: 0.2; elide: Text.ElideRight }
                                        }
                                        Text {
                                            objectName: "offerTitle-" + modelData.id
                                            x: root.titleX()
                                            width: root.titleWidthFor(parent.width)
                                            anchors.verticalCenter: parent.verticalCenter
                                            text: modelData.title
                                            color: Theme.ink
                                            font.pixelSize: root.tableTextSize
                                            font.weight: Font.Medium
                                            wrapMode: Text.WordWrap
                                            maximumLineCount: 2
                                            elide: Text.ElideRight
                                            lineHeight: 1.2
                                        }
                                        Text { objectName: "offerExperience-" + modelData.id; x: root.experienceX(parent.width); width: root.experienceWidth; anchors.verticalCenter: parent.verticalCenter; text: root.experienceLabel(modelData.experience); color: Theme.inkMuted; font.pixelSize: root.tableTextSize - 1; elide: Text.ElideRight }
                                        Text { objectName: "offerLocation-" + modelData.id; x: root.locationX(parent.width); width: root.locationWidth; anchors.verticalCenter: parent.verticalCenter; text: modelData.location || "—"; color: Theme.inkMuted; font.pixelSize: root.tableTextSize - 1; wrapMode: Text.WordWrap; maximumLineCount: 2; elide: Text.ElideRight; lineHeight: 1.2 }
                                        Text {
                                            objectName: "offerIa-" + modelData.id
                                            x: root.llmScoreX(parent.width)
                                            width: root.llmScoreWidth
                                            anchors.verticalCenter: parent.verticalCenter
                                            horizontalAlignment: Text.AlignHCenter
                                            text: modelData.llm_score_text || "—"
                                            color: modelData.llm_score === null || modelData.llm_score === undefined ? Theme.inkFaint : Theme.inkSoft
                                            font.pixelSize: root.tableTextSize - 1
                                            font.weight: Font.DemiBold
                                        }
                                        Text {
                                            objectName: "offerMatch-" + modelData.id
                                            x: root.scoreX(parent.width)
                                            width: root.scoreWidth
                                            anchors.verticalCenter: parent.verticalCenter
                                            horizontalAlignment: Text.AlignHCenter
                                            text: modelData.score_text || "—"
                                            color: modelData.score === null || modelData.score === undefined ? Theme.inkFaint : Theme.inkSoft
                                            font.pixelSize: root.tableTextSize - 1
                                            font.weight: Font.DemiBold
                                        }
                                    }
                                    HoverHandler { id: rowHover; cursorShape: Qt.PointingHandCursor }
                                    TapHandler { onTapped: AppBridge.selectJob(modelData.id) }
                                    Behavior on color { ColorAnimation { duration: Theme.motionFast } }
                                    Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }
                                }
                            }
                            Rectangle {
                                anchors.fill: parent
                                visible: AppBridge.jobsLoading
                                color: "#B60F0D16"
                                z: 5
                                BusyIndicator {
                                    anchors.centerIn: parent
                                    running: parent.visible
                                    width: 30
                                    height: 30
                                }
                                Text {
                                    anchors.top: parent.verticalCenter
                                    anchors.topMargin: 25
                                    anchors.horizontalCenter: parent.horizontalCenter
                                    text: "Chargement…"
                                    color: Theme.inkMuted
                                    font.pixelSize: 11
                                    font.weight: Font.DemiBold
                                }
                            }
                        }
                    }
                }
            }
        }

        // The offer detail is intentionally frameless: the content
        // should float on the application background instead of reading
        // as a second card beside the results table.
        Item {
                Layout.fillWidth: true
                Layout.preferredWidth: 1
                Layout.minimumWidth: 560
                Layout.fillHeight: true
                Item {
                    anchors.fill: parent
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    anchors.leftMargin: 18
                    anchors.rightMargin: 0
                    anchors.topMargin: 0
                    anchors.bottomMargin: 0
                    EmptyState {
                        visible: !AppBridge.currentJob.id
                        anchors.centerIn: parent
                        iconSource: Theme.icon("briefcase")
                        title: "Aucune offre sélectionnée"
                    }
                    ColumnLayout {
                        visible: Boolean(AppBridge.currentJob.id)
                        anchors.fill: parent
                        spacing: 12
                        Flickable {
                            id: detailViewport
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            contentWidth: width
                            contentHeight: detailContent.implicitHeight
                            boundsBehavior: Flickable.StopAtBounds
                            clip: true
                            ScrollBar.vertical: AppScrollBar { }

                            ColumnLayout {
                                id: detailContent
                                width: detailViewport.width - Theme.scrollGutter
                                spacing: 10
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 8
                                    Text {
                                        objectName: "offerDossierNumber"
                                        visible: Boolean(root.applicationData.id)
                                        text: visible ? "#" + String(root.applicationData.id) : ""
                                        Accessible.name: visible ? "Dossier " + String(root.applicationData.id) : ""
                                        color: Theme.accentDark
                                        font.pixelSize: Math.round(14 * root.detailFontScale)
                                        font.weight: Font.Bold
                                        Layout.alignment: Qt.AlignTop
                                    }
                                    Text {
                                        Layout.fillWidth: true
                                        text: AppBridge.currentJob.title || ""
                                        color: Theme.ink
                                        font.pixelSize: Math.round(20 * root.detailFontScale)
                                        font.weight: Font.DemiBold
                                        font.letterSpacing: -0.25
                                        lineHeight: 1.15
                                        wrapMode: Text.WordWrap
                                    }
                                    AppButton {
                                        id: detailTextZoomControl
                                        objectName: "detailZoomButton"
                                        Layout.preferredWidth: 30
                                        Layout.preferredHeight: 30
                                        Layout.alignment: Qt.AlignTop | Qt.AlignRight
                                        implicitWidth: 30
                                        text: "Aa"
                                        fontPixelSize: 10
                                        quiet: true
                                        opacity: hovered || visualFocus || detailZoomPopup.opened ? 1 : 0.45
                                        Accessible.name: "Ajuster la taille du texte de l’offre"
                                        onClicked: detailZoomPopup.opened ? detailZoomPopup.close() : detailZoomPopup.open()
                                        Behavior on opacity { NumberAnimation { duration: 130 } }
                                        Popup {
                                            id: detailZoomPopup
                                            objectName: "detailZoomPopup"
                                            x: detailTextZoomControl.width - width
                                            y: detailTextZoomControl.height + 4
                                            width: 118
                                            height: 38
                                            padding: 3
                                            focus: true
                                            closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
                                            background: Rectangle { radius: 10; color: Theme.surfaceRaised; border.color: Theme.lineStrong }
                                            contentItem: RowLayout {
                                                spacing: 2
                                                AppButton {
                                                    objectName: "detailZoomOut"
                                                    text: "A−"
                                                    Layout.fillWidth: true
                                                    implicitWidth: 32; implicitHeight: 30
                                                    fontPixelSize: 11; quiet: true
                                                    enabled: root.detailTextZoom > 0.85
                                                    Accessible.name: "Réduire le texte de l’offre"
                                                    onClicked: root.adjustDetailTextZoom(-0.1)
                                                }
                                                AppButton {
                                                    objectName: "detailZoomReset"
                                                    text: "Aa"
                                                    Layout.fillWidth: true
                                                    implicitWidth: 32; implicitHeight: 30
                                                    fontPixelSize: 11; quiet: true
                                                    Accessible.name: "Réinitialiser la taille du texte de l’offre"
                                                    onClicked: root.resetDetailTextZoom()
                                                }
                                                AppButton {
                                                    objectName: "detailZoomIn"
                                                    text: "A+"
                                                    Layout.fillWidth: true
                                                    implicitWidth: 32; implicitHeight: 30
                                                    fontPixelSize: 11; quiet: true
                                                    enabled: root.detailTextZoom < 1.55
                                                    Accessible.name: "Agrandir le texte de l’offre"
                                                    onClicked: root.adjustDetailTextZoom(0.1)
                                                }
                                            }
                                        }
                                    }
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 6
                                    Text {
                                        Layout.maximumWidth: Math.max(150, detailContent.width * 0.42)
                                        text: AppBridge.currentJob.company || "Entreprise non indiquée"
                                        color: Theme.ink
                                        font.pixelSize: root.detailMetaFontSize
                                        font.weight: Font.DemiBold
                                        elide: Text.ElideRight
                                    }
                                    Text { text: "·"; color: Theme.inkFaint; font.pixelSize: root.detailMetaFontSize }
                                    Text {
                                        Layout.fillWidth: true
                                        text: AppBridge.currentJob.location || "Lieu non indiqué"
                                        color: Theme.inkMuted
                                        font.pixelSize: root.detailMetaFontSize
                                        elide: Text.ElideRight
                                    }
                                }
                                Rectangle {
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 1
                                    Layout.bottomMargin: root.hasInsightCards ? 8 : 0
                                    color: Theme.line
                                }
                                Rectangle {
                                    visible: root.hasMatchInsights
                                    Layout.fillWidth: true
                                    implicitHeight: matchReasonsContent.implicitHeight + 24
                                    radius: 16
                                    color: root.successCardSurface
                                    border.color: root.successCardBorder
                                    Rectangle {
                                        anchors.left: parent.left
                                        anchors.right: parent.right
                                        anchors.top: parent.top
                                        anchors.leftMargin: 18
                                        anchors.rightMargin: 18
                                        anchors.topMargin: 1
                                        height: 1
                                        color: "#12FFFFFF"
                                    }
                                    ColumnLayout {
                                        id: matchReasonsContent
                                        anchors.fill: parent
                                        anchors.margins: 12
                                        spacing: 7
                                        RowLayout {
                                            Layout.fillWidth: true
                                            spacing: 8
                                            Rectangle {
                                                Layout.preferredWidth: 24
                                                Layout.preferredHeight: 24
                                                radius: 8
                                                color: root.successIconSurface
                                                border.color: root.successCardBorder
                                                SvgIcon { anchors.centerIn: parent; source: Theme.icon("check"); color: Theme.success; width: 13; height: 13 }
                                            }
                                            Text { Layout.fillWidth: true; text: "Correspond à votre profil"; color: Theme.success; font.pixelSize: root.detailMetaFontSize; font.weight: Font.Bold }
                                        }
                                        Repeater {
                                            model: AppBridge.currentJob.match_reasons || []
                                            delegate: RowLayout {
                                                required property var modelData
                                                Layout.fillWidth: true
                                                spacing: 8
                                                Rectangle { Layout.preferredWidth: 5; Layout.preferredHeight: 5; radius: 3; color: Theme.success }
                                                Text { Layout.fillWidth: true; text: modelData; color: Theme.ink; font.pixelSize: root.detailMetaFontSize; wrapMode: Text.WordWrap; lineHeight: 1.25 }
                                            }
                                        }
                                    }
                                }
                                Rectangle {
                                    visible: root.hasRiskInsights
                                    Layout.fillWidth: true
                                    implicitHeight: risksContent.implicitHeight + 24
                                    radius: 16
                                    color: root.warningCardSurface
                                    border.color: root.warningCardBorder
                                    Rectangle {
                                        anchors.left: parent.left
                                        anchors.right: parent.right
                                        anchors.top: parent.top
                                        anchors.leftMargin: 18
                                        anchors.rightMargin: 18
                                        anchors.topMargin: 1
                                        height: 1
                                        color: "#12FFFFFF"
                                    }
                                    ColumnLayout {
                                        id: risksContent
                                        anchors.fill: parent
                                        anchors.margins: 12
                                        spacing: 7
                                        RowLayout {
                                            Layout.fillWidth: true
                                            spacing: 8
                                            Rectangle {
                                                Layout.preferredWidth: 24
                                                Layout.preferredHeight: 24
                                                radius: 8
                                                color: root.warningIconSurface
                                                border.color: root.warningCardBorder
                                                SvgIcon { anchors.centerIn: parent; source: Theme.icon("alert-circle"); color: Theme.warning; width: 13; height: 13 }
                                            }
                                            Text { Layout.fillWidth: true; text: "Points à vérifier"; color: Theme.warning; font.pixelSize: root.detailMetaFontSize; font.weight: Font.Bold }
                                        }
                                        Repeater {
                                            model: AppBridge.currentJob.risks || []
                                            delegate: RowLayout {
                                                required property var modelData
                                                Layout.fillWidth: true
                                                spacing: 8
                                                Rectangle { Layout.preferredWidth: 5; Layout.preferredHeight: 5; radius: 3; color: Theme.warning }
                                                Text { Layout.fillWidth: true; text: modelData; color: Theme.ink; font.pixelSize: root.detailMetaFontSize; wrapMode: Text.WordWrap; lineHeight: 1.25 }
                                            }
                                        }
                                    }
                                }
                                Rectangle {
                                    visible: root.hasDuplicateInsight
                                    Layout.fillWidth: true
                                    implicitHeight: duplicateReviewContent.implicitHeight + 24
                                    radius: 16
                                    color: root.warningCardSurface
                                    border.color: Theme.warning
                                    ColumnLayout {
                                        id: duplicateReviewContent
                                        anchors.fill: parent
                                        anchors.margins: 12
                                        spacing: 8
                                        RowLayout {
                                            Layout.fillWidth: true
                                            spacing: 8
                                            Rectangle {
                                                Layout.preferredWidth: 24
                                                Layout.preferredHeight: 24
                                                radius: 8
                                                color: root.warningIconSurface
                                                border.color: root.warningCardBorder
                                                SvgIcon { anchors.centerIn: parent; source: Theme.icon("alert-circle"); color: Theme.warning; width: 13; height: 13 }
                                            }
                                            Text {
                                                Layout.fillWidth: true
                                                text: "Doublon possible"
                                                color: Theme.warning
                                                font.pixelSize: root.detailMetaFontSize
                                                font.weight: Font.Bold
                                                wrapMode: Text.WordWrap
                                            }
                                        }
                                        Text {
                                            Layout.fillWidth: true
                                            text: {
                                                var candidate = AppBridge.currentJob.duplicate_candidate || ({})
                                                return "Offre comparée : " + (candidate.company || "Entreprise inconnue") + " — " + (candidate.title || "Titre indisponible") + (candidate.location ? " · " + candidate.location : "")
                                            }
                                            color: Theme.ink
                                            font.pixelSize: root.detailSmallFontSize
                                            wrapMode: Text.WordWrap
                                        }
                                        RowLayout {
                                            Layout.fillWidth: true
                                            spacing: 8
                                            AppButton {
                                                Layout.fillWidth: true
                                                text: "Même offre"
                                                kind: "primary"
                                                fontPixelSize: root.detailButtonFontSize
                                                refined: true
                                                implicitHeight: 36
                                                enabled: !AppBridge.busy
                                                onClicked: AppBridge.resolveDuplicate(AppBridge.currentJob.id, true)
                                            }
                                            AppButton {
                                                Layout.fillWidth: true
                                                text: "Offres différentes"
                                                fontPixelSize: root.detailButtonFontSize
                                                refined: true
                                                implicitHeight: 36
                                                enabled: !AppBridge.busy
                                                onClicked: AppBridge.resolveDuplicate(AppBridge.currentJob.id, false)
                                            }
                                        }
                                    }
                                }
                                Rectangle {
                                    visible: root.hasArchiveInsight
                                    Layout.fillWidth: true
                                    implicitHeight: archiveContent.implicitHeight + 24
                                    radius: 16; color: root.warningCardSurface; border.color: root.warningCardBorder
                                    ColumnLayout {
                                        id: archiveContent
                                        anchors.fill: parent; anchors.margins: 12; spacing: 6
                                        RowLayout {
                                            Layout.fillWidth: true
                                            spacing: 8
                                            Rectangle {
                                                Layout.preferredWidth: 22
                                                Layout.preferredHeight: 22
                                                radius: 7
                                                color: root.warningIconSurface
                                                border.color: root.warningCardBorder
                                                SvgIcon { anchors.centerIn: parent; source: Theme.icon("folder"); color: Theme.warning; width: 12; height: 12 }
                                            }
                                            Text { Layout.fillWidth: true; text: "Raison de l’archivage"; color: Theme.warning; font.pixelSize: root.detailMetaFontSize; font.weight: Font.DemiBold }
                                        }
                                        Text { Layout.fillWidth: true; text: "• " + (AppBridge.currentJob.archive_reasons || []).slice(0, 4).join("\n• "); color: Theme.inkSoft; font.pixelSize: root.detailMetaFontSize; wrapMode: Text.WordWrap; lineHeight: 1.18 }
                                    }
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    Layout.topMargin: root.hasInsightCards ? 18 : 4
                                    spacing: 8
                                    Rectangle { Layout.preferredWidth: 3; Layout.preferredHeight: 13; radius: 2; color: Theme.accent }
                                    FormLabel { text: "DESCRIPTION DU POSTE"; fontPixelSize: root.detailMetaFontSize }
                                    Item { Layout.fillWidth: true }
                                    Text { Layout.maximumWidth: detailContent.width * 0.42; text: AppBridge.currentJob.role_type || AppBridge.currentJob.domain || ""; color: Theme.inkFaint; font.pixelSize: root.detailSmallFontSize; elide: Text.ElideRight }
                                }
                                Item {
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: Math.max(180, descriptionText.implicitHeight + 6)
                                    Text {
                                        id: descriptionText
                                        anchors.left: parent.left
                                        anchors.right: parent.right
                                        anchors.top: parent.top
                                        anchors.topMargin: 2
                                        text: root.descriptionForDisplay(AppBridge.currentJob.description)
                                        color: Theme.inkSoft
                                        font.pixelSize: root.detailBodyFontSize
                                        wrapMode: Text.WordWrap
                                        lineHeight: 1.32
                                        textFormat: Text.PlainText
                                    }
                                }
                                AppTextArea {
                                    id: applicationNotes
                                    visible: Boolean(root.applicationData.id)
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 72
                                    fontPixelSize: root.detailBodyFontSize
                                    text: root.applicationData.notes || ""
                                    placeholderText: "Ajouter une note de suivi…"
                                    onTextChanged: if (activeFocus) applicationNotesSaveTimer.restart()
                                }
                                Item { Layout.preferredHeight: 1 }
                            }
                        }
                        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 6
                            RowLayout {
                                visible: AppBridge.currentJob.status === "archived"
                                Layout.fillWidth: true
                                spacing: 6
                                AppButton {
                                    objectName: "offerRestoreAction"
                                    text: "Désarchiver et créer"
                                    iconSource: Theme.icon("refresh")
                                    kind: "primary"
                                    fontPixelSize: root.detailButtonFontSize
                                    refined: true
                                    implicitHeight: 34
                                    enabled: !AppBridge.busy
                                    onClicked: AppBridge.rescueJob(AppBridge.currentJob.id)
                                    ToolTip.visible: hovered
                                    ToolTip.text: "Analyse l’offre puis génère le CV et la lettre. Un doublon reprend son dossier principal."
                                }
                                Item { Layout.fillWidth: true }
                                AppButton {
                                    visible: Boolean(AppBridge.currentJob.url)
                                    text: "Offre"
                                    iconSource: Theme.icon("arrow-up-right")
                                    kind: "primary"
                                    fontPixelSize: root.detailButtonFontSize
                                    refined: true
                                    implicitHeight: 34
                                    onClicked: AppBridge.openUrl(AppBridge.currentJob.url)
                                }
                            }
                            RowLayout {
                                visible: Boolean(root.applicationData.id) && AppBridge.currentJob.status !== "archived"
                                Layout.fillWidth: true
                                spacing: 6
                                AppButton {
                                    visible: Boolean(AppBridge.currentJob.can_send)
                                    Layout.minimumWidth: root.detailActionButtonWidth
                                    Layout.preferredWidth: root.detailActionButtonWidth
                                    Layout.maximumWidth: root.detailActionButtonWidth
                                    text: "Envoyer"
                                    iconSource: Theme.icon("check")
                                    kind: "success"
                                    fontPixelSize: root.detailButtonFontSize
                                    refined: true
                                    iconSize: 13
                                    enabled: !AppBridge.busy
                                    implicitHeight: root.detailActionButtonHeight
                                    onClicked: AppBridge.markJobSent(AppBridge.currentJob.id)
                                }
                                AppButton {
                                    Layout.minimumWidth: root.detailActionButtonWidth
                                    Layout.preferredWidth: root.detailActionButtonWidth
                                    Layout.maximumWidth: root.detailActionButtonWidth
                                    text: "Archiver"
                                    iconSource: Theme.icon("folder")
                                    kind: "warning"
                                    fontPixelSize: root.detailButtonFontSize
                                    refined: true
                                    iconSize: 13
                                    implicitHeight: root.detailActionButtonHeight
                                    onClicked: AppBridge.archiveApplication(Number(root.applicationData.id))
                                }
                                AppButton {
                                    Layout.minimumWidth: root.detailActionButtonWidth
                                    Layout.preferredWidth: root.detailActionButtonWidth
                                    Layout.maximumWidth: root.detailActionButtonWidth
                                    text: "Offre"
                                    iconSource: Theme.icon("arrow-up-right")
                                    kind: "primary"
                                    fontPixelSize: root.detailButtonFontSize
                                    refined: true
                                    iconSize: 13
                                    enabled: Boolean(AppBridge.currentJob.url)
                                    implicitHeight: root.detailActionButtonHeight
                                    onClicked: AppBridge.openUrl(AppBridge.currentJob.url)
                                }
                                Item { Layout.fillWidth: true }
                            }
                            RowLayout {
                                visible: !Boolean(root.applicationData.id) && AppBridge.currentJob.status !== "archived"
                                Layout.fillWidth: true
                                spacing: 6
                                AppButton {
                                    objectName: "offerPrimaryAction"
                                    Layout.preferredWidth: Math.max(148, implicitWidth)
                                    text: AppBridge.currentJob.status === "duplicate_review" ? "Décision requise"
                                        : AppBridge.currentJob.related_application_id ? "Voir le dossier #" + AppBridge.currentJob.related_application_id
                                        : Boolean(AppBridge.currentJob.can_generate) ? "Créer"
                                        : "Analyser"
                                    iconSource: Theme.icon("sparkle")
                                    kind: "primary"
                                    fontPixelSize: root.detailButtonFontSize
                                    refined: true
                                    implicitHeight: 34
                                    enabled: !AppBridge.busy && AppBridge.currentJob.status !== "duplicate_review"
                                    onClicked: {
                                        if (AppBridge.currentJob.status === "duplicate_review") return
                                        if (AppBridge.currentJob.related_application_id) AppBridge.openApplication(AppBridge.currentJob.related_application_id)
                                        else if (AppBridge.currentJob.can_generate) AppBridge.generateApplication(AppBridge.currentJob.id)
                                        else AppBridge.analyzeJob(AppBridge.currentJob.id)
                                    }
                                }
                                Item { Layout.fillWidth: true }
                                AppButton {
                                    visible: Boolean(AppBridge.currentJob.url)
                                    text: "Offre"
                                    iconSource: Theme.icon("arrow-up-right")
                                    kind: "primary"
                                    fontPixelSize: root.detailButtonFontSize
                                    refined: true
                                    implicitHeight: 34
                                    onClicked: AppBridge.openUrl(AppBridge.currentJob.url)
                                }
                            }
                            RowLayout {
                                visible: Boolean(root.selectedId)
                                Layout.fillWidth: true
                                spacing: 6
                                AppButton {
                                    visible: Boolean(root.applicationData.id)
                                    Layout.minimumWidth: root.detailActionButtonWidth
                                    Layout.preferredWidth: root.detailActionButtonWidth
                                    Layout.maximumWidth: root.detailActionButtonWidth
                                    text: "CV"
                                    iconSource: Theme.icon("document")
                                    fontPixelSize: root.detailButtonFontSize
                                    refined: true
                                    quiet: true
                                    iconSize: 13
                                    implicitHeight: root.detailActionButtonHeight
                                    enabled: Boolean(root.applicationData.cv_pdf_path)
                                    onClicked: AppBridge.openPath(root.applicationData.cv_pdf_path)
                                }
                                AppButton {
                                    visible: Boolean(root.applicationData.id)
                                    Layout.minimumWidth: root.detailActionButtonWidth
                                    Layout.preferredWidth: root.detailActionButtonWidth
                                    Layout.maximumWidth: root.detailActionButtonWidth
                                    text: "Lettre"
                                    iconSource: Theme.icon("document")
                                    fontPixelSize: root.detailButtonFontSize
                                    refined: true
                                    quiet: true
                                    iconSize: 13
                                    implicitHeight: root.detailActionButtonHeight
                                    enabled: Boolean(root.applicationData.letter_pdf_path)
                                    onClicked: AppBridge.openPath(root.applicationData.letter_pdf_path)
                                }
                                AppButton {
                                    visible: Boolean(root.applicationData.id)
                                    Layout.minimumWidth: root.detailActionButtonWidth
                                    Layout.preferredWidth: root.detailActionButtonWidth
                                    Layout.maximumWidth: root.detailActionButtonWidth
                                    text: "Dossier"
                                    iconSource: Theme.icon("folder")
                                    fontPixelSize: root.detailButtonFontSize
                                    refined: true
                                    quiet: true
                                    iconSize: 13
                                    implicitHeight: root.detailActionButtonHeight
                                    onClicked: AppBridge.openApplicationFolder(Number(root.applicationData.id))
                                }
                                AppButton {
                                    visible: !Boolean(root.applicationData.id)
                                        && AppBridge.currentJob.status !== "archived"
                                        && AppBridge.currentJob.status !== "duplicate_review"
                                    text: Boolean(AppBridge.currentJob.shortlisted) ? "Retirer" : "Ajouter"
                                    iconSource: Boolean(AppBridge.currentJob.shortlisted) ? Theme.icon("x") : Theme.icon("plus")
                                    fontPixelSize: root.detailButtonFontSize
                                    refined: true
                                    implicitHeight: 32
                                    onClicked: AppBridge.setJobShortlisted(AppBridge.currentJob.id, !Boolean(AppBridge.currentJob.shortlisted))
                                }
                                AppButton {
                                    visible: !Boolean(root.applicationData.id)
                                        && AppBridge.currentJob.status !== "archived"
                                        && AppBridge.currentJob.status !== "duplicate_review"
                                    text: "Archiver"
                                    iconSource: Theme.icon("folder")
                                    kind: "warning"
                                    fontPixelSize: root.detailButtonFontSize
                                    refined: true
                                    implicitHeight: 32
                                    onClicked: AppBridge.archiveJob(AppBridge.currentJob.id)
                                }
                                Item { Layout.fillWidth: true }
                                Rectangle {
                                    Layout.preferredWidth: root.sortedJobs.length > 1 ? 150 : 30
                                    Layout.preferredHeight: 30
                                    radius: 10
                                    color: Theme.surfaceMuted
                                    border.color: Theme.line
                                    RowLayout {
                                        anchors.fill: parent
                                        anchors.margins: 1
                                        spacing: 0
                                        AppButton {
                                            Layout.preferredWidth: 28
                                            Layout.preferredHeight: 28
                                            text: ""
                                            iconSource: Theme.icon("refresh")
                                            refined: true
                                            quiet: true
                                            iconSize: 12
                                            Accessible.name: "Actualiser les offres"
                                            onClicked: AppBridge.loadJobs(searchField.text, statusFilter.currentValue || "", root.sortKey, root.sortAscending)
                                        }
                                        AppButton {
                                            visible: root.sortedJobs.length > 1
                                            Layout.preferredWidth: 28
                                            Layout.preferredHeight: 28
                                            text: ""
                                            iconSource: Theme.icon("chevron-left")
                                            refined: true
                                            quiet: true
                                            iconSize: 11
                                            enabled: root.canGoPrevious && !AppBridge.busy
                                            Accessible.name: "Offre précédente"
                                            onClicked: root.moveSelection(-1)
                                        }
                                        Text {
                                            visible: root.sortedJobs.length > 1
                                            Layout.fillWidth: true
                                            horizontalAlignment: Text.AlignHCenter
                                            text: root.selectedPosition >= 0
                                                ? (root.selectedPosition + 1) + " / " + root.sortedJobs.length
                                                : ""
                                            color: Theme.inkMuted
                                            font.pixelSize: root.detailSmallFontSize
                                            font.weight: Font.DemiBold
                                        }
                                        AppButton {
                                            visible: root.sortedJobs.length > 1
                                            Layout.preferredWidth: 28
                                            Layout.preferredHeight: 28
                                            text: ""
                                            iconSource: Theme.icon("chevron-right")
                                            refined: true
                                            quiet: true
                                            iconSize: 11
                                            enabled: root.canGoNext && !AppBridge.busy
                                            Accessible.name: "Offre suivante"
                                            onClicked: root.moveSelection(1)
                                        }
                                    }
                                }
                            }
                        }
                    }
                    Rectangle {
                        anchors.fill: parent
                        visible: AppBridge.jobDetailLoading
                        color: "#B60F0D16"
                        z: 8
                        BusyIndicator {
                            anchors.centerIn: parent
                            running: parent.visible
                            width: 30
                            height: 30
                        }
                        Text {
                            anchors.top: parent.verticalCenter
                            anchors.topMargin: 25
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: "Chargement…"
                            color: Theme.inkMuted
                            font.pixelSize: root.detailMetaFontSize
                            font.weight: Font.DemiBold
                        }
                    }
                }
            }
        }
    }
