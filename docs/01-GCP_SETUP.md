# 01 - GCP setup (one-time, ~5 min)

You need a Google Cloud project that hosts the OAuth client. If you
already have one for the site, reuse it. Otherwise create a new one.

## 1. Pick / create a GCP project

<https://console.cloud.google.com/projectcreate>

Name it whatever (e.g. `mysite-seo-ops`).

## 2. Enable APIs

Visit each link, click **Enable**:

- [Google Search Console API](https://console.cloud.google.com/apis/library/searchconsole.googleapis.com)
- [Google Analytics Data API](https://console.cloud.google.com/apis/library/analyticsdata.googleapis.com)
- [Google Analytics Admin API](https://console.cloud.google.com/apis/library/analyticsadmin.googleapis.com)

## 3. Configure the OAuth consent screen

<https://console.cloud.google.com/apis/credentials/consent>

- **User Type**: External (unless you're on Workspace; then Internal is fine).
- **App name**: anything you'll recognize (e.g. `vibe-seo-agent`).
- **User support email**: yours.
- **Scopes**: skip (we request them at runtime).
- **Test users**: add your own Google email — the one that owns the
  GSC property and GA4 property. This bypasses the "unverified app"
  warning for you specifically.
- Save.

## 4. Create the OAuth Desktop client

<https://console.cloud.google.com/apis/credentials>

- **Create Credentials → OAuth client ID**.
- **Application type**: **Desktop app** (NOT Web — desktop uses a
  loopback redirect that works without pre-registering URIs).
- **Name**: `vibe-seo-agent`.
- Click **Create**.
- In the modal that pops up, click **DOWNLOAD JSON**.

Save the downloaded file to `credentials/gsc-oauth-secrets.json` in
this repo. `credentials/` is gitignored.

## 5. Bootstrap

```bash
python -m seo_agent.gsc_client --bootstrap
```

A browser opens. Sign in as the account that owns your GSC property
and GA4 property. You'll see two consent items:

- See Search Console data
- See and download your Google Analytics data

Click **Continue** → **Allow**. The local server captures the code,
exchanges for tokens, and writes `credentials/gsc-oauth-token.json`
(also gitignored). The terminal prints every property you have access
to. If your site shows up, you're done — every API call from here on
uses this cached refresh token, no browser ever.

## 6. Find your IDs and put them in `.env`

```bash
python -m seo_agent.gsc_client --list-sites
# Copy the line matching your site (e.g. `https://yoursite.com/`)
# → set GSC_SITE_URL in .env

python -m seo_agent.ga4_client --list-properties
# Find your site's GA4 property; copy the numeric ID
# (e.g. `properties/529645777` → 529645777)
# → set GA4_PROPERTY_ID in .env
```

## 7. Sanity check

```bash
set -a; source .env; set +a
python -m seo_agent.health_check --skip-d1
```

Expected:

```
GSC    OK    N properties visible, R rows / I impressions / C clicks for <your-url>
GA4    OK    M accounts / P properties, K/N sample paths have traffic
D1     SKIP  skipped
ANTH.  SKIP  ANTHROPIC_API_KEY not set (skipped)

OVERALL: PASS
```

Move on to `02-VPS_DEPLOY.md` to put it on a server, or `03-CUSTOMIZING.md`
to wire it into your site.

## Troubleshooting

**"This app isn't verified"** during consent: click "Advanced" →
"Go to vibe-seo-agent (unsafe)". Safe — it's your own app. Add yourself
as a Test User in the consent screen settings to skip this next time.

**Bootstrap shows zero sites**: the OAuth user isn't on any GSC
property. Add the user (yourself) to your GSC property via Search
Console → Settings → Users and permissions, then retry.

**"Failed to add user: email not found" in GSC UI**: that's the 2024
SA block. It doesn't apply here — you're adding yourself as a regular
user, not a service account. Make sure you pasted your real Google
email, not a service-account email.

**Wrong account opened the browser**: kill the bootstrap, log out of
Google in your default browser, retry. Or use Incognito.
