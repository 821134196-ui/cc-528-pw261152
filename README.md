# 设备健康分析 (Device Health Analysis)

接入传感器时序、维修工单与停机记录，产出**绑定数据版本与规则版本**的健康分、告警与剩余寿命（RUL）估计。补齐历史点位后只重算受影响的时间窗，支持跨设备（资产）合并视图与迟到数据处理。

## 功能总览

| 能力 | 实现 |
|---|---|
| 数据接入 | `POST /api/ingest` 批量点位（含每点 `reported_at`），工单/停机记录入库 |
| 缺测处理 | 内部缺口 ≤ `gap_fill_max` 个槽位线性插补（`gap_filled`），更长缺口保留为覆盖率损失 |
| 重复采样 | `dedup_tolerance_s` 内链式分组，取中位值、保留最早时间戳（`dup_resolved`） |
| 时间漂移 | 基线延迟 = 实时批次 `MIN(reported_at - ts)`；实时点位偏移 ≥ `drift_min_s` 时校正（`drift_corrected`）；回填点永不校正 |
| 设备换型 | `device_model_events` 切分时间段，基线只在段内计算（换型后基线重置） |
| 版本绑定 | 每个健康快照/告警/RUL 都携带 `(data_version, rule_version)` 与 `superseded_by` 链，支持 as-of 版本视图与版本差异对比 |
| 增量重算 | 点位影响自身窗口 + 尾随基线覆盖该点的窗口，上限为最新数据所在窗口；每个重算窗口记入 `recompute_log` |
| 跨设备合并 | `asset_links` 有效区间按时刻选取有效设备，合并序列/健康无重复计数 |
| 告警处置 | open/ack/resolved + fixed/false_positive/monitoring/parts_ordered，重算时按告警身份（设备+规则+指标+窗口）继承处置状态 |
| RUL | 尾随 168 窗分箱（bin_w = K//12）最小二乘拟合，斜率门限 + fail_score 外推 |

## 目录结构

```
app/
  db.py        # SQLite schema + 数据版本时钟
  rules.py     # 规则配置与规则版本
  quality.py   # 漂移校正 / 去重 / 缺口插补（reclean_range）
  health.py    # 窗口健康分（段内基线、停机槽位剔除、覆盖率门限）
  alerts.py    # 告警评估与处置继承
  rul.py       # 剩余寿命估计
  pipeline.py  # 接入、受影响窗口计算、增量重算、规则变更
  merge.py     # 资产级跨设备合并
  api.py       # FastAPI 端点
  seed.py      # 确定性演示数据（40 天全场景）
web/           # 静态前端（无 JS 依赖，内联 SVG 图表）
tests/         # pytest（17 项）
```

## 运行

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# 生成演示数据（40 天，含全部异常场景）
.venv/bin/python -m app.seed --db dha.db --fresh

# 启动服务（前端 + API 同端口）
DHA_DB=dha.db .venv/bin/python -m uvicorn app.api:app --host 127.0.0.1 --port 8765
# 打开 http://127.0.0.1:8765/
```

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

覆盖：去重/插补/漂移校正/换型基线重置（test_quality）、受影响窗口计算与迟到数据局部重算（test_incremental）、跨设备合并有效区间（test_merge）、版本绑定与规则变更 diff、处置状态跨重算继承（test_versioning）、RUL 趋势/稳定/历史不足（test_rul）、API 全流程（test_api）。

## 演示数据场景（seed.py，random.Random(42) 确定性）

- **P-101 → P-101R**（资产 PUMP-A）：第 20 天换机，P-101R 第 30 天起轴承故障（振动发作频次/时长/幅值递增，温度每天 +2℃ 蠕变）→ 告警 + RUL ≈ 258h
- **P-102**：第 10 天起时钟漂移 +2s/h、3% 重复发送、5 处短缺口（插补）+ 第 15 天 2h 长缺口（迟到回填）
- **C-201**：第 18 天换型 CX-5→CX-7（基线重置），第 25 天 3h 温度越限
- **迟到数据**：批次 33（P-102 第 15 天 10–12 时，26 窗重算）、批次 34（P-101 第 5 天 06–07 时，25 窗重算），`reason=late_data`，未受影响窗口的 `data_version` 不变
- **规则变更**：v1→v2（vibration crit_z 3.5→2.6，alert_score_warn 80→85），全量窗口按新规则重算，`data_version` 不变，`rule_version` 递增

## 验证跨设备合并与迟到数据

1. **跨设备合并**：前端「设备时间线」选择 `资产: PUMP-A`，健康曲线与点位序列在第 20 天切换点处由 P-101 无缝接到 P-101R，交接重叠期（±2h）按 `asset_links` 有效区间归属，不重复计数。对应测试 `tests/test_merge.py`。
2. **迟到数据**：「数据批次」页点击批次 33/34，重算条带只点亮受影响窗口；「版本差异」对比 v32→v33（P-102）/ v32→v34（P-101）仅列出变化窗口。对应测试 `tests/test_incremental.py`。

## 主要 API

| 端点 | 说明 |
|---|---|
| `GET /api/overview` | 设备卡片：最新健康分、覆盖率、未结告警、RUL |
| `GET /api/devices/{id}/timeline` | 健康快照时间线 + 告警 + 工单 + 停机 + 换型事件 |
| `GET /api/devices/{id}/points?metric=&from=&to=` | 清洗后点位（含 quality 标记） |
| `GET /api/devices/{id}/evidence?window_start=&metric=` | 异常证据：窗口点位、基线、违规点 |
| `GET /api/alerts` / `POST /api/alerts/{id}/disposition` | 告警查询与处置 |
| `GET /api/assets/{id}/merged` | 资产合并视图 |
| `GET /api/versions` / `GET /api/version-diff` | 版本列表与 as-of 差异对比 |
| `GET /api/batches` / `POST /api/batches/{id}/recompute` | 批次与重算记录 |
| `POST /api/ingest` / `POST /api/rules` | 数据接入与规则变更 |
