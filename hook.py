"""
Action1 RMM → asset sync plugin.
Auth:      OAuth 2.0 — POST /oauth2/token → Bearer token
Endpoints: GET /endpoints/managed/{orgId}
"""
import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_PLUGIN = "action1"

_A1_SERVERS = {
    "eu": "https://app.eu.action1.com/api/3.0",
    "us": "https://app.action1.com/api/3.0",
}
# Kept for routes.py reference
_A1_BASE = _A1_SERVERS["eu"]


def _base_url(server: str) -> str:
    return _A1_SERVERS.get((server or "eu").strip().lower(), _A1_SERVERS["eu"])


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_config():
    from core.database import get_db
    with get_db() as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE ?",
            (f"plugin_{_PLUGIN}_%",)
        ).fetchall()
    prefix = f"plugin_{_PLUGIN}_"
    return {r["key"][len(prefix):]: r["value"] for r in rows}


def _set(key, value):
    from core.database import get_db
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"plugin_{_PLUGIN}_{key}", str(value))
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Action1 API helpers
# ---------------------------------------------------------------------------

def _clean_err(raw: str, limit: int = 400) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] if text else raw[:limit]


def _get_token(client_id, client_secret, base):
    """Exchange client credentials for an OAuth 2.0 bearer token."""
    data = urllib.parse.urlencode({
        "client_id":     client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(
        f"{base}/oauth2/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "MonkeHelpdesk/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        raise RuntimeError(f"Action1 OAuth {exc.code}: {_clean_err(raw)}") from exc
    token = body.get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in OAuth response: {body}")
    return token


def _fetch_endpoints(org_id, client_id, client_secret, groups_filter, base):
    token   = _get_token(client_id, client_secret, base)
    headers = {"Authorization": f"Bearer {token}",
               "Accept":        "application/json",
               "User-Agent":    "MonkeHelpdesk/1.0"}
    base_ep = f"{base}/endpoints/managed/{org_id}"
    results = []
    skip    = 0
    page    = 100

    while True:
        url = f"{base_ep}?$top={page}&$skip={skip}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raw  = exc.read().decode(errors="replace")
            hint = f" (URL: {url})" if exc.code in (403, 404) else ""
            raise RuntimeError(f"Action1 API {exc.code}{hint}: {_clean_err(raw)}") from exc

        items = data.get("items") if isinstance(data, dict) else data
        if not items:
            break

        for ep in items:
            if groups_filter:
                raw_groups = ep.get("group_membership", ep.get("groups", []))
                ep_groups  = {g["name"] if isinstance(g, dict) else str(g) for g in raw_groups}
                if not (groups_filter & ep_groups):
                    continue
            results.append(ep)

        if len(items) < page:
            break
        skip += page

    return results


# ---------------------------------------------------------------------------
# Field extraction helpers
# ---------------------------------------------------------------------------

def _str(*candidates):
    for v in candidates:
        if v and str(v).strip():
            return str(v).strip()
    return ""


def _ram_str(ep):
    v = ep.get("RAM", "")
    if v:
        return str(v).replace(" RAM", "").strip()
    return ""


def _storage_str(ep):
    return _str(ep.get("disk"))


def _asset_type(ep):
    platform = _str(ep.get("platform")).lower()
    if "linux" in platform or "unix" in platform:
        return "Server"
    if "mac" in platform or "darwin" in platform:
        return "Other"
    ff = _str(ep.get("form_factor"), ep.get("chassis_type")).lower()
    if "laptop" in ff or "notebook" in ff or "portable" in ff:
        return "Laptop"
    if "server" in ff:
        return "Server"
    return "Desktop"


def _os_str(ep):
    return _str(ep.get("OS"), ep.get("os"), ep.get("os_name"), ep.get("operating_system"))


# ---------------------------------------------------------------------------
# Core sync
# ---------------------------------------------------------------------------

def run_sync(config=None):
    from core.database import get_db
    from core.utils import now

    if config is None:
        config = _get_config()

    client_id     = (config.get("client_id")     or "").strip()
    client_secret = (config.get("client_secret") or "").strip()
    org_id        = (config.get("org_id")         or "").strip()

    if not all([client_id, client_secret, org_id]):
        _set("last_sync_error", "Missing required config: client_id, client_secret, org_id")
        return

    base          = _base_url(config.get("server", "eu"))
    all_devices   = config.get("all_devices", "0") == "1"
    groups_raw    = (config.get("endpoint_groups") or "").strip()
    groups_filter = set() if all_devices or not groups_raw else {g.strip() for g in groups_raw.split(",") if g.strip()}

    try:
        endpoints = _fetch_endpoints(org_id, client_id, client_secret, groups_filter, base)
        upserted  = 0

        with get_db() as conn:
            for ep in endpoints:
                hostname = _str(ep.get("device_name"), ep.get("name"), ep.get("hostname"))
                serial   = _str(ep.get("serial"), ep.get("serial_number"))
                if not hostname:
                    continue

                cpu_name = _str(ep.get("CPU_name"), ep.get("cpu"), ep.get("processor"))
                cpu_size = _str(ep.get("CPU_size"))
                cpu      = f"{cpu_name} ({cpu_size})" if cpu_name and cpu_size else cpu_name or cpu_size

                hw = {
                    "manufacturer":   _str(ep.get("manufacturer")),
                    "model":          _str(ep.get("model")),
                    "config_cpu":     cpu,
                    "config_ram":     _ram_str(ep),
                    "config_storage": _storage_str(ep),
                    "config_os":      _os_str(ep),
                }

                existing = None
                if serial:
                    existing = conn.execute(
                        "SELECT id FROM assets WHERE serial_number=?", (serial,)
                    ).fetchone()
                if not existing:
                    existing = conn.execute(
                        "SELECT id FROM assets WHERE asset_tag=?", (hostname,)
                    ).fetchone()

                if existing:
                    hw_with_source = {**hw, "sync_source": "action1"}
                    cols = ", ".join(f"{k}=?" for k in hw_with_source)
                    conn.execute(
                        f"UPDATE assets SET {cols} WHERE id=?",
                        [*hw_with_source.values(), existing["id"]]
                    )
                else:
                    try:
                        conn.execute(
                            "INSERT INTO assets "
                            "(name,asset_tag,asset_type,manufacturer,model,serial_number,"
                            "status,config_cpu,config_ram,config_storage,config_os,sync_source,created_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (hostname, hostname, _asset_type(ep),
                             hw["manufacturer"], hw["model"], serial,
                             "Available",
                             hw["config_cpu"], hw["config_ram"],
                             hw["config_storage"], hw["config_os"],
                             "action1", now())
                        )
                    except sqlite3.IntegrityError:
                        continue
                upserted += 1
            conn.commit()

        _set("last_sync_at",    now())
        _set("last_sync_count", str(upserted))
        _set("last_sync_error", "")

    except Exception as exc:
        _set("last_sync_error", str(exc))


# ---------------------------------------------------------------------------
# Background sync thread
# ---------------------------------------------------------------------------

def _sync_loop():
    while True:
        cfg = _get_config()
        try:
            interval = max(5, int(cfg.get("sync_interval_minutes") or 240))
        except (ValueError, TypeError):
            interval = 240
        time.sleep(interval * 60)
        run_sync(cfg)


_bg_thread = threading.Thread(target=_sync_loop, daemon=True, name="action1-sync")
_bg_thread.start()


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------

def on_event(event, payload, config):
    pass
