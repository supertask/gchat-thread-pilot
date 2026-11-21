"""DWD (domain-wide delegation) impersonation for the Google Chat plugin.

Pattern (vercel/chat adapter-gchat ``impersonateUser``, Hermes issue
#150819): a JWT whose ``sub`` claim is the delegated user's email is
exchanged for a USER-identity token for the requested scopes.

Keyless variant (this org): ``iam.disableServiceAccountKeyCreation`` /
``iam.managed.disableServiceAccountKeyCreation`` block SA private keys,
so the JWT is signed remotely by the IAM Credentials ``signJwt`` API
using the bot's own service account identity, then exchanged at the
OAuth2 token endpoint — the exact two-step google-auth performs locally
with ``with_subject()`` when an SA key exists. Verified live 2026-11-20:
signJwt 200 → token exchange OK → ``spaces.messages.list`` with a
``thread.name`` filter returned 200 (the app-identity 403 is bypassed).

Scopes granted in the admin console (ドメイン全体の委任, client <YOUR_DWD_CLIENT_ID>): ``chat.messages.readonly`` +
``chat.spaces.readonly``. The console rejects ``workspace.events`` with
「スコープが正しくありません」, so it is NOT requested; Chat message
subscriptions are authorised by the chat scopes (Workspace Events auth
guide: user-auth message events accept chat.messages(.readonly)).

This is the escape hatch for the 403 the bot's own app identity gets on
``spaces.messages.list`` (admin-approved readonly scope not reflected in
the API): with DWD the pilot reads history AS the delegated user under
``chat.messages.readonly`` instead of AS the app under
``chat.app.messages.readonly``.

Config (highest priority first):
  1. ``platforms.google_chat.extra.impersonate_user`` in config.yaml
  2. ``GOOGLE_CHAT_IMPERSONATE_USER`` env var

Unset → None → the adapter keeps plain app authentication (behaviour
unchanged). Set but unusable (ADC authorized_user / metadata creds have
no signer to sign the DWD JWT) → the adapter logs a warning and falls
back to app authentication too; DWD is never fatal.
"""

import datetime
import json
import os
import time

import google.auth.credentials as _gac

TOKEN_URI = "https://oauth2.googleapis.com/token"
SIGN_JWT_URL = ("https://iamcredentials.googleapis.com/v1/projects/-/"
                "serviceAccounts/{sa}:signJwt")

# User scopes granted to the delegated token.
#  - messages.readonly drives the thread-pilot history read (thread_pilot.py)
#  - spaces.readonly lists the spaces events_sub.py subscribes to
#  - workspace.events is NOT granted (admin console rejects it)
CHAT_MESSAGES_READONLY = "https://www.googleapis.com/auth/chat.messages.readonly"
CHAT_SPACES_READONLY = "https://www.googleapis.com/auth/chat.spaces.readonly"
# Rejected by the admin console (「スコープが正しくありません」) — kept only
# as documentation; do not add to DWD_SCOPES.
WORKSPACE_EVENTS = "https://www.googleapis.com/auth/workspace.events"

DWD_SCOPES = [CHAT_MESSAGES_READONLY, CHAT_SPACES_READONLY]


def impersonate_user(extra=None) -> "str | None":
    """Resolve the delegated user's email; ``None`` when not configured.

    ``extra`` is the adapter's ``PlatformConfig.extra`` (seeded from
    config.yaml ``platforms.google_chat.extra``). Falls back to the
    ``GOOGLE_CHAT_IMPERSONATE_USER`` env var.
    """
    value = None
    if isinstance(extra, dict):
        value = extra.get("impersonate_user")
    value = value or os.getenv("GOOGLE_CHAT_IMPERSONATE_USER") or ""
    value = str(value).strip()
    return value or None


def build_dwd_credentials(base_credentials, subject, scopes=None):
    """Return keyless DWD credentials carrying ``subject``, or ``None``.

    Signs the DWD JWT via IAM ``signJwt`` (the bot SA's system-managed
    key), so no SA private-key JSON is required — only credentials that
    can call the IAM API (the user-ADC impersonating the bot SA works).
    Raises ValueError when the bot SA email cannot be determined.
    """
    if not subject:
        return None
    sa_email = (getattr(base_credentials, "service_account_email", None)
                or os.getenv("GOOGLE_CHAT_SA_EMAIL", ""))
    sa_email = str(sa_email).strip()
    if not sa_email:
        raise ValueError(
            "DWD needs the bot SA email: base credentials carry no "
            "service_account_email and GOOGLE_CHAT_SA_EMAIL is unset.")
    return DwdKeylessCredentials(
        base_credentials, sa_email, subject, scopes or DWD_SCOPES)


class DwdKeylessCredentials(_gac.Credentials):
    """User-identity token minted via IAM ``signJwt`` — no SA private key.

    Attributes ``service_account_email`` and ``_subject`` mirror
    google.oauth2.service_account.Credentials so thread_pilot._identity
    classifies these as 'user' credentials unchanged.
    """

    def __init__(self, base_credentials, service_account_email, subject, scopes):
        super().__init__()
        self._base = base_credentials
        self.service_account_email = service_account_email
        self._subject = subject
        self._scopes = list(scopes or [])

    def with_scopes(self, scopes):
        return DwdKeylessCredentials(
            self._base, self.service_account_email, self._subject, scopes)

    def refresh(self, request=None):
        from google.auth.transport.requests import AuthorizedSession
        now = int(time.time())
        payload = json.dumps({
            "iss": self.service_account_email,
            "sub": self._subject,
            "scope": " ".join(self._scopes),
            "aud": TOKEN_URI,
            "exp": now + 3600,
            "iat": now,
        })
        # IAM signJwt requires the CALLER token to carry an IAM-capable scope;
        # ADC-loaded credentials are often scoped to chat.bot/pubsub only,
        # which yields 403 "insufficient authentication scopes". Self-scope
        # the base to cloud-platform just for the signJwt call (the source
        # token already grants cloud-platform via the gcloud login chain).
        try:
            _sign_base = self._base.with_scopes(
                ["https://www.googleapis.com/auth/cloud-platform"])
        except Exception:
            _sign_base = self._base
        with AuthorizedSession(_sign_base) as session:
            r = session.post(SIGN_JWT_URL.format(sa=self.service_account_email),
                             json={"payload": payload})
            if r.status_code != 200:
                raise RuntimeError(
                    f"IAM signJwt failed ({r.status_code}): {r.text[:200]}")
            signed = r.json()["signedJwt"]
        import urllib.parse
        import urllib.request
        body = urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": signed,
        }).encode()
        req = urllib.request.Request(
            TOKEN_URI, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req) as resp:
            tok = json.loads(resp.read())
        self.token = tok["access_token"]
        # google.auth's expired() compares against NAIVE utcnow() — an aware
        # expiry raises TypeError on the second token request. Store naive UTC.
        self.expiry = (datetime.datetime.now(datetime.timezone.utc)
                       + datetime.timedelta(seconds=int(tok.get("expires_in", 3600)) - 60)
                       ).replace(tzinfo=None)
