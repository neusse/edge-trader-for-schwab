# Domain Docs

This is a single-context repository.

## Before exploring

Read these when they exist:

- `GLOSSARY.md` at the repository root.
- ADRs under `docs/adr/` that affect the area being changed.

If either is absent, proceed silently. Domain-modeling workflows create them lazily when terminology or decisions are resolved.

## Layout

```
/
|-- GLOSSARY.md
|-- docs/
|   |-- adr/
|   `-- agents/
`-- companion/
```

## Use the glossary's vocabulary

Use terms as defined in `GLOSSARY.md` in issue titles, proposals, hypotheses, tests, and code. Avoid synonyms the glossary explicitly rejects.

If a required concept is missing, reconsider whether the project already uses another term or note the gap for domain modeling.

## Flag ADR conflicts

If proposed work conflicts with an existing ADR, state the conflict explicitly instead of silently overriding the decision.
