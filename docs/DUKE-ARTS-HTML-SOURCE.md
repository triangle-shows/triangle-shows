# A third Duke source: the arts.duke.edu listing

**Status: design, not implemented.** This note exists so the research behind it is not
repeated. It is the follow-up to #116, which ships the Bedework feeds.

## Why bother, when Duke already has a feed

#116 reads two Bedework feeds and unions them — the Arts topic and the Concert/Music
filter — because each is capped at 40 events and neither contains the other. That gets 64
events across a 23-day window.

`arts.duke.edu/events/` is a different view of the same calendar, and it is a bigger one.
Measured 2026-09-22:

| | both feeds (#116) | arts.duke.edu |
| --- | ---: | ---: |
| events | 64 | 85 (81 dated, 4 ongoing ranges) |
| **distinct titles** | 35 | **67** |
| forward window | 23 days | **30 days** |
| **titles the other lacks** | — | **48** |

Those 48 are the case for doing this. They are not filler: *An Evening with Fran
Lebowitz*, *A Conversation with David Rubenstein & Ken Burns*, *An Evening with Ariel
Stachel*. Ticketed events, on sale, that neither Bedework filter carries.

## What the page gives you

Each event is an `<article class="post post-event">` carrying:

| | where | notes |
| --- | --- | --- |
| id | `data-post-id` | **distinct per occurrence** — 85/85 in the sample |
| title | `h3` | |
| date | `.event-date-alt` | display text, **no year** |
| link | `h3 a` | the event's own page, an external site, or a `calendar.duke.edu` guid |
| venue | `a[href*="/places/"]` | e.g. `/places/duke-chapel/` |

85 is the whole list. The `.pagination` markup renders with zero links in it, so there is
no page 2 to walk.

## The three real costs

**No year on any date.** 0 of 81 dated cards carry one — they read
`Tuesday, September 22 at 12:00pm`. Year inference is required, and it is where
off-by-a-year bugs live, particularly across a December boundary where "January 3" is
next year and "December 28" is this one. `instantseats._parse_date` already solves the
same problem in this codebase and is the thing to copy rather than re-invent.

**Recurring series arrive as separate cards, but four do not.** Thirteen Organ
Demonstration cards each have their own `data-post-id` and their own date, which is what
is wanted. Four cards are ranges instead — `August 21, 2025 – December 31, 2026` for the
weekly carillon series — and a range is not an event. Those four have to be skipped, not
guessed at, or the calendar gains a show on a date nobody chose.

**It is noisier than the feeds.** Alongside Fran Lebowitz sit *AADS Brown Bag — Sick
Work*, *Adobe Express for Design (for Students)* and *AGS: Yorktown Staff Ride*. The
live-music classifier and the review queue absorb more from this source than from either
feed, and that is a running cost rather than a one-off.

And the ordinary one: it is HTML. A redesign breaks it, where a feed with documented
parameters does not.

## Shape of the work

**Add, do not replace.** The feeds are better data — ISO dates in local and UTC, stable
guids, a location uid, a cancellation status. This source is broader. Read all three.

**Deduping across sources is already solved, almost.** `_fetch_union` in
`duke_bedework.py` merges on `external_id`, and the page's cards link back to the same
`calendar.duke.edu` guids the feeds use. Where a card carries such a link, deriving the
same `external_id` makes the two sources collapse exactly. Where it does not — the 48 —
there is nothing to collide with, so `data-post-id` can stand alone under its own prefix,
e.g. `arts-post-84072`.

That prefix matters. Two sources minting bare integers and bare guids into one
`external_id` column will eventually collide by accident, and `plan_upsert` matches on
`(external_id, date)`.

**Open question: one scraper or two.** The feed reader and an HTML reader have almost
nothing in common beyond the union step. Either `duke_bedework.py` grows an HTML branch
selected by config, or a `duke_arts_html.py` sits beside it and the venue row lists both
scrapers — which the manager does not currently support, since `scraper_type` is a single
string. The first is less work and less clean. Worth deciding before writing code.

## What this does not fix

The venue is still one row. Eighteen rooms and counting, one colour, because the palette
has a single usable gap left (see #116). The room goes at the top of each event's
description; it is still not filterable and not on the calendar tile. That is a separate
decision about the colour rule, not about sources.
