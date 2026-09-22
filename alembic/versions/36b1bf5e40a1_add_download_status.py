"""add download status

Revision ID: 36b1bf5e40a1
Revises: a1c4f7b2e903
Create Date: 2026-09-22 21:40:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "36b1bf5e40a1"
down_revision: Union[str, None] = "a1c4f7b2e903"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_EVENTS_OLD = ("on_new_request", "on_successful_download", "on_failed_download")
_EVENTS_NEW = (
    "on_new_request",
    "on_grabbed",
    "on_successful_download",
    "on_failed_download",
)

_download_status = sa.Enum("grabbed", "downloaded", "failed", name="downloadstatusenum")


def upgrade() -> None:
    # sa.Enum only emits CREATE TYPE by itself for create_table, not add_column
    _download_status.create(op.get_bind(), checkfirst=True)

    for table in ("audiobook", "manualbookrequest"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("download_status", _download_status, nullable=True)
            )
            batch_op.create_index(
                f"ix_{table}_download_status", ["download_status"], unique=False
            )

    # notifications can now fire on the grab as well as on the files landing
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE eventenum ADD VALUE IF NOT EXISTS 'on_grabbed'")
    else:
        with op.batch_alter_table("notification", schema=None) as batch_op:
            batch_op.alter_column(
                "event",
                existing_type=sa.Enum(*_EVENTS_OLD, name="eventenum"),
                type_=sa.Enum(*_EVENTS_NEW, name="eventenum"),
                existing_nullable=False,
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # postgres cannot drop a value from an enum, so rebuild the type.
        # any notification using the removed event falls back to the closest
        # surviving one rather than being deleted.
        op.execute(
            "UPDATE notification SET event = 'on_successful_download' "
            "WHERE event = 'on_grabbed'"
        )
        op.execute("ALTER TYPE eventenum RENAME TO eventenum_old")
        sa.Enum(*_EVENTS_OLD, name="eventenum").create(op.get_bind(), checkfirst=False)
        op.execute(
            "ALTER TABLE notification ALTER COLUMN event "
            "TYPE eventenum USING event::text::eventenum"
        )
        op.execute("DROP TYPE eventenum_old")
    else:
        op.execute(
            "UPDATE notification SET event = 'on_successful_download' "
            "WHERE event = 'on_grabbed'"
        )
        with op.batch_alter_table("notification", schema=None) as batch_op:
            batch_op.alter_column(
                "event",
                existing_type=sa.Enum(*_EVENTS_NEW, name="eventenum"),
                type_=sa.Enum(*_EVENTS_OLD, name="eventenum"),
                existing_nullable=False,
            )

    for table in ("manualbookrequest", "audiobook"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_index(f"ix_{table}_download_status")
            batch_op.drop_column("download_status")

    _download_status.drop(op.get_bind(), checkfirst=True)
