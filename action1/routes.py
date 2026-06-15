import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request

from flask import Blueprint, jsonify, request, send_from_directory

from core.auth import login_required, super_admin_required
from core.database import get_db

bp = Blueprint("action1", __name__)

_PLUGIN = "action1"
_PAGES  = os.path.join(os.path.dirname(__file__), "pages")


def _status():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE ?",
            (f"plugin_{_PLUGIN}_%",)
        ).fetchall()
    prefix = f"plugin_{_PLUGIN}_"
    cfg    = {r["key"][len(prefix):]: r["value"] for r in rows}
    return {
        "last_sync_at":          cfg.get("last_sync_at", ""),
        "last_sync_count":       cfg.get("last_sync_count", ""),
        "last_sync_error":       cfg.get("last_sync_error", ""),
        "sync_interval_minutes": cfg.get("sync_interval_minutes", "240"),
    }


@bp.route("/plugin/action1")
@bp.route("/plugin/action1/")
@login_required
def page_index():
    return send_from_directory(_PAGES, "index.html")


@bp.route("/plugin/action1/status")
@login_required
def api_status():
    return jsonify(_status())


@bp.route("/plugin/action1/sync", methods=["POST"])
@super_admin_required
def api_sync():
    hook = sys.modules.get("plugin_action1_hook")
    if hook is None:
        return jsonify({"error": "Hook module not loaded — restart required"}), 503
    t = threading.Thread(target=hook.run_sync, daemon=True, name="action1-manual-sync")
    t.start()
    return jsonify({"ok": True})


@bp.route("/plugin/action1/test-connection", methods=["POST"])
@super_admin_required
def api_test_connection():
    """Probe token → organizations → endpoints path to diagnose 403s."""
    hook = sys.modules.get("plugin_action1_hook")
    if hook is None:
        return jsonify({"error": "Hook module not loaded — restart required"}), 503

    cfg = {}
    with get_db() as _c:
        for row in _c.execute("SELECT key,value FROM settings WHERE key LIKE 'plugin_action1_%'").fetchall():
            cfg[row["key"][len("plugin_action1_"):]] = row["value"]

    client_id     = cfg.get("client_id", "").strip()
    client_secret = cfg.get("client_secret", "").strip()
    org_id        = cfg.get("org_id", "").strip()
    server        = cfg.get("server", "eu")
    base          = hook._base_url(server)
    results       = {}

    if not client_id or not client_secret:
        return jsonify({"error": "Save client_id and client_secret first"}), 400

    try:
        token = hook._get_token(client_id, client_secret, base)
        results["token"] = "OK"
    except Exception as exc:
        return jsonify({"token": "FAIL", "error": str(exc)}), 400

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "MonkeHelpdesk/1.0"}
    probe_paths = [
        "organizations",
        f"endpoints/managed/{org_id}",
    ]
    org_body   = None
    sample_ep  = None
    for path in probe_paths:
        url = f"{base}/{path}?$top=1&$skip=0"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.loads(r.read())
                if path == "organizations":
                    org_body = body
                items = body.get("items", body) if isinstance(body, dict) else body
                count = len(items) if isinstance(items, list) else "?"
                results[path] = f"OK ({count} items)"
                if "endpoints/managed" in path and isinstance(items, list) and items:
                    sample_ep = items[0]
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")[:200]
            results[path] = f"HTTP {exc.code}: {re.sub(r'<[^>]+>', '', raw).strip()[:120]}"
        except Exception as exc:
            results[path] = f"ERROR: {exc}"

    return jsonify({"base": base, "org_id": org_id, "results": results,
                    "org_body": org_body, "sample_endpoint": sample_ep})


@bp.route("/plugin/action1/detect-org", methods=["POST"])
@super_admin_required
def api_detect_org():
    """Use entered credentials to auto-detect organization ID from Action1 /organizations."""
    hook = sys.modules.get("plugin_action1_hook")
    if hook is None:
        return jsonify({"error": "Hook module not loaded — restart required"}), 503

    data      = request.get_json(silent=True) or {}
    client_id = (data.get("client_id") or "").strip()
    client_secret = (data.get("client_secret") or "").strip()

    # Fall back to stored secret so auto-detect works after page reload
    if not client_secret:
        with get_db() as _conn:
            _row = _conn.execute(
                "SELECT value FROM settings WHERE key='plugin_action1_client_secret'"
            ).fetchone()
        if _row:
            client_secret = _row["value"]

    if not client_id:
        return jsonify({"error": "Client ID is required (enter it above first)"}), 400
    if not client_secret:
        return jsonify({"error": "Client Secret required — enter it once and save, then auto-detect will work"}), 400

    # Read stored server setting
    with get_db() as _sc:
        _sr = _sc.execute("SELECT value FROM settings WHERE key='plugin_action1_server'").fetchone()
    base = hook._base_url(_sr["value"] if _sr else "eu")

    try:
        token = hook._get_token(client_id, client_secret, base)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    req = urllib.request.Request(
        f"{base}/organizations",
        headers={"Authorization": f"Bearer {token}",
                 "Accept":        "application/json",
                 "User-Agent":    "MonkeHelpdesk/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw  = exc.read().decode(errors="replace")
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()[:400]
        hint = " — API key may lack Organization Read permission" if exc.code == 403 else ""
        return jsonify({"error": f"Action1 /organizations {exc.code}{hint}: {text}"}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    items = body.get("items") if isinstance(body, dict) else (body if isinstance(body, list) else [])
    if not items:
        return jsonify({"error": "No organizations returned — check API key permissions"}), 404

    orgs = []
    for o in items:
        oid  = o.get("id") or o.get("organization_id") or o.get("Id")
        name = o.get("name") or o.get("organization_name") or o.get("Name") or "Unknown"
        if oid:
            orgs.append({"id": str(oid), "name": str(name)})
    if not orgs:
        return jsonify({"error": "Could not parse org ID from response", "raw": body}), 400

    return jsonify({"organizations": orgs})
