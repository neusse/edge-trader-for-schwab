# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`.
- **Read an issue**: `gh issue view <number> --comments`, also fetching labels.
- **List issues**: use `gh issue list` with the appropriate state and label filters.
- **Comment**: `gh issue comment <number> --body "..."`
- **Apply or remove labels**: `gh issue edit <number> --add-label "..."` or `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

Infer the repository from `git remote -v`; `gh` does this automatically inside the clone.

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub shares one number space across issues and pull requests. Resolve an ambiguous `#42` with `gh pr view 42`, falling back to `gh issue view 42`.

## Skill operations

- When a skill says "publish to the issue tracker," create a GitHub issue.
- When a skill says "fetch the relevant ticket," run `gh issue view <number> --comments`.

## Wayfinding operations

The map is one GitHub issue with child issues as tickets.

- Label maps `wayfinder:map`.
- Label child tickets `wayfinder:research`, `wayfinder:prototype`, `wayfinder:grilling`, or `wayfinder:task`.
- Prefer GitHub sub-issues and native issue dependencies.
- If those features are unavailable, use task lists, `Part of #<map>`, and `Blocked by: #<n>` lines.
- Claim work with `gh issue edit <number> --add-assignee @me`.
- Resolve work by commenting with the answer, closing the child issue, and recording a context pointer in the map's Decisions-so-far section.
