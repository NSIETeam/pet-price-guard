# Pet Price Guard

宠物品牌公开渠道价格监测服务。它采集公开商品页中的结构化价格，将价格与内部审核底线比较，保存观测证据，并把新的疑似低价周期生成为待人工复核告警。

它不会自动改价、下架商品或联系商家，也不会绕过登录、验证码或反爬措施。

## 当前能力

- 单条、批量 JSON 和 CSV 监测任务创建
- CSS 选择器、OpenGraph 与 JSON-LD 结构化价格读取
- UTC cron 定时任务和手动运行
- 连续低价只告警一次；价格恢复后再次低于阈值会重新告警
- API Key 鉴权、公共 URL 校验、私网与保留地址拦截
- DNS 解析结果固定到实际 TCP 连接，阻止 DNS 重绑定绕过
- 持久化观测、证据哈希和 Webhook 投递队列
- Webhook 最多 5 次指数退避重试
- SQLite 本地运行；可通过 SQLAlchemy URL 使用 PostgreSQL

## 本地启动

要求 Python 3.12。

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export PPG_API_KEY='replace-with-a-long-random-secret'
alembic upgrade head
uvicorn app.main:app --reload
```

打开 <http://localhost:8000/docs>。除 `/health` 外，请求都需要：

```text
X-API-Key: replace-with-a-long-random-secret
```

## Docker

复制 `.env.example` 为 `.env`，至少设置 `PPG_API_KEY`，然后运行：

```bash
docker compose up --build -d
curl http://127.0.0.1:8000/health
```

Compose 只绑定本机 `127.0.0.1:8000`。如需对外服务，应放在带 TLS、限流和访问日志的反向代理之后。应用内调度器要求 API 只运行一个 worker；多副本部署应改用独立调度服务。

## 创建和运行监测

```bash
curl -X POST http://127.0.0.1:8000/monitors \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $PPG_API_KEY" \
  -d '{
    "brand":"PawCare",
    "product":"成犬粮 2kg",
    "sku":"DOG-2KG",
    "floor_price":"169.00",
    "schedule":"0 */6 * * *",
    "channels":[{"name":"official-store","url":"https://shop.example.com/item","selector":".price"}]
  }'
```

手动运行使用 `POST /monitors/{id}/run`；查询告警使用 `GET /alerts?status=open`；确认告警使用 `POST /alerts/{id}/ack`。

## CSV 导入

`POST /monitors/import-csv` 接受 JSON 对象，不是表单：

```json
{
  "csv_text": "brand,product,sku,floor_price,channel,url,selector,schedule\nPawCare,成犬粮 2kg,DOG-2KG,169,official,https://shop.example.com/item,.price,0 */6 * * *"
}
```

必需列为 `brand,product,floor_price,channel,url`；可选列为 `sku,selector,schedule`。一次 JSON 批量创建最多 1,000 个监测，每个监测最多 100 个渠道。

## 采价规则

未提供选择器时，仅从 OpenGraph 和 JSON-LD 的明确价格字段读取。系统不再从整页正文取“第一个数字”，以避免把销量、规格或日期误判成价格。无法可靠识别时会保存失败观测，要求配置 CSS 选择器。

所有金额使用两位小数的定点数。低价状态按渠道维护：连续低价只生成一个告警；只有先恢复至底线或以上，后续再次跌破才开启新告警周期。

## Webhook

设置 `PPG_WEBHOOK_URL` 后，新告警会入库为投递任务，再发送 `violation.opened` JSON。失败任务按指数退避重试，最多 5 次。摘要接口会返回待投递和永久失败数量；也可调用 `POST /webhooks/deliver` 手动触发到期任务。

Webhook URL 与商品 URL 一样必须是标准 80/443 端口的公共 HTTP(S) 地址，不能解析到环回、私网、链路本地或保留地址。每次投递携带基于告警 ID 的 `Idempotency-Key`；接收方应保存该键，以处理发送成功但确认落库前进程退出造成的安全重试。

公开页面响应默认最多读取 5 MiB，可通过 `PPG_MAX_RESPONSE_BYTES` 调整。解析前会固定经过校验的公共 IP 到实际 TCP 连接，并关闭环境代理继承，避免 DNS 重绑定和代理配置绕过访问边界。

## 从旧 MVP 升级

本版本的数据模型有不兼容变更：金额改为定点数，渠道增加低价状态，观测和告警增加渠道关联，并新增 Webhook 投递表。Alembic 已提供新部署的初始结构和未来版本迁移入口。API 启动时只校验迁移版本，不再自动创建表；数据库缺少迁移或版本不匹配时会拒绝启动。

升级已有实例前必须备份数据库。对于仅用于演示的 SQLite 数据，建议归档旧 `priceguard.db` 后由新版本创建数据库；生产数据应先编写并演练针对现有结构的显式迁移，不能直接覆盖上线。

## 验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q app tests
```

测试覆盖鉴权、cron 校验、批量与 CSV 接口、金额解析、SSRF 地址拦截、连续告警去重、恢复后重新告警和采集失败留痕。

## 合规边界

低于内部底线只代表待复核线索。采取渠道行动前，应人工核对商品规格、赠品、优惠券、满减、会员价、地区差异以及适用的法律和商业政策。
