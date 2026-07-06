# GitLab Forge Support Implementation Plan

## Goal

Allow one Foundry deployment to run the existing issue-to-change pipeline against
either GitHub or GitLab, including issue polling, human-input comments, repository
cloning and pushes, pull/merge request creation, issue closing, review feedback,
CI failure handling, API projection, and provider-correct UI links.

GitHub remains the default and must retain its current behavior. GitLab.com and
self-managed GitLab instances are supported through `glab`.

## Current State

- `src/foundry/stages/fetch.py`, `issue_comment.py`, and `pr.py` invoke `gh`
  directly and parse GitHub-specific JSON.
- `src/foundry/workflows.py` owns GitHub PR listing, review/check parsing,
  retry behavior, PR comments, and feedback deduplication.
- `src/foundry/worktree.py` clones through `gh repo clone`. Both development and
  PR-feedback workflows currently pass `SOURCE_REPO`, although code worktrees
  must come from `TARGET_REPO` when source and target differ.
- `Task` and SQLite retain issue fields and `pr_url`; the API and UI expose those
  names. `TaskDetails.tsx` constructs a `github.com` issue URL directly.
- Docker installs and mounts authentication for `gh` only. The entrypoint
  configures only the GitHub credential helper.
- The pipeline is polling-based. PR feedback is deduplicated by hashing formatted
  feedback in `repo_memory`.

## Assumptions

- A deployment and its SQLite database use one forge and one hostname at a time.
  Cross-forge source/target combinations are out of scope.
- `SOURCE_REPO` and `TARGET_REPO` may be different projects on the same forge and
  host. Issue actions target the source; clone, push, CI, and change-request
  actions target the target.
- `FORGE_PROVIDER` accepts `github` or `gitlab` and defaults to `github`.
  GitHub uses `GH_HOST` (default `github.com`); GitLab uses `GITLAB_HOST` or
  `GL_HOST` (default `gitlab.com`). Existing CLI token/config behavior remains
  authoritative (`GH_TOKEN`/`GITHUB_TOKEN`, `GITLAB_TOKEN`, or mounted config).
- No Python API client dependency is added. GitHub uses `gh`; GitLab uses
  `glab api` for stable REST JSON and `glab repo clone` for protocol-aware clone.
- Existing internal/API names `Stage.PR`, workflow `pr_feedback`, and `pr_url`
  remain for compatibility. Provider-neutral types call the external object a
  change request; the UI renders `PR` or `MR`.
- Preserve the current behavior of explicitly closing the source issue after a
  PR/MR opens. Do not change it to close-on-merge in this feature.
- GitLab actionable feedback is provider-neutralized from unresolved resolvable
  MR discussions and a failed/canceled pipeline for the current MR head. Missing
  approvals alone are not actionable, avoiding a dependency on paid-tier
  approval rules.
- Polling remains the trigger mechanism; GitLab webhooks are out of scope.

## Files

- `src/foundry/forges/__init__.py` — export forge types and select one provider
  from validated settings.
- `src/foundry/forges/base.py` — typed normalized dataclasses, provider protocol,
  and shared retry/error-boundary helpers.
- `src/foundry/forges/github.py` — move current `gh` integration and normalize
  GitHub issues, PRs, reviews, comments, and checks.
- `src/foundry/forges/gitlab.py` — implement GitLab project-path encoding,
  pagination, issue/MR operations, discussions, pipelines, and clone.
- `src/foundry/models.py` — add `ForgeKind` as `StrEnum` and persisted forge,
  host, and source issue URL metadata to `Task`.
- `src/foundry/config.py` — parse and validate provider/host configuration while
  retaining GitHub defaults.
- `src/foundry/state.py` — add an idempotent SQLite migration for new task
  metadata and persist/read the fields without losing existing rows.
- `src/foundry/pipeline.py` — create/inject one provider per run and manual issue
  execution.
- `src/foundry/stages/fetch.py` — consume normalized issues; retain queue,
  priority, restart, and manual-run behavior.
- `src/foundry/stages/issue_comment.py` — publish human-blocking comments through
  the selected provider.
- `src/foundry/stages/pr.py` — keep git commit/push and sanity checks local, but
  delegate change creation and source-issue closing to the provider.
- `src/foundry/worktree.py` — delegate initial clone to the provider and keep
  existing git synchronization/worktree behavior.
- `src/foundry/workflows.py` — consume normalized changes and feedback, remove
  GitHub response parsing, post feedback completion comments through the
  provider, and deduplicate a canonical feedback fingerprint.
- `src/foundry/cli.py` — make command help/output forge-neutral while retaining
  existing command names.
- `src/foundry/agents/prompts/implement.md` — replace GitHub-only task wording.
- `src/api/main.py` — select the configured provider for manual fetch and remove
  GitHub-only endpoint wording.
- `src/api/projections.py` — expose `forge`, `forge_host`, `issue_url`, and
  `change_kind` without changing existing fields.
- `web/src/api.ts` — mirror the extended strict API contract.
- `web/src/components/TaskDetails.tsx` — use backend-provided issue URLs and
  render PR/MR labels.
- `web/src/components/Topbar.tsx` — replace GitHub-only pull text.
- `web/src/stages.ts` — use provider-neutral change-request stage wording.
- `Dockerfile` — install a pinned `glab` binary alongside `gh`.
- `docker-compose.yml` — document/mount GitLab CLI authentication for worker and
  feedback services.
- `docker/entrypoint.sh` — configure the selected CLI as the HTTPS git credential
  helper without printing tokens.
- `.env.example` — document GitHub, GitLab.com, and self-managed GitLab settings.
- `scripts/add-and-process.sh` — select `gh` or `glab` for the smoke workflow.
- `README.md`, `docs/ARCHITECTURE.md`, and
  `docs/specs/observability-ui.md` — document forge selection, authentication,
  MR feedback semantics, normalized event/API fields, and limitations.
- `tests/test_forge_github.py` — GitHub adapter contract and response mapping.
- `tests/test_forge_gitlab.py` — GitLab API command construction, nested project
  paths, pagination, mapping, discussions, pipelines, comments, and errors.
- `tests/test_config.py`, `tests/test_state.py`, `tests/test_fetch.py`,
  `tests/test_pr_sanity.py`, `tests/test_pr_feedback.py`,
  `tests/test_worktree.py`, `tests/test_projections.py`, and `tests/test_api.py`
  — update integration contracts and preserve regression coverage.

The plan touches 39 files, including four new implementation files and two new
test modules, staying below `MAX_FILES_PER_PR`.

## Implementation Steps

1. Define the normalized forge contract.
   - Add `ForgeIssue`, `IssueQuery`, `ForgeChange`, `ChangeRequestInput`,
     `FeedbackItem`, `CheckResult`, and `ChangeFeedback` as frozen dataclasses.
   - Give `ForgeChange` provider-independent fields: number, title, branch, URL,
     and head SHA when available.
   - Give feedback items stable external IDs. Compute a deterministic fingerprint
     from sorted actionable item/check IDs and states, rather than formatted text.
   - Define provider methods for list/get issue, issue comment/close, clone,
     create/list change, load feedback, and change comment.
   - Keep subprocess retries at this external boundary. Retry only recognized
     timeout/connection/rate-limit failures; propagate authentication, validation,
     and malformed-response failures immediately.

2. Add configuration and compatible persistence.
   - Parse `FORGE_PROVIDER` case-insensitively and fail fast for other values.
   - Derive the active host from the provider-specific official environment
     variable and normalize away URL schemes/trailing slashes for stored identity.
   - Default newly constructed test/tasks to GitHub metadata so existing callers
     remain source-compatible.
   - Extend the fresh schema and add a `PRAGMA table_info(tasks)` migration that
     adds `forge`, `forge_host`, and nullable `issue_url` columns to old databases.
     Legacy rows become `github`/`github.com`; do not rewrite IDs or event links.
   - Persist fetched issue URLs. Do not put tokens or CLI configuration in SQLite
     or event payloads.

3. Extract current GitHub behavior into `GitHubProvider`.
   - Move all `gh issue`, `gh pr`, and `gh repo clone` command construction and
     GitHub-shaped JSON parsing out of stages/workflows/worktree.
   - Preserve label intersection, assignee, milestone, issue limit, base branch,
     explicit issue close, branch prefix, and current review/check semantics.
   - Normalize `CHANGES_REQUESTED`, failing `statusCheckRollup` entries, and
     contextual comments into `ChangeFeedback`.
   - Lock this extraction with adapter tests before changing orchestration.

4. Implement `GitLabProvider`.
   - URL-encode full `group/subgroup/project` paths for `/projects/:id` endpoints.
   - List opened issues with labels, assignee username, milestone, and bounded
     pagination; normalize GitLab string labels and `web_url`.
   - Fetch one issue by IID; create issue notes; close with `state_event=close`.
   - Clone through `glab repo clone`, allowing the CLI config to choose SSH or
     HTTPS.
   - Create an MR through the API with source branch, target branch, title, and
     description; normalize `iid`, `web_url`, source branch, and head SHA.
   - List opened MRs and filter `foundry/task-*` source branches.
   - Read MR discussions and pipelines. Include unresolved resolvable,
     non-system notes as requested changes. Treat only failed/canceled pipelines
     matching the MR head as failing CI; ignore running, skipped, and old-head
     pipelines.
   - Post automation status as a regular non-resolvable MR note so it does not
     create a new merge-blocking discussion.
   - Validate empty/malformed JSON and missing required fields with errors that
     include operation/project context but never token values.

5. Rewire the pipeline around provider injection.
   - Create one provider per polling/manual pass and pass it into fetch and
     development/feedback workflows.
   - Convert normalized issues to `Task` while preserving priority sorting and
     SQLite requeue/resume behavior.
   - Route blocked-task comments, change creation, issue closing, open-change
     discovery, feedback retrieval, and completion comments through the provider.
   - Make `_build_pr_feedback_input` and logging say change request while retaining
     persisted workflow/stage keys.
   - Replace `_gh_run_with_retry`, `_list_open_foundry_prs`,
     `_view_pr_feedback`, GitHub dict access, and `_format_pr_feedback` with
     normalized equivalents.
   - Keep event payloads compact and add `forge`, `change_number`, `change_url`,
     and `feedback_fingerprint`; do not persist raw full API responses.

6. Correct repository ownership in worktree flows.
   - Pass `TARGET_REPO`, not `SOURCE_REPO`, to both development and feedback base
     repository preparation.
   - Delegate only the initial clone to the provider; subsequent fetch, checkout,
     reset, worktree, commit, and push commands remain ordinary git.
   - Keep source issue comments/closure addressed by `task.repo`.
   - Add regression coverage proving distinct source and target repositories clone
     and push the target while closing/commenting the source.

7. Extend API and UI without breaking consumers.
   - Project `forge`, `forge_host`, persisted `issue_url`, and `change_kind`
     (`PR`/`MR`) alongside existing `repo`, issue fields, and `pr_url`.
   - Ensure legacy rows with no stored URL receive a safe provider-derived URL at
     projection time.
   - Update TypeScript types, issue links, change labels, pull tooltip, and stage
     title. Do not construct GitHub/GitLab URLs in React.
   - Preserve event replay, stage aliases, filtering, search, and existing API
     endpoints.

8. Add runtime packaging and operator documentation.
   - Install a pinned `glab` release in the backend image for reproducible builds.
   - Support mounted `~/.config/glab-cli` credentials and token-based
     `GITLAB_TOKEN`; document SSH key mounting when SSH clone/push is selected.
   - Configure the HTTPS credential helper for the active provider in the
     entrypoint and verify that no command line includes an access token.
   - Document required GitLab token scopes/roles, self-managed hostname setup,
     nested project paths, source/target behavior, and the one-forge-per-database
     limitation.
   - Make the smoke script create/list an issue through the configured provider.

9. Complete contract and regression tests.
   - Use mocked `shell.run` results only; unit tests must not contact either forge.
   - Add matching provider contract scenarios for issue lifecycle, clone, change
     lifecycle, feedback, network retry, auth failure, and malformed output.
   - Add GitLab-specific scenarios for nested groups, pagination, string labels,
     unresolved/resolved discussions, old/current pipeline SHAs, and self-managed
     hosts.
   - Add migration coverage starting from the legacy `tasks` schema and verify
     task IDs/status/stage/URLs/events survive repeated `init_db` calls.
   - Update workflow tests to assert normalized inputs and stable feedback
     deduplication, including that Foundry's own status note does not cause a
     repeated implementation pass.

## Verification

- `uv run pytest tests/test_forge_github.py tests/test_forge_gitlab.py tests/test_config.py tests/test_state.py tests/test_fetch.py tests/test_pr_sanity.py tests/test_pr_feedback.py tests/test_worktree.py tests/test_projections.py tests/test_api.py`
- `uv run ruff check .`
- `uv run pytest`
- `npm --prefix web run build`
- `npm --prefix web run lint`
- Manual GitHub smoke: existing `foundry run-issue <number>` flow still opens a
  PR, closes/comments the source issue, and shows correct links.
- Manual GitLab smoke on a disposable project: `FORGE_PROVIDER=gitlab` fetches a
  labeled issue, pushes `foundry/task-*`, opens an MR, closes/comments the source
  issue, displays GitLab links, detects one unresolved discussion or failed
  pipeline, pushes one follow-up commit, comments once, and skips unchanged
  feedback on the next pass.
- Repeat the GitLab smoke against a self-managed hostname or a local test instance
  before declaring self-managed support complete.

## Risks and Edge Cases

- `glab` output and authentication behavior vary by version; pin the Docker
  version and keep parsing limited to documented REST fields.
- GitLab project IIDs are project-local and nested namespaces require URL
  encoding. Never treat the numeric IID as a global project object ID.
- GitLab labels are strings while GitHub labels are objects. Priority sorting
  must operate only on normalized label names.
- GitLab can expose several pipelines for an MR. Acting on an old commit's failed
  pipeline would create an unnecessary fix; match the current head SHA.
- General non-resolvable MR notes are not automatically actionable in the first
  release. Reviewers must use a resolvable discussion for code-change requests.
- A bot status note must not alter the actionable feedback fingerprint or create
  a resolvable thread, otherwise feedback polling can loop.
- Source and target projects can differ, so shorthand issue-closing syntax in the
  PR/MR body is unreliable. The provider must explicitly close/comment the source
  issue and include its full URL in the body.
- Existing databases default legacy task metadata to GitHub. Operators switching
  a deployment to GitLab should use a separate database as required by the
  one-forge-per-database assumption.
- HTTPS pushes require a working CLI credential helper; SSH pushes require keys
  and known-host configuration in the container. Both paths need smoke coverage.
- GitLab API rate limits and transient errors must not be mistaken for empty
  queues or absent feedback.
- Keep provider responses and subprocess errors scrubbed of credentials before
  writing append-only events or logs.
