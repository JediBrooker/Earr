"""add book language

Revision ID: e1a7c3d90b21
Revises: c41d8e2f7a90
Create Date: 2026-10-30 14:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "e1a7c3d90b21"
down_revision: Union[str, None] = "c41d8e2f7a90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # unknown for books cached before now; filled in the next time Audible is asked
    with op.batch_alter_table("audiobook", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("language", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("audiobook", schema=None) as batch_op:
        batch_op.drop_column("language")
