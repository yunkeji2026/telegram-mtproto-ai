"""I3: 跨境电商客服回复模板预置数据（中/英/日三语，按场景分组）。

由 main.py 在 InboxStore 首次初始化时调用 store.seed_templates(SEED_TEMPLATES)。
id 固定，幂等：重启不重复插入。
"""

SEED_TEMPLATES = [
    # ── 开场白 greeting ──────────────────────────────
    {
        "id": "tpl-zh-greeting-01",
        "title": "中文问候",
        "content": "您好！感谢您的联系，很高兴为您服务。请问有什么我可以帮助您的？",
        "language": "zh",
        "scene": "greeting",
    },
    {
        "id": "tpl-en-greeting-01",
        "title": "English Greeting",
        "content": "Hello! Thank you for contacting us. How can I assist you today?",
        "language": "en",
        "scene": "greeting",
    },
    {
        "id": "tpl-ja-greeting-01",
        "title": "日本語挨拶",
        "content": "こんにちは！お問い合わせいただきありがとうございます。どのようにお手伝いできますか？",
        "language": "ja",
        "scene": "greeting",
    },

    # ── 订单查询 order_inquiry ────────────────────────
    {
        "id": "tpl-zh-order-01",
        "title": "订单确认（中）",
        "content": "您的订单已成功下单，订单号为【{订单号}】，预计 3-7 个工作日内发货。如有疑问请随时告知，谢谢！",
        "language": "zh",
        "scene": "order_inquiry",
    },
    {
        "id": "tpl-en-order-01",
        "title": "Order Confirmation (EN)",
        "content": "Your order #{ORDER_NO} has been successfully placed. It will be shipped within 3–7 business days. Please feel free to reach us if you have any questions!",
        "language": "en",
        "scene": "order_inquiry",
    },
    {
        "id": "tpl-ja-order-01",
        "title": "注文確認（日）",
        "content": "ご注文（注文番号：{注文番号}）を受け付けました。3〜7営業日以内に発送予定です。ご不明な点がございましたらお知らせください。",
        "language": "ja",
        "scene": "order_inquiry",
    },

    # ── 物流查询 shipping ─────────────────────────────
    {
        "id": "tpl-zh-shipping-01",
        "title": "物流状态（中）",
        "content": "您的包裹已通过{快递公司}发出，追踪单号为{追踪号}，您可以通过官网实时查询物流状态。通常 7-15 个工作日可到达，请耐心等待。",
        "language": "zh",
        "scene": "shipping",
    },
    {
        "id": "tpl-en-shipping-01",
        "title": "Shipping Status (EN)",
        "content": "Your parcel has been dispatched via {carrier}. Tracking number: {TRACKING_NO}. Estimated delivery: 7–15 business days. You can track your shipment on the carrier's website.",
        "language": "en",
        "scene": "shipping",
    },
    {
        "id": "tpl-ja-shipping-01",
        "title": "配送状況（日）",
        "content": "お荷物は{運送会社}にて発送済みです。追跡番号：{追跡番号}。通常7〜15営業日でお届け予定です。",
        "language": "ja",
        "scene": "shipping",
    },
    {
        "id": "tpl-zh-shipping-delay",
        "title": "物流延误说明（中）",
        "content": "非常抱歉！由于{原因}，您的包裹可能出现延误，预计延误 3-5 天。给您带来不便深感歉意，我们会持续为您跟进。感谢您的耐心与理解！",
        "language": "zh",
        "scene": "shipping",
    },

    # ── 退款 refund ───────────────────────────────────
    {
        "id": "tpl-zh-refund-01",
        "title": "退款受理（中）",
        "content": "您的退款申请已受理，退款金额 {金额} 将在 3-5 个工作日内原路退回至您的支付账户。如超时未到账，请再次联系我们，非常感谢您的支持！",
        "language": "zh",
        "scene": "refund",
    },
    {
        "id": "tpl-en-refund-01",
        "title": "Refund Accepted (EN)",
        "content": "Your refund request has been approved. A refund of {AMOUNT} will be credited back to your original payment method within 3–5 business days. Thank you for your patience!",
        "language": "en",
        "scene": "refund",
    },
    {
        "id": "tpl-zh-refund-review",
        "title": "退款审核中（中）",
        "content": "您的退款申请正在审核中，我们将在 1-2 个工作日内给您答复。如有紧急情况，请告知我们，谢谢！",
        "language": "zh",
        "scene": "refund",
    },

    # ── 产品咨询 product_info ─────────────────────────
    {
        "id": "tpl-zh-product-01",
        "title": "产品详情回复（中）",
        "content": "感谢您对我们产品的关注！关于您询问的{产品名称}：{产品详情}。如需更多信息或想要下单，请随时联系我们！",
        "language": "zh",
        "scene": "product_info",
    },
    {
        "id": "tpl-en-product-01",
        "title": "Product Information (EN)",
        "content": "Thank you for your interest in our products! Regarding {PRODUCT_NAME}: {PRODUCT_DETAILS}. Please feel free to contact us for more information or to place an order!",
        "language": "en",
        "scene": "product_info",
    },

    # ── 投诉处理 complaint ────────────────────────────
    {
        "id": "tpl-zh-complaint-01",
        "title": "投诉致歉（中）",
        "content": "非常抱歉给您带来了不好的体验！我们已将您的问题记录，并会优先处理。请您放心，我们会尽快为您解决，再次致以真诚的歉意。",
        "language": "zh",
        "scene": "complaint",
    },
    {
        "id": "tpl-en-complaint-01",
        "title": "Complaint Apology (EN)",
        "content": "We sincerely apologize for the inconvenience you've experienced! Your feedback has been noted and will be prioritized. We will resolve this issue as soon as possible. Thank you for your understanding.",
        "language": "en",
        "scene": "complaint",
    },

    # ── 结束语 closing ────────────────────────────────
    {
        "id": "tpl-zh-closing-01",
        "title": "结束语（中）",
        "content": "感谢您的耐心等待！如果还有任何问题，随时欢迎联系我们，祝您生活愉快！",
        "language": "zh",
        "scene": "closing",
    },
    {
        "id": "tpl-en-closing-01",
        "title": "Closing (EN)",
        "content": "Thank you for your patience! Please don't hesitate to reach out if you have any further questions. Have a wonderful day!",
        "language": "en",
        "scene": "closing",
    },
    {
        "id": "tpl-ja-closing-01",
        "title": "締めくくり（日）",
        "content": "お待たせして申し訳ございません。他にご質問がございましたら、いつでもお問い合わせください。素晴らしい一日をお過ごしください！",
        "language": "ja",
        "scene": "closing",
    },
]
