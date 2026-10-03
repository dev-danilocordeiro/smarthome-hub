# Alerts, notifications and webhooks

How the hub tells people that something needs them, and how to receive those alerts in
your own tools. Design and trade-offs: [ADR 0013](adr/0013-alerts-and-notifications.md).

## Alerts

| Kind | Opens when | Resolves when | Severity |
|---|---|---|---|
| `device_offline` | a device has been offline for 5 minutes | it is back online | `critical` for locks and cameras, else `warning` |
| `low_battery` | a battery reading is below 15 % | a reading is 20 % or more | as above |
| `energy_budget` | the month's consumption reaches 80 % (`info`) or 100 % (`warning`) of the budget | the month ends | |

One condition is one alert, however many times it flaps. Members of the home (not
guests) get an inbox entry when an alert opens and when it resolves; email follows each
member's preferences:

```http
PUT /homes/{home}/notification-preferences
{"email_enabled": true, "min_email_severity": "warning", "quiet_start": 22, "quiet_end": 7}
```

Quiet hours are whole hours of the home's time zone and may wrap past midnight. An email
due during quiet hours waits until they end, and is dropped if the alert resolved in the
meantime. **Critical alerts are sent at once**, quiet hours or not.

## Webhooks

An owner can register one HTTPS endpoint per home:

```http
PUT /homes/{home}/webhook
{"url": "https://example.com/hooks/smarthome", "enabled": true, "rotate_secret": false}
```

The response that creates the webhook (or rotates its secret) is the **only** one that
includes `secret`. Store it: it signs every delivery. `POST /homes/{home}/webhook/test`
queues a signed `ping`.

### What you receive

```http
POST /hooks/smarthome HTTP/1.1
Content-Type: application/json
Idempotency-Key: 5f0c…:opened:webhook
X-Smarthome-Signature: t=1791021774,v1=8c1f…e2

{"alert":{"details":{"battery_pct":9.0},"device_id":"lock-1f60…","home_id":"4aae…",
"id":"5f0c…","key":"low_battery:lock-1f60…","kind":"low_battery","opened_at":"2026-10-03T10:41:26+00:00",
"resolved_at":null,"severity":"critical","status":"open","title":"Front door battery is low (9%)"},
"type":"alert.opened"}
```

`type` is `alert.opened`, `alert.resolved` or `ping`.

### Verifying a delivery

`v1` is `hex(HMAC-SHA256(secret, "<t>.<raw body>"))`. Recompute it over the **raw**
request body (before any JSON parsing), compare in constant time, and reject timestamps
more than five minutes away from your clock, so a captured request cannot be replayed:

```python
import hashlib, hmac, time


def verify(secret: str, header: str, body: bytes, tolerance_s: int = 300) -> bool:
    fields = dict(part.split("=", 1) for part in header.split(","))
    timestamp = int(fields["t"])
    if abs(time.time() - timestamp) > tolerance_s:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return hmac.compare_digest(expected.hexdigest(), fields.get("v1", ""))
```

```js
import { createHmac, timingSafeEqual } from "node:crypto";

export function verify(secret, header, rawBody, toleranceS = 300) {
  const fields = Object.fromEntries(header.split(",").map((p) => p.split("=", 2)));
  const t = Number(fields.t);
  if (Math.abs(Date.now() / 1000 - t) > toleranceS) return false;
  const expected = createHmac("sha256", secret).update(`${t}.`).update(rawBody).digest("hex");
  const given = Buffer.from(fields.v1 ?? "", "utf8");
  return given.length === expected.length && timingSafeEqual(given, Buffer.from(expected, "utf8"));
}
```

### Retries and duplicates

Answer with any `2xx` quickly (within 5 s) and do the work afterwards.

- `5xx`, timeouts, connection errors and `408`/`409`/`425`/`429` are retried with
  exponential backoff (30 s, 1 min, 2 min… capped at 1 h), up to 8 attempts.
- Any other `4xx` is final: the hub stops trying.
- Redirects are not followed.

Delivery is **at least once**. The same transition can arrive twice (for instance if the
hub restarts between sending and recording the send); it carries the same
`Idempotency-Key`, so store the keys you have processed and ignore repeats.

### Where a webhook may point

Outside development, the URL must be `https`, must not embed credentials, and its host
must resolve to a public address: loopback, private ranges (10/8, 172.16/12,
192.168/16, fc00::/7), link-local (including 169.254.169.254) and unspecified addresses
are refused, when the URL is saved and again before each delivery. In development
(`SMARTHOME_ENVIRONMENT=local`) any address is allowed, so a receiver on your laptop works.
