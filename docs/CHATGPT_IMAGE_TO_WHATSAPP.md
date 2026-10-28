# Image-driven ChatGPT -> WhatsApp alerts

This service is for the specific workflow in which a user uploads a chart screenshot to a configured Custom GPT, the GPT visually analyzes that image, and only then calls the API to send the result to the user's WhatsApp. It is **not** a continuous market-alert service. A text-only chat turn must not call the action.

The API receives the GPT's *analysis*, not the original image. Visual inspection happens in ChatGPT; the service cannot independently inspect or prove that a screenshot was attached. The Custom GPT instructions and an authenticated action token are therefore part of the safety boundary.

## Architecture

1. User uploads an image of the trading chart to the configured Custom GPT.
2. The GPT inspects the image and prepares a cautious result in the required structure.
3. The GPT action calls `POST /v1/image-analysis`, authenticated with `BPR02_IMAGE_ALERT_ACTION_TOKEN`, and passes `image_attached: true`.
4. The endpoint validates the payload and sends the analysis using Meta's official WhatsApp Cloud API.
5. The endpoint responds with the delivery request status. Meta's acceptance is not proof the handset received/read the message.

No screenshot means no call to this action and therefore no WhatsApp message. The endpoint also rejects a payload that lacks `image_attached: true` or the `chatgpt_image_analysis` source label. It accepts `CALL`, `PUT`, or `NO TRADE`, so a visible but inconclusive chart can still produce a useful NO TRADE report. It never submits a trading order.

**Important boundary:** this does not turn the existing ordinary chat into an outgoing WhatsApp integration. For ChatGPT to call an external API action, configure the OpenAPI schema in a Custom GPT that supports Actions, then upload screenshots in that GPT. The ordinary chat in which these files are being edited does not gain the action automatically. See [OpenAI's GPT Actions overview](https://developers.openai.com/api/docs/actions/introduction) and [configuration guide](https://help.openai.com/en/articles/9442513-configuring-actions-in-gpts).

**Account limitation (checked 28 October 2026):** OpenAI's current Help Center says personal ChatGPT accounts—including Free, Go, Plus and Pro—cannot create or publish new GPTs. If you do not already have an eligible Custom GPT that can be edited, this route cannot be activated from a personal account alone. In that case, a separate mobile-friendly upload app backed by the vision/API service is the practical alternative, but it would not be this same chat. See [OpenAI's current GPT creation availability](https://help.openai.com/en/articles/8554397-creating-and-editing-gpts).

## Configure WhatsApp Cloud API first

In Meta's WhatsApp Business Platform:

1. Set up a WhatsApp Business/Cloud API sender and identify its **Phone Number ID**.
2. Create and obtain approval for a message template, such as `bpr02_image_analysis`, with exactly one body text placeholder `{{1}}`.
3. Confirm the recipient has opted in to receive the messages.
4. Keep access tokens and other credentials out of source control and conversation messages.

The current template sender puts the full compact analysis into that one template variable. The template name and language configured here must match a template approved in the Meta account. Messages initiated by the business are subject to WhatsApp's current template and customer-service-window rules. See [Meta's WhatsApp Cloud API documentation](https://developers.facebook.com/docs/whatsapp/cloud-api).

Required environment variables:

- `BPR02_IMAGE_ALERT_ACTION_TOKEN`: a long random secret used to authenticate the GPT action. Configure the same value in the Custom GPT Action's Bearer API Key authentication.
- `WHATSAPP_ACCESS_TOKEN`: secret Meta access token.
- `WHATSAPP_PHONE_NUMBER_ID`: numeric sender Phone Number ID.
- `WHATSAPP_RECIPIENT`: recipient number in international format, e.g. `+54911...`.
- `WHATSAPP_TEMPLATE_NAME`: approved template name.
- `WHATSAPP_TEMPLATE_LANGUAGE`: approved template language, default `es_AR`.
- `WHATSAPP_API_VERSION`: Graph API version, default `v26.0`.

Do not paste any of these secrets into this chat or commit them to GitHub.

## Deploy the service

A Render Blueprint is included in the repository as `render.yaml`. Use the Render Dashboard to create a Blueprint/Web Service from this repository and enter every `sync: false` variable securely when prompted. Once deployment finishes, check `https://YOUR-SERVICE.onrender.com/health`; it should return JSON with `status: ok`. That health check never sends a WhatsApp message.

The included free Render configuration is intended for an initial experiment only. Render's free web services spin down after 15 minutes idle and wake on a request; their local files are ephemeral, so the duplicate-ID journal may be lost on restarts/sleep/redeploy. See [Render's free-service limitations](https://render.com/docs/free). Do not count on exactly-once delivery using only ephemeral local storage. For reliable ongoing use, deploy on an always-on service with durable storage or add a persistent database first.

## Configure the Custom GPT Action

1. In a Custom GPT that supports Actions, open its editor and choose **Actions -> Create new action**.
2. Copy the OpenAPI schema from `openapi/chatgpt-image-whatsapp.yaml`.
3. Replace the placeholder under `servers[0].url` with the deployed HTTPS service URL, for example `https://YOUR-SERVICE.onrender.com`.
4. Set action authentication to **API Key -> Bearer** and enter the exact value of `BPR02_IMAGE_ALERT_ACTION_TOKEN`.
5. Test the action in the editor with a synthetic request. Do not use a real WhatsApp recipient for initial tests until the account/template is ready.

### Custom GPT instructions

Add the following instruction to the Custom GPT. Keep it alongside the user's usual chart-analysis instructions:

> IMAGE-ONLY WHATSAPP RULE: Never call `sendImageAnalysisToWhatsApp` unless the current user message contains a visible trading-chart screenshot and you have actually inspected that screenshot. Do not call it for text-only turns, a screenshot discussed in an earlier turn, general questions, or live market events. After analyzing the image, always return your analysis in ChatGPT as usual and call the action once with the concise result for that image. Use signal CALL, PUT, or NO TRADE. If evidence or chart labels are unclear, use NO TRADE and say why; do not invent prices, levels, confirmations, or expiries. Set `image_attached` to true only in that screenshot turn and set `analysis_source` to `chatgpt_image_analysis`. Generate a new `analysis_id` for each uploaded screenshot and reuse the same ID only if retrying that same notification. Do not send a message for any turn without a chart image. Do not imply that an order was placed.

### Payload sent by the action

The action does not upload chart pixels. It passes the result of the image analysis, e.g.:

```json
{
  "analysis_id": "2ac537a3-2957-47bd-9e19-bpr02-example-001",
  "image_attached": true,
  "analysis_source": "chatgpt_image_analysis",
  "symbol": "EURUSD_otc",
  "timeframe": "M1",
  "signal": "NO TRADE",
  "reason": "La captura no muestra una confirmación suficiente.",
  "entry_trigger": "Esperar el cierre de vela y una recuperación confirmada.",
  "expiry": "No aplicable sin confirmación.",
  "confirmation": "Cierre y retesteo visibles.",
  "invalidation": "La estructura visible deja de ser válida."
}
```

The timestamp on the WhatsApp message is generated by the service when the action request is received; the model should not fabricate a timestamp. The endpoint validates the payload and auth token, limits request size, avoids request-body/token logs, and never passes requests to any trading platform.

## Test and run locally

Install the project and tests:

```bash
python -m pip install -e ".[test]"
python -m pytest
```

Run the HTTP service only after supplying all required environment variables:

```bash
python -m bpr02.image_alert_api
```

It listens on the port configured by `PORT` (default `8080`). In hosted use, the service must be behind HTTPS. Unit tests use a local loopback server and a fake notifier; they do not call Meta or send real WhatsApp messages.

## Security and limitations

- The endpoint cannot independently prove that ChatGPT saw an image. That rule depends on the configured GPT instructions; a holder of the bearer token can call the endpoint directly.
- Anyone who has access to the Action configuration and its token can potentially send messages. Rotate the token if it is exposed.
- `accepted_by_meta` means Meta accepted the message API request, not that a WhatsApp recipient received or read it.
- The WhatsApp account must be properly configured, the recipient must have opted in, and the template/language must match Meta's configuration.
- This is a report of visual chart interpretation, not a promise of future price movement or a trading order. Screenshots are snapshots and may be stale, cropped, or ambiguous.
