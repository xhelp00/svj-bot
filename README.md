# SVJ Bot — WhatsApp Chatbot for Homeowners Associations

A WhatsApp chatbot for Czech HOAs (SVJ — Společenství vlastníků jednotek) that answers member questions based on internal documents (bylaws, house rules, playbook).

## Architecture

```
WhatsApp → Meta Webhook → Cloud Run (FastAPI) → Gemini 2.5 Flash-Lite → WhatsApp
                                ↕
                    Google Drive (documents)
                    Secret Manager (secrets)
```

## Components

| File | Description |
|------|-------------|
| `main.py` | FastAPI app, webhook handler, admin commands |
| `whatsapp.py` | WhatsApp Business API integration (receive/send messages) |
| `llm.py` | Gemini LLM integration + relevance classifier for group messages |
| `knowledge_base.py` | System prompt builder from loaded documents |
| `drive_loader.py` | PDF and DOCX document loader from Google Drive |
| `secret_manager.py` | Secret retrieval from Google Cloud Secret Manager |
| `Dockerfile` | Container setup for Cloud Run |

## Features

- **Czech language** — responds in Czech, handles Czech input natively
- **Knowledge-based answers** — answers only from uploaded documents, never hallucinations
- **Group chat support** — intelligently decides when to respond in group conversations
- **Prompt injection protection** — refuses to reveal instructions, settings, or document names
- **Admin commands** — `!reload` to refresh knowledge base (admin-only)
- **Auto-reload** — documents refresh from Google Drive every hour
- **Secure secrets** — all credentials stored in Google Cloud Secret Manager

## Setup

### Prerequisites
- Google Cloud account with billing enabled
- Meta Developer account with WhatsApp Business API
- A phone number registered with WhatsApp Business API

### 1. Google Cloud
```bash
gcloud projects create svj-bot
gcloud config set project svj-bot
gcloud services enable run.googleapis.com secretmanager.googleapis.com drive.googleapis.com
```

### 2. Google Drive
1. Create a folder (e.g., `BOT_KNOWLEDGE`)
2. Upload your documents (PDF, DOCX, or Google Docs)
3. Share the folder with the service account email (Viewer access)

### 3. Secrets
Store each secret in Secret Manager:
```bash
echo -n "YOUR_VALUE" | gcloud secrets create SECRET_NAME --data-file=-
```

Required secrets: `GEMINI_API_KEY`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_VERIFY_TOKEN`, `GOOGLE_SERVICE_ACCOUNT_JSON`

### 4. Deploy
```bash
gcloud run deploy svj-bot \
  --source=. \
  --region=europe-west1 \
  --platform=managed \
  --allow-unauthenticated \
  --set-env-vars="GCP_PROJECT_ID=svj-bot,GOOGLE_DRIVE_FOLDER_ID=YOUR_FOLDER_ID,WHATSAPP_PHONE_NUMBER_ID=YOUR_PHONE_ID,BUILDING_NAME=YOUR_SVJ_NAME,ADMIN_PHONE=YOUR_ADMIN_NUMBER"
```

### 5. WhatsApp Webhook
In Meta Developer Console → WhatsApp → Configuration:
- **Callback URL**: `https://YOUR_SERVICE_URL/webhook`
- **Verify token**: the value you stored in `WHATSAPP_VERIFY_TOKEN`
- Subscribe to: `messages`

## Operations

### Admin commands (via WhatsApp, admin number only)
- `!reload` — immediately reload documents from Google Drive

### Update documents
1. Edit/add files in the **BOT_KNOWLEDGE** folder on Google Drive
2. Supported formats: **PDF**, **DOCX**, **Google Docs**
3. Send `!reload` via WhatsApp, or wait up to 1 hour for auto-reload

### Check logs
```bash
gcloud run services logs read svj-bot --region=europe-west1 --limit=50
```

### Update a secret
```bash
echo -n "NEW_VALUE" | gcloud secrets versions add SECRET_NAME --data-file=-
```
> Note: After updating a secret, redeploy the service — secrets are cached at startup.

## Local Development

1. Copy `.env.example` to `.env` and fill in values
2. Place `service-account.json` in the project root
3. Run:
```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```
> For local webhook testing, use [ngrok](https://ngrok.com/) to tunnel to localhost.

## Security

- **Secrets** — stored in Google Cloud Secret Manager, not in env vars or code
- **Prompt injection** — system prompt is hardened against manipulation attempts
- **Document names** — never revealed; bot references documents generically ("as per the bylaws")
- **Admin commands** — restricted to admin phone number only
- **Group messages** — bot only responds to relevant HOA questions, ignores casual chat

## Environment Variables (Cloud Run)

Non-sensitive configuration only:

| Variable | Description |
|----------|-------------|
| `GCP_PROJECT_ID` | Google Cloud project ID |
| `GOOGLE_DRIVE_FOLDER_ID` | Google Drive folder ID with documents |
| `WHATSAPP_PHONE_NUMBER_ID` | WhatsApp Business phone number ID |
| `BUILDING_NAME` | SVJ name for the system prompt |
| `ADMIN_PHONE` | Admin phone number without + (e.g., `420720994342`) |

## Secrets (Secret Manager)

| Secret | Description |
|--------|-------------|
| `GEMINI_API_KEY` | Gemini LLM API key |
| `WHATSAPP_ACCESS_TOKEN` | Permanent token from Meta System User |
| `WHATSAPP_VERIFY_TOKEN` | Webhook verification token |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service account JSON key for Google Drive |

## Cost Estimate

Estimated monthly cost at typical usage (5–20 messages/day):

| Service | Cost |
|---------|------|
| Gemini API | ~$0.05–0.10 |
| Cloud Run | $0.00 (free tier) |
| Secret Manager | $0.00 (free tier, <10K accesses) |
| Google Drive API | $0.00 |
| **Total** | **~$0.05–0.10/month** |

## License

MIT — see [LICENSE](LICENSE).
