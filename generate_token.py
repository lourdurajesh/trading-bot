"""
generate_token.py
─────────────────
Automated Fyers access token generator — TOTP + PIN.
Based on confirmed working community implementations.

Required in .env:
    FYERS_APP_ID        — e.g. BM1TIONOVH-100
    FYERS_SECRET_KEY    — your app secret
    FYERS_REDIRECT_URI  — must match Fyers app settings exactly
    FYERS_CLIENT_ID     — your Fyers login ID e.g. XJ12251
    FYERS_PIN           — your 4-digit Fyers login PIN
    FYERS_TOTP_SECRET   — TOTP secret from myaccount.fyers.in
"""

import base64
import hashlib
import logging
import os
import sys
from urllib.parse import parse_qs, urlparse

import pyotp
import requests
from dotenv import load_dotenv, set_key
from fyers_apiv3 import fyersModel

load_dotenv(override=True)

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("generate_token")

APP_ID       = os.getenv("FYERS_APP_ID", "")
SECRET_KEY   = os.getenv("FYERS_SECRET_KEY", "")
REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI", "https://trade.fyers.in/api-login/redirect-uri/index.html")
CLIENT_ID    = os.getenv("FYERS_CLIENT_ID", "")
PIN          = os.getenv("FYERS_PIN", "")
TOTP_SECRET  = os.getenv("FYERS_TOTP_SECRET", "")
ENV_FILE     = ".env"

BASE_URL        = "https://api-t2.fyers.in/vagator/v2"
BASE_URL_2      = "https://api-t1.fyers.in/api/v3"
URL_SEND_OTP    = f"{BASE_URL}/send_login_otp"
URL_VERIFY_OTP  = f"{BASE_URL}/verify_otp"
URL_VERIFY_PIN  = f"{BASE_URL}/verify_pin"
URL_TOKEN       = f"{BASE_URL_2}/token"

# Session with browser-like headers — required by Fyers
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/112.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
})


def validate_config() -> bool:
    missing = []
    if not APP_ID:      missing.append("FYERS_APP_ID")
    if not SECRET_KEY:  missing.append("FYERS_SECRET_KEY")
    if not CLIENT_ID:   missing.append("FYERS_CLIENT_ID")
    if not PIN:         missing.append("FYERS_PIN")
    if not TOTP_SECRET: missing.append("FYERS_TOTP_SECRET")
    if missing:
        logger.error(f"Missing in .env: {', '.join(missing)}")
        return False
    return True


def step1_send_otp() -> str:
    """Send login OTP — returns request_key."""
    logger.info("Step 1: Initiating login...")
    resp = SESSION.post(URL_SEND_OTP, json={
        "fy_id":  CLIENT_ID,
        "app_id": "2",
    })
    data = resp.json()
    if data.get("s") != "ok":
        raise RuntimeError(f"send_login_otp failed: {data}")
    logger.info(f"Got request_key: {data['request_key'][:20]}...")
    return data["request_key"]


def step2_verify_totp(request_key: str) -> str:
    """Verify TOTP OTP — returns new request_key."""
    logger.info("Step 2: Verifying TOTP...")
    otp = pyotp.TOTP(TOTP_SECRET).now()
    logger.info(f"OTP: {otp}")
    resp = SESSION.post(URL_VERIFY_OTP, json={
        "request_key": request_key,
        "otp":         otp,
    })
    data = resp.json()
    if data.get("s") != "ok":
        raise RuntimeError(f"verify_totp failed: {data.get('message', data)}")
    logger.info("TOTP verified.")
    return data["request_key"]


def step3_verify_pin(request_key: str) -> str:
    """Verify PIN — returns short-lived login token."""
    logger.info("Step 3: Verifying PIN...")
    resp = SESSION.post(URL_VERIFY_PIN, json={
        "request_key":     request_key,
        "identity_type":   "pin",
        "identifier":      PIN,
        "recaptcha_token": "",
    })
    data = resp.json()
    if data.get("s") != "ok":
        raise RuntimeError(f"verify_pin failed: {data.get('message', data)}")
    token = data.get("data", {}).get("access_token", "")
    if not token:
        raise RuntimeError(f"No token in verify_pin response: {data}")
    logger.info("PIN verified.")
    return token


def step4_get_access_token(login_token: str) -> str:
    """
    Get access token via token endpoint.
    code_challenge must be empty string — confirmed working format.
    """
    logger.info("Step 4: Getting access token...")

    # app_id here is just the part before '-100'
    app_id_short = APP_ID.split("-")[0]

    resp = SESSION.post(URL_TOKEN, json={
        "fyers_id":       CLIENT_ID,
        "app_id":         app_id_short,
        "redirect_uri":   REDIRECT_URI,
        "appType":        "100",
        "code_challenge": "",       # empty string — not a hash
        "state":          "sample_state",
        "scope":          "",
        "nonce":          "",
        "response_type":  "code",
        "create_cookie":  True,
    }, headers={
        "Authorization": f"Bearer {login_token}",
    })

    data = resp.json()
    logger.info(f"Step 4 raw response: {data}")

    if data.get("s") != "ok":
        raise RuntimeError(f"get_auth_code failed: {data}")

    # Try all known locations for the auth code
    auth_code = (
        data.get("data", {}).get("auth_code", "")
        or data.get("data", {}).get("auth", "")
        or data.get("auth_code", "")
    )

    # Also check redirect URL
    if not auth_code:
        url = data.get("Url", "") or data.get("data", {}).get("redirectUrl", "")
        if url and "auth_code=" in url:
            auth_code = parse_qs(urlparse(url).query).get("auth_code", [""])[0]

    if not auth_code:
        raise RuntimeError(f"Could not find auth_code in response: {data}")

    logger.info(f"Auth code: {auth_code[:30]}...")
    return auth_code


def step5_generate_token(auth_code: str) -> str:
    """Exchange auth_code for final access token via Fyers SDK."""
    logger.info("Step 5: Generating access token...")
    session = fyersModel.SessionModel(
        client_id     = APP_ID,
        secret_key    = SECRET_KEY,
        redirect_uri  = REDIRECT_URI,
        response_type = "code",
        grant_type    = "authorization_code",
    )
    session.set_token(auth_code)
    response = session.generate_token()
    logger.info(f"generate_token response: {response}")

    if response.get("s") != "ok":
        raise RuntimeError(f"generate_token failed: {response}")

    token = response.get("access_token", "")
    if not token:
        raise RuntimeError("Empty access_token in response")

    logger.info("Access token generated successfully.")
    return token


def save_token(token: str) -> None:
    set_key(ENV_FILE, "FYERS_ACCESS_TOKEN", token)
    logger.info(f"Token saved to {ENV_FILE}")


def main():
    logger.info("=" * 50)
    logger.info("  Fyers Token Generator")
    logger.info("=" * 50)

    if not validate_config():
        sys.exit(1)

    try:
        request_key  = step1_send_otp()
        request_key  = step2_verify_totp(request_key)
        login_token  = step3_verify_pin(request_key)
        access_token = step4_get_access_token(login_token)
        save_token(access_token)
        logger.info("")
        logger.info("Done. Run:  python main.py")

    except RuntimeError as e:
        logger.error(f"\nFailed: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()