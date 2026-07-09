"""Initial PostgreSQL schema for Chainlit persistence and app conversation context."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260425_000001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("identifier", sa.Text(), nullable=False, unique=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("createdAt", sa.Text(), nullable=True),
    )

    op.create_table(
        "threads",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("createdAt", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("userId", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("userIdentifier", sa.Text(), nullable=True),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index("ix_threads_userIdentifier", "threads", ["userIdentifier"])

    op.create_table(
        "steps",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("threadId", sa.Text(), sa.ForeignKey("threads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parentId", sa.Text(), nullable=True),
        sa.Column("streaming", sa.Boolean(), nullable=False),
        sa.Column("waitForAnswer", sa.Boolean(), nullable=True),
        sa.Column("isError", sa.Boolean(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("input", sa.Text(), nullable=True),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("createdAt", sa.Text(), nullable=True),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("start", sa.Text(), nullable=True),
        sa.Column("end", sa.Text(), nullable=True),
        sa.Column("generation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("showInput", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("indent", sa.Integer(), nullable=True),
        sa.Column("defaultOpen", sa.Boolean(), nullable=True),
        sa.Column("autoCollapse", sa.Boolean(), nullable=True),
        sa.Column("disableFeedback", sa.Boolean(), nullable=True),
        sa.Column("modes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index("ix_steps_threadId", "steps", ["threadId"])

    op.create_table(
        "elements",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("threadId", sa.Text(), sa.ForeignKey("threads.id", ondelete="CASCADE"), nullable=True),
        sa.Column("type", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("chainlitKey", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("display", sa.Text(), nullable=True),
        sa.Column("objectKey", sa.Text(), nullable=True),
        sa.Column("size", sa.Text(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("forId", sa.Text(), nullable=True),
        sa.Column("mime", sa.Text(), nullable=True),
        sa.Column("props", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index("ix_elements_threadId", "elements", ["threadId"])

    op.create_table(
        "feedbacks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("forId", sa.Text(), nullable=False),
        sa.Column("threadId", sa.Text(), sa.ForeignKey("threads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
    )
    op.create_index("ix_feedbacks_threadId", "feedbacks", ["threadId"])

    op.create_table(
        "conversation_contexts",
        sa.Column("thread_id", sa.Text(), sa.ForeignKey("threads.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("conversation_history", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("conversation_contexts")
    op.drop_index("ix_feedbacks_threadId", table_name="feedbacks")
    op.drop_table("feedbacks")
    op.drop_index("ix_elements_threadId", table_name="elements")
    op.drop_table("elements")
    op.drop_index("ix_steps_threadId", table_name="steps")
    op.drop_table("steps")
    op.drop_index("ix_threads_userIdentifier", table_name="threads")
    op.drop_table("threads")
    op.drop_table("users")
