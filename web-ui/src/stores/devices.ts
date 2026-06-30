import { defineStore } from "pinia";
import { computed, reactive } from "vue";
import { api } from "../api/client";
import type { Device } from "../types";
import { useWsStore } from "./ws";

export const useDevicesStore = defineStore("devices", () => {
  // Keyed by serial_number — a device.update with a new current_port is a field
  // update on the same entry, so the card "follows" the device across ports.
  const bySerial = reactive<Record<string, Device>>({});

  const list = computed(() =>
    Object.values(bySerial).sort((a, b) => {
      if (a.online !== b.online) return b.online - a.online;
      return (a.friendly_name || a.serial_number).localeCompare(
        b.friendly_name || b.serial_number,
      );
    }),
  );

  async function load() {
    const devices = await api.get<Device[]>("/api/devices");
    for (const d of devices) bySerial[d.serial_number] = d;
  }

  function init() {
    const ws = useWsStore();
    ws.subscribe("device.update", (d: any) => {
      if (d && d.deleted) delete bySerial[d.serial_number];
      else if (d) bySerial[d.serial_number] = d;
    });
    load();
  }

  async function setFriendlyName(serial: string, name: string) {
    const updated = await api.patch<Device>(`/api/devices/${serial}`, {
      friendly_name: name,
    });
    bySerial[serial] = updated;
  }

  async function refresh(serial: string) {
    const res = await api.post<{ device: Device }>(
      `/api/devices/${serial}/refresh`,
    );
    bySerial[serial] = res.device;
  }

  // Pin a pio env (manual override) or release to auto-detect (env=null).
  async function setEnv(serial: string, env: string | null) {
    const updated = await api.put<Device>(`/api/devices/${serial}/env`, {
      env,
    });
    bySerial[serial] = updated;
  }

  // Pin (or clear, with location=null) which uhubctl hub port the node sits on.
  async function setHubPort(
    serial: string,
    location: string | null,
    port: number | null,
  ) {
    const updated = await api.put<Device>(`/api/devices/${serial}/hub-port`, {
      location,
      port,
    });
    bySerial[serial] = updated;
  }

  // Auto-bind the node to its hub port (unique VID match) or get candidates.
  async function locate(serial: string) {
    const res = await api.post<{
      located: boolean;
      device: Device;
      candidates: { location: string; port: number }[];
    }>(`/api/devices/${serial}/locate`);
    if (res.located && res.device) bySerial[serial] = res.device;
    return res;
  }

  // Cut/restore/cycle USB power to the node via its tracked hub port.
  async function power(serial: string, action: "on" | "off" | "cycle") {
    return api.post(`/api/devices/${serial}/power/${action}`);
  }

  // Run the escalating recovery ladder (reboot → power-cycle, + reflash when
  // allowReflash). Long-running; progress streams on the recovery.update topic.
  async function recover(serial: string, allowReflash = false) {
    return api.post<{
      recovered: boolean;
      final_step: string | null;
      steps: { step: string; label: string; skipped: string | null; healthy_after?: boolean }[];
    }>(`/api/devices/${serial}/recover`, { allow_reflash: allowReflash });
  }

  // Lightweight sibling to recover(): just free a held/wedged serial port —
  // wait out a holder, then power-cycle the node's own hub slot if it's wedged.
  // The device may come back on a new path (returned as new_port).
  async function unwedge(serial: string) {
    return api.post<{
      recovered: boolean;
      new_port: string | null;
      holders: [string, string][];
      error?: string;
    }>(`/api/devices/${serial}/unwedge`);
  }

  // Identify which hub port this node is on by power-cycling candidate ports
  // and watching which node drops; auto-pins the match (and any it cycles).
  async function identify(serial: string) {
    const res = await api.post<{
      identified: boolean;
      slot: { location: string; port: number } | null;
      mapped: { serial: string; location: string; port: number }[];
      device: Device;
    }>(`/api/devices/${serial}/identify`);
    if (res.device) bySerial[serial] = res.device;
    return res;
  }

  return {
    bySerial,
    list,
    load,
    init,
    setFriendlyName,
    refresh,
    setEnv,
    setHubPort,
    locate,
    power,
    identify,
    recover,
    unwedge,
  };
});
