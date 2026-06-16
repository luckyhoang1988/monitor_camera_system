"""add status indexes for dashboard queries

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-16 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('ix_nvr_status', 'nvr_devices', ['current_status'], unique=False)
    op.create_index(
        'ix_nvr_area_status', 'nvr_devices', ['area', 'current_status'], unique=False
    )
    op.create_index(
        'ix_cam_status', 'camera_channels', ['current_status'], unique=False
    )


def downgrade() -> None:
    op.drop_index('ix_cam_status', table_name='camera_channels')
    op.drop_index('ix_nvr_area_status', table_name='nvr_devices')
    op.drop_index('ix_nvr_status', table_name='nvr_devices')
