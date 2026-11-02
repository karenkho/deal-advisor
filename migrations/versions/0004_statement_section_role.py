"""The role the AI gave a statement's section.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("statements", sa.Column("section_role", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("statements", "section_role")
