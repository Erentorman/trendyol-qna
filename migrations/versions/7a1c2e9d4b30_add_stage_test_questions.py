"""add stage_test_questions

Revision ID: 7a1c2e9d4b30
Revises: 45cb3e5a086f
Create Date: 2026-10-12 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '7a1c2e9d4b30'
down_revision: Union[str, Sequence[str], None] = '45cb3e5a086f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('stage_test_questions',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('trendyol_question_id', sa.BigInteger(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('content_id', sa.BigInteger(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('user_full_name', sa.String(length=255), nullable=False),
    sa.Column('create_http_status', sa.Integer(), nullable=False),
    sa.Column('correlation_id', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_stage_test_questions'))
    )


def downgrade() -> None:
    op.drop_table('stage_test_questions')
