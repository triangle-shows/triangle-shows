"""Alembic migration 0006: add events.is_manually_created.

Role: Marks a row an admin created by hand rather than a scraper finding it, so the
reconcile pass in scrapers/manager.py can refuse to delete it.

Why a column and not a check on `source`. plan_upsert deletes any row inside the
scraped date window that the scrape did not return, and a hand-added event is by
definition never returned by a scraper. Filtering those orphans on `source != "manual"`
looks equivalent and is not: _apply_scraped writes `row.source = se.source` whenever a
scraped event matches a stored row by hash or by (external_id, date). So the first time
a venue happens to list a show that was already added by hand, that row's `source`
becomes the scraper's name, the protection silently evaporates, and the next run deletes
it. A dedicated flag no scraper code writes cannot be stripped that way — the same
reason events.is_manual_override has survived alongside the classifier.

Distinct from is_manual_override, which records that an admin set the *live-music
verdict* by hand. A row can be manually created and still auto-classified, or scraped
and manually overridden. Conflating them would make either signal unreadable.

Additive and safe: one non-null boolean with a server default, so existing rows become
False (scraper-created, which they all are) without a backfill pass. Rolling back to
0005 leaves the column unread rather than broken.
Requires: A live PostgreSQL database at revision 0005.
"""

from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default rather than a nullable column plus a backfill: every existing row is
    # scraper-created, so False is correct for all of them and Postgres can fill it
    # without rewriting the table.
    op.add_column(
        'events',
        sa.Column(
            'is_manually_created',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Indexed because reconcile filters on it on every scrape of every venue, and because
    # the admin list will want "show me what I added by hand". Only a tiny minority of
    # rows will ever be true, so the index stays small.
    op.create_index('ix_events_is_manually_created', 'events', ['is_manually_created'])


def downgrade() -> None:
    op.drop_index('ix_events_is_manually_created', table_name='events')
    op.drop_column('events', 'is_manually_created')
