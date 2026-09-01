"""Candidate detection and validation for admin-flagged duplicate events.

Role: pure logic behind the admin duplicate-review queue and the mark/unmark endpoints
in app.api.admin. No database access and no ORM imports, so every rule here is testable
without Postgres.

The division of labour is the whole design, and it is a reaction to how #63 came about.
Two automated title rules had already been wrong in opposite directions — stripping every
bracketed token merged genuine early/late double bills, then treating a session label as
decisive refused to merge "[LATE] Sub Rosa" with "Sub Rosa", which are one show. So:

  * **Grouping is by fact, not inference.** A candidate cluster is simply more than one
    unfolded event at the same venue on the same date. No title comparison decides
    anything.

  * **Similarity only orders the queue.** `cluster_score` sorts likely duplicates to the
    top. Being wrong costs a scroll, never a wrong verdict, which is the property that
    makes it safe to use fuzzy matching here at all.

  * **A human decides.** Nothing in this module ever sets `duplicate_of_id`.

A cluster resolves itself: folding a row sets `duplicate_of_id`, which removes it from the
unfolded count, so a (venue, date) group with only one unfolded row left stops being a
candidate. Nothing has to record "done". What does *not* resolve is a genuine double bill,
where both rows correctly stay unfolded and the cluster reappears every time — tracked as
a known limitation in #90, with `cluster_score` ordering as the mitigation.
"""

# --- Imports ---

import re
from difflib import SequenceMatcher
from typing import Any, Iterable, Optional

# --- Similarity (ordering only) ---

# Tokens that distinguish a *session* rather than a show: the ones that made two listings
# for one night look different. Stripped before comparison so "[LATE] Sub Rosa" scores
# near-identical to "Sub Rosa" and floats to the top of the queue.
#
# This list being incomplete is harmless, which is the point of confining it to ordering.
# A missed token means a real duplicate ranks lower, not that it is missed.
_SESSION_NOISE = re.compile(
    r"(?ix)"
    r"\b(late|early|first|second|1st|2nd|matinee|encore|night|show|seating|set)\b"
    r"|\b(box\s*seats?|balcony|orchestra|mezzanine|standing|ga|general\s+admission)\b"
)


def normalize_for_similarity(title: str) -> str:
    """Reduce a title to what should be compared when ranking duplicate candidates.

    Deliberately aggressive — punctuation, bracketed asides and session words all go.
    That aggression is exactly what was wrong when a rule like this *decided* whether two
    rows were the same show; it is fine here because the only consequence is queue order.
    """
    text = (title or "").lower()
    text = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", text)  # bracketed asides
    text = _SESSION_NOISE.sub(" ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def title_similarity(a: str, b: str) -> float:
    """How alike two titles are, 0.0 to 1.0, after normalization.

    One title normalizing to nothing (a listing named only "[LATE]") scores 0 rather than
    matching every other empty-normalizing title, which would otherwise pull unrelated
    rows to the top of the queue.
    """
    na, nb = normalize_for_similarity(a), normalize_for_similarity(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def cluster_score(titles: Iterable[str]) -> float:
    """The strongest pairwise similarity in a cluster — its sort key in the queue.

    Max rather than mean: a cluster of three where two are obviously one show and the
    third is unrelated still deserves attention, and averaging would bury it.
    """
    items = list(titles)
    best = 0.0
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            best = max(best, title_similarity(items[i], items[j]))
    return best


# --- Cluster assembly ---


def build_clusters(rows: list[Any]) -> list[dict]:
    """Group rows into duplicate-review clusters, most likely duplicates first.

    `rows` is every event at a (venue, date) that already has two or more *unfolded*
    rows — the caller does that selection in SQL. Rows already folded are passed in too,
    so a cluster can show what was previously absorbed into its survivor.

    Only attribute access is used (.id, .name, .date, .venue_id, .duplicate_of_id, ...),
    so tests pass stand-ins rather than ORM objects.

    Ordering is `cluster_score` descending, then all-approved clusters after unresolved
    ones, then by date. `approved_at` is used *only* here, as a weak "a human has looked
    at these rows recently" signal for sort position — never to hide a cluster. Hiding on
    it would let an approval granted for a live-music decision silently remove a duplicate
    nobody had assessed; ordering cannot, because the cluster stays reachable. See #90.
    """
    grouped: dict[tuple[int, Any], list[Any]] = {}
    for row in rows:
        grouped.setdefault((row.venue_id, row.date), []).append(row)

    clusters = []
    for (venue_id, day), members in grouped.items():
        unfolded = [r for r in members if getattr(r, "duplicate_of_id", None) is None]
        # Defensive: the caller's HAVING should guarantee this, but a cluster of one
        # cannot be reviewed and must never render as a decision waiting to be made.
        if len(unfolded) < 2:
            continue
        # Fall back to doors_time, because the UI's time label does. Sorting on show_time
        # alone put a row labelled "doors 19:00" below one labelled "show 22:00" whenever
        # the earlier row had no show time — the ordering and the visible label disagreeing
        # about the same two rows, in the one column the admin is using to tell them apart.
        members.sort(key=lambda r: (r.show_time or r.doors_time or _NEVER, r.id))
        score = cluster_score([r.name for r in unfolded])
        all_approved = all(getattr(r, "approved_at", None) is not None for r in unfolded)
        clusters.append({
            "venue_id": venue_id,
            "date": day,
            "score": score,
            "all_approved": all_approved,
            "members": members,
        })

    clusters.sort(key=lambda c: (-c["score"], c["all_approved"], c["date"]))
    return clusters


class _Never:
    """Sorts after every real time, so rows without a show_time land last."""

    def __lt__(self, other: Any) -> bool:
        return False

    def __gt__(self, other: Any) -> bool:
        return True


_NEVER = _Never()


# --- Validation ---


class FoldError(ValueError):
    """A fold the admin asked for would leave the data in a state nothing can read."""


def validate_fold(
    survivor_id: int,
    duplicate_ids: Iterable[int],
    *,
    already_folded: Optional[dict[int, Optional[int]]] = None,
) -> list[int]:
    """Check a fold request and return the duplicate ids to write, de-duplicated.

    `already_folded` maps event id to its current `duplicate_of_id`, for every event in
    the request. Used for the chain rule, which needs a lookup and so cannot be a
    database constraint the way the self-reference rule can.

    Rules, and why each exists:

    **No self-reference.** Folding a row into itself makes it its own survivor, so it is
    hidden with nothing to restore it to. Also enforced by `ck_events_duplicate_of_not_self`
    in migration 0006 — checked here as well so the admin gets a sentence rather than an
    IntegrityError.

    **The survivor must not itself be folded.** Otherwise A points at B which points at C:
    A is hidden behind a row that is itself hidden, so unmarking A restores it to
    invisibility and the admin has no way to see why. Chains are refused rather than
    silently rewritten, because the intended survivor is genuinely ambiguous — the caller
    is told which row to use instead.

    **At least one duplicate.** An empty fold is a no-op that would otherwise report
    success, which reads as "it worked" on a click that did nothing.
    """
    ids = list(dict.fromkeys(duplicate_ids))  # de-dupe, keep order
    if not ids:
        raise FoldError("Select at least one event to fold in.")
    if survivor_id in ids:
        raise FoldError("An event cannot be a duplicate of itself.")

    folded = already_folded or {}
    survivor_target = folded.get(survivor_id)
    if survivor_target is not None:
        raise FoldError(
            f"Event {survivor_id} is itself marked as a duplicate of event "
            f"{survivor_target}. Unfold it first, or fold into event "
            f"{survivor_target} instead."
        )
    return ids
