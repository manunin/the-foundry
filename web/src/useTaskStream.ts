// useTaskStream — subscribe to /api/tasks/{id}/events (SSE) and keep a
// deduplicated list of events merged with the initial REST snapshot.

import { useEffect, useRef, useState } from "react";

import { apiUrl, fetchTask } from "./api";
import type { UiEvent } from "./api";

interface StreamState {
  events: UiEvent[];
  connected: boolean;
  error: string | null;
}

interface Options {
  enabled?: boolean;
}

const SSE_EVENT_KINDS = [
  "stage_started",
  "stage_finished",
  "stage_failed",
  "agent_tool",
  "agent_text",
  "agent_thinking",
  "agent_result",
] as const;

export function useTaskStream(
  taskId: number | null,
  opts: Options = {},
): StreamState {
  const { enabled = true } = opts;
  const [events, setEvents] = useState<UiEvent[]>([]);
  const [connected, setConnected] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const lastSeqRef = useRef<number>(0);
  const bySeqRef = useRef<Set<number>>(new Set());

  useEffect(() => {
    if (taskId === null || !enabled) {
      setEvents([]);
      setConnected(false);
      setError(null);
      lastSeqRef.current = 0;
      bySeqRef.current = new Set();
      return;
    }

    let cancelled = false;
    let es: EventSource | null = null;

    // Reset state for new taskId
    lastSeqRef.current = 0;
    bySeqRef.current = new Set();
    setEvents([]);
    setError(null);
    setConnected(false);

    const mergeEvent = (ev: UiEvent): void => {
      if (bySeqRef.current.has(ev.seq)) return;
      bySeqRef.current.add(ev.seq);
      if (ev.seq > lastSeqRef.current) lastSeqRef.current = ev.seq;
      setEvents((prev) => {
        const next = [...prev, ev];
        next.sort((a, b) => a.seq - b.seq);
        return next;
      });
    };

    const connectSSE = (): void => {
      if (cancelled) return;
      // EventSource automatically sends Last-Event-ID on reconnect,
      // based on `id:` lines it has seen.
      es = new EventSource(apiUrl(`/api/tasks/${taskId}/events`));
      es.onopen = () => {
        if (cancelled) return;
        setConnected(true);
        setError(null);
      };
      es.onerror = () => {
        if (cancelled) return;
        setConnected(false);
        // Browser auto-reconnects; we don't close here.
      };
      const handleMessage = (e: MessageEvent<string>): void => {
        if (cancelled) return;
        try {
          const parsed = JSON.parse(e.data) as UiEvent;
          mergeEvent(parsed);
        } catch {
          // ignore malformed frames
        }
      };
      es.onmessage = handleMessage;
      for (const kind of SSE_EVENT_KINDS) {
        es.addEventListener(kind, handleMessage);
      }
    };

    // 1) Pull snapshot via REST.
    // 2) Then open SSE — EventSource replays from seq=0 by default, which is fine;
    //    duplicates are deduped by `bySeqRef`.
    fetchTask(taskId)
      .then((task) => {
        if (cancelled) return;
        const snapshot = task.events ?? [];
        for (const ev of snapshot) mergeEvent(ev);
        connectSSE();
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
        // Still try SSE — it will replay from SQLite.
        connectSSE();
      });

    return () => {
      cancelled = true;
      if (es) es.close();
      setConnected(false);
    };
  }, [taskId, enabled]);

  return { events, connected, error };
}
