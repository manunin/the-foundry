# UI Agent Deploy and Crawler Testing Implementation Plan

## Goal

Allow an issue marked `ui-agent-test` to opt into UI-aware planning and a
post-implementation quality gate that deploys the task worktree to the target
repository's Mac mini stand, runs browser crawler scenarios, displays structured
results and screenshots in Foundry's stage UI, and feeds application/browser
diagnostics into the next implementation attempt when a crawler scenario fails.

Unlabelled tasks retain the current pipeline and do not require SSH, browser
tools, deployment skills, or remote infrastructure.

## Current State

- Forge adapters already normalize issue labels in `ForgeIssue`, but
  `stages/fetch.py` drops them when constructing `Task`. SQLite therefore cannot
  make label-dependent decisions after FETCH; GitHub's single-issue lookup also
  does not currently request labels.
- `workflows.dev_task` owns a resumable `IMPLEMENT -> VERIFY` loop. Stage results
  are keyed by `(task_id, stage, attempt)`, and retry input already combines the
  original plan, the previous implementation summary, and the verifier report.
- There is no `UI_TESTS` stage or UI-test agent role. The current agent roles and
  per-stage settings are PLAN, IMPLEMENT, and VERIFY.
- Target worktrees can contain repository-owned skills. The requested
  `.codex/skills/deploy-mac-mini-json-ui` skill is not present in this Foundry
  worktree and must be discovered/read from each target task worktree.
- The worker image contains `openssh-client`, but Compose does not mount SSH
  configuration, host keys, or private keys into the worker.
- Append-only `stage_started`/`stage_finished` events are the source of stage
  input/output in `api.projections`. Long strings are bounded, but binary
  artifacts are not persisted or served by the API.
- `web/src/stages.ts` defines a fixed timeline through VERIFY and PR.
  `StageIO.tsx` renders text/files/key-value payloads but has no crawler-report or
  image renderer.
- Existing documentation tells operators to create the queue label manually;
  Foundry does not mutate forge label definitions during startup.

## Assumptions

- `ui-agent-test` is an additive issue label, not a queue filter: issues still
  need the configured `ISSUE_LABEL(S)` to be fetched. Label matching is
  case-insensitive, while the documented canonical name is `ui-agent-test`.
- The label is created once by a repository administrator (GitHub or GitLab), as
  with the existing queue label. Foundry recognizes and persists it but does not
  require label-administration permissions or create remote labels at startup.
- The target repository owns the deploy procedure. For opted-in tasks it must
  contain `.codex/skills/deploy-mac-mini-json-ui/SKILL.md`; Foundry does not copy
  or hard-code that repository-specific deployment logic.
- The configured UI-tests agent has browser/crawler tooling available through
  its normal CLI configuration (for Codex, the mounted `.codex` configuration).
  Missing deploy skill, SSH material, browser capability, or an unreachable stand
  is an infrastructure failure requiring operator intervention, not a code retry.
- The planner describes concrete crawler scenarios, routes, assertions, and
  screenshot checkpoints. The UI-tests agent executes those scenarios after
  deployment and writes a machine-readable manifest plus screenshots under a
  Foundry-owned temporary directory in the worktree.
- The quality-gate order is `IMPLEMENT -> VERIFY -> UI_TESTS -> PR`: cheap local
  checks run before consuming the remote stand. A local VERIFY failure skips
  deployment. A crawler assertion failure consumes the same
  `MAX_IMPLEMENT_ATTEMPTS` budget and returns to IMPLEMENT.
- Only crawler/assertion failures are automatically retried. Deployment, SSH,
  malformed-manifest, browser-tool, and artifact-copy failures are classified as
  infrastructure failures and block for human action so code changes are not
  guessed from broken infrastructure.
- The first release uses one worker and one configured remote stand. Concurrent
  deployments to the same stand are out of scope; the stage must still identify
  runs by task and attempt so artifacts and logs never collide.
- API projections expose `ui_tests_enabled`; the React timeline omits UI_TESTS
  entirely for ordinary tasks rather than showing a misleading pending step.

## Files

- `src/foundry/models.py` — add `Stage.UI_TESTS` and persisted issue-label fields
  to `Task`.
- `src/foundry/state.py` — migrate existing SQLite databases, serialize labels,
  and include UI_TESTS in reset/resume invalidation order.
- `src/foundry/forges/github.py` — include labels in single-issue responses so
  manual runs use the same opt-in contract as polling.
- `src/foundry/stages/fetch.py` — preserve and refresh normalized labels on new
  and existing tasks and resume interrupted UI_TESTS work.
- `src/foundry/config.py` — expose the canonical label and bounded UI-test
  artifact/log settings without making UI infrastructure mandatory globally.
- `src/foundry/agents/base.py` and `src/foundry/agents/config.py` — add a typed
  UI_TESTS agent role and `AGENT_UI_TESTS_*` overrides.
- `src/foundry/agents/stub.py` — provide a deterministic passing UI_TESTS result
  for offline workflow tests.
- `src/foundry/agents/prompts/ui_tests.md` — require deploy-skill discovery,
  crawler execution, log capture, screenshots, and the result-manifest contract.
- `src/foundry/stages/ui_tests.py` — run the agent, validate/normalize its
  manifest, classify assertion versus infrastructure failures, bound logs, copy
  screenshots to durable storage, and remove temporary worktree artifacts.
- `src/foundry/workflows.py` — add UI-aware planning context and integrate the
  persisted UI_TESTS gate into the existing retry/resume loop.
- `src/foundry/pipeline.py` — treat UI_TESTS as a post-implementation stage for
  terminal failure handling.
- `src/api/main.py` — add a task-scoped artifact endpoint that serves only
  persisted UI-test files under the configured artifact root.
- `src/api/projections.py` — project UI_TESTS events and artifact URLs without an
  agent-stage alias, and expose the label-derived `ui_tests_enabled` flag.
- `web/src/api.ts` — add strict crawler-result and screenshot metadata types.
- `web/src/stages.ts` — insert the `ui_tests` timeline entry between VERIFY and
  PR.
- `web/src/components/StageIO.tsx` — render crawler totals, scenario results,
  log excerpts, and screenshot thumbnails/links in the Output tab.
- `web/src/components/StageDetailPanel.tsx` — treat UI_TESTS as an agent-backed
  stage with its live event stream.
- `web/src/styles.css` — add responsive crawler-result and screenshot-gallery
  styling.
- `docker-compose.yml` — mount an operator-selected SSH directory read-only into
  the worker and document the Codex/browser configuration dependency.
- `.env.example` — document the opt-in label, artifact/log limits,
  `HOST_SSH_DIR`, read-only known-host requirements, and UI-tests agent settings.
- `README.md` — document label creation for GitHub/GitLab, target-repo skill and
  crawler prerequisites, Docker SSH setup, execution semantics, and triage.
- `docs/ARCHITECTURE.md` — update the FSM, retry rules, persistence, and remote
  UI-test lifecycle.
- `docs/specs/observability-ui.md` — define the UI_TESTS event/output and artifact
  API/UI contracts.
- `tests/test_state.py` — cover label migration/round-trip and UI_TESTS
  invalidation.
- `tests/test_fetch.py`, `tests/test_forge_github.py`, and
  `tests/test_forge_gitlab.py` — cover polling/manual label preservation and
  refresh for both normalized forge shapes.
- `tests/test_agents_config.py`, `tests/test_agents_stub.py`, and
  `tests/test_agents_base.py` — cover the new role, overrides, stub, and prompt.
- `tests/test_ui_tests_stage.py` — cover manifest validation, failure
  classification, bounded diagnostics, safe artifact copying, and cleanup.
- `tests/test_workflows.py` and `tests/test_pipeline.py` — cover labelled and
  unlabelled flows, retry handoff, attempt exhaustion, blocking, and restart
  recovery.
- `tests/test_projections.py` and `tests/test_api.py` — cover stage output and
  authorized artifact delivery/path rejection.
- `tests/test_docker_compose.py` — cover the worker-only read-only SSH mount and
  absence of private-key content in image/environment definitions.

The planned change touches 35 files, below `MAX_FILES_PER_PR`.

## Implementation Steps

1. Persist the opt-in label as task identity.
   - Add `issue_labels: tuple[str, ...]` to `Task` and an `issue_labels_json`
     SQLite column with an idempotent `PRAGMA table_info(tasks)` migration;
     legacy rows default to an empty list and remain non-UI tasks.
   - Serialize only normalized label names. Reject malformed stored shapes by
     falling back to an empty tuple rather than failing every task projection.
   - Make GitHub `get_issue` request `labels` (GitLab already normalizes labels),
     pass labels through `_issue_to_task`, and refresh issue title/body/URL/labels
     while a task is still before PLAN. Freeze the labels once PLAN starts so
     adding/removing a label mid-execution cannot silently change its plan or
     quality gates; a full reset/fresh execution can adopt the new forge state.
   - Centralize case-insensitive `task_has_label(task, settings.ui_test_label)`;
     do not add `ui-agent-test` to the forge query, because that would exclude
     ordinary implementation tasks.
   - Add UI_TESTS to every exhaustive stage order/requeue/invalidation mapping.

2. Extend PLAN for opted-in tasks without forking the whole planner.
   - Append a clearly delimited UI crawler planning requirement to the existing
     repository context before `agent_plan_stage.run`; keep the OpenSpec prompt
     variant compatible by augmenting input rather than selecting a competing
     template.
   - Require the plan to read the target deploy skill and specify the stand URL
     discovery, user journeys, initial state/fixtures, viewport, assertions,
     browser-console/network failure rules, and screenshot checkpoints.
   - Tell the planner to return terminal `NEED_VERIFICATION` when the repository
     lacks the deploy skill or cannot identify a testable route/acceptance
     behavior. Unlabelled planner prompts remain byte-for-byte unchanged.
   - Record `ui_tests_enabled: true` in PLAN stage input so the UI/event history
     explains why the extra planning requirement was applied.

3. Define and implement the UI-tests execution contract.
   - Add `AgentStage.UI_TESTS`, its per-stage defaults/overrides, and a prompt that
     instructs the agent to read (not modify) the target deploy skill, deploy the
     current worktree, execute only the planned crawler cases, capture core/UI
     service logs and browser console/network logs, and take screenshots.
   - Require `.foundry/ui-tests/result.json` with a versioned schema containing
     overall status, deployed URL, scenario name/status/duration/error,
     screenshot relative paths, and relative paths for core/UI/browser logs.
     Paths must be relative; secrets/cookies/authorization headers must not be
     placed in the manifest or logs.
   - In `stages/ui_tests.py`, run the configured agent in the task worktree,
     validate the JSON and referenced paths, ensure every resolved source stays
     under `.foundry/ui-tests`, reject symlinks/path traversal/non-image
     screenshots, cap file count and byte size, and copy accepted artifacts to
     `DB_PATH.parent/artifacts/task-{id}/attempt-{attempt}`.
   - Normalize the stage result to the existing verification decision fields
     (`passed`, `retryable`, `requires_human`, `failure_kind`, `report`) plus
     structured `scenarios`, `screenshots`, `deployed_url`, and bounded
     `core_logs`, `ui_logs`, `browser_logs`. Assertion failures are
     `failure_kind=ui_crawler` and retryable; all execution/contract failures are
     `failure_kind=infra` and require human action.
   - Always delete `.foundry/ui-tests` after successful copying/parsing so test
     evidence cannot be committed into the product PR. Durable artifacts survive
     worktree cleanup and repeated API reads.

4. Insert UI_TESTS into the resumable quality-gate loop.
   - Keep local VERIFY first. If it passes and the task is opted in, load or run
     UI_TESTS for the same attempt using `stage_results`; if no label is present,
     preserve the existing immediate transition from VERIFY to PR.
   - Emit `stage_started` input with attempt, plan/scenario handoff, skill path,
     and artifact policy; emit `stage_finished` with the normalized crawler
     summary and cost/token metadata. Never place SSH key material, cookies, or
     full unbounded logs in events.
   - On UI assertion failure, build the next IMPLEMENT input from the original
     plan, prior implementation summary, failed scenario/assertion details, and
     bounded core/UI/browser log tails. This becomes the active quality report
     for the existing reset/checkpoint/retry path and consumes one shared
     implementation attempt.
   - On pass, continue to PR. On infrastructure failure, use the existing human
     block/comment path at `Stage.UI_TESTS`. On exhaustion, finish FAILED without
     opening a PR.
   - Resume safely after process death: saved VERIFY/UI_TESTS results are reused;
     an incomplete stage reruns the same attempt; durable artifact directories
     are replaced atomically per attempt rather than appended.

5. Make remote SSH configuration available without embedding credentials.
   - Add `${HOST_SSH_DIR:-./.docker/ssh}:/root/.ssh:ro` to the worker only. The
     host directory supplies `config`, private keys, and pre-populated
     `known_hosts`; readonly operation means the container must not rely on
     interactive host-key acceptance.
   - Keep SSH files out of the Docker build context, environment, SQLite, and
     event payloads. Document restrictive host permissions and a preflight
     `docker compose run --rm worker ssh -G <alias>` / non-interactive connection
     check using the alias required by the deploy skill.
   - Reuse the existing Codex config mount for browser MCP/tool configuration and
     document `AGENT_UI_TESTS_BACKEND/MODEL/TIMEOUT_SEC/MAX_TURNS`. Do not make a
     browser package a Foundry Python dependency when the configured agent owns
     crawler execution.

6. Expose durable evidence through API and UI.
   - Return artifact metadata as opaque task-scoped URLs, never filesystem paths.
     Add `GET /api/tasks/{task_id}/artifacts/{artifact_path:path}` that verifies
     the task exists, resolves beneath that task's artifact root, accepts only
     manifest-listed artifacts, and responds 404 to traversal, cross-task,
     missing, or unlisted paths.
   - Project UI_TESTS exactly like other stages, expose `ui_tests_enabled`, and
     update live projection/stage ordering so running, retry, failure, and
     completion render correctly. Derive each task's visible stage list from
     this flag so unlabelled tasks retain the current six-step timeline.
   - Add a typed crawler report renderer in the Output tab: aggregate pass/fail
     counts, deployed URL, per-scenario diagnostics, collapsible log excerpts,
     and lazy-loaded screenshot thumbnails linking to full images. Keep generic
     StageIO fallback behavior for older/unknown payloads.
   - Mark UI_TESTS as agent-backed for its event-stream tab, but omit the ask-agent
     composer unless the existing composer becomes functional in a separate
     feature.

7. Document and lock the full contract with tests.
   - Document one-time label creation (`gh label create ui-agent-test ...` and
     the GitLab equivalent), the fact that the queue label is still required,
     target skill placement, crawler/browser prerequisites, SSH mount layout,
     retry classifications, artifact retention, and log redaction expectations.
   - Add focused unit/integration tests using fake providers, agents, manifests,
     logs, and PNG files only; no test may contact a forge, Mac mini, SSH host, or
     browser service.
   - Assert the exact flows: unlabelled `IMPLEMENT -> VERIFY -> PR`; labelled
     pass `IMPLEMENT -> VERIFY -> UI_TESTS -> PR`; crawler fail feeds all three
     log classes into the next IMPLEMENT; infra failure blocks; exhausted
     crawler failures do not open PR; restart after saved UI_TESTS does not
     redeploy.
   - Test old-database migration, manual/polled label equivalence, changed label
     refresh, output projection, screenshot rendering contract, artifact
     traversal/cross-task rejection, and Docker credential non-disclosure.

## Verification

- `uv run pytest tests/test_state.py tests/test_fetch.py tests/test_forge_github.py tests/test_forge_gitlab.py tests/test_agents_base.py tests/test_agents_config.py tests/test_agents_stub.py tests/test_ui_tests_stage.py tests/test_workflows.py tests/test_pipeline.py tests/test_projections.py tests/test_api.py tests/test_docker_compose.py`
- `uv run ruff check .`
- `uv run pytest`
- `npm --prefix web run build`
- `npm --prefix web run lint`
- Manual unlabelled regression: process a disposable queue-labelled issue and
  confirm the timeline has no executed UI_TESTS event, no SSH/browser
  requirement, and the PR flow is unchanged.
- Manual labelled smoke: create/apply `ui-agent-test`, run against a disposable
  target repository containing the deploy skill, confirm the Mac mini stand is
  deployed from the task worktree, crawler scenarios run, and Output shows logs
  plus viewable screenshots before PR creation.
- Manual failure smoke: introduce a known UI regression, confirm the first
  crawler run fails, the next IMPLEMENT input contains the broken assertion and
  core/UI/browser logs, and a second deploy/crawl passes without committing
  `.foundry/ui-tests` artifacts.
- Container preflight: verify the configured SSH alias resolves and connects
  non-interactively from the worker while `/root/.ssh` remains read-only and no
  key content appears in `docker inspect`, task events, or API responses.

## Risks and Edge Cases

- Agent prose is not a test protocol. The versioned on-disk JSON manifest is the
  source of truth; missing/malformed manifests must never be interpreted as a
  passing crawl.
- Remote deployment is a side effect and can leave a stale stand after crashes.
  Runs must carry task/attempt identity, and the deploy skill should be
  idempotent. Automatic teardown is out of scope unless that skill defines it.
- A shared stand can produce cross-task interference. The initial single-worker
  assumption must be revisited with a lease/lock before enabling parallel task
  workers or PR-feedback deployments.
- Read-only SSH mounts cannot learn new host keys. Operators must provision
  `known_hosts` ahead of time; disabling host-key checking is not an acceptable
  workaround.
- Application/browser logs can contain credentials or personal data. Prompts
  require redaction, Foundry stores only bounded tails, and artifact/API tests
  must ensure raw SSH/config files cannot be referenced.
- Screenshots are binary and potentially large. Enforce MIME/extension, count,
  per-file, and per-attempt limits before copying; render them lazily so the task
  list and SSE payload remain small.
- Removing the opt-in label during a running attempt is ambiguous. Snapshotting
  labels from PLAN onward makes recovery deterministic; the next full reset or
  newly fetched pre-PLAN task can adopt the changed label set.
