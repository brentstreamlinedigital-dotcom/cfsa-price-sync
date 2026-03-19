# CFSA Price Sync — Setup Guide

## What's already done
- Full Python codebase built and tested (31/31 tests passing)
- All 11 supplier configs calibrated with real column headers
- 164 products normalized from existing Excel — `~/Downloads/cfsa_master_import.csv` ready

---

## Step 1 — Create Google Sheet (10 min)

1. Go to https://sheets.google.com → create a new blank spreadsheet
2. Name it: **CFSA Master**
3. **File → Import → Upload** → select `~/Downloads/cfsa_master_import.csv`
4. Choose: Import to **new sheet**, name it `master`
5. Copy the **Spreadsheet ID** from the URL:
   `https://docs.google.com/spreadsheets/d/YOUR_SPREADSHEET_ID/edit`

---

## Step 2 — Create GCP Project (15 min)

1. Go to https://console.cloud.google.com/projectcreate
2. Project name: `cfsa-price-sync`
3. After creation, copy the **Project ID** (e.g. `cfsa-price-sync-123456`)
4. Enable APIs (run these after gcloud is authenticated):
   ```bash
   gcloud services enable \
     sheets.googleapis.com \
     drive.googleapis.com \
     gmail.googleapis.com \
     firestore.googleapis.com \
     run.googleapis.com \
     secretmanager.googleapis.com \
     cloudbuild.googleapis.com \
     --project=YOUR_PROJECT_ID
   ```

---

## Step 3 — Create Service Account (10 min)

In Cloud Console → IAM → Service Accounts → Create:
- Name: `cfsa-sync`
- Roles:
  - Editor (or: Firebase Admin + Secret Manager Accessor + Cloud Run Invoker)
- Create and download **JSON key** → save as `sa-key.json` (never commit this!)
- Share the Google Sheet with the service account email:
  `cfsa-sync@YOUR_PROJECT_ID.iam.gserviceaccount.com` → **Editor** access

OR via gcloud:
```bash
gcloud iam service-accounts create cfsa-sync \
  --display-name="CFSA Price Sync" \
  --project=YOUR_PROJECT_ID

gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:cfsa-sync@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/editor"

gcloud iam service-accounts keys create sa-key.json \
  --iam-account=cfsa-sync@YOUR_PROJECT_ID.iam.gserviceaccount.com
```

---

## Step 4 — Gmail API Setup (10 min)

1. In Cloud Console → APIs & Services → Gmail API → Enable
2. The service account needs **domain-wide delegation** OR use your personal Gmail:
   - Go to https://myaccount.google.com/permissions → allow the service account
   - OR: In Google Workspace admin → Security → API Controls → Domain-wide Delegation
     Add the service account client ID with scope:
     `https://www.googleapis.com/auth/gmail.modify`

---

## Step 5 — Shopify Access Token (5 min)

1. Shopify Admin → Settings → Apps and sales channels → Develop apps
2. Create app: `CFSA Price Sync`
3. Configure Admin API scopes: `write_products`, `write_inventory`, `read_products`
4. Install app → copy the **Admin API access token** (shown once)

Store the token in Secret Manager:
```bash
echo -n "shpat_YOUR_TOKEN" | gcloud secrets create SHOPIFY_ACCESS_TOKEN \
  --data-file=- \
  --project=YOUR_PROJECT_ID
```

---

## Step 6 — Link Shopify Products to Master Sheet (30 min, one-time)

1. Shopify Admin → Products → Export → All products (CSV)
2. Open the CSV — note columns `ID` (product ID) and `Variant ID`
3. In the master Google Sheet, columns M and N:
   - M = `shopify_product_id` → paste `gid://shopify/Product/ID`
   - N = `shopify_variant_id` → paste `gid://shopify/ProductVariant/VARIANT_ID`

Tip: Use VLOOKUP on SKU to match rows if ordering differs.

---

## Step 7 — Update app.yaml

Edit `config/app.yaml`:
```yaml
google:
  project_id: YOUR_PROJECT_ID          # from Step 2
  sheets:
    spreadsheet_id: YOUR_SPREADSHEET_ID  # from Step 1

shopify:
  shop_domain: YOUR_STORE.myshopify.com  # e.g. camping-fridge-sa.myshopify.com

firebase:
  # (uses same GCP project — just works after Step 2)
```

And update `location_id` in each `config/suppliers/*.yaml`:
```bash
# Get your Shopify location ID:
# Shopify Admin → Settings → Locations → click your location
# ID is in the URL: /admin/locations/1234567890
# Format as: gid://shopify/Location/1234567890
```

---

## Step 8 — Test locally

```bash
cd cfsa-price-sync
export GOOGLE_APPLICATION_CREDENTIALS=sa-key.json
export SHOPIFY_ACCESS_TOKEN=shpat_xxxx

# Dry run — parses emails + diffs, no writes
python -m src.main --dry-run

# Single supplier test
python -m src.main --supplier engel --dry-run

# Full import from Excel (one-time)
python -m src.initial_import \
  --excel "/path/to/Camping Fridge SA Pricelist.xlsx" \
  --spreadsheet-id YOUR_SPREADSHEET_ID
```

---

## Step 9 — Deploy to Cloud Run (15 min)

```bash
# Authenticate
gcloud auth configure-docker
gcloud auth login

# Build and push
docker build -t gcr.io/YOUR_PROJECT_ID/cfsa-price-sync:latest .
docker push gcr.io/YOUR_PROJECT_ID/cfsa-price-sync:latest

# Create Cloud Run Job
gcloud run jobs create cfsa-price-sync \
  --image gcr.io/YOUR_PROJECT_ID/cfsa-price-sync:latest \
  --region europe-west1 \
  --service-account cfsa-sync@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --set-secrets "SHOPIFY_ACCESS_TOKEN=SHOPIFY_ACCESS_TOKEN:latest" \
  --memory 1Gi \
  --cpu 1 \
  --max-retries 2 \
  --task-timeout 1800 \
  --project YOUR_PROJECT_ID

# Test run
gcloud run jobs execute cfsa-price-sync \
  --region europe-west1 \
  --args="--trigger=manual,--dry-run" \
  --wait
```

---

## Step 10 — GitHub Actions (5 min)

Add these secrets in GitHub → Settings → Secrets:
- `GCP_SA_KEY` — contents of `sa-key.json`
- `GCP_PROJECT_ID` — your project ID
- `CLOUD_RUN_JOB_URL` — from Cloud Run job details

The workflow in `.github/workflows/daily_sync.yaml` will then auto-trigger at 07:00 and 13:00 SAST.

---

## Quick reference — replace these placeholders everywhere

| Placeholder | Your value |
|---|---|
| `YOUR_PROJECT_ID` | e.g. `cfsa-price-sync-123456` |
| `YOUR_SPREADSHEET_ID` | from Google Sheets URL |
| `YOUR_STORE.myshopify.com` | e.g. `camping-fridge-sa.myshopify.com` |
| `YOUR_LOCATION_ID` | from Shopify Locations URL |
| `gid://shopify/Location/YOUR_LOCATION_ID` | full GID format |
