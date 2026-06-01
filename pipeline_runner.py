"""
pipeline_runner.py
───────────────────
The main CI/CD pipeline entry point for performance testing.

Logic:
  1. Check if openapi.yaml changed since last JMX was generated
  2. If YES  → regenerate JMX using AI agent, commit it back to repo
  3. If NO   → reuse existing JMX from repo (fast path)
  4. ALWAYS  → run JMeter against the deployed API
  5. ALWAYS  → run GPT-4o analysis on results
  6. ALWAYS  → check SLAs and exit 0 (PASS) or 1 (FAIL)

Usage:
  python pipeline_runner.py --config perf-config.yaml
  python pipeline_runner.py --config perf-config.yaml --force-regenerate
  python pipeline_runner.py --config perf-config.yaml --dry-run
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests
import yaml


ROOT = Path(__file__).parent


# ─── Hash tracking ────────────────────────────────────────────────────────────

HASH_FILE = ROOT / ".perf_spec_hash"


def get_spec_hash(spec_path: Path) -> str:
    """SHA256 hash of the spec file — used to detect changes."""
    return hashlib.sha256(spec_path.read_bytes()).hexdigest()


def get_stored_hash() -> str | None:
    """Read previously stored spec hash."""
    if HASH_FILE.exists():
        return HASH_FILE.read_text().strip()
    return None


def store_hash(h: str):
    """Store current spec hash after successful JMX generation."""
    HASH_FILE.write_text(h)


def spec_changed(spec_path: Path) -> bool:
    """
    Returns True if the spec file has changed since last JMX generation.
    Uses SHA256 hash comparison — more reliable than git diff in CI.
    """
    current  = get_spec_hash(spec_path)
    previous = get_stored_hash()

    if previous is None:
        print("[pipeline] No previous spec hash found — first run, generating JMX")
        return True

    if current != previous:
        print(f"[pipeline] Spec changed — will regenerate JMX")
        print(f"[pipeline]   Previous hash: {previous[:12]}...")
        print(f"[pipeline]   Current  hash: {current[:12]}...")
        return True

    print("[pipeline] Spec unchanged — reusing existing JMX")
    return False


# ─── Load config ──────────────────────────────────────────────────────────────

def load_config(config_file: str) -> dict:
    path = Path(config_file)
    if not path.exists():
        print(f"[pipeline] ERROR: {config_file} not found")
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


# ─── Submit job to performance agent service ──────────────────────────────────

def submit_job(service_url: str, payload: dict) -> str:
    """Submit a performance test job to the agent service."""
    print(f"\n[pipeline] Submitting job → {service_url}/jobs/submit")
    r = requests.post(f"{service_url}/jobs/submit",
                      json=payload, timeout=30)
    if r.status_code != 200:
        print(f"[pipeline] ERROR: {r.status_code} — {r.text}")
        sys.exit(1)
    data = r.json()
    print(f"[pipeline] Job ID: {data['job_id']}")
    return data["job_id"]


# ─── Poll for completion ──────────────────────────────────────────────────────

def wait_for_job(service_url: str, job_id: str,
                 max_wait_sec: int = 1800) -> dict:
    import time
    print(f"[pipeline] Waiting for job {job_id}...")
    interval = 30
    elapsed  = 0

    while elapsed < max_wait_sec:
        time.sleep(interval)
        elapsed += interval
        r = requests.get(f"{service_url}/jobs/{job_id}/status", timeout=15)
        if r.status_code != 200:
            continue
        state  = r.json()
        status = state["status"]
        print(f"[pipeline] [{elapsed}s] Status: {status}")

        if status == "completed":
            return state
        if status == "failed":
            print(f"[pipeline] Job failed: {state.get('error')}")
            return state

    print(f"[pipeline] Timed out after {max_wait_sec}s")
    return {"status": "timeout", "overall": "UNKNOWN"}


# ─── Download reports ─────────────────────────────────────────────────────────

def download_reports(service_url: str, job_id: str, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    for suffix, fname in [
        ("/report",          f"report_{job_id}.html"),
        ("/report/markdown", f"report_{job_id}.md"),
    ]:
        r = requests.get(f"{service_url}/jobs/{job_id}{suffix}", timeout=30)
        if r.status_code == 200:
            out = output_dir / fname
            out.write_bytes(r.content)
            print(f"[pipeline] Saved: {out}")


# ─── Build payload ────────────────────────────────────────────────────────────

def build_payload(cfg: dict, force_regenerate: bool,
                  spec_path: Path, spec_has_changed: bool) -> dict:
    """
    Build the job request payload for the performance agent service.
    Key flag: regenerate_jmx tells the agent whether to rebuild the JMX.
    """
    should_regenerate = force_regenerate or spec_has_changed

    payload = {
        "team_name"        : cfg["team"]["name"],
        "base_url"         : cfg["api"]["base_url"],
        "profile"          : cfg.get("profile", "load"),
        "regenerate_jmx"   : should_regenerate,   # ← the key flag

        # Spec — always send so agent has it available
        "spec_content"     : spec_path.read_text(encoding="utf-8")
                             if spec_path.exists() else None,
        "spec_url"         : cfg["api"].get("spec_url"),

        # Auth from env (CI secrets)
        "auth_username"    : os.environ.get("API_AUTH_USERNAME",
                             cfg.get("api", {}).get("auth_username", "")),
        "auth_password"    : os.environ.get("API_AUTH_PASSWORD", ""),

        # SLA thresholds
        "sla": cfg["sla"],

        # Load settings
        "load": cfg.get("load", {}),

        # Certs
        "certs": {
            "skip_ssl_verify"    : cfg.get("certs", {}).get("skip_ssl_verify", False),
            "client_cert_password": os.environ.get("CLIENT_CERT_PASSWORD", ""),
        },

        # Notifications
        "webhook_url" : cfg.get("notify", {}).get("webhook_url"),
        "notify_email": cfg.get("notify", {}).get("email"),
    }

    print(f"\n[pipeline] JMX strategy: "
          f"{'REGENERATE (spec changed)' if should_regenerate else 'REUSE existing JMX'}")
    return payload


def generate_github_summary(service_url: str, job_id: str,
                            state: dict, cfg: dict, output_dir: Path):
    """
    Generate a rich GitHub Actions Summary markdown file.
    Reads the downloaded markdown report and creates a visual summary
    with bar indicators directly renderable in GitHub Actions Summary tab.
    Written to perf-results/github_summary.md which ci.yml pipes to
    GITHUB_STEP_SUMMARY.
    """
    overall    = state.get("overall", "UNKNOWN")
    violations = state.get("violations", 0)
    profile    = cfg.get("profile", "baseline")
    base_url   = cfg.get("api", {}).get("base_url", "")

    # ── Emoji indicators ──────────────────────────────────────────────────
    overall_emoji = "✅" if overall == "PASS" else "❌"
    badge = "🟢 PASS" if overall == "PASS" else "🔴 FAIL"

    # ── Parse metrics from downloaded markdown report ─────────────────────
    metrics_rows = []
    md_files = list(output_dir.glob("*.md"))
    if md_files:
        content = md_files[0].read_text(encoding="utf-8", errors="ignore")
        in_table = False
        for line in content.splitlines():
            if "|" in line and "Endpoint" in line:
                in_table = True
                continue
            if in_table and line.startswith("|---"):
                continue
            if in_table and line.startswith("|"):
                metrics_rows.append(line)
            elif in_table and not line.startswith("|"):
                in_table = False

    # ── Build bar chart from metrics ──────────────────────────────────────
    def make_bar(value: float, max_val: float, width: int = 20) -> str:
        """Create a unicode progress bar."""
        if max_val == 0:
            return "░" * width
        filled = min(int((value / max_val) * width), width)
        return "█" * filled + "░" * (width - filled)

    # Parse metric values from table rows
    parsed = []
    for row in metrics_rows:
        cols = [c.strip() for c in row.split("|") if c.strip()]
        if len(cols) >= 7:
            try:
                endpoint = cols[0].replace("`", "")[:45]
                requests = int(cols[1])
                mean     = float(cols[2].replace("ms",""))
                p95      = float(cols[3].replace("ms",""))
                p99      = float(cols[4].replace("ms",""))
                err      = float(cols[5].replace("%",""))
                tps      = float(cols[6])
                sla      = cols[7] if len(cols) > 7 else "?"
                parsed.append((endpoint, requests, mean, p95, p99, err, tps, sla))
            except (ValueError, IndexError):
                continue

    # ── Build summary markdown ────────────────────────────────────────────
    lines = []

    # Header
    lines += [
        f"# {overall_emoji} Performance Test Report — {badge}",
        "",
        f"| | |",
        f"|---|---|",
        f"| **Target API** | {base_url} |",
        f"| **Profile** | {profile} |",
        f"| **Threads** | {cfg.get('load', {}).get('threads', 1)} |",
        f"| **Duration** | {cfg.get('load', {}).get('duration_sec', 60)}s |",
        f"| **SLA Violations** | {violations} |",
        f"| **Job ID** | {job_id} |",
        "",
        "---",
        "",
    ]

    # Metrics table with SLA status
    if parsed:
        lines += [
            "## 📊 Metrics by Endpoint",
            "",
            "| Endpoint | Requests | Mean | P95 | P99 | Err% | TPS | SLA |",
            "|---|---:|---:|---:|---:|---:|---:|:---:|",
        ]
        for ep, req, mean, p95, p99, err, tps, sla in parsed:
            sla_icon = "✅" if sla.strip() == "PASS" else "❌"
            lines.append(
                f"| `{ep}` | {req} | {mean}ms | {p95}ms | {p99}ms "
                f"| {err}% | {tps} | {sla_icon} {sla} |"
            )
        lines.append("")
        lines.append("---")
        lines.append("")

    # Visual bar charts using unicode
    if parsed:
        max_p95 = max((r[3] for r in parsed), default=1)
        max_tps = max((r[6] for r in parsed), default=1)

        lines += [
            "## 📈 Response Time — P95 Visual",
            "",
            "```",
        ]
        for ep, req, mean, p95, p99, err, tps, sla in parsed:
            bar   = make_bar(p95, max_p95, 30)
            short = ep[:30].ljust(30)
            lines.append(f"{short} |{bar}| {p95}ms")
        lines += ["```", ""]

        lines += [
            "## ⚡ Throughput — TPS Visual",
            "",
            "```",
        ]
        for ep, req, mean, p95, p99, err, tps, sla in parsed:
            bar   = make_bar(tps, max_tps, 30)
            short = ep[:30].ljust(30)
            lines.append(f"{short} |{bar}| {tps} rps")
        lines += ["```", ""]

        # Error rate summary
        lines += [
            "## 🔍 Error Rate Summary",
            "",
        ]
        for ep, req, mean, p95, p99, err, tps, sla in parsed:
            icon = "🟢" if err == 0 else "🔴"
            lines.append(f"- {icon} `{ep[:50]}` — {err}%")
        lines += ["", "---", ""]

    # SLA summary
    lines += [
        "## 🎯 SLA Thresholds",
        "",
        f"- P95 must be < **{cfg.get('sla', {}).get('p95_ms', 2000)}ms**",
        f"- P99 must be < **{cfg.get('sla', {}).get('p99_ms', 4000)}ms**",
        f"- Error rate must be < **{cfg.get('sla', {}).get('error_rate_pct', 5.0)}%**",
        f"- TPS must be > **{cfg.get('sla', {}).get('throughput_min', 0.5)}**",
        "",
        "---",
        "",
        "> 💡 **Full HTML report with interactive charts** is available in the",
        "> **Artifacts** section below — download `perf-report-N` ZIP and open `report_*.html`",
    ]

    # Write to file
    summary_file = output_dir / "github_summary.md"
    summary_file.write_text("\n".join(lines), encoding="utf-8")
    print(f"[pipeline] GitHub summary: {summary_file}")


# ─── Main pipeline ────────────────────────────────────────────────────────────

def run_pipeline(config_file: str,
                 profile_override: str = None,
                 force_regenerate: bool = False,
                 dry_run: bool = False):

    print("=" * 65)
    print("  PERFORMANCE TESTING PIPELINE")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    # ── Load config ───────────────────────────────────────────────────────
    cfg = load_config(config_file)

    # Apply profile override from CLI if provided
    if profile_override:
        cfg["profile"] = profile_override
        print(f"[pipeline] Profile overridden to: {profile_override}")

    service_url = os.environ.get(
        "PERF_AGENT_URL",
        cfg.get("service_url", "http://localhost:8000")
    ).rstrip("/")

    # ── Locate spec file ──────────────────────────────────────────────────
    spec_file = cfg["api"].get("spec_file", "openapi.yaml")
    spec_path = Path(spec_file)

    # ── Detect spec change ────────────────────────────────────────────────
    # This is the CORE DECISION: regenerate JMX or reuse?
    if spec_path.exists():
        spec_has_changed = spec_changed(spec_path)
    else:
        print(f"[pipeline] Local spec not found: {spec_path}")
        print(f"[pipeline] Will use spec_url — treating as changed")
        spec_has_changed = True

    # ── Build payload ─────────────────────────────────────────────────────
    payload = build_payload(cfg, force_regenerate,
                            spec_path, spec_has_changed)

    if dry_run:
        payload["dry_run"] = True
        print("[pipeline] DRY RUN — JMeter will be skipped, AI demo mode")

    # ── Submit job ────────────────────────────────────────────────────────
    job_id = submit_job(service_url, payload)

    # ── Wait for completion ───────────────────────────────────────────────
    max_wait = cfg.get("load", {}).get("duration_sec", 300) + \
               cfg.get("load", {}).get("ramp_up_sec", 60) + 120
    state = wait_for_job(service_url, job_id, max_wait_sec=max_wait)

    # ── Download reports ──────────────────────────────────────────────────
    output_dir = ROOT / "perf-results"
    if state.get("status") == "completed":
        download_reports(service_url, job_id, output_dir)

        # ── Update spec hash after successful run ─────────────────────────
        if payload.get("regenerate_jmx") and spec_path.exists():
            store_hash(get_spec_hash(spec_path))
            print(f"[pipeline] Spec hash updated — JMX is current")

        # ── Generate GitHub Actions Summary file ──────────────────────────
        generate_github_summary(service_url, job_id, state, cfg, output_dir)

    # ── Final verdict ─────────────────────────────────────────────────────
    overall    = state.get("overall", "UNKNOWN")
    violations = state.get("violations", 0)

    print(f"\n{'='*65}")
    print(f"  RESULT: {overall}")
    print(f"  Violations : {violations}")
    print(f"  Reports    : {output_dir}")
    print(f"{'='*65}\n")

    # Exit code drives CI/CD pipeline gate
    # 0 = PASS = merge allowed
    # 1 = FAIL = pipeline blocked
    sys.exit(0 if overall == "PASS" else 1)


# ─── Multi-spec runner ────────────────────────────────────────────────────────

def run_all_pipelines(configs_dir: str,
                      services: list = None,
                      profile_override: str = None,
                      force_regenerate: bool = False,
                      dry_run: bool = False):
    """
    Discover all perf-config.yaml files under a directory and run
    one pipeline job per config. Optionally filter by service name.

    services = None        → run ALL services found
    services = ["booking"] → run only booking-service
    services = ["booking", "user"] → run only those two
    """
    configs_path = Path(configs_dir)
    all_configs  = sorted(configs_path.rglob("perf-config.yaml"))

    if not all_configs:
        print(f"[multi] No perf-config.yaml files found under: {configs_dir}")
        sys.exit(1)

    # ── Filter by selected services ───────────────────────────────────────
    if services:
        # Normalise to lowercase for case-insensitive matching
        selected     = [s.lower().strip() for s in services]
        config_files = [
            c for c in all_configs
            if any(sel in c.parent.name.lower() for sel in selected)
        ]
        if not config_files:
            print(f"[multi] No matching services found for: {services}")
            print(f"[multi] Available services:")
            for c in all_configs:
                print(f"         - {c.parent.name}")
            sys.exit(1)

        skipped = [c.parent.name for c in all_configs if c not in config_files]
        if skipped:
            print(f"[multi] Skipping: {', '.join(skipped)}")
    else:
        config_files = all_configs

    print("=" * 65)
    print(f"  MULTI-SERVICE PERFORMANCE PIPELINE")
    print(f"  Running {len(config_files)} of {len(all_configs)} service(s)")
    if services:
        print(f"  Filter : {', '.join(services)}")
    print("=" * 65)

    results = []

    for config_file in config_files:
        service = config_file.parent.name
        print(f"\n{'─'*65}")
        print(f"  Running: {service}  ({config_file})")
        print(f"{'─'*65}")

        try:
            run_pipeline(
                config_file      = str(config_file),
                profile_override = profile_override,
                force_regenerate = force_regenerate,
                dry_run          = dry_run,
            )
            results.append((service, "PASS"))
        except SystemExit as e:
            status = "PASS" if e.code == 0 else "FAIL"
            results.append((service, status))
        except Exception as e:
            print(f"[multi] ERROR in {service}: {e}")
            results.append((service, "ERROR"))

    # ── Consolidated summary ──────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("  MULTI-SERVICE SUMMARY")
    print("=" * 65)
    overall_pass = True
    for service, status in results:
        icon = "✅" if status == "PASS" else "❌"
        print(f"  {icon}  {service:<30} {status}")
        if status != "PASS":
            overall_pass = False

    print("=" * 65)
    print(f"  OVERALL: {'PASS — all services met SLA' if overall_pass else 'FAIL — one or more services violated SLA'}")
    print("=" * 65)

    sys.exit(0 if overall_pass else 1)


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Performance Testing Pipeline Runner"
    )
    parser.add_argument("--config", default="perf-config.yaml",
                        help="Path to master perf-config.yaml")
    parser.add_argument("--profile", default=None,
                        choices=["baseline", "load", "stress", "spike", "soak"],
                        help="Override load profile for all services")
    parser.add_argument("--force-regenerate", action="store_true",
                        help="Force JMX regeneration even if spec unchanged")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip JMeter — test AI analysis layer only")
    args = parser.parse_args()

    # ── Read master config ────────────────────────────────────────────────
    master_cfg   = load_config(args.config)
    services_dir = master_cfg.get("services_dir", "perf")
    run_services = master_cfg.get("run_services") or []

    # Normalise run_services — could be None, [], or a list of names
    if isinstance(run_services, str):
        run_services = [s.strip() for s in run_services.split(",") if s.strip()]

    # ── Validate services_dir exists ──────────────────────────────────────
    if not Path(services_dir).is_dir():
        print(f"[pipeline] ERROR: services_dir '{services_dir}' not found.")
        print(f"[pipeline] Create a perf/ folder with one subfolder per service.")
        print(f"[pipeline] Each subfolder needs: openapi.yaml + perf-config.yaml")
        sys.exit(1)

    print(f"[pipeline] services_dir : {services_dir}")
    print(f"[pipeline] run_services : {run_services if run_services else 'ALL'}")

    # ── Run all selected services ─────────────────────────────────────────
    run_all_pipelines(
        configs_dir      = services_dir,
        services         = run_services if run_services else None,
        profile_override = args.profile,
        force_regenerate = args.force_regenerate,
        dry_run          = args.dry_run,
    )
