# Pet Price Guard

面向宠物品牌的**公开渠道价格监测工具**。不需要理解爬虫或价格规则：提供品牌、产品、价格底线和需要关注的商品页，系统会按计划检查价格，保存记录，并创建需要人工复核的“疑似低价”告警。

> 适用范围：无需登录的公开商品页，或已获授权的官方 API。它不自动改价、不联系商家，也不绕过验证码、登录或反爬机制。

## 你需要准备什么

每个需要监测的商品只需要这些业务信息：

1. `brand`：品牌名称，例如 `PawCare`。
2. `product`：产品名称与规格，例如 `成犬粮 2kg`。
3. `floor_price`：内部审核用的最低公开展示价，例如 `169`。
4. `channel` 和 `url`：关注的渠道名称和该商品的公开页面 URL。
5. 可选 `schedule`：标准 cron 表达式，例如 `0 */6 * * *`（每 6 小时）。

## 5 分钟启动

### 本地运行

```powershell
cd pet-price-guard
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

打开 <http://localhost:8000/docs>，即可使用自动生成的 API 文档。

### Docker 运行

```powershell
docker compose up
```

服务默认监听 `http://localhost:8000`。

## 最快的使用方式

### 命令行：创建一个监测

```powershell
python -m app.cli add-monitor `
  --brand "PawCare" `
  --product "成犬粮 2kg" `
  --sku "DOG-FOOD-2KG" `
  --channel "jd" `
  --url "https://example.com/products/dog-food-2kg" `
  --floor 169 `
  --schedule "0 */6 * * *"

python -m app.cli run 1
python -m app.cli report
```

`run` 会立即检查一次；`report` 返回监测任务数、未确认告警数和全部告警数。

### API：创建一个监测

`POST /monitors`

```json
{
  "brand": "PawCare",
  "product": "成犬粮 2kg",
  "sku": "DOG-FOOD-2KG",
  "floor_price": 169,
  "schedule": "0 */6 * * *",
  "channels": [{
    "name": "jd",
    "url": "https://example.com/products/dog-food-2kg",
    "selector": ".price"
  }]
}
```

`selector` 可选。未提供时，系统按 JSON-LD、OpenGraph 和页面文本依次寻找价格；提供时使用该 CSS 选择器读取价格。

## 给 Agent 的批量入口

当一个 Agent 已有品牌、SKU、产品、阈值和渠道 URL 时，使用 `POST /monitors/batch`。一次最多可创建 1,000 个任务；无需传入采集器、数据库或调度器配置。

```json
{
  "monitors": [
    {
      "brand": "PawCare",
      "product": "成犬粮 2kg",
      "sku": "DOG-FOOD-2KG",
      "floor_price": 169,
      "schedule": "0 */6 * * *",
      "channels": [
        {"name": "jd", "url": "https://example.com/jd/dog-food"},
        {"name": "tmall", "url": "https://example.com/tmall/dog-food"}
      ]
    },
    {
      "brand": "PawCare",
      "product": "猫罐头 85g x 12",
      "sku": "CAT-CAN-12",
      "floor_price": 99,
      "channels": [{"name": "douyin", "url": "https://example.com/douyin/cat-can"}]
    }
  ]
}
```

成功后返回创建数量和每个任务的 ID。手动执行：`POST /monitors/{id}/run`。

## CSV 导入

`POST /monitors/import-csv` 接受一个名为 `csv_text` 的 UTF-8 参数。CSV 首行必须包含以下列；`sku`、`selector`、`schedule` 可以留空。

```csv
brand,product,sku,floor_price,channel,url,selector,schedule
PawCare,成犬粮 2kg,DOG-FOOD-2KG,169,jd,https://example.com/jd/dog-food,.price,0 */6 * * *
PawCare,猫罐头 85g x 12,CAT-CAN-12,99,douyin,https://example.com/douyin/cat-can,,
```

PowerShell 示例：

```powershell
$csv = Get-Content .\listings.csv -Raw
Invoke-RestMethod -Method Post `
  -Uri "http://localhost:8000/monitors/import-csv" `
  -Body @{ csv_text = $csv }
```

## 定时检查

`schedule` 使用标准 5 段 cron：`分 时 日 月 周`。

| 计划 | 示例 | 含义 |
| --- | --- | --- |
| 每 6 小时 | `0 */6 * * *` | 00:00、06:00、12:00、18:00 运行 |
| 每天 09:00 | `0 9 * * *` | 每天早上 9 点运行 |
| 每周一 09:00 | `0 9 * * 1` | 每周一早上 9 点运行 |

调度由 API 进程加载。生产环境请只运行一个 API worker，或改用外部调度器，避免重复执行。

## 告警与内部通知

当系统第一次观察到价格低于 `floor_price` 时，会创建一条去重的疑似低价告警。持续相同低价不会反复创建同一告警。

- 查看：`GET /alerts`
- 只看未确认：`GET /alerts?status=open`
- 确认：`POST /alerts/{id}/ack`
- 概览：`GET /reports/summary`

如需通知内部系统，在部署环境设置：

```powershell
$env:PPG_WEBHOOK_URL = "https://internal.example.com/pricing-events"
```

每条新告警会以 JSON 发送 `violation.opened` 事件，包含品牌、产品、SKU、渠道、URL、观测价、阈值、证据哈希和发生时间。Webhook 地址由部署者设置，**不会**由 API 调用方或导入文件指定。

## 如何本地演示

不需要访问真实网站即可验证完整流程：将渠道 URL 写成 `demo://80`，再设置阈值为 `100`。系统会读取演示价格 80 并生成疑似低价告警。

```powershell
$env:PPG_COLLECTOR = "demo"
python -m app.cli add-monitor --brand "Demo" --product "演示商品" --channel demo --url "demo://80" --floor 100
python -m app.cli run 1
```

## 数据与合规边界

- 默认使用本地 SQLite 文件 `priceguard.db`；设置 `PPG_DATABASE_URL` 可切换到 PostgreSQL。
- 系统保存观测价格、页面文本摘要和证据哈希，用于内部复核。
- 低于阈值只表示“疑似低价线索”，在采取渠道行动前应核对规格、赠品、满减、会员价和当地合规要求。
- 不采集需要登录、验证码或访问控制绕过的内容。

## 验证

```powershell
python -m pytest -q
python -m compileall -q app
```

当前版本已验证：核心告警去重单元测试、批量创建 API、CSV 导入 API。