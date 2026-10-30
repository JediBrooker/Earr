"""add request limits

Revision ID: e4dae6123e54
Revises: e3c9e5012d43
Create Date: 2026-10-30 14:00:03.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e4dae6123e54"
down_revision: Union[str, None] = "e3c9e5012d43"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # limits count requests by when they were made, which updated_at stopped
    # saying once approvals started changing it
    for table in ("audiobookrequest", "manualbookrequest"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "created_at",
                    sa.DateTime(),
                    server_default=sa.func.now(),
                    nullable=False,
                )
            )
        # the best guess for existing requests
        op.execute(f"UPDATE {table} SET created_at = updated_at")

    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(sa.Column("request_limit", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("request_limit")
    for table in ("manualbookrequest", "audiobookrequest"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("created_at")
