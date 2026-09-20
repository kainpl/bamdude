# Copilot instructions

The guide for coding agents in this repository is [`CLAUDE.md`](../CLAUDE.md)
(tool-agnostic despite its name); [`AGENTS.md`](../AGENTS.md) is the short
form. Read `CLAUDE.md` before the first change: it holds the architecture,
the invariants that must not be broken, the commands that actually verify a
change (`npm run typecheck`, never a bare `npx tsc --noEmit`), and the
checklists for adding an endpoint, a column, a permission or a translation
(every user-facing string exists in `en` and `uk`).

`CONTRIBUTING.md` → *Working with an AI assistant* has the house rules for an
agent-made pull request.
