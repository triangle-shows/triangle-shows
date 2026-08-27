"""Alembic migration 0003: add live-music classification columns to events.

Role: Adds the three columns that drive the live-music filter and admin override
feature — is_live_music (the effective flag the calendar/feeds read),
is_manual_override (protects admin decisions from being overwritten by re-scrapes),
and classification_reason (human-readable explanation). Additive and safe on an
existing DB: every current row defaults to is_live_music=True (visible) until the
first post-migration scrape runs ScrapeManager.reclassify_all().
Requires: A live PostgreSQL database at revision 0002.
"""

from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default is set so the columns backfill on existing rows without a
    # rewrite; the ORM-level Python default (models.py) handles new inserts.
    op.add_column(
        'events',
        sa.Column('is_live_music', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        'events',
        sa.Column('is_manual_override', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'events',
        sa.Column('classification_reason', sa.String(200), nullable=True),
    )
    op.create_index('ix_events_is_live_music', 'events', ['is_live_music'])

    # Drop the server_defaults now that existing rows are backfilled; going forward
    # the application layer supplies these values explicitly.
    op.alter_column('events', 'is_live_music', server_default=None)
    op.alter_column('events', 'is_manual_override', server_default=None)


def downgrade() -> None:
    op.drop_index('ix_events_is_live_music', table_name='events')
    op.drop_column('events', 'classification_reason')
    op.drop_column('events', 'is_manual_override')
    op.drop_column('events', 'is_live_music')
