import json
from pathlib import Path

import pytest

from sofit import generate, model_backends, render
from sofit.footage_progress import Progress, report_source_credit


@pytest.mark.parametrize("mode", [None, "text", "json"])
@pytest.mark.parametrize("audio_only", [False, True])
def test_saved_assets_report_every_span_before_render_and_on_rerender(
    monkeypatch, tmp_path, capsys, mode, audio_only
):
    asset = tmp_path / "asset.mp4"
    asset.write_bytes(b"cached-video")
    attribution = "Full attribution: " + "creator credit " * 100
    source = {
        "provider": "youtube",
        "source_url": "https://www.youtube.com/watch?v=abcdefghijk",
        "media_url": "https://media.example/file?token=private",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "creator": "Creator",
        "attribution": attribution,
        "attribution_required": True,
    }
    clip = {
        "id": "clip-1",
        "focus": 0.5,
        "segments": [{"start": 10, "end": 14}, {"start": 20, "end": 24}],
        "cutaways": [
            {
                "span": i,
                "start": 1,
                "end": 3,
                "video": str(asset),
                "source": source if i == 0 else {},
            }
            for i in range(2)
        ],
    }
    monkeypatch.setattr(render, "_is_audio_only", lambda _: audio_only)
    monkeypatch.setattr(render, "_extract_embedded_art", lambda *a: None)
    monkeypatch.setattr(render, "_prep_audiogram_assets", lambda *a: {"bg": "canvas"})
    monkeypatch.setattr(render, "_accent_from_art", lambda *a: None)
    monkeypatch.setattr(render, "_apply_brand_overlays", lambda *a: None)
    monkeypatch.setattr(
        render, "_concat_parts", lambda parts, out: out.write_bytes(b"rendered")
    )
    log = tmp_path / "progress.jsonl"
    seen = []

    def extract(**kwargs):
        output = capsys.readouterr().err
        if mode == "json":
            events = [json.loads(line) for line in output.splitlines()]
            credits = [
                event["credit"] for event in events if event["stage"] == "source_credit"
            ]
        else:
            credits = [
                json.loads(line.split(": ", 1)[1])
                for line in output.splitlines()
                if line.startswith("source credit before render")
            ]
        assert len(credits) == 1  # Already visible before any composition.
        credit = credits[0]
        assert credit["clip"] == "clip-1"
        assert (credit["start"], credit["end"]) == (1, 3)
        if credit["span"] == 0:
            for key in (
                "provider",
                "source_url",
                "license",
                "license_url",
                "creator",
                "attribution",
                "attribution_required",
            ):
                assert credit[key] == source[key]
        else:
            assert (
                credit["license"]
                == credit["creator"]
                == credit["attribution_required"]
                == "unknown"
            )
        assert "private" not in output
        seen.append(credit)
        kwargs["output_path"].write_bytes(b"rendered")

    monkeypatch.setattr(render, "extract_clip", extract)
    for attempt in range(2):
        progress = Progress(mode, log)
        with progress.active():
            render.render_clips(
                str(tmp_path / "episode"),
                [clip],
                str(tmp_path / "out"),
                hook_card=False,
            )
            progress.event("complete")
        capsys.readouterr()
    assert [credit["span"] for credit in seen] == [0, 1, 0, 1]
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert [
        event["credit"] for event in events if event["stage"] == "source_credit"
    ] == seen
    assert all(
        "credit" not in event for event in events if event["stage"] != "source_credit"
    )


def test_credit_without_progress_and_local_asset_stays_silent(capsys):
    report_source_credit("clip", 0, {"start": 0, "end": 2})
    assert capsys.readouterr().err == ""
    report_source_credit(
        "clip", 0, {"start": 0, "end": 2, "source": {"creator": "\x1b[31mName"}}
    )
    output = capsys.readouterr().err
    assert "\x1b" not in output
    assert "unknown" in output


@pytest.mark.parametrize(
    "backend,metric", [("api", "claude"), ("example-cli", "example")]
)
def test_transport_metrics_count_retries_and_failures(monkeypatch, backend, metric):
    attempts = iter(["invalid", "{}"])

    def transport(*args, **kwargs):
        return next(attempts)

    monkeypatch.setattr(model_backends, "_registered", {})
    if backend == "api":
        monkeypatch.setattr(generate, "_call_api", transport)
    else:
        model_backends.register_backend(backend, transport, cache_key="example-v1")
    progress = Progress()
    with progress.active():
        assert generate.call_claude_json("s", "u", lambda x: x, titler=backend) == {}
        with pytest.raises(StopIteration):
            generate.call_claude_json("s", "u", lambda x: x, titler=backend)
    assert progress.counts["model_calls"] == progress.counts[metric + "_calls"] == 3
    assert progress.seconds[metric] >= 0
    assert not any(progress.running.values())
