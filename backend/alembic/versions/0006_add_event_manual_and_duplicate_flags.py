"""Alembic migration 0006: add events.is_manually_created and events.duplicate_of_id.

Role: Two columns recording that a row's fate was a human decision, so the reconcile
pass in scrapers/manager.py can refuse to delete it — one for events an admin created
by hand, one for events an admin folded into another as a duplicate.

Both in one migration because they are one mechanism. `scraper_must_not_delete()` is a
single predicate over both, the candidate sort in plan_upsert has to rank them against
each other, and the case most likely to be missed is a row carrying *both* flags. Two
migrations would have meant two ALTER TABLEs and two chances to get that interaction
wrong; the columns arrive together with the tests that cover the combination.

Why a column and not a check on `source`. plan_upsert deletes any row inside the
scraped date window that the scrape did not return, and a hand-added event is by
definition never returned by a scraper. Filtering those orphans on `source != "manual"`
looks equivalent and is not: _apply_scraped writes `row.source = se.source` whenever a
scraped event matches a stored row by hash or by (external_id, date). So the first time
a venue happens to list a show that was already added by hand, that row's `source`
becomes the scraper's name, the protection silently evaporates, and the next run deletes
it. A dedicated flag no scraper code writes cannot be stripped that way — the same
reason events.is_manual_override has survived alongside the classifier.

is_manually_created is distinct from is_manual_override, which records that an admin set
the *live-music verdict* by hand. A row can be manually created and still auto-classified,
or scraped and manually overridden. Conflating them would make either signal unreadable.

duplicate_of_id points at the surviving row rather than being a bare "hidden" boolean
(issue #63 weighed both). The pointer buys three things a boolean cannot: the admin UI
can show "3 hidden duplicates" against the survivor; the one-off cleanup in
tools/dedupe_past_events.py can flag instead of delete and still be undone, because the
mapping is retained; and ON DELETE SET NULL means that if the survivor is itself deleted
the flag clears and the hidden row returns to the calendar, which is correct — a row
should not stay hidden on account of a row that no longer exists.

That last behaviour has a visible consequence worth stating: if a venue edits the
surviving listing's title, its hash changes, the old row is superseded and deleted, and
the duplicate reappears for an admin to re-flag. Accepted deliberately. The alternative —
a duplicate hidden forever behind a row nobody can find — is the failure mode a boolean
would have had permanently.

Additive and safe. is_manually_created is a non-null boolean with a server default, so
existing rows become False (scraper-created, which they all are) without a backfill.
duplicate_of_id is nullable, so existing rows are NULL, meaning "not a duplicate".
Rolling back to 0005 leaves both columns unread rather than broken.
Requires: A live PostgreSQL database at revision 0005.
"""

from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- events.is_manually_created ---
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

    # --- events.duplicate_of_id ---
    op.add_column('events', sa.Column('duplicate_of_id', sa.Integer(), nullable=True))
    # ON DELETE SET NULL, not CASCADE: deleting the survivor must un-hide the duplicate,
    # not delete it. CASCADE would make removing one row silently remove every row folded
    # into it, which is the opposite of the reversibility this column exists for.
    op.create_foreign_key(
        'fk_events_duplicate_of_id',
        source_table='events',
        referent_table='events',
        local_cols=['duplicate_of_id'],
        remote_cols=['id'],
        ondelete='SET NULL',
    )
    # A row cannot be a duplicate of itself. Cheap to enforce here, and unlike the
    # no-chains rule (do not point at a row that is itself flagged) it needs no lookup,
    # so it belongs in the database rather than in an API handler that could be bypassed.
    op.create_check_constraint(
        'ck_events_duplicate_of_not_self',
        'events',
        'duplicate_of_id IS NULL OR duplicate_of_id <> id',
    )
    # Partial index. The read paths ask `duplicate_of_id IS NULL`, which matches virtually
    # every row and is better served by a sequential scan, so indexing the NULLs would be
    # dead weight on every insert. What does need an index is the admin lookup — "which
    # rows were folded into this one", `WHERE duplicate_of_id = :id` — and that only ever
    # touches non-null values. Restricting the index to them keeps it proportional to the
    # number of flagged duplicates instead of the size of the events table.
    op.create_index(
        'ix_events_duplicate_of_id',
        'events',
        ['duplicate_of_id'],
        postgresql_where=sa.text('duplicate_of_id IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('ix_events_duplicate_of_id', table_name='events')
    op.drop_constraint('ck_events_duplicate_of_not_self', 'events', type_='check')
    op.drop_constraint('fk_events_duplicate_of_id', 'events', type_='foreignkey')
    op.drop_column('events', 'duplicate_of_id')
    op.drop_index('ix_events_is_manually_created', table_name='events')
    op.drop_column('events', 'is_manually_created')
