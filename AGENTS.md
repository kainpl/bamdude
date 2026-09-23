# AGENTS.md

The guide for coding agents working in this repository is **[`CLAUDE.md`](CLAUDE.md)**.
The name is Claude Code's convention; the content is tool-agnostic — read it
first whatever assistant you are. It covers the architecture, the commands that
actually verify a change, the invariants that must not be broken, and the
checklist for adding an endpoint, a column, a permission or a translation.

The five things that most often go wrong for an agent here:

1. **Every user-facing string exists in `en` and `uk`** — frontend locales and
   backend `data/*_en.json` / `*_uk.json` alike. One language is a failing test.
2. **A schema change is a model edit AND a new migration** (`backend/app/migrations/mNNN_*.py`).
   Existing migrations are frozen after a release; never edit one.
3. **`npm run typecheck`, never bare `npx tsc --noEmit`** — the latter enters no
   file and exits 0 on anything.
4. **Every new `Permission` lands in three places** (`core/permissions.py`, the
   API-key scope map in `core/auth.py`, and a seed for Administrators in the
   migration) — a drift-guard test fails otherwise.
5. **Tests are run, not described.** `ruff check backend/`,
   `python -m pytest backend/tests/ -n 4 --timeout=300 --timeout-method=thread`
   from the repo root (CONTRIBUTING.md explains why both the directory and the
   working directory matters)
   (or the targeted file), `npm run lint && npm run typecheck && npm run test:run`
   — and the CHANGELOG entry goes in the same change.

`CONTRIBUTING.md` has the setup, the CI checks and the house rules, including
a section on working with an AI assistant.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

When the user types `/graphify`, use the installed graphify skill or instructions before doing anything else.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- Dirty graphify-out/ files are expected after hooks or incremental updates; dirty graph files are not a reason to skip graphify. Only skip graphify if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
