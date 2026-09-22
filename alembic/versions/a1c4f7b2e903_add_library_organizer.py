"""add library organizer

Revision ID: a1c4f7b2e903
Revises: 1718055d5ca8
Create Date: 2026-09-22 20:40:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "a1c4f7b2e903"
down_revision: Union[str, None] = "1718055d5ca8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "libraryimport",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("asin_or_uuid", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("book_title", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("release_title", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "imported",
                "failed",
                "expired",
                name="libraryimportstatusenum",
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("source_path", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("target_path", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_libraryimport_status", "libraryimport", ["status"])

    with op.batch_alter_table("audiobook", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("series", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "series_position", sqlmodel.sql.sqltypes.AutoString(), nullable=True
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("audiobook", schema=None) as batch_op:
        batch_op.drop_column("series_position")
        batch_op.drop_column("series")

    op.drop_index("ix_libraryimport_status", table_name="libraryimport")
    op.drop_table("libraryimport")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TYPE libraryimportstatusenum")
