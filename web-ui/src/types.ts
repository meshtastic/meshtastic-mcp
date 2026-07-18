// SPDX-FileCopyrightText: Meshtastic contributors
// SPDX-License-Identifier: GPL-3.0-only

export interface Device {
  serial_number: string;
  node_num: number | null;
  friendly_name: string | null;
  hw_model: string | null;
  vid: string | null;
  pid: string | null;
  role: string | null;
  current_port: string | null;
  firmware_version: string | null;
  region: string | null;
  env: string | null;
  env_locked: number;
  flashed_fw_branch: string | null;
  flashed_fw_sha: string | null;
  flashed_at: number | null;
  hub_location: string | null;
  hub_port: number | null;
  online: number;
  first_seen: number;
  last_seen: number;
  has_stable_id: boolean;
  stale: boolean;
}

export interface Camera {
  id: number;
  name: string;
  type: string;
  device_index: string | null;
  backend: string | null;
  rotation: number;
  mirror: number;
  enabled: number;
  created_at: number;
  device_serial: string | null;
  assigned_at: number | null;
  deleted?: boolean;
}

export interface FirmwareRef {
  available: boolean;
  branch?: string | null;
  sha?: string | null;
  short_sha?: string | null;
  dirty?: boolean | null;
  subject?: string | null;
  committed_at?: string | null;
}

export interface TestLeaf {
  nodeid: string;
  tier: string;
  file: string;
  testname: string;
  outcome: string; // pending | running | passed | failed | skipped
  duration?: number | null;
}

export interface TestRun {
  id: number;
  started_at: number;
  finished_at: number | null;
  exit_code: number | null;
  fw_branch: string | null;
  fw_sha: string | null;
  passed: number;
  failed: number;
  skipped: number;
}

export interface NightlyConfig {
  enabled: boolean;
  hour: number;
  minute: number;
  self_update: boolean;
  prebuild: boolean;
  force_bake: boolean;
  suite_args: string[];
  catchup_window_h: number;
  suite_timeout_h: number;
  firmware_branch: string;
  firmware_url: string;
  soak_hours: number;
  soak_traffic_interval_min: number;
  soak_snapshot_interval_min: number;
  soak_keepalive: boolean;
  llm_autostart: boolean;
  recovery_allow_reflash: boolean;
  pipeline_timeout_h: number;
  keep_nights: number;
}

export interface NightlyState {
  active: boolean;
  step: string | null;
  nightly_id: number | null;
  next_run_at: string | null;
  last: NightlyRun | null;
}

export interface NightlyStatus {
  config: NightlyConfig;
  state: NightlyState;
}

export interface NightlyReportConfig {
  enabled: boolean;
  repo: string;
  auto_create_repo: boolean;
  max_body_kb: number;
}

export interface NightlyReportMeta {
  nightly_run_id: number;
  created_at: number;
  status: string; // posted | disabled | gh_* | repo_missing | ... | reporter_error
  issue_url: string | null;
  error: string | null;
  title: string | null;
  failures: number;
  observations: number;
  body_md?: string | null;
}

export interface NightlyRun {
  id: number;
  scheduled_for: number;
  started_at: number;
  finished_at: number | null;
  status: string; // running | awaiting_restart | passed | failed | error | canceled
  step: string | null;
  trigger: string;
  run_id: number | null;
  suite_attempts: number;
  soak_started_at: number | null;
  mcp_sha_before: string | null;
  mcp_sha_after: string | null;
  fw_sha_before: string | null;
  fw_sha_after: string | null;
  summary: {
    passed: number;
    failed: number;
    skipped: number;
    exit_code: number | null;
  } | null;
  report?: NightlyReportMeta | null;
}

export interface NightlyObservation {
  id: number;
  step: string;
  severity: string; // info | warn | error
  kind: string;
  message: string;
  data: Record<string, unknown> | null;
  ts: number;
}

export interface NightlyRunDetail extends NightlyRun {
  observations: NightlyObservation[];
  run?: TestRun | null;
}
