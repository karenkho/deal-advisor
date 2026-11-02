"""Withdrawn open questions and BRD section items: who, when and why, never deleted.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("open_questions", sa.Column("withdrawn", postgresql.JSONB(none_as_null=True), nullable=True))
    op.add_column("brd_section_items", sa.Column("withdrawn", postgresql.JSONB(none_as_null=True), nullable=True))


def downgrade() -> None:
    op.drop_column("brd_section_items", "withdrawn")
    op.drop_column("open_questions", "withdrawn")
