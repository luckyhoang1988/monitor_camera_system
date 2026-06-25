"""add record bitrate + retention days estimate to nvr_devices

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-25 14:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'nvr_devices', sa.Column('record_bitrate_kbps', sa.Integer(), nullable=True)
    )
    op.add_column(
        'nvr_devices', sa.Column('retention_days_est', sa.Float(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('nvr_devices', 'retention_days_est')
    op.drop_column('nvr_devices', 'record_bitrate_kbps')
