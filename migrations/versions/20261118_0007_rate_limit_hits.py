"""Add shared rate limits and distinguish positive and negative search cache entries."""

import sqlalchemy as sa
from alembic import op

revision = "20261118_0007"
down_revision = "20261118_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_hits",
        sa.Column("key", sa.String(80), primary_key=True),
        sa.Column("window_start", sa.BigInteger(), primary_key=True),
        sa.Column("hits", sa.Integer(), nullable=False),
    )
    op.create_index("ix_rate_limit_hits_window_start", "rate_limit_hits", ["window_start"])
    op.add_column(
        "search_cache",
        sa.Column("is_negative", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.execute("DELETE FROM search_cache WHERE is_negative")
    op.drop_column("search_cache", "is_negative")
    op.drop_index("ix_rate_limit_hits_window_start", table_name="rate_limit_hits")
    op.drop_table("rate_limit_hits")
