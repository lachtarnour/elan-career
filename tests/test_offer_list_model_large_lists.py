"""Large-list update contracts for the desktop offer model."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QCoreApplication, QPersistentModelIndex
from PySide6.QtTest import QAbstractItemModelTester, QSignalSpy

from smartapply.desktop.models import OfferListModel


@pytest.fixture(scope="module", autouse=True)
def _qt_application():
    application = QCoreApplication.instance() or QCoreApplication([])
    yield application


def _rows(count: int) -> list[dict]:
    return [{"id": index, "title": f"Offer {index}"} for index in range(count)]


def test_large_stable_update_and_insert_preserve_indexes_without_reset() -> None:
    model = OfferListModel()
    tester = QAbstractItemModelTester(
        model,
        QAbstractItemModelTester.FailureReportingMode.Warning,
    )
    rows = _rows(1_000)
    model.replace(rows)
    retained = QPersistentModelIndex(model.index(700))
    resets = QSignalSpy(model.modelReset)
    inserts = QSignalSpy(model.rowsInserted)
    updated = [dict(row) for row in rows]
    updated[700]["title"] = "Updated offer"
    updated.insert(500, {"id": 10_000, "title": "Inserted offer"})

    model.replace(updated)

    assert resets.count() == 0
    assert inserts.count() == 1
    assert retained.isValid()
    assert model.data(retained, model.row_role) == {
        "id": 700,
        "title": "Updated offer",
    }
    assert model.rowCount() == 1_001
    assert tester is not None


def test_large_reorder_uses_one_valid_reset_and_retains_all_contents() -> None:
    model = OfferListModel()
    tester = QAbstractItemModelTester(
        model,
        QAbstractItemModelTester.FailureReportingMode.Warning,
    )
    rows = _rows(1_000)
    model.replace(rows)
    resets = QSignalSpy(model.modelReset)
    moves = QSignalSpy(model.rowsMoved)
    reordered = list(reversed(rows))

    model.replace(reordered)

    assert resets.count() == 1
    assert moves.count() == 0
    assert model.rowCount() == len(reordered)
    assert [
        model.data(model.index(position), model.row_role)
        for position in range(model.rowCount())
    ] == reordered
    assert tester is not None
