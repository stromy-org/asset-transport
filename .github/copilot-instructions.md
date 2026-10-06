<!--
  GENERATED FILE — DO NOT EDIT.
  Source of truth: AGENTS.md (cross-vendor standard).
  Override file:   .agent-overrides/copilot.md (optional, appended below)
  Regenerate with: scripts/render-agent-md.py
-->

# AGENTS.md

Self-contained instructions for Codex and other AI agents working on stromy-asset-transport.

> **AGENTS.md is the canonical instruction file** for this repo (cross-vendor standard).
> `CLAUDE.md` and `.github/copilot-instructions.md` are generated from this file by
> `scripts/render-agent-md.py`. Gemini CLI reads this file directly via
> `context.fileName: ["AGENTS.md"]` in `.gemini/settings.json`. **Do not hand-edit
> the generated files.**

<!-- org-core:start v1 GENERATED from global-skills/global-instructions/org-core.md by stromy-org scripts/render-org-core.py; edit the source, not this copy -->
## Org core — how work lands in this org

This repository belongs to the stromy-org fleet. These rules apply here whichever agent you are and wherever this clone lives. The machine rules (git safety, worktrees, commits, `.env`) come from your user-level instructions; this block does not repeat them.

- **The control plane is `stromy-org/stromy-org`.** Its `catalog.json` is the inventory of every repo. **Work that spans more than one repo starts from a stromy-org root checkout, not from here.** That includes a parent submodule pointer, a shared template or skill, and a fleet-wide check.
- **Land through a branch and a PR** (`gh pr create`, or `scripts/sync.sh land` from the stromy-org root). A pushed branch without a PR is parked, not landed. **Every PR you mention carries its full URL** (`https://github.com/<owner>/<repo>/pull/<n>`), never only `#<n>`.
- **Parent submodule pointers are bot-owned.** When this repo is a stromy-org submodule, push its branch and stop. Never hand-edit or stage the parent's gitlink.
- **Merging a `deploy_on_main` repo deploys it.** `catalog.json` marks those repos. A merge needs an explicit deploy decision: the reviewed route (`premerge_review.py`, then `merge-reviewed.sh`, both run from the stromy-org root) or an operator yes for that specific PR. It is never a wrap-up side effect. Merge over a red check only when the failure is proven to already exist on `main`.
- **Every commit goes through the `conventional-commit` skill**: Conventional Commits with gitmoji.
- **Backlog and plans:** track work with the `backlog` skill against this repo's own backlog, and plan with the `plan-*` skills. Never hand-edit a rendered `BACKLOG.md` or `DONE.md` where a renderer owns it.
- **Generated files are never hand-edited.** `CLAUDE.md` and `.github/copilot-instructions.md` are rendered from `AGENTS.md` (`scripts/render-agent-md.py`). This block is rendered from the org-core source in `global-skills`, and `scripts/check-org-core.py --check` verifies it. To change it, edit the source in `global-skills` and re-render; never edit the copy here.
- **Client confidentiality:** never name a client, its end clients, projects or people in anything another client can read.
- **Contract:** `infra-docs/ai/instruction-distribution.md` defines which instructions reach which agent, and the budgets.
<!-- org-core:end -->

## Project Overview

Domain-agnostic content-addressed asset store and outbound delivery ladder shared by Stromy MCP servers

## Commands

```bash
uv sync
uv sync --extra all              # All optional extras
uv run pytest -v
uv run ruff check src/
uv run pyright src/stromy_asset_transport/
```


## Notes

This is a minimal-surface utility lib. See README.md for module structure and usage.


## Agent-md rendering

`AGENTS.md` is the only authored agent-instruction file. Regenerate the rest:

```bash
python3 scripts/render-agent-md.py            # CLAUDE.md + .github/copilot-instructions.md
python3 scripts/render-agent-md.py --check    # exit 1 if stale
```

**Never hand-edit** `CLAUDE.md` or `.github/copilot-instructions.md` — they carry a "GENERATED FILE" banner; edits are wiped on next render.

## Commit Standards

- Conventional Commits with gitmoji
- Every commit via the `conventional-commit` skill (machine-wide)
- Co-Authored-By trailer on AI-assisted commits

## Skill Workflow

- **Commits**: `/conventional-commit`
- **Library maintenance**: `/python-library-maintain` (in-satellite — bump version, tag release, refresh AGENTS, sync optional extras)
- **New skills (rare for libs)**: `/skill-creator`
