import { defineStore } from "pinia";
import { ref } from "vue";
import { api } from "../api/client";
import { useWsStore } from "./ws";

export interface KeepAliveStatus {
  config: {
    enabled: boolean;
    interval_s: number;
    event: string;
    screen_on_secs: number;
  };
  stats: {
    enabled: boolean;
    provisioned: number;
    events_sent: number;
    last_error: string | null;
    last_cycle_ts: number | null;
  };
}

export const useKeepAliveStore = defineStore("keepalive", () => {
  const status = ref<KeepAliveStatus | null>(null);

  async function load() {
    status.value = await api.get<KeepAliveStatus>("/api/keepalive");
  }

  function init() {
    const ws = useWsStore();
    ws.subscribe("keepalive.update", (s: KeepAliveStatus) => {
      status.value = s;
    });
    load();
  }

  async function save(patch: Record<string, unknown>) {
    status.value = await api.put<KeepAliveStatus>("/api/keepalive", patch);
  }

  return { status, load, init, save };
});
