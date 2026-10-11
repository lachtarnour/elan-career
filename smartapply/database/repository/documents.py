"""Generated-document repository helpers."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from smartapply.database.models import GeneratedDocument


def add_document(
    session: Session,
    application_id: int,
    doc_type: str,
    **fields: Any,
) -> GeneratedDocument:
    doc = GeneratedDocument(application_id=application_id, doc_type=doc_type, **fields)
    session.add(doc)
    session.flush()
    return doc


def upsert_document(
    session: Session,
    application_id: int,
    doc_type: str,
    **fields: Any,
) -> GeneratedDocument:
    """Create or replace the single current document of a given type."""
    docs = (
        session.execute(
            select(GeneratedDocument)
            .where(GeneratedDocument.application_id == application_id)
            .where(GeneratedDocument.doc_type == doc_type)
            .order_by(GeneratedDocument.id.asc())
        )
        .scalars()
        .all()
    )
    if not docs:
        return add_document(session, application_id, doc_type=doc_type, **fields)
    current = docs[0]
    for key, value in fields.items():
        setattr(current, key, value)
    for stale in docs[1:]:
        session.delete(stale)
    session.flush()
    return current


def delete_document(session: Session, application_id: int, doc_type: str) -> None:
    """Remove every previous version of an artifact absent from the new dossier."""
    session.execute(
        delete(GeneratedDocument).where(
            GeneratedDocument.application_id == application_id,
            GeneratedDocument.doc_type == doc_type,
        )
    )
