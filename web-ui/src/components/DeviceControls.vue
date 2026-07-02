<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { api } from "../api/client";
import { useActionsStore } from "../stores/actions";
import { useBuildsStore } from "../stores/builds";
import { useDevicesStore } from "../stores/devices";
import { useFirmwareStore } from "../stores/firmware";
import { useTestsStore } from "../stores/tests";
import { useWsStore } from "../stores/ws";
import type { Device } from "../types";

const props = defineProps<{ device: Device }>();
const tests = useTestsStore();
const builds = useBuildsStore();
const devices = useDevicesStore();
const fw = useFirmwareStore();
const ws = useWsStore();
const actionStream = useActionsStore();

const nodedbSize = ref(500);
const flashStats = ref<any>(null);

const isNative = computed(() => props.device.role === "native");
const nativeName = computed(() => props.device.serial_number.split(":")[1] ?? "");

async function loadFlashStats() {
  if (isNative.value) return; // native nodes aren't flashed
  try {
    flashStats.value = await api.get(
      `/api/devices/${props.device.serial_number}/flash-stats`,
    );
  } catch {
    /* ignore */
  }
}
onMounted(loadFlashStats);

// role → default pio env (mirrors the backend identity map).
const ROLE_ENV: Record<string, string> = {
  nrf52: "rak4631",
  esp32s3: "heltec-v3",
};

// Is there a prebuilt artifact for this device's target at the current ref?
// Prefer the env resolved from hw_model; fall back to the coarse role default.
const flashReady = computed(() => {
  const env =
    props.device.env ||
    (props.device.role ? ROLE_ENV[props.device.role] : undefined);
  if (!env) return false;
  const b = builds.statusFor(env, fw.ref.sha);
  return b?.status === "success" || b?.status === "cached";
});

const busy = ref(false);
const msg = ref<string | null>(null);
const ok = ref(true);
const sendText = ref("");

// Server-backed activity stream for the in-flight action on this device
// (flash/inject/factory-reset/reboot): live phase + elapsed + last output line,
// richer than the client-side ticking timer below.
const serverAction = computed(() =>
  actionStream.activeFor(props.device.serial_number),
);

// Ticking elapsed for the in-flight action so a slow one (flash, factory-reset,
// inject+reboot) shows it's working instead of looking frozen.
const runStartedAt = ref<number | null>(null);
const nowTick = ref(Date.now());
const runElapsed = computed(() =>
  busy.value && runStartedAt.value != null
    ? Math.floor((nowTick.value - runStartedAt.value) / 1000)
    : null,
);
function fmtElapsed(s: number): string {
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

async function run(
  label: string,
  fn: () => Promise<any>,
  format?: (result: any) => { msg: string; ok: boolean },
) {
  busy.value = true;
  runStartedAt.value = Date.now();
  msg.value = `${label}…`;
  ok.value = true;
  try {
    const result = await fn();
    const f = format ? format(result) : { msg: `${label} ✓`, ok: true };
    msg.value = f.msg;
    ok.value = f.ok;
  } catch (e: any) {
    msg.value = `${label}: ${e.message}`;
    ok.value = false;
  } finally {
    busy.value = false;
    runStartedAt.value = null;
  }
}

const base = () => `/api/devices/${props.device.serial_number}`;
const flash = () =>
  run("flash", () => api.post(`${base()}/flash`, {})).then(loadFlashStats);
const injectNodeDb = () =>
  run(`inject ${nodedbSize.value}-node db`, () =>
    api.post(`${base()}/inject-nodedb`, { size: nodedbSize.value }),
  );
const reboot = () => run("reboot", () => api.post(`${base()}/reboot`, {}));
const factory = () => {
  if (confirm(`Factory-reset ${props.device.friendly_name || props.device.serial_number}?`))
    run("factory-reset", () => api.post(`${base()}/factory-reset`, {}));
};
const getConfig = () =>
  run(
    "get-config",
    () => api.get(`${base()}/config?section=lora`),
    (c) => ({ msg: JSON.stringify(c).slice(0, 120), ok: true }),
  );

// Native (Docker meshtasticd) container lifecycle.
const nativeBase = () => `/api/native/${nativeName.value}`;
const startNode = () => run("start", () => api.post(`${nativeBase()}/start`));
const stopNode = () => run("stop", () => api.post(`${nativeBase()}/stop`));
const restartNode = () => run("restart", () => api.post(`${nativeBase()}/restart`));

const actions = computed(() =>
  isNative.value
    ? [
        { label: "Start", fn: startNode },
        { label: "Stop", fn: stopNode },
        { label: "Restart", fn: restartNode },
        { label: "Config", fn: getConfig },
      ]
    : [
        { label: "Flash", fn: flash },
        { label: "Reboot", fn: reboot },
        { label: "Config", fn: getConfig },
        { label: "Factory Reset", fn: factory, danger: true },
      ],
);

const doSend = () => {
  if (!sendText.value.trim()) return;
  const text = sendText.value;
  run("send-text", () => api.post(`${base()}/send-text`, { text })).then(() => {
    if (ok.value) sendText.value = "";
  });
};

// USB power control (uhubctl). The node's hub port is tracked on the device;
// if it's unmapped the backend auto-binds a unique VID match, or returns 409
// with candidates to pick in device settings.
const serial = computed(() => props.device.serial_number);
const hubLabel = computed(() =>
  props.device.hub_location != null
    ? `hub ${props.device.hub_location}:${props.device.hub_port}`
    : "port unmapped — auto/assign in ⚙",
);
const powerCycle = () =>
  run("power cycle", () => devices.power(serial.value, "cycle"));
const powerOff = () => {
  if (confirm(`Cut USB power to ${props.device.friendly_name || serial.value}?`))
    run("power off", () => devices.power(serial.value, "off"));
};
const powerOn = () => run("power on", () => devices.power(serial.value, "on"));

// Escalating recovery ladder with live step progress over the WS.
const recovering = ref(false);
const recoverStep = ref<string | null>(null);
function onRecovery(d: any) {
  if (!d || d.serial !== serial.value) return;
  if (d.state === "step") recoverStep.value = d.label + (d.skipped ? " (skipped)" : "…");
  else if (d.state === "started") recoverStep.value = "starting…";
}
let tick: ReturnType<typeof setInterval> | undefined;
onMounted(() => {
  ws.subscribe("recovery.update", onRecovery);
  tick = setInterval(() => (nowTick.value = Date.now()), 1000);
});
onUnmounted(() => {
  ws.unsubscribe("recovery.update", onRecovery);
  if (tick) clearInterval(tick);
});

function recover(allowReflash: boolean) {
  const label = allowReflash ? "recover + reflash" : "recover";
  if (allowReflash && !confirm(`Recover ${props.device.friendly_name || serial.value} — escalating to a firmware reflash if needed. Continue?`))
    return;
  recovering.value = true;
  recoverStep.value = "starting…";
  run(
    label,
    () => devices.recover(serial.value, allowReflash),
    (r) => ({
      msg: r.recovered
        ? `recovered via ${r.final_step}`
        : "not recovered — try reflash / check power",
      ok: r.recovered,
    }),
  ).finally(() => {
    recovering.value = false;
    recoverStep.value = null;
  });
}

// Lightweight unwedge: just free the serial port (wait → power-cycle its hub
// slot if genuinely wedged). The node may re-enumerate on a new path.
const unwedge = () =>
  run(
    "unwedge",
    () => devices.unwedge(serial.value),
    (r) => ({
      msg: r.recovered
        ? `unwedged → ${r.new_port}`
        : `not unwedged${r.error ? ": " + r.error : ""}`,
      ok: r.recovered,
    }),
  );
</script>

<template>
  <div class="space-y-2">
    <div
      v-if="tests.running"
      class="text-[11px] text-amber-400/80 bg-amber-950/30 rounded px-2 py-1"
    >
      device control disabled — test run in progress
    </div>
    <div class="flex flex-wrap gap-1.5">
      <button
        v-for="b in actions"
        :key="b.label"
        :disabled="busy || tests.running"
        @click="b.fn"
        class="text-xs px-2 py-1 rounded border transition disabled:opacity-40 disabled:cursor-not-allowed"
        :title="
          b.label === 'Flash' && flashReady
            ? 'prebuilt artifact ready for ' + (fw.ref.short_sha || 'current ref')
            : ''
        "
        :class="[
          b.danger
            ? 'border-rose-800 text-rose-300 hover:bg-rose-950/40'
            : 'border-slate-700 text-slate-300 hover:bg-slate-800',
          b.label === 'Flash' && flashReady ? 'border-emerald-700 text-emerald-300' : '',
        ]"
      >
        {{ b.label === "Flash" && flashReady ? "Flash ✓" : b.label }}
      </button>
    </div>
    <div class="flex gap-1.5">
      <input
        v-model="sendText"
        @keyup.enter="doSend"
        :disabled="tests.running"
        placeholder="send text…"
        class="flex-1 text-xs bg-slate-900 border border-slate-700 rounded px-2 py-1 outline-none focus:border-emerald-700 disabled:opacity-40"
      />
      <button
        @click="doSend"
        :disabled="busy || tests.running"
        class="text-xs px-2 py-1 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-40"
      >
        send
      </button>
    </div>

    <!-- inject fake NodeDB -->
    <div class="flex gap-1.5 items-center">
      <span class="text-[11px] text-slate-500">inject NodeDB</span>
      <select
        v-model.number="nodedbSize"
        :disabled="tests.running"
        class="text-xs bg-slate-900 border border-slate-700 rounded px-1.5 py-1 outline-none disabled:opacity-40"
      >
        <option :value="250">250</option>
        <option :value="500">500</option>
        <option :value="1000">1000</option>
        <option :value="2000">2000</option>
      </select>
      <span class="text-[11px] text-slate-600">nodes</span>
      <button
        @click="injectNodeDb"
        :disabled="busy || tests.running"
        class="text-xs px-2 py-1 rounded border border-sky-800 text-sky-300 hover:bg-sky-950/40 disabled:opacity-40"
        title="XModem-push a fresh fake NodeDB fixture, then reboot"
      >
        inject + reboot
      </button>
    </div>

    <!-- USB power (uhubctl) -->
    <div v-if="!isNative" class="flex gap-1.5 items-center flex-wrap">
      <span class="text-[11px] text-slate-500">power</span>
      <button
        @click="powerCycle"
        :disabled="busy || tests.running"
        class="text-xs px-2 py-1 rounded border border-amber-800 text-amber-300 hover:bg-amber-950/40 disabled:opacity-40"
        title="USB power-cycle this port (uhubctl off → on)"
      >
        ⟳ cycle
      </button>
      <button
        @click="powerOff"
        :disabled="busy || tests.running"
        class="text-xs px-2 py-1 rounded border border-rose-800 text-rose-300 hover:bg-rose-950/40 disabled:opacity-40"
        title="Cut USB power to this port"
      >
        ⏻ off
      </button>
      <button
        @click="powerOn"
        :disabled="busy || tests.running"
        class="text-xs px-2 py-1 rounded border border-emerald-800 text-emerald-300 hover:bg-emerald-950/40 disabled:opacity-40"
        title="Restore USB power to this port"
      >
        ⏼ on
      </button>
      <span class="text-[11px] text-slate-600 mono">{{ hubLabel }}</span>
    </div>

    <!-- troubleshooting / recovery ladder -->
    <div v-if="!isNative" class="flex gap-1.5 items-center flex-wrap">
      <span class="text-[11px] text-slate-500">recover</span>
      <button
        @click="recover(false)"
        :disabled="busy || recovering || tests.running"
        class="text-xs px-2 py-1 rounded border border-indigo-800 text-indigo-300 hover:bg-indigo-950/40 disabled:opacity-40"
        title="escalate: soft reboot → USB power-cycle until the node answers"
      >
        ⛑ recover
      </button>
      <button
        @click="recover(true)"
        :disabled="busy || recovering || tests.running"
        class="text-xs px-2 py-1 rounded border border-amber-800 text-amber-300 hover:bg-amber-950/40 disabled:opacity-40"
        title="also escalate to 1200bps→reflash if reboot + power-cycle don't recover it"
      >
        + reflash
      </button>
      <button
        @click="unwedge"
        :disabled="busy || recovering || tests.running"
        class="text-xs px-2 py-1 rounded border border-teal-800 text-teal-300 hover:bg-teal-950/40 disabled:opacity-40"
        title="free a held/wedged serial port: wait out a holder, then power-cycle its hub slot if the device is wedged"
      >
        ⚡ unwedge
      </button>
      <span
        v-if="recovering && recoverStep"
        class="text-[11px] text-amber-400/90 mono animate-pulse"
        >{{ recoverStep }}</span
      >
    </div>

    <!-- flash timing: direct artifact vs host rebuild -->
    <div
      v-if="flashStats && (flashStats.artifact || flashStats.rebuild)"
      class="text-[11px] text-slate-500"
    >
      flash:
      <span v-if="flashStats.artifact" class="text-emerald-400"
        >{{ flashStats.artifact.duration_s }}s artifact</span
      >
      <span v-if="flashStats.artifact && flashStats.rebuild"> vs </span>
      <span v-if="flashStats.rebuild" class="text-slate-400"
        >{{ flashStats.rebuild.duration_s }}s rebuild</span
      >
      <span v-if="flashStats.speedup" class="text-emerald-300">
        — {{ flashStats.speedup }}× faster</span
      >
    </div>
    <!-- server-backed action stream: phase + live elapsed + last output line -->
    <div
      v-if="serverAction"
      class="text-[11px] mono flex items-center gap-1.5"
      :class="serverAction.state === 'error' ? 'text-rose-400' : 'text-sky-300/90'"
    >
      <span class="shrink-0 uppercase tracking-wide text-slate-500">{{
        serverAction.kind
      }}</span>
      <span
        class="truncate"
        :class="{
          'animate-pulse':
            serverAction.state === 'started' || serverAction.state === 'running',
        }"
        :title="serverAction.last_line || ''"
      >
        <span v-if="serverAction.phase">{{ serverAction.phase }} </span
        >{{ actionStream.elapsedFor(serverAction) }}s<span v-if="serverAction.last_line">
          — {{ serverAction.last_line }}</span
        ><span v-if="serverAction.state === 'done'"> ✓</span>
      </span>
    </div>
    <div
      v-if="msg"
      class="text-[11px] mono truncate flex items-center gap-1.5"
      :class="ok ? 'text-slate-500' : 'text-rose-400'"
      :title="msg"
    >
      <span class="truncate">{{ msg }}</span>
      <span
        v-if="runElapsed != null"
        class="tabular-nums text-amber-400/80 animate-pulse shrink-0"
        >{{ fmtElapsed(runElapsed) }}</span
      >
    </div>
  </div>
</template>
