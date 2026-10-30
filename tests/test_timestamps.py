"""Update times are written with the same clock as creation times.

SQLite's now() is UTC while the app writes local time, so updates used to land
hours away from when rows were created whenever the server wasn't on UTC.
"""

from datetime import datetime, timedelta

from sqlmodel import Session

from app.internal.models import LibraryImport, LibraryImportStatusEnum


def test_an_update_is_stamped_with_the_apps_clock(session: Session):
    entry = LibraryImport(asin_or_uuid="B1", book_title="t", release_title="r")
    session.add(entry)
    session.commit()

    entry.status = LibraryImportStatusEnum.imported
    session.add(entry)
    session.commit()
    session.refresh(entry)

    assert abs(entry.updated_at - datetime.now()) < timedelta(minutes=1)
    assert entry.updated_at >= entry.created_at
