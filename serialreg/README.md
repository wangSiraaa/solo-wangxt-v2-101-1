# 连续出版物登记应用（SerialReg）

管理连续出版物的「编号覆盖」与「实体位置」两层关系。典型场景：**一期合刊覆盖两个期号，
同时又是馆内不同的物理实体**——系统用四层模型同时表达，不允许用一个条码覆盖多个期号关系。

## 核心领域模型（PostgreSQL）

| 层 | 模型 | 含义 |
|---|---|---|
| 书目 | `Title` | 刊名、ISSN、出版状态；停刊需填**停刊月份** |
| 编号 | `IssueNumber` | 卷期编号槽位（卷+期唯一），**不含年月**，跨年卷只有一个编号 |
| 发行 | `Issue` + `IssueNumbering` | 一次出版行为。普通期关联 1 个编号，**两期合刊关联 ≥2 个独立编号记录** |
| 实体 | `Item` | 一条条码 = 一个实物，指向一个 `Issue`，多期号覆盖由关联表表达 |
| 装订 | `Binding` + `BindingEntry` | 多个实物订成一册，记录装订后位置并封存各实物原位置 |
| 流通 | `Loan` + `LoanEvent` | 本地借出单与**仅追加事件链**：借出/归还/遗失，带幂等标识与顺序 |

关键业务规则：

- **发行年月与卷期编号分开录入**：跨年卷（如 v.60 no.3 印 2023-12～2024-01）
  编号仍是一条，发行覆盖区间记在 `Issue.issue_month / issue_month_end`。
- **缺号 ≠ 缺藏**：
  - `not_published`（在刊但无发行记录）/ `ceased_gap`（停刊月之后）= **缺号**，
    只表示没有发行，不自动判定缺藏；
  - `issued+missing` = **缺藏**：已发行但没有可用实物（未入藏或全部丢失）。
  - 借出中的实物仍是馆藏（`issued+held`），只是当前不可得（`availability=checked_out/overdue`）。
- **合刊**：一个实物 + 两条（或更多）`IssueNumbering`。从 no.3 或 no.4 都能定位到同一实物，
  借出后从任一期号/条码查到的都是**同一张借出单、同一到期日与同一流通状态**。
- **装订**：实物条码与多期号关系不变，实际位置改指向装订册；
  **拆订**后按封存的 `previous_location` 恢复各自位置，合刊编号关系依旧完整。
- 禁止跨刊混装、重复装订；`status=bound` 只能由装订/拆订流程设置。
- **流通事件链（事件溯源）**：
  - 每条 `LoanEvent` 有全局唯一 `event_id`（**幂等标识**，重复提交只回放、不重复改状态）
    与单内严格递增的 `seq`（发生顺序）；`occurred_at` 是业务发生时间；
  - **迟到事件**（`occurred_at` 早于已应用的最新处置）只入审计链
    （`applied=false, superseded=true`），不覆盖较新处置；
  - **逾期不入库**：由 `Loan.due_at` 与当前时间读时派生（`derived_status=overdue`），
    所以"逾期后又收到较早的遗失/归还"仍按事件顺序保留正确当前状态与完整历史；
  - 借出时封存架位，归还时恢复同一位置；
  - **借出中的实体不能装订、不能再次借出；已装订实体不能拆成单件外借**；
  - `Loan.status/Item.status` 是事件链的物化结果，可用
    `python manage.py rebuild_loans`（或 `POST /api/loans/{id}/rebuild/`）重放恢复，
    模拟刷新/重启。

## 技术栈

- 后端：Django 5 + Django REST Framework（`backend/`）
- 数据库：PostgreSQL 16（本地无 PG 时可用 `SERIALREG_DB=sqlite` 跑开发/测试）
- 前端：Vue 3 + Vite（`frontend/`），馆员时间轴界面

## 快速启动

### Docker Compose（推荐，含 PostgreSQL 与样例数据）

```bash
cd serialreg
docker compose up --build
# 前端 http://localhost:5173   后端 http://localhost:8000/api/
```

后端容器启动时自动 `migrate` 并执行 `seed_sample`（跨年卷 / 停刊 / 两期合刊 + 装订样例）。

### 本地分别启动

```bash
# 后端
cd backend
pip install -r requirements.txt
SERIALREG_DB=postgres PGHOST=127.0.0.1 python manage.py migrate
SERIALREG_DB=postgres python manage.py seed_sample
SERIALREG_DB=postgres python manage.py runserver

# 前端
cd frontend
npm install && npm run dev     # http://localhost:5173 ，/api 代理到 8000
```

## 验证

```bash
# PostgreSQL
SERIALREG_DB=postgres pytest -q
# 无 PG 环境（SQLite，ORM 通用）
SERIALREG_DB=sqlite pytest -q
```

15 条原有测试覆盖：跨年卷单编号跨两年、停刊必须填月份、缺号(`ceased_gap`/`not_published`)
不等于缺藏(`issued+missing`)、合刊保留两条编号关联、任一期号可定位、条码反查得到两个期号、
装订后从 no.3/no.4/no.5 均指向装订册、禁止跨刊混装与重复装订、拆订恢复原位置且关系完好、
合刊实物在两个槽位下重复提交装订时自动去重。

另加 19 条流通测试（`serials/test_circulation.py`）覆盖验收：

1. **合刊借出**后从 no.3、no.4 与条码三个入口都是同一张借出单、同一到期日与保管位置；
2. **归还幂等**：同一 `event_id` 重放只产生一次归还，归还后原架位正确恢复；
3. 借出中尝试装订、已装订实体尝试单件借出、借出中再次借出 / PATCH 改状态均被拒绝，
   原装订/合刊关系不变；
4. 逾期后收到较早的遗失事件（留痕不生效）→ 当下报失生效 → 更早归还仍不能覆盖遗失；
   遗失后迟到归还同样被压下；事件链完整，`rebuild_loans` 重放（模拟重启）恢复正确状态。

手工端到端（样例数据）：

```bash
# 装订态下从合刊任一期号定位 → 装订库
curl "/api/items/locate/?title=3&volume=8&number=4"
# 拆订 → 恢复「现刊区 B-02」
curl -X POST /api/bindings/unbind/ -H "Content-Type: application/json" -d '{"binding_id":1}'

# 借出（event_id 幂等；重复提交不会重复借出）
curl -X POST /api/loans/checkout/ -H "Content-Type: application/json" -d '{
  "barcode":"SY-8-34","borrower":"读者乙","due_at":"2026-10-15T00:00:00Z",
  "event_id":"11111111-1111-4111-8111-111111111111"}'
# 从 no.3 / no.4 / 条码均显示 availability=checked_out|overdue、同一张单与到期日
curl "/api/items/locate/?barcode=SY-8-34"
# 归还（同 event_id 重放只生效一次）；刷新/重启后可按事件链重放
curl -X POST /api/loans/return_item/ -H "Content-Type: application/json" -d '{
  "barcode":"SY-8-34","event_id":"22222222-2222-4222-8222-222222222222"}'
python manage.py rebuild_loans            # 全量按事件链重建当前状态
```

## 主要 API

| 方法/路径 | 说明 |
|---|---|
| `GET/POST /api/titles/` | 刊名；停刊须带 `ceased_month` |
| `GET/POST /api/numbers/` | 卷期编号槽位 |
| `GET/POST /api/issues/` | 发行期；`kind=combined` 时 `number_ids` 至少 2 个 |
| `GET/POST /api/items/` | 入藏实物（条码+发行期+位置） |
| `GET /api/items/locate/?title=&volume=&number=` | 按期号定位（缺号返回空匹配+状态）；含 `availability`/`custody`/`due_at` |
| `GET /api/items/locate/?barcode=` | 按条码反查（含合刊覆盖的全部期号与当前借出单） |
| `GET/POST /api/bindings/` | 装订（同刊、未装订且**在馆未借出**实物） |
| `POST /api/bindings/unbind/` | 拆订，恢复各自位置 |
| `GET /api/loans/` | 借出单列表，支持 `item`/`barcode`/`title`/`active` 过滤，内嵌完整事件链 |
| `POST /api/loans/checkout/` | 借出（`barcode`|`item`、`borrower`、`due_at`、可选 `occurred_at`/`event_id`） |
| `POST /api/loans/return_item/` | 归还事件（`loan`|`item`|`barcode` + `event_id`，迟到事件只留痕） |
| `POST /api/loans/lost/` | 遗失事件（参数同归还） |
| `GET /api/loans/{id}/events/` | 单张借出单的完整事件链（含未生效/迟到事件与原因） |
| `POST /api/loans/{id}/rebuild/` | 按事件链重放该单当前状态 |
| `GET /api/timeline/?title=` | 时间轴：编号槽位×发行×实物×流通状态×停刊标记 |

事件提交统一返回 `event_result: {replayed, applied, superseded, rejected, event_id, seq,
reject_reason}`：`replayed=true` 表示幂等命中（未再改状态），`superseded=true` 表示迟到事件
已留痕但未覆盖较新处置。业务冲突（已装订/借出中/无借出单等）返回 `409` 与机器可读 `code`。

## 界面

- 左侧刊种列表（含停刊月份徽标）与新增刊种；
- **时间轴**：每个卷期一个节点，区分「已入藏 / 缺藏 / 缺号 / 停刊后缺号」，
  展开显示发行年月区间、合刊徽标、实物条码、实际可得性（可借/借出中/逾期/遗失/已装订）、
  当前保管位置（架位/借阅人/装订册）与到期日，并可直接借出、归还、报失；
- 定位栏：按期号（合刊任一期号）或条码检索；借还后以同一检索条件自动刷新，
  三个入口始终显示同一张借出单；
- 登记操作：编号槽位 → 发行期（普通/合刊，年月与编号分录）→ 入藏；
- 装订面板：只可勾选同刊、未装订且在馆（未借出）的实物建装订册，
  借出/遗失中的实物明确列出并禁用；一键拆订并显示各实物原位置。
- 所有借还请求由前端自动生成 UUID 作为幂等标识，网络重试安全。
