# 港口泊位与航道调度

纯Python标准库实现的港口泊位与航道调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动；启动时先执行租约恢复扫描。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、时间窗冲突、租约校验和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询（记录、审计、航道租约、值班、系统事件）。
- `src/service.py`：用例编排、权限检查、值班检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线与系统安全事件。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、租约生命周期和失败场景测试。

## 航道通行租约

靠泊计划可声明`channel`（航道）。声明后，`confirm`前必须持有有效租约：

- `request_channel`：按船舶和时段抢占航道，同一航道同一时段只留一份有效租约，租约带单调递增的`fencing_token`和过期时间（`ttl_seconds`，默认300秒）。
- `renew_channel`：在租约有效期内续租；已释放或已过期的租约是终态，晚到的续租不能将其救活。
- `release_channel`：主动释放；`depart`/`cancel`会在同一事务内自动释放。
- 租约过期后`confirm`被拒绝并记入审计，需重新核对时段再次申请；`confirm`成功时把租约依据（`channel_lease_basis`）写入记录，已靠泊记录永久保留当时依据。
- 服务重启时先扫描并把过期租约落库为`expired`，再对外服务。
- 租约操作仅当班值班员（或admin）可执行；`POST /api/shifts/handover`交接班，非当班者或越权换班会被拒绝并记入审计。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`；`action`除`confirm/berth/depart/cancel`外，新增`request_channel/renew_channel/release_channel`。
- `GET /api/leases`：租约列表，可带`channel`、`record_id`、`limit`参数，`effective_state`为按当前时间折算的状态。
- `GET /api/shifts/current`：当前值班员。
- `POST /api/shifts/handover`：换班，请求体为`{"officer_id":"..."}`。
- `GET /api/audit/system`：系统级安全事件（换班、越权拒绝、租约恢复等）。

## 启动

```bash
python3 app.py --db ./data.db --port 8321
```

默认端口为`8321`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、租约抢占与并发、过期拒绝、续租与释放、崩溃恢复和值班换班审计。
