import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode, urlparse

import requests
from bs4 import BeautifulSoup
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
        CREATE TABLE IF NOT EXISTS scenarios (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          owner_id TEXT NOT NULL, name TEXT NOT NULL, memo TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(owner_id, name)
        );
        """)
        columns = {row[1] for row in con.execute("PRAGMA table_info(scenarios)")}
        if "booth_url" not in columns:
            con.execute("ALTER TABLE scenarios ADD COLUMN booth_url TEXT NOT NULL DEFAULT ''")
        now = datetime.now(timezone.utc).isoformat()
        con.execute("""INSERT OR IGNORE INTO scenarios(owner_id,name,memo,created_at,updated_at)
                       SELECT DISTINCT owner_id,scenario,'',?,? FROM handouts""", (now, now))


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


def booth_info(raw_url):
    url = raw_url.strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not (parsed.hostname == "booth.pm" or (parsed.hostname or "").endswith(".booth.pm")):
        raise ValueError("BOOTHの商品URLを入力してください。")
    response = requests.get(url, headers={"User-Agent": "Hikokubin/1.1 (+scenario metadata import)"}, timeout=12, allow_redirects=True)
    response.raise_for_status()
    final_host = urlparse(response.url).hostname or ""
    if not (final_host == "booth.pm" or final_host.endswith(".booth.pm")):
        raise ValueError("BOOTH以外へ転送されたため取得を中止しました。")
    soup = BeautifulSoup(response.text, "html.parser")
    def meta(prop):
        node = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return (node.get("content") or "").strip() if node else ""
    title = meta("og:title") or (soup.title.string.strip() if soup.title and soup.title.string else "")
    description = meta("og:description") or meta("description")
    title = title.removesuffix(" - BOOTH").strip()
    if not title:
        raise ValueError("商品タイトルを取得できませんでした。")
    return title[:200], description[:4000], response.url


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
    selected = request.args.get("scenario", type=int)
    with db() as con:
        scenarios = con.execute("SELECT * FROM scenarios WHERE owner_id=? ORDER BY updated_at DESC", (session["user"]["id"],)).fetchall()
        chosen = con.execute("SELECT * FROM scenarios WHERE id=? AND owner_id=?", (selected, session["user"]["id"])).fetchone() if selected else None
        if chosen:
            handouts = con.execute("SELECT * FROM handouts WHERE owner_id=? AND scenario=? ORDER BY id DESC", (session["user"]["id"], chosen["name"])).fetchall()
        else:
            handouts = con.execute("SELECT * FROM handouts WHERE owner_id=? ORDER BY id DESC", (session["user"]["id"],)).fetchall()
    return render_template("dashboard.html", handouts=handouts, scenarios=scenarios, chosen=chosen)


@app.route("/scenario/new", methods=["GET", "POST"])
@login_required
def new_scenario():
    if request.method == "POST":
        name, memo = request.form.get("name", "").strip(), request.form.get("memo", "").strip()
        booth_url = request.form.get("booth_url", "").strip()
        if request.form.get("action") == "fetch":
            try:
                title, description, booth_url = booth_info(booth_url)
                flash("BOOTHからタイトルと概要を取得しました。内容を確認して保存してください。", "ok")
                return render_template("scenario.html", scenario=None, draft={"name": title, "memo": description, "booth_url": booth_url})
            except Exception as exc:
                flash(f"BOOTHから取得できませんでした：{exc}", "error")
                return render_template("scenario.html", scenario=None, draft={"name": name, "memo": memo, "booth_url": booth_url})
        if not name:
            flash("シナリオ名を入力してください。", "error")
        else:
            now = datetime.now(timezone.utc).isoformat()
            try:
                with db() as con:
                    con.execute("INSERT INTO scenarios(owner_id,name,memo,created_at,updated_at,booth_url) VALUES(?,?,?,?,?,?)", (session["user"]["id"], name, memo, now, now, booth_url))
                flash("シナリオメモを保存しました。", "ok")
                return redirect(url_for("dashboard"))
            except sqlite3.IntegrityError:
                flash("同じ名前のシナリオがすでにあります。", "error")
    return render_template("scenario.html", scenario=None)


@app.route("/scenario/<int:sid>/edit", methods=["GET", "POST"])
@login_required
def edit_scenario(sid):
    with db() as con:
        scenario = con.execute("SELECT * FROM scenarios WHERE id=? AND owner_id=?", (sid, session["user"]["id"])).fetchone()
        if not scenario: abort(404)
        if request.method == "POST":
            old_name = scenario["name"]
            name, memo = request.form.get("name", "").strip(), request.form.get("memo", "").strip()
            booth_url = request.form.get("booth_url", "").strip()
            if request.form.get("action") == "fetch":
                try:
                    title, description, booth_url = booth_info(booth_url)
                    draft = {"id": sid, "name": title, "memo": description, "booth_url": booth_url}
                    flash("BOOTHから情報を取得しました。保存すると反映されます。", "ok")
                    return render_template("scenario.html", scenario=draft, draft=draft)
                except Exception as exc:
                    flash(f"BOOTHから取得できませんでした：{exc}", "error")
            if not name:
                flash("シナリオ名を入力してください。", "error")
            else:
                try:
                    con.execute("UPDATE scenarios SET name=?,memo=?,booth_url=?,updated_at=? WHERE id=? AND owner_id=?", (name, memo, booth_url, datetime.now(timezone.utc).isoformat(), sid, session["user"]["id"]))
                    if old_name != name:
                        con.execute("UPDATE handouts SET scenario=? WHERE owner_id=? AND scenario=?", (name, session["user"]["id"], old_name))
                    flash("シナリオメモを保存しました。", "ok")
                    return redirect(url_for("dashboard", scenario=sid))
                except sqlite3.IntegrityError:
                    flash("同じ名前のシナリオがすでにあります。", "error")
    return render_template("scenario.html", scenario=scenario)


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
                now = datetime.now(timezone.utc).isoformat()
                con.execute("INSERT OR IGNORE INTO scenarios(owner_id,name,memo,created_at,updated_at) VALUES(?,?,?,?,?)", (session["user"]["id"], scenario, "", now, now))
            flash("HOを保存しました。", "ok")
            return redirect(url_for("dashboard"))
    return render_template("edit.html", handout=None, initial_scenario=request.args.get("scenario", ""))


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
