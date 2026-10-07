# 深水油田协同运营平台

本项目是一套可离线运行的 Python 服务端平台，用于管理深水油田生产物流、油藏证据评估和关键装备质量。平台把生产节点、输送通道、原油批次、油藏方案、分析决定、装备观测、权限和审计事件持久化到 SQLite，供海上平台、浮式生产储卸装置、油藏团队、装备保障和审计人员协作使用。

## 目录

- src/production_flow/：生产节点、输送通道、原油批次、外输申请、分配和情景分析；
- src/reservoir_assurance/：油藏项目、证据版本、评估协议、观测导入、分析任务与准入决定；
- src/equipment_quality/：装备批次、传感观测、质量分析、账号权限和审批；
- src/equipment_qualification/：装备资格与适用边界——设计版本、部件批次、试验协议、证据链、环境包络、偏差处置、作业放行门禁与任意时间解释；
- fixtures/：离线验收使用的评估协议与结构化观测；
- tests/：领域规则、错误边界、事务、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m production_flow.acceptance --workspace .
    PYTHONPATH=src python3 -m reservoir_assurance.acceptance --workspace .
    PYTHONPATH=src python3 -m equipment_quality.acceptance
    PYTHONPATH=src python3 -m equipment_qualification.acceptance --workspace .

四条命令会在临时 SQLite 数据库中完成生产流转、油藏证据评估、装备质量和装备资格与适用边界流程，不访问外部网络。

## HTTP 服务

    PYTHONPATH=src python3 -m production_flow.api --database production-flow.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m reservoir_assurance.api --database reservoir-assurance.sqlite3 --host 127.0.0.1 --port 8081
    PYTHONPATH=src python3 -m equipment_quality.api --database equipment-quality.sqlite3 --host 127.0.0.1 --port 8082
    PYTHONPATH=src python3 -m equipment_qualification.api --database equipment-qualification.sqlite3 --host 127.0.0.1 --port 8083

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

## 装备资格与适用边界

src/equipment_qualification/ 回答“某一设计版本的装备为何被允许在特定井况下使用”，把
设计版本、部件批次、试验协议、校准证据、环境包络、软件版本、偏差处置和批准范围串成
可追溯链：

- **按能力部分通过**：资格以「设计版本 × 能力」授予，携带水深/温度/压力/作业阶段
  包络；试验报告、校准证据、软件版本认证、设计符合性四类证据缺项时标记为
  `partially_qualified` 并列出缺口，只有有效 accepted 偏差才能临时放行。
- **证据内容寻址**：证据按规范化 JSON 的 SHA-256 去重，相同内容重放直接返回原证据，
  不重复写批准链；放行必须携带 `Idempotency-Key`，同一放行键只能成功签发一次，
  重放返回原放行（即使放行已作废也不会重新发放）。
- **撤回与召回只影响未执行作业**：证据撤回或部件召回把 `released` 状态的放行置为
  `voided`；已 `consumed` 的作业与其依据快照原样保留。
- **任意时间解释**：`GET /designs/{id}/status?at=...` 按给定时间点（结合
  `depth_m/temperature_c/pressure_mpa/phase/equipment_serial`）返回各能力当时状态
  （qualified / partially_qualified / evidence_withdrawn / component_recalled /
  withdrawn）、适用边界、证据缺口、偏差和作业放行的当时状态，未来的撤回/召回不影响
  历史解释。

