# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Environment doctor: probe every external dependency and tell you how to get it.

`capabilities.detect()` answers *which capability groups light up*; this module answers
the follow-up an agent (or a human) actually needs: **for each missing dependency, the
exact, current command to acquire it on this platform.**

Design goals (dev + agent ergonomics):
- One call returns structured, machine-readable results *and* a human report — an agent
  can parse `run()` and self-provision, a dev can read `report()`.
- Acquisition commands are **platform-aware** (macOS/Homebrew vs Debian/apt) and reflect
  the hard-won, current reality (e.g. `idb_companion` lives in the `facebook/fb` tap, and
  `fb-idb` requires Python <= 3.12), not a stale README.
- Every check is non-fatal: probing never raises, so the doctor works on a bare install.

Exposed as the `doctor` MCP tool (`server.py`) and the `meshtastic-mcp doctor` CLI.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import capabilities, config

# ---------------------------------------------------------------------------
# Platform-aware acquisition hints
# ---------------------------------------------------------------------------
_IS_MAC = sys.platform == "darwin"
_IS_LINUX = sys.platform.startswith("linux")


def _pkg(mac: str, debian: str = "", *, note: str = "") -> str:
    """Pick the platform-appropriate install command, with an optional note."""
    cmd = mac if _IS_MAC else (debian or mac)
    return f"{cmd}{'  # ' + note if note else ''}"


def _android_cli_install() -> str:
    """Google `android` CLI install (the run/layout/screen tool used by `avd`)."""
    base = "https://dl.google.com/android/cli/latest"
    slug = "darwin_arm64" if _IS_MAC else "linux_x86_64"
    return f"curl -fsSL {base}/{slug}/install.sh | bash"


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------
STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_DEGRADED = "degraded"  # present but with a caveat (e.g. wrong Python for fb-idb)


@dataclass
class Check:
    """One probed dependency."""

    name: str
    group: str  # core | firmware | android | apple | observability
    status: str  # STATUS_*
    needed_for: str
    detail: str = ""
    fix: str = ""  # the command to acquire it (empty when ok)
    env_override: str = ""  # env var that can point at a custom path/binary

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


@dataclass
class DoctorReport:
    platform: str
    capabilities: str
    checks: list[Check] = field(default_factory=list)

    @property
    def missing(self) -> list[Check]:
        return [c for c in self.checks if c.status == STATUS_MISSING]

    @property
    def degraded(self) -> list[Check]:
        return [c for c in self.checks if c.status == STATUS_DEGRADED]

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "capabilities": self.capabilities,
            "ok": not self.missing,
            "checks": [asdict(c) for c in self.checks],
            "missing": [c.name for c in self.missing],
            "degraded": [c.name for c in self.degraded],
            "fix_commands": [c.fix for c in self.checks if c.fix and not c.ok],
        }


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------
def _which(name: str) -> str | None:
    return shutil.which(name)


def _bin_check(
    name: str,
    group: str,
    needed_for: str,
    fix: str,
    *,
    env_override: str = "",
) -> Check:
    path = _which(name)
    if path:
        return Check(name, group, STATUS_OK, needed_for, detail=path, env_override=env_override)
    return Check(name, group, STATUS_MISSING, needed_for, fix=fix, env_override=env_override)


def _repo_root_check(
    name: str,
    group: str,
    needed_for: str,
    root_or_none_fn,  # callable: () -> Path | None
    env_var: str,
    clone_url: str,
    clone_dir_hint: str,
) -> Check:
    """Generic repo-root check: present + valid → ok; missing → missing with clone hint."""
    root = root_or_none_fn()
    if root:
        return Check(name, group, STATUS_OK, needed_for, detail=str(root), env_override=env_var)
    return Check(
        name,
        group,
        STATUS_MISSING,
        needed_for,
        detail=f"{env_var} not set",
        fix=(
            f"git clone --recurse-submodules {clone_url} {clone_dir_hint} && "
            f"export {env_var}=$PWD/{clone_dir_hint}"
        ),
        env_override=env_var,
    )


def _firmware_check() -> Check:
    root = config.firmware_root_or_none()
    if root:
        return Check(
            "firmware-tree",
            "firmware",
            STATUS_OK,
            "build / flash / boards / userprefs",
            detail=str(root),
            env_override="MESHTASTIC_FIRMWARE_ROOT",
        )
    return Check(
        "firmware-tree",
        "firmware",
        STATUS_MISSING,
        "build / flash / boards / userprefs",
        detail="no platformio.ini above cwd",
        fix="git clone https://github.com/meshtastic/firmware && "
        "export MESHTASTIC_FIRMWARE_ROOT=$PWD/firmware",
        env_override="MESHTASTIC_FIRMWARE_ROOT",
    )


def _sdr_check() -> Check:
    """RF-compliance oracle (`rf_scan`/`rf_confirm_tx`): needs `pyrtlsdr` importable
    (the `sdr` extra) *and* librtlsdr on the system *and* an RTL-SDR attached.
    Reports the most specific missing piece rather than a generic "not available".
    """
    from . import sdr as sdr_mod

    try:
        from rtlsdr import RtlSdr  # noqa: F401
    except ImportError:
        return Check(
            "pyrtlsdr",
            "sdr",
            STATUS_MISSING,
            "RF-compliance oracle (rf_scan / rf_confirm_tx)",
            detail="pyrtlsdr not importable",
            fix="pip install 'meshtastic-mcp[sdr]'  # also needs librtlsdr: "
            + _pkg("brew install librtlsdr", "apt install librtlsdr-dev rtl-sdr"),
        )
    try:
        devices = sdr_mod.list_devices()
    except sdr_mod.SdrError as exc:
        return Check(
            "rtl-sdr-device",
            "sdr",
            STATUS_MISSING,
            "RF-compliance oracle (rf_scan / rf_confirm_tx)",
            detail=str(exc),
            fix=_pkg(
                "brew install librtlsdr",
                "apt install librtlsdr-dev rtl-sdr  # then plug in an RTL-SDR",
            ),
        )
    if not devices:
        return Check(
            "rtl-sdr-device",
            "sdr",
            STATUS_MISSING,
            "RF-compliance oracle (rf_scan / rf_confirm_tx)",
            detail="pyrtlsdr + librtlsdr present, but no RTL-SDR attached",
            fix="plug in an RTL-SDR (e.g. a NooElec NESDR) and retry",
        )
    return Check(
        "rtl-sdr-device",
        "sdr",
        STATUS_OK,
        "RF-compliance oracle (rf_scan / rf_confirm_tx)",
        detail=f"{len(devices)} device(s): {', '.join(devices)}",
    )


def _pio_check() -> Check:
    try:
        path = config.pio_bin()
        return Check(
            "platformio",
            "firmware",
            STATUS_OK,
            "build / flash native + embedded targets",
            detail=str(path),
            env_override="MESHTASTIC_PIO_BIN",
        )
    except config.ConfigError as exc:
        return Check(
            "platformio",
            "firmware",
            STATUS_MISSING,
            "build / flash native + embedded targets",
            detail=str(exc),
            fix="pipx install platformio  # or `pip install platformio`",
            env_override="MESHTASTIC_PIO_BIN",
        )


def _idb_interpreter_version() -> tuple[int, int] | None:
    """Best-effort: the Python (major, minor) behind the `idb` console script.

    `idb` is typically a pipx console script whose shebang points at its own venv
    interpreter — *not* the meshtastic-mcp server interpreter — so that's the version that
    actually matters for the fb-idb asyncio breakage. Returns None if undeterminable.
    """
    exe = _which("idb")
    if not exe:
        return None
    try:
        with open(exe, encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    interp = first[2:].strip().split()[0]
    try:
        out = subprocess.run(
            [interp, "-c", "import sys;print(sys.version_info[0],sys.version_info[1])"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        major, minor = (int(x) for x in out.stdout.split())
        return major, minor
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _fbidb_check() -> Check:
    """fb-idb (the `idb` client) breaks on Python 3.14 (asyncio.get_event_loop removed)."""
    if _which("idb") is None:
        return Check(
            "fb-idb",
            "apple",
            STATUS_MISSING,
            "iOS Simulator UI dump + tap/text (the adb analog)",
            fix=_pkg(
                "brew install python@3.12 && "
                "pipx install --python $(brew --prefix)/bin/python3.12 fb-idb",
                note="fb-idb needs Python <= 3.12",
            ),
        )
    # Inspect the interpreter the `idb` script actually runs under (not the server's).
    ver = _idb_interpreter_version()
    if ver is not None and ver >= (3, 14):
        return Check(
            "fb-idb",
            "apple",
            STATUS_DEGRADED,
            "iOS Simulator UI dump + tap/text",
            detail=f"idb runs under Python {ver[0]}.{ver[1]}; fb-idb uses the removed "
            "asyncio.get_event_loop and will crash. Reinstall under <=3.12.",
            fix="pipx reinstall fb-idb --python $(brew --prefix)/bin/python3.12",
        )
    detail = _which("idb") or ""
    if ver is not None:
        detail += f"  (python {ver[0]}.{ver[1]})"
    return Check("fb-idb", "apple", STATUS_OK, "iOS Simulator UI dump + tap/text", detail=detail)


def _ocr_check() -> Check:
    """OCR is optional; report the active backend or how to get one."""
    have_tesseract = _which("tesseract") is not None
    try:
        import pytesseract  # noqa: F401

        have_pytesseract = True
    except Exception:
        have_pytesseract = False
    try:
        import easyocr  # noqa: F401

        have_easyocr = True
    except Exception:
        have_easyocr = False

    if have_easyocr or (have_pytesseract and have_tesseract):
        backend = "easyocr" if have_easyocr else "pytesseract+tesseract"
        return Check(
            "ocr",
            "observability",
            STATUS_OK,
            "OLED screen-capture text extraction",
            detail=f"backend={backend}",
            env_override="MESHTASTIC_UI_OCR_BACKEND",
        )
    return Check(
        "ocr",
        "observability",
        STATUS_MISSING,
        "OLED screen-capture text extraction (optional)",
        detail="no OCR backend importable",
        fix=_pkg(
            "pip install 'meshtastic-mcp[ocr]'  # or brew install tesseract",
            "pip install 'meshtastic-mcp[ocr]'  # or apt install tesseract-ocr",
        ),
        env_override="MESHTASTIC_UI_OCR_BACKEND",
    )


def _java_check() -> Check:
    """JDK 17+ required by the Android Gradle build."""
    java = _which("java")
    if not java:
        return Check(
            "java",
            "android",
            STATUS_MISSING,
            "build APK from source (Gradle requires JDK 17+)",
            fix=_pkg(
                "brew install --cask temurin@21",
                "apt install openjdk-21-jdk  # or use SDKMAN: sdk install java 21-tem",
            ),
            env_override="JAVA_HOME",
        )
    # Best-effort version check — non-fatal if we can't parse it.
    try:
        out = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=5)
        ver_line = (out.stderr or out.stdout).splitlines()[0]
        # e.g. 'openjdk version "21.0.3" ...' or 'java version "17.0.10" ...'
        m = re.search(r'"(\d+)(?:\.(\d+))?', ver_line)
        if m:
            major = int(m.group(1))
            # Java 9+: version string is just the major (11, 17, 21…)
            # Java 8: "1.8.0_xxx", so major==1 and minor==8
            if major == 1 and m.group(2):
                major = int(m.group(2))
            if major < 17:
                return Check(
                    "java",
                    "android",
                    STATUS_DEGRADED,
                    "build APK from source",
                    detail=f"JDK {major} found at {java}; Gradle needs ≥17",
                    fix=_pkg(
                        "brew install --cask temurin@21",
                        "apt install openjdk-21-jdk",
                    ),
                    env_override="JAVA_HOME",
                )
        return Check(
            "java", "android", STATUS_OK, "build APK from source", detail=f"{java}  ({ver_line})"
        )
    except Exception:
        return Check("java", "android", STATUS_OK, "build APK from source", detail=java)


def _android_sdk_check() -> Check:
    """Android SDK (ANDROID_HOME / ANDROID_SDK_ROOT) needed by Gradle."""
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not sdk:
        # Try well-known defaults before flagging missing.
        candidates = [
            Path.home() / "Library" / "Android" / "sdk",  # macOS / Android Studio
            Path.home() / "Android" / "Sdk",  # Linux / Android Studio
        ]
        for c in candidates:
            if c.is_dir():
                sdk = str(c)
                break
    if sdk and Path(sdk).is_dir():
        return Check(
            "android-sdk",
            "android",
            STATUS_OK,
            "build APK from source (Gradle ANDROID_HOME)",
            detail=sdk,
            env_override="ANDROID_HOME",
        )
    return Check(
        "android-sdk",
        "android",
        STATUS_MISSING,
        "build APK from source (Gradle ANDROID_HOME)",
        fix=_pkg(
            "brew install --cask android-studio  # sets up SDK automatically",
            "apt install android-sdk  # or install Android Studio from developer.android.com",
        ),
        env_override="ANDROID_HOME",
    )


def _uhubctl_check() -> Check:
    """Check uhubctl presence *and* whether it works without root (udev rules)."""
    path = _which("uhubctl") or os.environ.get("MESHTASTIC_UHUBCTL_BIN")
    needed = "USB power-cycle fault injection / flash recovery (optional)"

    if not path:
        return Check(
            "uhubctl",
            "observability",
            STATUS_MISSING,
            needed,
            fix=_pkg("brew install uhubctl", "apt install uhubctl"),
            env_override="MESHTASTIC_UHUBCTL_BIN",
        )

    # Binary found — now check whether it can actually enumerate hubs without root.
    # `uhubctl` exits non-zero and prints a permission error when udev rules are absent.
    if _IS_LINUX:
        try:
            res = subprocess.run(
                [path],
                capture_output=True,
                text=True,
                timeout=5,
            )
            permission_error = "permission" in (res.stderr + res.stdout).lower()
            no_devices = "no compatible" in (res.stderr + res.stdout).lower()

            if permission_error:
                udev_rule = (
                    "sudo curl -fsSL https://raw.githubusercontent.com/mvp/uhubctl/master"
                    "/udev/rules.d/52-usb.rules -o /etc/udev/rules.d/52-usb.rules "
                    "&& sudo udevadm trigger --attr-match=subsystem=usb"
                )
                if (
                    "dialout"
                    not in subprocess.run(["groups"], capture_output=True, text=True).stdout
                ):
                    udev_rule += "\nsudo usermod -a -G dialout $USER  # then log out + back in"
                return Check(
                    "uhubctl",
                    "observability",
                    STATUS_DEGRADED,
                    needed,
                    detail=f"{path} found but USB permission denied (udev rules missing)",
                    fix=udev_rule,
                    env_override="MESHTASTIC_UHUBCTL_BIN",
                )

            if no_devices:
                # Binary works (no permission error) but no supported hubs plugged in yet.
                return Check(
                    "uhubctl",
                    "observability",
                    STATUS_OK,
                    needed,
                    detail=f"{path} (no supported hub detected — plug one in to use)",
                    env_override="MESHTASTIC_UHUBCTL_BIN",
                )
        except Exception:
            pass

    return Check(
        "uhubctl",
        "observability",
        STATUS_OK,
        needed,
        detail=path,
        env_override="MESHTASTIC_UHUBCTL_BIN",
    )


def run() -> DoctorReport:
    """Probe everything and return a structured report (never raises)."""
    caps = capabilities.detect()
    checks: list[Check] = [
        # firmware capability
        _firmware_check(),
        _pio_check(),
        # android capability
        _repo_root_check(
            "android-source",
            "android",
            "build APK from source (scripts/build_android_apk.sh --source-dir)",
            config.android_root_or_none,
            "MESHTASTIC_ANDROID_ROOT",
            "https://github.com/meshtastic/Meshtastic-Android",
            "Meshtastic-Android",
        ),
        _java_check(),
        _android_sdk_check(),
        _bin_check(
            "android",
            "android",
            "Android AVD lifecycle + UI drive (the Google `android` CLI: run/layout/screen)",
            _android_cli_install(),
        ),
        _bin_check(
            "adb",
            "android",
            "Android device/emulator control (input, layout, install)",
            _pkg("brew install --cask android-platform-tools", "apt install android-tools-adb"),
        ),
        # apple capability
        _repo_root_check(
            "apple-source",
            "apple",
            "build iOS Simulator .app from source (scripts/build_apple.sh --source-dir)",
            config.apple_root_or_none,
            "MESHTASTIC_APPLE_ROOT",
            "https://github.com/meshtastic/Meshtastic-Apple",
            "Meshtastic-Apple",
        ),
        _bin_check(
            "xcrun",
            "apple",
            "iOS Simulator / macOS app lifecycle (simctl, build)",
            "xcode-select --install  # or install Xcode from the App Store",
        ),
        _bin_check(
            "idb_companion",
            "apple",
            "iOS Simulator backend for idb",
            "brew tap facebook/fb && brew trust facebook/fb "
            "&& brew install facebook/fb/idb-companion  # NOT the `companion` cask",
        ),
        _fbidb_check(),
        # observability / hardware-bench extras (optional)
        _bin_check(
            "ffmpeg",
            "observability",
            "OLED camera capture for firmware UI tests (optional)",
            _pkg("brew install ffmpeg", "apt install ffmpeg"),
            env_override="MESHTASTIC_UI_CAMERA_BACKEND",
        ),
        _uhubctl_check(),
        _ocr_check(),
        # org-knowledge skill
        _bin_check(
            "gh",
            "org-knowledge",
            "meshtastic-org-knowledge skill — repo/issue/PR/release queries via gh CLI",
            _pkg("brew install gh", "apt install gh  # or: https://cli.github.com"),
        ),
        # sdr capability (RF compliance oracle)
        _sdr_check(),
    ]
    return DoctorReport(
        platform=f"{platform.system()} {platform.machine()} / Python {platform.python_version()}",
        capabilities=caps.summary(),
        checks=checks,
    )


def report(rep: DoctorReport | None = None) -> str:
    """Human-readable doctor output."""
    rep = rep or run()
    lines = [
        f"meshtastic-mcp doctor  —  {rep.platform}",
        f"active capabilities: {rep.capabilities}",
        "",
    ]
    icon = {STATUS_OK: "✓", STATUS_MISSING: "✗", STATUS_DEGRADED: "!"}
    by_group: dict[str, list[Check]] = {}
    for c in rep.checks:
        by_group.setdefault(c.group, []).append(c)
    for group, checks in by_group.items():
        lines.append(f"[{group}]")
        for c in checks:
            head = f"  {icon.get(c.status, '?')} {c.name:<14} {c.status}"
            if c.detail:
                head += f"  ({c.detail})"
            lines.append(head)
            if not c.ok and c.fix:
                lines.append(f"      → {c.fix}")
            if not c.ok and c.env_override:
                lines.append(f"      ↳ or set {c.env_override}")
        lines.append("")
    if rep.missing:
        lines.append(f"missing: {', '.join(c.name for c in rep.missing)}")
    if rep.degraded:
        lines.append(f"degraded: {', '.join(c.name for c in rep.degraded)}")
    if not rep.missing and not rep.degraded:
        lines.append("all probed dependencies satisfied.")
    return "\n".join(lines)
