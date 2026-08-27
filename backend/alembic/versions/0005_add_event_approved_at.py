"""Alembic migration 0005: add events.approved_at.

Role: Backs admin review tracking for the live-music filter. A non-null approved_at
means someone has checked that event's classification and agreed with it; the admin
list hides approved events by default so the queue only shows unreviewed work.

Approval records agreement with a *specific verdict*, not the event as a whole:
ScrapeManager.reclassify_all clears approved_at whenever it changes an event's
is_live_music, so a re-classified event returns to the queue instead of staying
hidden behind a stale approval.

Additive and safe: adds one nullable column, so existing rows are simply unapproved
and a rollback to the previous revision leaves the column unread rather than broken.
Requires: A live PostgreSQL database at revision 0004.
"""

from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable with no server_default: every existing event starts unapproved, which
    # is the correct starting state for a review queue.
    op.add_column('events', sa.Column('approved_at', sa.DateTime(), nullable=True))
    # Partial index — the common query filters approved_at IS NULL, and only a
    # minority of rows will ever be non-null.
    op.create_index('ix_events_approved_at', 'events', ['approved_at'])


def downgrade() -> None:
    op.drop_index('ix_events_approved_at', table_name='events')
    op.drop_column('events', 'approved_at')
