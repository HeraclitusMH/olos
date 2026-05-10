from httpx import AsyncClient


async def test_get_whatsapp_webhook_returns_200(client: AsyncClient) -> None:
    response = await client.get("/webhooks/whatsapp")

    assert response.status_code == 200
    assert response.json()["status"] == "placeholder"


async def test_get_whatsapp_webhook_echoes_query_params(client: AsyncClient) -> None:
    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": "verify-token-test",
        "hub.challenge": "1234567890",
    }
    response = await client.get("/webhooks/whatsapp", params=params)

    assert response.status_code == 200
    body = response.json()
    assert body["params"]["hub.mode"] == "subscribe"
    assert body["params"]["hub.challenge"] == "1234567890"


async def test_post_whatsapp_webhook_returns_200(client: AsyncClient) -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "0", "changes": []}],
    }
    response = await client.post("/webhooks/whatsapp", json=payload)

    assert response.status_code == 200
    assert response.json() == {"status": "received"}


async def test_post_whatsapp_webhook_accepts_empty_body(client: AsyncClient) -> None:
    response = await client.post("/webhooks/whatsapp", json={})

    assert response.status_code == 200
