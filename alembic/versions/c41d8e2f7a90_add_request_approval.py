"""add request approval

Revision ID: c41d8e2f7a90
Revises: 095bcaddc575
Create Date: 2026-10-30 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c41d8e2f7a90"
down_revision: Union[str, None] = "095bcaddc575"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_EVENTS_OLD = (
    "on_new_request",
    "on_grabbed",
    "on_successful_download",
    "on_failed_download",
)
_EVENTS_NEW = (*_EVENTS_OLD, "on_request_approved", "on_request_declined")


def upgrade() -> None:
    # existing requests count as not yet approved, which only matters for
    # requests from untrusted users
    for table in ("audiobookrequest", "manualbookrequest"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "approved", sa.Boolean(), nullable=False, server_default=sa.false()
                )
            )

    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE eventenum ADD VALUE IF NOT EXISTS 'on_request_approved'")
        op.execute("ALTER TYPE eventenum ADD VALUE IF NOT EXISTS 'on_request_declined'")
    else:
        with op.batch_alter_table("notification", schema=None) as batch_op:
            batch_op.alter_column(
                "event",
                existing_type=sa.Enum(*_EVENTS_OLD, name="eventenum"),
                type_=sa.Enum(*_EVENTS_NEW, name="eventenum"),
                existing_nullable=False,
            )


def downgrade() -> None:
    # neither event has an older equivalent to fall back to
    op.execute(
        "DELETE FROM notification "
        "WHERE event IN ('on_request_approved', 'on_request_declined')"
    )
    if op.get_bind().dialect.name == "postgresql":
        # postgres cannot drop a value from an enum, so rebuild the type
        op.execute("ALTER TYPE eventenum RENAME TO eventenum_old")
        sa.Enum(*_EVENTS_OLD, name="eventenum").create(op.get_bind(), checkfirst=False)
        op.execute(
            "ALTER TABLE notification ALTER COLUMN event "
            "TYPE eventenum USING event::text::eventenum"
        )
        op.execute("DROP TYPE eventenum_old")
    else:
        with op.batch_alter_table("notification", schema=None) as batch_op:
            batch_op.alter_column(
                "event",
                existing_type=sa.Enum(*_EVENTS_NEW, name="eventenum"),
                type_=sa.Enum(*_EVENTS_OLD, name="eventenum"),
                existing_nullable=False,
            )

    for table in ("manualbookrequest", "audiobookrequest"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("approved")
