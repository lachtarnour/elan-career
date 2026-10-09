import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    objectName: "profilePage"
    signal navigateRequested(string route)

    function preferenceLabel(value) {
        var labels = {remote: "À distance", hybrid: "Hybride", onsite: "Sur site", "Full-time": "Temps plein", "Part-time": "Temps partiel"}
        return labels[value] || value
    }

    function profileDate(value) {
        var date = String(value || "").trim()
        if (/^(present|current|ongoing)$/i.test(date)) return "Aujourd’hui"
        var iso = /^(\d{4})-(\d{2})$/.exec(date)
        return iso ? iso[2] + "/" + iso[1] : date
    }

    function experienceDates(experience) {
        return profileDate(experience.start_date) + " – " + profileDate(experience.end_date)
    }

    Flickable {
        anchors.fill: parent
        contentWidth: width
        contentHeight: content.implicitHeight + 20
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: AppScrollBar { }

        ColumnLayout {
            id: content
            width: Math.min(parent.width - Theme.scrollGutter, Theme.pageContentMaxWidth)
            x: (parent.width - Theme.scrollGutter - width) / 2
            spacing: Theme.pageGap

            PageHeader {
                Layout.fillWidth: true
                title: "Profil"
                AppButton { text: "Ouvrir le dossier"; iconSource: Theme.icon("folder"); onClicked: AppBridge.openProfileFolder() }
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 18
                Layout.rightMargin: 18
                spacing: 18
                Rectangle {
                    Layout.preferredWidth: 58
                    Layout.preferredHeight: 58
                    Layout.alignment: Qt.AlignTop
                    radius: 29
                    color: Theme.accentSoft
                    Text { anchors.centerIn: parent; text: AppBridge.profile.initials || "É"; color: Theme.accentBright; font.pixelSize: 21; font.weight: Font.DemiBold }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 5
                    Text { Layout.fillWidth: true; text: AppBridge.profile.name || "Votre profil"; color: Theme.ink; font.pixelSize: 24; font.weight: Font.Bold; wrapMode: Text.WordWrap }
                    Text { Layout.fillWidth: true; text: [AppBridge.profile.title, AppBridge.profile.location].filter(function(value) { return Boolean(value) }).join(" · "); color: Theme.inkSoft; font.pixelSize: 13; wrapMode: Text.WordWrap }
                    Text { Layout.fillWidth: true; text: AppBridge.profile.email || ""; visible: text.length > 0; color: Theme.accentBright; font.pixelSize: 12; wrapMode: Text.WrapAnywhere }
                    Text {
                        Layout.fillWidth: true
                        Layout.topMargin: 4
                        visible: text.length > 0
                        text: AppBridge.profile.summary || ""
                        color: Theme.inkMuted
                        font.pixelSize: 13
                        lineHeight: 1.3
                        wrapMode: Text.WordWrap
                    }
                }
            }

            Rectangle { Layout.fillWidth: true; Layout.leftMargin: 18; Layout.rightMargin: 18; Layout.preferredHeight: 1; color: Theme.line }

            ColumnLayout {
                objectName: "profilePreferencesSection"
                Layout.fillWidth: true
                Layout.leftMargin: 18
                Layout.rightMargin: 18
                spacing: Theme.sectionGap
                SectionTitle { title: "Préférences de recherche" }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.pageGap
                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                        spacing: 7
                        FormLabel { text: "POSTES VISÉS" }
                        Text {
                            Layout.fillWidth: true
                            text: (AppBridge.profile.target_roles || []).join(" · ") || "Aucun poste renseigné"
                            color: Theme.inkSoft
                            font.pixelSize: 13
                            wrapMode: Text.WordWrap
                        }
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                        spacing: 7
                        FormLabel { text: "CONTRATS ET TÉLÉTRAVAIL" }
                        Text {
                            Layout.fillWidth: true
                            text: (AppBridge.profile.contracts || []).concat(AppBridge.profile.remote_policies || [])
                                .map(function(value) { return root.preferenceLabel(value) }).join(" · ") || "Aucune préférence renseignée"
                            color: Theme.inkSoft
                            font.pixelSize: 13
                            wrapMode: Text.WordWrap
                        }
                    }
                }
            }

            Rectangle { Layout.fillWidth: true; Layout.leftMargin: 18; Layout.rightMargin: 18; Layout.preferredHeight: 1; color: Theme.line }

            ColumnLayout {
                objectName: "profileSkillsSection"
                Layout.fillWidth: true
                Layout.leftMargin: 18
                Layout.rightMargin: 18
                spacing: Theme.sectionGap
                SectionTitle { title: "Compétences" }
                GridLayout {
                    Layout.fillWidth: true
                    columns: 2
                    uniformCellWidths: true
                    columnSpacing: 36
                    rowSpacing: 18
                    Repeater {
                        model: AppBridge.profile.skill_categories || []
                        delegate: ColumnLayout {
                            required property var modelData
                            Layout.fillWidth: true
                            Layout.preferredWidth: 1
                            spacing: 5
                            Text { Layout.fillWidth: true; text: modelData.name; color: Theme.inkSoft; font.pixelSize: 13; font.weight: Font.DemiBold; wrapMode: Text.WordWrap }
                            Text { Layout.fillWidth: true; text: (modelData.skills || []).join(" · "); color: Theme.inkMuted; font.pixelSize: 12; wrapMode: Text.WordWrap }
                        }
                    }
                }
                Text { visible: (AppBridge.profile.skill_categories || []).length === 0; text: "Aucune compétence renseignée"; color: Theme.inkMuted; font.pixelSize: 13 }
            }

            Rectangle { Layout.fillWidth: true; Layout.leftMargin: 18; Layout.rightMargin: 18; Layout.preferredHeight: 1; color: Theme.line }

            RowLayout {
                objectName: "profileEvidenceRow"
                Layout.fillWidth: true
                Layout.leftMargin: 18
                Layout.rightMargin: 18
                spacing: Theme.pageGap

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.preferredWidth: 1
                    Layout.alignment: Qt.AlignTop
                    spacing: Theme.sectionGap
                    SectionTitle { title: "Projets" }
                    Repeater {
                        model: AppBridge.profile.project_items || []
                        delegate: ColumnLayout {
                            required property var modelData
                            required property int index
                            Layout.fillWidth: true
                            spacing: 5
                            Text {
                                objectName: "profileProjectTitle_" + modelData.id
                                Layout.fillWidth: true
                                text: modelData.title
                                color: Theme.ink
                                font.pixelSize: 14
                                font.weight: Font.DemiBold
                                wrapMode: Text.WordWrap
                            }
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.topMargin: 9
                                Layout.preferredHeight: 1
                                visible: index < (AppBridge.profile.project_items || []).length - 1
                                color: Theme.line
                            }
                        }
                    }
                    Text { visible: (AppBridge.profile.project_items || []).length === 0; text: "Aucun projet renseigné"; color: Theme.inkMuted; font.pixelSize: 13 }
                }

                Rectangle { Layout.preferredWidth: 1; Layout.fillHeight: true; Layout.topMargin: 4; Layout.bottomMargin: 4; color: Theme.line }

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.preferredWidth: 1
                    Layout.alignment: Qt.AlignTop
                    spacing: Theme.sectionGap
                    SectionTitle { title: "Expériences" }
                    Repeater {
                        model: AppBridge.profile.experience_items || []
                        delegate: ColumnLayout {
                            required property var modelData
                            required property int index
                            Layout.fillWidth: true
                            spacing: 5
                            Text {
                                objectName: "profileExperienceTitle_" + modelData.id
                                Layout.fillWidth: true
                                text: modelData.title
                                color: Theme.ink
                                font.pixelSize: 14
                                font.weight: Font.DemiBold
                                wrapMode: Text.WordWrap
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 12
                                Text { Layout.fillWidth: true; text: modelData.company; color: Theme.inkSoft; font.pixelSize: 12; wrapMode: Text.WordWrap }
                                Text {
                                    objectName: "profileExperienceDates_" + modelData.id
                                    Layout.alignment: Qt.AlignTop
                                    text: root.experienceDates(modelData)
                                    color: Theme.inkMuted
                                    font.pixelSize: 12
                                }
                            }
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.topMargin: 9
                                Layout.preferredHeight: 1
                                visible: index < (AppBridge.profile.experience_items || []).length - 1
                                color: Theme.line
                            }
                        }
                    }
                    Text { visible: (AppBridge.profile.experience_items || []).length === 0; text: "Aucune expérience renseignée"; color: Theme.inkMuted; font.pixelSize: 13 }
                }
            }
        }
    }
}
