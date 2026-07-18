// SPDX-FileCopyrightText: Meshtastic contributors
// SPDX-License-Identifier: GPL-3.0-only

import { defineStore } from "pinia";
import { ref } from "vue";
import { api } from "../api/client";
import { useWsStore } from "./ws";
import type {
  NightlyReportConfig,
  NightlyRun,
  NightlyRunDetail,
  NightlyStatus,
} from "../types";

export const useNightlyStore = defineStore("nightly", () => {
  const status = ref<NightlyStatus | null>(null);
  const reportConfig = ref<NightlyReportConfig | null>(null);
  const runs = ref<NightlyRun[]>([]);

  async function load() {
    status.value = await api.get<NightlyStatus>("/api/nightly");
  }

  async function loadReportConfig() {
    reportConfig.value = await api.get<NightlyReportConfig>("/api/nightly/report-config");
  }

  async function loadRuns() {
    runs.value = await api.get<NightlyRun[]>("/api/nightly/runs");
  }

  function init() {
    const ws = useWsStore();
    ws.subscribe("nightly.update", (frame: { type?: string }) => {
      // Frames are lightweight signals; the REST payloads are the truth.
      if (frame.type === "finished" || frame.type === "report") {
        load();
        loadRuns();
      } else if (frame.type === "state" || frame.type === "step") {
        load();
      }
    });
    load();
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
