<!-- SPDX-FileCopyrightText: Meshtastic contributors -->
<!-- SPDX-License-Identifier: GPL-3.0-only -->

<script setup lang="ts">
import { onMounted, ref } from "vue";
import AppBar from "./components/AppBar.vue";
import DeviceGrid from "./components/DeviceGrid.vue";
import NightlyPanel from "./components/NightlyPanel.vue";
import TestDashboard from "./components/TestDashboard.vue";
import { useActionsStore } from "./stores/actions";
import { useBuildsStore } from "./stores/builds";
import { useCamerasStore } from "./stores/cameras";
import { useDatadogStore } from "./stores/datadog";
import { useDevicesStore } from "./stores/devices";
import { useKeepAliveStore } from "./stores/keepalive";
import { useFirmwareStore } from "./stores/firmware";
import { useNightlyStore } from "./stores/nightly";
import { useTestsStore } from "./stores/tests";
import { useWsStore } from "./stores/ws";

const tab = ref("fleet");

const ws = useWsStore();
const devices = useDevicesStore();
const cameras = useCamerasStore();
const firmware = useFirmwareStore();
const tests = useTestsStore();
const builds = useBuildsStore();
const datadog = useDatadogStore();
const keepalive = useKeepAliveStore();
const nightly = useNightlyStore();
const actions = useActionsStore();

onMounted(() => {
  ws.connect();
  devices.init();
  cameras.init();
  firmware.init();
  tests.init();
  builds.init();
  datadog.init();
  keepalive.init();
  nightly.init();
  actions.init();
});
</script>

<template>
  <div class="min-h-screen">
    <AppBar :tab="tab" @update:tab="(v) => (tab = v)" />
    <main>
      <DeviceGrid v-show="tab === 'fleet'" />
      <TestDashboard v-if="tab === 'tests'" />
      <NightlyPanel v-if="tab === 'nightly'" />
    </main>
  </div>
</template>
