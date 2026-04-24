"""设置页：人工转接「快速场景」脚本存在（防回归）"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_HTML = ROOT / "src" / "web" / "templates" / "settings.html"


def test_human_escalation_scenario_wizard_markup():
    text = SETTINGS_HTML.read_text(encoding="utf-8")
    assert "he-scenario-panel" in text
    assert "heApplyScenario" in text
    assert "_heWorkHoursLooksNonEmpty" in text
    assert "schedule_or_manual" in text
    assert "仅按周排班" in text
