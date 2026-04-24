# ── AI智能客服系统 — 开发便捷命令 ─────────────────────────
.PHONY: test test-web test-all lint format install-dev help

## 快速运行 Web Admin 测试（推荐日常使用）
test:
	python -m pytest \
		tests/test_web_auth.py \
		tests/test_web_templates.py \
		tests/test_web_users.py \
		tests/test_web_diff_rollback.py \
		tests/test_web_api.py \
		tests/test_web_alert.py \
		-v --tb=short -p no:warnings

## 运行全部测试（含 audit_store / config_manager）
test-all:
	python -m pytest tests/ -v --tb=short -p no:warnings

## 只跑 Web Admin 测试（精简输出）
test-web:
	python -m pytest tests/test_web_*.py -q --tb=short -p no:warnings

## 代码风格检查
lint:
	ruff check src/web/ src/utils/ --select E,F --ignore E501,E402,F401,F811

## 安装开发依赖（pytest + ruff + pre-commit）
install-dev:
	pip install pytest pytest-anyio httpx ruff pre-commit
	pre-commit install --hook-type pre-push

## 帮助
help:
	@echo "make test       — 运行 Web Admin 测试"
	@echo "make test-all   — 运行全部测试"
	@echo "make lint       — 代码检查"
	@echo "make install-dev— 安装开发依赖"
