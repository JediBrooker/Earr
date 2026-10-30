"""add issues and quality upgrades

Revision ID: f1c4a7e20b38
Revises: e5ebf7234f65
Create Date: 2026-10-30 16:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "f1c4a7e20b38"
down_revision: Union[str, None] = "e5ebf7234f65"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_EVENTS_OLD = (
    "on_new_request",
    "on_grabbed",
    "on_successful_download",
    "on_failed_download",
    "on_request_approved",
    "on_request_declined",
)
_EVENTS_NEW = (*_EVENTS_OLD, "on_issue_reported", "on_issue_resolved")


def upgrade() -> None:
    op.create_table(
        "issue",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("asin", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("book_title", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("user_username", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "wrong_book", "bad_audio", "incomplete", "other", name="issuekindenum"
            ),
            nullable=False,
        ),
        sa.Column("message", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("open", "resolved", name="issuestatusenum"),
            server_default="open",
            nullable=False,
        ),
        sa.Column("resolution", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("resolved_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_username"], ["user.username"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("issue", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_issue_asin"), ["asin"], unique=False)
        batch_op.create_index(batch_op.f("ix_issue_status"), ["status"], unique=False)

    with op.batch_alter_table("audiobook", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "quality_format", sqlmodel.sql.sqltypes.AutoString(), nullable=True
            )
        )
    with op.batch_alter_table("libraryimport", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "replaces", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )

    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE eventenum ADD VALUE IF NOT EXISTS 'on_issue_reported'")
        op.execute("ALTER TYPE eventenum ADD VALUE IF NOT EXISTS 'on_issue_resolved'")
    else:
        with op.batch_alter_table("notification", schema=None) as batch_op:
            batch_op.alter_column(
                "event",
                existing_type=sa.Enum(*_EVENTS_OLD, name="eventenum"),
                type_=sa.Enum(*_EVENTS_NEW, name="eventenum"),
                existing_nullable=False,
            )


def downgrade() -> None:
    op.execute(
        "DELETE FROM notification "
        "WHERE event IN ('on_issue_reported', 'on_issue_resolved')"
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

    with op.batch_alter_table("libraryimport", schema=None) as batch_op:
        batch_op.drop_column("replaces")
    with op.batch_alter_table("audiobook", schema=None) as batch_op:
        batch_op.drop_column("quality_format")
    with op.batch_alter_table("issue", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_issue_status"))
        batch_op.drop_index(batch_op.f("ix_issue_asin"))
    op.drop_table("issue")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS issuestatusenum")
        op.execute("DROP TYPE IF EXISTS issuekindenum")
