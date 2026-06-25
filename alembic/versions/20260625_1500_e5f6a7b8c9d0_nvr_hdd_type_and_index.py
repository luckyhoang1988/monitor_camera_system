"""nvr_hdd: add hdd_type, drop unique (nvr_id,hdd_id) -> index nvr_id

NVR RAID trả volume ảo + đĩa vật lý trùng id -> bỏ unique, dùng delete+insert mỗi lượt quét.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-25 15:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('nvr_hdd', sa.Column('hdd_type', sa.String(length=40), nullable=True))
    # Bỏ unique cũ (gây trùng khóa với NVR RAID) -> index thường theo nvr_id.
    op.drop_index('ix_nvr_hdd_nvr_hddid', table_name='nvr_hdd')
    op.create_index('ix_nvr_hdd_nvr', 'nvr_hdd', ['nvr_id'], unique=False)
    # Dọn dữ liệu ổ cũ (đã ghi bằng logic sai) -> lượt quét tới ghi lại sạch.
    op.execute('DELETE FROM nvr_hdd')


def downgrade() -> None:
    op.drop_index('ix_nvr_hdd_nvr', table_name='nvr_hdd')
    op.create_index(
        'ix_nvr_hdd_nvr_hddid', 'nvr_hdd', ['nvr_id', 'hdd_id'], unique=True
    )
    op.drop_column('nvr_hdd', 'hdd_type')
