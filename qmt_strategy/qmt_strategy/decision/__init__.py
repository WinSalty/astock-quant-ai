"""决策采集（复盘用、best-effort、与交易热路径物理隔离）。

子模块 decision_emitter 提供 DecisionEmitter：在各决策点 O(1) 非阻塞入队，独立后台线程批量
best-effort 回流到信号侧 qmt_decision_log。任何异常只吞不传播，队列满即丢——绝不阻塞/影响真实交易。
"""
