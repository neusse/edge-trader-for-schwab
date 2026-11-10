import re
from pathlib import Path


def test_turning_voice_off_cancels_active_and_queued_speech():
    """The toggle must stop speech already in progress, not only block new alerts."""
    html = (Path(__file__).resolve().parents[1] / "companion" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r'\$\("voice"\)\.addEventListener\("click", e => \{(.*?)\n\}\);',
        html,
        re.S,
    )
    assert match, "voice click handler not found"
    assert "speechSynthesis.cancel()" in match.group(1)
