"""
Tests that retiring a venue cannot be blocked by its series rules — review item #9.

seed_venues() deletes venues named in REMOVED_SLUGS on every startup, via
session.delete(). That cascades only through relationships SQLAlchemy knows about. Venue
had them for events and scrape_logs but not for series_overrides, and the foreign key had
no ON DELETE, so the first retirement of a venue holding a series override would raise
ForeignKeyViolation inside the lifespan handler — and the container would never become
healthy. REMOVED_SLUGS is [""] today, which is why nothing has broken yet.

The fix has two halves and this file checks both, because either alone leaves a hole:

  * an ORM relationship with delete-orphan, which covers session.delete()
  * ON DELETE CASCADE on the constraint, which covers anything bypassing the ORM

Asserted against mapper metadata and the migration source rather than a live database.
The model being right while the migration is wrong is the realistic drift — the model
governs nothing at runtime, since the table is created by the migration — so the migration
is checked too. Neither check needs Postgres.
"""

import pathlib
import re

from app.models import SeriesOverride, Venue


MIGRATION = (
    pathlib.Path(__file__).resolve().parent.parent
    / "alembic" / "versions" / "0004_add_series_overrides.py"
)


class TestOrmCascade:
    """Covers session.delete(venue), which is how seed_venues() retires a venue."""

    def test_venue_has_a_series_overrides_relationship(self):
        assert "series_overrides" in Venue.__mapper__.relationships

    def test_it_cascades_deletes_like_events_and_scrape_logs(self):
        rel = Venue.__mapper__.relationships["series_overrides"]
        assert rel.cascade.delete
        assert rel.cascade.delete_orphan

    def test_it_matches_the_cascade_on_the_other_two_collections(self):
        """Consistency is the point — a venue's dependents should not have three different
        deletion behaviors depending on which was added when."""
        cascades = {
            name: (Venue.__mapper__.relationships[name].cascade.delete,
                   Venue.__mapper__.relationships[name].cascade.delete_orphan)
            for name in ("events", "scrape_logs", "series_overrides")
        }
        assert len(set(cascades.values())) == 1, cascades

    def test_the_reverse_relationship_is_wired(self):
        """back_populates on Venue requires the matching attribute here, or SQLAlchemy
        raises at mapper configuration time."""
        assert "venue" in SeriesOverride.__mapper__.relationships


class TestDatabaseConstraint:
    """Covers deletions that never pass through the ORM's cascade."""

    def test_the_model_declares_on_delete_cascade(self):
        fks = list(SeriesOverride.__table__.c.venue_id.foreign_keys)
        assert len(fks) == 1
        assert fks[0].ondelete == "CASCADE"

    def test_the_migration_declares_it_too(self):
        """The migration, not the model, is what creates the constraint in the database.
        A model-only fix would look correct here and change nothing in production."""
        source = MIGRATION.read_text(encoding="utf-8")
        fk = re.search(
            r"ForeignKeyConstraint\(\s*\['venue_id'\][^)]*\)", source, re.DOTALL
        )
        assert fk is not None, "venue_id foreign key not found in 0004"
        assert "ondelete='CASCADE'" in fk.group(0) or 'ondelete="CASCADE"' in fk.group(0)
