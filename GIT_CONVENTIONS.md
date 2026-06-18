# Git Conventions

## Branch structure

```
main          ← production; always deployable; protected
  └── dev     ← integration branch; feature branches merge here first
        ├── feature/xyz
        ├── feature/abc
        └── fix/xyz
```

- `main` only receives merges from `dev` (or a `hotfix/*` branch for urgent prod fixes)
- Feature and fix branches are created from `dev`, worked on, then merged back to `dev` via PR
- When `dev` is stable and ready to ship, open a PR from `dev` → `main` to trigger a deploy
- `hotfix/*` branches are the exception — they branch from `main` directly for urgent production patches

## Branch naming

| Prefix | Use for |
|--------|---------|
| `feature/` | New functionality |
| `fix/` | Bug fixes |
| `hotfix/` | Urgent production patches branched from `main` |

Names should be short and descriptive: `feature/event-submissions`, `fix/filter-performance`.

## Branch protection (GitHub Settings → Branches)

- **`main`**: require PR to merge, no direct push, optionally require CI to pass
- **`dev`**: optional — at minimum block force-pushes

Set `dev` as the default branch (GitHub Settings → General → Default branch) so new PRs target `dev` by default.

## Pull requests

- Open a PR for any non-trivial change — new features, significant refactors
- Small fixes can be committed directly to `dev`; the `dev` → `main` merge should always be a PR
- Use **Draft PRs** for in-flight work you want tracked but not yet merged
- Use `closes #12` in PR descriptions to auto-close linked issues when the PR merges
- The PR description is where you record *why* the change was made — commit messages cover *what*

## Issue and project tracking

- **GitHub Issues** track individual tasks, bugs, and features
- **GitHub Projects** (kanban board) organizes issues into columns: Backlog / In Progress / Done
- Cards on the board are Issues (or Draft items for unplanned/inbox ideas — convertible to Issues later)
- PRs can be added to the board to show in-flight work alongside the backlog
- `closes #N` in a PR description auto-moves the linked Issue to Done on merge

## Deployment

Push to `main` triggers Cloud Build automatically → deploys to Cloud Run (`triangle-shows`, `us-east1`).

For visual/color changes: push one change at a time and confirm it looks correct on the live site before stacking further changes.

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

# Rebase a feature branch onto dev
git checkout feature/my-feature
git rebase origin/dev
git push --force-with-lease origin feature/my-feature

# Delete a remote branch
git push origin --delete feature/old-branch
```
