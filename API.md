# AI Sales Message API

Turns an advisor's short key points into a polished, client-ready intro message,
grounded only in the deal terms, product copy, client profile and any explicitly
enabled, current-day organization market commentary you send. It never invents
market facts or figures, and it keeps every rate and amount exactly as supplied.

- **Base URL:** `https://<your-service>.onrender.com` (ask the service owner)
- **Auth:** an API key on every request
- **Format:** JSON in, JSON out

---

## Authentication

Send the key in **either** header:

```http
Authorization: Bearer <API_KEY>
```
```http
X-API-Key: <API_KEY>
```

A missing or wrong key returns `401`. The key is a server secret: **call this
API from your backend**, not from browser code where users could read the key.

---

## Endpoints

### `POST /v1/ai/sales-message`

**Request body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `product` | string | **yes** | Structure name, e.g. `"FEC"`, `"Knock Out Conv. (LEV)"` |
| `advisor_note` | string | **yes** | The advisor's key points (max 5,000 chars) |
| `deal_terms` | object | no | Label → value, e.g. `{"Pair": "AUDUSD", "Notional": "USD 1,000,000", "Expiry": "2026-12-09"}`. Values must be strings. Reproduced exactly. |
| `product_family` | string | no | e.g. `"Knock-Out family"` |
| `product_outline` | string[] | no | Background description of the product |
| `product_benefits` | string[] | no | Benefits the message may mention |
| `product_risks` | string[] | no | Risks the message may mention |
| `client_name` | string | no | Contact name |
| `client_company` | string | no | Company name |
| `client_industry` | string | no | |
| `client_risk_appetite` | string | no | e.g. `"Balanced"` |
| `client_hedging_horizon` | string | no | e.g. `"3–12 months"` |
| `client_functional_currency` | string | no | e.g. `"AUD"` |
| `client_notes` | string | no | Objectives, budget rate, volumes… |
| `writing_style` | string | no | `"Executive"`, `"Concise"`, `"Technical"`, `"Relationship-first"` |
| `tone` | string | no | `"Formal"`, `"Consultative"`, `"Direct"`, `"Warm"` |
| `sales_positioning` | string | no | Where to put the emphasis (never adds facts) |
| `advisor_guidance` | string | no | Free-form standing guidance (max 12,000 characters). May include a sample output for tone/structure; its facts are not reused unless independently supported by the current deal, profile or advisor note. |
| `market_commentary` | string | no | Organization-approved daily market commentary (max 8,000 characters); included only when `use_market_commentary` is true and its date is today in UTC. Treated as source facts, not instructions. |
| `market_commentary_date` | date | no | ISO date (`YYYY-MM-DD`) the commentary applies to. Stale and future commentary is excluded. |
| `use_market_commentary` | bool | no | Defaults to `false`; explicitly enables current-day market commentary in this draft. |
| `master_mkt_why` / `master_client_why` / `master_product_why` | string | no | Legacy fields retained for existing integrations; new clients should use `advisor_guidance`. |
| `suggest_subject` | bool | no | `true` also returns an email `subject` |
| `show_alternative` | bool | no | `true` also returns an `alternative_message` (same facts, different angle) |

**Response `200`**

```json
{
  "message": "Hi Ada, …",
  "subject": "Your AUD/USD hedge — indicative terms",
  "alternative_message": null,
  "model": "gpt-4o-mini",
  "generated_at": "2026-09-29T01:20:00Z"
}
```

`subject` and `alternative_message` are `null` unless requested.

**Errors**

| Status | Meaning | What to do |
|---|---|---|
| `401` | Missing or wrong API key | Check the key and header |
| `422` | Invalid body (e.g. no `product` or `advisor_note`) | `detail` lists the fields |
| `502` | The AI provider failed or timed out | Retry after a short wait |
| `503` | The service has no OpenAI key configured | Contact the service owner |

Typical response time is 3–10 seconds; set a client timeout of about 30 seconds.

### `POST /v1/ai/rank-sales-message`

Same auth as above. Scores a drafted message 0–5 against organization indicators.

| Field | Type | Required | Notes |
|---|---|---|---|
| `message` | string | **yes** | The drafted message (max 8,000 chars) |
| `indicators` | `{id, name, guidance}[]` | no | Up to 20. Empty or omitted = the service's 8 default indicators. IDs must be unique. |
| `client_context` | string | no | Max 4,000 chars |
| `deal_context` | string | no | Max 4,000 chars |

Response `200`: `{"indicators": [{"id", "score", "evidence"}], "overall_score": 3.1, "band": "red" | "amber" | "green"}`
(red < 2.3 ≤ amber < 3.8 ≤ green).

### `POST /v1/ai/sms-summary`

Same auth as above. Summarises a pitch into one SMS of at most 200 characters, ending with the deal link.

| Field | Type | Required | Notes |
|---|---|---|---|
| `sales_pitch` | string | **yes** | Max 8,000 chars |
| `deal_link` | string | **yes** | Appended as `Deal: <link>` |

Response `200`: `{"message": "… Deal: https://…"}`. Nothing is sent; the caller delivers the SMS.

### `POST /v1/ui/sales-message` (not for integrations)

Used only by the service's own web page. It needs no key but rejects anything
that isn't a same-origin browser request (`403`) and is rate-limited (`429`).
Integrations must use the `/v1/ai/*` endpoints with a key. In production, set
`UI_ENABLED=false` so the keyless `/v1/ui/*` routes are switched off: their
same-origin check relies on browser headers that a script can fake.

The built-in page also uses `POST /v1/ui/rank-sales-message` to evaluate a draft
against the organization indicators configured in the demo. It returns a 0–5
score and evidence for each indicator. This endpoint has the same same-origin
and rate limits and is not an integration API.

### `GET /healthz`

No key needed. Returns `{"status": "ok", "key_configured": true, "auth_required": true}`.

---

## Examples

**curl**

```bash
curl -X POST "$BASE_URL/v1/ai/sales-message" \
  -H "Authorization: Bearer $AI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "product": "FEC",
    "deal_terms": {"Pair": "AUDUSD", "Direction": "Buy USD", "Notional": "USD 1,000,000", "FEC Rate": "0.6545"},
    "product_benefits": ["Locks in a known exchange rate."],
    "product_risks": ["No benefit if the market moves in your favour."],
    "advisor_note": "protects budget rate 0.66\ncovers 30% of monthly needs",
    "client_name": "Ada Novik",
    "client_company": "Novik Trading",
    "suggest_subject": true
  }'
```

The built-in demo lets an organization save one dated commentary entry from
the account menu. Its displayed `@commentary.foundryfx.example` address is a
prototype-only placeholder; inbound email delivery and shared organization
storage are not connected. Only commentary dated for the current UTC day can be
sent to generation, and its use is off by default until the advisor enables
the toggle.

**Node.js (server side)**

```js
const res = await fetch(`${process.env.AI_BASE_URL}/v1/ai/sales-message`, {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${process.env.AI_API_KEY}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ product: "FEC", advisor_note: "protects budget rate", deal_terms: { Pair: "AUDUSD" } }),
  signal: AbortSignal.timeout(30_000),
});
if (!res.ok) throw new Error(`AI service ${res.status}: ${await res.text()}`);
const { message, subject } = await res.json();
```

**Python**

```python
import os, requests

r = requests.post(
    f"{os.environ['AI_BASE_URL']}/v1/ai/sales-message",
    headers={"Authorization": f"Bearer {os.environ['AI_API_KEY']}"},
    json={"product": "FEC", "advisor_note": "protects budget rate", "deal_terms": {"Pair": "AUDUSD"}},
    timeout=30,
)
r.raise_for_status()
print(r.json()["message"])
```

**Rust (reqwest)**

```rust
let resp: serde_json::Value = reqwest::Client::new()
    .post(format!("{base_url}/v1/ai/sales-message"))
    .bearer_auth(&api_key)
    .json(&serde_json::json!({ "product": "FEC", "advisor_note": "protects budget rate" }))
    .timeout(std::time::Duration::from_secs(30))
    .send().await?
    .error_for_status()?
    .json().await?;
let message = resp["message"].as_str().unwrap_or_default();
```

---

## Calling from a browser on another domain

Server-to-server is recommended. If a web front end on another domain must call
the API directly, the service owner adds that origin to `CORS_ORIGINS`
(comma-separated, e.g. `https://builder.foundryfx.com`). Anything in browser
code is visible to users, so only do this behind your own login.

## Key management (service owner)

- Keys live in the `AI_API_TOKEN` environment variable (Render → Environment).
- Several keys can be active at once, comma-separated. Use this to give each
  app its own key, or to rotate: add the new key, move apps over, then remove
  the old one.
- Generate a key: `python -c "import secrets; print('ffx_' + secrets.token_urlsafe(32))"`
