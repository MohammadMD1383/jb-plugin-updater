#!/usr/bin/env python3
"""
update-plugins  —  CLI updater for JetBrains IDE plugins.

Discovers an IDE installed via JetBrains Toolbox, reads installed user plugins,
queries the JetBrains Marketplace for compatible updates (respecting the IDE
build number), and installs them — all with a live TUI.

Primary platform : Linux + JetBrains Toolbox
Secondary        : macOS / Windows Toolbox (best-effort)
Non-Toolbox      : detected via common install paths / --plugins-dir override

Usage
-----
    update-plugins <ide>              # check and install updates
    update-plugins <ide> --dry-run   # check only, do not install
    update-plugins <ide> --list-only # list installed plugins and exit

IDE aliases
-----------
    clion / cl                         CLion
    ij / iu / idea / intellij / idea-u IntelliJ IDEA Ultimate
    ic / idea-ce                       IntelliJ IDEA Community Edition
    rider / rd                         Rider
    pycharm / py / pc                  PyCharm Professional
    pycharm-ce / pce                   PyCharm Community Edition
    webstorm / ws                      WebStorm
    goland / go / gl                   GoLand
    phpstorm / ps                      PhpStorm
    rubymine / rm                      RubyMine
    datagrip / dg                      DataGrip
    rustrover / rr                     RustRover
    aqua                               Aqua
    fleet                              Fleet
    dataspell / ds                     DataSpell
    gateway / gw                       Gateway
    mps                                MPS

Requirements
------------
    pip install requests rich
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ─── dependency guard ─────────────────────────────────────────────────────────

def _require(*packages: str) -> None:
    missing = [p for p in packages if importlib.util.find_spec(p) is None]
    if missing:
        print(f"Missing packages: {', '.join(missing)}")
        print(f"Install with:  pip install {' '.join(missing)}")
        sys.exit(1)

_require("requests", "rich")

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from rich.console  import Console
from rich.markup   import escape
from rich.panel    import Panel
from rich.progress import (
    BarColumn, DownloadColumn, MofNCompleteColumn, Progress,
    SpinnerColumn, TextColumn, TimeRemainingColumn, TransferSpeedColumn,
)
from rich.rule     import Rule
from rich.text     import Text

console = Console()

# ─── IDE alias table ──────────────────────────────────────────────────────────

_ALIASES: dict[str, str] = {}          # lower-case alias → Toolbox app-dir name
_TB_ALIASES: dict[str, list[str]] = {} # Toolbox name → list of aliases (for matching)
for _tb_name, _alias_list in {
    "CLion":       ["clion",       "cl"],
    "IDEA-U":      ["ij", "iu", "idea", "intellij", "idea-u"],
    "IDEA-C":      ["ic", "idea-ce", "ideace", "idea-community"],
    "Rider":       ["rider",       "rd"],
    "PyCharm-P":   ["pycharm",     "py", "pc"],
    "PyCharm-C":   ["pycharm-ce",  "pce"],
    "WebStorm":    ["webstorm",    "ws"],
    "GoLand":      ["goland",      "go", "gl"],
    "PhpStorm":    ["phpstorm",    "ps"],
    "RubyMine":    ["rubymine",    "rm"],
    "DataGrip":    ["datagrip",    "dg"],
    "RustRover":   ["rustrover",   "rr"],
    "Aqua":        ["aqua"],
    "Fleet":       ["fleet"],
    "DataSpell":   ["dataspell",   "ds"],
    "Gateway":     ["gateway",     "gw"],
    "MPS":         ["mps"],
}.items():
    _TB_ALIASES[_tb_name] = _alias_list
    for _a in _alias_list:
        _ALIASES[_a.lower()] = _tb_name

MARKETPLACE = "https://plugins.jetbrains.com"

# ─── platform detection ──────────────────────────────────────────────────────

import platform as _platform

_PLATFORM_TAG: str = ""
match (_platform.system(), _platform.machine()):
    case ("Linux", "x86_64"):   _PLATFORM_TAG = "linux-x86_64"
    case ("Linux", "aarch64"):  _PLATFORM_TAG = "linux-arm64"
    case ("Darwin", "x86_64"):  _PLATFORM_TAG = "mac-x86_64"
    case ("Darwin", "arm64"):   _PLATFORM_TAG = "mac-arm64"
    case ("Windows", "AMD64"):  _PLATFORM_TAG = "windows-x86_64"
    case ("Windows", "ARM64"):  _PLATFORM_TAG = "windows-arm64"

def _extract_platform(version: str) -> str:
    """Extract platform tag from a version string like '262.6653.22-linux-x86_64'."""
    for tag in ("linux-x86_64", "linux-arm64", "mac-x86_64", "mac-arm64",
                "windows-x86_64", "windows-arm64"):
        if version.endswith(tag):
            return tag
    return ""

# ─── data classes ─────────────────────────────────────────────────────────────

@dataclass
class IDEInfo:
    name:         str
    version:      str
    build:        str       # e.g. "243.22562.145"
    product_code: str       # e.g. "CL"
    install_dir:  Path
    plugins_dir:  Path      # user plugins dir

@dataclass
class Plugin:
    xml_id:  str            # plugin's unique string ID from plugin.xml
    name:    str
    version: str
    dir:     Path           # directory inside plugins_dir

@dataclass
class Update:
    plugin:      Plugin
    new_ver:     str
    url:         str            # direct download URL (.zip)
    since_build: str = ""       # compatible since this build
    until_build: str = ""       # compatible until this build

# ─── HTTP session with retries ────────────────────────────────────────────────

_session = requests.Session()
_session.headers["User-Agent"] = "JetBrains-Plugin-Updater/1.0"
_session.headers["Accept"] = "application/json"
_retry = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    raise_on_status=False,
)
_session.mount("https://", HTTPAdapter(max_retries=_retry))
_session.mount("http://",  HTTPAdapter(max_retries=_retry))

# ─── Toolbox path resolution ──────────────────────────────────────────────────

def _toolbox_apps_dirs() -> list[Path]:
    """Return all existing JetBrains Toolbox 'apps' directories."""
    home = Path.home()
    xdg  = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))

    candidates: list[Path] = [
        xdg  / "JetBrains" / "Toolbox" / "apps",                                   # Linux
        home / "Library" / "Application Support" / "JetBrains" / "Toolbox" / "apps",# macOS
    ]
    if "APPDATA" in os.environ:
        candidates.append(
            Path(os.environ["APPDATA"]) / "JetBrains" / "Toolbox" / "apps"         # Windows
        )

    # Honour custom install path from Toolbox .settings.json
    for cfg_dir in [
        xdg  / "JetBrains" / "Toolbox",
        home / "Library" / "Application Support" / "JetBrains" / "Toolbox",
    ]:
        sf = cfg_dir / ".settings.json"
        if sf.exists():
            try:
                cfg = json.loads(sf.read_text(encoding="utf-8", errors="replace"))
                loc = cfg.get("install_location") or cfg.get("toolsDirectory")
                if loc:
                    p = Path(loc)
                    candidates.insert(0, p / "apps")
                    candidates.insert(0, p)
            except Exception:
                pass

    # Deduplicate while preserving order
    seen: set[Path] = set()
    result: list[Path] = []
    for p in candidates:
        if p not in seen and p.is_dir():
            seen.add(p)
            result.append(p)
    return result

# ─── IDE installation discovery ───────────────────────────────────────────────

def _is_ide_root(path: Path) -> bool:
    """Does this directory look like an IDE installation root?"""
    return (
        path.is_dir()
        and ((path / "product-info.json").exists() or (path / "build.txt").exists())
    )


def _latest_build_dir(app_dir: Path) -> Optional[Path]:
    """
    Find the newest build directory under a Toolbox app directory.

    Toolbox layout (both 1.x and 2.x):
        apps/<AppName>/ch-<N>/<build>/   ← channel dirs
        apps/<AppName>/<build>/          ← direct (some layouts)
        apps/<AppName>/                  ← direct install (IDE root is the app dir itself)
    """
    found: list[Path] = []

    # The app dir itself might be the IDE root (direct install)
    if _is_ide_root(app_dir):
        found.append(app_dir)

    for child in app_dir.iterdir():
        if not child.is_dir():
            continue
        if child.name.startswith("ch-"):
            for bd in child.iterdir():
                if _is_ide_root(bd):
                    found.append(bd)
        elif _is_ide_root(child):
            found.append(child)

    if not found:
        return None

    def _build_key(p: Path) -> tuple:
        try:
            return tuple(int(x) for x in p.name.split("."))
        except ValueError:
            return (0,)

    return max(found, key=_build_key)


def _looks_like_plugins_dir(path: Path) -> bool:
    """Check if a directory looks like it contains JetBrains plugins."""
    if not path.is_dir():
        return False
    for child in path.iterdir():
        if child.is_dir() and ((child / "lib").is_dir() or (child / "META-INF").is_dir()):
            return True
    return False


def _user_plugins_dir(data_dir_name: str) -> Path:
    """Resolve the user plugins directory for a given IDE data-directory name."""
    home = Path.home()
    xdg  = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    jb   = xdg / "JetBrains"

    if jb.is_dir():
        # 1. exact match (most common)
        exact = jb / data_dir_name / "plugins"
        if exact.exists():
            return exact

        # 2. prefix match — handles minor-version discrepancies
        #    e.g. dataDirectoryName="CLion2024.3" but dir is "CLion2024.3"
        prefix = data_dir_name
        matches: list[Path] = [
            d / "plugins"
            for d in sorted(jb.iterdir(), reverse=True)
            if d.is_dir()
            and d.name.startswith(prefix[: max(4, len(prefix) - 2)])
            and (d / "plugins").exists()
        ]
        if matches:
            # prefer the closest name
            matches.sort(key=lambda m: (m.parent.name != data_dir_name, m.parent.name))
            return matches[0]

        # 3. Some Toolbox layouts store plugins directly in the data dir
        #    (no "plugins" subdirectory)
        direct = jb / data_dir_name
        if _looks_like_plugins_dir(direct):
            return direct

    # macOS
    mac = home / "Library" / "Application Support" / "JetBrains" / data_dir_name / "plugins"
    if mac.exists():
        return mac

    # return expected path even if it does not exist yet
    return jb / data_dir_name / "plugins"


def _parse_product_info(build_dir: Path) -> Optional[IDEInfo]:
    """Read product-info.json and produce an IDEInfo."""
    pf = build_dir / "product-info.json"
    if not pf.exists():
        return None
    try:
        pi = json.loads(pf.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None

    name     = pi.get("name",            "").strip()
    version  = pi.get("version",         "").strip()
    build    = pi.get("buildNumber",     "").strip()
    code     = pi.get("productCode",     "").strip()
    data_dir = pi.get("dataDirectoryName","").strip()

    # Fall back to build.txt if buildNumber is absent
    if not build:
        bt = build_dir / "build.txt"
        if bt.exists():
            raw = bt.read_text().strip()
            build = raw.split("-", 1)[-1] if "-" in raw else raw

    if not (name and build):
        return None

    return IDEInfo(
        name=name,
        version=version,
        build=build,
        product_code=code,
        install_dir=build_dir,
        plugins_dir=_user_plugins_dir(data_dir),
    )


def _probe_app_dir(app_dir: Path) -> Optional[IDEInfo]:
    bd = _latest_build_dir(app_dir)
    return _parse_product_info(bd) if bd else None


def find_ide(user_arg: str, plugins_dir_override: Optional[Path] = None) -> Optional[IDEInfo]:
    """
    Locate an IDE installation matching *user_arg*.

    Search order:
      1. Exact Toolbox app-directory name match
      2. Substring / prefix match on app-directory name
      3. Match by product name or code inside product-info.json
    """
    arg_lower    = user_arg.lower()
    toolbox_name = _ALIASES.get(arg_lower)
    apps_dirs    = _toolbox_apps_dirs()

    for apps_dir in apps_dirs:
        for app_dir in sorted(apps_dir.iterdir()):
            if not app_dir.is_dir():
                continue
            name_lower = app_dir.name.lower()
            # Check aliases for the resolved toolbox name
            _tb_aliases_lower = [
                a.lower() for a in _TB_ALIASES.get(toolbox_name, [])
            ] if toolbox_name else []
            if (
                (toolbox_name and name_lower == toolbox_name.lower())
                or (toolbox_name and any(a in name_lower for a in _tb_aliases_lower if len(a) > 2))
                or arg_lower in name_lower
                or name_lower.startswith(arg_lower)
            ):
                info = _probe_app_dir(app_dir)
                if info:
                    if plugins_dir_override:
                        info.plugins_dir = plugins_dir_override
                    return info

    # Slower fallback: read every product-info.json and match by name/code
    for apps_dir in apps_dirs:
        for app_dir in sorted(apps_dir.iterdir()):
            if not app_dir.is_dir():
                continue
            info = _probe_app_dir(app_dir)
            if info and (
                arg_lower in info.name.lower()
                or arg_lower == info.product_code.lower()
            ):
                if plugins_dir_override:
                    info.plugins_dir = plugins_dir_override
                return info

    return None

# ─── plugin scanning ──────────────────────────────────────────────────────────

def _parse_plugin_xml(content: bytes, plugin_dir: Path) -> Optional[Plugin]:
    """Parse a plugin.xml byte string and return a Plugin."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        # Strip non-XML characters and retry
        content = re.sub(rb"[^\x09\x0A\x0D\x20-\xFE]", b"", content)
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            return None

    xml_id  = (root.findtext("id")      or "").strip()
    name    = (root.findtext("name")    or "").strip()
    version = (root.findtext("version") or "").strip()

    xml_id = xml_id or name
    name   = name   or xml_id
    if not xml_id:
        return None
    return Plugin(xml_id=xml_id, name=name, version=version or "?", dir=plugin_dir)


def _plugin_meta(plugin_dir: Path) -> Optional[Plugin]:
    """Extract plugin metadata from a plugin directory."""
    # 1. Direct META-INF/plugin.xml (extracted by newer IDEs / Toolbox)
    direct = plugin_dir / "META-INF" / "plugin.xml"
    if direct.exists():
        p = _parse_plugin_xml(direct.read_bytes(), plugin_dir)
        if p:
            return p

    # 2. Search inside JARs — prefer lib/*.jar, then root *.jar
    lib  = plugin_dir / "lib"
    jars: list[Path] = []
    if lib.is_dir():
        jars.extend(sorted(lib.glob("*.jar")))
    jars.extend(sorted(plugin_dir.glob("*.jar")))

    # Put the most likely primary JAR first (name resembles plugin dir)
    slug = re.sub(r"[^a-z0-9]", "", plugin_dir.name.lower())
    jars.sort(key=lambda j: 0 if slug in re.sub(r"[^a-z0-9]", "", j.stem.lower()) else 1)

    for jar in jars:
        try:
            with zipfile.ZipFile(jar, "r") as zf:
                names = zf.namelist()
                for candidate in ("META-INF/plugin.xml", "plugin.xml"):
                    if candidate in names:
                        p = _parse_plugin_xml(zf.read(candidate), plugin_dir)
                        if p:
                            return p
                # Slow path: any path ending in /plugin.xml
                for n in names:
                    if n.endswith("/plugin.xml"):
                        p = _parse_plugin_xml(zf.read(n), plugin_dir)
                        if p:
                            return p
        except Exception:
            continue

    return None


def scan_plugins(plugins_dir: Path) -> list[Plugin]:
    """Return all user plugins found inside *plugins_dir*."""
    if not plugins_dir.is_dir():
        return []
    result: list[Plugin] = []
    for d in sorted(plugins_dir.iterdir()):
        if d.is_dir():
            meta = _plugin_meta(d)
            if meta:
                result.append(meta)
    return result

# ─── Marketplace update check ─────────────────────────────────────────────────

def _build_key(build_str: str) -> tuple:
    """Parse a build string like '262.6653' into a comparable tuple."""
    s = build_str.strip()
    has_wildcard = s.endswith(".*")
    parts = re.split(r"[.\-]", s.rstrip(".*"))
    out: list = []
    for p in parts:
        try:
            out.append((0, int(p)))
        except ValueError:
            out.append((1, p.lower()))
    if has_wildcard:
        # wildcard means "any sub-version", so make this sort higher
        out.append((2,))
    return tuple(out)


def _is_build_compatible(ide_build: str, since: str, until: str) -> bool:
    """Check if ide_build falls within the since→until range."""
    if not since:
        return True  # no constraint means compatible
    try:
        ide_key = _build_key(ide_build)
        since_key = _build_key(since)
        if ide_key < since_key:
            return False
        if until:
            until_key = _build_key(until)
            if ide_key > until_key:
                return False
    except Exception:
        pass
    return True


def _search_plugin_numeric_id(xml_id: str) -> Optional[int]:
    """Look up a plugin's numeric Marketplace ID from its xmlId."""
    try:
        resp = _session.get(
            f"{MARKETPLACE}/api/searchPlugins",
            params={"search": xml_id, "max": 10},
            timeout=20,
        )
        if resp.status_code == 200:
            for p in resp.json().get("plugins", []):
                if p.get("xmlId") == xml_id:
                    return p["id"]
    except (requests.RequestException, ValueError, KeyError):
        pass
    return None


def _fetch_one_plugin(xml_id: str, build: str) -> Optional[dict]:
    """
    Query the Marketplace for a single plugin's compatible version.

    Steps:
      1. Search by xmlId → get numeric plugin ID
      2. GET /api/plugins/{id}/updates → get all versions
      3. Filter by actual build compatibility (since→until range)
      4. Return latest compatible version with download URL

    Returns dict with keys: version, url, since-build, until-build, numeric_id
    """
    num_id = _search_plugin_numeric_id(xml_id)
    if not num_id:
        return None

    try:
        resp = _session.get(
            f"{MARKETPLACE}/api/plugins/{num_id}/updates",
            timeout=20,
        )
        if resp.status_code == 200:
            updates = resp.json()
            # Collect all compatible updates
            compatible: list[dict] = []
            for u in updates:
                since = u.get("since", "")
                until = u.get("until", "")
                if _is_build_compatible(build, since, until):
                    compatible.append(u)

            if compatible:
                # Prefer matching platform, then platform-independent, then any
                best = None
                for u in compatible:
                    plat = _extract_platform(u.get("version", ""))
                    if plat == _PLATFORM_TAG:
                        best = u
                        break
                if not best:
                    for u in compatible:
                        plat = _extract_platform(u.get("version", ""))
                        if not plat:
                            best = u
                            break
                if not best:
                    best = compatible[0]

                since = best.get("since", "")
                until = best.get("until", "")
                file_path = best.get("file", "")
                url = f"{MARKETPLACE}/files/{file_path}" if file_path else ""
                return {
                    "version": best.get("version", ""),
                    "url": url,
                    "since-build": since,
                    "until-build": until,
                    "numeric_id": str(num_id),
                }
            # Found on Marketplace but no compatible version for this build
            return {"version": "", "url": "", "numeric_id": str(num_id), "_no_compat": True}
    except (requests.RequestException, ValueError):
        pass
    return None


def _fetch_remote_versions(plugins: list[Plugin], build: str) -> dict[str, dict[str, str]]:
    """
    Query the JetBrains Marketplace for each plugin individually (concurrently).

    The old batch /plugins/list endpoint no longer accepts multiple pluginId
    params, so we query each plugin one at a time with a thread pool.

    Returns: {xml_id: {"version": str, "url": str}}
    """
    if not plugins:
        return {}

    xml_ids = [p.xml_id for p in plugins]
    result: dict[str, dict[str, str]] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_fetch_one_plugin, pid, build): pid
            for pid in xml_ids
        }
        for future in concurrent.futures.as_completed(futures):
            pid = futures[future]
            info = future.result()
            if info:
                result[pid] = info

    return result



def _ver_tuple(v: str) -> tuple:
    """
    Convert a version string to a comparable tuple.

    Handles: 1.2.3, 2024.1.3, 1.0-EAP, 2.0.0-beta.1, etc.
    Platform tags (linux-x86_64, etc.) are stripped before comparison.
    """
    # Strip platform tag for comparison
    plat = _extract_platform(v)
    if plat:
        v = v[: -len(plat)].rstrip("-_")
    parts = re.split(r"[.\-_+]", v.strip())
    out: list = []
    for p in parts:
        try:
            out.append((0, int(p)))
        except ValueError:
            out.append((1, p.lower()))
    return tuple(out)


def compute_updates(plugins: list[Plugin], remote: dict[str, dict[str, str]]) -> list[Update]:
    """Return plugins that have a newer version available on the Marketplace."""
    updates: list[Update] = []
    for pl in plugins:
        info = remote.get(pl.xml_id)
        if not info or info.get("_no_compat"):
            continue
        nv = info["version"]
        if not nv:
            continue
        try:
            is_newer = _ver_tuple(nv) > _ver_tuple(pl.version)
        except Exception:
            is_newer = nv != pl.version
        if is_newer:
            updates.append(Update(
                plugin=pl, new_ver=nv, url=info["url"],
                since_build=info.get("since-build", ""),
                until_build=info.get("until-build", ""),
            ))
    return updates

# ─── installation ─────────────────────────────────────────────────────────────

def install_plugin(
    upd: Update,
    plugins_dir: Path,
    progress: Progress,
    task,
) -> tuple[bool, str]:
    """Download *upd* and install it under *plugins_dir*."""
    try:
        with _session.get(upd.url, stream=True, timeout=120, allow_redirects=True) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            if total:
                progress.update(task, total=total)

            with tempfile.TemporaryDirectory() as tmp:
                dl   = Path(tmp) / "plugin.zip"
                done = 0
                with open(dl, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            fh.write(chunk)
                            done += len(chunk)
                            progress.update(task, completed=done)

                if not zipfile.is_zipfile(dl):
                    return False, "downloaded file is not a valid ZIP"

                with zipfile.ZipFile(dl, "r") as zf:
                    members = zf.namelist()
                    roots   = {m.split("/")[0] for m in members if m.strip("/")}

                    # Remove old plugin directory first
                    if upd.plugin.dir.exists():
                        shutil.rmtree(upd.plugin.dir)

                    if len(roots) == 1:
                        # Standard layout: single top-level dir inside the ZIP
                        new_root_name = next(iter(roots))
                        new_root      = plugins_dir / new_root_name

                        # Remove any pre-existing dir with the new name too
                        if new_root.exists():
                            shutil.rmtree(new_root)

                        zf.extractall(plugins_dir)

                        # Normalise name to original so the IDE keeps tracking it
                        expected = upd.plugin.dir
                        if new_root.exists() and new_root != expected:
                            if expected.exists():
                                shutil.rmtree(expected)
                            new_root.rename(expected)
                    else:
                        # Flat layout or multi-root: extract into the old dir
                        upd.plugin.dir.mkdir(parents=True, exist_ok=True)
                        zf.extractall(upd.plugin.dir)

        return True, upd.new_ver

    except requests.HTTPError as exc:
        return False, f"HTTP {exc.response.status_code}"
    except requests.Timeout:
        return False, "download timed out"
    except Exception as exc:
        return False, str(exc)

# ─── TUI helpers ──────────────────────────────────────────────────────────────

def _spin(description: str) -> Progress:
    """Return a transient spinner Progress for short blocking steps."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        console=console,
        transient=True,
    )

# ─── main workflow ────────────────────────────────────────────────────────────

def run(ide_arg: str, dry_run: bool, list_only: bool,
        plugins_dir_override: Optional[Path], proxy: Optional[str] = None) -> None:

    # ── Resolve proxy ──────────────────────────────────────────────────────────
    # --proxy takes precedence, then env vars, then requests defaults
    resolved_proxy = proxy
    if not resolved_proxy:
        resolved_proxy = (
            os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("http_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("all_proxy")
            or os.environ.get("ALL_PROXY")
        )
    if resolved_proxy:
        _session.proxies = {"http": resolved_proxy, "https": resolved_proxy}

    console.print()
    console.print(Panel.fit(
        f"[bold cyan]JetBrains Plugin Updater[/bold cyan]  [dim]·[/dim]  "
        f"[yellow]{escape(ide_arg)}[/yellow]",
        border_style="cyan",
    ))
    console.print()

    # ── 1 · Locate IDE ────────────────────────────────────────────────────────
    with _spin(f"Locating [cyan]{escape(ide_arg)}[/cyan] installation…") as p:
        p.add_task("")
        ide = find_ide(ide_arg, plugins_dir_override)

    if ide is None:
        console.print(f"[red]✗[/red]  IDE [bold]{escape(ide_arg)}[/bold] not found.")
        searched = _toolbox_apps_dirs()
        if searched:
            console.print("  Toolbox 'apps' directories searched:")
            for d in searched:
                console.print(f"    [dim]{d}[/dim]")
        else:
            console.print(
                "  JetBrains Toolbox does not appear to be installed.\n"
                "  Use [bold]--plugins-dir[/bold] to point at a plugins directory directly."
            )
        raise SystemExit(1)

    console.print(
        f"[green]✓[/green]  [bold]{escape(ide.name)}[/bold] {ide.version}  "
        f"[dim](build {ide.build})[/dim]"
    )
    console.print(f"   [dim]{ide.install_dir}[/dim]")
    if resolved_proxy:
        console.print(f"   [dim]Proxy: {resolved_proxy}[/dim]")

    # ── 2 · Plugins directory ─────────────────────────────────────────────────
    if not ide.plugins_dir.is_dir():
        console.print(
            f"\n[yellow]⚠[/yellow]  User plugins directory not found:\n"
            f"   [dim]{ide.plugins_dir}[/dim]\n\n"
            "  No user-installed plugins to update.\n"
            "  (Bundled plugins are managed by Toolbox and not handled here.)"
        )
        return

    console.print(f"[green]✓[/green]  Plugins: [dim]{ide.plugins_dir}[/dim]")

    # ── 3 · Scan installed plugins ────────────────────────────────────────────
    with _spin("Scanning installed plugins…") as p:
        p.add_task("")
        plugins = scan_plugins(ide.plugins_dir)

    if not plugins:
        console.print("[yellow]⚠[/yellow]  No plugins found in the plugins directory.")
        return

    console.print(f"[green]✓[/green]  Found [bold]{len(plugins)}[/bold] plugin(s)")

    if list_only:
        console.print()
        console.print(Rule("[bold]Installed Plugins[/bold]", style="dim cyan"))
        console.print()
        for pl in plugins:
            console.print(
                f"   [cyan]{escape(pl.xml_id)}[/cyan]  "
                f"[dim]{pl.version}[/dim]  {escape(pl.name)}"
            )
        return

    # ── 4 · Query Marketplace (per-plugin) ─────────────────────────────────────
    console.print()
    console.print(Rule("[bold]Checking for Updates[/bold]", style="dim cyan"))
    console.print()
    console.print(f"  [dim]Build:[/dim]  {ide.build}")
    console.print(f"  [dim]Query:[/dim]  {MARKETPLACE}/api/plugins/{{id}}/updates?build={ide.build}")
    console.print(f"  [dim]Plugins:[/dim] {len(plugins)} installed")
    console.print()

    with _spin(
        f"Querying Marketplace [dim](build {ide.build}, {len(plugins)} plugins)[/dim]…"
    ) as p:
        p.add_task("")
        remote = _fetch_remote_versions(plugins, ide.build)

    if not remote and plugins:
        console.print(
            "[yellow]⚠[/yellow]  Could not reach the Marketplace.  "
            "Check your internet connection and try again."
        )
        return

    console.print(f"  [dim]Marketplace responded:[/dim] {len(remote)} plugin(s) matched")
    console.print()

    updates_available = compute_updates(plugins, remote)
    update_map        = {u.plugin.xml_id: u for u in updates_available}
    not_found_count   = 0

    # ── 5 · Per-plugin status display ─────────────────────────────────────────
    not_found_count = 0

    for pl in plugins:
        upd = update_map.get(pl.xml_id)
        rinfo = remote.get(pl.xml_id)
        compat = ""
        if rinfo:
            sb = rinfo.get("since-build", "")
            ub = rinfo.get("until-build", "")
            if sb or ub:
                compat = f"  [dim]compat {sb}–{ub}[/dim]"
        if upd:
            console.print(
                f"  [yellow]↑[/yellow]  [bold]{escape(pl.name)}[/bold]  "
                f"[dim]{pl.version}[/dim] → [yellow]{upd.new_ver}[/yellow]{compat}"
            )
        elif pl.xml_id in remote:
            console.print(
                f"  [green]✓[/green]  {escape(pl.name)}  [dim]{pl.version}[/dim]{compat}"
            )
        else:
            console.print(
                f"  [dim]·  {escape(pl.name)}  {pl.version}  "
                "(not on Marketplace)[/dim]"
            )
            not_found_count += 1

    console.print()

    up_to_date = len(plugins) - len(updates_available) - not_found_count
    console.print("  [dim]Summary:[/dim]")
    console.print(f"    [dim]Up to date:[/dim]       {up_to_date}")
    if updates_available:
        console.print(f"    [yellow]Updates available:[/yellow] {len(updates_available)}")
    if not_found_count:
        console.print(
            f"    [dim]Not on Marketplace:[/dim] {not_found_count}"
        )
    console.print()

    if not_found_count:
        console.print(
            f"[dim]  {not_found_count} plugin(s) not found on Marketplace.[/dim]"
        )
        console.print()

    if not updates_available:
        console.print("[bold green]✓  All plugins are up to date.[/bold green]")
        return

    n = len(updates_available)
    console.print(f"[bold yellow]{n}[/bold yellow] update(s) available")

    if dry_run:
        console.print("[dim]  --dry-run: nothing installed.[/dim]")
        return

    # ── 6 · Download & install ────────────────────────────────────────────────
    console.print()
    console.print(Rule("[bold]Installing Updates[/bold]", style="dim cyan"))
    console.print()

    ok_count = fail_count = 0

    with Progress(
        TextColumn("[bold]{task.description}", justify="left"),
        BarColumn(bar_width=28),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        for upd in updates_available:
            label = (
                f"[cyan]{escape(upd.plugin.name)}[/cyan]  "
                f"[dim]{upd.plugin.version}[/dim] → {upd.new_ver}"
            )
            task = progress.add_task(label, total=None)

            ok, msg = install_plugin(upd, ide.plugins_dir, progress, task)
            progress.update(task, visible=False)

            if ok:
                ok_count += 1
                console.print(
                    f"  [green]✓[/green]  [bold]{escape(upd.plugin.name)}[/bold] "
                    f"→ [green]{upd.new_ver}[/green]"
                )
            else:
                fail_count += 1
                console.print(
                    f"  [red]✗[/red]  [bold]{escape(upd.plugin.name)}[/bold]: "
                    f"[red]{escape(msg)}[/red]"
                )

    # ── 7 · Summary ───────────────────────────────────────────────────────────
    console.print()
    if fail_count == 0:
        console.print(Panel(
            f"[bold green]✓  Updated {ok_count} plugin(s) successfully.[/bold green]\n"
            "[dim]Restart your IDE to apply the changes.[/dim]",
            border_style="green",
        ))
    else:
        lines = [
            f"[green]✓ {ok_count} updated[/green]"
            + (f"   [red]✗ {fail_count} failed[/red]" if fail_count else ""),
            "[dim]Restart your IDE to apply the changes.[/dim]",
        ]
        console.print(Panel("\n".join(lines), border_style="yellow"))

# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        prog="update-plugins",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "ide",
        help="IDE name or alias (e.g. clion, ij, pycharm, rider, …)",
    )
    ap.add_argument(
        "-n", "--dry-run",
        action="store_true",
        help="Check for updates but do not download or install anything",
    )
    ap.add_argument(
        "-l", "--list-only",
        action="store_true",
        help="List installed plugins and exit without checking for updates",
    )
    ap.add_argument(
        "--plugins-dir",
        metavar="PATH",
        help="Override the plugins directory (useful for non-Toolbox installs)",
    )
    ap.add_argument(
        "-x", "--proxy",
        metavar="URL",
        help="Proxy URL (e.g. http://127.0.0.1:10808 or socks5://127.0.0.1:10808)",
    )
    args = ap.parse_args()

    plugins_dir_override = Path(args.plugins_dir) if args.plugins_dir else None
    if plugins_dir_override and not plugins_dir_override.is_dir():
        console.print(
            f"[red]✗[/red]  --plugins-dir does not exist: {plugins_dir_override}"
        )
        raise SystemExit(1)

    try:
        run(
            ide_arg=args.ide,
            dry_run=args.dry_run,
            list_only=args.list_only,
            plugins_dir_override=plugins_dir_override,
            proxy=args.proxy,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
