"""Watermark settings live in src/watermark/settings.py, not in src/config.py (WM05 C1).

Production releases "code only": src/config.py must stay byte-identical to the production
revision, so the watermark switches are read by their own pydantic class from the same .env.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.config import PROJECT_ROOT, Settings

REPO = Path(__file__).resolve().parents[2]

# name -> default, copied from the R04 block of src/config.py (same env names, same defaults).
EXPECTED_DEFAULTS = {
    "watermark_bot_enabled": False,
    "watermark_allowed_chat_ids": "",
    "watermark_clean_dir": PROJECT_ROOT / "data" / "clean",
    "watermark_lama_enabled": True,
    "watermark_lama_model_path": PROJECT_ROOT / "data" / "models" / "big-lama.pt",
    "watermark_halo_corner_mode": "skip",
    "watermark_clean_on_delivery": False,
    "watermark_site_use_clean": False,
    "watermark_jpeg_quality": 95,
}


@pytest.fixture(autouse=True)
def _clean_watermark_env(monkeypatch):
    """The operator shell may export WATERMARK_* (for example the model path): defaults only."""
    import os

    for key in [k for k in os.environ if k.upper().startswith("WATERMARK_")]:
        monkeypatch.delenv(key)


def _env_names_in_example() -> set[str]:
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    block = text.split("Watermark remover", 1)[1]
    return set(re.findall(r"^#?\s*(WATERMARK_[A-Z0-9_]+)=", block, flags=re.MULTILINE))


def _fresh():
    from src.watermark.settings import WatermarkSettings

    return WatermarkSettings(_env_file=None)


def test_defaults_match_the_r04_block():
    settings = _fresh()
    for name, expected in EXPECTED_DEFAULTS.items():
        assert getattr(settings, name) == expected, name


def test_every_env_name_of_the_example_block_is_read():
    from src.watermark.settings import WatermarkSettings

    names = _env_names_in_example()
    assert names, "the Watermark block of .env.example is empty"
    fields = {name.upper() for name in WatermarkSettings.model_fields}
    assert names <= fields, sorted(names - fields)


def test_every_field_is_documented_in_the_example_block():
    from src.watermark.settings import WatermarkSettings

    documented = {name.lower() for name in _env_names_in_example()}
    assert set(WatermarkSettings.model_fields) <= documented


def test_settings_class_has_no_watermark_fields():
    assert [name for name in Settings.model_fields if name.startswith("watermark_")] == []


@pytest.mark.parametrize(
    ("env", "field", "raw", "expected"),
    [
        ("WATERMARK_BOT_ENABLED", "watermark_bot_enabled", "1", True),
        ("WATERMARK_LAMA_ENABLED", "watermark_lama_enabled", "0", False),
        ("WATERMARK_HALO_CORNER_MODE", "watermark_halo_corner_mode", "lama", "lama"),
        ("WATERMARK_JPEG_QUALITY", "watermark_jpeg_quality", "88", 88),
        ("WATERMARK_ALLOWED_CHAT_IDS", "watermark_allowed_chat_ids", "1,2", "1,2"),
        ("WATERMARK_SITE_USE_CLEAN", "watermark_site_use_clean", "true", True),
        ("WATERMARK_CLEAN_ON_DELIVERY", "watermark_clean_on_delivery", "yes", True),
    ],
)
def test_values_are_read_from_the_environment(monkeypatch, env, field, raw, expected):
    monkeypatch.setenv(env, raw)
    assert getattr(_fresh(), field) == expected


def test_paths_are_read_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("WATERMARK_CLEAN_DIR", str(tmp_path / "clean"))
    monkeypatch.setenv("WATERMARK_LAMA_MODEL_PATH", str(tmp_path / "m.pt"))
    settings = _fresh()
    assert settings.watermark_clean_dir == tmp_path / "clean"
    assert settings.watermark_lama_model_path == tmp_path / "m.pt"


def test_invalid_values_are_rejected(monkeypatch):
    monkeypatch.setenv("WATERMARK_HALO_CORNER_MODE", "bogus")
    with pytest.raises(ValueError):
        _fresh()


def test_jpeg_quality_is_bounded(monkeypatch):
    monkeypatch.setenv("WATERMARK_JPEG_QUALITY", "10")
    with pytest.raises(ValueError):
        _fresh()


def test_reads_the_same_dotenv_and_ignores_foreign_keys():
    from src.watermark.settings import WatermarkSettings

    assert Path(WatermarkSettings.model_config["env_file"]) == PROJECT_ROOT / ".env"
    assert WatermarkSettings.model_config["extra"] == "ignore"
    assert WatermarkSettings.model_config["case_sensitive"] is False


def test_dotenv_file_values_are_used(tmp_path):
    from src.watermark.settings import WatermarkSettings

    env = tmp_path / ".env"
    env.write_text("WATERMARK_BOT_ENABLED=1\nTG_BOT_TOKEN=foreign\n", encoding="utf-8")
    assert WatermarkSettings(_env_file=env).watermark_bot_enabled is True


def test_getter_is_cached_and_resettable():
    from src.watermark.settings import get_watermark_settings

    get_watermark_settings.cache_clear()
    first = get_watermark_settings()
    assert get_watermark_settings() is first
    get_watermark_settings.cache_clear()
    assert get_watermark_settings() is not first


def test_lama_inpainter_exists_when_the_model_file_is_present(monkeypatch, tmp_path):
    """Regression for the R04 `getattr(settings, ..., default)` trap: a missing field made the
    inpainter silently unavailable. The getter must see the real settings object."""
    import src.watermark.lama as lama
    from src.watermark.settings import get_watermark_settings

    model = tmp_path / "big-lama.pt"
    model.write_bytes(b"fake model")
    settings = get_watermark_settings()
    monkeypatch.setattr(settings, "watermark_lama_enabled", True)
    monkeypatch.setattr(settings, "watermark_lama_model_path", model)
    monkeypatch.setattr(lama, "torch_available", lambda: True)
    monkeypatch.setattr(lama, "_instance", None)
    inpainter = lama.get_lama_inpainter()
    assert inpainter is not None
    assert inpainter.model_path == model


def test_lama_inpainter_loads_the_model_through_torch_jit(monkeypatch, tmp_path):
    torch = pytest.importorskip("torch")
    import src.watermark.lama as lama
    from src.watermark.settings import get_watermark_settings

    model = tmp_path / "big-lama.pt"
    model.write_bytes(b"fake model")
    loaded: list[str] = []

    class FakeModel:
        def eval(self):
            return self

    def fake_load(path, map_location=None):
        loaded.append(path)
        return FakeModel()

    settings = get_watermark_settings()
    monkeypatch.setattr(settings, "watermark_lama_enabled", True)
    monkeypatch.setattr(settings, "watermark_lama_model_path", model)
    monkeypatch.setattr(settings, "watermark_lama_isolation", "inline")  # model in this process
    monkeypatch.setattr(torch.jit, "load", fake_load)
    monkeypatch.setattr(lama, "_instance", None)
    inpainter = lama.get_lama_inpainter()
    assert inpainter is not None
    assert isinstance(inpainter._load(), FakeModel)
    assert loaded == [str(model)]


def test_lama_disabled_flag_gives_no_inpainter(monkeypatch, tmp_path):
    import src.watermark.lama as lama
    from src.watermark.settings import get_watermark_settings

    model = tmp_path / "big-lama.pt"
    model.write_bytes(b"x")
    settings = get_watermark_settings()
    monkeypatch.setattr(settings, "watermark_lama_enabled", False)
    monkeypatch.setattr(settings, "watermark_lama_model_path", model)
    assert lama.get_lama_inpainter() is None
