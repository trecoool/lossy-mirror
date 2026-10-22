#!/usr/bin/env python3
# One-way mirror: mapped audio formats are converted, everything else the filter allows is copied as-is.
# One target per source track: same-name audio files compete, the best one wins, older targets are replaced.
# Up to date = target mtime equals source mtime (plus size for copies). Writes are atomic renames,
# conversions are built in a local temp file first. Orphans are removed last, behind a ratio guard.

import argparse
import fnmatch
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import audio
import config

TMP_SUFFIX = ".tmp"

log = logging.getLogger("lossy-mirror")


def included(rules, name, is_dir):
    """rsync filter semantics on basenames, case-insensitive: first match wins, no match = include."""
    for include, pattern, dir_only in rules:
        if dir_only and not is_dir:
            continue
        if fnmatch.fnmatchcase(name.casefold(), pattern):
            return include
    return True


def plan(src, cfg, only):
    """Returns ({destination rel path: (source path, Conversion or None)}, [(winner rel, [loser rels])])."""
    jobs, collisions = {}, []
    for dirpath, dirnames, filenames in os.walk(os.path.join(src, only)):
        dirnames[:] = sorted(d for d in dirnames if included(cfg.rules, d, True))
        rel_dir = os.path.relpath(dirpath, src)
        tracks = {}
        for name in sorted(filenames):
            if not included(cfg.rules, name, False):
                continue
            stem, ext = os.path.splitext(name)
            if audio.is_audio(ext):
                tracks.setdefault(stem.casefold(), []).append(os.path.join(dirpath, name))
            else:
                jobs[os.path.normpath(os.path.join(rel_dir, name))] = (os.path.join(dirpath, name), None)
        for paths in tracks.values():
            winner = paths[0] if len(paths) == 1 else audio.best(paths, cfg.priority)
            name = os.path.basename(winner)
            stem, ext = os.path.splitext(name)
            conv = cfg.conversions.get(ext.lower())
            target = stem + conv.output if conv else name
            jobs[os.path.normpath(os.path.join(rel_dir, target))] = (winner, conv)
            if len(paths) > 1:
                collisions.append((os.path.relpath(winner, src),
                                   [os.path.relpath(p, src) for p in paths if p != winner]))
    return jobs, collisions


def same_mtime(a, b):
    return int(os.stat(a).st_mtime) == int(os.stat(b).st_mtime)


def finish(src_file, tmp, dst_file):
    st = os.stat(src_file)
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(tmp, dst_file)


def discard(path):
    """Removes path if present; no exists() check, a network fs may report a renamed file as still there."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def shadowed(dst_file, dst, expected):
    """Same-stem audio files beside the target that no job owns (e.g. the old format of this track)."""
    folder, name = os.path.split(dst_file)
    if not os.path.isdir(folder):
        return []
    stem = os.path.splitext(name)[0].casefold()
    found = []
    for other in sorted(os.listdir(folder)):
        o_stem, o_ext = os.path.splitext(other)
        rel = os.path.normpath(os.path.relpath(os.path.join(folder, other), dst))
        if other != name and o_stem.casefold() == stem and audio.is_audio(o_ext) and rel not in expected:
            found.append(rel)
    return found


def remove_all(dst, rels):
    for rel in rels:
        os.remove(os.path.join(dst, rel))


def build_command(conv, src_file, out):
    argv = []
    for arg in conv.command:
        if arg == "{resample}":
            rate = audio.sample_rate(src_file)
            if conv.max_sample_rate and rate > conv.max_sample_rate:
                target = 44100 if rate % 44100 == 0 and conv.max_sample_rate >= 44100 else conv.max_sample_rate
                argv += ["-ar", str(target)]
        else:
            argv.append(arg.replace("{input}", src_file).replace("{output}", out))
    return argv


def sync_convert(src_file, dst_file, conv, dry_run, dst, siblings):
    if os.path.exists(dst_file) and same_mtime(src_file, dst_file):
        if not dry_run:
            remove_all(dst, siblings)
        return None
    tags = audio.read_tags(src_file)  # refuse before any work if tags cannot be carried over
    if dry_run:
        return "convert"
    fd, work = tempfile.mkstemp(suffix=conv.output)
    os.close(fd)
    tmp = dst_file + TMP_SUFFIX
    try:
        subprocess.run(build_command(conv, src_file, work), check=True)
        audio.write_tags(work, tags)
        shutil.copyfile(work, tmp)
        finish(src_file, tmp, dst_file)
        remove_all(dst, siblings)  # same watcher batch as the rename: Navidrome sees a move, keeps history
    finally:
        for path in (work, tmp):
            discard(path)
    return "convert"


def sync_copy(src_file, dst_file, dry_run, dst, siblings):
    if os.path.exists(dst_file):
        s, d = os.stat(src_file), os.stat(dst_file)
        if s.st_size == d.st_size and int(s.st_mtime) == int(d.st_mtime):
            if not dry_run:
                remove_all(dst, siblings)
            return None
    if dry_run:
        return "copy"
    tmp = dst_file + TMP_SUFFIX
    try:
        shutil.copyfile(src_file, tmp)
        finish(src_file, tmp, dst_file)
        remove_all(dst, siblings)
    finally:
        discard(tmp)
    return "copy"


def find_orphans(dst, only, expected):
    orphans = []
    for dirpath, _, filenames in os.walk(os.path.join(dst, only)):
        for name in filenames:
            rel = os.path.normpath(os.path.relpath(os.path.join(dirpath, name), dst))
            if rel not in expected:
                orphans.append(rel)
    return sorted(orphans)


def remove_empty_dirs(dst, only):
    top = os.path.normpath(os.path.join(dst, only))
    for dirpath, _, _ in os.walk(top, topdown=False):
        if os.path.normpath(dirpath) != top and not os.listdir(dirpath):
            os.rmdir(dirpath)


def run(src, dst, cfg, only="", dry_run=False, jobs=1, max_delete_ratio=0.1):
    """One full mirror pass. Returns (exit code, number of files written or deleted)."""
    pool = ThreadPoolExecutor(max_workers=jobs)
    previous_term = signal.getsignal(signal.SIGTERM)

    def on_term(*_):
        pool.shutdown(wait=False, cancel_futures=True)  # otherwise exit waits for every queued file
        sys.exit(143)

    signal.signal(signal.SIGTERM, on_term)
    try:
        return _run(pool, src, dst, cfg, only, dry_run, max_delete_ratio)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        pool.shutdown()


def _run(pool, src, dst, cfg, only, dry_run, max_delete_ratio):
    start = time.time()
    log.info("Filter: " + " | ".join(f"{'+' if inc else '-'} {pat}{'/' if d else ''}" for inc, pat, d in cfg.rules))
    for out in sorted({c.output for c in cfg.conversions.values()}):
        log.info(f"Convert: {', '.join(e for e in cfg.priority if cfg.conversions[e].output == out)} -> {out}")
    plan_jobs, collisions = plan(src, cfg, only)
    if not plan_jobs:
        log.error(f"ERROR: nothing found under {os.path.join(src, only)}, aborting")
        return 1, 0
    log.info(f"Planned {len(plan_jobs)} files{' (dry run)' if dry_run else ''}")
    for winner, losers in collisions:
        log.warning(f"COLLISION {winner} wins over {', '.join(losers)}")

    def run_job(item):
        """Returns (action, siblings, error) for one destination file: siblings replaced, or kept on error."""
        rel, (src_file, conv) = item
        dst_file = os.path.join(dst, rel)
        siblings = []
        try:
            if audio.is_audio(os.path.splitext(rel)[1]):
                siblings = shadowed(dst_file, dst, plan_jobs)
            if not dry_run:
                os.makedirs(os.path.dirname(dst_file), exist_ok=True)
            if conv:
                return sync_convert(src_file, dst_file, conv, dry_run, dst, siblings), siblings, None
            return sync_copy(src_file, dst_file, dry_run, dst, siblings), siblings, None
        except Exception as e:
            return None, siblings, e

    counts = {"convert": 0, "copy": 0, "error": 0}
    replaced, kept = set(), set()
    report = []  # anything needing attention, repeated at the end of the log
    total = len(plan_jobs)
    results = pool.map(run_job, plan_jobs.items())  # yields in plan order: log stays sorted
    for i, (rel, (action, siblings, error)) in enumerate(zip(plan_jobs, results), 1):
        if error:
            counts["error"] += 1
            log.error(f"[{i}/{total}] ERROR {rel}: {error}")
            report.append(f"ERROR {rel}: {error}")
            for old in siblings:  # a failed track keeps its previous version in the mirror
                kept.add(old)
                log.warning(f"[{i}/{total}] KEEP {old}")
                report.append(f"KEEP {old} (previous version of the failed track)")
            continue
        if not action:
            continue
        counts[action] += 1
        log.info(f"[{i}/{total}] {action.upper()} {rel}")
        for old in siblings:
            replaced.add(old)
            log.info(f"[{i}/{total}] REPLACE {old}")

    orphans = [o for o in find_orphans(dst, only, set(plan_jobs)) if o not in replaced and o not in kept]
    total_dst = total + len(orphans)
    deleted = 0
    if orphans and len(orphans) > max(20, max_delete_ratio * total_dst):
        log.error(f"WARNING: {len(orphans)}/{total_dst} destination files are orphans, skipping removal "
                  f"(raise max delete ratio if intended)")
        for rel in orphans if dry_run else orphans[:20]:
            log.error(f"  orphan: {rel}")
        counts["error"] += 1
        report.append(f"ORPHANS NOT REMOVED: {len(orphans)}/{total_dst}, above max delete ratio (listed above)")
    else:
        for rel in orphans:
            log.info(f"DELETE {rel}")
            if not dry_run:
                os.remove(os.path.join(dst, rel))
        deleted = len(orphans)
        if not dry_run:
            remove_empty_dirs(dst, only)

    if collisions:
        report.append(f"{len(collisions)} collisions: same-name audio in a folder, only the best is mirrored "
                      f"(COLLISION lines above)")
    log.info("Report:" if report else "Report: nothing to report")
    for line in report:
        log.info(f"  {line}")
    elapsed = time.strftime("%H:%M:%S", time.gmtime(time.time() - start))
    log.info(f"Done in {elapsed}: {counts['convert']} converted, {counts['copy']} copied, {len(replaced)} replaced, "
             f"{len(orphans)} orphans, {len(collisions)} collisions, {counts['error']} errors")
    changed = counts["convert"] + counts["copy"] + len(replaced) + deleted
    return (1 if counts["error"] else 0), changed


def main():
    p = argparse.ArgumentParser()
    p.add_argument("src")
    p.add_argument("dst")
    p.add_argument("--config", default="/config/mirror.toml")
    p.add_argument("--only", default="", help="subpath to restrict the run to, e.g. 'Artist/Album'")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--jobs", type=int, default=1, help="files processed in parallel")
    p.add_argument("--max-delete-ratio", type=float, default=0.1,
                   help="skip orphan removal if it would delete more than this share of destination files")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    os.umask(0)
    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        log.error(f"ERROR: {e}")
        return 2
    code, _ = run(args.src, args.dst, cfg, args.only, args.dry_run, args.jobs, args.max_delete_ratio)
    return code


if __name__ == "__main__":
    sys.exit(main())
