# 冷库温度越限复盘 API

基于 **Python 3.11 + FastAPI + Uvicorn** 的冷库温度复盘服务，提供两个接口：

**越限复盘**（`POST /api/v1/cold-storage/alarms/review`）：输入同一冷库的一批**带时区**
温度读数、温度上限、最大采样间隔、最短报警时长与维护豁免时段，服务会：

1. 校验读数时间**严格递增**、所有时间带时区、豁免时段合法（结束严格晚于开始）；
2. 在相邻读数间隔不超过 `max_sample_gap_seconds` 时做**线性插值**，求出温度**严格超过**
   `temperature_limit` 的连续区间（恰好等于上限不算越限）；
3. 相邻读数间隔超过阈值时标记为**采样断档（未知区间）**，不做插值，越限区间**不会跨越断档**；
4. 从越限连续区间中**扣除维护豁免时段**（重叠部分不计入报警时长，重叠豁免自动合并）；
5. 扣减后各段持续时长 `>= min_alarm_seconds` 才作为报警输出。

**热暴露评估**（`POST /api/v1/cold-storage/exposure`）：复用同一套读数校验与断档规则，
在货物存放窗口内对 `max(温度 - 上限, 0)` 做**精确积分**（℃·秒）并与热暴露预算比较，
详见下文[热暴露接口](#热暴露接口)。

## 接口地址与端口

| 项目 | 地址 |
| --- | --- |
| 健康检查 | `GET http://localhost:8000/health` |
| 复盘接口 | `POST http://localhost:8000/api/v1/cold-storage/alarms/review` |
| 热暴露接口 | `POST http://localhost:8000/api/v1/cold-storage/exposure` |
| Swagger 文档 | `http://localhost:8000/docs` |
| OpenAPI Schema | `http://localhost:8000/openapi.json` |

> 响应中的所有时间统一转换为 **UTC（`...Z`）**；`start` 为闭端点、`end` 为开端点。

## 单条启动命令

依赖安装与初始化已全部纳入启动脚本 `entrypoint.sh`（安装依赖 → 应用初始化检查 → 启动 uvicorn）。

### 方式一：Docker Compose（推荐）

```bash
docker compose up --build
```

镜像在构建期预装依赖，容器启动时 `entrypoint.sh` 自动做初始化检查并拉起服务。
重新执行该命令会复用缓存；端口已被占用时可换宿主机端口：

```bash
HOST_PORT=18000 docker compose up --build   # 服务变为 http://localhost:18000
```

### 方式二：Docker

```bash
docker build -t cold-storage-review-api:1.0.0 .
docker run --rm -p 8000:8000 cold-storage-review-api:1.0.0
```

### 方式三：本地直接运行

```bash
./entrypoint.sh
```

首次运行会自动创建 `.venv` 虚拟环境并安装 `requirements.txt`（需要系统 Python ≥ 3.11
且 `venv` 可用；依赖已就绪时后续启动自动跳过安装）。

## 必要配置

所有配置均有默认值，无额外依赖（不使用数据库）。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_HOST` | `0.0.0.0` | 监听地址 |
| `APP_PORT` | `8000` | 容器/进程监听端口 |
| `HOST_PORT` | `8000` | 仅 Compose 使用：宿主机映射端口 |

请求体参数：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `readings[].time` | ISO 8601 时间 | 必须带时区，整体**严格递增** | 读数时间 |
| `readings[].temperature` | number | 有限数值（拒绝 NaN/Infinity） | 温度（°C） |
| `temperature_limit` | number | 有限数值 | 温度上限，严格大于才算越限 |
| `max_sample_gap_seconds` | number | `> 0` | 最大相邻采样间隔（秒），超出即断档 |
| `min_alarm_seconds` | number | `>= 0` | 最短报警时长（秒），阈值含等号 |
| `maintenance_windows[]` | 时间段 | `end > start`，均带时区 | 维护豁免时段（可多条，允许重叠） |

校验失败返回 HTTP `422`（读数非递增、缺时区、无效时段等），并附带具体字段错误。

## 请求示例

```bash
curl -X POST http://localhost:8000/api/v1/cold-storage/alarms/review \
  -H "Content-Type: application/json" \
  -d '{
    "readings": [
      {"time": "2026-09-25T10:00:00+08:00", "temperature": -17.0},
      {"time": "2026-09-25T10:01:00+08:00", "temperature": -17.0},
      {"time": "2026-09-25T10:02:00+08:00", "temperature": -17.0},
      {"time": "2026-09-25T10:10:00+08:00", "temperature": -17.0},
      {"time": "2026-09-25T10:11:00+08:00", "temperature": -17.0}
    ],
    "temperature_limit": -18.0,
    "max_sample_gap_seconds": 120,
    "min_alarm_seconds": 60,
    "maintenance_windows": [
      {"start": "2026-09-25T10:00:30+08:00", "end": "2026-09-25T10:01:00+08:00"}
    ]
  }'
```

响应（时间已转 UTC；10:02→10:10 间隔 8 分钟超过 120 秒，标为未知且不跨越）：

```json
{
  "alarms": [
    {
      "start": "2026-09-25T02:01:00Z",
      "end": "2026-09-25T02:02:00Z",
      "duration_seconds": 60.0
    },
    {
      "start": "2026-09-25T02:10:00Z",
      "end": "2026-09-25T02:11:00Z",
      "duration_seconds": 60.0
    }
  ],
  "exceed_intervals": [
    {"start": "2026-09-25T02:00:00Z", "end": "2026-09-25T02:02:00Z", "duration_seconds": 120.0},
    {"start": "2026-09-25T02:10:00Z", "end": "2026-09-25T02:11:00Z", "duration_seconds": 60.0}
  ],
  "unknown_intervals": [
    {"start": "2026-09-25T02:02:00Z", "end": "2026-09-25T02:10:00Z", "duration_seconds": 480.0}
  ],
  "alarm_count": 2,
  "unknown_count": 1
}
```

- `alarms`：扣除豁免并满足最短时长的报警区间（本示例首段 120s 中有 30s 豁免，剩余
  `[0,30)`、`[60,120)` 两段，前者不足 60s 被过滤，后者保留）；
- `exceed_intervals`：扣减豁免前的原始连续越限区间，供复盘对照；
- `unknown_intervals`：采样断档区间。

## 热暴露接口

`POST /api/v1/cold-storage/exposure` 评估货物存放窗口内的**热暴露量**（温度越上限的
℃·秒 积分），并与预算比较：

1. 读数校验与断档规则与复盘接口完全一致（带时区、严格递增、间隔超限即断档）；
2. 存放窗口 `storage_window` 为半开区间 `[start, end)`，必须**非空且位于首末读数
   时间之间**，否则返回 `422`；
3. 仅在有效采样段内线性插值；段裁剪到窗口后**精确积分** `max(温度 - 上限, 0)`，
   跨段累计；
4. 采样断档不插值、不贡献已知量，但**不清零**已累计的已知量；落在窗口内的断档
   时长计为未知秒数；
5. 不扣减任何维护豁免（本接口没有豁免参数）。

请求体字段（`readings`、`temperature_limit`、`max_sample_gap_seconds` 与复盘接口相同）：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `readings[].time` | ISO 8601 时间 | 必须带时区，整体**严格递增** | 读数时间 |
| `readings[].temperature` | number | 有限数值（拒绝 NaN/Infinity） | 温度（°C） |
| `temperature_limit` | number | 有限数值 | 温度上限（°C） |
| `max_sample_gap_seconds` | number | `> 0` | 最大相邻采样间隔（秒），超出即断档 |
| `storage_window` | 时间段 | `end > start`，带时区，且位于首末读数之间 | 货物存放窗口 `[start, end)` |
| `exposure_budget_celsius_seconds` | number | `> 0` | 正数热暴露预算（℃·秒） |

响应字段：

| 字段 | 说明 |
| --- | --- |
| `status` | `exceeded`=已知量达到预算；`indeterminate`=未达到且窗口内有未知时长；`within`=未达到且窗口内无未知时长 |
| `known_exposure_celsius_seconds` | 窗口内已知热暴露量（℃·秒），仅统计有效采样段 |
| `unknown_seconds_in_window` | 窗口内采样断档（未知）秒数 |
| `budget_reached_at` | 已知累计量首次达到预算的 UTC 时刻（在段内精确求解，不取下一个采样点）；未达到为 `null` |

### 热暴露请求示例

```bash
curl -X POST http://localhost:8000/api/v1/cold-storage/exposure \
  -H "Content-Type: application/json" \
  -d '{
    "readings": [
      {"time": "2026-09-25T10:00:00+08:00", "temperature": -16.0},
      {"time": "2026-09-25T10:01:00+08:00", "temperature": -16.0},
      {"time": "2026-09-25T10:06:00+08:00", "temperature": -16.0},
      {"time": "2026-09-25T10:07:00+08:00", "temperature": -16.0}
    ],
    "temperature_limit": -18.0,
    "max_sample_gap_seconds": 120,
    "storage_window": {"start": "2026-09-25T10:00:00+08:00", "end": "2026-09-25T10:07:00+08:00"},
    "exposure_budget_celsius_seconds": 150
  }'
```

响应（10:01→10:06 间隔 5 分钟超过 120 秒，计为未知；已知量跨断档累计 240 ℃·秒，
在第 2 个有效段的 10:06:15 处达到预算 150）：

```json
{
  "status": "exceeded",
  "known_exposure_celsius_seconds": 240.0,
  "unknown_seconds_in_window": 300.0,
  "budget_reached_at": "2026-09-25T02:06:15Z"
}
```

## 判定规则细则

- 温度**严格**超过上限才越限；越限穿越点由线性插值精确计算（不必落在采样点上）。
- 相邻两段在公共采样点温度严格越限时合并为一个连续区间；公共点温度恰好等于上限则断开。
- 采样间隔 `== max_sample_gap_seconds` 仍允许插值；只有严格超过才是断档。
- 豁免时段与越限区间取交集扣减；豁免超出越限范围的部分自动裁剪，重叠豁免先合并。
- 最短时长判断含等号（持续时长恰好等于 `min_alarm_seconds` 也会报警）。
- 热暴露积分把每个有效段按"温度=上限"的插值折点切分后精确求积（分段线性 + 梯形/
  二次求根），预算达成时刻在段内解析求解，不会进位到下一个采样点。

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py       # FastAPI 路由与启动入口
│   ├── review.py     # 插值 / 断档 / 豁免扣减 / 时长筛选 / 热暴露积分核心算法
│   └── schemas.py    # 请求与响应模型（Pydantic 校验）
├── tests/            # 算法层与 API 层测试（pytest）
├── entrypoint.sh     # 单条启动命令：装依赖 + 初始化 + 起服务
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt
```

## 本地运行测试

```bash
pip install -r requirements-dev.txt
PYTHONPATH=. pytest -q
```
