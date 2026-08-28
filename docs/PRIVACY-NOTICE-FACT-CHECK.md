# Privacy notice fact check — owner approval required

Date: 2026-08-28

Status: **draft findings; not approved legal text**

The current public notice should not be republished unchanged. These statements
are contradicted by the application or are broader than available evidence:

| Current statement | Verified implementation / evidence | Required correction |
| --- | --- | --- |
| “We don't train on your data.” | Photogen does not train a model, but prompts and images are sent to Google Gemini. Provider use/retention must be described under the applicable account and API terms. | Limit the statement to Photogen's own behavior and link the current provider terms approved by the owner. |
| “We never see” a browser-stored API key. | The key is sent to the Photogen server in the `X-API-Key` request header so the server can call Gemini. It is not intentionally persisted by the application. | Say the key is stored locally, transmitted to the server for each provider request, and not intentionally written to application persistence or logs. |
| Outputs are stored in `/app/data`. | Generated outputs use `CREATIVE_OUTPUT_DIR` (`/app/outputs` in the production contract); account/campaign state uses `/app/data`. | Describe both persistent stores without exposing unnecessary host implementation detail. |
| “No third-party data processor is involved.” | Google Gemini processes generation inputs; Figma, SMTP, Stripe, hosting, and infrastructure may also process scoped data when configured. | List actual processor categories and purposes; do not claim none. |
| Generated content is kept “as long as the host is running.” | Upload retention defaults to 30 days, while other records/outputs have different or incompletely documented lifecycles and backups may retain copies. | Publish a verified retention table covering uploads, outputs, campaign records, accounts, logs, email, and backups. |
| “Photogen does not set any cookies.” | Authentication uses a session token stored in browser local storage rather than a cookie today, but the notice omits campaign/account storage and third-party request behavior. | State actual browser storage and avoid categorical claims that can drift. |

## Approval needed

The owner should approve a revised notice that accurately names the controller,
purposes, data categories, processors, retention, deletion process, security
limitations, international transfers where applicable, and contact channel.
Legal review may be appropriate before public commercial use. This fact check
is product evidence, not legal advice.
