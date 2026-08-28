def monitor_payload(price="80", schedule=None):
    payload = {
        "brand": "PawCare",
        "product": "Dog Food 2kg",
        "sku": "DOG-2KG",
        "floor_price": "100.00",
        "channels": [{"name": "demo", "url": f"demo://{price}"}],
    }
    if schedule is not None:
        payload["schedule"] = schedule
    return payload


def test_health_is_public(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_api_requires_key(client):
    assert client.get("/monitors").status_code == 401
    assert client.get("/monitors", headers={"X-API-Key": "wrong"}).status_code == 401


def test_create_run_ack_flow(client, auth_headers):
    created = client.post("/monitors", json=monitor_payload(), headers=auth_headers)
    assert created.status_code == 201, created.text
    monitor_id = created.json()["id"]
    result = client.post(f"/monitors/{monitor_id}/run", headers=auth_headers)
    assert result.json()["new_alerts"] == 1
    alerts = client.get("/alerts?status=open", headers=auth_headers).json()
    assert len(alerts) == 1
    acknowledged = client.post(f"/alerts/{alerts[0]['id']}/ack", headers=auth_headers)
    assert acknowledged.json()["status"] == "acknowledged"


def test_invalid_cron_is_rejected(client, auth_headers):
    response = client.post("/monitors", json=monitor_payload(schedule="not cron"), headers=auth_headers)
    assert response.status_code == 422


def test_batch_limit_and_csv_json_contract(client, auth_headers):
    batch = client.post("/monitors/batch", json={"monitors": [monitor_payload("80"), monitor_payload("90")]}, headers=auth_headers)
    assert batch.status_code == 201
    assert batch.json()["created"] == 2
    csv_text = "brand,product,sku,floor_price,channel,url,selector,schedule\nPawCare,Cat Food,CAT,99,demo,demo://80,,"
    imported = client.post("/monitors/import-csv", json={"csv_text": csv_text}, headers=auth_headers)
    assert imported.status_code == 201, imported.text
    assert imported.json()["created"] == 1


def test_csv_missing_columns_is_clear(client, auth_headers):
    response = client.post("/monitors/import-csv", json={"csv_text": "brand,product\nA,B"}, headers=auth_headers)
    assert response.status_code == 422
    assert "missing columns" in response.json()["detail"]

