"""渠道适配器集合（P2-7）：把「既有 `web.session.Session`」接到 Web 之外的入口。

本包内的适配器一律**只做入口适配**——不复制业务逻辑、不直接触碰 `CoreBank`（H2），
并如实声明为**能力子集**（非等价实现，未接真实平台）。
"""

from __future__ import annotations
