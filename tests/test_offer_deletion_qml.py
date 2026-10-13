"""The real QML confirmation acts on its captured offer, using an inert bridge."""

import os
import runpy
import subprocess
import sys
from pathlib import Path


def test_ready_offer_deletion_confirmation_and_local_cleanup():
    helpers = runpy.run_path(str(Path(__file__).with_name("test_desktop_qml_responsiveness.py")))
    harness = helpers["_HARNESS"].replace(
        "class Bridge(QObject):",
        """class Bridge(QObject):
    offersDeleted = Signal("QVariantList", "QVariantList")

    @Slot(int)
    def deleteReadyOffer(self, job_id):
        self.deleted.append(job_id)
""",
    )
    scenario = r"""
bridge.deleted = []
host = QQuickWindow()
host.resize(1400, 850)
page.setParentItem(host.contentItem())
host.show()
button = page.findChild(QQuickItem, "offerDeleteAction")
archive = page.findChild(QQuickItem, "offerArchiveApplicationAction")
dialog = page.findChild(QObject, "deleteReadyOfferDialog")
assert button and dialog and button.property("visible") and button.property("enabled")
app.processEvents()
assert button.x() > archive.x()
assert button.x() + button.width() <= button.parentItem().width()
assert QMetaObject.invokeMethod(button, "clicked", Qt.DirectConnection)
wait_until(lambda: dialog.property("visible"))
assert dialog.property("targetJobId") == 42
select(43, 8)
assert dialog.property("targetJobId") == 42
assert QMetaObject.invokeMethod(dialog, "reject", Qt.DirectConnection)
assert bridge.deleted == []
assert QMetaObject.invokeMethod(button, "clicked", Qt.DirectConnection)
wait_until(lambda: dialog.property("visible"))
assert dialog.property("targetJobId") == 43
bridge.protected = [43]
bridge.busyJobIdsChanged.emit()
app.processEvents()
assert dialog.property("confirmEnabled") is False
bridge.protected = []
bridge.busyJobIdsChanged.emit()
app.processEvents()
assert dialog.property("confirmEnabled") is True
assert QMetaObject.invokeMethod(dialog, "accept", Qt.DirectConnection)
assert bridge.deleted == [43]
notes.setProperty("text", "draft to erase")
bridge.offersDeleted.emit([43], [8])
app.processEvents()
assert "8" not in page.property("noteDrafts").toVariant()
bridge.job["status"] = "sent"
bridge.currentJobChanged.emit()
app.processEvents()
assert button.property("visible") is False
host.hide()
page.setParentItem(None)
host.deleteLater()
"""
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "QT_QUICK_BACKEND": "software",
        "QSG_RHI_BACKEND": "software",
        "QT_QUICK_CONTROLS_STYLE": "Basic",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            harness + scenario + helpers["_TEARDOWN"],
            str(Path(__file__).parents[1] / "smartapply/desktop/qml"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_analysis_offer_deletion_requires_confirmation():
    helpers = runpy.run_path(str(Path(__file__).with_name("test_desktop_qml_responsiveness.py")))
    harness = helpers["_HARNESS"].replace(
        "class Bridge(QObject):",
        """class Bridge(QObject):
    @Slot(int)
    def deleteAnalysisOffer(self, job_id):
        self.deleted.append(job_id)
""",
    )
    scenario = r"""
bridge.deleted = []
bridge.protected = []
bridge.analysis_workspace = {"candidates": [
    {"id": 42, "title": "Data Scientist", "company": "Acme", "status": "filtered"},
    {"id": 43, "title": "AI Engineer", "company": "Other", "status": "filtered"},
], "runs": []}
page = load_page("AnalysisPage.qml")
host = QQuickWindow()
host.resize(1400, 850)
page.setParentItem(host.contentItem())
host.show()
bridge.selectAnalysisOffer(42)
app.processEvents()
button = page.findChild(QQuickItem, "analysisDeleteOfferAction")
dialog = page.findChild(QObject, "analysisDeleteConfirm")
assert button and dialog and button.property("visible") and button.property("enabled")
assert QMetaObject.invokeMethod(button, "clicked", Qt.DirectConnection)
wait_until(lambda: dialog.property("visible"))
assert dialog.property("targetJobId") == 42
bridge.selectAnalysisOffer(43)
app.processEvents()
assert dialog.property("targetJobId") == 42
assert QMetaObject.invokeMethod(dialog, "reject", Qt.DirectConnection)
assert bridge.deleted == []
bridge.selectAnalysisOffer(42)
app.processEvents()
assert QMetaObject.invokeMethod(button, "clicked", Qt.DirectConnection)
wait_until(lambda: dialog.property("visible"))
bridge.protected = [42]
bridge.busyJobIdsChanged.emit()
app.processEvents()
assert dialog.property("confirmEnabled") is False
bridge.protected = []
bridge.busyJobIdsChanged.emit()
app.processEvents()
assert dialog.property("confirmEnabled") is True
assert QMetaObject.invokeMethod(dialog, "accept", Qt.DirectConnection)
assert bridge.deleted == [42]
host.hide()
page.setParentItem(None)
host.deleteLater()
"""
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "QT_QUICK_BACKEND": "software",
        "QSG_RHI_BACKEND": "software",
        "QT_QUICK_CONTROLS_STYLE": "Basic",
    }
    result = subprocess.run(
        [sys.executable, "-c", harness + scenario + helpers["_TEARDOWN"],
         str(Path(__file__).parents[1] / "smartapply/desktop/qml")],
        env=env, capture_output=True, text=True, timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr
