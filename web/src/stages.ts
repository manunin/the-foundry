// Canonical pipeline stage list shown in the UI.
// Order matches `src/foundry/pipeline.py` (with agent-prefixed aliases
// applied in `src/api/projections.py`).

export interface StageMeta {
  id: string;
  label: string;
  title: string;
}

export const STAGES: StageMeta[] = [
  { id: "fetch", label: "fetch", title: "Fetch issue" },
  { id: "context", label: "context", title: "Build context" },
  { id: "agent_plan", label: "plan", title: "Agent plan" },
  { id: "agent_implement", label: "implement", title: "Agent implement" },
  { id: "verify", label: "verify", title: "Verify" },
  { id: "ui_tests", label: "ui tests", title: "UI crawler tests" },
  { id: "pr", label: "change", title: "Open change request" },
];

export function stagesForTask(uiTestsEnabled: boolean): StageMeta[] {
  return uiTestsEnabled ? STAGES : STAGES.filter((stage) => stage.id !== "ui_tests");
}
