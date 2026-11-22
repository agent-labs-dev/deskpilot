import pytest
from test_core import make_desk

from deskpilot import batch


def test_an_empty_paste_rejects_the_batch_before_earlier_input(tmp_path, monkeypatch):
    desk, fake, _, plugin = make_desk(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="invalid batch"):
        desk.batch(
            batch.parse(
                {
                    "window": "notepad",
                    "actions": [
                        {"action": "key", "combo": "Return"},
                        {"action": "paste", "text": ""},
                    ],
                }
            )
        )
    assert fake.calls == []
    assert plugin.calls == []
