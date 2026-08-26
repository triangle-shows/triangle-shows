# Contributing

## Branch structure

```
main                ← production; always deployable; protected
  ├── feature/xyz
  ├── feature/abc
  └── fix/xyz
```

- `main` is the only long-lived branch. It is always deployable, and every push to it deploys.
- Feature and fix branches are created from `main`, worked on, then merged back into `main` via PR.
- There is no integration branch. Work is combined by merging PRs into `main`, not by staging them elsewhere first.

## Branch naming

| Prefix | Use for |
|--------|---------|
| `feature/` | New functionality |
| `fix/` | Bug fixes |

Names should be short and descriptive: `feature/event-submissions`, `fix/filter-performance`.

## Branch protection

`main` is protected by a repository ruleset (GitHub Settings → Rules → Rulesets):

| Rule | Effect |
|------|--------|
| Require a pull request | No direct pushes to `main` — every change arrives through a PR |
| Restrict deletions | The branch itself cannot be deleted |
| Block force pushes | History can only move forward; nothing already on `main` can be rewritten or dropped |

"Restrict deletions" protects the *branch*, not files — a PR that deletes files merges normally.

The ruleset currently targets the default branch rather than `main` by name. Since `main` is the default, that resolves correctly today. If the default branch is ever changed, repoint the ruleset to `refs/heads/main` first, or `main` will silently lose its protection.

Automatic head-branch deletion is on, so a feature branch is removed from GitHub once its PR merges. Your local copy survives — clean up with `git fetch --prune` and `git branch -d <branch>`.

## Pull requests

- **Every change goes through a PR.** The ruleset blocks direct pushes to `main` for everyone except repository admins, who can bypass it. Treat that bypass as an escape hatch, not the workflow.
- Use **Draft PRs** for in-flight work you want tracked but not yet merged.
- Use `closes #12` in PR descriptions to auto-close linked issues when the PR merges.
- The PR description is where you record *why* the change was made — commit messages cover *what*.

## Issue and project tracking

- **GitHub Issues** track individual tasks, bugs, and features
- **GitHub Projects** (kanban board) organizes issues into columns: Backlog / In Progress / Done
- Cards on the board are Issues (or Draft items for unplanned/inbox ideas — convertible to Issues later)
- PRs can be added to the board to show in-flight work alongside the backlog
- `closes #N` in a PR description auto-moves the linked Issue to Done on merge

## Deployment

Merging a PR into `main` triggers Cloud Build automatically → deploys to Cloud Run (`triangle-shows`, `us-east1`).

**Every merge to `main` incurs a small GCP build and hosting cost, and there is no staging environment.** Because each merged PR deploys on its own, group related work into a single PR rather than merging a string of small ones. Test locally first — see [SELF-HOSTING.md](../docs/SELF-HOSTING.md).

For visual/color changes: merge one change at a time and confirm it looks correct on the live site before stacking further changes.

## Database & migrations

**Git branches isolate code, not data.** There is a single shared Neon PostgreSQL database behind one secret (`triangle-shows-db-url`). A feature branch does *not* get its own database — Neon has a branching feature, but this repo doesn't wire it up automatically.

What this means in practice:

- **Feature branches never touch Neon.** Cloud Build deploys on push to `main` only, so un-merged work can't reach the production database.
- **Local dev never touches Neon.** `docker-compose.yml` and `backend/.env` point `DATABASE_URL` at a local Postgres, so `docker-compose up`, scrapes, and migrations all run against throwaway local data. Experiment freely.
- **Neon is touched only on merge to `main`.** On the first boot of the new Cloud Run revision, the app runs `alembic upgrade head` at startup (in `main.py`'s lifespan) against Neon. That is the moment a new migration actually executes in production.

Because there is no separate staging copy of the data, a migration proves itself against real data only at deploy time. So:

- **Write backward-compatible ("expand") migrations.** Add columns as nullable or with a default; don't drop/rename in the same change that ships the code depending on it. That keeps a Cloud Run rollback safe — old code simply ignores new columns, and the DB stays at the newer Alembic revision (only an explicit `alembic downgrade` removes anything).
- **Rehearse risky migrations on a Neon branch.** For anything beyond a simple additive change, create a Neon branch of the database (copy-on-write, cheap), point a local run at that branch's connection string, verify, then discard it. That gives you production-shaped data without risk — the DB-side equivalent of a feature branch.

## Useful commands

```bash
# Create and push a new branch
git checkout -b feature/my-feature
git push -u origin feature/my-feature

# Check remote connections
git remote -v

# List all branches (local + remote)
git branch -a

# See what's on a branch that isn't in main
git log origin/main..origin/feature/my-feature --oneline

# Bring a feature branch up to date with main
git checkout feature/my-feature
git rebase origin/main
git push --force-with-lease origin feature/my-feature

# Clean up after a PR merges (GitHub deletes the remote branch itself)
git fetch --prune
git branch -d feature/my-feature

# Delete a remote branch
git push origin --delete feature/old-branch
```
