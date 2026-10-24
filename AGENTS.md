# Agent collaboration guide

This file defines how AI agents collaborate on Vitals. The local LLM running
through Pi is the project coordinator. Cloud agents running in Codex and Claude
contribute implementation, research, review and documentation work, with the
coordinator retaining the project-wide view.

## Roles

### Pi coordinator

- Own the task queue and keep the GitHub Project priorities, owners, status and
  dependencies current. Break broad requests into reviewable tasks and assign
  each task a clear outcome.
- Track progress across Agent Broker conversations. Record each task's owner,
  branch or pull request, current status, blockers and next action. Ask agents
  for updates when a handoff or dependency needs resolving.
- Provide Codex and Claude with shared context, relevant issue/spec links,
  acceptance criteria and any file ownership boundaries. Combine their input
  and resolve conflicting recommendations against the project documents and
  user direction.
- Keep GitHub Issues as the bug and task record, the GitHub Project as the
  prioritized work queue, pull requests as the review and merge path, and the
  repository Wiki as published project documentation. Link records to one
  another where useful.
- Review completed work for scope, integration and documentation consistency;
  ensure accepted decisions and durable project guidance are reflected in the
  repository or Wiki.

### Codex agent

- Take clearly scoped implementation, debugging, test, documentation or code
  review tasks from the coordinator.
- Work on the assigned branch, follow the acceptance criteria, and avoid
  unrelated edits. Check the existing specs and tests before changing behavior.
- Report the files changed, commands or checks run, results, remaining risks,
  and any follow-up issue needed. Raise blockers promptly instead of silently
  changing scope.

### Claude agent

- Take clearly scoped implementation, research, design review, test, or
  documentation tasks from the coordinator. When possible, provide an
  independent review of work prepared by another agent.
- Ground recommendations in the repository, project specs and verifiable
  evidence. Mark assumptions and distinguish confirmed behavior from proposed
  behavior.
- Report findings, changed files or sources, checks performed, remaining
  questions and blockers to the coordinator.

### Additional agents

- Follow the same task boundaries and reporting expectations. The coordinator
  assigns a specific deliverable and identifies whether the agent may edit
  files or should return findings only.

## Coordination through Agent Broker

- Treat Pi as the single source of task assignment and priority. Cloud agents
  provide input and execute delegated work; they do not independently reprioritize
  the project or assign conflicting work.
- Each delegated task should state its goal, priority, owner, relevant GitHub
  issue, expected deliverable, acceptance criteria, branch, dependencies and
  whether it is read-only or may edit files.
- Keep one active editor per task and coordinate shared-file edits before
  starting. Independent investigations and reviews may run concurrently.
- Send concise progress updates at meaningful milestones: started, findings,
  implementation complete, checks complete, and blocked. Include links or
  commit/PR references when available.
- Return decisions and useful findings to Pi through Agent Broker so they can
  be captured in the relevant GitHub issue, project item or documentation.
  Do not treat unrecorded chat as the only record of a project decision.
- If two agents disagree, present the evidence and trade-offs to Pi. The
  coordinator resolves the project decision, following the user's direction.

## Git and GitHub workflow

- Use the GitHub repository as the canonical source for code and versioned
  documentation. Keep the default branch releasable.
- Start work from an up-to-date base branch and use a focused branch named for
  its issue or task, such as `issue-42-battery-tooltip`. Do not commit directly
  to the default branch unless the coordinator explicitly assigns that workflow.
- Keep changes focused and make commits understandable. Do not rewrite or
  discard another agent's work. Coordinate before editing files already owned
  by another active task.
- Open a pull request for review and link its issue. Include a short description,
  user-visible behavior, checks performed and any known limitation. The
  coordinator handles integration and confirms the GitHub Issue and Project
  status after merge.
- Use GitHub Issues to report bugs with reproduction steps, expected and actual
  behavior, environment details and relevant logs. Use issues for planned work
  with a clear outcome and acceptance criteria.
- Keep priorities and workflow status in the GitHub Project current. The
  coordinator owns prioritization; agents may recommend priority changes with
  a reason.
- Put durable user and contributor documentation in the GitHub Wiki when it is
  intended to be browsed separately. Keep implementation guidance, specs and
  decisions needed alongside code in the repository, and link related Wiki
  pages where appropriate.

## Project-specific working agreements

- Treat `docs/BRIEF.md`, `docs/SPEC.md` and `docs/DESIGN.md` as the current
  product and technical context. If code and these documents disagree, report
  the mismatch and ask Pi to identify the intended behavior before broadening
  the task.
- Preserve Vitals' modular daemon/client architecture, Linux and Hyprland
  constraints, Waybar integration and low-resource goals described in the
  project documents.
- Prefer changes that fit the existing Python package and systemd user-service
  model. Avoid adding dependencies without explaining their runtime and
  packaging impact.
- Keep hardware and desktop-environment access behind clear boundaries so
  collectors and controls can be tested with fixtures or substitutes.
- Before reporting a task complete, summarize the result and checks actually
  performed. Do not claim a check passed if it was not run.
