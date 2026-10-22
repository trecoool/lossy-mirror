#!/usr/bin/env python3
# Container entrypoint: runs mirror passes on source changes (inotify + debounce) and on a sweep timer.
# ARMED=0 runs one dry-run pass (the plan) and idles. MODE=once runs a single pass and exits with its status.

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

import config
import mirror

log = mirror.log
CONFIG_FILE = "/config/mirror.toml"
DEFAULT_CONFIG = "/app/default-mirror.toml"
LOG_DIR = "/config/logs"
LOG_FORMAT = logging.Formatter("%(asctime)s %(message)s", "%y%m%d-%H:%M:%S")


def env(name, default):
    return os.environ.get(name, default)


def prepare_config_dir():
    """Creates logs and default config owned by PUID:PGID (appdata folders are created by root)."""
    uid, gid = int(env("PUID", "99")), int(env("PGID", "100"))
    os.makedirs(LOG_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_FILE):
        shutil.copyfile(DEFAULT_CONFIG, CONFIG_FILE)
        log.info(f"Wrote default config to {CONFIG_FILE}")
    if os.getuid() == 0:
        for path in (LOG_DIR, CONFIG_FILE):
            os.chown(path, uid, gid)


def drop_privileges():
    """Runs as PUID:PGID (Unraid default nobody:users) so every written file has that owner."""
    if os.getuid() != 0:
        return
    os.setgroups([])
    os.setgid(int(env("PGID", "100")))
    os.setuid(int(env("PUID", "99")))


def rotate_logs(keep):
    logs = sorted(f for f in os.listdir(LOG_DIR) if f.endswith(".log"))
    for name in logs[:-keep] if keep > 0 else logs:
        os.remove(os.path.join(LOG_DIR, name))


def mirror_pass(reason, dry_run):
    """One pass with its own log file; the file is kept only if the pass changed something or failed."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(LOG_DIR, f"{stamp}_mirror.log")
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(LOG_FORMAT)
    log.addHandler(handler)
    log.info(f"Pass started ({reason}{', dry run' if dry_run else ''})")
    try:
        cfg = config.load(CONFIG_FILE)  # reloaded every pass: edits apply without a restart
        code, changed = mirror.run("/src", "/dst", cfg, dry_run=dry_run, jobs=int(env("JOBS", "2")),
                                   max_delete_ratio=float(env("MAX_DELETE_RATIO", "0.1")))
    except config.ConfigError as e:
        log.error(f"ERROR: config: {e}")
        code, changed = 1, 0
    except Exception as e:
        log.error(f"ERROR: pass crashed: {type(e).__name__}: {e}")
        code, changed = 1, 0
    finally:
        log.removeHandler(handler)
        handler.close()
    if code == 0 and changed == 0 and not dry_run:
        os.remove(path)
    else:
        suffix = "_PLAN.log" if dry_run else "_OK.log" if code == 0 else "_ERRORED.log"
        os.replace(path, path.replace("_mirror.log", suffix))
        rotate_logs(int(env("LOG_COUNT", "20")))
    return code


class SourceWatcher:
    """inotifywait in monitor mode; remembers when the source last changed. Restarts it if it dies."""

    def __init__(self, paths):
        self.paths = paths
        self.changed = threading.Event()
        self.last_event = 0.0
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            proc = subprocess.Popen(["inotifywait", "-m", "-r", "-q", "--format", "%e %w%f",
                                     "-e", "create,delete,modify,moved_to,moved_from", *self.paths],
                                    stdout=subprocess.PIPE, text=True, errors="replace")
            log.info(f"Watching {', '.join(self.paths)}")
            for _ in proc.stdout:
                self.last_event = time.monotonic()
                self.changed.set()
            log.error(f"ERROR: inotifywait exited with {proc.wait()}, restarting in 60s (sweeps continue)")
            time.sleep(60)

    def wait_settled(self, debounce):
        """Blocks until no event arrived for `debounce` seconds."""
        while (quiet := time.monotonic() - self.last_event) < debounce:
            time.sleep(debounce - quiet)
        self.changed.clear()


def watch():
    debounce = int(env("DEBOUNCE_SECONDS", "60"))
    sweep = float(env("SWEEP_HOURS", "12")) * 3600
    watcher = SourceWatcher(env("WATCH_PATHS", "/src").split(":"))
    mirror_pass("startup", dry_run=False)
    last_pass = time.monotonic()
    while True:
        if watcher.changed.wait(timeout=max(0.0, last_pass + sweep - time.monotonic())):
            log.info(f"Source changed, waiting {debounce}s for activity to settle")
            watcher.wait_settled(debounce)
            mirror_pass("source changed", dry_run=False)
        else:
            mirror_pass("sweep", dry_run=False)
        last_pass = time.monotonic()


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # PID 1 ignores SIGTERM without a handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(LOG_FORMAT)
    log.addHandler(console)
    log.setLevel(logging.INFO)
    os.umask(int(env("UMASK", "000"), 8))
    prepare_config_dir()
    drop_privileges()
    armed = env("ARMED", "0") == "1"
    if env("MODE", "watch") == "once":
        return mirror_pass("once", dry_run=not armed)
    if not armed:
        mirror_pass("plan", dry_run=True)
        log.info(f"Not armed: nothing was written. Review the plan in {LOG_DIR}, then set ARMED=1 and restart.")
        threading.Event().wait()
    watch()


if __name__ == "__main__":
    sys.exit(main())
