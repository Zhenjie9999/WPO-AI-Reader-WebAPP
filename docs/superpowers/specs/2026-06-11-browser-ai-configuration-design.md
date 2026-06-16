# Browser-managed AI configuration design

## Goal

Allow each user of Worldpanel AI Reader to configure their own OpenAI-compatible
AI service from the frontend. The application must stop using AI credentials from
`.env`. Natural-language planning and data checking must share the user's selected
AI configuration.

Worldpanel credentials remain separate and continue to use the existing login
flow.

## User experience

Add an `AI 服务` panel to the left sidebar before the Worldpanel login panel. It
contains:

- API address
- Model name
- API key, displayed as a password field
- `保存并测试` button
- `清除配置` button
- A compact connection status: not configured, testing, available, or failed

On page load, the browser restores the three values from `localStorage`. The API
key remains masked. Saving tests the configuration against the entered service
and, on success, binds it to the current Worldpanel session if one exists.

The page must clearly state that the configuration is stored in the current
browser and can be used by other people who use the same browser profile.

Clearing the configuration removes all three values from `localStorage`, clears
the fields, and removes the AI configuration from the current server session.

## Architecture

### Browser storage

The frontend stores the API address, model, and API key in one namespaced
`localStorage` record. It never renders the full API key as visible text and never
adds it to chat messages, status messages, or URLs.

The browser sends the configuration only to the local Worldpanel AI Reader
backend.

### Server session storage

The backend accepts the AI configuration through a dedicated session endpoint.
It validates the address, model, and key, then stores an `AISettings` instance
only inside the existing in-memory Worldpanel session.

The AI configuration must not be written to:

- `.env`
- runtime query caches
- report caches
- logs or exception messages
- Git or application files

When a Worldpanel session is created, it has no AI configuration until the
frontend binds the locally stored configuration. When the server restarts or the
session expires, the server-side copy disappears; the browser can bind its saved
configuration again.

### Environment configuration

`get_settings()` must no longer read `OPENAI_API_KEY` or `AI_API_KEY` for runtime
AI use. The default API address and model may be retained only as non-secret UI
defaults if desired, but they must not enable AI without a frontend-provided key.

Health responses must report only whether frontend AI configuration is supported.
They must not claim that AI is enabled because an environment key exists.

## API design

### `POST /api/ai/test`

Tests a submitted configuration without persisting it.

Request:

```json
{
  "base_url": "https://example.com/v1/chat/completions",
  "model": "gpt-5.4",
  "api_key": "secret"
}
```

Success returns a redacted status containing the provider, model, and enabled
state. Failure returns a concise message without including the API key or raw
authorization headers.

### `PUT /api/sessions/{session_id}/ai`

Tests and binds the submitted configuration to an existing Worldpanel session.
The response is redacted.

### `DELETE /api/sessions/{session_id}/ai`

Removes the AI configuration from the current server session.

If the user saves AI configuration before logging into Worldpanel, the frontend
tests it immediately and binds it automatically after the next successful login.

## AI usage flow

Natural-language Pivot planning obtains AI settings from the current Worldpanel
session. Data checking uses the same session AI settings.

If a session has no AI configuration, or the configured service fails:

- Pivot planning falls back to local rules and explicitly reports that fallback.
- Data checking returns the local check result and explicitly reports that AI
  summarization was unavailable.
- No other user's AI configuration or environment configuration is used.

The existing non-session `/api/check` behavior should remain local-only. The
frontend should send the current session ID when requesting a check so the shared
session AI can be used.

## Security and validation

- Accept only `http://` or `https://` API addresses.
- Reject empty model names and API keys.
- Never return the API key from any endpoint.
- Redact authorization values and API keys from errors and logs.
- Do not place AI configuration inside Pivot plans, execution receipts, or cache
  keys.
- Keep configurations isolated by Worldpanel session.
- Show a browser-storage warning next to the save controls.

This is a local trusted-user tool. Browser `localStorage` is intentionally chosen
for convenient restoration, with the documented risk that another person using
the same browser profile can use the stored key.

## Testing

### Unit and API tests

- Environment AI keys do not enable runtime AI.
- AI test endpoint validates required fields and redacts responses.
- Binding stores AI settings only in the requested session.
- Sessions cannot read another session's AI settings.
- Clearing removes the session configuration.
- Errors never include the submitted key.
- Pivot planning receives session AI settings.
- Data checking receives the same session AI settings.
- Missing or failed AI configuration falls back to local rules.

### Frontend tests

- Saved configuration is restored from `localStorage`.
- API key input remains a password field.
- Save tests and binds the configuration.
- Login automatically binds a previously saved configuration.
- Clear removes browser and server copies.
- Status accurately shows available, failed, and local-rule states.

### Manual acceptance

1. Open the app with an AI key still present in `.env`; verify AI remains
   unconfigured.
2. Enter a valid API address, model, and key; save and verify the available
   status.
3. Log in, prepare Data Explorer, and verify Pivot planning uses the configured
   AI.
4. Run data checking and verify it uses the same AI configuration.
5. Restart the server; reload the page and verify the browser restores and
   rebinds the saved configuration.
6. Clear the configuration and verify subsequent actions use local rules.

## Out of scope

- Server-side persistent storage or encryption of AI keys
- Synchronizing AI configuration across browsers or devices
- Multi-user accounts and permissions for the local Worldpanel AI Reader
- Supporting non-OpenAI-compatible request formats
