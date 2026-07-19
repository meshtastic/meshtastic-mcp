<!-- SPDX-FileCopyrightText: Meshtastic contributors -->
<!-- SPDX-License-Identifier: GPL-3.0-only -->

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { useNightlyStore } from "../stores/nightly";
import NightlyReportCard from "./NightlyReportCard.vue";

const nightly = useNightlyStore();

// The ordered pipeline (mirrors STEPS in web/services/nightly.py). Anything not
// in this list (bench_recover/handoff/done) is post-soak → the "report" chip.
const STEPS = [
  { key: "self_update", label: "self-update" },
  { key: "firmware_update", label: "firmware" },
  { key: "prebuild", label: "prebuild" },
  { key: "bench_check", label: "bench check" },
  { key: "suite", label: "suite" },
  { key: "soak", label: "soak" },
];

// A 1 Hz clock so the elapsed + soak-progress meters advance smoothly even while
// the soak runs quiet for hours (no WS traffic to drive re-renders otherwise).
const now = ref(Date.now());
let timer: number | undefined;
onMounted(() => {
  timer = window.setInterval(() => (now.value = Date.now()), 1000);
});
onUnmounted(() => {
  if (timer) window.clearInterval(timer);
});

const active = computed(() => !!nightly.status?.state.active);
const curStep = computed(() => nightly.status?.state.step ?? null);
const stepIdx = computed(() => {
  const i = STEPS.findIndex((s) => s.key === curStep.value);
  return i === -1 ? STEPS.length : i; // unknown/post-soak → all main steps done
});
const runElapsed = computed(() =>
  nightly.activeStart != null ? Math.max(0, now.value / 1000 - nightly.activeStart) : null,
);
const soakTotal = computed(() => (nightly.status?.config.soak_hours ?? 0) * 3600);
const soakElapsed = computed(() =>
  nightly.soakStart != null ? Math.max(0, now.value / 1000 - nightly.soakStart) : null,
);
const soakPct = computed(() =>
  soakElapsed.value != null && soakTotal.value > 0
    ? Math.min(100, (soakElapsed.value / soakTotal.value) * 100)
    : null,
);
const feed = computed(() => [...nightly.activity].reverse()); // newest first

function fmtDur(s: number | null): string {
  if (s == null) return "—";
  const t = Math.floor(s);
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const sec = t % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
}
function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString();
}
const SEV: Record<string, string> = {
  error: "text-rose-400",
  warn: "text-amber-400",
  info: "text-slate-400",
};
const sevColor = (sev: string): string => SEV[sev] ?? "text-slate-400";

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

    <!-- live run -->
    <div
      v-if="active"
      class="card-rail rounded-xl border border-indigo-700/50 bg-indigo-950/20 p-4"
    >
      <div class="flex items-center gap-3 mb-3">
        <span class="w-2 h-2 rounded-full bg-indigo-400 animate-pulse" />
        <h3 class="section-label text-indigo-200">Run in progress</h3>
        <span class="text-xs text-slate-400">elapsed {{ fmtDur(runElapsed) }}</span>
        <span class="flex-1" />
        <span class="text-xs text-slate-600 mono">#{{ nightly.status?.state.nightly_id }}</span>
      </div>

      <!-- phase timeline -->
      <div class="flex flex-wrap items-center gap-1.5 mb-3">
        <template v-for="(s, i) in STEPS" :key="s.key">
          <span
            class="text-[10px] px-2 py-0.5 rounded-full border flex items-center gap-1"
            :class="
              i < stepIdx
                ? 'border-emerald-700/50 bg-emerald-950/40 text-emerald-300/90'
                : i === stepIdx
                  ? 'border-indigo-500 bg-indigo-700/30 text-indigo-200'
                  : 'border-slate-800 text-slate-600'
            "
          >
            <span v-if="i < stepIdx">✓</span>
            <span
              v-else-if="i === stepIdx"
              class="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-pulse"
            />
            {{ s.label }}
          </span>
          <span v-if="i < STEPS.length - 1" class="text-slate-700 text-[10px]">›</span>
        </template>
        <span class="text-slate-700 text-[10px]">›</span>
        <span
          class="text-[10px] px-2 py-0.5 rounded-full border"
          :class="
            stepIdx >= STEPS.length
              ? 'border-indigo-500 bg-indigo-700/30 text-indigo-200'
              : 'border-slate-800 text-slate-600'
          "
          >report</span
        >
      </div>

      <!-- soak progress -->
      <div v-if="soakPct != null" class="mb-3">
        <div class="flex items-center justify-between text-[11px] text-slate-400 mb-1">
          <span>soak · {{ fmtDur(soakElapsed) }} / {{ fmtDur(soakTotal) }}</span>
          <span>{{ Math.round(soakPct) }}% · ~{{ fmtDur(soakTotal - (soakElapsed ?? 0)) }} left</span>
        </div>
        <div class="h-2 rounded-full bg-slate-800 overflow-hidden">
          <div class="h-full bg-indigo-500 transition-all duration-1000" :style="{ width: soakPct + '%' }" />
        </div>
      </div>

      <!-- activity feed -->
      <div class="text-[11px] text-slate-500 mb-1">activity</div>
      <div
        class="rounded-lg border border-slate-800 bg-slate-950/50 max-h-52 overflow-y-auto divide-y divide-slate-800/60"
      >
        <div
          v-for="o in feed"
          :key="o.id"
          class="flex items-start gap-2 px-2 py-1 text-[11px]"
        >
          <span class="text-slate-600 mono shrink-0">{{ fmtTime(o.ts) }}</span>
          <span class="text-slate-600 shrink-0 uppercase text-[9px] mt-0.5">{{ o.step }}</span>
          <span
            class="mono truncate min-w-0"
            :class="sevColor(o.severity)"
            :title="o.message"
            >{{ o.message }}</span
          >
        </div>
        <div v-if="feed.length === 0" class="px-2 py-2 text-[11px] text-slate-600">
          waiting for the first observation…
        </div>
      </div>
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
