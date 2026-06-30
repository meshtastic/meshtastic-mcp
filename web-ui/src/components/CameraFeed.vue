<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { api } from "../api/client";
import { useCamerasStore } from "../stores/cameras";
import type { Camera } from "../types";

const props = defineProps<{ camera?: Camera }>();
const cameras = useCamerasStore();

const errored = ref(false);
const statusMsg = ref<string | null>(null);
const fullscreen = ref(false);
// Cache-bust so reassign / remount restarts the stream.
const nonce = ref(Date.now());

const src = computed(() =>
  props.camera
    ? `/api/cameras/${props.camera.id}/stream.mjpg?t=${nonce.value}`
    : "",
);

const rotation = computed(() => props.camera?.rotation ?? 0);
const mirrored = computed(() => !!props.camera?.mirror);

// Rotation + mirror are pure CSS (the MJPEG stream isn't restarted). For 90/270
// we scale a 16:9 feed (filling the 16:9 box) by 9/16 so it fits after the
// quarter turn. Mirror is a horizontal flip applied before the rotation.
const imgStyle = computed(() => {
  const r = rotation.value;
  const scale = r === 90 || r === 270 ? 0.5625 : 1;
  const flip = mirrored.value ? " scaleX(-1)" : "";
  return {
    transform: `rotate(${r}deg) scale(${scale})${flip}`,
    transition: "transform 0.2s ease",
  };
});

// Only the camera id changing should restart the stream (not a rotation save).
watch(
  () => props.camera?.id,
  () => {
    errored.value = false;
    statusMsg.value = null;
    fullscreen.value = false;
    nonce.value = Date.now();
  },
);

async function onError() {
  errored.value = true;
  fullscreen.value = false;
  if (!props.camera) return;
  try {
    const s = await api.get<{ ok: boolean; error: string | null }>(
      `/api/cameras/${props.camera.id}/status`,
    );
    statusMsg.value = s.ok ? "stream interrupted" : s.error;
  } catch {
    statusMsg.value = "camera unavailable";
  }
}

function retry() {
  errored.value = false;
  statusMsg.value = null;
  nonce.value = Date.now();
}

async function rotate() {
  if (!props.camera) return;
  try {
    await cameras.setRotation(props.camera.id, (rotation.value + 90) % 360);
  } catch {
    /* ignore — transient */
  }
}

async function mirror() {
  if (!props.camera) return;
  try {
    await cameras.setMirror(props.camera.id, !mirrored.value);
  } catch {
    /* ignore — transient */
  }
}

function onKey(e: KeyboardEvent) {
  if (e.key === "Escape") fullscreen.value = false;
}
onMounted(() => window.addEventListener("keydown", onKey));
onUnmounted(() => window.removeEventListener("keydown", onKey));
</script>

<template>
  <div
    class="relative aspect-video w-full bg-black rounded-md overflow-hidden border border-slate-800"
  >
    <template v-if="camera && !errored">
      <!-- Placeholder kept in the card while the live feed is teleported out. -->
      <div
        v-if="fullscreen"
        class="absolute inset-0 flex items-center justify-center gap-2 text-xs text-slate-500"
      >
        previewing fullscreen ·
        <button @click="fullscreen = false" class="underline hover:text-slate-300">
          exit
        </button>
      </div>

      <!-- The same <img> element relocates into the modal (Teleport preserves it),
           so the MJPEG stream is never restarted or doubled. -->
      <Teleport to="body" :disabled="!fullscreen">
        <div
          :class="
            fullscreen
              ? 'fixed inset-0 z-50 flex items-center justify-center bg-slate-950/95 backdrop-blur p-4 sm:p-10'
              : 'absolute inset-0'
          "
          @click.self="fullscreen = false"
        >
          <img
            :src="src"
            :style="imgStyle"
            :class="[
              'object-contain',
              fullscreen
                ? 'max-w-full max-h-full cursor-zoom-out'
                : 'w-full h-full cursor-zoom-in',
            ]"
            @click="fullscreen = !fullscreen"
            @error="onError"
            alt="camera feed"
          />
          <span
            class="absolute top-2 left-2 text-[11px] px-1.5 py-0.5 rounded bg-black/60 text-emerald-300"
            >● {{ camera.name }}
            <span v-if="fullscreen" class="mono text-slate-400"
              >· idx {{ camera.device_index }}</span
            ></span
          >
          <div class="absolute top-2 right-2 flex gap-1">
            <button
              @click="mirror"
              class="p-1.5 rounded bg-black/60 transition"
              :class="
                mirrored
                  ? 'text-emerald-300'
                  : 'text-slate-300 hover:text-emerald-300'
              "
              :title="
                mirrored ? 'mirror: on (horizontal flip)' : 'mirror (horizontal flip)'
              "
            >
              <svg
                viewBox="0 0 24 24"
                class="w-3.5 h-3.5"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              >
                <path d="M12 3v18" />
                <path d="M16 7l4 5-4 5" />
                <path d="M8 7l-4 5 4 5" />
              </svg>
            </button>
            <button
              @click="rotate"
              class="p-1.5 rounded bg-black/60 text-slate-300 hover:text-emerald-300 transition"
              :title="`rotate (now ${rotation}°)`"
            >
              <svg
                viewBox="0 0 24 24"
                class="w-3.5 h-3.5"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              >
                <polyline points="23 4 23 10 17 10" />
                <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
              </svg>
            </button>
            <button
              @click="fullscreen = !fullscreen"
              class="p-1.5 rounded bg-black/60 text-slate-300 hover:text-emerald-300 transition"
              :title="fullscreen ? 'exit fullscreen (Esc)' : 'fullscreen preview'"
            >
              <svg
                v-if="!fullscreen"
                viewBox="0 0 24 24"
                class="w-3.5 h-3.5"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              >
                <path d="M15 3h6v6" />
                <path d="M9 21H3v-6" />
                <path d="M21 3l-7 7" />
                <path d="M3 21l7-7" />
              </svg>
              <svg
                v-else
                viewBox="0 0 24 24"
                class="w-3.5 h-3.5"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              >
                <path d="M4 14h6v6" />
                <path d="M20 10h-6V4" />
                <path d="M14 10l6-6" />
                <path d="M10 14l-6 6" />
              </svg>
            </button>
          </div>
        </div>
      </Teleport>
    </template>

    <div
      v-else-if="camera && errored"
      class="absolute inset-0 flex flex-col items-center justify-center gap-2 text-center px-3"
    >
      <span class="text-rose-400 text-sm">⚠ no signal</span>
      <span class="text-xs text-slate-500">{{
        statusMsg || "camera produced no frames"
      }}</span>
      <button
        @click="retry"
        class="text-xs px-2 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300"
      >
        retry
      </button>
    </div>

    <div
      v-else
      class="absolute inset-0 flex items-center justify-center text-xs text-slate-600"
    >
      no camera assigned
    </div>
  </div>
</template>
