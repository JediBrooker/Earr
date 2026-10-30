"""add follows

Revision ID: e2b8d4f01c32
Revises: e1a7c3d90b21
Create Date: 2026-10-30 14:00:01.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "e2b8d4f01c32"
down_revision: Union[str, None] = "e1a7c3d90b21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "follow",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_username", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "kind", sa.Enum("author", "series", name="followkindenum"), nullable=False
        ),
        sa.Column("audible_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("known_asins", sa.JSON(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_username"], ["user.username"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_username", "kind", "audible_id"),
    )
    with op.batch_alter_table("follow", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_follow_user_username"), ["user_username"], unique=False
        )

    with op.batch_alter_table("audiobookrequest", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("via_follow", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("audiobookrequest", schema=None) as batch_op:
        batch_op.drop_column("via_follow")
    with op.batch_alter_table("follow", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_follow_user_username"))
    op.drop_table("follow")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS followkindenum")
