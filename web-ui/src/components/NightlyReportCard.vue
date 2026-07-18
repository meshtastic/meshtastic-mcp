<!-- SPDX-FileCopyrightText: Meshtastic contributors -->
<!-- SPDX-License-Identifier: GPL-3.0-only -->

<script setup lang="ts">
import { ref } from "vue";
import { useNightlyStore } from "../stores/nightly";
import type { NightlyRun, NightlyRunDetail } from "../types";

const props = defineProps<{ run: NightlyRun }>();
const nightly = useNightlyStore();

const expanded = ref(false);
const detail = ref<NightlyRunDetail | null>(null);
const busy = ref(false);

const STATUS_STYLE: Record<string, string> = {
  passed: "text-emerald-300 border-emerald-700/60 bg-emerald-700/15",
  failed: "text-rose-300 border-rose-700/60 bg-rose-700/15",
  error: "text-amber-300 border-amber-700/60 bg-amber-700/15",
  canceled: "text-slate-400 border-slate-700 bg-slate-800/40",
  running: "text-indigo-300 border-indigo-700/60 bg-indigo-700/15",
  awaiting_restart: "text-indigo-300 border-indigo-700/60 bg-indigo-700/15",
};

function fmtDate(ts: number): string {
  return new Date(ts * 1000).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function short(sha: string | null): string {
  return sha ? sha.slice(0, 7) : "—";
}

async function toggle() {
  expanded.value = !expanded.value;
  if (expanded.value && detail.value === null) {
    busy.value = true;
    try {
      detail.value = await nightly.detail(props.run.id);
    } finally {
      busy.value = false;
    }
  }
}

async function repost() {
  busy.value = true;
  try {
    await nightly.repost(props.run.id);
    detail.value = null; // refetch on next expand
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="rounded-lg border border-slate-800 bg-slate-900/50">
    <button class="w-full flex items-center gap-3 px-3 py-2 text-xs text-left" @click="toggle">
      <span class="text-slate-400 shrink-0 w-28">{{ fmtDate(run.started_at) }}</span>
      <span
        class="px-1.5 py-0.5 rounded border text-[10px] fs-display shrink-0"
        :class="STATUS_STYLE[run.status] ?? STATUS_STYLE.canceled"
        >{{ run.status }}</span
      >
      <template v-if="run.summary">
        <span class="text-emerald-400 tabular-nums">{{ run.summary.passed }}</span>
        <span class="text-rose-400 tabular-nums">{{ run.summary.failed }}</span>
        <span class="text-slate-500 tabular-nums">{{ run.summary.skipped }}</span>
      </template>
      <span class="mono text-emerald-300/60 shrink-0">
        {{ short(run.fw_sha_before) }}<span class="text-slate-600">→</span>{{ short(run.fw_sha_after) }}
      </span>
      <span v-if="run.report" class="text-slate-500 tabular-nums"
        >{{ run.report.observations }} obs</span
      >
      <span class="flex-1" />
      <a
        v-if="run.report?.issue_url"
        :href="run.report.issue_url"
        target="_blank"
        rel="noopener"
        class="text-indigo-300 hover:text-indigo-200 shrink-0"
        @click.stop
        >issue ↗</a
      >
      <span
        v-else-if="run.report"
        class="text-amber-400/90 shrink-0"
        :title="run.report.error ?? undefined"
        >{{ run.report.status }}</span
      >
    </button>

    <div v-if="expanded" class="border-t border-slate-800 px-3 py-2 text-xs">
      <div v-if="busy" class="text-slate-500">loading…</div>
      <template v-else-if="detail">
        <div class="flex items-center gap-2 mb-2">
          <span class="text-slate-500">delivery: {{ detail.report?.status ?? "none" }}</span>
          <span v-if="detail.report?.error" class="text-rose-400 truncate" :title="detail.report.error"
            >{{ detail.report.error }}</span
          >
          <button
            v-if="detail.report && detail.report.status !== 'posted'"
            @click="repost"
            class="px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700"
          >
            repost
          </button>
        </div>
        <div v-if="detail.observations.length" class="mb-2 max-h-40 overflow-y-auto">
          <div
            v-for="o in detail.observations"
            :key="o.id"
            class="mono text-[10px] leading-4"
            :class="
              o.severity === 'error'
                ? 'text-rose-300'
                : o.severity === 'warn'
                  ? 'text-amber-300/90'
                  : 'text-slate-500'
            "
          >
            [{{ o.step }}] {{ o.kind }} — {{ o.message }}
          </div>
        </div>
        <!-- Plain-text preview only: the body embeds device-authored (untrusted)
             log content, so it is never rendered as HTML. -->
        <pre
          v-if="detail.report?.body_md"
          class="max-h-96 overflow-auto whitespace-pre-wrap rounded bg-slate-950/70 border border-slate-800 p-2 text-[10px] leading-4 text-slate-300"
          >{{ detail.report.body_md }}</pre
        >
      </template>
    </div>
  </div>
</template>
