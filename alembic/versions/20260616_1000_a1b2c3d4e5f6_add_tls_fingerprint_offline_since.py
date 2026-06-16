"""add tls_fingerprint to nvr_devices and offline_since to camera_channels

Revision ID: a1b2c3d4e5f6
Revises: 5c2cb64ff546
Create Date: 2026-06-16 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '5c2cb64ff546'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'nvr_devices',
        sa.Column('tls_fingerprint', sa.String(length=128), nullable=True),
    )
    op.add_column(
        'camera_channels',
        sa.Column('offline_since', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('camera_channels', 'offline_since')
    op.drop_column('nvr_devices', 'tls_fingerprint')
