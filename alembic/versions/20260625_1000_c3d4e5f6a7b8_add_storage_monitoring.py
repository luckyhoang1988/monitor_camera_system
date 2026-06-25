"""add storage/HDD monitoring (columns + nvr_hdd, nvr_storage_logs)

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-25 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Cột tóm tắt lưu trữ trên nvr_devices ---
    op.add_column(
        'nvr_devices',
        sa.Column(
            'storage_status', sa.String(length=20), nullable=False,
            server_default='Unknown',
        ),
    )
    op.add_column('nvr_devices', sa.Column('storage_total_mb', sa.Integer(), nullable=True))
    op.add_column('nvr_devices', sa.Column('storage_free_mb', sa.Integer(), nullable=True))
    op.add_column('nvr_devices', sa.Column('storage_used_pct', sa.Float(), nullable=True))
    op.add_column('nvr_devices', sa.Column('hdd_count', sa.Integer(), nullable=True))
    op.add_column('nvr_devices', sa.Column('hdd_healthy_count', sa.Integer(), nullable=True))
    op.add_column('nvr_devices', sa.Column('raid_status', sa.String(length=40), nullable=True))
    op.add_column(
        'nvr_devices',
        sa.Column('storage_last_checked_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column('nvr_devices', sa.Column('storage_last_error', sa.Text(), nullable=True))

    # --- Bảng trạng thái hiện tại từng ổ ---
    op.create_table(
        'nvr_hdd',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('nvr_id', sa.Integer(), nullable=False),
        sa.Column('hdd_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=True),
        sa.Column('capacity_mb', sa.Integer(), nullable=True),
        sa.Column('free_mb', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=40), nullable=True),
        sa.Column('is_recording', sa.Boolean(), nullable=True),
        sa.Column('smart_health', sa.String(length=40), nullable=True),
        sa.Column('temperature_c', sa.Integer(), nullable=True),
        sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['nvr_id'], ['nvr_devices.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_nvr_hdd_nvr_hddid', 'nvr_hdd', ['nvr_id', 'hdd_id'], unique=True
    )

    # --- Bảng lịch sử sức khỏe lưu trữ ---
    op.create_table(
        'nvr_storage_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('nvr_id', sa.Integer(), nullable=False),
        sa.Column('overall_status', sa.String(length=20), nullable=False),
        sa.Column('total_mb', sa.Integer(), nullable=True),
        sa.Column('free_mb', sa.Integer(), nullable=True),
        sa.Column('used_pct', sa.Float(), nullable=True),
        sa.Column('hdd_error_count', sa.Integer(), nullable=True),
        sa.Column('error_msg', sa.Text(), nullable=True),
        sa.Column(
            'checked_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.ForeignKeyConstraint(['nvr_id'], ['nvr_devices.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_nvr_storage_log_nvr_checked',
        'nvr_storage_logs', ['nvr_id', 'checked_at'], unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_nvr_storage_log_nvr_checked', table_name='nvr_storage_logs')
    op.drop_table('nvr_storage_logs')
    op.drop_index('ix_nvr_hdd_nvr_hddid', table_name='nvr_hdd')
    op.drop_table('nvr_hdd')
    op.drop_column('nvr_devices', 'storage_last_error')
    op.drop_column('nvr_devices', 'storage_last_checked_at')
    op.drop_column('nvr_devices', 'raid_status')
    op.drop_column('nvr_devices', 'hdd_healthy_count')
    op.drop_column('nvr_devices', 'hdd_count')
    op.drop_column('nvr_devices', 'storage_used_pct')
    op.drop_column('nvr_devices', 'storage_free_mb')
    op.drop_column('nvr_devices', 'storage_total_mb')
    op.drop_column('nvr_devices', 'storage_status')
