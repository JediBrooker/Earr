"""add blocklist

Revision ID: e3c9e5012d43
Revises: e2b8d4f01c32
Create Date: 2026-10-30 14:00:02.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "e3c9e5012d43"
down_revision: Union[str, None] = "e2b8d4f01c32"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "blocklistedrelease",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("guid", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("indexer", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("asin_or_uuid", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("book_title", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("reason", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("blocklistedrelease", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_blocklistedrelease_title"), ["title"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_blocklistedrelease_guid"), ["guid"], unique=False
        )

    # which release a grab was, so a failed one can be blocklisted exactly
    with op.batch_alter_table("libraryimport", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("guid", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("indexer", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("libraryimport", schema=None) as batch_op:
        batch_op.drop_column("indexer")
        batch_op.drop_column("guid")
    with op.batch_alter_table("blocklistedrelease", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_blocklistedrelease_guid"))
        batch_op.drop_index(batch_op.f("ix_blocklistedrelease_title"))
    op.drop_table("blocklistedrelease")
