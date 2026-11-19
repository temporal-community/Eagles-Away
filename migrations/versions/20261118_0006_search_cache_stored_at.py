"""Record when cache rows were stored and how long their live search took.

The TTL is applied when reading, and the search duration drives probabilistic early
expiration. Cached search results are disposable, so existing rows are cleared.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261118_0006"
down_revision = "20261117_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM search_cache")
    op.drop_index("ix_search_cache_expires_at", table_name="search_cache")
    with op.batch_alter_table("search_cache") as batch:
        batch.drop_column("expires_at")
        batch.add_column(sa.Column("stored_at", sa.DateTime(timezone=True), nullable=False))
        batch.add_column(sa.Column("fetch_seconds", sa.Float(), nullable=False))
    op.create_index("ix_search_cache_stored_at", "search_cache", ["stored_at"])


def downgrade() -> None:
    op.execute("DELETE FROM search_cache")
    op.drop_index("ix_search_cache_stored_at", table_name="search_cache")
    with op.batch_alter_table("search_cache") as batch:
        batch.drop_column("fetch_seconds")
        batch.drop_column("stored_at")
        batch.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_search_cache_expires_at", "search_cache", ["expires_at"])
