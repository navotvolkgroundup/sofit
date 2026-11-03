"""run-and-digest.sh on a Linux box and on the original Mac.

The script shells out to scrape.py and, on the Mac, to the claude CLI.
These tests stub both. They never touch the real home directory, a browser,
or the performance log.
"""
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish" / "run-and-digest.sh"
PY = shutil.which("python3")

_OK = {
    "status": "ok",
    "updated": 2,
    "failed": 1,
    "sessions": {"tiktok": True, "instagram": False, "youtube": None},
    "best": {"hook": "standout"},
    "worst": None,
}


def _scrape(payload=None, *, code=0, stderr="", body=None) -> str:
    if body is not None:
        return body
    return (
        "import json, sys\n"
        f"sys.stderr.write({stderr!r})\n"
        f"print(json.dumps({payload!r}))\n"
        f"raise SystemExit({code})\n"
    )


def _wrapper(path: Path, real: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$0\" >> \"$HOME/used-python\"\n"
        f"exec {real} \"$@\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run(tmp_path, body, *, channel="none", python=None, publish=None,
         copy_beside=False, claude=False, prepare=None):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    if prepare is not None:
        prepare(home)
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    if claude:
        stub = bindir / "claude"
        stub.write_text(
            "#!/bin/sh\n"
            "cat > \"$HOME/claude.stdin\"\n"
            "printf '%s\\n' \"$@\" > \"$HOME/claude.argv\"\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
    if copy_beside:
        work = tmp_path / "beside"
        work.mkdir()
        script = work / "run-and-digest.sh"
        shutil.copy(SCRIPT, script)
        (work / "scrape.py").write_text(body, encoding="utf-8")
    else:
        script = SCRIPT
        publish = publish or (tmp_path / "publish")
        publish.mkdir(parents=True, exist_ok=True)
        (publish / "scrape.py").write_text(body, encoding="utf-8")
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = str(bindir) + os.pathsep + "/usr/bin:/bin"
    env["LC_ALL"] = "C.UTF-8"
    env["LANG"] = "C.UTF-8"
    env.pop("SOFIT_PYTHON", None)
    env.pop("SOFIT_PUBLISH_SCRIPTS", None)
    if channel is None:
        env.pop("SOFIT_DIGEST_CHANNEL", None)
    else:
        env["SOFIT_DIGEST_CHANNEL"] = channel
    if python is not None:
        env["SOFIT_PYTHON"] = python
    if not copy_beside:
        env["SOFIT_PUBLISH_SCRIPTS"] = str(publish)
    proc = subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc, home


def _history(home):
    path = home / ".ws-scraper" / "logs" / "scrape-history.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _logs(home):
    return home / ".ws-scraper" / "logs"


def test_run_and_digest_bash_n():
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR


def test_none_mode_sibling_scrape_records_history_and_prints_summary(tmp_path):
    proc, home = _run(
        tmp_path, _scrape(_OK, stderr="noise\n"),
        channel="none", copy_beside=True, claude=True)
    assert proc.returncode == 0, proc.stderr
    assert "noise" not in proc.stdout
    assert not (home / "claude.stdin").exists()
    summary = json.loads(proc.stdout)
    assert summary["status"] == "ok"
    assert summary["updated"] == 2
    assert summary["failed"] == 1
    assert summary["dead_sessions"] == ["instagram"]
    assert summary["sessions"]["youtube"] is None
    assert summary["code"] == 0
    rows = _history(home)
    assert len(rows) == 1
    assert rows[0]["updated"] == 2
    assert rows[0]["failed"] == 1
    assert re.fullmatch(r"\d{8}-\d{6}", rows[0]["stamp"])
    err = list(_logs(home).glob("*.err"))
    assert len(err) == 1
    assert err[0].read_text(encoding="utf-8") == "noise\n"
    assert list(_logs(home).glob("*.digest.log")) == []


def test_publish_scripts_env_overrides_sibling_scrape(tmp_path):
    home_dir = tmp_path / "case"
    home_dir.mkdir()
    # copy_beside would ignore SOFIT_PUBLISH_SCRIPTS; lay both out by hand.
    beside = tmp_path / "beside"
    beside.mkdir()
    script = beside / "run-and-digest.sh"
    shutil.copy(SCRIPT, script)
    (beside / "scrape.py").write_text(
        "import sys\nsys.stderr.write('WRONG\\n')\nraise SystemExit(9)\n",
        encoding="utf-8")
    publish = tmp_path / "publish"
    publish.mkdir()
    (publish / "scrape.py").write_text(_scrape(_OK), encoding="utf-8")
    home = home_dir / "home"
    home.mkdir()
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C.UTF-8",
        "SOFIT_DIGEST_CHANNEL": "none",
        "SOFIT_PUBLISH_SCRIPTS": str(publish),
        "SOFIT_PYTHON": PY,
    })
    proc = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["updated"] == 2
    assert "WRONG" not in proc.stderr
    assert _history(home)[0]["status"] == "ok"


def test_none_mode_session_dead_exits_clean_with_dead_session(tmp_path):
    payload = {"status": "session_dead", "fix": "run scrape.py --headed and log in"}
    proc, home = _run(tmp_path, _scrape(payload, code=2), channel="none", claude=True)
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["status"] == "session_dead"
    assert summary["code"] == 2
    assert summary["updated"] == 0
    assert summary["failed"] == 0
    assert summary["dead_sessions"] == ["tiktok"]
    assert _history(home)[0]["status"] == "session_dead"
    assert not (home / "claude.stdin").exists()


def test_none_mode_crash_and_empty_and_garbage(tmp_path):
    crash, home = _run(
        tmp_path / "crash",
        _scrape(body="import sys\nsys.stderr.write('boom\\n')\nraise SystemExit(1)\n"),
        channel="none")
    assert crash.returncode == 1
    summary = json.loads(crash.stdout)
    assert summary["status"] == "crash"
    assert summary["code"] == 1
    assert summary["updated"] == 0
    assert summary["failed"] == 0
    assert summary["dead_sessions"] == []
    assert _history(home)[0]["status"] == "crash"
    assert _history(home)[0]["code"] == 1
    assert list(_logs(home).glob("*.err"))[0].read_text(encoding="utf-8") == "boom\n"

    empty, home = _run(
        tmp_path / "empty",
        _scrape(body="raise SystemExit(0)\n"),
        channel="none")
    assert empty.returncode == 1
    assert json.loads(empty.stdout)["status"] == "empty"
    assert _history(home)[0]["status"] == "empty"

    bad, home = _run(
        tmp_path / "bad",
        _scrape(body="import sys\nsys.stdout.write('nope')\n"),
        channel="none")
    assert bad.returncode == 1
    assert json.loads(bad.stdout)["status"] == "unparseable"
    row = _history(home)[0]
    assert row["status"] == "unparseable"
    assert row["raw"] == "nope"


def test_default_channel_is_whatsapp_and_sends_only_when_rows_updated(tmp_path):
    proc, home = _run(tmp_path / "send", _scrape(_OK), channel=None, claude=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    stdin = (home / "claude.stdin").read_text(encoding="utf-8")
    argv = (home / "claude.argv").read_text(encoding="utf-8").splitlines()
    assert "best" in stdin
    assert "DIGEST_FAILED" in stdin
    assert "~/.sofit/performance.jsonl" in stdin
    assert argv[0] == "-p"
    assert argv[1] == "--allowedTools"
    assert "mcp__whatsapp__send_message" in argv[2]
    assert list(_logs(home).glob("*.digest.log"))
    assert _history(home)[0]["updated"] == 2

    quiet, home = _run(
        tmp_path / "quiet",
        _scrape({**_OK, "updated": 0}),
        channel=None, claude=True)
    assert quiet.returncode == 0, quiet.stderr
    assert quiet.stdout == ""
    assert not (home / "claude.stdin").exists()
    assert _history(home)[0]["updated"] == 0


def test_whatsapp_session_dead_and_crash_still_alert(tmp_path):
    dead, home = _run(
        tmp_path / "dead",
        _scrape({"status": "session_dead", "fix": "login"}, code=2),
        channel="whatsapp", claude=True, python=PY)
    assert dead.returncode == 0, dead.stderr
    assert dead.stdout == ""
    stdin = (home / "claude.stdin").read_text(encoding="utf-8")
    assert "ALERT_FAILED" in stdin
    assert "הסשן של טיקטוק פג" in stdin
    assert "--headed" in stdin
    assert "groundup-toolkit" not in stdin
    assert "hebrew-chapters" not in stdin
    assert _history(home)[0]["status"] == "session_dead"

    crash, home = _run(
        tmp_path / "crash",
        _scrape(body="import sys\nraise SystemExit(7)\n"),
        channel="whatsapp", claude=True)
    assert crash.returncode == 1
    assert crash.stdout == ""
    stdin = (home / "claude.stdin").read_text(encoding="utf-8")
    assert "ALERT_FAILED" in stdin
    assert "קרס (קוד 7)" in stdin
    assert _history(home)[0]["status"] == "crash"


def test_missing_claude_skips_send_without_failing_the_scrape(tmp_path):
    proc, home = _run(tmp_path / "ok", _scrape(_OK), channel="whatsapp", claude=False)
    assert proc.returncode == 0, proc.stderr
    assert "claude not found" in proc.stdout
    assert "skipping WhatsApp digest" in proc.stdout
    assert "command not found" not in proc.stderr
    assert _history(home)[0]["updated"] == 2
    assert list(_logs(home).glob("*.digest.log")) == []

    # ~/.local/bin/claude is the Mac fallback. A stub there still sends.
    local, home = _run(
        tmp_path / "local", _scrape(_OK), channel="whatsapp", claude=False,
        prepare=lambda h: _wrapper_claude(h / ".local" / "bin" / "claude"))
    assert local.returncode == 0, local.stderr
    assert local.stdout == ""
    assert "DIGEST_FAILED" in (home / "claude.stdin").read_text(encoding="utf-8")

    crash, home = _run(
        tmp_path / "crash",
        _scrape(body="import sys\nraise SystemExit(1)\n"),
        channel="whatsapp", claude=False)
    assert crash.returncode == 1
    assert "claude not found" in crash.stdout
    assert "קרס" in crash.stdout
    assert "command not found" not in crash.stderr
    assert _history(home)[0]["status"] == "crash"


def _wrapper_claude(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        "cat > \"$HOME/claude.stdin\"\n"
        "printf '%s\\n' \"$@\" > \"$HOME/claude.argv\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_python_honors_override_then_venv_then_python3(tmp_path):
    body = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "Path.home().joinpath('exe.txt').write_text(sys.executable)\n"
        "print(json.dumps({'status':'ok','updated':0,'failed':0,'sessions':{}}))\n"
    )
    override = tmp_path / "override-python"

    def both(home):
        _wrapper(home / ".ws-scraper" / "venv" / "bin" / "python", PY)
        _wrapper(override, PY)

    chosen, home = _run(
        tmp_path / "override", body, channel="none", python=str(override), prepare=both)
    assert chosen.returncode == 0, chosen.stderr
    used = (home / "used-python").read_text(encoding="utf-8").splitlines()
    assert used
    assert set(used) == {str(override)}

    def venv_only(home):
        _wrapper(home / ".ws-scraper" / "venv" / "bin" / "python", PY)

    mac, home = _run(tmp_path / "mac", body, channel="none", prepare=venv_only)
    assert mac.returncode == 0, mac.stderr
    venv = home / ".ws-scraper" / "venv" / "bin" / "python"
    used = (home / "used-python").read_text(encoding="utf-8").splitlines()
    assert used
    assert set(used) == {str(venv)}

    plain, home = _run(tmp_path / "plain", body, channel="none")
    assert plain.returncode == 0, plain.stderr
    assert not (home / "used-python").exists()
    exe = (home / "exe.txt").read_text(encoding="utf-8")
    assert Path(exe).name.startswith("python3")
    assert ".ws-scraper" not in exe


def test_unknown_digest_channel_exits_before_scrape(tmp_path):
    marker = tmp_path / "ran"
    body = f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
    proc, home = _run(tmp_path, body, channel="slack")
    assert proc.returncode == 1
    assert "unknown SOFIT_DIGEST_CHANNEL=slack" in proc.stdout
    assert not marker.exists()
    assert not (home / ".ws-scraper").exists()


def test_no_legacy_checkout_paths():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "hebrew-chapters" not in text
    assert "groundup-toolkit" not in text
    assert "/usr/bin/python3" not in text
    assert "SOFIT_PUBLISH_SCRIPTS" in text
    assert "SOFIT_PYTHON" in text
    assert 'CHANNEL="${SOFIT_DIGEST_CHANNEL:-whatsapp}"' in text
    assert 'SCRAPE="${SOFIT_PUBLISH_SCRIPTS:-$SCRIPT_DIR}/scrape.py"' in text
