#!/usr/bin/env python3
"""Alert a Teams chat when a container on this VM crashes or stays down.

Every service runs with `restart: always`, so a crash is normally invisible:
Docker brings the container back within seconds and `docker ps` shows it Up.
The signal is the container's RestartCount going up between two runs, not its
state. A container that is simply not running (image gone, daemon trouble,
someone stopped it) is caught separately, after it has stayed down for two runs
in a row so a deploy's recreate doesn't trip it.

Covers the compose stacks in WATCHED_PROJECTS (or --projects). A stack with no
containers at all, or a container that disappears from a stack, also counts as
down. Run from cron every minute; state lives next to this script.

The webhook is a Teams Workflows "Send webhook alerts to a chat" URL, read from
TEAMS_WEBHOOK_URL or the webhook file (default ~/.container-watchdog/webhook).

Stdlib only, so it runs on the VM's system python3 without a venv.
"""
import argparse
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

HOME_DIR = os.path.expanduser("~/.container-watchdog")
# Compose project names, which are the stack's directory name unless the
# compose file sets `name:` or it is started with -p.
WATCHED_PROJECTS = [
    "docker-agile-classifier",
    "docker-agile-classifier-2",
    "docker-agile-registration",
    "docker-agile-registration-2",
    "docker-ap-invoice",
    "docker-agile-ap",
    "docker-agile-docalloc",
]
# A crash loop restarts every few seconds; one alert per container per window,
# with the restarts in between counted onto the next one.
RESTART_COOLDOWN_SECONDS = 900
# Runs a container must be seen not running before it is reported down.
DOWN_AFTER_RUNS = 2
LOG_TAIL_LINES = 25
MAX_DETAIL_CHARS = 3000
POST_TIMEOUT = 10


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60, check=True).stdout


def list_containers(projects):
    ids = docker("ps", "-aq", "--filter", "label=com.docker.compose.project").split()
    if not ids:
        return {}
    containers = {}
    for c in json.loads(docker("inspect", *ids)):
        labels = c["Config"].get("Labels") or {}
        if labels.get("com.docker.compose.project") not in projects:
            continue
        name = c["Name"].lstrip("/")
        containers[name] = {
            "id": c["Id"],
            "project": labels.get("com.docker.compose.project", "-"),
            "service": labels.get("com.docker.compose.service", "-"),
            "image": c["Config"].get("Image", "-"),
            "status": c["State"]["Status"],
            "restart_count": c.get("RestartCount", 0),
            "exit_code": c["State"].get("ExitCode", 0),
            "oom_killed": c["State"].get("OOMKilled", False),
            "started_at": c["State"].get("StartedAt", "-"),
            "finished_at": c["State"].get("FinishedAt", "-"),
        }
    return containers


def log_tail(name):
    try:
        r = subprocess.run(["docker", "logs", "--tail", str(LOG_TAIL_LINES), name],
                           capture_output=True, text=True, timeout=30)
        # Containers log to either stream; interleaving is lost but the tail is what matters.
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return f"(could not read logs: {e})"


def build_card(title, color, facts, detail=None):
    body = [
        {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "color": color, "wrap": True},
        {"type": "FactSet", "facts": [{"title": k, "value": str(v)} for k, v in facts]},
    ]
    if detail:
        if len(detail) > MAX_DETAIL_CHARS:
            detail = "…" + detail[-MAX_DETAIL_CHARS:]
        body.append({"type": "TextBlock", "text": detail, "wrap": True, "fontType": "Monospace"})
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "msteams": {"width": "Full"},
                "body": body,
            },
        }],
    }


def container_facts(host, name, c):
    return [
        ("Host", host),
        ("Container", name),
        ("Stack / service", f"{c['project']} / {c['service']}"),
        ("Image", c["image"]),
        ("Status", c["status"]),
        ("Last exit code", c["exit_code"]),
        ("OOM killed", "yes" if c["oom_killed"] else "no"),
        ("Started (UTC)", c["started_at"][:19]),
    ]


def check(state, containers, projects, host, now):
    """Compare this run with the last one. Returns (new_state, cards)."""
    cards = []
    new_state = {"containers": {}, "projects": {}, "docker_down": False}
    present = {c["project"] for c in containers.values()}

    for name, c in containers.items():
        prev = state.get("containers", {}).get(name)
        entry = {
            "id": c["id"],
            "project": c["project"],
            "service": c["service"],
            "restart_count": c["restart_count"],
            "down_runs": 0 if c["status"] == "running" else (prev or {}).get("down_runs", 0) + 1,
            "reported_down": (prev or {}).get("reported_down", False),
            "last_restart_alert": (prev or {}).get("last_restart_alert", 0),
            "pending_restarts": (prev or {}).get("pending_restarts", 0),
        }

        # A new id means the container was recreated (a deploy); its count starts
        # again from 0, so there is nothing to compare against. Restarts still
        # held back by the cooldown carry over and are reported.
        if prev and prev["id"] == c["id"]:
            entry["pending_restarts"] += max(0, c["restart_count"] - prev["restart_count"])

        if entry["pending_restarts"] and now - entry["last_restart_alert"] >= RESTART_COOLDOWN_SECONDS:
            n = entry["pending_restarts"]
            facts = container_facts(host, name, c) + [("Restarts", f"{n} since last alert, {c['restart_count']} total")]
            cards.append(build_card(f"🔁 {name} crashed and was restarted", "Warning", facts, log_tail(name)))
            entry["pending_restarts"] = 0
            entry["last_restart_alert"] = now

        if entry["down_runs"] >= DOWN_AFTER_RUNS and not entry["reported_down"]:
            cards.append(build_card(f"🔴 {name} is down", "Attention",
                                    container_facts(host, name, c) + [("Stopped (UTC)", c["finished_at"][:19])],
                                    log_tail(name)))
            entry["reported_down"] = True
        elif c["status"] == "running" and entry["reported_down"]:
            cards.append(build_card(f"🟢 {name} is running again", "Good", container_facts(host, name, c)))
            entry["reported_down"] = False

        new_state["containers"][name] = entry

    # A deploy recreates a container under the same name, so one that is gone
    # for two runs was removed (`docker compose rm`, `docker rm`). Its last known
    # entry is kept until it comes back. A stack with nothing left is reported
    # once as a whole below instead of container by container.
    for name, prev in state.get("containers", {}).items():
        if name in containers or prev.get("project") not in projects or prev.get("project") not in present:
            continue
        entry = dict(prev, down_runs=prev.get("down_runs", 0) + 1)
        if entry["down_runs"] >= DOWN_AFTER_RUNS and not entry.get("reported_down"):
            cards.append(build_card(f"🔴 {name} is down (container removed)", "Attention", [
                ("Host", host), ("Container", name),
                ("Stack / service", f"{prev.get('project')} / {prev.get('service')}"),
            ]))
            entry["reported_down"] = True
        new_state["containers"][name] = entry

    for p in projects:
        prev = state.get("projects", {}).get(p, {})
        entry = {
            "down_runs": 0 if p in present else prev.get("down_runs", 0) + 1,
            "reported_down": prev.get("reported_down", False),
        }
        if entry["down_runs"] >= DOWN_AFTER_RUNS and not entry["reported_down"]:
            cards.append(build_card(f"🔴 {p} has no containers", "Attention", [
                ("Host", host), ("Stack", p),
                ("Hint", "stack not started, taken down, or its compose project name differs"),
            ]))
            entry["reported_down"] = True
        elif p in present and entry["reported_down"]:
            services = sorted(c["service"] for c in containers.values() if c["project"] == p)
            cards.append(build_card(f"🟢 {p} has containers again", "Good",
                                    [("Host", host), ("Stack", p), ("Services", ", ".join(services))]))
            entry["reported_down"] = False
        new_state["projects"][p] = entry

    return new_state, cards


def post(url, card):
    req = urllib.request.Request(url, data=json.dumps(card).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=POST_TIMEOUT) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"webhook returned {resp.status}")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-dir", default=HOME_DIR)
    ap.add_argument("--webhook-file", default=os.path.join(HOME_DIR, "webhook"))
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of posting; state is still saved")
    ap.add_argument("--test-alert", action="store_true", help="post one test card and exit")
    ap.add_argument("--projects", help="comma-separated compose projects to watch instead of WATCHED_PROJECTS")
    ap.add_argument("--status", action="store_true", help="print what each watched stack has running and exit")
    args = ap.parse_args()
    projects = [p.strip() for p in args.projects.split(",") if p.strip()] if args.projects else WATCHED_PROJECTS

    os.makedirs(args.state_dir, exist_ok=True)
    state_path = os.path.join(args.state_dir, "state.json")
    host = socket.gethostname()
    now = time.time()

    url = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
    if not url and os.path.exists(args.webhook_file):
        with open(args.webhook_file) as f:
            url = f.read().strip()
    if not url and not (args.dry_run or args.status):
        sys.exit(f"no webhook: set TEAMS_WEBHOOK_URL or write it to {args.webhook_file}")

    def send(card):
        if args.dry_run:
            print(json.dumps(card, indent=2, ensure_ascii=False))
        else:
            post(url, card)

    if args.test_alert:
        send(build_card(f"✅ container watchdog test from {host}", "Good", [("Host", host)]))
        return

    if args.status:
        found = list_containers(projects)
        for p in projects:
            cs = sorted(f"{n} ({c['status']})" for n, c in found.items() if c["project"] == p)
            print(f"{p}: {', '.join(cs) if cs else 'NO CONTAINERS'}")
        return

    # Cron can overlap a slow run (docker hanging); the second one just exits.
    lock = open(os.path.join(args.state_dir, "lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return

    state = load_json(state_path, {})
    try:
        containers = list_containers(projects)
    except Exception as e:
        e = getattr(e, "stderr", None) or e
        # The daemon itself is down: every container is down with it, and there
        # is nothing to compare. Alert once, keep the old state for when it's back.
        if not state.get("docker_down"):
            send(build_card(f"🔴 Docker is not responding on {host}", "Attention", [("Host", host)], str(e)))
            state["docker_down"] = True
            with open(state_path, "w") as f:
                json.dump(state, f)
        sys.exit(f"docker unavailable: {e}")

    first_run = not state
    new_state, cards = check(state, containers, projects, host, now)
    if state.get("docker_down"):
        cards.insert(0, build_card(f"🟢 Docker is responding again on {host}", "Good", [("Host", host)]))

    # Save before posting: a failed post must not make the next run re-alert
    # everything, and a lost alert is better than one repeated every minute.
    tmp = state_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(new_state, f, indent=1)
    os.replace(tmp, state_path)

    if first_run:
        print(f"baseline recorded for {len(containers)} containers")
        return
    for card in cards:
        try:
            send(card)
        except Exception as e:
            print(f"post failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
