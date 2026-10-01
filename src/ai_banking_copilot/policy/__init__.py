"""可信执行内核（PDP / PEP）：三色裁决 · 执行门 · 审计链 · 熔断 · saga 撤销。

分层纪律：本包**不 import 大模型客户端**、**不产事实**；一切数值来自 `gateway`，
一切裁决来自 `contracts.authorization`。
"""
