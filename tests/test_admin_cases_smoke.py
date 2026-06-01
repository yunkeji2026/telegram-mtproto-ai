"""冒烟测试：运营 case 管理端点（cases_routes 抽出后，可调不崩）。

telegram_client=None → 无 context_store → active 返回空、note/close 返回 404，
均不得 500（raise_server_exceptions 会让任何异常使测试失败）。
"""

from __future__ import annotations


def test_cases_active_empty_without_ctx_store(auth_client):
    r = auth_client.get("/api/cases/active")
    assert r.status_code == 200
    body = r.json()
    assert body == {"cases": [], "count": 0}


def test_case_note_404_without_ctx_store(auth_client):
    r = auth_client.post("/api/cases/c-123/note", json={"note": "x"})
    assert r.status_code != 500
    assert r.status_code == 404


def test_case_close_404_without_ctx_store(auth_client):
    r = auth_client.post("/api/cases/c-123/close", json={"resolution": "done"})
    assert r.status_code != 500
    assert r.status_code == 404
