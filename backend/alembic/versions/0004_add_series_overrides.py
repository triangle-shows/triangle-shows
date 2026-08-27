"""Alembic migration 0004: add the series_overrides table.

Role: Backs admin series-level overrides for the live-music filter. A row keyed by
(venue_id, normalized_name) forces is_live_music for every event in a recurring
series, including future scraped instances (applied in ScrapeManager.reclassify_all).
Additive and safe: creates a new empty table, touches no existing data.
Requires: A live PostgreSQL database at revision 0003.
"""

from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'series_overrides',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('venue_id', sa.Integer(), nullable=False),
        sa.Column('normalized_name', sa.String(200), nullable=False),
        sa.Column('display_name', sa.String(500), nullable=True),
        sa.Column('is_live_music', sa.Boolean(), nullable=False),
        sa.Column('note', sa.String(500), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        # ON DELETE CASCADE so retiring a venue cannot be blocked by its series rules.
        # Edited into this migration rather than added by a later one: 0003-0005 have never
        # been applied anywhere (both main and prod sit at 0002), so there is no deployed
        # constraint to alter.
        sa.ForeignKeyConstraint(['venue_id'], ['venues.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('venue_id', 'normalized_name', name='uq_series_override_key'),
    )
    op.create_index('ix_series_overrides_venue_id', 'series_overrides', ['venue_id'])


def downgrade() -> None:
    op.drop_index('ix_series_overrides_venue_id', table_name='series_overrides')
    op.drop_table('series_overrides')
