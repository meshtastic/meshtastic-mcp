<!-- SPDX-FileCopyrightText: Meshtastic contributors -->
<!-- SPDX-License-Identifier: GPL-3.0-only -->

<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { useNightlyStore } from "../stores/nightly";
import NightlyReportCard from "./NightlyReportCard.vue";

const nightly = useNightlyStore();

const draft = reactive({
  enabled: false,
  hour: 1,
  minute: 30,
  self_update: true,
  prebuild: true,
  force_bake: true,
  soak_hours: 2,
  llm_autostart: false,
  recovery_allow_reflash: true,
});
const reportDraft = reactive({ enabled: true, repo: "", auto_create_repo: false });

let seeded = false;
watch(
  () => nightly.status,
  (s) => {
    if (!s || seeded) return;
    Object.assign(draft, {
      enabled: s.config.enabled,
      hour: s.config.hour,
      minute: s.config.minute,
      self_update: s.config.self_update,
      prebuild: s.config.prebuild,
      force_bake: s.config.force_bake,
      soak_hours: s.config.soak_hours,
      llm_autostart: s.config.llm_autostart,
      recovery_allow_reflash: s.config.recovery_allow_reflash,
    });
    seeded = true;
  },
  { immediate: true },
);
let reportSeeded = false;
watch(
  () => nightly.reportConfig,
  (c) => {
    if (!c || reportSeeded) return;
    Object.assign(reportDraft, {
      enabled: c.enabled,
      repo: c.repo,
      auto_create_repo: c.auto_create_repo,
    });
    reportSeeded = true;
  },
  { immediate: true },
);

const busy = ref(false);
const msg = ref<string | null>(null);
const ok = ref(true);

const nextRun = computed(() => {
  const iso = nightly.status?.state.next_run_at;
  return iso ? new Date(iso).toLocaleString() : null;
});

async function act(label: string, fn: () => Promise<unknown>) {
  busy.value = true;
  msg.value = `${label}…`;
  ok.value = true;
  try {
    await fn();
    msg.value = `${label} ✓`;
  } catch (e: any) {
    ok.value = false;
    msg.value = e.message;
  } finally {
    busy.value = false;
  }
}

const save = () => act("saved", () => nightly.save({ ...draft }));
const saveReport = () => act("saved", () => nightly.saveReportConfig({ ...reportDraft }));
const runNow = () => act("started", () => nightly.runNow());
const cancelRun = () => act("canceling", () => nightly.cancelRun());

async function testDelivery() {
  busy.value = true;
  msg.value = "checking gh…";
  ok.value = true;
  try {
    const r = (await nightly.test(false)) as {
      ok?: boolean;
      status?: string;
      hint?: string;
      error?: string;
    };
    ok.value = !!r.ok;
    msg.value = r.ok ? "gh + repo reachable ✓" : `${r.status}: ${r.hint || r.error}`;
  } catch (e: any) {
    ok.value = false;
    msg.value = e.message;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="p-4 flex flex-col gap-4 max-w-5xl mx-auto">
    <!-- schedule card -->
    <div class="card-rail rounded-xl border border-slate-700/80 bg-slate-900/60 p-4">
      <div class="flex items-center gap-3 mb-3">
        <span class="w-1 h-3.5 rounded-full bg-indigo-500/80" />
        <h3 class="section-label">Nightly bake</h3>
        <span
          class="flex items-center gap-1.5 text-xs"
          :class="nightly.status?.state.active ? 'text-indigo-300' : 'text-slate-500'"
        >
          <span
            class="w-2 h-2 rounded-full"
            :class="nightly.status?.state.active ? 'bg-indigo-400 animate-pulse' : 'bg-slate-600'"
          />
          {{
            nightly.status?.state.active
              ? `running — ${nightly.status.state.step ?? "…"}`
              : draft.enabled
                ? `next: ${nextRun ?? "…"}`
                : "disabled"
          }}
        </span>
        <span class="flex-1" />
        <button
          v-if="!nightly.status?.state.active"
          @click="runNow"
          :disabled="busy"
          class="text-xs px-3 py-1 rounded bg-indigo-700/30 border border-indigo-700 text-indigo-300 hover:bg-indigo-700/50 disabled:opacity-40"
        >
          run now
        </button>
        <button
          v-else
          @click="cancelRun"
          :disabled="busy"
          class="text-xs px-3 py-1 rounded bg-rose-700/30 border border-rose-700 text-rose-300 hover:bg-rose-700/50 disabled:opacity-40"
        >
          cancel
        </button>
      </div>

      <div class="grid grid-cols-2 md:grid-cols-4 gap-x-6 gap-y-2 text-xs">
        <label class="flex items-center gap-2 text-slate-400">
          <input type="checkbox" v-model="draft.enabled" class="accent-indigo-500" />
          enabled
        </label>
        <label class="flex items-center gap-2 text-slate-400">
          start
          <input
            type="number"
            min="0"
            max="23"
            v-model.number="draft.hour"
            class="w-14 bg-slate-900 border border-slate-700 rounded px-1.5 py-0.5"
          />
          :
          <input
            type="number"
            min="0"
            max="59"
            v-model.number="draft.minute"
            class="w-14 bg-slate-900 border border-slate-700 rounded px-1.5 py-0.5"
          />
        </label>
        <label class="flex items-center gap-2 text-slate-400">
          soak (h)
          <input
            type="number"
            min="0"
            step="0.5"
            v-model.number="draft.soak_hours"
            class="w-16 bg-slate-900 border border-slate-700 rounded px-1.5 py-0.5"
          />
        </label>
        <label class="flex items-center gap-2 text-slate-400">
          <input type="checkbox" v-model="draft.force_bake" class="accent-indigo-500" />
          force re-bake
        </label>
        <label class="flex items-center gap-2 text-slate-400">
          <input type="checkbox" v-model="draft.prebuild" class="accent-indigo-500" />
          prebuild firmware
        </label>
        <label class="flex items-center gap-2 text-slate-400">
          <input type="checkbox" v-model="draft.self_update" class="accent-indigo-500" />
          self-update mcp
        </label>
        <label class="flex items-center gap-2 text-slate-400">
          <input type="checkbox" v-model="draft.llm_autostart" class="accent-indigo-500" />
          auto-start local LLM
        </label>
        <label class="flex items-center gap-2 text-slate-400">
          <input type="checkbox" v-model="draft.recovery_allow_reflash" class="accent-indigo-500" />
          recovery may reflash
        </label>
      </div>

      <div class="flex items-center gap-2 mt-3">
        <button
          @click="save"
          :disabled="busy"
          class="text-xs px-3 py-1 rounded bg-emerald-700/30 border border-emerald-700 text-emerald-300 hover:bg-emerald-700/50 disabled:opacity-40"
        >
          save
        </button>
        <span
          v-if="msg"
          class="text-xs mono truncate"
          :class="ok ? 'text-slate-500' : 'text-rose-400'"
          :title="msg"
          >{{ msg }}</span
        >
      </div>
      <p class="text-[10px] text-slate-600 mt-2">
        Each night: pull firmware <code>develop</code> into the nightly checkout, self-update
        meshtastic-mcp (restart + resume), bake every connected board onto the private
        <code>McpTest</code> channel, run the full suite, soak the mesh while collecting logs +
        camera snapshots, analyze (local LLM when reachable), then post the report.
      </p>
    </div>

    <!-- reporting card -->
    <div class="card-rail rounded-xl border border-slate-700/80 bg-slate-900/60 p-4">
      <div class="flex items-center gap-3 mb-3">
        <span class="w-1 h-3.5 rounded-full bg-emerald-500/80" />
        <h3 class="section-label">GitHub report delivery</h3>
      </div>
      <div class="flex flex-col gap-2 text-xs">
        <label class="text-slate-500">
          report repo (owner/name — private recommended)
          <input
            v-model="reportDraft.repo"
            placeholder="thebentern/fleet-nightly"
            class="w-full mt-1 bg-slate-900 border border-slate-700 rounded px-2 py-1 outline-none focus:border-emerald-700 mono"
          />
        </label>
        <div class="flex items-center gap-4">
          <label class="flex items-center gap-2 text-slate-400">
            <input type="checkbox" v-model="reportDraft.enabled" class="accent-emerald-500" />
            post issues
          </label>
          <label class="flex items-center gap-2 text-slate-400">
            <input type="checkbox" v-model="reportDraft.auto_create_repo" class="accent-emerald-500" />
            auto-create private repo
          </label>
        </div>
        <div class="flex items-center gap-2 mt-1">
          <button
            @click="saveReport"
            :disabled="busy"
            class="text-xs px-3 py-1 rounded bg-emerald-700/30 border border-emerald-700 text-emerald-300 hover:bg-emerald-700/50 disabled:opacity-40"
          >
            save
          </button>
          <button
            @click="testDelivery"
            :disabled="busy"
            class="text-xs px-3 py-1 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-40"
          >
            test connection
          </button>
        </div>
        <p class="text-[10px] text-slate-600">
          Posts one issue per night via the machine's <code>gh</code> login. Reports are always
          rendered and stored locally — this only controls delivery. GitHub does not email you
          for your own actions; this trail plus the history below is the record.
        </p>
      </div>
    </div>

    <!-- history -->
    <div class="rounded-xl border border-slate-700 bg-slate-900/60 p-3">
      <div class="text-xs text-slate-500 mb-2">nightly history</div>
      <div class="flex flex-col gap-2">
        <NightlyReportCard v-for="r in nightly.runs" :key="r.id" :run="r" />
        <div v-if="nightly.runs.length === 0" class="text-xs text-slate-600">
          no nightly runs recorded yet
        </div>
      </div>
    </div>
  </div>
</template>
