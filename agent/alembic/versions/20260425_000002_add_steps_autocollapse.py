"""Add missing Chainlit step autoCollapse column."""

from alembic import op
import sqlalchemy as sa


revision = "20260425_000002"
down_revision = "20260425_000001"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("steps", "autoCollapse"):
        op.add_column("steps", sa.Column("autoCollapse", sa.Boolean(), nullable=True))


def downgrade() -> None:
    if _has_column("steps", "autoCollapse"):
        op.drop_column("steps", "autoCollapse")
