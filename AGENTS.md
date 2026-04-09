# XiangShan Agent Guide

This repository uses local agent instructions. When working in `src/test/python/MemBlock/`, agents must treat the MemBlock collaboration documents as part of the task contract.

When working in `src/test/python/`, agents must read:
- `src/test/python/AGENTS.md`

## MemBlock Rule

If the user request touches any file under `src/test/python/MemBlock/`, or the task is clearly about the MemBlock Python verification environment, the agent must read:

- `src/test/python/MemBlock/ROLES.md`

before planning or editing code.

## MemBlock Role Workflow

For MemBlock tasks, the agent must follow this startup sequence:

1. Read `src/test/python/MemBlock/ROLES.md`.
2. Infer the most likely primary role from the requested work.
3. In the first task-facing response, tell the user which role is being assumed, using either the English or Chinese codename, but always include the formal role name in parentheses.
4. If the role inference is ambiguous, ask the user which role should be used before continuing.
5. If the task later crosses into another role's core ownership, explicitly say that a role switch is needed before broadening the patch.

The current MemBlock default roles are:

- `Pathfinder` / `探路者` -> `testcase/sequence`
- `Bridgekeeper` / `守桥人` -> `env/monitor/facade`
- `Oracle` / `神谕者` -> `model/coverage`
- `Captain` / `船长` -> `integrator/owner`

Examples of the required first-response style:

- `我将按 船长（integrator/owner）角色开展工作。`
- `我将按 神谕者（model/coverage）角色开展工作。`
- `I will work as Captain (integrator/owner).`
- `I will work as Oracle (model/coverage).`

## MemBlock Working Expectations

When acting on a MemBlock task, the agent should also preserve these expectations:

- prefer real DUT validation over mock-only proof
- prefer existing facade, sequence, monitor, and model layers over test-local shortcuts
- keep testcase, model, and project-entry doc changes separated when practical
- append to `src/test/python/MemBlock/CHANGELOG.md` instead of rewriting older entries
- treat `src/test/python/MemBlock/docs/coverage_summary.md` and `src/test/python/MemBlock/docs/coverage_todo.md` as the shared coverage-status source

If there is any conflict between this file and the more specific MemBlock role document, follow `src/test/python/MemBlock/ROLES.md` for MemBlock work.
