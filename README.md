# 联程航班 UTC 时间线处理系统

航空时刻数据员录入短程联程时刻表及其更正版，系统用 Python `zoneinfo` 处理
IANA 时区与夏令时，裁决出**唯一可行**的完整 UTC 时间线后发布，支持页面查看与
CSV 下载。基于 Flask + SQLite，Docker Compose 一键启动。

## 快速开始

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

数据持久化在命名卷 `schedule_data`（容器内 `/data/schedule.db`）。

本地无 Docker 时：

```bash
python3 -m pip install -r requirements.txt
python3 -m flask --app wsgi:app run --port 8000
# 生产启动：gunicorn --bind 0.0.0.0:8000 --workers 4 wsgi:app
```

## 输入 CSV 格式

每份文件含 **2～5 个航段**，必需列（中英文表头均可，顺序不限）：

| 列名 | 含义 | 示例 |
| --- | --- | --- |
| `seq` | 航段序号，从 1 连续递增 | `1` |
| `origin` / `destination` | 出发/到达机场代码（3~4 位大写字母数字） | `JFK` |
| `origin_tz` / `destination_tz` | 两地 IANA 时区 | `America/New_York` |
| `depart_local` / `arrive_local` | 当地时间 `YYYY-MM-DD HH:MM` | `2024-11-03 01:10` |
| `flight_minutes` | 计划飞行分钟（正整数） | `140` |

首页可下载 5 个示例文件，覆盖：歧义但唯一可行、两种解释、不存在的当地时刻、
三段有效行程、约束违规。

## 裁决规则

### 1. 夏令时（zoneinfo）

- **春季跳变**：时钟向前拨，落在跳变区间的当地时刻**不存在**，该解释无效，
  整条时间线计为零种可行解释并说明原因。
- **秋季回退**：时钟向后拨，回退窗口内的当地时刻有两种 UTC 解释
  （`fold=0` 夏令时 / `fold=1` 标准时），系统枚举全部端点的笛卡尔积，
  再按业务约束过滤、按完整 UTC 时间戳去重。

### 2. 完整时间线必须同时满足

- 每段**实际飞行分钟 = 计划飞行分钟**（按 UTC 差值计算）；
- 相邻航段**机场首尾相接**（前段到达 == 后段出发）；
- **转机间隔 30～180 分钟**（含边界，按 UTC 差值计算）。

### 3. 解释数量决定发布与否

- **恰好一种**可行解释 → 发布，页面展示并可下载各段 UTC 起降时刻与转机间隔；
- **零种** → 拒绝，逐条说明不存在时刻、飞行分钟不符或转机越界等原因；
- **多种** → 拒绝，列出全部候选解释，说明无法唯一确定。
- 拒绝时**原发布保持不变**。

### 4. 修订号与并发

- 仅**更高修订号**可替换当前已发布版本；较低/相等修订号判过期拒绝；
- 已发布修订**同号同内容重传** → 幂等，沿用原结果；**同号异内容** → 冲突拒绝；
- 被拒修订不占号，同号可换内容重新评估；
- 并发上传在单条 `BEGIN IMMEDIATE` 事务内串行裁决，依据各自**提交时刻的最新
  修订**判断；SQLite 启用 WAL 与 `busy_timeout`，配合 Gunicorn 多 worker。
- 列表、详情、下载等只读路径使用 `BEGIN DEFERRED` 读事务，**不获取写锁**：
  读取尚未结束时，其他行程的上传以及同一行程较高修订的发布都可正常完成。
- 详情页一次响应内的当前修订号、修订记录与 UTC 航段取自**同一 WAL 快照**
  （单条一致性视图 JOIN），并发更正不会让页面混用新旧版本；读取结束后发起的
  新请求立即看到新版。
- 下载在单个读快照内读取修订与航段，只包含**一个完整已发布版本**。
- 下载只返回**完整已发布版本**（当前版或历史已发布修订号），未发布修订返回 404。

## 页面与接口

| 方法/路径 | 说明 |
| --- | --- |
| `GET /` | 上传表单 + 行程列表 |
| `POST /upload` | 上传 CSV 并裁决（表单字段 `itinerary_no`、`revision_no`、`schedule_csv`） |
| `GET /itinerary/<行程号>` | 当前发布时间线、最近裁决原因、修订历史 |
| `GET /download/<行程号>` | 下载当前已发布版 CSV |
| `GET /download/<行程号>?revision=N` | 下载指定的历史已发布修订 |
| `GET /samples/<文件名>` | 下载示例 CSV |

## 项目结构

```
app/
  __init__.py      Flask 工厂与路由
  timeline.py      zoneinfo DST 解析、解释枚举、业务约束
  csv_parser.py    CSV 解码/表头映射/字段校验
  db.py            SQLite schema、WAL、立即事务
  service.py       修订裁决、发布、幂等/冲突/过期、查询
  templates/       Jinja2 页面
samples/           5 个示例 CSV
tests/             pytest 测试（引擎、服务、Web、并发）
wsgi.py            Gunicorn 入口
Dockerfile / docker-compose.yml / requirements.txt
```

## 测试

```bash
python3 -m pytest tests/
```

46 个测试覆盖：DST 三种端点状态、边界转机间隔、飞行分钟/机场相接校验、
零/一/多解释、修订幂等/冲突/过期/替换、下载仅限已发布版本，以及多线程并发
提交的串行一致性。`tests/test_read_concurrency.py` 另有 7 个测试：慢读取期间
上传/发布不被阻塞、读路由不等待写锁、详情与下载的一致性快照。
