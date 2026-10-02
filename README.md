# 港口泊位与航道调度

纯Python标准库实现的港口泊位与航道调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动（可注入时钟便于测试）。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、时间窗冲突、租约时段校验。
- `src/repository.py`：SQLite建表、事务、航道租约、值班班次和查询。
- `src/service.py`：用例编排、身份/班次/权限闸门、崩溃恢复、租约抢占与审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景、租约并发/续租/恢复测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8321
```

默认端口为`8321`。服务启动时自动建表，并先执行**崩溃恢复**：把所有已到期但仍为`active`的航道租约落定为`expired`（写审计），再对外服务。

## 并发与一致性约定

- **租约抢占**：申请按`船舶+时段`抢占，`BEGIN IMMEDIATE`事务内做窗口重叠检查；同一航道由部分唯一索引`uq_active_lease_per_channel`保证只存在一份`active`租约，两个控制台同时放行只有一方成功。
- **栅栏**：每份租约带按航道单调递增的`fence_epoch`和`version`。续租必须带当前`lease_version`；已释放/已过期租约的晚到续租一律拒绝，不会被重新救活。同航道重新申请得到新的epoch。
- **靠泊确认**：`confirm`必须提供`lease_id/lease_version`，在同一事务内清扫过期租约并核对状态、版本、船舶、航道与时段覆盖。租约过期或时段不覆盖计划时返回`409 lease_expired`，要求重新核对时段并申请；确认时把租约快照固化为`payload.lease_basis`，之后`berth/depart`不再依赖活租约，已靠泊记录永久保留当时依据。
- **值班班次**：全局只允许一个`open`班次。所有写操作必须携带当前班次（`X-Shift-Id`头或`shift_id`字段）；换班后旧班次、越权角色、缺少身份的操作返回`403`并写入`denial`审计。

## 主要接口

只读接口提供`X-User-Id`、`X-Role`即可；写操作还需`X-Shift-Id`。

### 靠泊计划

- `GET /api/records` / `GET /api/records/{id}` / `GET /api/records/{id}/audit`
- `POST /api/records`：创建计划。`data`新增必填`channel`，并包含船舶、泊位、吃水与`eta_hour/etd_hour`窗口。
- `POST /api/records/{id}/actions/{confirm|berth|depart|cancel}`：`confirm`的`data`需含`pilot_id`、`lease_id`、`lease_version`。

### 航道租约

- `POST /api/leases`：申请/抢占租约，`data`为`{"vessel","channel","start_hour","end_hour","record_id"?}`，可带`lease_key`。
- `POST /api/leases/{id}/renew`：`{"lease_version":n,"end_hour":k}`。
- `POST /api/leases/{id}/release`：`{"lease_version":n,"reason"?}`。
- `GET /api/leases?state=&channel=`、`GET /api/leases/{id}`、`GET /api/leases/{id}/audit`。

### 值班与审计

- `POST /api/shifts`：开班（自动换班：旧班次关闭并审计）。
- `POST /api/shifts/{id}/close`：闭班。
- `GET /api/shifts/active`：当前班次。
- `GET /api/denials`：被拒绝操作（越权、换班后操作等）审计列表。
- `GET /health`、`GET /api/stats`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、两个控制台并发只一方成功、续租栅栏、过期拒绝与重新核对、崩溃恢复、换班拒绝与审计、已靠泊依据保留。
