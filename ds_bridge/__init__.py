# -*- coding: utf-8 -*-
"""DeepSeek 网页版桥接层。

四个模块：
    plan.py     扫导出目录 / 排序 / 切批次（纯逻辑）
    host.py     拉起并"真嵌入"打包自带的 Electron（DeepSeek 官网页签宿主）
    sender.py   按批次自动投喂：挂附件 → 发送 → 截断思考 → 下一批
    (Electron 侧代码在仓库根的 dsview/ 目录)

这一层不 import gui/，方便单测。
"""

__all__ = ['plan', 'host', 'sender']
