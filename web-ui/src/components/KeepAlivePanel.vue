<!-- SPDX-FileCopyrightText: Meshtastic contributors -->
<!-- SPDX-License-Identifier: GPL-3.0-only -->

<script setup lang="ts">
import { reactive, ref, watch } from "vue";
import { useKeepAliveStore } from "../stores/keepalive";

const ka = useKeepAliveStore();

const collapsed = ref(localStorage.getItem("fs.keepalive.collapsed") !== "false");
watch(collapsed, (v) =>
  localStorage.setItem("fs.keepalive.collapsed", String(v)),
);

// Benign input-broker events; USER_PRESS short-press advances the carousel.
const EVENTS = ["USER_PRESS", "RIGHT", "LEFT", "UP", "DOWN", "SELECT"];

const draft = reactive({ enabled: false, interval_s: 30, event: "USER_PRESS" });

let seeded = false;
watch(
  () => ka.status,
  (s) => {
    if (!s || seeded) return;
    draft.enabled = s.config.enabled;
    draft.interval_s = s.config.interval_s;
    draft.event = s.config.event;
    seeded = true;
  },
  { immediate: true },
);

const busy = ref(false);
async function apply() {
  busy.value = true;
  try {
    await ka.save({
      enabled: draft.enabled,
      interval_s: Number(draft.interval_s) || 30,
      event: draft.event,
    });
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="card-rail rounded-xl border border-slate-700/80 bg-slate-900/60 p-4">
    <div class="flex items-center gap-3" :class="{ 'mb-3': !collapsed }">
      <button
        @click="collapsed = !collapsed"
        class="flex items-center gap-2 text-left shrink-0"
      >
        <svg
          class="w-3 h-3 text-slate-500 transition-transform"
          :class="collapsed ? '' : 'rotate-90'"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2.5"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <polyline points="9 6 15 12 9 18" />
        </svg>
        <span class="w-1 h-3.5 rounded-full bg-indigo-500/80" />
        <h3 class="section-label">Screen keep-alive</h3>
      </button>
      <span
        v-if="ka.status"
        class="flex items-center gap-1.5 text-xs"
        :class="ka.status.stats.enabled ? 'text-emerald-400' : 'text-slate-500'"
      >
        <span
          class="w-2 h-2 rounded-full"
          :class="ka.status.stats.enabled ? 'bg-emerald-400' : 'bg-slate-600'"
        />
        {{ ka.status.stats.enabled ? "on" : "off" }}
      </span>
      <span
        v-if="ka.status?.stats.enabled"
        class="text-xs text-slate-500 tabular-nums"
        >{{ ka.status.stats.provisioned }} provisioned ·
        {{ ka.status.stats.events_sent }} pokes</span
      >
      <span
        v-if="ka.status?.stats.last_error"
        class="text-xs text-rose-400 truncate"
        :title="ka.status.stats.last_error"
        >⚠ {{ ka.status.stats.last_error }}</span
      >
    </div>

    <div v-show="!collapsed" class="flex flex-wrap items-end gap-3 text-xs">
      <label class="flex items-center gap-2">
        <input type="checkbox" v-model="draft.enabled" class="accent-emerald-500" />
        <span class="text-slate-300">Keep node screens on</span>
      </label>
      <label class="text-slate-500"
        >every
        <input
          v-model.number="draft.interval_s"
          type="number"
          min="5"
          class="w-16 ml-1 bg-slate-900 border border-slate-700 rounded px-2 py-1 outline-none focus:border-emerald-700"
        />
        <span class="ml-1">s</span></label
      >
      <label class="text-slate-500"
        >poke
        <select
          v-model="draft.event"
          class="ml-1 bg-slate-900 border border-slate-700 rounded px-2 py-1 outline-none"
        >
          <option v-for="e in EVENTS" :key="e" :value="e">{{ e }}</option>
        </select></label
      >
      <button
        @click="apply"
        :disabled="busy"
        class="text-xs px-3 py-1 rounded bg-emerald-700/30 border border-emerald-700 text-emerald-300 hover:bg-emerald-700/50 disabled:opacity-40"
      >
        apply
      </button>
    </div>
    <p v-show="!collapsed" class="text-[10px] text-slate-600 mt-2">
      Provisions each connected node's <code>display.screen_on_secs</code> (a
      one-time config write that reboots the node) and then periodically injects
      an input-broker admin message to keep the OLED awake + cycling for the
      cameras. Paused while a test run owns the ports.
    </p>
  </div>
</template>
