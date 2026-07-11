"""main.py 启动/CLI 编排的抽取目标包（2026-07-11 起分阶段拆分 God-file main.py）。

Stage 1: cli.py —— --check / --init 命令行入口。
后续阶段（见 docs-business/REFACTOR_BLUEPRINT_main.md）：
  - web_app.py   —— initialize() 内联的 FastAPI 应用工厂 + 自动发送/草稿闭包
  - subsystems.py—— _maybe_*/_ensure_*/_init_* 各可选子系统装配
  - lifecycle.py —— AIChatAssistant.initialize()/start()/stop() 编排骨架
"""
