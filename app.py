from flask import Flask, jsonify, send_from_directory
import os
import requests

app = Flask(__name__, static_folder="public")


BAN_API_KEY = os.environ.get("BAN_API_KEY")
BAN_UNIVERSE_ID = "6765805766"
BAN_MAX_PAGES = int(os.environ.get("BAN_MAX_PAGES", "5"))
ROBLOX_BAN_LOG_URL = f"https://apis.roblox.com/cloud/v2/universes/{BAN_UNIVERSE_ID}/user-restrictions:listLogs"


def shorten_public_ban_reason(reason: str) -> str:
    rl = (reason or "").lower()
    if "you created or used an account" in rl:
        return "Alt Account"
    if any(k in rl for k in ("cheating", "further", "exploiting", "automatic")):
        return "Exploiting"
    if any(k in rl for k in ("bm", "bye", "scam", "economy", "black", "cross-trading", "cross")):
        return "Economy"
    return "Other"


def extract_user_id(path: str) -> str:
    if not path:
        return ""
    return path.split("/")[-1]


def map_log_entry(entry: dict) -> dict:
    user_path = entry.get("user", "")
    moderator_path = (entry.get("moderator") or {}).get("robloxUser", "")
    public_reason = entry.get("displayReason") or entry.get("privateReason") or ""

    return {
        "userId": extract_user_id(user_path),
        "userPath": user_path,
        "place": entry.get("place", ""),
        "moderatorId": extract_user_id(moderator_path),
        "createTime": entry.get("createTime"),
        "startTime": entry.get("startTime"),
        "active": entry.get("active", False),
        "excludeAltAccounts": entry.get("excludeAltAccounts", False),
        "privateReason": entry.get("privateReason", ""),
        "displayReason": entry.get("displayReason", ""),
        "shortReason": shorten_public_ban_reason(public_reason),
    }


@app.route("/")
def root():
    return send_from_directory("public", "index.html")


@app.route("/api/bans")
def bans():
    if not BAN_API_KEY:
        return jsonify({"error": "BAN_API_KEY not configured"}), 500

    headers = {"x-api-key": BAN_API_KEY}
    params = {"maxPageSize": 100}
    page_token = None
    pages = 0
    collected = []

    while pages < BAN_MAX_PAGES:
        if page_token:
            params["pageToken"] = page_token
        elif "pageToken" in params:
            params.pop("pageToken")

        try:
            resp = requests.get(ROBLOX_BAN_LOG_URL, headers=headers, params=params, timeout=15)
        except requests.RequestException as exc:
            return jsonify({"error": "Failed to reach Roblox API", "detail": str(exc)}), 502

        if resp.status_code != 200:
            return (
                jsonify({"error": "Roblox API returned an error", "status": resp.status_code, "body": resp.text}),
                502,
            )

        data = resp.json() or {}
        logs = data.get("logs", [])
        collected.extend(map_log_entry(log) for log in logs)

        page_token = data.get("nextPageToken")
        pages += 1
        if not page_token:
            break

    return jsonify({"logs": collected, "nextPageToken": page_token, "pagesFetched": pages})


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory("public", path)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
