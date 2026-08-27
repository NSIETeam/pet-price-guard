# Pet Price Guard

MVP for public price monitoring for pet brands. Configure a brand, products, and public channel URLs, then run manually or on a schedule. It stores observations, deduplicated suspected MAP violations, and evidence hashes.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open `http://localhost:8000/docs`. The CLI is also available:

```bash
python -m app.cli add-monitor --brand "Acme Pets" --product "Salmon 2kg" --sku AC-2 --channel jd --url https://example.test/item --floor 99
python -m app.cli run 1
python -m app.cli report
```

Use `PPG_DATABASE_URL` to point at PostgreSQL; defaults to SQLite `priceguard.db`. `PPG_COLLECTOR=demo` uses deterministic sample data for local testing. The default `web` collector fetches public pages and parses JSON-LD, OpenGraph, or an optional CSS selector. It does not bypass access controls or anti-bot systems.

## API

- `POST /monitors` create a monitor (`brand`, `product`, `sku`, `floor_price`, `channels`, optional `schedule` cron expression)
- `GET /monitors`, `POST /monitors/{id}/run`
- `GET /alerts`, `POST /alerts/{id}/ack`
- `GET /reports/summary`

Schedules are loaded by the API process with APScheduler. For production, run one API worker or use an external scheduler to avoid duplicate jobs.

## Scope

This tool is for internal policy review and public price intelligence. It does not automatically change seller prices. Verify promotions, bundles, membership prices, and legal/commercial policy before taking action.
