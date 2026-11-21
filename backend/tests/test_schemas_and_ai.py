import json
from types import SimpleNamespace
from app import ai
from app.schemas import SceneItem, ScenePlan


def test_scene_plan_schema_validation_and_reindex():
    plan = ScenePlan(
        title="FastAPI Async Guide",
        topic="FastAPI Async",
        format="short",
        target_duration_seconds=45.0,
        scenes=[
            SceneItem(
                scene_index=5,
                narration="Aaj hum FastAPI async seekhenge.",
                visual_prompt="Minimalist dark code editor showing async Python",
                caption_text="FastAPI Async Seekho",
                scene_duration=15.0,
                source_notes="FastAPI official docs",
            ),
            SceneItem(
                scene_index=9,
                narration="BackgroundTasks se response fast hota hai.",
                visual_prompt="Server architecture diagram with glowing queues",
                caption_text="Fast Response with BackgroundTasks",
                scene_duration=30.0,
                source_notes="Original conceptual explanation",
            ),
        ],
    )
    assert [s.scene_index for s in plan.scenes] == [1, 2]
    assert plan.synthetic_disclosure is True
    assert "FastAPI async" in plan.full_narration


def test_generate_scene_plan_structured_output(monkeypatch):
    sample = {
        "title": "Python Decorators in Hinglish",
        "topic": "Python Decorators",
        "format": "short",
        "language": "Hinglish",
        "target_duration_seconds": 40.0,
        "synthetic_disclosure": True,
        "facts_to_verify": [],
        "scenes": [
            {
                "scene_index": 1,
                "narration": "Decorators Python mein function ko wrap karte hain.",
                "visual_prompt": "Abstract glowing layers wrapping a Python function node",
                "caption_text": "Python Decorators Kya Hain?",
                "scene_duration": 20.0,
                "source_notes": "Python 3 documentation",
                "needs_verification": False,
            },
            {
                "scene_index": 2,
                "narration": "Logging aur auth ke liye ye best pattern hai.",
                "visual_prompt": "Clean dark terminal with decorator syntax",
                "caption_text": "Logging & Auth Pattern",
                "scene_duration": 20.0,
                "source_notes": "Original conceptual explanation",
                "needs_verification": False,
            },
        ],
    }

    class FakeModels:
        def generate_content(self, model, contents, config=None):
            return SimpleNamespace(text=json.dumps(sample), parsed=None)

    class FakeClient:
        models = FakeModels()

    monkeypatch.setattr(ai, "_client", lambda: FakeClient())
    plan = ai.generate_scene_plan("Python Decorators", fmt="short")
    assert isinstance(plan, ScenePlan)
    assert len(plan.scenes) == 2
    assert plan.scenes[0].scene_index == 1
