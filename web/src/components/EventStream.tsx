// EventStream — renders an agent event stream in the "telegram" style:
// each event is a single line with a leading emoji icon.
// Auto-scrolls to the bottom when new events arrive.

import type { JSX } from "react";
import { useEffect, useRef, useState } from "react";

import type { UiEvent } from "../api";
import { formatDurationMs } from "../utils";

interface Props {
  events: UiEvent[];
  style?: "telegram";
}

function fmtTs(ms: number): string {
  try {
    const d = new Date(ms);
    const hh = d.getHours().toString().padStart(2, "0");
    const mm = d.getMinutes().toString().padStart(2, "0");
    const ss = d.getSeconds().toString().padStart(2, "0");
    return `${hh}:${mm}:${ss}`;
  } catch {
    return "";
  }
}

function getString(payload: Record<string, unknown>, key: string): string {
  const v = payload[key];
  return typeof v === "string" ? v : "";
}

function compactDisplayText(text: string): string {
  return text
    .replace(/\r\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function shouldRenderSpan(event: UiEvent, payload: Record<string, unknown>): boolean {
  const spanType = getString(payload, "span_type");
  if (event.kind === "agent_span_failed") {
    return true;
  }
  return spanType !== "turn";
}

function ThinkingRow({ event }: { event: UiEvent }): JSX.Element {
  const [open, setOpen] = useState(false);
  const text = compactDisplayText(getString(event.payload, "text"));
  const preview = text.length > 60 ? `${text.slice(0, 60)}…` : text;
  return (
    <div className="event-row">
      <span className="event-ico">🧠</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <span style={{ color: "var(--fg-2)", fontStyle: "italic", fontSize: 12 }}>
          Thinking:{" "}
        </span>
        {open ? (
          <span style={{ color: "var(--fg-1)", whiteSpace: "pre-wrap", fontSize: 12 }}>
            {text}
          </span>
        ) : (
          <span style={{ color: "var(--fg-2)", fontSize: 12 }}>{preview || "—"}</span>
        )}
        {text.length > 60 && (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            style={{
              marginLeft: 6,
              fontSize: 10.5,
              color: "var(--accent)",
              cursor: "pointer",
            }}
          >
            {open ? "свернуть" : "развернуть"}
          </button>
        )}
      </div>
      <span className="event-ts mono">{fmtTs(event.ts_ms)}</span>
    </div>
  );
}

function AgentResultRow({ event }: { event: UiEvent }): JSX.Element {
  const [open, setOpen] = useState(false);
  const summary = compactDisplayText(getString(event.payload, "summary"));
  const text = compactDisplayText(getString(event.payload, "text"));
  const hasFullText = text.length > 0 && text !== summary;
  return (
    <div className="event-row">
      <span className="event-ico">📝</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            color: "var(--fg-0)",
            fontSize: 12,
            fontWeight: 600,
          }}
        >
          Final: {summary}
        </div>
        {hasFullText && (
          <>
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              style={{
                marginTop: 4,
                fontSize: 10.5,
                color: "var(--accent)",
                cursor: "pointer",
              }}
            >
              {open ? "свернуть" : "показать полный ответ"}
            </button>
            {open && (
              <pre
                className="mono"
                style={{
                  margin: "4px 0 0",
                  fontSize: 11.5,
                  lineHeight: 1.5,
                  color: "var(--fg-1)",
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                  background: "transparent",
                }}
              >
                {text}
              </pre>
            )}
          </>
        )}
      </div>
      <span className="event-ts mono">{fmtTs(event.ts_ms)}</span>
    </div>
  );
}

function renderEvent(event: UiEvent): JSX.Element | null {
  const payload = event.payload ?? {};

  if (
    event.kind === "agent_span_started" ||
    event.kind === "agent_span_finished" ||
    event.kind === "agent_span_failed"
  ) {
    if (!shouldRenderSpan(event, payload)) {
      return null;
    }
    const spanType = getString(payload, "span_type") || "span";
    const name = getString(payload, "name") || spanType;
    const duration = payload.duration_ms;
    const durationLabel =
      typeof duration === "number" ? formatDurationMs(duration) : "";
    const icon =
      event.kind === "agent_span_started"
        ? "▶"
        : event.kind === "agent_span_failed"
          ? "✕"
          : "✓";
    return (
      <div className="event-row" key={event.seq}>
        <span className="event-ico">{icon}</span>
        <div style={{ flex: 1, minWidth: 0, display: "flex", gap: 6 }}>
          <span className="mono" style={{ color: "var(--fg-3)", fontSize: 11 }}>
            {spanType}
          </span>
          <span className="ellipsis" style={{ color: "var(--fg-1)", fontSize: 11.5 }}>
            {name}
          </span>
          {durationLabel && (
            <span className="mono" style={{ color: "var(--fg-2)", fontSize: 11 }}>
              {durationLabel}
            </span>
          )}
        </div>
        <span className="event-ts mono">{fmtTs(event.ts_ms)}</span>
      </div>
    );
  }

  if (event.kind === "agent_tool") {
    const tool = getString(payload, "tool") || "tool";
    const detail = getString(payload, "detail");
    return (
      <div className="event-row" key={event.seq}>
        <span className="event-ico">⚙</span>
        <div style={{ flex: 1, minWidth: 0, display: "flex", gap: 6, alignItems: "baseline" }}>
          <span className="mono" style={{ color: "var(--fg-0)", fontWeight: 600, fontSize: 12 }}>
            {tool}
          </span>
          {detail && (
            <>
              <span style={{ color: "var(--fg-3)" }}>:</span>
              <span
                className="mono ellipsis"
                style={{ color: "var(--fg-1)", fontSize: 11.5 }}
                title={detail}
              >
                {detail}
              </span>
            </>
          )}
        </div>
        <span className="event-ts mono">{fmtTs(event.ts_ms)}</span>
      </div>
    );
  }

  if (event.kind === "agent_thinking") {
    return <ThinkingRow key={event.seq} event={event} />;
  }

  if (event.kind === "agent_text") {
    const text = compactDisplayText(getString(payload, "text"));
    return (
      <div className="event-row" key={event.seq}>
        <span className="event-ico">💬</span>
        <div
          style={{
            flex: 1,
            minWidth: 0,
            color: "var(--fg-1)",
            fontSize: 12,
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
          }}
        >
          {text}
        </div>
        <span className="event-ts mono">{fmtTs(event.ts_ms)}</span>
      </div>
    );
  }

  if (event.kind === "agent_result") {
    return <AgentResultRow key={event.seq} event={event} />;
  }

  // stage_started / stage_finished / stage_failed — rendered in stage header, skip.
  return null;
}

export default function EventStream({ events }: Props): JSX.Element {
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    // Auto-scroll to bottom when new events arrive.
    el.scrollTop = el.scrollHeight;
  }, [events.length]);

  const visible = events.map(renderEvent).filter(Boolean) as JSX.Element[];

  if (visible.length === 0) {
    return (
      <div style={{ padding: 14, fontSize: 11.5, color: "var(--fg-3)" }}>
        Пока нет событий агента
      </div>
    );
  }

  return (
    <div
      ref={scrollRef}
      style={{
        maxHeight: 300,
        overflowY: "auto",
        padding: "6px 0",
      }}
    >
      {visible}
    </div>
  );
}
