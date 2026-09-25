import json
from sofit import generate, model_backends as models
from sofit.footage_selection import ModelFrameJudge, ClaudeFrameJudge


def test_default_transports_and_versioned_cache_identity(monkeypatch):
    monkeypatch.delenv("SOFIT_TITLER_MODEL", raising=False)
    assert models.backend("api").transport is generate._call_api
    assert models.backend("claude-cli").transport is generate._call_claude_cli
    assert ModelFrameJudge is ClaudeFrameJudge
    assert (
        ModelFrameJudge("api").cache_key
        == "claude-frames-v1:api:semantic-v2:" + generate.CLAUDE_MODEL
    )
    assert (
        ModelFrameJudge("claude-cli").cache_key
        == "claude-frames-v1:claude-cli:semantic-v2:configured-cli-default"
    )


def test_source_judgment_adds_attribution_without_replacing_visual_evidence(
    monkeypatch, tmp_path
):
    from sofit.footage import Candidate, VisualIntent
    from sofit.footage_selection import Frame

    candidate = Candidate(
        "youtube",
        "https://www.youtube.com/watch?v=abcdefghijk",
        "https://example.org/v.mp4",
        "Manufacturing robot heads",
        30,
        320,
        240,
        creator="Maker",
        description="Publisher-supplied context",
    )

    def call(system, user, validate, **kwargs):
        data = json.loads(user)
        assert data["source_context"]["creator"] == "Maker"
        assert data["source_context"]["source_url"] == candidate.source_url
        assert "Metadata alone cannot establish identity or action" in system
        assert "For manufacturing intents" in system
        assert "actual screen recordings" in system
        assert "explicitly requested" in system
        assert "Never accept an unrelated" in system
        assert kwargs["images"] == [tmp_path / "frame.jpg"]
        return validate(
            {"frames": [{"index": 0, "scores": [0.2], "reason": "unrelated presenter"}]}
        )

    monkeypatch.setattr(generate, "call_claude_json", call)
    assert ModelFrameJudge().score_source(
        [Frame(1, tmp_path / "frame.jpg")],
        [VisualIntent("robot manufacturing", "Maker robots", 3)],
        candidate,
    ) == [[(0.2, "unrelated presenter")]]
