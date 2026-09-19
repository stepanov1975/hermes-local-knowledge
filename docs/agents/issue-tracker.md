# Issue tracker: GitHub

Issues and specs live in GitHub Issues for
`stepanov1975/hermes-local-knowledge`. Use the `gh` CLI.

## Conventions

Use `--repo stepanov1975/hermes-local-knowledge` explicitly.

- Create: `gh issue create --title "..." --body-file <file>`
- Read: `gh issue view <number> --comments`; also inspect labels.
- List: `gh issue list --state open`, with appropriate label filters.
- Comment: `gh issue comment <number> --body-file <file>`
- Label: `gh issue edit <number> --add-label "..." --remove-label "..."`
- Close: `gh issue close <number> --comment "..."`

Search existing issues and PRs before creating duplicates.
Read the relevant ticket and comments before acting.
Follow CONTRIBUTING.md and SECURITY.md; never publish private
telemetry, transcripts, credentials, or private source documents.

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub shares issue and PR numbers. Resolve an ambiguous reference
with `gh pr view <number>`, falling back to `gh issue view <number>`.
An explicitly requested PR remains eligible for review.

## Skill operations

“Publish to the issue tracker” means create a GitHub issue.
“Fetch the relevant ticket” means read the issue and its comments.

For wayfinding:
- Use one `wayfinder:map` issue for the map.
- Link child tickets as GitHub sub-issues, or use a task list and
  `Part of #<map>` references when sub-issues are unavailable.
- Use `wayfinder:research`, `wayfinder:prototype`,
  `wayfinder:grilling`, or `wayfinder:task` as appropriate.
- Prefer native issue dependencies; otherwise record
  `Blocked by: #<number>` references.
- Select unassigned, unblocked open children in map order.
- Claim by assigning the driving developer.
- Resolve with an answer comment, close the child, and add a
  decision pointer to the map.
