# Domain Docs

This repo uses a single-context layout: `CONTEXT.md` at the repo root and ADRs under `docs/adr/`.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root.
- **`docs/adr/`**: read ADRs relevant to the area you are about to work in.

If these files do not exist, proceed silently. Domain documentation is created lazily by `/domain-modeling` when terms or decisions are resolved.

## Use the glossary's vocabulary

When naming a domain concept in an issue, proposal, hypothesis, or test, use the term defined in `CONTEXT.md`.

If the concept is missing, reconsider whether it belongs to the project's vocabulary; record a real gap for `/domain-modeling`.

## Flag ADR conflicts

If a proposal contradicts an existing ADR, identify the ADR and explain why its decision should be reconsidered.
