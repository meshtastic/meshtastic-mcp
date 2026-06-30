<script setup lang="ts">
import { reactive, ref, watch } from "vue";
import { useDatadogStore } from "../stores/datadog";

const dd = useDatadogStore();

// Collapsed by default; status stays visible in the header. Persisted.
const collapsed = ref(localStorage.getItem("fs.datadog.collapsed") !== "false");
watch(collapsed, (v) => localStorage.setItem("fs.datadog.collapsed", String(v)));

// Two inputs, nothing else: a Datadog client token and the tester id. Site
// (US5), scrub (redact), and shipping-always-on are baked in the backend.
const draft = reactive({ host: "", api_key: "" });

let seeded = false;
watch(
  () => dd.status,
  (s) => {
    if (!s || seeded) return;
    draft.host = s.config.host;
    seeded = true;
  },
  { immediate: true },
);

const busy = ref(false);
const msg = ref<string | null>(null);
const ok = ref(true);

async function save() {
  busy.value = true;
  msg.value = "saving…";
  ok.value = true;
  try {
    const payload: Record<string, unknown> = { host: draft.host };
    if (draft.api_key.trim()) payload.api_key = draft.api_key.trim();
    await dd.save(payload);
    draft.api_key = ""; // never keep the secret in the field
    msg.value = "saved";
  } catch (e: any) {
    msg.value = e.message;
    ok.value = false;
  } finally {
    busy.value = false;
  }
}

async function test() {
  busy.value = true;
  msg.value = "checking…";
  ok.value = true;
  try {
    const r = await dd.test();
    ok.value = r.ok;
    msg.value = r.ok ? "token accepted by Datadog ✓" : `failed: ${r.error}`;
  } catch (e: any) {
    ok.value = false;
    msg.value = e.message;
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
        <h3 class="section-label">Datadog logging</h3>
      </button>
      <span
        v-if="dd.status"
        class="flex items-center gap-1.5 text-xs"
        :class="dd.status.stats.running ? 'text-emerald-400' : 'text-slate-500'"
      >
        <span
          class="w-2 h-2 rounded-full"
          :class="dd.status.stats.running ? 'bg-emerald-400' : 'bg-slate-600'"
        />
        {{ dd.status.stats.running ? "shipping" : "no token" }}
      </span>
      <span
        v-if="dd.status?.stats.running"
        class="text-xs text-slate-500 tabular-nums"
        >{{ dd.status.stats.sent_logs }} logs</span
      >
      <span
        v-if="dd.status?.stats.last_error"
        class="text-xs text-rose-400 truncate"
        :title="dd.status.stats.last_error"
        >⚠ {{ dd.status.stats.last_error }}</span
      >
    </div>

    <div v-show="!collapsed" class="flex flex-col gap-2 text-xs">
      <label class="text-slate-500">
        Datadog client token
        <input
          v-model="draft.api_key"
          type="password"
          :placeholder="
            dd.status?.config.has_key
              ? `set (••••${dd.status.config.key_hint}) — leave blank to keep`
              : 'pub… (an API key also works)'
          "
          class="w-full mt-1 bg-slate-900 border border-slate-700 rounded px-2 py-1 outline-none focus:border-emerald-700"
        />
      </label>
      <label class="text-slate-500">
        Tester / bench ID
        <input
          v-model="draft.host"
          placeholder="hostname"
          class="w-full mt-1 bg-slate-900 border border-slate-700 rounded px-2 py-1 outline-none focus:border-emerald-700"
        />
      </label>

      <div class="flex items-center gap-2 mt-1">
        <button
          @click="save"
          :disabled="busy"
          class="text-xs px-3 py-1 rounded bg-emerald-700/30 border border-emerald-700 text-emerald-300 hover:bg-emerald-700/50 disabled:opacity-40"
        >
          save
        </button>
        <button
          @click="test"
          :disabled="busy"
          class="text-xs px-3 py-1 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-40"
        >
          test token
        </button>
        <span
          v-if="msg"
          class="text-xs mono truncate"
          :class="ok ? 'text-slate-500' : 'text-rose-400'"
          :title="msg"
          >{{ msg }}</span
        >
      </div>
      <p class="text-[10px] text-slate-600">
        Ships every connected node's firmware logs to Datadog
        (<code>us5.datadoghq.com</code>, GPS redacted) the moment a token is set —
        same schema as FleetLog. Clear the token to stop.
      </p>
    </div>
  </div>
</template>
