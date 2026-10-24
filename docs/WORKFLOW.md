# Contribution workflow (human + cloud agents)

Vitals is built with help from cloud coding agents (Claude, Codex) reached
through the local agent-broker. The rules below are how we keep that safe
and reviewable.

## Repo layout

- `main` — always working. **Nothing merges without an agent review.**
- One branch per unit of work: `feat/<slug>`, `fix/<slug>`, `chore/<slug>`.
- Every branch maps to a GitHub issue; every PR links `Closes #N`.

## The loop

1. **Issue** — file a feature request / bug on the GitHub project
   (board: "Vitals"). Assign it a milestone (v0.1, v0.2, backlog).
2. **Spec/design (if large)** — delegate a spec or design doc to Claude
   or Codex via agent-broker; orchestrator lands it under `docs/`.
3. **Implement** — small, well-understood changes may be coded by the
   orchestrator agent; larger work is delegated. Work happens on the
   branch. Tests are expected for collector/protocol logic.
4. **Review (required)** — before merge, the *full branch diff* is
   reviewed by a cloud agent that is **not** the one that wrote it:
   - generate `.review/<branch>.diff` (`git diff main...<branch>`),
   - delegate review with the diff + affected files as context,
   - reviewer returns findings classified as `blocker / major / minor / nit`,
   - orchestrator arbitrates: fixes blockers/majors on the branch (or
     justifies rejection in the PR), pushes fixes, re-runs tests.
5. **Merge** — squash-merge the PR once: tests pass, smoke script passes,
   review findings are resolved, and the orchestrator posts the review
   summary as a PR comment (the human-facing audit trail).

## Agent constraints (agent-broker)

- Workers are **read-only** (no writes, no network for codex, no shell for
  claude). They review and design; they never push.
- Reviews must cite file:line. Findings are adopted only after the
  orchestrator verifies them against the code.

## Definition of done (per issue)

- [ ] Unit tests pass (`python3 -m unittest discover -s tests`)
- [ ] `tests/smoke.sh` passes (bounded end-to-end check)
- [ ] Agent review complete, blockers/majors resolved or documented
- [ ] PR merged, issue closed, board status updated
