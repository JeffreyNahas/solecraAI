# WhatsApp integration

Meta WhatsApp Cloud API. Code: `app/whatsapp/`. The rest of the app only uses the `Channel`
interface (`app/channel.py`), so another messaging channel could replace WhatsApp.

## Webhook verification (GET `/webhook`)
When the Callback URL is saved in Meta, Meta calls
`GET /webhook?hub.mode=subscribe&hub.verify_token=…&hub.challenge=…`. If the token equals
`WHATSAPP_VERIFY_TOKEN` we echo `hub.challenge`; otherwise 403.

## Signature (every POST)
Meta signs the raw body with the App Secret: header `X-Hub-Signature-256: sha256=<hex HMAC>`.
`verify_signature` recomputes it with `WHATSAPP_APP_SECRET` and compares in constant time.
Unset secret, missing or wrong header → 401. (`app/whatsapp/signature.py`)

## Incoming payloads (`app/whatsapp/parse.py`)
Only `object == "whatsapp_business_account"`, `field == "messages"` are read.

| Meta message | Parsed as |
|---|---|
| `type: text` | `msg_type=text`, `text=text.body` |
| `type: interactive` (`button_reply`/`list_reply`) | `msg_type=button`, `button_id=reply.id` |
| `type: button` (template quick reply) | `msg_type=button`, `button_id=button.payload` |
| anything else (image, audio…) | `msg_type=other` |

`value.statuses` (sent/delivered/read/failed reports) are not stored; `failed` ones are logged
with Meta's error codes. Sample payloads: `tests/fixtures/`.

## Sending (`app/whatsapp/client.py`)
`POST https://graph.facebook.com/{WHATSAPP_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages`,
`Authorization: Bearer {WHATSAPP_ACCESS_TOKEN}`.

- `send_text` — text up to 4096 chars.
- `send_buttons` — interactive message, body ≤ 1024 chars, 1–3 reply buttons, titles ≤ 20 chars.
- Every send has an **idempotency key** recorded in `outbound_messages`; an already-sent key
  returns the stored id without calling Meta.
- Retries: network errors, 429 and 5xx, up to 3 attempts (1 s, 2 s backoff). Other 4xx fail at
  once (`ChannelError`), row marked `failed`.
- `WHATSAPP_DRY_RUN=true` logs the payload instead of calling Meta.

## Button ids (`app/whatsapp/buttons.py`)
`<action>:<approval_id>:<version>`, action ∈ `approve`, `edit`, `reject`. Example: `approve:12:1`.

## 24-hour window
Free-form messages (text, buttons) can only be sent within 24 h of the recipient's last message
to the bot. `owners.last_inbound_at` records that time. Outside the window Meta accepts the call
but later reports a failed delivery (error `131047`). Phase 1 will use approved **utility
templates** for messages that start a conversation (e.g. morning "N drafts ready").

## Environments
- **Dev:** Meta's free test number; recipients must be added and verified in API Setup (max 5).
- **Production (before owners use it):** real number on Solecra's WhatsApp Business Account,
  display name approval, business verification, payment method for templates.
