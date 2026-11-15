import pytest

from app.agents.utterance import normalize


@pytest.mark.parametrize("raw,clean", [
    ("hey nova schedule my meeting for tommorow", "schedule my meeting for tomorrow"),
    ("Hey NOVA, schedule my meeting for tomorrow", "Schedule my meeting for tomorrow"),
    ("ok nova remind me tmrw", "remind me tomorrow"),
    ("Nova: shedule a mtg 2morrow 11 baje", "schedule a meeting tomorrow at 11"),
    ("meeting at 4 baje shaam", "meeting at 4 evening"),
    ("Hey NOVA", "Hey NOVA"),  # a bare greeting stays a greeting
    ("Novak Djokovic news", "Novak Djokovic news"),  # not the wake word
])
def test_normalize(raw, clean):
    assert normalize(raw) == clean
