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


# ─── Main pipeline ────────────────────────────────────────────────────────────

def run_pipeline(config_file: str,
                 force_regenerate: bool = False,
                 dry_run: bool = False):

    print("=" * 65)
    print("  PERFORMANCE TESTING PIPELINE")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    # ── Load config ───────────────────────────────────────────────────────
    cfg = load_config(config_file)

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
        # Only update hash if JMX was regenerated successfully
        # so next run knows not to regenerate again
        if payload.get("regenerate_jmx") and spec_path.exists():
            store_hash(get_spec_hash(spec_path))
            print(f"[pipeline] Spec hash updated — JMX is current")

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


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Performance Testing Pipeline Runner"
    )
    parser.add_argument("--config", default="perf-config.yaml",
                        help="Path to perf-config.yaml")
    parser.add_argument("--force-regenerate", action="store_true",
                        help="Force JMX regeneration even if spec unchanged")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip JMeter — test AI analysis layer only")
    args = parser.parse_args()

    run_pipeline(
        config_file      = args.config,
        force_regenerate = args.force_regenerate,
        dry_run          = args.dry_run,
    )
