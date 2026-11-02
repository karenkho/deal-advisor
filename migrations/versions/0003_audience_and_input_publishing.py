"""who an open question is for; publishing state of input documents

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('open_questions', sa.Column('audience', sa.Text(), server_default='client', nullable=False))
    op.execute("UPDATE open_questions SET audience = 'internal' WHERE source = 'brd_section'")
    for name in ('publish_status', 'drive_file_id', 'drive_url', 'local_path'):
        op.add_column('documents', sa.Column(name, sa.Text(), nullable=True))


def downgrade() -> None:
    for name in ('local_path', 'drive_url', 'drive_file_id', 'publish_status'):
        op.drop_column('documents', name)
    op.drop_column('open_questions', 'audience')
