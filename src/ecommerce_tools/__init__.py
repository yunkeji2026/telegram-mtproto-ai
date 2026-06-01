"""电商工具层（Phase D：电商业务 API）。

设计原则：
- 数据只能查不可编：工具返回结构化结果（found/not_found），查不到就明确标未知。
- 所有工具调用可审计（接 audit_store）。
- connector 可插拔（mock / shopify / 自有 ERP），核心不绑定具体平台。

注意：包名特意用 ecommerce_tools 而非 tools，避免与仓库顶层 tools/ 命名空间包
（脚本目录，含 qwen_tts_wrapper 等）冲突——测试会把 src/ 加进 sys.path，
顶层 tools 会被同名常规包遮蔽。
"""

from .models import OrderInfo, ShipmentInfo, ToolResult
from .connector import EcommerceConnector
from .mock_connector import MockEcommerceConnector
from .shopify_connector import ShopifyConnector
from .service import EcommerceToolService, build_connector

__all__ = [
    "OrderInfo",
    "ShipmentInfo",
    "ToolResult",
    "EcommerceConnector",
    "MockEcommerceConnector",
    "ShopifyConnector",
    "EcommerceToolService",
    "build_connector",
]
