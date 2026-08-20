#!/usr/bin/env bash
#
# Deploy InvoiceOps AI to Google Cloud Run + Cloud SQL (PostgreSQL) in one command.
#
# One-time prereqs:
#   - Install gcloud:            https://cloud.google.com/sdk/docs/install
#   - Log in:                    gcloud auth login
#   - Make sure billing is enabled on the project.
#   - Have your Gemini API key ready (the script prompts if GEMINI_API_KEY is unset).
#
# Usage:
#   GEMINI_API_KEY=sk-... ./deploy/gcp-cloudrun.sh
#   # or just run it and paste the key when prompted:
#   ./deploy/gcp-cloudrun.sh
#
# Re-running is safe: the instance/db/secrets are reused and the service is redeployed.
#
set -euo pipefail

# ---- Config (override any of these via env vars) ----------------------------
PROJECT_ID="${PROJECT_ID:-project-79819fef-b9e3-4bcd-99e}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-invoiceops-ai}"
DB_INSTANCE="${DB_INSTANCE:-invoiceops-sql}"
DB_TIER="${DB_TIER:-db-f1-micro}"
DB_NAME="${DB_NAME:-invoiceops}"
DB_USER="${DB_USER:-invoiceops}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.5-flash}"
BASIC_AUTH_USERNAME="${BASIC_AUTH_USERNAME:-admin}"

DB_URL_SECRET="invoiceops-database-url"
GEMINI_SECRET="gemini-api-key"
BASIC_AUTH_SECRET="basic-auth-password"

echo "==> Project: $PROJECT_ID | Region: $REGION | Service: $SERVICE"
gcloud config set project "$PROJECT_ID" >/dev/null

# ---- 1. Enable required APIs ------------------------------------------------
echo "==> Enabling APIs..."
gcloud services enable \
  run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com

# ---- 2. Cloud SQL: instance / database / user -------------------------------
if ! gcloud sql instances describe "$DB_INSTANCE" >/dev/null 2>&1; then
  echo "==> Creating Cloud SQL instance '$DB_INSTANCE' (a few minutes on first run)..."
  gcloud sql instances create "$DB_INSTANCE" \
    --database-version=POSTGRES_16 --edition=ENTERPRISE --tier="$DB_TIER" \
    --region="$REGION" --storage-size=10 --storage-auto-increase
else
  echo "==> Cloud SQL instance '$DB_INSTANCE' already exists."
fi

gcloud sql databases describe "$DB_NAME" --instance="$DB_INSTANCE" >/dev/null 2>&1 \
  || gcloud sql databases create "$DB_NAME" --instance="$DB_INSTANCE"

# Generate a URL-safe password and (re)set it so it always matches the secret.
DB_PASSWORD="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | cut -c1-24)"
if gcloud sql users list --instance="$DB_INSTANCE" --format='value(name)' | grep -qx "$DB_USER"; then
  gcloud sql users set-password "$DB_USER" --instance="$DB_INSTANCE" --password="$DB_PASSWORD"
else
  gcloud sql users create "$DB_USER" --instance="$DB_INSTANCE" --password="$DB_PASSWORD"
fi

CONN_NAME="$(gcloud sql instances describe "$DB_INSTANCE" --format='value(connectionName)')"
echo "==> Cloud SQL connection name: $CONN_NAME"

# ---- 3. Secrets (DATABASE_URL + Gemini key) ---------------------------------
# psycopg2 talks to Cloud SQL over the mounted unix socket at /cloudsql/<conn>.
DATABASE_URL="postgresql+psycopg2://${DB_USER}:${DB_PASSWORD}@/${DB_NAME}?host=/cloudsql/${CONN_NAME}"

upsert_secret() {  # $1=name $2=value
  if gcloud secrets describe "$1" >/dev/null 2>&1; then
    printf '%s' "$2" | gcloud secrets versions add "$1" --data-file=-
  else
    printf '%s' "$2" | gcloud secrets create "$1" --data-file=- --replication-policy=automatic
  fi
}

echo "==> Storing DATABASE_URL secret ($DB_URL_SECRET)..."
upsert_secret "$DB_URL_SECRET" "$DATABASE_URL"

if [ -z "${GEMINI_API_KEY:-}" ]; then
  read -r -s -p "Enter GEMINI_API_KEY: " GEMINI_API_KEY; echo
fi
echo "==> Storing GEMINI_API_KEY secret ($GEMINI_SECRET)..."
upsert_secret "$GEMINI_SECRET" "$GEMINI_API_KEY"

# Shared login for the public site (HTTP Basic Auth). Username is a plain env
# var; the password is stored as a secret.
if [ -z "${BASIC_AUTH_PASSWORD:-}" ]; then
  read -r -s -p "Set a site password for user '${BASIC_AUTH_USERNAME}': " BASIC_AUTH_PASSWORD; echo
fi
echo "==> Storing site password secret ($BASIC_AUTH_SECRET)..."
upsert_secret "$BASIC_AUTH_SECRET" "$BASIC_AUTH_PASSWORD"

# ---- 4. Grant the Cloud Run runtime service account access ------------------
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
# The same default compute SA is also used by Cloud Build for "deploy from
# source", so it needs cloudbuild.builds.builder (source bucket + Artifact
# Registry + logging) in addition to the runtime roles.
echo "==> Granting build + runtime roles to $RUNTIME_SA ..."
for role in \
  roles/cloudbuild.builds.builder \
  roles/cloudsql.client \
  roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${RUNTIME_SA}" --role="$role" --condition=None >/dev/null
done

# ---- 5. Deploy to Cloud Run from source -------------------------------------
echo "==> Building + deploying '$SERVICE' to Cloud Run..."
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --add-cloudsql-instances "$CONN_NAME" \
  --set-env-vars "LLM_PROVIDER=gemini,GEMINI_MODEL=${GEMINI_MODEL},BASIC_AUTH_USERNAME=${BASIC_AUTH_USERNAME}" \
  --set-secrets "DATABASE_URL=${DB_URL_SECRET}:latest,GEMINI_API_KEY=${GEMINI_SECRET}:latest,BASIC_AUTH_PASSWORD=${BASIC_AUTH_SECRET}:latest" \
  --cpu 1 --memory 1Gi --min-instances 0 --max-instances 3 --timeout 300

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo
echo "============================================================"
echo " Deployed. Shareable link:"
echo "   ${URL}/ui/"
echo
echo " Login (share with your users):"
echo "   username: ${BASIC_AUTH_USERNAME}"
echo "   password: (the site password you set)"
echo "============================================================"
echo
echo "Optional (only needed for the manual template-matching upload flow, which"
echo "uses Postgres pg_trgm — the autonomous agent flow does NOT need it):"
echo "   gcloud sql connect ${DB_INSTANCE} --user=${DB_USER} --database=${DB_NAME}"
echo "   then run:  CREATE EXTENSION IF NOT EXISTS pg_trgm;"
