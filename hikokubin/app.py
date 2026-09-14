import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode

import requests
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", secrets.token_hex(32))

CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080").rstrip("/")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data" if Path("/data").exists() else "./data"))
DB_PATH = DATA_DIR / "hikokubin.db"
API = "https://discord.com/api/v10"


def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS handouts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          owner_id TEXT NOT NULL, scenario TEXT NOT NULL, name TEXT NOT NULL,
          recipient_id TEXT NOT NULL, recipient_name TEXT DEFAULT '', body TEXT NOT NULL,
          created_at TEXT NOT NULL, sent_at TEXT, received_at TEXT
        );
        """)


init_db()


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapped


def receipt_signature(handout_id):
    key = app.secret_key.encode()
    return hmac.new(key, str(handout_id).encode(), hashlib.sha256).hexdigest()


@app.get("/")
def index():
    if "user" in session:
        return redirect(url_for("dashboard"))
    return render_template("index.html")


@app.get("/login")
def login():
    if not CLIENT_ID or not CLIENT_SECRET:
        return "Discord OAuthの環境変数が設定されていません。READMEを確認してください。", 503
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": f"{BASE_URL}/callback",
        "response_type": "code",
        "scope": "identify",
        "state": state,
    }
    return redirect(f"{API}/oauth2/authorize?{urlencode(params)}")


@app.get("/callback")
def callback():
    if not request.args.get("state") or request.args.get("state") != session.pop("oauth_state", None):
        abort(400)
    token = requests.post(f"{API}/oauth2/token", data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": request.args.get("code", ""),
        "redirect_uri": f"{BASE_URL}/callback",
    }, timeout=15)
    token.raise_for_status()
    me = requests.get(f"{API}/users/@me", headers={"Authorization": f"Bearer {token.json()['access_token']}"}, timeout=15)
    me.raise_for_status()
    user = me.json()
    session["user"] = {"id": user["id"], "name": user.get("global_name") or user["username"], "avatar": user.get("avatar")}
    return redirect(url_for("dashboard"))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.get("/dashboard")
@login_required
def dashboard():
    with db() as con:
        handouts = con.execute("SELECT * FROM handouts WHERE owner_id=? ORDER BY id DESC", (session["user"]["id"],)).fetchall()
    return render_template("dashboard.html", handouts=handouts)


@app.route("/handout/new", methods=["GET", "POST"])
@login_required
def new_handout():
    if request.method == "POST":
        scenario = request.form.get("scenario", "").strip()
        name = request.form.get("name", "").strip()
        recipient_id = request.form.get("recipient_id", "").strip()
        recipient_name = request.form.get("recipient_name", "").strip()
        body = request.form.get("body", "").strip()
        if not scenario or not name or not body or not recipient_id.isdigit():
            flash("シナリオ名、HO名、DiscordユーザーID、本文を確認してください。", "error")
        else:
            with db() as con:
                con.execute("INSERT INTO handouts(owner_id,scenario,name,recipient_id,recipient_name,body,created_at) VALUES(?,?,?,?,?,?,?)",
                            (session["user"]["id"], scenario, name, recipient_id, recipient_name, body, datetime.now(timezone.utc).isoformat()))
            flash("HOを保存しました。", "ok")
            return redirect(url_for("dashboard"))
    return render_template("edit.html", handout=None)


@app.route("/handout/<int:hid>/edit", methods=["GET", "POST"])
@login_required
def edit_handout(hid):
    with db() as con:
        ho = con.execute("SELECT * FROM handouts WHERE id=? AND owner_id=?", (hid, session["user"]["id"])).fetchone()
        if not ho: abort(404)
        if request.method == "POST":
            values = (request.form.get("scenario", "").strip(), request.form.get("name", "").strip(),
                      request.form.get("recipient_id", "").strip(), request.form.get("recipient_name", "").strip(),
                      request.form.get("body", "").strip(), hid, session["user"]["id"])
            if not values[0] or not values[1] or not values[2].isdigit() or not values[4]:
                flash("入力内容を確認してください。", "error")
            else:
                con.execute("UPDATE handouts SET scenario=?,name=?,recipient_id=?,recipient_name=?,body=? WHERE id=? AND owner_id=?", values)
                flash("変更を保存しました。", "ok")
                return redirect(url_for("dashboard"))
    return render_template("edit.html", handout=ho)


@app.post("/handout/<int:hid>/send")
@login_required
def send_handout(hid):
    if not BOT_TOKEN:
        flash("RailwayにDISCORD_BOT_TOKENが設定されていません。", "error")
        return redirect(url_for("dashboard"))
    with db() as con:
        ho = con.execute("SELECT * FROM handouts WHERE id=? AND owner_id=?", (hid, session["user"]["id"])).fetchone()
        if not ho: abort(404)
        headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
        dm = requests.post(f"{API}/users/@me/channels", headers=headers, json={"recipient_id": ho["recipient_id"]}, timeout=15)
        if not dm.ok:
            flash("DMを開けませんでした。Botとの共通サーバーと相手のDM設定を確認してください。", "error")
            return redirect(url_for("dashboard"))
        receipt = f"{BASE_URL}/receipt/{hid}/{receipt_signature(hid)}"
        payload = {
            "embeds": [{
                "title": f"【秘匿HO：{ho['name']}】",
                "description": ho["body"][:4000],
                "color": 7361935,
                "author": {"name": ho["scenario"]},
                "footer": {"text": "この内容は、ほかの探索者には見えません。"}
            }],
            "components": [{"type": 1, "components": [{"type": 2, "style": 5, "label": "受領しました", "url": receipt}]}]
        }
        sent = requests.post(f"{API}/channels/{dm.json()['id']}/messages", headers=headers, data=json.dumps(payload), timeout=15)
        if sent.ok:
            con.execute("UPDATE handouts SET sent_at=?,received_at=NULL WHERE id=?", (datetime.now(timezone.utc).isoformat(), hid))
            flash(f"{ho['recipient_name'] or ho['recipient_id']}へ送信しました。", "ok")
        else:
            flash("Discordへの送信に失敗しました。", "error")
    return redirect(url_for("dashboard"))


@app.post("/handout/<int:hid>/delete")
@login_required
def delete_handout(hid):
    with db() as con:
        con.execute("DELETE FROM handouts WHERE id=? AND owner_id=?", (hid, session["user"]["id"]))
    flash("HOを削除しました。", "ok")
    return redirect(url_for("dashboard"))


@app.get("/receipt/<int:hid>/<signature>")
def receipt(hid, signature):
    if not hmac.compare_digest(signature, receipt_signature(hid)): abort(403)
    with db() as con:
        ho = con.execute("SELECT * FROM handouts WHERE id=?", (hid,)).fetchone()
        if not ho: abort(404)
        con.execute("UPDATE handouts SET received_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), hid))
    return render_template("received.html", ho=ho)


@app.get("/health")
def health():
    return {"status": "ok", "app": "hikokubin"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=True)
