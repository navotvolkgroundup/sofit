"""Which show sofit is working on, and where that show's machine-local files live.

A network keeps one folder per show: ~/.sofit/shows/<slug>/ holds its
publish.json, brand.json, show.json, assets/, plans/, performance.jsonl and
seo-playbook.md. The show is $SOFIT_SHOW, else the slug in ~/.sofit/default-show,
else none - the single-show layout where ~/.sofit itself is the show.

Stdlib only and Python 3.9-safe: the publish scripts import this from the
ws-scraper venv, which has neither sofit's dependencies nor a newer Python.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

# What belongs to one show. ~/.sofit/env (API keys) stays network-wide.
SHOW_ITEMS = ("publish.json", "brand.json", "show.json", "assets", "plans",
              "performance.jsonl", "seo-playbook.md", "processed-episodes.txt")
SHOW_TEMPLATE = {"name": "", "never": [], "hosts": [], "about": ""}
DEFAULT_PROFILE = "~/.ws-scraper/profile"


def root() -> Path:
    return Path.home() / ".sofit"


def slug() -> str | None:
    s = os.environ.get("SOFIT_SHOW", "").strip()
    if not s:
        try:
            s = (root() / "default-show").read_text(encoding="utf-8").strip()
        except OSError:
            s = ""
    return s or None


def home() -> Path:
    s = slug()
    return root() / "shows" / s if s else root()


def _json(name: str) -> dict:
    try:
        return json.loads((home() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def show_config() -> dict:
    """show.json: name, never (spellings to avoid), hosts, about. {} if absent."""
    return _json("show.json")


def browser_profile() -> Path:
    """Chrome profile holding this show's logins - one per show, since one
    profile can be logged in to only one account per platform."""
    p = os.environ.get("WS_SCRAPER_PROFILE") or _json("publish.json").get(
        "browser_profile") or DEFAULT_PROFILE
    return Path(p).expanduser()


def migrate(new_slug: str, apply: bool = False) -> list[str]:
    """Move the single-show ~/.sofit layout into shows/<new_slug>/.

    Each moved item leaves a symlink at its old path, so hardcoded paths
    (skills, launchd jobs, plans_dir in publish.json) keep working. Rows in
    the performance log get "show". Safe to rerun. Returns the steps, which
    are only printed, not done, unless apply.
    """
    r = root()
    dest = r / "shows" / new_slug
    steps = []
    for item in SHOW_ITEMS:
        src = r / item
        if src.is_symlink() or not src.exists() or (dest / item).exists():
            continue
        steps.append(f"move {src} -> {dest / item} (symlink left behind)")
        if apply:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest / item))
            src.symlink_to(dest / item)
    log = dest / "performance.jsonl" if apply else r / "performance.jsonl"
    if log.exists():
        lines = log.read_text(encoding="utf-8").splitlines()
        out, n = [], 0
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                out.append(line)  # keep a malformed line rather than lose it
                continue
            if "show" not in row:
                row["show"], n = new_slug, n + 1
            out.append(json.dumps(row, ensure_ascii=False))
        if n:
            steps.append(f"tag {n} performance rows with show={new_slug}")
            if apply:
                tmp = log.with_suffix(".jsonl.tmp")
                tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
                os.replace(tmp, log.resolve())
    if not (dest / "show.json").exists():
        steps.append(f"write {dest / 'show.json'} template (fill in name/never/hosts/about)")
        if apply:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "show.json").write_text(
                json.dumps(SHOW_TEMPLATE, ensure_ascii=False, indent=1) + "\n",
                encoding="utf-8")
    current = r / "default-show"
    if not current.exists() or current.read_text(encoding="utf-8").strip() != new_slug:
        steps.append(f"set {r / 'default-show'} = {new_slug}")
        if apply:
            current.write_text(new_slug + "\n", encoding="utf-8")
    return steps


def cmd_migrate(argv: list[str]) -> int:
    """sofit show-migrate <slug> [--apply]"""
    args = [a for a in argv if a != "--apply"]
    if len(args) != 1:
        print("usage: sofit show-migrate <slug> [--apply]")
        return 2
    steps = migrate(args[0], apply="--apply" in argv)
    for s in steps:
        print(("done: " if "--apply" in argv else "would ") + s)
    if not steps:
        print("nothing to do")
    elif "--apply" not in argv:
        print("dry run - rerun with --apply")
    return 0
