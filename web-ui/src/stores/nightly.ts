// SPDX-FileCopyrightText: Meshtastic contributors
// SPDX-License-Identifier: GPL-3.0-only

import { defineStore } from "pinia";
import { ref } from "vue";
import { api } from "../api/client";
import { useWsStore } from "./ws";
import type {
  NightlyObservation,
  NightlyReportConfig,
  NightlyRun,
  NightlyRunDetail,
  NightlyStatus,
} from "../types";

export const useNightlyStore = defineStore("nightly", () => {
  const status = ref<NightlyStatus | null>(null);
  const reportConfig = ref<NightlyReportConfig | null>(null);
  const runs = ref<NightlyRun[]>([]);

  // Live view of the in-flight run: observations stream in over the WS, and the
  // start/soak timestamps drive the elapsed + soak-progress meters. All empty
  // when nothing is running.
  const activity = ref<NightlyObservation[]>([]);
  const activeStart = ref<number | null>(null);
  const soakStart = ref<number | null>(null);

  async function load() {
    status.value = await api.get<NightlyStatus>("/api/nightly");
  }

  async function loadReportConfig() {
    reportConfig.value = await api.get<NightlyReportConfig>("/api/nightly/report-config");
  }

  async function loadRuns() {
    runs.value = await api.get<NightlyRun[]>("/api/nightly/runs");
  }

  // Seed the live view from the active run's detail (observation backlog + the
  // start/soak timestamps). Clears everything when nothing is running.
  async function seedActive() {
    const id = status.value?.state.nightly_id;
    if (!status.value?.state.active || !id) {
      activity.value = [];
      activeStart.value = null;
      soakStart.value = null;
      return;
    }
    const d = await detail(id);
    activeStart.value = d.started_at ?? null;
    soakStart.value = d.soak_started_at ?? null;
    // Merge (dedupe by id) so a live frame that arrived mid-fetch isn't dropped.
    const seen = new Set(activity.value.map((o) => o.id));
    const merged = activity.value.concat((d.observations || []).filter((o) => !seen.has(o.id)));
    activity.value = merged.sort((a, b) => a.ts - b.ts);
  }

  function init() {
    const ws = useWsStore();
    ws.subscribe("nightly.update", (frame: Record<string, any>) => {
      // Observations stream in live; append (dedupe by id) for the activity feed.
      if (frame.type === "observation") {
        if (!activity.value.some((o) => o.id === frame.id)) {
          activity.value.push({
            id: frame.id,
            step: frame.step,
            severity: frame.severity,
            kind: frame.kind,
            message: frame.message,
            data: frame.data ?? null,
            ts: frame.ts,
          });
        }
      } else if (frame.type === "finished" || frame.type === "report") {
        // The REST payloads are the truth; refresh + let seedActive clear the view.
        load().then(seedActive);
        loadRuns();
      } else if (frame.type === "state" || frame.type === "step") {
        load().then(seedActive);
      }
    });
    load().then(seedActive);
    loadReportConfig();
    loadRuns();
  }

  async function save(updates: Record<string, unknown>) {
    status.value = await api.put<NightlyStatus>("/api/nightly", updates);
  }

  async function saveReportConfig(updates: Record<string, unknown>) {
    reportConfig.value = await api.put<NightlyReportConfig>(
      "/api/nightly/report-config",
      updates,
    );
  }

  async function test(post: boolean): Promise<Record<string, unknown>> {
    return api.post("/api/nightly/test", { post });
  }

  async function runNow(): Promise<void> {
    await api.post("/api/nightly/run-now");
    await load();
  }

  async function cancelRun(): Promise<void> {
    await api.post("/api/nightly/cancel");
    await load();
  }

  async function detail(id: number): Promise<NightlyRunDetail> {
    return api.get<NightlyRunDetail>(`/api/nightly/runs/${id}`);
  }

  async function repost(id: number): Promise<void> {
    await api.post(`/api/nightly/runs/${id}/repost`);
    await loadRuns();
  }

  return {
    status,
    reportConfig,
    runs,
    activity,
    activeStart,
    soakStart,
    init,
    load,
    loadRuns,
    save,
    saveReportConfig,
    test,
    runNow,
    cancelRun,
    detail,
    repost,
  };
});
