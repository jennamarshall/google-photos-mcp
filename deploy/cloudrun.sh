#!/usr/bin/env bash
# Deploy the Google Photos connector to Google Cloud Run as a remote connector
# you can add in claude.ai (and so use from the Claude mobile apps).
#
# Run from the repo root, ideally in Google Cloud Shell (it has gcloud already):
#   ALLOWED_EMAILS=you@gmail.com ./deploy/cloudrun.sh
#
# Optional: PROJECT_ID (default: gcloud's current project), REGION (default us-central1),
#           SERVICE (default google-photos-mcp)
set -euo pipefail

: "${ALLOWED_EMAILS:?Set ALLOWED_EMAILS to the Google account(s) allowed to use the connector}"
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-google-photos-mcp}"
[ -n "$PROJECT_ID" ] || { echo "No project: run 'gcloud config set project YOUR_PROJECT'"; exit 1; }

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
URL="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"

echo "==> Enabling APIs in $PROJECT_ID"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com photoslibrary.googleapis.com photospicker.googleapis.com \
  --project "$PROJECT_ID"

cat <<MSG

==> Create a Google OAuth client for the hosted connector (one time):
    https://console.cloud.google.com/apis/credentials?project=$PROJECT_ID
    Create credentials -> OAuth client ID -> Application type: "Web application"
    Authorized redirect URI:

        $URL/oauth/google/callback

    (The OAuth consent screen must list your account under Test users.)

MSG
read -r -p "Paste the OAuth client ID: " GOOGLE_OAUTH_CLIENT_ID
read -r -s -p "Paste the OAuth client secret (hidden): " GOOGLE_OAUTH_CLIENT_SECRET; echo

put_secret() {  # name value
  if gcloud secrets describe "$1" --project "$PROJECT_ID" >/dev/null 2>&1; then
    printf '%s' "$2" | gcloud secrets versions add "$1" --data-file=- --project "$PROJECT_ID" >/dev/null
  else
    printf '%s' "$2" | gcloud secrets create "$1" --data-file=- --replication-policy=automatic --project "$PROJECT_ID" >/dev/null
  fi
}
echo "==> Storing secrets in Secret Manager"
put_secret google-photos-mcp-oauth-secret "$GOOGLE_OAUTH_CLIENT_SECRET"
if ! gcloud secrets describe google-photos-mcp-server-secret --project "$PROJECT_ID" >/dev/null 2>&1; then
  put_secret google-photos-mcp-server-secret "$(head -c 48 /dev/urandom | base64 | tr -d '\n/+=')"
fi
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
for s in google-photos-mcp-oauth-secret google-photos-mcp-server-secret; do
  gcloud secrets add-iam-policy-binding "$s" --member "serviceAccount:$RUNTIME_SA" \
    --role roles/secretmanager.secretAccessor --project "$PROJECT_ID" >/dev/null
done

echo "==> Building and deploying (takes a few minutes)"
gcloud run deploy "$SERVICE" --source . --region "$REGION" --project "$PROJECT_ID" \
  --allow-unauthenticated --min-instances 0 --max-instances 3 --memory 512Mi \
  --set-env-vars "^@^PUBLIC_URL=$URL@GOOGLE_OAUTH_CLIENT_ID=$GOOGLE_OAUTH_CLIENT_ID@ALLOWED_EMAILS=$ALLOWED_EMAILS" \
  --set-secrets "GOOGLE_OAUTH_CLIENT_SECRET=google-photos-mcp-oauth-secret:latest,SERVER_SECRET=google-photos-mcp-server-secret:latest"

ACTUAL="$(gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT_ID" --format='value(status.url)')"
if [ "$ACTUAL" != "$URL" ]; then
  echo "!! Cloud Run gave the URL $ACTUAL rather than $URL."
  echo "   Updating PUBLIC_URL; also change the OAuth client's redirect URI to $ACTUAL/oauth/google/callback"
  gcloud run services update "$SERVICE" --region "$REGION" --project "$PROJECT_ID" \
    --update-env-vars "PUBLIC_URL=$ACTUAL" >/dev/null
  URL="$ACTUAL"
fi
curl -fsS "$URL/healthz" >/dev/null && echo "==> Health check OK"

cat <<MSG

Done. Add it to Claude:
  claude.ai -> Settings -> Connectors -> Add custom connector
  URL:  $URL/mcp

Then connect it (sign in with Google). It will show up in the Claude mobile app too.
MSG
