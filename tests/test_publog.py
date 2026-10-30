

def test_pers_suffix_stripped_and_style_tagged():
    from sofit.publog import _clip_and_variant
    assert _clip_and_variant("WS205_clip-7.pers.mp4") == ("clip-7", 0)
    assert _clip_and_variant("clip-7.hook2.pers.mp4") == ("clip-7", 2)


def test_speaker_field_written(tmp_path):
    import json
    from sofit import publog
    log = tmp_path / "perf.jsonl"
    rc = publog.main(["clip-3", "--episode", "T1", "--platform", "tiktok",
                      "--url", "https://t.example/1", "--hook", "hook text",
                      "--speaker", "תור", "--log", str(log)])
    assert rc == 0
    row = json.loads(log.read_text().strip())
    assert row["speaker"] == "תור" and row["clip"] == "clip-3"


def test_full_episode_logs_without_a_clips_spec(tmp_path):
    import json
    from sofit import publog
    for platform in ("youtube", "spotify"):
        log = tmp_path / f"{platform}.jsonl"
        rc = publog.main(["episode", "--episode", "WS214", "--platform", platform,
                          "--url", f"https://example.test/{platform}",
                          "--log", str(log)])
        assert rc == 0
        row = json.loads(log.read_text())
        assert row["clip"] == "episode" and row["hook"] == "full episode"
        assert row["platform"] == platform and row["episode"] == "WS214"


def test_episode_filename_is_a_full_episode(tmp_path):
    import json
    from sofit import publog
    log = tmp_path / "ep.jsonl"
    rc = publog.main(["WS214_episode.mp4", "--episode", "WS214",
                      "--platform", "youtube", "--url", "https://youtu.be/abc",
                      "--hook", "כותרת הפרק", "--log", str(log)])
    assert rc == 0
    row = json.loads(log.read_text())
    assert row["clip"] == "WS214_episode" and row["hook"] == "כותרת הפרק"


def test_a_clip_still_needs_a_hook(tmp_path, capsys):
    from sofit import publog
    rc = publog.main(["clip-9", "--episode", "T1", "--platform", "youtube",
                      "--url", "https://youtu.be/needs-hook",
                      "--log", str(tmp_path / "x.jsonl")])
    assert rc == 1
    assert "hook" in capsys.readouterr().err
