# 冷库温度越限复盘 API

基于 **Python 3.11 + FastAPI + Uvicorn** 的温度越限复盘服务。输入同一冷库的一批**带时区**
温度读数、温度上限、最大采样间隔、最短报警时长与维护豁免时段，服务会：

1. 校验读数时间**严格递增**、所有时间带时区、豁免时段合法（结束严格晚于开始）；
2. 在相邻读数间隔不超过 `max_sample_gap_seconds` 时做**线性插值**，求出温度**严格超过**
   `temperature_limit` 的连续区间（恰好等于上限不算越限）；
3. 相邻读数间隔超过阈值时标记为**采样断档（未知区间）**，不做插值，越限区间**不会跨越断档**；
4. 从越限连续区间中**扣除维护豁免时段**（重叠部分不计入报警时长，重叠豁免自动合并）；
5. 扣减后各段持续时长 `>= min_alarm_seconds` 才作为报警输出。

## 接口地址与端口

| 项目 | 地址 |
| --- | --- |
| 健康检查 | `GET http://localhost:8000/health` |
| 复盘接口 | `POST http://localhost:8000/api/v1/cold-storage/alarms/review` |
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

## 判定规则细则

- 温度**严格**超过上限才越限；越限穿越点由线性插值精确计算（不必落在采样点上）。
- 相邻两段在公共采样点温度严格越限时合并为一个连续区间；公共点温度恰好等于上限则断开。
- 采样间隔 `== max_sample_gap_seconds` 仍允许插值；只有严格超过才是断档。
- 豁免时段与越限区间取交集扣减；豁免超出越限范围的部分自动裁剪，重叠豁免先合并。
- 最短时长判断含等号（持续时长恰好等于 `min_alarm_seconds` 也会报警）。

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py       # FastAPI 路由与启动入口
│   ├── review.py     # 插值 / 断档 / 豁免扣减 / 时长筛选核心算法
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
