"""thêm bảng rollup daily_nvr_uptime (uptime NVR theo ngày, giữ lâu dài)

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-06-25 17:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'daily_nvr_uptime',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('nvr_id', sa.Integer(), nullable=False),
        sa.Column('total_checks', sa.Integer(), nullable=False),
        sa.Column('online_checks', sa.Integer(), nullable=False),
        sa.Column('uptime_pct', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(['nvr_id'], ['nvr_devices.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_daily_nvr_uptime_day_nvr', 'daily_nvr_uptime', ['day', 'nvr_id'],
        unique=True,
    )
    op.create_index(
        'ix_daily_nvr_uptime_nvr_day', 'daily_nvr_uptime', ['nvr_id', 'day'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_daily_nvr_uptime_nvr_day', table_name='daily_nvr_uptime')
    op.drop_index('ix_daily_nvr_uptime_day_nvr', table_name='daily_nvr_uptime')
    op.drop_table('daily_nvr_uptime')
