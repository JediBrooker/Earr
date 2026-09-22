"""add download client fields

Revision ID: 095bcaddc575
Revises: dd029e887d5c
Create Date: 2026-09-22 23:05:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "095bcaddc575"
down_revision: Union[str, None] = "dd029e887d5c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("libraryimport", schema=None) as batch_op:
        # both nullable: grabs recorded before this migration have neither, and
        # usenet grabs never get a client id
        batch_op.add_column(
            sa.Column("client_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("protocol", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("libraryimport", schema=None) as batch_op:
        batch_op.drop_column("protocol")
        batch_op.drop_column("client_id")
