"""Alembic migration 0007: add the show_submissions table.

Role: Backs /new-shows-form, where people allowed through a Cloudflare Access policy can
propose a show for the calendar. A submission waits here until an admin approves it,
which creates the Venue (if a new one was proposed) and the Event through the same code
path an admin's own hand-add uses, or rejects it.

A separate table rather than a "pending" flag on events: see the ShowSubmission
docstring in app/models.py. In short, every public read path reads `events`, and each
one would have to remember to exclude pending rows; here there is nothing to forget.

Neither FK carries a CHECK tying it to anything else. venue_id is ON DELETE SET NULL, so
a "venue_id or new_venue_name" constraint would make deleting a venue fail on any
pending submission pointing at it. event_id is ON DELETE SET NULL so deleting an
approved event does not erase the record that someone proposed it.

Additive and safe: a new table nothing existing reads. A rollback to 0006 leaves it
unread rather than broken.
Requires: A live PostgreSQL database at revision 0006.
"""

from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'show_submissions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('submitted_by', sa.String(length=320), nullable=False),
        sa.Column('submitted_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column(
            'venue_id', sa.Integer(),
            sa.ForeignKey('venues.id', ondelete='SET NULL', name='fk_show_submissions_venue_id'),
            nullable=True,
        ),
        sa.Column('new_venue_name', sa.String(length=200), nullable=True),
        sa.Column('new_venue_city', sa.String(length=50), nullable=True),
        sa.Column('new_venue_website', sa.String(length=500), nullable=True),
        sa.Column('name', sa.String(length=500), nullable=False),
        sa.Column('artist', sa.String(length=300), nullable=True),
        sa.Column('support_artists', sa.Text(), nullable=True),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('doors_time', sa.Time(), nullable=True),
        sa.Column('show_time', sa.Time(), nullable=True),
        sa.Column('ticket_url', sa.String(length=1000), nullable=True),
        sa.Column('price_min', sa.Float(), nullable=True),
        sa.Column('price_max', sa.Float(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('genre', sa.String(length=100), nullable=True),
        sa.Column('age_restriction', sa.String(length=50), nullable=True),
        sa.Column('is_live_music', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('reviewed_by', sa.String(length=320), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column(
            'event_id', sa.Integer(),
            sa.ForeignKey('events.id', ondelete='SET NULL', name='fk_show_submissions_event_id'),
            nullable=True,
        ),
    )
    # The admin list asks for status = 'pending'; the form asks for one submitter's rows.
    op.create_index('ix_show_submissions_status', 'show_submissions', ['status'])
    op.create_index('ix_show_submissions_submitted_by', 'show_submissions', ['submitted_by'])


def downgrade() -> None:
    op.drop_index('ix_show_submissions_submitted_by', table_name='show_submissions')
    op.drop_index('ix_show_submissions_status', table_name='show_submissions')
    op.drop_table('show_submissions')
