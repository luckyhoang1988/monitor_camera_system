"""nvr_devices: thêm bitrate_checked_at + smart_supported (giảm request ISAPI)

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-25 16:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'nvr_devices',
        sa.Column('bitrate_checked_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'nvr_devices', sa.Column('smart_supported', sa.Boolean(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('nvr_devices', 'smart_supported')
    op.drop_column('nvr_devices', 'bitrate_checked_at')
