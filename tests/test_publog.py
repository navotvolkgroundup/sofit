

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


def test_show_field_written_when_a_show_is_active(tmp_path, monkeypatch):
    import json
    from sofit import publog
    log = tmp_path / "perf.jsonl"
    args = ["clip-3", "--episode", "T1", "--platform", "tiktok", "--hook", "h",
            "--log", str(log)]
    monkeypatch.setenv("SOFIT_SHOW", "other-show")
    assert publog.main(args + ["--url", "https://t.example/1"]) == 0
    assert json.loads(log.read_text().strip())["show"] == "other-show"
