# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Entry point for `python -m meshtastic_mcp` and the `meshtastic-mcp` console script.

Default (no args): run the MCP server over stdio.
Subcommands:
  install [--client C] [--local] [--print]   register this server in an MCP client's config
                                (default client: claude-code, user scope) + install skills
  uninstall [--client C] [--purge-skills]    remove the server registration (and optionally skills)
  doctor [--json]               probe external deps and print how to acquire any missing ones
  skills install/uninstall [--dest DIR]      copy/remove the bundled agent skills
                                (default: ~/.agents/skills)
  provision [--dir DIR]         clone missing source repos (firmware/android/apple) and
                                print the export commands to activate them

  -- Read-only device/hardware queries (no MCP server needed) --
  devices [--all] [--json]                  list connected Meshtastic devices
  boards [--arch ARCH] [--query Q]          list PlatformIO board environments
    boards get ENV                          show full metadata for one board env
  info PORT [--json]                        firmware version / region / node info
  nodes PORT [--json]                       mesh peers visible to this node

Examples:
  meshtastic-mcp devices
  meshtastic-mcp boards --arch esp32s3 --query heltec
  meshtastic-mcp boards get heltec-v3
  meshtastic-mcp info /dev/ttyUSB0
  meshtastic-mcp nodes /dev/ttyUSB0
"""

from __future__ import annotations

import argparse
import json as _json_mod
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_SKILLS_SRC = Path(__file__).resolve().parent / "skills"
_DEFAULT_SKILLS_DEST = Path.home() / ".agents" / "skills"

# Candidate .env files loaded (in order) when a MESHTASTIC_* var is not set.
# Each file may contain `export KEY=value` or `KEY=value` lines.
_ENV_CANDIDATES = [
    Path.home() / ".config" / "meshtastic-mcp" / ".env",
    Path.home() / ".local" / "share" / "meshtastic-mcp" / "repos" / ".env",
]


def _load_env_files() -> None:
    """Load MESHTASTIC_* env vars from candidate .env files (first-found wins per key).

    Only sets vars that are not already present in the environment, so explicit
    env vars (from the shell, MCP client config, etc.) always take precedence.
    Any line not matching `[export] KEY=value` is silently ignored.
    """
    _kv = re.compile(r"^\s*(?:export\s+)?(MESHTASTIC_\w+)=(.+)")
    for path in _ENV_CANDIDATES:
        if not path.is_file():
            continue
        try:
            for raw in path.read_text().splitlines():
                m = _kv.match(raw)
                if m and m.group(1) not in os.environ:
                    os.environ[m.group(1)] = m.group(2).strip().strip("\"'")
        except OSError:
            pass


def _bundled_skill_names() -> list[str]:
    if not _SKILLS_SRC.is_dir():
        return []
    return sorted(p.name for p in _SKILLS_SRC.iterdir() if p.is_dir())


def _skills_install(dest: Path) -> int:
    if not _SKILLS_SRC.is_dir():
        print(f"no bundled skills found at {_SKILLS_SRC}", file=sys.stderr)
        return 1
    dest.mkdir(parents=True, exist_ok=True)
    installed = []
    for pack in _SKILLS_SRC.iterdir():
        if not pack.is_dir():
            continue
        target = dest / pack.name
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(pack, target)
        installed.append(pack.name)
    print(f"installed {len(installed)} skill(s) into {dest}: {', '.join(installed)}")
    return 0


def _skills_uninstall(dest: Path) -> int:
    removed = []
    for name in _bundled_skill_names():
        target = dest / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed.append(name)
    if removed:
        print(f"removed {len(removed)} skill(s) from {dest}: {', '.join(removed)}")
    else:
        print(f"no bundled skills found in {dest}")
    return 0


# ---------------------------------------------------------------------------
# MCP client registration (install / uninstall the server)
# ---------------------------------------------------------------------------


# Where common MCP clients keep their `mcpServers` JSON config.
def _client_config_path(client: str, scope: str) -> Path:
    home = Path.home()
    if client == "claude-code":
        return (home / ".claude.json") if scope == "user" else (Path.cwd() / ".mcp.json")
    if client == "claude-desktop":
        if sys.platform == "darwin":
            return (
                home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
            )
        if os.name == "nt":
            return Path(os.environ.get("APPDATA", home)) / "Claude" / "claude_desktop_config.json"
        return home / ".config" / "Claude" / "claude_desktop_config.json"
    if client == "cursor":
        return (
            (home / ".cursor" / "mcp.json")
            if scope == "user"
            else (Path.cwd() / ".cursor" / "mcp.json")
        )
    if client == "windsurf":
        return home / ".codeium" / "windsurf" / "mcp_config.json"
    raise ValueError(f"unknown client {client!r}")


def _server_entry(local: bool, env: dict[str, str]) -> dict:
    """The mcpServers entry for this server. `local` registers the current
    interpreter (`python -m meshtastic_mcp`, for editable/dev installs); the
    default is the zero-install `uvx meshtastic-mcp`."""
    if local:
        entry: dict = {"command": sys.executable, "args": ["-m", "meshtastic_mcp"]}
    else:
        entry = {"command": "uvx", "args": ["meshtastic-mcp"]}
    if env:
        entry["env"] = env
    return entry


def _load_json_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = _json_mod.loads(path.read_text() or "{}")
    except _json_mod.JSONDecodeError as exc:
        raise SystemExit(
            f"{path} is not valid JSON ({exc}); refusing to edit. "
            f"Fix it, or use --print to copy the snippet manually."
        ) from exc
    return data if isinstance(data, dict) else {}


def _install(args) -> int:
    name = args.name
    entry = _server_entry(args.local, dict(args.env or []))
    if args.print:
        print(_json_mod.dumps({"mcpServers": {name: entry}}, indent=2))
        return 0
    path = Path(args.config) if args.config else _client_config_path(args.client, args.scope)
    config = _load_json_config(path)
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SystemExit(f"{path}: 'mcpServers' is not an object; refusing to edit.")
    action = "updated" if name in servers else "registered"
    servers[name] = entry
    if args.dry_run:
        print(f"[dry-run] would write to {path}:")
        print(_json_mod.dumps({"mcpServers": {name: entry}}, indent=2))
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_mod.dumps(config, indent=2) + "\n")
    print(f"{action} MCP server '{name}' in {path}")
    print(f"  command: {entry['command']} {' '.join(entry['args'])}")
    if not args.no_skills:
        _skills_install(args.skills_dest)
    print("\nRestart your MCP client to pick up the change.")
    return 0


def _uninstall(args) -> int:
    name = args.name
    path = Path(args.config) if args.config else _client_config_path(args.client, args.scope)
    config = _load_json_config(path)
    servers = config.get("mcpServers")
    if isinstance(servers, dict) and name in servers:
        del servers[name]
        path.write_text(_json_mod.dumps(config, indent=2) + "\n")
        print(f"unregistered MCP server '{name}' from {path}")
    else:
        print(f"MCP server '{name}' not found in {path}")
    if args.purge_skills:
        _skills_uninstall(args.skills_dest)
    return 0


def _default_provision_dir() -> Path:
    try:
        from platformdirs import user_data_dir

        return Path(user_data_dir("meshtastic-mcp")) / "repos"
    except Exception:
        return Path.home() / ".local" / "share" / "meshtastic-mcp" / "repos"


_REPOS = [
    # (env_var, dir_name, clone_url, sentinel)
    (
        "MESHTASTIC_FIRMWARE_ROOT",
        "firmware",
        "https://github.com/meshtastic/firmware",
        "platformio.ini",
    ),
    (
        "MESHTASTIC_ANDROID_ROOT",
        "Meshtastic-Android",
        "https://github.com/meshtastic/Meshtastic-Android",
        "gradlew",
    ),
    (
        "MESHTASTIC_APPLE_ROOT",
        "Meshtastic-Apple",
        "https://github.com/meshtastic/Meshtastic-Apple",
        "Meshtastic.xcworkspace",
    ),
]


def _provision(base_dir: Path) -> int:
    base_dir.mkdir(parents=True, exist_ok=True)
    exports: list[str] = []
    ok = True

    for env_var, dir_name, clone_url, sentinel in _REPOS:
        existing = os.environ.get(env_var)
        if existing:
            print(f"  {env_var} already set → {existing}")
            exports.append(f"export {env_var}={existing}")
            continue

        dest = base_dir / dir_name
        if dest.is_dir() and (dest / sentinel).exists():
            print(f"  {dir_name}: already cloned at {dest}")
        else:
            print(f"  cloning {clone_url} → {dest}")
            try:
                subprocess.run(
                    ["git", "clone", "--recurse-submodules", clone_url, str(dest)],
                    check=True,
                )
            except subprocess.CalledProcessError:
                print(f"  ERROR: failed to clone {clone_url}", file=sys.stderr)
                ok = False
                continue

        exports.append(f"export {env_var}={dest}")

    # Write to both the repos dir and the canonical config location so
    # `meshtastic-mcp` CLI picks them up automatically in future runs.
    env_content = "\n".join(exports) + "\n"
    env_file = base_dir / ".env"
    env_file.write_text(env_content)

    config_env = Path.home() / ".config" / "meshtastic-mcp" / ".env"
    config_env.parent.mkdir(parents=True, exist_ok=True)
    config_env.write_text(env_content)

    print()
    print("Add to your shell profile (or source this block):")
    print()
    for line in exports:
        print(f"  {line}")
    print()
    print(f"Auto-loaded by meshtastic-mcp CLI: {config_env}")
    print(f"Sourceable shell file:              {env_file}")
    print(f"  source {env_file}")

    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Read-only CLI command implementations
# ---------------------------------------------------------------------------


def _print_result(data: object, as_json: bool) -> None:
    """Pretty-print a dict/list as JSON or a human table."""
    if as_json:
        print(_json_mod.dumps(data, indent=2, default=str))
        return
    if isinstance(data, list):
        for item in data:
            _print_result(item, as_json=False)
            print()
    elif isinstance(data, dict):
        for k, v in data.items():
            print(f"  {k:<24} {v}")
    else:
        print(data)


def _cmd_devices(args) -> int:
    from meshtastic_mcp import devices as _devices

    result = _devices.list_devices(include_unknown=args.all)
    if not result:
        print("no devices found" + (" (try --all to see all serial ports)" if not args.all else ""))
        return 0
    if args.json:
        print(_json_mod.dumps(result, indent=2, default=str))
        return 0
    for d in result:
        flag = " [meshtastic]" if d.get("likely_meshtastic") else ""
        desc = d.get("description") or ""
        vid = d.get("vid") or ""
        print(f"  {d['port']}{flag}  {desc}  {vid}")
    return 0


def _cmd_boards(args) -> int:
    # boards get ENV
    if getattr(args, "boards_cmd", None) == "get":
        from meshtastic_mcp import boards as _boards

        try:
            result = _boards.get_board(args.env)
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        _print_result(result, getattr(args, "json", False))
        return 0

    # boards list
    from meshtastic_mcp import boards as _boards

    board_list = _boards.list_boards(
        architecture=args.arch,
        query=args.query,
        actively_supported_only=args.supported,
    )
    if not board_list:
        print("no boards matched")
        return 0
    if args.json:
        print(_json_mod.dumps(board_list, indent=2, default=str))
        return 0
    print(f"  {'env':<30} {'arch':<12} {'display_name'}")
    print("  " + "-" * 70)
    for b in board_list:
        arch = b.get("architecture") or ""
        print(f"  {b['env']:<30} {arch!s:<12} {b.get('display_name', '')}")
    return 0


def _cmd_info(args) -> int:
    from meshtastic_mcp import info as _info

    try:
        result = _info.device_info(args.port)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(_json_mod.dumps(result, indent=2, default=str))
        return 0
    # human-readable summary
    print(f"  port            {result.get('port')}")
    print(f"  hw_model        {result.get('hw_model')}")
    print(f"  firmware        {result.get('firmware_version')}")
    print(f"  region          {result.get('region')}")
    print(f"  node_num        {result.get('my_node_num')}")
    print(f"  long_name       {result.get('long_name')}")
    print(f"  short_name      {result.get('short_name')}")
    print(f"  primary_channel {result.get('primary_channel')}")
    print(f"  num_nodes       {result.get('num_nodes')}")
    return 0


def _cmd_nodes(args) -> int:
    from meshtastic_mcp import info as _info

    try:
        result = _info.list_nodes(args.port)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(_json_mod.dumps(result, indent=2, default=str))
        return 0
    print(
        f"  {'node_num':<12} {'short':<6} {'long_name':<30} {'hw_model':<25} {'snr':<6} last_heard"
    )
    print("  " + "-" * 95)
    for n in result:
        user = n.get("user") or {}
        ts = n.get("last_heard")
        heard = str(ts) if ts else "—"
        snr = n.get("snr") or "—"
        print(
            f"  {n.get('node_num', '')!s:<12} "
            f"{user.get('short_name', ''):<6} "
            f"{user.get('long_name', ''):<30} "
            f"{user.get('hw_model', ''):<25} "
            f"{snr!s:<6} "
            f"{heard}"
        )
    return 0


def _cmd_capture_stats(args) -> int:
    """Compute the realism stat schema for a capture (SQLite / JSONL / preset)."""
    from meshtastic_mcp.replay import capture as _capture
    from meshtastic_mcp.replay import metrics as _metrics
    from meshtastic_mcp.replay import sim as _sim

    src = args.source
    try:
        if src in _sim.PRESETS or src == "sim":
            profile = "meshcon" if src == "sim" else src
            cap = _sim.generate(
                nodes=args.sim_nodes, days=args.sim_days, seed=args.sim_seed, profile=profile
            )
        elif src.endswith(".jsonl"):
            cap = _capture.from_recorder_jsonl(src)
        else:
            cap = _capture.from_sqlite(src, limit_nodes=0)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    stats = _metrics.capture_stats(cap)
    # observation-level extras only the SQLite packet_seen table carries
    if not (src in _sim.PRESETS or src == "sim" or src.endswith(".jsonl")):
        try:
            stats["observed"] = _metrics.sqlite_extra_stats(_capture._resolve_db(src))
        except Exception:
            pass
    if args.json:
        print(_json_mod.dumps(stats, indent=2, default=str))
        return 0
    dec = sum(stats["portnum_mix"].values()) or 1
    print(
        f"capture: {stats['label']}  ({stats['packets']} pkts, {stats['span_hours']} h, "
        f"{stats['nodes']['count']} nodes, {stats['pkts_per_hour']} pkts/hr)"
    )
    print(f"  encrypted: {stats['encrypted_fraction']}   tak_packets: {stats['tak_packets']}")
    print("  portnum mix:")
    for name, n in list(stats["portnum_mix"].items())[:10]:
        print(f"    {name:<16} {n:>8}  ({100 * n / dec:4.1f}%)")
    txt = stats["text"]["len"]
    print(
        f"  text: n={stats['text']['n']} p50={txt['p50']} p90={txt['p90']} max={txt['max']} "
        f"dm={stats['text']['dm_fraction']}"
    )
    print(
        f"  talker skew (top1/top10): {stats['talker_skew']['top1pct_share']} / "
        f"{stats['talker_skew']['top10pct_share']}"
    )
    print(f"  position interval p50: {stats['position']['interval_s']['p50']} s")
    tel = stats["telemetry"]
    print(f"  telemetry variants: {tel['variant_mix']}")
    ch = tel["chutil"]
    print(f"  chutil p50/p90/max: {ch['p50']}/{ch['p90']}/{ch['max']}")
    if tel["env_fields"]:
        print(f"  env fields: {tel['env_fields']}")
    rx = stats["rx"]
    if rx["rssi"]["n"]:
        print(f"  rx snr p50={rx['snr']['p50']}  rssi p50={rx['rssi']['p50']}")
    return 0


def _cmd_watch(args) -> int:
    """Live-tail a recorder JSONL stream. Ctrl-C to stop."""
    import time

    from meshtastic_mcp import log_query

    key = {"logs": "lines", "packets": "packets", "events": "events"}[args.stream]

    def query(start, end="now"):
        if args.stream == "logs":
            return log_query.logs_window(start=start, end=end)
        if args.stream == "packets":
            return log_query.packets_window(start=start, end=end)
        return log_query.events_window(start=start, end=end)

    print(f"Watching {args.stream} (Ctrl-C to stop)…", file=sys.stderr)
    seen: set[str] = set()
    try:
        # Prime with recent history so we don't replay the whole buffer.
        last = time.time()
        while True:
            window = query(start=last - args.interval - 1, end="now")
            for rec in window.get(key, []):
                # Dedupe by a stable signature (ts + a short content slice).
                sig = f"{rec.get('ts')}-{str(rec.get('line') or rec.get('id') or rec)[:60]}"
                if sig in seen:
                    continue
                seen.add(sig)
                line = (
                    rec.get("line") if args.stream == "logs" else _json_mod.dumps(rec, default=str)
                )
                print(line)
            # Bound the dedupe set so it doesn't grow without limit.
            if len(seen) > 5000:
                seen = set(list(seen)[-2000:])
            last = time.time()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
        return 0


def _cmd_completion(args) -> int:
    """Print a shell completion script for bash or zsh."""
    cmds = "install uninstall doctor skills provision devices boards info nodes watch completion"
    if args.shell == "bash":
        print(f"""# meshtastic-mcp bash completion
# Add to ~/.bashrc:  eval "$(meshtastic-mcp completion bash)"
_meshtastic_mcp() {{
    local cur="${{COMP_WORDS[COMP_CWORD]}}"
    if [ "$COMP_CWORD" -eq 1 ]; then
        COMPREPLY=( $(compgen -W "{cmds}" -- "$cur") )
    fi
}}
complete -F _meshtastic_mcp meshtastic-mcp""")
    else:  # zsh
        print(f"""# meshtastic-mcp zsh completion
# Add to ~/.zshrc:  eval "$(meshtastic-mcp completion zsh)"
_meshtastic_mcp() {{
    local -a cmds
    cmds=({cmds})
    _describe 'command' cmds
}}
compdef _meshtastic_mcp meshtastic-mcp""")
    return 0


def main(argv=None) -> None:
    _load_env_files()
    p = argparse.ArgumentParser(
        prog="meshtastic-mcp",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd")

    doc = sub.add_parser("doctor", help="probe external deps + how to acquire missing ones")
    doc.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    sk = sub.add_parser("skills", help="manage bundled agent skills")
    sksub = sk.add_subparsers(dest="skills_cmd", required=True)
    inst = sksub.add_parser("install", help="copy bundled skills into the skills dir")
    inst.add_argument("--dest", type=Path, default=_DEFAULT_SKILLS_DEST)
    skun = sksub.add_parser("uninstall", help="remove the bundled skills from the skills dir")
    skun.add_argument("--dest", type=Path, default=_DEFAULT_SKILLS_DEST)

    def _env_kv(s: str) -> tuple[str, str]:
        k, _, v = s.partition("=")
        return (k, v)

    _CLIENTS = ("claude-code", "claude-desktop", "cursor", "windsurf")
    ins = sub.add_parser("install", help="register this server in an MCP client config")
    ins.add_argument("--client", choices=_CLIENTS, default="claude-code")
    ins.add_argument("--scope", choices=("user", "project"), default="user")
    ins.add_argument("--config", default=None, metavar="PATH", help="explicit config file to edit")
    ins.add_argument("--name", default="meshtastic", help="server name in the config")
    ins.add_argument(
        "--local",
        action="store_true",
        help="register the current interpreter (python -m) instead of uvx",
    )
    ins.add_argument(
        "--env",
        action="append",
        type=_env_kv,
        metavar="KEY=VALUE",
        help="env var to bake into the entry (repeatable)",
    )
    ins.add_argument("--no-skills", action="store_true", help="don't also install the skills")
    ins.add_argument("--skills-dest", type=Path, default=_DEFAULT_SKILLS_DEST)
    ins.add_argument("--print", action="store_true", help="print the JSON snippet, edit nothing")
    ins.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")

    uns = sub.add_parser("uninstall", help="remove this server's MCP client registration")
    uns.add_argument("--client", choices=_CLIENTS, default="claude-code")
    uns.add_argument("--scope", choices=("user", "project"), default="user")
    uns.add_argument("--config", default=None, metavar="PATH")
    uns.add_argument("--name", default="meshtastic")
    uns.add_argument("--purge-skills", action="store_true", help="also remove installed skills")
    uns.add_argument("--skills-dest", type=Path, default=_DEFAULT_SKILLS_DEST)

    prov = sub.add_parser(
        "provision",
        help="clone missing source repos (firmware/android/apple) and print export commands",
    )
    prov.add_argument(
        "--dir",
        type=Path,
        default=None,
        metavar="DIR",
        help=f"base directory for cloned repos (default: {_default_provision_dir()})",
    )

    # --- read-only device / hardware queries ---
    dev = sub.add_parser("devices", help="list connected Meshtastic devices")
    dev.add_argument("--all", action="store_true", help="include non-Meshtastic serial ports")
    dev.add_argument("--json", action="store_true", help="emit JSON")

    brd = sub.add_parser("boards", help="list / inspect PlatformIO board environments")
    brd.add_argument("--arch", default=None, help="filter by architecture (esp32s3, nrf52840 …)")
    brd.add_argument("--query", default=None, metavar="Q", help="substring filter on name/env")
    brd.add_argument("--supported", action="store_true", help="only actively-supported boards")
    brd.add_argument("--json", action="store_true", help="emit JSON")
    brdsub = brd.add_subparsers(dest="boards_cmd")
    brdget = brdsub.add_parser("get", help="full metadata for one board env")
    brdget.add_argument("env", help="PlatformIO env name (e.g. heltec-v3)")
    brdget.add_argument("--json", action="store_true", help="emit JSON")

    inf = sub.add_parser("info", help="device firmware/region/node info")
    inf.add_argument("port", help="serial port or tcp://host:port")
    inf.add_argument("--json", action="store_true", help="emit JSON")

    nod = sub.add_parser("nodes", help="list mesh peers visible to this node")
    nod.add_argument("port", help="serial port or tcp://host:port")
    nod.add_argument("--json", action="store_true", help="emit JSON")

    wat = sub.add_parser("watch", help="live-tail recorder streams (logs/packets/events)")
    wat.add_argument(
        "stream",
        choices=("logs", "packets", "events"),
        help="which recorder stream to follow",
    )
    wat.add_argument("--interval", type=float, default=2.0, help="poll interval seconds")

    cst = sub.add_parser(
        "capture-stats",
        help="compute realism statistics for a capture (SQLite/JSONL) or a sim preset",
    )
    cst.add_argument(
        "source",
        help="path to a *.db/*.db.gz/*.jsonl capture, or a sim preset (meshcon/burningman/defcon)",
    )
    cst.add_argument("--sim-nodes", type=int, default=800, help="nodes when source is a preset")
    cst.add_argument("--sim-days", type=int, default=3, help="days when source is a preset")
    cst.add_argument("--sim-seed", type=int, default=1337, help="seed when source is a preset")
    cst.add_argument("--json", action="store_true", help="emit JSON")

    comp = sub.add_parser("completion", help="print a shell completion script")
    comp.add_argument("shell", choices=("bash", "zsh"), help="target shell")

    args = p.parse_args(argv)

    if args.cmd == "doctor":
        from meshtastic_mcp import doctor

        rep = doctor.run()
        if args.json:
            print(_json_mod.dumps(rep.to_dict(), indent=2))
        else:
            print(doctor.report(rep))
        raise SystemExit(1 if rep.missing else 0)

    if args.cmd == "install":
        raise SystemExit(_install(args))

    if args.cmd == "uninstall":
        raise SystemExit(_uninstall(args))

    if args.cmd == "skills":
        if args.skills_cmd == "uninstall":
            raise SystemExit(_skills_uninstall(args.dest))
        raise SystemExit(_skills_install(args.dest))

    if args.cmd == "provision":
        base = args.dir or _default_provision_dir()
        print(f"Provisioning source repos under {base}")
        print()
        raise SystemExit(_provision(base))

    if args.cmd == "devices":
        raise SystemExit(_cmd_devices(args))

    if args.cmd == "boards":
        raise SystemExit(_cmd_boards(args))

    if args.cmd == "info":
        raise SystemExit(_cmd_info(args))

    if args.cmd == "nodes":
        raise SystemExit(_cmd_nodes(args))

    if args.cmd == "watch":
        raise SystemExit(_cmd_watch(args))

    if args.cmd == "capture-stats":
        raise SystemExit(_cmd_capture_stats(args))

    if args.cmd == "completion":
        raise SystemExit(_cmd_completion(args))

    # default: run the MCP server
    from meshtastic_mcp.server import app

    app.run()


if __name__ == "__main__":
    main()
