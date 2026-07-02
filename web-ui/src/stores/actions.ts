// SPDX-FileCopyrightText: Meshtastic contributors
// SPDX-License-Identifier: GPL-3.0-only

// Live server-side activity stream for device actions (flash, inject-nodedb,
// factory-reset, reboot). The backend wraps each action in an Activity that
// publishes `action.update` frames {id, kind, target, phase, state, elapsed_s,
// last_line, ts}; this store keys them by id, ticks `now` every 1s so elapsed
// updates live between frames (mirrors the runningElapsed pattern in
// stores/tests.ts), and drops an entry a few seconds after it finishes.

import { defineStore } from "pinia";
import { reactive, ref } from "vue";
import { useWsStore } from "./ws";

export type ActionState = "started" | "running" | "done" | "error";

export interface DeviceAction {
  id: string;
  kind: string;
  target: string;
  phase: string | null;
  state: ActionState;
  elapsed_s: number;
  last_line: string | null;
  since: number; // epoch ms the action started (derived from server elapsed_s)
}

// How long a finished action lingers in the UI before it's dropped.
const DROP_AFTER_MS = 3000;

export const useActionsStore = defineStore("actions", () => {
  const actions = reactive<Record<string, DeviceAction>>({});
  const now = ref(Date.now()); // ticks every 1s so elapsed updates live
  const removers = new Map<string, ReturnType<typeof setTimeout>>();

  function isTerminal(s: ActionState): boolean {
    return s === "done" || s === "error";
  }

  function onUpdate(d: any) {
    if (!d || !d.id) return;
    const existing = actions[d.id];
    // Once terminal, ignore a late `running` frame (a heartbeat that was
    // scheduled just before the action finished) so it doesn't revive the row.
    if (existing && isTerminal(existing.state) && !isTerminal(d.state)) return;

    actions[d.id] = {
      id: d.id,
      kind: d.kind,
      target: d.target,
      phase: d.phase ?? null,
      state: d.state,
      elapsed_s: d.elapsed_s ?? 0,
      last_line: d.last_line ?? null,
      since: Date.now() - (d.elapsed_s ?? 0) * 1000,
    };

    if (isTerminal(d.state)) {
      const prev = removers.get(d.id);
      if (prev) clearTimeout(prev);
      removers.set(
        d.id,
        setTimeout(() => {
          delete actions[d.id];
          removers.delete(d.id);
        }, DROP_AFTER_MS),
      );
    }
  }

  function init() {
    const ws = useWsStore();
    ws.subscribe("action.update", onUpdate);
    setInterval(() => (now.value = Date.now()), 1000);
  }

  // Whole seconds the action has been running. While live it ticks from
  // `since`; once finished it freezes at the server's final elapsed.
  function elapsedFor(a: DeviceAction): number {
    if (isTerminal(a.state)) return Math.floor(a.elapsed_s);
    return Math.max(0, Math.floor((now.value - a.since) / 1000));
  }

  // The newest action targeting a device (or null). Reads `actions` so callers
  // re-render when a frame lands.
  function activeFor(serial: string): DeviceAction | null {
    let best: DeviceAction | null = null;
    for (const a of Object.values(actions)) {
      if (a.target !== serial) continue;
      if (!best || a.since > best.since) best = a;
    }
    return best;
  }

  return { actions, now, init, elapsedFor, activeFor };
});
