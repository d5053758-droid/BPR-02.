# BPR-02 mobile screenshot -> WhatsApp

## What this provides

Because an ordinary ChatGPT conversation cannot directly invoke an arbitrary outbound webhook from a user's image upload, this repository provides a separate, phone-friendly upload page. Open it, select a chart screenshot, and tap **Analizar captura y enviar WhatsApp**. The service receives the actual image bytes, sends them to OpenAI's vision-capable Responses API for analysis, validates the resulting structured fields, and only then sends an approved WhatsApp template message.

This changes one part of the experience: the screenshot must be uploaded to the BPR-02 page, not to the ordinary ChatGPT conversation. It cannot automatically react to an image uploaded in this chat. The ChatGPT account's vision subscription is not reused by this page; OpenAI API usage is billed separately under the API account. See [OpenAI image and vision inputs](https://developers.openai.com/api/docs/guides/images-vision) and [current API pricing](https://platform.openai.com/pricing).

The endpoint does not accept a text-only alert as a substitute for an image. `POST /v1/analyze-image` requires a base64 PNG/JPEG/WEBP data URL and an explicit `image_attached: true` assertion; the server validates the image signature and byte size, performs visual analysis, and sends nothing if analysis fails. The vision prompt defaults to NO TRADE if the screenshot is unclear, stale, not a chart, or lacks enough visible evidence. The app does not place or manage orders.

## 1. WhatsApp Business configuration

In Meta's WhatsApp Business Platform / Cloud API:

1. Set up a business sender and get its **Phone Number ID** (different from the displayed phone number).
2. Create and obtain approval for a template, e.g. `bpr02_image_analysis`, with exactly one body text placeholder `{{1}}`.
3. Confirm that the intended recipient has opted in to receive these notifications.
4. Save the access token securely; do not paste credentials into this conversation or commit them to GitHub.

Required variables:

- `WHATSAPP_ACCESS_TOKEN`: Meta API access token.
- `WHATSAPP_PHONE_NUMBER_ID`: numeric sender Phone Number ID.
- `WHATSAPP_RECIPIENT`: recipient in international format, e.g. `+54911...`.
- `WHATSAPP_TEMPLATE_NAME`: approved template name.
- `WHATSAPP_TEMPLATE_LANGUAGE`: approved language; defaults to `es_AR`.
- `WHATSAPP_API_VERSION`: Graph API version; defaults to `v26.0`.

Messages initiated by the business and those outside the customer service window must follow Meta's current template and consent rules. See the [official WhatsApp Cloud API docs](https://developers.facebook.com/docs/whatsapp/cloud-api).

## 2. OpenAI vision access

Create an API key in the OpenAI API platform and configure it as a secret:

- `OPENAI_API_KEY`: private API key.
- `OPENAI_VISION_MODEL`: defaults to `gpt-6.1-terra`, a model with image-input support. You can choose another enabled vision-capable model after checking the model catalogue.

This key is separate from ChatGPT Free/Plus/Pro access. API charges apply per use and are not included in the ChatGPT subscription. Begin with a few test screenshots and review the usage/budget settings before using it regularly.

## 3. Deploy the web service

The repository includes a Render Blueprint in `render.yaml`.

1. Open Render and create a new Blueprint from the repository `d5053758-droid/BPR-02.`.
2. In the setup/deployment form, enter these secrets:
   - `BPR02_IMAGE_ALERT_ACTION_TOKEN`: generate a long random private value. This is the password required to use the upload page.
   - `OPENAI_API_KEY`
   - `WHATSAPP_ACCESS_TOKEN`
   - `WHATSAPP_PHONE_NUMBER_ID`
   - `WHATSAPP_RECIPIENT`
   - `WHATSAPP_TEMPLATE_NAME`
3. Confirm the default values `OPENAI_VISION_MODEL=gpt-6.1-terra`, `WHATSAPP_TEMPLATE_LANGUAGE=es_AR`, and `WHATSAPP_API_VERSION=v26.0` match your actual account and approved template.
4. Deploy the service. Check `https://YOUR-SERVICE.onrender.com/health`; it should return `status: ok`. This health check does not send a WhatsApp message.

The blueprint currently targets a free Render service for a proof of concept. Free instances may sleep after inactivity, and their local file system is ephemeral. For dependable repeated use and robust duplicate suppression, move to an always-on service with durable storage before relying on it.

## 4. Use it from your phone

1. Open `https://YOUR-SERVICE.onrender.com/`.
2. Enter the private `BPR02_IMAGE_ALERT_ACTION_TOKEN` you configured for the service.
3. Select a current screenshot of the chart (PNG, JPG or WEBP; maximum 4 MB).
4. Check the preview, then tap **Analizar captura y enviar WhatsApp**.
5. Wait for the result. The page displays the analysis and whether Meta accepted the WhatsApp request.

If you do not choose a screenshot, the action remains disabled. If the image is rejected, the vision call fails, or the analysis is invalid, the service does not send a WhatsApp message. If the API succeeds but WhatsApp rejects the message, the page reports the failure instead of claiming delivery. Meta acceptance alone does not prove that the message reached or was read on the handset.

The page sends the image directly to this service over HTTPS; it does not upload it to GitHub. The service forwards the image to OpenAI for analysis and uses the result to compose the WhatsApp message. Avoid uploading screenshots that expose passwords, account identifiers, or other private information.

## 5. Testing and limits

Run the offline tests:

```bash
python -m pip install -e ".[test]"
python -m pytest
```

The tests mock both vision and message sending, so they do not call OpenAI or Meta and do not send real WhatsApp messages. Once deployed, first use a test recipient and a template that is actually approved in your Meta account.

The vision model can misread small text, candles, labels, prices, or chart structure. A screenshot is only a snapshot and does not prove that the same price remains available. Treat the message as a manually reviewed research note, never as a guaranteed prediction or an instruction that an order was placed.
