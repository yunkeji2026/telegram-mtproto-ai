"""设置页：人工转接精简/完整视图控件存在（防回归）"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_HTML = ROOT / "src" / "web" / "templates" / "settings.html"


def test_human_escalation_view_mode_markup():
    text = SETTINGS_HTML.read_text(encoding="utf-8")
    assert 'id="body-he"' in text
    assert "he-view-simple" in text
    assert "he-view-controls" in text
    assert "heInitViewMode" in text
    assert "he_ui_view_mode" in text
    assert 'class="he-subcard he-advanced"' in text
    assert "he-inline-advanced" in text
    assert "he-minimal-path" in text
    assert "heApplyMinimalDefaults" in text
