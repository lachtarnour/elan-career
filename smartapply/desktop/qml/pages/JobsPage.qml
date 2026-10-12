import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    objectName: "jobsPage"
    signal navigateRequested(string route)
    signal filterChanged(string status)

    property int selectedId: displayedJobId
    property bool activeView: visible
    property int topInset: 0
    property real savedListScrollY: 0
    property bool returningToOffers: false
    property bool restoreListScroll: false
    property bool pageResetRequested: false
    // A generated application is displayed as part of its offer. This keeps
    // the offer list and the application tracking state in one workspace.
    property var applicationData: AppBridge.currentJob.application || ({})
    property int displayedJobId: 0
    readonly property bool detailMatchesSelection: displayedJobId === Number(AppBridge.currentJob.id || 0)
    property int notesApplicationId: 0
    property bool loadingNoteText: false
    property var noteDrafts: ({})
    readonly property var activeNoteDraft: noteDrafts[String(notesApplicationId)] || ({})
    // Materialize once per notification: indexed Qt sequence reads otherwise
    // call the Python property getter again for every inspected ID.
    readonly property var busyJobIdsSnapshot: {
        var ids = AppBridge.busyJobIds || []
        return ids.slice()
    }
    readonly property bool selectedJobBusy: busyJobIdsSnapshot.indexOf(Number(selectedId)) >= 0
    readonly property bool duplicateCandidateBusy: busyJobIdsSnapshot.indexOf(Number((AppBridge.currentJob.duplicate_candidate || {}).id || 0)) >= 0
    readonly property var generationWorkspace: AppBridge.generationWorkspace || ({})
    readonly property var generationReservedIds: generationWorkspace.reserved_job_ids || []
    readonly property var generationResumableIds: generationWorkspace.resumable_job_ids || []
    readonly property bool generationLaunchEnabled: !AppBridge.shutdownRequested
        && !AppBridge.generationWorkspaceLoading

    function canStartGeneration(ids) {
        return generationLaunchEnabled && ids.length > 0 && ids.every(function(id) {
            return isGenerationResumable(id)
                || (!anyJobBusy([id]) && generationReservedIds.indexOf(Number(id)) < 0)
        })
    }

    function isGenerationResumable(id) {
        return generationResumableIds.indexOf(Number(id)) >= 0
    }

    function canGenerateOffer(row) {
        return Boolean(row.can_generate) || isGenerationResumable(row.id)
    }

    function chooseGenerationMode(ids, shortlist, count) {
        if (shortlist ? !generationLaunchEnabled : !canStartGeneration(ids)) return
        generationMode.jobIds = ids.slice()
        generationMode.shortlist = Boolean(shortlist)
        generationMode.offerCount = shortlist ? Number(count || 0) : ids.length
        generationMode.open()
    }

    GenerationModeDialog {
        id: generationMode
        controlsEnabled: root.generationLaunchEnabled
        onModeChosen: function(mode, ids, shortlist) {
            generationTracking.expanded = true
            generationTracking.selectedRunId = ""
            if (shortlist) AppBridge.startShortlistedGeneration(mode)
            else if (mode === "immediate") AppBridge.startImmediateGeneration(ids)
            else AppBridge.startGenerationBatch(ids)
        }
    }
    ConfirmDialog {
        id: deleteOfferDialog
        objectName: "deleteReadyOfferDialog"
        anchors.centerIn: parent
        property int targetJobId: 0
        property string targetTitle: ""
        property string targetCompany: ""
        heading: "Supprimer cette offre ?"
        message: targetTitle + " · " + targetCompany
            + "\n\nL’offre, ses copies confirmées et son dossier de candidature seront effacés définitivement. Cette action est irréversible."
        confirmText: "Supprimer définitivement"
        kind: "danger"
        confirmEnabled: !AppBridge.shutdownRequested && root.busyJobIdsSnapshot.indexOf(targetJobId) < 0
        onAccepted: AppBridge.deleteReadyOffer(targetJobId)
    }
    readonly property var documentReviewPoints: applicationData.review_points || []
    readonly property int documentReviewPointCount: documentReviewPoints.length
    property string sortKey: "llm_score"
    property bool sortAscending: false
    property string filterStatus: "analyzed"
    property string requestedFilter: "analyzed"
    property string filterDisposition: ""
    property string archiveOrigin: ""
    property string missionRelation: ""
    property bool matchPointsExpanded: false
    property bool riskPointsExpanded: false
    property var selectedJobs: ({})
    property var selectedOrder: []
    readonly property int tableScrollGutter: Theme.scrollGutter
    // Header and rows share one measured content width and the same grid.
    readonly property real tableContentWidth: Math.max(0, tableSurface.width - 24 - tableScrollGutter)
    readonly property real tableScale: Math.min(1, Math.max(0, (tableContentWidth - 540) / 440))
    readonly property int columnGap: Math.round(6 + 4 * tableScale)
    readonly property int selectWidth: 24
    readonly property int experienceWidth: Math.round(52 + 12 * tableScale)
    readonly property int llmScoreWidth: Math.round(42 + 6 * tableScale)
    readonly property int publishedDateWidth: Math.round(76 + 12 * tableScale)
    readonly property int operationDateWidth: Math.round(94 + 10 * tableScale)
    readonly property int archiveDateWidth: Math.round(110 + 10 * tableScale)
    readonly property bool isNewOffersFilter: filterStatus === "scraped"
    readonly property bool showsLifecycleDate: ["analyzed", "ready_for_form_submission", "sent"].indexOf(filterStatus) >= 0
    readonly property bool showsArchiveDate: filterStatus === "archived"
    // Nouvelle shows the source publication date beside the local-filter
    // disposition. Unknown publication dates remain explicit instead of
    // falling back to the import date.
    readonly property int filterDispositionWidth: 2 * llmScoreWidth + columnGap
    // Reserve the widest right-hand block for every status. The company,
    // title, experience and location columns then keep their positions.
    readonly property int rightColumnsWidth: Math.max(
        publishedDateWidth + columnGap + filterDispositionWidth,
        llmScoreWidth + columnGap + Math.max(operationDateWidth, archiveDateWidth))
    readonly property int visibleColumnGapCount: 5
    readonly property real textColumnsWidth: Math.max(0, tableContentWidth - selectWidth - experienceWidth - rightColumnsWidth - columnGap * visibleColumnGapCount)
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
    readonly property bool hasFitInsights: AppBridge.currentJob.llm_score !== null
        && AppBridge.currentJob.llm_score !== undefined
    readonly property bool hasAnalysis: Boolean(AppBridge.currentJob.has_analysis)
    readonly property var matchInsightPoints: AppBridge.currentJob.match_reasons || []
    readonly property bool hasMatchInsights: hasAnalysis
    readonly property bool hasClassificationInsight: Boolean(AppBridge.currentJob.classification_summary)
    readonly property bool hasRiskInsights: (AppBridge.currentJob.risks || []).length > 0
    readonly property var orderedRiskPoints: prioritizeRiskPoints(AppBridge.currentJob.risks || [])
    readonly property bool hasDuplicateInsight: AppBridge.currentJob.duplicate_review_status === "pending"
    readonly property bool hasArchiveInsight: AppBridge.currentJob.status === "archived"
        && (AppBridge.currentJob.archive_reasons || []).length > 0
    readonly property bool hasInsightCards: hasFitInsights || hasClassificationInsight || hasMatchInsights || hasRiskInsights || hasDuplicateInsight || hasArchiveInsight
        || documentReviewPointCount > 0

    function prioritizeRiskPoints(points) {
        if (points.length <= 4) return points
        var first = points.slice(0, 2)
        var candidatePoints = points.filter(function(point) {
            return /^(Appui candidat à préciser|Écart établi|Non documenté|Orientation\/préférence)\s*:/.test(String(point))
        }).slice(0, 2)
        var ordered = first.slice()
        candidatePoints.forEach(function(point) {
            if (ordered.indexOf(point) < 0) ordered.push(point)
        })
        points.forEach(function(point) {
            if (ordered.indexOf(point) < 0) ordered.push(point)
        })
        return ordered
    }

    function setNoteDraft(applicationId, draft) {
        var next = {}
        for (var key in noteDrafts) next[key] = noteDrafts[key]
        next[String(applicationId)] = draft
        noteDrafts = next
    }

    function loadActiveNotes() {
        var key = String(notesApplicationId)
        if (notesApplicationId && !noteDrafts[key]) {
            setNoteDraft(notesApplicationId, {
                text: String((AppBridge.currentJob.application || {}).notes || ""), revision: 0,
                savedRevision: 0, submittedRevision: 0, error: ""
            })
        }
        loadingNoteText = true
        applicationNotes.text = notesApplicationId ? noteDrafts[key].text : ""
        loadingNoteText = false
    }

    function editNote(text) {
        if (loadingNoteText || !notesApplicationId) return
        var draft = noteDrafts[String(notesApplicationId)]
        if (!draft || draft.text === text) return
        setNoteDraft(notesApplicationId, {
            text: text, revision: draft.revision + 1,
            savedRevision: draft.savedRevision, submittedRevision: draft.submittedRevision,
            error: ""
        })
        applicationNotesSaveTimer.restart()
    }

    function saveNote(applicationId) {
        var draft = noteDrafts[String(applicationId)]
        if (!draft || draft.revision <= draft.savedRevision || draft.revision <= draft.submittedRevision) return
        setNoteDraft(applicationId, {
            text: draft.text, revision: draft.revision,
            savedRevision: draft.savedRevision, submittedRevision: draft.revision, error: ""
        })
        AppBridge.saveApplicationNotes(Number(applicationId), draft.text, draft.revision)
    }

    function flushNotes() {
        applicationNotesSaveTimer.stop()
        for (var key in noteDrafts) saveNote(Number(key))
    }

    function updateSelectedDetail() {
        var nextJobId = Number(AppBridge.currentJob.id || 0)
        var nextApplicationId = Number((AppBridge.currentJob.application || {}).id || 0)
        if (notesApplicationId !== nextApplicationId) {
            saveNote(notesApplicationId)
            notesApplicationId = nextApplicationId
            loadActiveNotes()
        }
        if (displayedJobId !== nextJobId) {
            displayedJobId = nextJobId
            matchPointsExpanded = false
            riskPointsExpanded = false
            detailViewport.contentY = 0
            var index = selectedIndex()
            if (index >= 0) jobsTable.positionViewAtIndex(index, ListView.Contain)
        }
    }

    function anyJobBusy(ids) {
        var protectedIds = root.busyJobIdsSnapshot
        return ids.some(function(id) { return protectedIds.indexOf(Number(id)) >= 0 })
    }

    function companyX() { return selectWidth + columnGap }
    function titleX() { return companyX() + companyWidth + columnGap }
    function lifecycleDateX(contentWidth) { return contentWidth - operationDateWidth }
    function archiveDateX(contentWidth) { return contentWidth - archiveDateWidth }
    function llmScoreX(contentWidth) { return contentWidth - rightColumnsWidth }
    function filterDispositionX(contentWidth) { return contentWidth - filterDispositionWidth }
    function publishedDateX(contentWidth) { return filterDispositionX(contentWidth) - columnGap - publishedDateWidth }
    function locationX(contentWidth) {
        var rightColumnX = isNewOffersFilter ? publishedDateX(contentWidth) : llmScoreX(contentWidth)
        return rightColumnX - columnGap - locationWidth
    }
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


    readonly property var sortedJobs: AppBridge.jobs || []
    property var selectedRows: {
        return selectedOrder.map(function(id) { return selectedJobs[String(id)] })
            .filter(function(row) { return Boolean(row) })
    }
    property var selectedIds: selectedRows.map(function(row) { return Number(row.id) })
    property var topLabelIds: selectedRows.filter(function(row) { return row.status !== "archived" && !row.application_id && !row.shortlisted }).map(function(row) { return Number(row.id) })
    property var generationIds: selectedRows.filter(function(row) {
        return root.canGenerateOffer(row) && (root.isGenerationResumable(row.id)
            || generationReservedIds.indexOf(Number(row.id)) < 0)
    }).map(function(row) { return Number(row.id) })
    property var rescueIds: selectedRows.filter(function(row) { return row.status === "archived" }).map(function(row) { return Number(row.id) })
    property int selectedCount: selectedIds.length
    readonly property bool allVisibleSelected: {
        if (sortedJobs.length === 0) return false
        for (var i = 0; i < sortedJobs.length; ++i) {
            if (!selectedJobs[String(sortedJobs[i].id)]) return false
        }
        return true
    }
    readonly property bool allFilteredSelected: AppBridge.jobsTotal > 0
        && selectedCount === AppBridge.jobsTotal

    function isSelected(jobId) { return Boolean(selectedJobs[String(jobId)]) }
    function setSelected(jobId, checked) {
        var next = {}
        for (var key in selectedJobs) next[key] = selectedJobs[key]
        var order = selectedOrder.slice()
        if (checked) {
            var row = sortedJobs.find(function(item) { return Number(item.id) === Number(jobId) })
            if (row) {
                next[String(jobId)] = row
                if (order.indexOf(Number(jobId)) < 0) order.push(Number(jobId))
            }
        }
        else {
            delete next[String(jobId)]
            order = order.filter(function(id) { return id !== Number(jobId) })
        }
        selectedJobs = next
        selectedOrder = order
    }
    function clearSelection() { selectedJobs = ({}); selectedOrder = [] }
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
        var order = selectedOrder.slice()
        for (var i = 0; i < sortedJobs.length; ++i) {
            var row = sortedJobs[i]
            if (allVisibleSelected) {
                delete next[String(row.id)]
                order = order.filter(function(id) { return id !== Number(row.id) })
            } else {
                next[String(row.id)] = row
                if (order.indexOf(Number(row.id)) < 0) order.push(Number(row.id))
            }
        }
        selectedJobs = next
        selectedOrder = order
    }
    function selectAllFiltered() { AppBridge.selectAllMatchingJobs() }
    function sortBy(key) {
        restoreListScroll = false
        jobsTable.contentY = 0
        if (sortKey === key) sortAscending = !sortAscending
        else {
            sortKey = key
            sortAscending = ["llm_score", "published_date", "operation_date", "archived_at"].indexOf(key) < 0
        }
        loadOffers(0)
    }

    function loadOffers(page) {
        var targetPage = page === undefined ? AppBridge.jobsPage : Math.max(0, Number(page))
        if (targetPage !== AppBridge.jobsPage) {
            restoreListScroll = false
            savedListScrollY = 0
            jobsTable.contentY = 0
            AppBridge.selectJob(0)
        }
        if (filterStatus === "archived")
            AppBridge.loadJobs(searchField.text, filterStatus, sortKey, sortAscending,
                "", targetPage, archiveOrigin)
        else if (filterStatus === "analyzed" && missionRelation !== "")
            AppBridge.loadJobs(searchField.text, filterStatus, sortKey, sortAscending,
                "", targetPage, "", missionRelation)
        else
            AppBridge.loadJobs(searchField.text, filterStatus, sortKey, sortAscending,
                isNewOffersFilter ? filterDisposition : "", targetPage)
    }

    function selectDisposition(disposition) {
        searchTimer.stop()
        filterDisposition = disposition
        restoreListScroll = false
        savedListScrollY = 0
        jobsTable.contentY = 0
        clearSelection()
        loadOffers(0)
    }

    function selectArchiveOrigin(origin) {
        searchTimer.stop()
        archiveOrigin = origin
        restoreListScroll = false
        savedListScrollY = 0
        jobsTable.contentY = 0
        clearSelection()
        loadOffers(0)
    }

    function selectMissionRelation(relation) {
        searchTimer.stop()
        missionRelation = relation
        restoreListScroll = false
        savedListScrollY = 0
        jobsTable.contentY = 0
        clearSelection()
        AppBridge.selectJob(0)
        loadOffers(0)
    }

    function setMinLlmScorePercent(percent) {
        AppBridge.setMinimumLlmScorePercent(Number(percent))
        restoreListScroll = false
        savedListScrollY = 0
        jobsTable.contentY = 0
        clearSelection()
        AppBridge.selectJob(0)
        loadOffers(0)
    }

    function useDefaultSortForStatus(status) {
        sortKey = status === "scraped" ? "filter_disposition"
            : "llm_score"
        sortAscending = status === "scraped"
    }

    function applyRequestedFilter() {
        if (!statusFilter) return
        var index = statusFilter.indexOfValue(requestedFilter)
        var nextStatus = index < 0 ? "analyzed" : requestedFilter
        index = statusFilter.indexOfValue(nextStatus)
        if (filterStatus !== nextStatus) {
            filterStatus = nextStatus
            useDefaultSortForStatus(nextStatus)
            pageResetRequested = true
            clearSelection()
            savedListScrollY = 0
            restoreListScroll = false
        }
        statusFilter.currentIndex = index
    }

    function refreshOffers(jobId) {
        applyRequestedFilter()
        if (isNewOffersFilter) {
            return
        }
        searchTimer.stop()
        restoreListScroll = returningToOffers && !jobId
        returningToOffers = false
        var resetPage = pageResetRequested
        pageResetRequested = false
        if (resetPage && !jobId) AppBridge.selectJob(0)
        loadOffers(resetPage ? 0 : undefined)
        var target = Number(jobId || (resetPage ? 0 : displayedJobId))
        if (target) AppBridge.selectJob(target)
        else updateSelectedDetail()
    }

    onRequestedFilterChanged: applyRequestedFilter()
    Component.onCompleted: {
        applyRequestedFilter()
        if (activeView) updateSelectedDetail()
    }
    onActiveViewChanged: {
        if (!activeView && jobsTable) {
            restoreListScroll = false
            savedListScrollY = jobsTable.contentY
            returningToOffers = true
            searchTimer.stop()
            flushNotes()
        }
    }
    Component.onDestruction: if (AppBridge) flushNotes()
    Timer {
        id: searchTimer
        objectName: "offerSearchDebounce"
        interval: 260
        onTriggered: {
            if (root.activeView) {
                root.clearSelection()
                jobsTable.contentY = 0
                root.loadOffers(0)
            }
        }
    }

    Timer {
        id: applicationNotesSaveTimer
        interval: 500
        onTriggered: root.flushNotes()
    }

    Connections {
        target: AppBridge
        function onOffersDeleted(jobIds, applicationIds) {
            var drafts = {}
            for (var key in root.noteDrafts) {
                if (applicationIds.indexOf(Number(key)) < 0) drafts[key] = root.noteDrafts[key]
            }
            root.noteDrafts = drafts
            var selected = {}
            for (var jobKey in root.selectedJobs) {
                if (jobIds.indexOf(Number(jobKey)) < 0) selected[jobKey] = root.selectedJobs[jobKey]
            }
            root.selectedJobs = selected
            root.selectedOrder = root.selectedOrder.filter(function(id) {
                return jobIds.indexOf(Number(id)) < 0
            })
        }
        function onJobSelectionClearRequested() { root.clearSelection() }
        function onAllMatchingJobsReady(rows) {
            var next = ({})
            rows.forEach(function(row) { next[String(row.id)] = row })
            root.selectedJobs = next
            root.selectedOrder = rows.map(function(row) { return Number(row.id) })
        }
        function onCurrentJobChanged() { if (root.activeView) root.updateSelectedDetail() }
        function onJobsChanged() {
            if (!root.activeView || !root.restoreListScroll) return
            Qt.callLater(function() {
                if (root.activeView && root.restoreListScroll) {
                    jobsTable.forceLayout()
                    jobsTable.contentY = root.savedListScrollY
                    root.restoreListScroll = false
                }
            })
        }
        function onNotesFlushRequested() { root.flushNotes() }
        function onApplicationNotesSaved(applicationId, revision) {
            var draft = root.noteDrafts[String(applicationId)]
            if (!draft) return
            root.setNoteDraft(applicationId, {
                text: draft.text, revision: draft.revision,
                savedRevision: Math.max(draft.savedRevision, revision),
                submittedRevision: draft.submittedRevision,
                error: revision >= draft.revision ? "" : draft.error
            })
        }
        function onApplicationNotesSaveFailed(applicationId, revision, message) {
            var draft = root.noteDrafts[String(applicationId)]
            if (!draft || revision < draft.submittedRevision) return
            root.setNoteDraft(applicationId, {
                text: draft.text, revision: draft.revision, savedRevision: draft.savedRevision,
                submittedRevision: draft.savedRevision, error: message
            })
        }
    }

    RowLayout {
        anchors.fill: parent
        anchors.margins: 10
        anchors.topMargin: 10 + root.topInset
        anchors.bottomMargin: 10 + (generationTracking.visible ? generationTracking.height + 8 : 0)
        spacing: 0
        visible: !root.isNewOffersFilter

        ColumnLayout {
            id: offersColumn
            Layout.fillWidth: true
            Layout.preferredWidth: 1
            Layout.minimumWidth: 500
            Layout.fillHeight: true
            Layout.rightMargin: 14
            spacing: 10

            Item {
                Layout.fillWidth: true
                Layout.minimumHeight: 38
                Layout.preferredHeight: 38
                Layout.maximumHeight: 38
                RowLayout {
                    id: toolbarRow
                    objectName: "offerToolbar"
                    anchors.fill: parent
                    spacing: 8
                    AppSelect {
                        id: statusFilter
                        objectName: "offerStatusFilter"
                        Layout.minimumWidth: 112
                        Layout.preferredWidth: toolbarRow.width < 600 ? 112 : 124
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
                            root.restoreListScroll = false
                            root.filterStatus = currentValue
                            root.useDefaultSortForStatus(currentValue)
                            root.pageResetRequested = false
                            root.filterChanged(currentValue)
                            root.clearSelection()
                            AppBridge.selectJob(0)
                            jobsTable.contentY = 0
                            if (!root.isNewOffersFilter) root.loadOffers(0)
                        }
                    }
                    Item {
                        id: secondaryFilterSlot
                        objectName: "offerSecondaryFilterSlot"
                        Layout.minimumWidth: toolbarRow.width < 600 ? 130 : 170
                        Layout.preferredWidth: Layout.minimumWidth
                        Layout.maximumWidth: Layout.minimumWidth
                        Layout.preferredHeight: 34
                        AppSelect {
                            id: dispositionFilter
                            objectName: "offerDispositionFilter"
                            visible: root.isNewOffersFilter
                            anchors.fill: parent
                            implicitHeight: 34
                            leftPadding: 12
                            rightPadding: 34
                            fontPixelSize: 12
                            indicatorSize: 14
                            indicatorMargin: 12
                            textRole: "label"
                            valueRole: "value"
                            Accessible.name: "Filtrer par classe"
                            model: [
                                {value: "", label: "Toutes les classes"},
                                {value: "rejected", label: "Rejeté"},
                                {value: "pending_relevance", label: "À classer"}
                            ]
                            currentIndex: Math.max(0, indexOfValue(root.filterDisposition))
                            onActivated: root.selectDisposition(currentValue)
                        }
                        AppSelect {
                            id: missionRelationFilter
                            objectName: "offerMissionRelationFilter"
                            visible: root.filterStatus === "analyzed"
                            anchors.fill: parent
                            implicitHeight: 34
                            leftPadding: 10
                            rightPadding: 24
                            fontPixelSize: 11
                            indicatorSize: 14
                            indicatorMargin: 8
                            textRole: "label"
                            valueRole: "value"
                            Accessible.name: "Métier proposé selon les missions : " + displayText
                            Accessible.description: "Classement des missions de l’offre, indépendant du score d’adéquation et de la candidature."
                            ToolTip.visible: hovered && !popup.visible
                            ToolTip.delay: 700
                            ToolTip.text: "Selon les missions de l’offre, indépendamment du score d’adéquation et de votre candidature."
                            model: [
                                {value: "", label: "Toutes les offres"},
                                {value: "direct", label: "Métier ciblé"},
                                {value: "adjacent", label: "Métier proche"},
                                {value: "clearly_outside", label: "Hors cible"},
                                {value: "unknown", label: "Non déterminé"}
                            ]
                            currentIndex: Math.max(0, indexOfValue(root.missionRelation))
                            onActivated: root.selectMissionRelation(currentValue)
                        }
                        AppSelect {
                            id: archiveOriginFilter
                            objectName: "offerArchiveOriginFilter"
                            visible: root.filterStatus === "archived"
                            anchors.fill: parent
                            implicitHeight: 34
                            leftPadding: 12
                            rightPadding: 34
                            fontPixelSize: 12
                            indicatorSize: 14
                            indicatorMargin: 12
                            textRole: "label"
                            valueRole: "value"
                            Accessible.name: "Origine de l’archivage"
                            model: [
                                {value: "", label: "Tous les archivages"},
                                {value: "automatic_filter", label: "Filtre automatique"},
                                {value: "mission_screen", label: "Missions hors cible"},
                                {value: "manual", label: "Archivage manuel"},
                                {value: "confirmed_duplicate", label: "Doublon confirmé"}
                            ]
                            currentIndex: Math.max(0, indexOfValue(root.archiveOrigin))
                            onActivated: root.selectArchiveOrigin(currentValue)
                        }
                    }
                    RowLayout {
                        id: shortlistControls
                        objectName: "offerShortlistControls"
                        visible: root.filterStatus === "analyzed" && !searchControl.expanded
                        Layout.minimumWidth: toolbarRow.width < 600 ? 156 : 192
                        Layout.preferredWidth: Layout.minimumWidth
                        Layout.maximumWidth: Layout.minimumWidth
                        spacing: 6
                        AppSpinBox {
                            id: topK
                            from: 1
                            to: 100
                            value: AppBridge.shortlistTopK
                            Layout.minimumWidth: toolbarRow.width < 600 ? 62 : 90
                            Layout.preferredWidth: Layout.minimumWidth
                            Layout.maximumWidth: Layout.minimumWidth
                            implicitHeight: 34
                            leftPadding: toolbarRow.width < 600 ? 22 : 34
                            rightPadding: toolbarRow.width < 600 ? 22 : 34
                            fontPixelSize: 12
                            indicatorSize: 13
                            indicatorWidth: toolbarRow.width < 600 ? 19 : 28
                            indicatorMargin: 4
                            onUserValueModified: function(nextValue) { AppBridge.setShortlistTopK(Number(nextValue)) }
                        }
                        AppButton {
                            Layout.minimumWidth: toolbarRow.width < 600 ? 88 : 96
                            Layout.preferredWidth: Layout.minimumWidth
                            Layout.maximumWidth: Layout.minimumWidth
                            text: "Shortlist"
                            iconSource: Theme.icon("sparkle")
                            kind: "primary"
                            refined: true
                            iconSize: 14
                            fontPixelSize: 11
                            implicitHeight: 34
                            enabled: !AppBridge.shutdownRequested
                            onClicked: {
                                topK.commitInput()
                                AppBridge.updateShortlist(topK.value)
                            }
                        }
                    }
                    Item { Layout.fillWidth: true }
                    Item {
                        id: searchControl
                        objectName: "expandableSearch"
                        property bool expanded: false
                        Layout.minimumWidth: 34
                        Layout.preferredWidth: expanded ? Math.min(260, Math.max(34,
                            toolbarRow.width - statusFilter.Layout.preferredWidth
                            - secondaryFilterSlot.Layout.preferredWidth
                            - (shortlistControls.visible ? shortlistControls.Layout.preferredWidth + toolbarRow.spacing : 0)
                            - refreshAction.implicitWidth - toolbarRow.spacing * 4)) : 34
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
                            onTextChanged: {
                                root.restoreListScroll = false
                                searchTimer.restart()
                            }
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
                    AppButton {
                        id: refreshAction
                        objectName: "offerRefreshAction"
                        text: ""
                        iconSource: Theme.icon("refresh")
                        refined: true
                        quiet: true
                        implicitWidth: 34
                        implicitHeight: 34
                        Accessible.name: "Actualiser les offres"
                        enabled: !AppBridge.shutdownRequested
                        onClicked: root.loadOffers()
                    }
                }
            }

            Rectangle {
                objectName: "offerSelectionBand"
                Layout.fillWidth: true
                Layout.preferredHeight: 48
                radius: 13
                color: root.selectedCount > 0 ? "#252039" : Theme.surfaceMuted
                border.color: root.selectedCount > 0 ? Theme.accentLine : Theme.line
                Rectangle { visible: root.selectedCount > 0; anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter; width: 3; height: 24; radius: 2; color: Theme.accentBright }
                Text {
                    visible: root.selectedCount === 0
                    anchors.left: parent.left
                    anchors.leftMargin: 14
                    anchors.verticalCenter: parent.verticalCenter
                    text: AppBridge.jobsLoading ? "Chargement des offres…"
                        : AppBridge.jobsTotal + (AppBridge.jobsTotal === 1 ? " offre" : " offres")
                    color: Theme.inkMuted
                    font.pixelSize: 11
                }
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 12
                    anchors.rightMargin: 8
                    spacing: 8
                    visible: root.selectedCount > 0
                    Rectangle {
                        Layout.preferredWidth: 28; Layout.preferredHeight: 28; radius: 9
                        color: Theme.accentSoft; border.color: Theme.accentLine
                        Text { anchors.centerIn: parent; text: root.selectedCount > 999 ? "999+" : root.selectedCount; color: Theme.accentDark; font.pixelSize: 11; font.weight: Font.Bold }
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
                        visible: !root.isNewOffersFilter && root.topLabelIds.length > 0
                        text: "Ajouter (" + root.topLabelIds.length + ")"
                        iconSource: Theme.icon("plus")
                        refined: true
                        iconSize: 13
                        fontPixelSize: 11
                        implicitHeight: 30
                        enabled: root.topLabelIds.length > 0 && !root.anyJobBusy(root.topLabelIds) && !AppBridge.shutdownRequested
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
                        enabled: root.rescueIds.length > 0 && !AppBridge.shutdownRequested
                        onClicked: AppBridge.rescueJobs(root.rescueIds)
                    }
                    AppButton {
                        objectName: "offerCreateSelectionAction"
                        visible: root.generationIds.length > 0
                        text: "Créer (" + root.generationIds.length + ")"
                        iconSource: Theme.icon("sparkle")
                        kind: "primary"
                        refined: true
                        iconSize: 13
                        fontPixelSize: 11
                        implicitHeight: 30
                        enabled: root.canStartGeneration(root.generationIds)
                        onClicked: root.chooseGenerationMode(root.generationIds, false, 0)
                    }
                }
            }

            Item {
                id: tableSurface
                objectName: "offerTable"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Item {
                    anchors.fill: parent
                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 0

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 42
                            color: "#191720"
                            radius: 0
                            Item {
                                anchors.fill: parent
                                anchors.leftMargin: 12
                                anchors.rightMargin: 12 + root.tableScrollGutter
                                SelectionBox {
                                    x: 0
                                    width: root.selectWidth
                                    anchors.verticalCenter: parent.verticalCenter
                                    checked: root.allFilteredSelected
                                    partial: root.selectedCount > 0 && !root.allFilteredSelected
                                    enabled: !AppBridge.selectionLoading && !AppBridge.jobsLoading
                                    Accessible.name: "Sélectionner tous les résultats du filtre"
                                    onToggled: function(checked) { if (checked) root.selectAllFiltered(); else root.clearSelection() }
                                }
                                SortHeader { objectName: "offerCompanyHeader"; x: root.companyX(); width: root.companyWidth; anchors.verticalCenter: parent.verticalCenter; title: "Entreprise"; sortKey: "company"; active: root.sortKey === sortKey; ascending: root.sortAscending; onSortRequested: function(key) { root.sortBy(key) } }
                                SortHeader { objectName: "offerTitleHeader"; x: root.titleX(); width: root.titleWidthFor(parent.width); anchors.verticalCenter: parent.verticalCenter; title: "Poste"; sortKey: "title"; active: root.sortKey === sortKey; ascending: root.sortAscending; onSortRequested: function(key) { root.sortBy(key) } }
                                SortHeader { x: root.experienceX(parent.width); width: root.experienceWidth; anchors.verticalCenter: parent.verticalCenter; title: "Exp."; sortKey: "experience"; active: root.sortKey === sortKey; ascending: root.sortAscending; onSortRequested: function(key) { root.sortBy(key) } }
                                SortHeader { objectName: "offerLocationHeader"; x: root.locationX(parent.width); width: root.locationWidth; anchors.verticalCenter: parent.verticalCenter; title: "Lieu"; sortKey: "location"; active: root.sortKey === sortKey; ascending: root.sortAscending; onSortRequested: function(key) { root.sortBy(key) } }
                                SortHeader { objectName: "offerIaHeader"; visible: !root.isNewOffersFilter; x: root.llmScoreX(parent.width); width: root.llmScoreWidth; anchors.verticalCenter: parent.verticalCenter; title: "IA"; sortKey: "llm_score"; active: root.sortKey === sortKey; ascending: root.sortAscending; centered: true; onSortRequested: function(key) { root.sortBy(key) } }
                                SortHeader {
                                    objectName: "offerLifecycleDateHeader"
                                    visible: root.showsLifecycleDate
                                    x: root.lifecycleDateX(parent.width)
                                    width: root.operationDateWidth
                                    anchors.verticalCenter: parent.verticalCenter
                                    title: "Operation date"
                                    Accessible.name: "Trier par date d’opération puis score IA"
                                    sortKey: "operation_date"
                                    active: root.sortKey === sortKey
                                    ascending: root.sortAscending
                                    centered: true
                                    onSortRequested: function(key) { root.sortBy(key) }
                                }
                                SortHeader {
                                    objectName: "offerArchiveDateHeader"
                                    visible: root.showsArchiveDate
                                    x: root.archiveDateX(parent.width)
                                    width: root.archiveDateWidth
                                    anchors.verticalCenter: parent.verticalCenter
                                    title: "Date d’archivage"
                                    sortKey: "archived_at"
                                    active: root.sortKey === sortKey
                                    ascending: root.sortAscending
                                    centered: true
                                    onSortRequested: function(key) { root.sortBy(key) }
                                }
                                SortHeader { objectName: "offerPublishedDateHeader"; visible: root.isNewOffersFilter; x: root.publishedDateX(parent.width); width: root.publishedDateWidth; anchors.verticalCenter: parent.verticalCenter; title: "Date"; sortKey: "published_date"; active: root.sortKey === sortKey; ascending: root.sortAscending; centered: true; onSortRequested: function(key) { root.sortBy(key) } }
                                SortHeader { objectName: "offerFilterHeader"; visible: root.isNewOffersFilter; x: root.filterDispositionX(parent.width); width: root.filterDispositionWidth; anchors.verticalCenter: parent.verticalCenter; title: "Décision"; sortKey: "filter_disposition"; active: root.sortKey === sortKey; ascending: root.sortAscending; centered: true; onSortRequested: function(key) { root.sortBy(key) } }
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
                                objectName: "offerListViewport"
                                anchors.fill: parent
                                anchors.margins: 4
                                visible: AppBridge.jobs.length > 0
                                clip: true
                                spacing: 2
                                reuseItems: true
                                cacheBuffer: 420
                                boundsBehavior: Flickable.StopAtBounds
                                onMovementStarted: root.restoreListScroll = false
                                model: AppBridge.jobsModel || root.sortedJobs
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
                                        Column {
                                            x: root.titleX()
                                            width: root.titleWidthFor(parent.width)
                                            anchors.verticalCenter: parent.verticalCenter
                                            spacing: 2
                                            Text {
                                                objectName: "offerTitle-" + modelData.id
                                                width: parent.width
                                                text: modelData.title
                                                color: Theme.ink
                                                font.pixelSize: root.tableTextSize
                                                font.weight: Font.Medium
                                                wrapMode: Text.WordWrap
                                                maximumLineCount: 2
                                                elide: Text.ElideRight
                                                lineHeight: 1.2
                                            }
                                            Text {
                                                objectName: "offerArchiveSubLabel-" + modelData.id
                                                visible: modelData.status === "archived" && Boolean(modelData.archive_sub_label)
                                                width: parent.width
                                                text: modelData.archive_sub_label || ""
                                                color: Theme.warning
                                                font.pixelSize: root.tableTextSize - 3
                                                elide: Text.ElideRight
                                            }
                                        }
                                        Text { objectName: "offerExperience-" + modelData.id; x: root.experienceX(parent.width); width: root.experienceWidth; anchors.verticalCenter: parent.verticalCenter; text: root.experienceLabel(modelData.experience); color: Theme.inkMuted; font.pixelSize: root.tableTextSize - 1; elide: Text.ElideRight }
                                        Text { objectName: "offerLocation-" + modelData.id; x: root.locationX(parent.width); width: root.locationWidth; anchors.verticalCenter: parent.verticalCenter; text: modelData.location || "—"; color: Theme.inkMuted; font.pixelSize: root.tableTextSize - 1; wrapMode: Text.WordWrap; maximumLineCount: 2; elide: Text.ElideRight; lineHeight: 1.2 }
                                        Text {
                                            objectName: "offerIa-" + modelData.id
                                            visible: !root.isNewOffersFilter
                                            x: root.llmScoreX(parent.width)
                                            width: root.llmScoreWidth
                                            anchors.verticalCenter: parent.verticalCenter
                                            horizontalAlignment: Text.AlignHCenter
                                            text: (modelData.llm_score_text || "—")
                                                + (modelData.llm_score !== null && modelData.llm_score !== undefined && !modelData.fit_current ? " *" : "")
                                            Accessible.name: "Score IA " + (modelData.llm_score_text || "indisponible")
                                                + ". " + (modelData.fit_status_label || "")
                                            ToolTip.visible: scoreHover.hovered
                                            ToolTip.text: (modelData.fit_status_label || "")
                                            HoverHandler { id: scoreHover }
                                            color: modelData.llm_score === null || modelData.llm_score === undefined ? Theme.inkFaint : Theme.inkSoft
                                            font.pixelSize: root.tableTextSize - 1
                                            font.weight: Font.DemiBold
                                        }
                                        Text {
                                            objectName: "offerPublishedDate-" + modelData.id
                                            visible: root.isNewOffersFilter
                                            x: root.publishedDateX(parent.width)
                                            width: root.publishedDateWidth
                                            anchors.verticalCenter: parent.verticalCenter
                                            horizontalAlignment: Text.AlignHCenter
                                            text: modelData.published_date || "—"
                                            color: modelData.published_date && modelData.published_date !== "—" ? Theme.inkMuted : Theme.inkFaint
                                            font.pixelSize: root.tableTextSize - 1
                                        }
                                        Text {
                                            objectName: "offerLifecycleDate-" + modelData.id
                                            visible: root.showsLifecycleDate
                                            x: root.lifecycleDateX(parent.width)
                                            width: root.operationDateWidth
                                            anchors.verticalCenter: parent.verticalCenter
                                            horizontalAlignment: Text.AlignHCenter
                                            text: modelData.operation_date || "—"
                                            color: text === "—" ? Theme.inkFaint : Theme.inkMuted
                                            font.pixelSize: root.tableTextSize - 1
                                        }
                                        Text {
                                            objectName: "offerArchiveDate-" + modelData.id
                                            visible: root.showsArchiveDate
                                            x: root.archiveDateX(parent.width)
                                            width: root.archiveDateWidth
                                            anchors.verticalCenter: parent.verticalCenter
                                            horizontalAlignment: Text.AlignHCenter
                                            text: modelData.archived_at || "—"
                                            color: text === "—" ? Theme.inkFaint : Theme.inkMuted
                                            font.pixelSize: root.tableTextSize - 1
                                        }
                                        Text {
                                            objectName: "offerFilter-" + modelData.id
                                            visible: root.isNewOffersFilter
                                            x: root.filterDispositionX(parent.width)
                                            width: root.filterDispositionWidth
                                            anchors.verticalCenter: parent.verticalCenter
                                            horizontalAlignment: Text.AlignHCenter
                                            text: modelData.filter_disposition_label || "À classer"
                                            color: modelData.filter_disposition === "pending_relevance" ? Theme.accentBright : Theme.inkFaint
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
                                objectName: "offerListLoadingOverlay"
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
            RowLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: 34
                spacing: 8
                AppButton {
                    objectName: "offerSelectPageAction"
                    text: root.allVisibleSelected ? "Retirer cette page" : "Sélectionner cette page"
                    refined: true
                    fontPixelSize: 11
                    implicitHeight: 30
                    enabled: root.sortedJobs.length > 0 && !AppBridge.jobsLoading
                    onClicked: root.selectAllVisible()
                }
                Text {
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignHCenter
                    text: AppBridge.jobsTotal > 0
                        ? (AppBridge.jobsPage * AppBridge.jobsPageSize + 1) + "–"
                            + Math.min(AppBridge.jobsTotal, (AppBridge.jobsPage + 1) * AppBridge.jobsPageSize)
                            + " / " + AppBridge.jobsTotal
                        : "0 offre"
                    color: Theme.inkMuted
                    font.pixelSize: 11
                }
                AppButton {
                    objectName: "offerPreviousPageAction"
                    text: "‹"
                    refined: true
                    implicitWidth: 32
                    implicitHeight: 30
                    enabled: AppBridge.jobsPage > 0 && !AppBridge.jobsLoading
                    onClicked: root.loadOffers(AppBridge.jobsPage - 1)
                }
                Text {
                    text: (AppBridge.jobsPage + 1) + " / "
                        + Math.max(1, Math.ceil(AppBridge.jobsTotal / AppBridge.jobsPageSize))
                    color: Theme.inkSoft
                    font.pixelSize: 11
                }
                AppButton {
                    objectName: "offerNextPageAction"
                    text: "›"
                    refined: true
                    implicitWidth: 32
                    implicitHeight: 30
                    enabled: (AppBridge.jobsPage + 1) * AppBridge.jobsPageSize < AppBridge.jobsTotal
                        && !AppBridge.jobsLoading
                    onClicked: root.loadOffers(AppBridge.jobsPage + 1)
                }
            }
        }

        Rectangle {
            objectName: "offerWorkspaceDivider"
            Layout.preferredWidth: 1
            Layout.fillHeight: true
            Layout.topMargin: 8
            Layout.bottomMargin: 8
            color: Theme.line
        }

        Item {
                Layout.fillWidth: true
                Layout.preferredWidth: 1
                Layout.minimumWidth: 500
                Layout.fillHeight: true
                Layout.leftMargin: 14
                Item {
                    anchors.fill: parent
                    EmptyState {
                        visible: !AppBridge.currentJob.id || !root.detailMatchesSelection
                        anchors.centerIn: parent
                        iconSource: Theme.icon("briefcase")
                        title: root.selectedId ? "Offre indisponible" : "Aucune offre sélectionnée"
                    }
                    ColumnLayout {
                        objectName: "offerDetailContent"
                        visible: Boolean(AppBridge.currentJob.id) && root.detailMatchesSelection
                        enabled: !AppBridge.jobDetailLoading && root.detailMatchesSelection
                        anchors.fill: parent
                        spacing: 12
                        Flickable {
                            id: detailViewport
                            objectName: "offerDetailViewport"
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
                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 3
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
                                        Text {
                                            objectName: "offerDetailArchiveSubLabel"
                                            visible: AppBridge.currentJob.status === "archived"
                                                && Boolean(AppBridge.currentJob.archive_sub_label)
                                            text: "Archivée · " + (AppBridge.currentJob.archive_sub_label || "")
                                            color: Theme.warning
                                            font.pixelSize: root.detailMetaFontSize
                                            font.weight: Font.DemiBold
                                        }
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
                                Text {
                                    Layout.fillWidth: true
                                    visible: !!AppBridge.currentJob.source_company
                                        && AppBridge.currentJob.source_company !== AppBridge.currentJob.company
                                    text: "Nom de l’annonce : " + (AppBridge.currentJob.source_company || "")
                                    color: Theme.inkMuted
                                    font.pixelSize: root.detailMetaFontSize - 1
                                    elide: Text.ElideRight
                                }
                                Rectangle {
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 1
                                    Layout.bottomMargin: root.hasInsightCards ? 8 : 0
                                    color: Theme.line
                                }
                                OfferAnalysisSummary {
                                    objectName: "offerAnalysisSummary"
                                    visible: root.hasAnalysis
                                    Layout.fillWidth: true
                                    offer: AppBridge.currentJob
                                    fontPixelSize: root.detailMetaFontSize
                                    labelFontPixelSize: root.detailMetaFontSize
                                }
                                Rectangle {
                                    objectName: "offerClassificationCard"
                                    visible: root.hasClassificationInsight
                                    Layout.fillWidth: true
                                    implicitHeight: classificationContent.implicitHeight + 24
                                    radius: 16
                                    color: Theme.surfaceMuted
                                    border.color: Theme.line
                                    ColumnLayout {
                                        id: classificationContent
                                        anchors.fill: parent
                                        anchors.margins: 12
                                        spacing: 8
                                        Text {
                                            Layout.fillWidth: true
                                            text: "Motif de la classification"
                                            color: Theme.ink
                                            font.pixelSize: root.detailMetaFontSize
                                            font.weight: Font.Bold
                                        }
                                        Text {
                                            objectName: "offerClassificationSummary"
                                            Layout.fillWidth: true
                                            text: AppBridge.currentJob.classification_summary || ""
                                            textFormat: Text.PlainText
                                            color: Theme.inkSoft
                                            font.pixelSize: root.detailMetaFontSize
                                            wrapMode: Text.WordWrap
                                        }
                                        Text {
                                            objectName: "offerClassificationReasons"
                                            visible: (AppBridge.currentJob.classification_reasons || []).length > 0
                                            Layout.fillWidth: true
                                            text: "• " + (AppBridge.currentJob.classification_reasons || []).join("\n• ")
                                            textFormat: Text.PlainText
                                            color: Theme.inkMuted
                                            font.pixelSize: root.detailSmallFontSize
                                            wrapMode: Text.WordWrap
                                        }
                                    }
                                }
                                Rectangle {
                                    objectName: "documentReviewCard"
                                    visible: root.documentReviewPointCount > 0
                                    Layout.fillWidth: true
                                    implicitHeight: documentReviewContent.implicitHeight + 28
                                    radius: 16
                                    color: Theme.surfaceMuted
                                    border.color: Theme.warningLine
                                    Rectangle {
                                        anchors.left: parent.left
                                        anchors.right: parent.right
                                        anchors.top: parent.top
                                        anchors.leftMargin: 16
                                        anchors.rightMargin: 16
                                        height: 1
                                        color: Theme.highlight
                                    }
                                    ColumnLayout {
                                        id: documentReviewContent
                                        anchors.fill: parent
                                        anchors.margins: 14
                                        spacing: 11
                                        RowLayout {
                                            Layout.fillWidth: true
                                            spacing: 8
                                            Rectangle {
                                                Layout.preferredWidth: 7
                                                Layout.preferredHeight: 7
                                                radius: 4
                                                color: Theme.warning
                                            }
                                            Text {
                                                objectName: "documentReviewCardTitle"
                                                Layout.fillWidth: true
                                                text: "CV et lettre"
                                                color: Theme.ink
                                                font.pixelSize: root.detailMetaFontSize
                                                font.weight: Font.Bold
                                                elide: Text.ElideRight
                                            }
                                            Text {
                                                objectName: "documentReviewCardCount"
                                                text: root.documentReviewPointCount
                                                    + (root.documentReviewPointCount === 1
                                                        ? " point à vérifier" : " points à vérifier")
                                                color: Theme.warning
                                                font.pixelSize: root.detailSmallFontSize
                                                font.weight: Font.DemiBold
                                            }
                                        }
                                        Repeater {
                                            model: root.documentReviewPoints
                                            delegate: RowLayout {
                                                required property var modelData
                                                Layout.fillWidth: true
                                                Layout.alignment: Qt.AlignTop
                                                spacing: 9
                                                Rectangle {
                                                    Layout.preferredWidth: 3
                                                    Layout.preferredHeight: 18
                                                    Layout.alignment: Qt.AlignTop
                                                    radius: 2
                                                    color: Theme.warning
                                                    opacity: 0.7
                                                }
                                                ColumnLayout {
                                                    Layout.fillWidth: true
                                                    spacing: 2
                                                    Text {
                                                        objectName: "documentReviewPoint_" + modelData.key
                                                        Layout.fillWidth: true
                                                        text: modelData.label
                                                        textFormat: Text.PlainText
                                                        color: Theme.inkSoft
                                                        font.pixelSize: root.detailMetaFontSize
                                                        font.weight: Font.DemiBold
                                                        wrapMode: Text.WordWrap
                                                    }
                                                    Text {
                                                        objectName: "documentReviewDetail_" + modelData.key
                                                        visible: Boolean(modelData.detail)
                                                        Layout.fillWidth: true
                                                        text: modelData.detail || ""
                                                        textFormat: Text.PlainText
                                                        color: Theme.inkMuted
                                                        font.pixelSize: root.detailSmallFontSize
                                                        wrapMode: Text.WordWrap
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                                Rectangle {
                                    objectName: "offerMatchCard"
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
                                            objectName: "offerMatchPoints"
                                            model: root.matchPointsExpanded
                                                ? root.matchInsightPoints
                                                : root.matchInsightPoints.slice(0, 3)
                                            delegate: RowLayout {
                                                required property var modelData
                                                Layout.fillWidth: true
                                                spacing: 8
                                                Rectangle { Layout.preferredWidth: 5; Layout.preferredHeight: 5; radius: 3; color: Theme.success }
                                                Text {
                                                    objectName: "offerMatchPointText"
                                                    Layout.fillWidth: true
                                                    text: modelData
                                                    textFormat: Text.PlainText
                                                    color: Theme.ink
                                                    font.pixelSize: root.detailMetaFontSize
                                                    wrapMode: Text.WordWrap
                                                    lineHeight: 1.25
                                                    maximumLineCount: root.matchPointsExpanded ? 999 : 2
                                                    elide: root.matchPointsExpanded ? Text.ElideNone : Text.ElideRight
                                                }
                                            }
                                        }
                                        Text {
                                            objectName: "offerMatchPointsEmpty"
                                            visible: root.matchInsightPoints.length === 0
                                            Layout.fillWidth: true
                                            text: "Aucun point de correspondance renseigné dans cette analyse."
                                            textFormat: Text.PlainText
                                            color: Theme.inkMuted
                                            font.pixelSize: root.detailMetaFontSize
                                            wrapMode: Text.WordWrap
                                        }
                                        AppButton {
                                            objectName: "offerMatchPointsToggle"
                                            visible: root.matchInsightPoints.length > 3
                                            text: root.matchPointsExpanded ? "Réduire"
                                                : "Voir les " + (root.matchInsightPoints.length - 3) + " autres points"
                                            quiet: true
                                            refined: true
                                            fontPixelSize: root.detailSmallFontSize
                                            onClicked: root.matchPointsExpanded = !root.matchPointsExpanded
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
                                            objectName: "offerRiskPoints"
                                            model: root.riskPointsExpanded
                                                ? root.orderedRiskPoints : root.orderedRiskPoints.slice(0, 4)
                                            delegate: RowLayout {
                                                required property var modelData
                                                Layout.fillWidth: true
                                                spacing: 8
                                                Rectangle { Layout.preferredWidth: 5; Layout.preferredHeight: 5; radius: 3; color: Theme.warning }
                                                Text {
                                                    objectName: "offerRiskPointText"
                                                    Layout.fillWidth: true
                                                    text: modelData
                                                    textFormat: Text.PlainText
                                                    color: Theme.ink
                                                    font.pixelSize: root.detailMetaFontSize
                                                    wrapMode: Text.WordWrap
                                                    lineHeight: 1.25
                                                    maximumLineCount: root.riskPointsExpanded ? 999 : 2
                                                    elide: root.riskPointsExpanded ? Text.ElideNone : Text.ElideRight
                                                }
                                            }
                                        }
                                        AppButton {
                                            objectName: "offerRiskPointsToggle"
                                            visible: root.orderedRiskPoints.length > 4
                                            text: root.riskPointsExpanded ? "Réduire"
                                                : "Voir les " + (root.orderedRiskPoints.length - 4) + " autres points"
                                            quiet: true
                                            refined: true
                                            fontPixelSize: root.detailSmallFontSize
                                            onClicked: root.riskPointsExpanded = !root.riskPointsExpanded
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
                                                enabled: !AppBridge.shutdownRequested && !root.selectedJobBusy && !root.duplicateCandidateBusy
                                                onClicked: AppBridge.resolveDuplicate(AppBridge.currentJob.id, true)
                                            }
                                            AppButton {
                                                Layout.fillWidth: true
                                                text: "Offres différentes"
                                                fontPixelSize: root.detailButtonFontSize
                                                refined: true
                                                implicitHeight: 36
                                                enabled: !AppBridge.shutdownRequested && !root.selectedJobBusy && !root.duplicateCandidateBusy
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
                                    objectName: "offerRawDescriptionHeading"
                                    visible: !root.hasAnalysis
                                    Layout.fillWidth: true
                                    Layout.topMargin: root.hasInsightCards ? 18 : 4
                                    spacing: 8
                                    Rectangle { Layout.preferredWidth: 3; Layout.preferredHeight: 13; radius: 2; color: Theme.accent }
                                    FormLabel { text: "DESCRIPTION DU POSTE"; fontPixelSize: root.detailMetaFontSize }
                                    Item { Layout.fillWidth: true }
                                    Text { Layout.maximumWidth: detailContent.width * 0.42; text: AppBridge.currentJob.role_type || AppBridge.currentJob.domain || ""; color: Theme.inkFaint; font.pixelSize: root.detailSmallFontSize; elide: Text.ElideRight }
                                }
                                OfferDescription {
                                    objectName: "offerRawDescription"
                                    visible: !root.hasAnalysis
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: implicitHeight
                                    description: AppBridge.currentJob.description || ""
                                    fontPixelSize: root.detailBodyFontSize
                                }
                                AppTextArea {
                                    id: applicationNotes
                                    objectName: "applicationNotes"
                                    visible: Boolean(root.applicationData.id)
                                    enabled: !AppBridge.shutdownRequested
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 72
                                    fontPixelSize: root.detailBodyFontSize
                                    placeholderText: "Ajouter une note de suivi…"
                                    onTextChanged: root.editNote(text)
                                    onActiveFocusChanged: if (!activeFocus) root.flushNotes()
                                }
                                RowLayout {
                                    visible: Boolean(root.notesApplicationId)
                                    Layout.fillWidth: true
                                    Text {
                                        objectName: "applicationNotesSaveState"
                                        Layout.fillWidth: true
                                        text: root.activeNoteDraft.error ? "Échec de l’enregistrement ; note conservée."
                                            : root.activeNoteDraft.revision > root.activeNoteDraft.savedRevision ? "Enregistrement…" : "Enregistré"
                                        color: root.activeNoteDraft.error ? Theme.warning : Theme.inkFaint
                                        font.pixelSize: root.detailSmallFontSize
                                        wrapMode: Text.WordWrap
                                    }
                                    AppButton {
                                        objectName: "applicationNotesRetry"
                                        visible: Boolean(root.activeNoteDraft.error)
                                        text: "Réessayer"
                                        quiet: true
                                        onClicked: { root.flushNotes(); AppBridge.retryNotesSave() }
                                    }
                                }
                                Item { Layout.preferredHeight: 1 }
                            }
                        }
                        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 6
                            Text {
                                visible: root.selectedJobBusy
                                Layout.fillWidth: true
                                text: "Cette offre est en cours de traitement. Les actions incompatibles attendent sa fin."
                                color: Theme.warning
                                font.pixelSize: root.detailSmallFontSize
                                wrapMode: Text.WordWrap
                            }
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
                                    enabled: !AppBridge.shutdownRequested
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
                                    objectName: "offerRegenerateAction"
                                    visible: AppBridge.currentJob.status === "ready_for_form_submission"
                                    Layout.minimumWidth: root.detailActionButtonWidth
                                    Layout.preferredWidth: root.detailActionButtonWidth
                                    Layout.maximumWidth: root.detailActionButtonWidth
                                    text: "Régénérer"
                                    iconSource: Theme.icon("refresh")
                                    kind: "primary"
                                    fontPixelSize: root.detailButtonFontSize
                                    refined: true
                                    iconSize: 13
                                    enabled: !AppBridge.shutdownRequested
                                    implicitHeight: root.detailActionButtonHeight
                                    onClicked: AppBridge.regenerateApplication(AppBridge.currentJob.id)
                                    ToolTip.visible: hovered
                                    ToolTip.text: "Régénère le CV et la lettre dans le même dossier et remplace les fichiers actuels."
                                }
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
                                    enabled: !AppBridge.shutdownRequested && !root.selectedJobBusy
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
                                    objectName: "offerArchiveApplicationAction"
                                    enabled: !AppBridge.shutdownRequested && !root.selectedJobBusy
                                    onClicked: AppBridge.archiveApplication(Number(root.applicationData.id))
                                }
                                AppButton {
                                    objectName: "offerDeleteAction"
                                    visible: AppBridge.currentJob.status === "ready_for_form_submission"
                                    Layout.minimumWidth: root.detailActionButtonWidth
                                    Layout.preferredWidth: root.detailActionButtonWidth
                                    Layout.maximumWidth: root.detailActionButtonWidth
                                    text: "Supprimer"
                                    iconSource: Theme.icon("trash")
                                    kind: "danger"
                                    fontPixelSize: root.detailButtonFontSize
                                    refined: true
                                    iconSize: 13
                                    implicitHeight: root.detailActionButtonHeight
                                    enabled: !AppBridge.shutdownRequested && !root.selectedJobBusy
                                    onClicked: {
                                        deleteOfferDialog.targetJobId = Number(AppBridge.currentJob.id)
                                        deleteOfferDialog.targetTitle = String(AppBridge.currentJob.title || "")
                                        deleteOfferDialog.targetCompany = String(AppBridge.currentJob.company || "")
                                        deleteOfferDialog.open()
                                    }
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
                                visible: (!Boolean(root.applicationData.id)
                                    || root.isGenerationResumable(AppBridge.currentJob.id))
                                    && AppBridge.currentJob.status !== "archived"
                                Layout.fillWidth: true
                                spacing: 6
                                AppButton {
                                    objectName: "offerPrimaryAction"
                                    Layout.preferredWidth: Math.max(148, implicitWidth)
                                    text: AppBridge.currentJob.status === "duplicate_review" ? "Décision requise"
                                        : AppBridge.currentJob.related_application_id ? "Voir le dossier #" + AppBridge.currentJob.related_application_id
                                        : root.isGenerationResumable(AppBridge.currentJob.id) ? "Reprendre"
                                        : Boolean(AppBridge.currentJob.can_generate) ? "Créer"
                                        : "Ouvrir l’analyse"
                                    iconSource: Theme.icon("sparkle")
                                    kind: "primary"
                                    fontPixelSize: root.detailButtonFontSize
                                    refined: true
                                    implicitHeight: 34
                                    enabled: !AppBridge.shutdownRequested && AppBridge.currentJob.status !== "duplicate_review"
                                        && (!root.canGenerateOffer(AppBridge.currentJob)
                                            || root.canStartGeneration([Number(AppBridge.currentJob.id)]))
                                    onClicked: {
                                        if (AppBridge.currentJob.status === "duplicate_review") return
                                        if (AppBridge.currentJob.related_application_id) AppBridge.openApplication(AppBridge.currentJob.related_application_id)
                                        else if (root.canGenerateOffer(AppBridge.currentJob)) root.chooseGenerationMode([Number(AppBridge.currentJob.id)], false, 0)
                                        else root.navigateRequested("analysis")
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
                                    property string documentPath: root.applicationData.status === "ready_for_form_submission"
                                                                  ? (root.applicationData.cv_html_path || "")
                                                                  : (root.applicationData.cv_pdf_path || "")
                                    text: "CV"
                                    iconSource: Theme.icon("document")
                                    fontPixelSize: root.detailButtonFontSize
                                    refined: true
                                    quiet: true
                                    iconSize: 13
                                    implicitHeight: root.detailActionButtonHeight
                                    enabled: Boolean(documentPath)
                                    onClicked: AppBridge.openPath(documentPath)
                                }
                                AppButton {
                                    visible: Boolean(root.applicationData.id)
                                    Layout.minimumWidth: root.detailActionButtonWidth
                                    Layout.preferredWidth: root.detailActionButtonWidth
                                    Layout.maximumWidth: root.detailActionButtonWidth
                                    property string documentPath: root.applicationData.status === "ready_for_form_submission"
                                                                  ? (root.applicationData.letter_html_path || "")
                                                                  : (root.applicationData.letter_pdf_path || "")
                                    text: "Lettre"
                                    iconSource: Theme.icon("document")
                                    fontPixelSize: root.detailButtonFontSize
                                    refined: true
                                    quiet: true
                                    iconSize: 13
                                    implicitHeight: root.detailActionButtonHeight
                                    enabled: Boolean(documentPath)
                                    onClicked: AppBridge.openPath(documentPath)
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
                                    text: Boolean(AppBridge.currentJob.shortlisted) || Boolean(AppBridge.currentJob.shortlist_pending) ? "Retirer" : "Ajouter"
                                    iconSource: Boolean(AppBridge.currentJob.shortlisted) || Boolean(AppBridge.currentJob.shortlist_pending) ? Theme.icon("x") : Theme.icon("plus")
                                    fontPixelSize: root.detailButtonFontSize
                                    refined: true
                                    implicitHeight: 32
                                    objectName: "offerShortlistAction"
                                    enabled: !AppBridge.shutdownRequested && (!root.selectedJobBusy || Boolean(AppBridge.currentJob.shortlist_pending))
                                    onClicked: AppBridge.setJobShortlisted(AppBridge.currentJob.id, !(Boolean(AppBridge.currentJob.shortlisted) || Boolean(AppBridge.currentJob.shortlist_pending)))
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
                                    objectName: "offerArchiveJobAction"
                                    enabled: !AppBridge.shutdownRequested && !root.selectedJobBusy
                                    onClicked: AppBridge.archiveJob(AppBridge.currentJob.id)
                                }
                                Item { Layout.fillWidth: true }
                                Rectangle {
                                    visible: root.sortedJobs.length > 1
                                    Layout.preferredWidth: visible ? 120 : 0
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
                                            iconSource: Theme.icon("chevron-left")
                                            refined: true
                                            quiet: true
                                            iconSize: 11
                                            enabled: root.canGoPrevious && !AppBridge.shutdownRequested
                                            objectName: "offerPreviousAction"
                                            Accessible.name: "Offre précédente"
                                            onClicked: root.moveSelection(-1)
                                        }
                                        Text {
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
                                            Layout.preferredWidth: 28
                                            Layout.preferredHeight: 28
                                            text: ""
                                            iconSource: Theme.icon("chevron-right")
                                            refined: true
                                            quiet: true
                                            iconSize: 11
                                            enabled: root.canGoNext && !AppBridge.shutdownRequested
                                            objectName: "offerNextAction"
                                            Accessible.name: "Offre suivante"
                                            onClicked: root.moveSelection(1)
                                        }
                                    }
                                }
                            }
                        }
                    }
                    Rectangle {
                        objectName: "offerDetailLoadingOverlay"
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

    Item {
        objectName: "offerFilterControlsBand"
        visible: !root.isNewOffersFilter
        x: offersColumn.parent.x + offersColumn.x
        width: offersColumn.width
        anchors.top: parent.top
        anchors.topMargin: root.topInset > 0 ? 3 : 0
        height: 28
        z: 1
        RowLayout {
            anchors.fill: parent
            spacing: 8
            RowLayout {
                visible: ["analyzed", "ready_for_form_submission", "sent"].indexOf(root.filterStatus) >= 0
                spacing: 7
                Text {
                    text: "IA min."
                    color: Theme.inkMuted
                    font.pixelSize: 11
                    verticalAlignment: Text.AlignVCenter
                }
                Slider {
                    id: minimumScoreSlider
                    objectName: "offerMinimumLlmScoreSlider"
                    Layout.preferredWidth: 150
                    Layout.preferredHeight: 24
                    from: 0
                    to: 100
                    stepSize: 1
                    snapMode: Slider.SnapAlways
                    value: Math.max(0, AppBridge.minimumLlmScorePercent)
                    leftPadding: 6
                    rightPadding: 6
                    Accessible.name: "Score IA minimum"
                    ToolTip.visible: hovered
                    ToolTip.text: "Le seuil porte sur le score IA global actuel. Les évaluations historiques restent visibles pour réévaluation."
                    Accessible.description: AppBridge.minimumLlmScorePercent < 0
                        ? "Aucun seuil" : Math.round(value) + " pour cent"
                    onMoved: {
                        if (!pressed) root.setMinLlmScorePercent(Math.round(value))
                    }
                    onPressedChanged: {
                        if (!pressed) root.setMinLlmScorePercent(Math.round(value))
                    }
                    background: Rectangle {
                        x: minimumScoreSlider.leftPadding
                        y: (minimumScoreSlider.height - height) / 2
                        width: minimumScoreSlider.availableWidth
                        height: 3
                        radius: 2
                        color: Theme.lineStrong
                        Rectangle {
                            width: parent.width * minimumScoreSlider.visualPosition
                            height: parent.height
                            radius: parent.radius
                            color: Theme.accentBright
                        }
                    }
                    handle: Rectangle {
                        x: minimumScoreSlider.leftPadding
                            + minimumScoreSlider.visualPosition * (minimumScoreSlider.availableWidth - width)
                        y: (minimumScoreSlider.height - height) / 2
                        width: 12
                        height: 12
                        radius: 6
                        color: minimumScoreSlider.pressed ? Theme.accentBright : Theme.inkSoft
                        border.color: Theme.accentBright
                        border.width: 1
                    }
                }
                Text {
                    Layout.preferredWidth: 44
                    text: AppBridge.minimumLlmScorePercent < 0 && !minimumScoreSlider.pressed
                        ? "Aucun" : Math.round(minimumScoreSlider.value) + " %"
                    color: AppBridge.minimumLlmScorePercent < 0 ? Theme.inkMuted : Theme.inkSoft
                    font.pixelSize: 11
                    font.weight: Font.DemiBold
                }
                AppButton {
                    objectName: "offerMinimumLlmScoreClear"
                    visible: AppBridge.minimumLlmScorePercent >= 0
                    Layout.preferredWidth: visible ? 22 : 0
                    Layout.preferredHeight: 22
                    text: ""
                    iconSource: Theme.icon("x")
                    iconSize: 11
                    refined: true
                    quiet: true
                    Accessible.name: "Supprimer le score IA minimum"
                    onClicked: root.setMinLlmScorePercent(-1)
                }
            }
            Item { Layout.fillWidth: true }
        }
    }

    GenerationTracking {
        id: generationTracking
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: 10
        height: implicitHeight
        visible: !root.isNewOffersFilter && (runs.length > 0 || Boolean(selectedRun.id)
            || commandRunning || error.length > 0)
        workspace: root.generationWorkspace
        loading: AppBridge.generationWorkspaceLoading
        operation: AppBridge.generationWorkspaceOperation || ""
        error: AppBridge.generationWorkspaceError || ""
        controlsEnabled: !AppBridge.shutdownRequested
        onRefreshRequested: AppBridge.refreshGenerationWorkspace()
        onStopRequested: AppBridge.cancelImmediateGeneration()
        onRetryRequested: function(runId) { AppBridge.retryGenerationRun(runId) }
        onCancelRequested: function(runId) { AppBridge.cancelGenerationRun(runId) }
        onAbandonRequested: function(runId) { AppBridge.abandonGenerationRun(runId) }
        onHistoryPageRequested: function(page) { AppBridge.setGenerationHistoryPage(page) }
        onRunSelected: function(runId) { AppBridge.selectGenerationRun(runId) }
    }

    AnalysisPage {
        anchors.fill: parent
        topInset: root.topInset
        visible: root.isNewOffersFilter
        onFilterChanged: function(status) {
            root.filterStatus = status
            root.useDefaultSortForStatus(status)
            root.filterChanged(status)
            root.clearSelection()
            root.pageResetRequested = false
            if (status !== "scraped") {
                AppBridge.selectJob(0)
                root.loadOffers(0)
            }
        }
    }
}
