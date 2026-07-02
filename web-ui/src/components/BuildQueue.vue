<!-- SPDX-FileCopyrightText: Meshtastic contributors -->
<!-- SPDX-License-Identifier: GPL-3.0-only -->

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { useBuildsStore } from "../stores/builds";
import { useFirmwareStore } from "../stores/firmware";
import type { Build } from "../stores/builds";

const builds = useBuildsStore();
const fw = useFirmwareStore();

const STATUS: Record<string, { glyph: string; cls: string }> = {
  queued: { glyph: "…", cls: "text-slate-400" },
  building: { glyph: "⏳", cls: "text-amber-400 animate-pulse" },
  success: { glyph: "✓", cls: "text-emerald-400" },
  cached: { glyph: "✓", cls: "text-emerald-400/70" },
  failed: { glyph: "✗", cls: "text-rose-400" },
  cancelled: { glyph: "∅", cls: "text-slate-500" },
};

// Ticking clock (epoch seconds) so building rows show live elapsed time.
const now = ref(Date.now() / 1000);
let timer: ReturnType<typeof setInterval> | undefined;
onMounted(() => {
  timer = setInterval(() => (now.value = Date.now() / 1000), 1000);
});
onUnmounted(() => {
  if (timer) clearInterval(timer);
});

// Seconds to display: live elapsed while building, final duration once done.
function seconds(b: Build): number | null {
  if (b.status === "building" && b.created_at) {
    return Math.max(0, now.value - b.created_at);
  }
  return b.duration_s ?? null;
}

const busy = ref(false);
const note = ref<string | null>(null);
async function prebuild() {
  busy.value = true;
  note.value = "enqueuing…";
  try {
    const n = await builds.prebuildTracked();
    note.value =
      n > 0
        ? `queued ${n} build${n === 1 ? "" : "s"}`
        : "no connected targets to build";
  } catch (e: any) {
    note.value = `failed: ${e.message}`;
  } finally {
    busy.value = false;
    setTimeout(() => (note.value = null), 5000);
  }
}
</script>

<template>
  <div class="card-rail rounded-xl border border-slate-700/80 bg-slate-900/60 p-4">
    <div class="flex items-center gap-3 mb-3">
      <span class="w-1 h-3.5 rounded-full bg-indigo-500/80" />
      <h3 class="section-label">Build Queue</h3>
      <span
        v-if="!builds.dockerAvailable"
        class="text-[11px] px-2 py-0.5 rounded bg-amber-950/40 text-amber-400"
        title="Docker not detected — builds fall back to host pio (not parallelized)"
        >Docker unavailable — host builds</span
      >
      <div class="flex-1" />
      <span v-if="note" class="text-[11px] text-slate-400 mono">{{ note }}</span>
      <button
        @click="prebuild()"
        :disabled="busy"
        class="text-xs px-3 py-1 rounded bg-emerald-700/30 border border-emerald-700 text-emerald-300 hover:bg-emerald-700/50 disabled:opacity-40 disabled:cursor-not-allowed"
        :title="'prebuild connected device targets @ ' + (fw.ref.short_sha || '')"
      >
        {{ busy ? "enqueuing…" : "prebuild current ref" }}
      </button>
    </div>

    <div class="flex flex-wrap gap-2">
      <div
        v-for="b in builds.list"
        :key="b.id"
        class="flex flex-col gap-0.5 text-xs rounded-lg border px-2.5 py-1.5"
        :class="
          b.status === 'building'
            ? 'border-amber-700/60 bg-amber-950/10'
            : 'border-slate-800'
        "
        :title="b.error || b.artifact_dir || ''"
      >
        <div class="flex items-center gap-2">
          <span :class="(STATUS[b.status] || STATUS.queued).cls">{{
            (STATUS[b.status] || STATUS.queued).glyph
          }}</span>
          <span class="text-slate-200">{{ b.env }}</span>
          <span class="mono text-emerald-300/60">{{ b.fw_sha?.slice(0, 7) }}</span>
          <span
            v-if="seconds(b) != null"
            :class="b.status === 'building' ? 'text-amber-400/80 mono' : 'text-slate-500'"
            >{{ seconds(b)!.toFixed(0) }}s</span
          >
          <span v-if="b.cached" class="text-slate-600">cached</span>
        </div>
        <!-- live build log line — only while building, never clobbers the row -->
        <div
          v-if="b.status === 'building' && builds.lastLog[b.id]"
          class="mono text-[10px] text-slate-500 truncate max-w-[16rem]"
          :title="builds.lastLog[b.id]"
        >
          {{ builds.lastLog[b.id] }}
        </div>
      </div>
      <div v-if="builds.list.length === 0" class="text-xs text-slate-600">
        no builds yet — "prebuild current ref" builds each connected target in
        parallel (Docker) in the background
      </div>
    </div>
  </div>
</template>
