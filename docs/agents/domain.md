# Domain docs

## Layout

This repository uses a single context:
- `CONTEXT.md` at the repository root.
- Architecture decision records under `docs/adr/`.

## Before exploring

Read root CONTEXT.md when present and ADRs relevant to the work.
Continue to follow AGENTS.md's existing prerequisite reading.

If domain files do not exist, proceed silently. Do not create
placeholder context or speculative decisions during setup.
The domain-modeling skill creates these documents lazily as
terms and decisions are resolved.

## Vocabulary and decisions

Use domain terms defined in CONTEXT.md rather than conflicting
synonyms. If a necessary concept is missing, consider whether it
is a genuine glossary gap for domain-modeling.

Explicitly flag proposals that contradict an existing ADR;
do not silently override the decision.
