# Contributing

## Branch structure

```
prod                    ← what's actually deployed; protected
  └── main              ← default branch; where work integrates; protected
        ├── feature/xyz
        ├── feature/abc
        └── fix/xyz
```

- `main` is the default branch and where all work lands. Merging into it does **not** deploy.
- `prod` is the only branch that deploys. It receives merges from `main` and nothing else — never commit to it directly.
- Feature and fix branches are created from `main`, worked on, then merged back into `main` via PR.
- Shipping is a separate, deliberate step: open a PR from `main` → `prod` when you're ready for the changes to go live.

This split exists so that merging work and deploying work are two different decisions. You can merge a dozen PRs into `main` over a week and ship them as one deploy.

## Branch naming

| Prefix | Use for |
|--------|---------|
| `feature/` | New functionality |
| `fix/` | Bug fixes |

Names should be short and descriptive: `feature/event-submissions`, `fix/filter-performance`.

An urgent production fix needs no special prefix — it's a `fix/` branch that gets merged to `main` and promoted to `prod` immediately, rather than waiting to be batched.

## Branch protection

Both long-lived branches are protected by repository rulesets (GitHub Settings → Rules → Rulesets):

| Rule | Effect |
|------|--------|
| Require a pull request | No direct pushes — every change arrives through a PR |
| Restrict deletions | The branch itself cannot be deleted |
| Block force pushes | History can only move forward; nothing already on the branch can be rewritten or dropped |

"Restrict deletions" protects the *branch*, not files — a PR that deletes files merges normally.

Repository admins can bypass these rules. Treat that as an escape hatch, not the workflow.

The `main` ruleset targets the default branch rather than `main` by name. Since `main` is the default, that resolves correctly today. If the default branch is ever changed, repoint the ruleset to `refs/heads/main` first, or `main` will silently lose its protection.

Automatic head-branch deletion is on, so a feature branch is removed from GitHub once its PR merges. `main` is exempt because its ruleset restricts deletions — it survives being merged into `prod`. Your local copies survive regardless; clean up with `git fetch --prune` and `git branch -d <branch>`.

## Pull requests

- **Every change goes through a PR**, including the `main` → `prod` promotion.
- Use **Draft PRs** for in-flight work you want tracked but not yet merged.
- Use `closes #12` in PR descriptions to auto-close linked issues when the PR merges.
- The PR description is where you record *why* the change was made — commit messages cover *what*.
- For a promotion PR, the description is the release note: what's shipping and anything to watch after it goes live.

## Issue and project tracking

- **GitHub Issues** track individual tasks, bugs, and features
- **GitHub Projects** (kanban board) organizes issues into columns: Backlog / In Progress / Done
- Cards on the board are Issues (or Draft items for unplanned/inbox ideas — convertible to Issues later)
- PRs can be added to the board to show in-flight work alongside the backlog
- `closes #N` in a PR description auto-moves the linked Issue to Done on merge

## Deployment

Merging a PR into `prod` triggers Cloud Build automatically → deploys to Cloud Run (`triangle-shows`, `us-east1`). The trigger watches `prod` and only `prod`; pushes to `main` and feature branches build nothing.

**Every deploy incurs a small GCP build and hosting cost, and there is no staging environment.** Batch related work into `main` and promote once, rather than promoting after every merge.

To watch a deploy land:

```bash
git checkout prod && git pull
python tools/wait_for_deploy.py
```

That script compares the deployed commit against your local `HEAD`, so run it from `prod` — from `main` it will never match.

For visual/color changes: promote one change at a time and confirm it looks correct on the live site before stacking further changes.

## Releases

Deploys are triggered by the promotion PR, not by tags — tagging is for humans, marking which commit shipped as which version.

```bash
git checkout prod && git pull
git tag v1.3.0
git push origin v1.3.0
```

Tags point at commits rather than branches, so tagging on `prod` works exactly like tagging anywhere else. From a tag you can open a GitHub Release to write up what changed.

## Database & migrations

**Git branches isolate code, not data.** There is a single shared Neon PostgreSQL database behind one secret (`triangle-shows-db-url`). A feature branch does *not* get its own database — Neon has a branching feature, but this repo doesn't wire it up automatically.

What this means in practice:

- **Only `prod` touches Neon.** Cloud Build deploys on push to `prod` only, so neither feature branches nor `main` can reach the production database.
- **Local dev never touches Neon.** `docker-compose.yml` and `backend/.env` point `DATABASE_URL` at a local Postgres, so `docker-compose up`, scrapes, and migrations all run against throwaway local data. Experiment freely.
- **Neon is touched only on merge to `prod`.** On the first boot of the new Cloud Run revision, the app runs `alembic upgrade head` at startup (in `main.py`'s lifespan) against Neon. That is the moment a new migration actually executes in production.

Because there is no separate staging copy of the data, a migration proves itself against real data only at deploy time. So:

- **Write backward-compatible ("expand") migrations.** Add columns as nullable or with a default; don't drop/rename in the same change that ships the code depending on it. That keeps a Cloud Run rollback safe — old code simply ignores new columns, and the DB stays at the newer Alembic revision (only an explicit `alembic downgrade` removes anything).
- **Rehearse risky migrations on a Neon branch.** For anything beyond a simple additive change, create a Neon branch of the database (copy-on-write, cheap), point a local run at that branch's connection string, verify, then discard it. That gives you production-shaped data without risk — the DB-side equivalent of a feature branch.

## Useful commands

```bash
# Create and push a new branch
git checkout -b feature/my-feature
git push -u origin feature/my-feature

# See what's on a branch that isn't in main
git log origin/main..origin/feature/my-feature --oneline

# Bring a feature branch up to date with main
git checkout feature/my-feature
git rebase origin/main
git push --force-with-lease origin feature/my-feature

# See what's merged into main but not yet shipped
git log origin/prod..origin/main --oneline

# Ship it — open the promotion PR
gh pr create --base prod --head main --title "Release" --body "What's shipping..."

# Clean up after a PR merges (GitHub deletes the remote branch itself)
git fetch --prune
git branch -d feature/my-feature

# Delete a remote branch
git push origin --delete feature/old-branch
```
