"""add search attempt tracking

Revision ID: dd029e887d5c
Revises: 36b1bf5e40a1
Create Date: 2026-09-22 22:20:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "dd029e887d5c"
down_revision: Union[str, None] = "36b1bf5e40a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("audiobook", "manualbookrequest")


def upgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            # existing rows have never been retried, so they start at zero
            batch_op.add_column(
                sa.Column(
                    "search_attempts",
                    sa.Integer(),
                    server_default="0",
                    nullable=False,
                )
            )
            batch_op.add_column(
                sa.Column("last_searched_at", sa.DateTime(), nullable=True)
            )


def downgrade() -> None:
    for table in reversed(_TABLES):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("last_searched_at")
            batch_op.drop_column("search_attempts")
