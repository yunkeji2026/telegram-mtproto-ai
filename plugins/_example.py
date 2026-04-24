"""
示例插件 — 文件名以 _ 开头不会被自动加载。
要创建自己的插件：

1. 在 plugins/ 目录下创建 .py 文件（不以 _ 开头）
2. 定义一个继承 Skill 基类的类（命名为 PluginSkill 或任意名称）
3. 实现 async execute(self, text, user_id, context) 方法
4. 在 config.yaml 中设置 plugins.enabled: true

示例:
"""

# from src.skills.skill_manager import Skill
#
# class PluginSkill(Skill):
#     async def execute(self, text, user_id, context):
#         if "ping" in text.lower():
#             return "pong!"
#         return None
