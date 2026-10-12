from flask import Flask, request, jsonify, send_from_directory
from urllib.parse import urlparse
import urllib.request
import urllib.error
import os
from flask_cors import CORS
from TikTokLive import TikTokLiveClient
from TikTokLive.events import CommentEvent, GiftEvent, LikeEvent, ConnectEvent, DisconnectEvent, RoomUserSeqEvent
import threading
import asyncio
import re, sqlite3, secrets, hashlib, time, smtplib, urllib.request
from email.message import EmailMessage
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _load_env_file():
    """Le um arquivo .env simples na pasta do servidor (chave=valor por linha) e
    injeta em os.environ, sem sobrescrever variaveis que ja existam no ambiente."""
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception as e:
        print(f"[.env] falha ao ler {path}: {e}")


_load_env_file()
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
# Lista de modelos em ordem de preferencia. Se o primeiro falhar (404, limite, erro
# do servidor), o proximo e tentado automaticamente. Pode ser configurada no .env:
#   AI_MODELS=gemini-3.6-flash,gemini-3.8-flash
AI_MODELS = [m.strip() for m in os.environ.get("AI_MODELS", "gemini-3.6-flash,gemini-3.8-flash").split(",") if m.strip()]
_single = os.environ.get("AI_MODEL", "").strip()  # compatibilidade com o .env antigo
if _single and _single not in AI_MODELS:
    AI_MODELS.insert(0, _single)


app = Flask(__name__)
CORS(app)


# Serve o painel principal e os overlays como paginas normais do site,
# assim tudo roda em "localhost" e o navegador nao bloqueia os iframes.
@app.route("/")
def home():
    resp = send_from_directory(BASE_DIR, "tique-bom-integrated.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/<path:filename>")
def serve_file(filename):
    if filename.endswith(".html"):
        resp = send_from_directory(BASE_DIR, filename)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp
    return "Not found", 404

sessions = {}      # username -> {"client": ..., "loop": ..., "thread": ...}
interactions = {}  # username -> dados mostrados no painel


AVATAR_HOSTS = ("tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com", "tiktokcdn-sg.com", "tiktokcdn2.com",
                "ibytedtos.com", "ibyteimg.com", "byteimg.com", "muscdn.com", "tiktokv.com", "tiktokv.us")
def clean_url(u):
    import html as _html
    u = (u or "").replace("\\u002F", "/").replace("\\u0026", "&")
    return _html.unescape(u).strip()


def avatar_from(user):
    """Pega uma URL de foto do usuario, evitando formatos que o navegador nao abre (heic).
    Versoes recentes da lib TikTokLive nao usam mais 'user.avatar': a foto vem em
    avatar_thumb / avatar_medium / avatar_large (cada um com .url_list ou .urls)."""
    candidates = []
    for attr in ("avatar", "avatar_thumb", "avatar_medium", "avatar_large"):
        img = getattr(user, attr, None)
        if img is None:
            continue
        for list_attr in ("urls", "url_list"):
            urls = getattr(img, list_attr, None)
            if urls:
                candidates.extend(list(urls))
    if not candidates:
        print(f"[avatar_from] nenhuma URL de avatar encontrada para {getattr(user,'nickname','?')} (atributos: {[a for a in dir(user) if 'avatar' in a.lower()]})")
        return ""
    for u in candidates:
        if ".heic" not in u.lower():
            return u
    return candidates[0]


def new_data():
    return {
        "comments": [],
        "gifts": [],
        "likers": {},        # user_id -> {user, avatar, total_likes} (totais acumulados)
        "liker_events": [],  # lista em ordem de chegada, pro overlay mostrar um a um
        "total_comments": 0,
        "total_gifts": 0,
        "total_likes": 0,
        "viewer_count": 0,
        "viewer_series": [],  # [{"t": timestamp, "v": contagem}], amostrado em intervalos curtos p/ grafico
        "status": "connecting",
        "host_avatar": "",
        "host_name": "",
        "error": None,
    }


def run_client(username):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    client = TikTokLiveClient(unique_id="@" + username)
    sessions[username]["client"] = client
    sessions[username]["loop"] = loop
    data = interactions[username]

    # guarda o indice de cada usuario dentro de data["likers"], pra somar likes sem duplicar card
    liker_index = {}

    # cache de avatares: LikeEvent geralmente nao traz foto do usuario, so
    # unique_id/nickname. CommentEvent e GiftEvent trazem o perfil completo,
    # entao usamos essas fotos como reserva quando uma curtida chega sem avatar.
    avatar_cache = {}

    @client.on(ConnectEvent)
    async def on_connect(event: ConnectEvent):
        data["status"] = "connected"
        data["_session_started"] = time.time()
        try:
            with db() as conn:
                cur = conn.execute(
                    "INSERT INTO live_sessions(username,started,total_comments,total_gifts,total_likes,peak_viewers) VALUES(?,?,0,0,0,0)",
                    (username, data["_session_started"]),
                )
                data["_session_row_id"] = cur.lastrowid
        except Exception as e:
            print(f"[{username}] erro ao abrir historico: {e}")
        try:
            data["host_avatar"] = clean_url(await client.get_avatar_url())
            data["host_name"] = username
        except Exception as e:
            print(f"[{username}] sem foto do host via get_avatar_url: {e}")
            try:
                owner = getattr(event, "user", None)
                if owner is not None:
                    data["host_name"] = getattr(owner, "nickname", "") or username
                    data["host_avatar"] = avatar_from(owner)
            except Exception as e2:
                print(f"[{username}] fallback tambem falhou: {e2}")
        print(f"[{username}] Conectado a live.")

    @client.on(CommentEvent)
    async def on_comment(event: CommentEvent):
        avatar_url = avatar_from(event.user)
        if avatar_url and not data.get("host_avatar") and (getattr(event.user, "unique_id", "") or "").lower() == username.lower():
            data["host_avatar"] = avatar_url
            data["host_name"] = event.user.nickname
        cid = getattr(event.user, "unique_id", None) or event.user.nickname
        if avatar_url:
            avatar_cache[cid] = avatar_url
        data["comments"].append({"user": event.user.nickname, "text": event.comment, "avatar": avatar_url})
        data["comments"] = data["comments"][-50:]
        data["total_comments"] += 1
        print(f"[{username}] {event.user.nickname}: {event.comment}")

    @client.on(GiftEvent)
    async def on_gift(event: GiftEvent):
        if event.gift.streakable and event.streaking:
            return
        avatar_url = avatar_from(event.user)
        gid = getattr(event.user, "unique_id", None) or event.user.nickname
        if avatar_url:
            avatar_cache[gid] = avatar_url
        data["gifts"].append({
            "user": event.user.nickname,
            "gift_name": event.gift.name,
            "quantity": event.repeat_count,
            "avatar": avatar_url,
        })
        data["gifts"] = data["gifts"][-50:]
        data["total_gifts"] += event.repeat_count
        print(f"[{username}] Presente: {event.gift.name} x{event.repeat_count}")

    VIEWER_SAMPLE_SECONDS = 15  # intervalo minimo entre pontos no grafico de espectadores

    @client.on(RoomUserSeqEvent)
    async def on_viewer_count(event: RoomUserSeqEvent):
        # No RoomUserSeqEvent, "total" e a quantidade de pessoas assistindo AGORA.
        # "total_user" e o acumulado de quem ja passou pela live (por isso dava milhares
        # numa live com ~60 pessoas), entao NAO usamos ele como espectadores.
        count = getattr(event, "total", None)
        if count is None:
            count = getattr(event, "viewer_count", None) or getattr(event, "viewerCount", None)
        count = count or 0
        data["viewer_count"] = count
        series = data["viewer_series"]
        now = time.time()
        if not series or (now - series[-1]["t"]) >= VIEWER_SAMPLE_SECONDS:
            print(f"[{username}] Espectadores agora: {count} (acumulado total_user={getattr(event, 'total_user', None)})")
            series.append({"t": now, "v": count})
            data["viewer_series"] = series[-400:]  # limite de pontos p/ nao crescer sem fim

    @client.on(LikeEvent)
    async def on_like(event: LikeEvent):
        qty = getattr(event, "count", 1) or 1
        data["total_likes"] += qty

        # Usa o nome de usuario como identificador: mais estavel entre versoes da lib
        user_id = getattr(event.user, "unique_id", None) or event.user.nickname
        avatar_url = avatar_from(event.user)
        if not avatar_url:
            # LikeEvent raramente traz foto; usa a ultima vista num comentario/presente
            avatar_url = avatar_cache.get(user_id, "")
        elif user_id not in avatar_cache:
            avatar_cache[user_id] = avatar_url

        if user_id in liker_index:
            data["likers"][user_id]["total_likes"] += qty
            if avatar_url and not data["likers"][user_id].get("avatar"):
                data["likers"][user_id]["avatar"] = avatar_url
        else:
            data["likers"][user_id] = {
                "user": event.user.nickname,
                "avatar": avatar_url,
                "total_likes": qty,
            }
        liker_index[user_id] = True

        # lista ordenada por ordem de chegada do evento, pro overlay mostrar
        data["liker_events"].append(data["likers"][user_id])
        data["liker_events"] = data["liker_events"][-50:]

        print(f"[{username}] Like: {event.user.nickname} (+{qty})")

    @client.on(DisconnectEvent)
    async def on_disconnect(event: DisconnectEvent):
        data["status"] = "disconnected"
        row_id = data.get("_session_row_id")
        if row_id:
            try:
                import json as _json
                breakdown = {}
                for g in data.get("gifts", []):
                    breakdown[g["gift_name"]] = breakdown.get(g["gift_name"], 0) + g.get("quantity", 1)
                with db() as conn:
                    conn.execute(
                        "UPDATE live_sessions SET ended=?,total_comments=?,total_gifts=?,total_likes=?,gifts_breakdown=? WHERE id=?",
                        (time.time(), data["total_comments"], data["total_gifts"], data["total_likes"],
                         _json.dumps(breakdown, ensure_ascii=False), row_id),
                    )
            except Exception as e:
                print(f"[{username}] erro ao fechar historico: {e}")
        print(f"[{username}] Desconectado da live.")

    try:
        loop.run_until_complete(client.connect())
    except Exception as e:
        data["status"] = "error"
        data["error"] = str(e)
        print(f"[{username}] ERRO: {e}")


@app.route("/api/connect", methods=["POST"])
def connect():
    body = request.get_json(force=True, silent=True) or {}
    username = body.get("username", "").strip().lstrip("@")

    if not username:
        return jsonify({"error": "Nome de usuario vazio"}), 400

    existing = interactions.get(username)
    if username in sessions and existing and existing["status"] != "error":
        return jsonify({"message": "Ja conectado"}), 200

    interactions[username] = new_data()
    t = threading.Thread(target=run_client, args=(username,), daemon=True)
    sessions[username] = {"thread": t}
    t.start()

    return jsonify({"message": f"Conectando a live de {username}"}), 200


@app.route("/api/disconnect/<username>", methods=["POST"])
def disconnect(username):
    username = username.strip().lstrip("@")
    session = sessions.pop(username, None)

    if session and session.get("client") and session.get("loop"):
        try:
            future = asyncio.run_coroutine_threadsafe(
                session["client"].disconnect(), session["loop"]
            )
            future.result(timeout=5)
        except Exception as e:
            print(f"Erro ao desconectar {username}: {e}")

    interactions.pop(username, None)
    return jsonify({"message": f"Desconectado de {username}"}), 200


# Quando o streamer nao informou localizacao, caimos num ponto generico dentro do Brasil
# (em vez de deixar lat/lng nulos), so para o canal aparecer no globo da Home.
import random as _random

_BR_FALLBACK_POINTS = [
    (-23.55, -46.63),  # Sao Paulo
    (-22.91, -43.17),  # Rio de Janeiro
    (-15.78, -47.93),  # Brasilia
    (-19.92, -43.94),  # Belo Horizonte
    (-12.97, -38.50),  # Salvador
    (-3.12, -60.02),   # Manaus
    (-8.05, -34.88),   # Recife
    (-25.43, -49.27),  # Curitiba
    (-30.03, -51.23),  # Porto Alegre
    (-1.46, -48.49),   # Belem
]


def _br_fallback_point(seed):
    # mesmo username sempre cai no mesmo ponto, pra nao "pular" de lugar a cada consulta
    idx = sum(ord(ch) for ch in seed) % len(_BR_FALLBACK_POINTS)
    return _BR_FALLBACK_POINTS[idx]


def _user_location(username):
    with db() as c:
        row = c.execute("SELECT country, city, lat, lng FROM users WHERE LOWER(name)=LOWER(?) OR email LIKE ?",
                         (username, username + "@%")).fetchone()
    if not row or row["lat"] is None or row["lng"] is None:
        country = (row["country"] if row else "") or ""
        city = (row["city"] if row else "") or ""
        # so usamos o fallback do Brasil quando nao ha pais declarado ou o pais e o Brasil,
        # para nao jogar no mapa quem claramente mora em outro lugar
        if not country or "brasil" in country.lower() or "brazil" in country.lower():
            lat, lng = _br_fallback_point(username.lower())
            return {"country": country or "Brasil", "city": city, "lat": lat, "lng": lng}
        return {"country": country, "city": city, "lat": None, "lng": None}
    return {"country": row["country"] or "", "city": row["city"] or "", "lat": row["lat"], "lng": row["lng"]}


def _user_is_pro(username):
    # mesma busca de _user_location: tenta achar a conta pelo nome ou pelo prefixo do e-mail
    with db() as c:
        row = c.execute("SELECT plan, is_admin FROM users WHERE LOWER(name)=LOWER(?) OR email LIKE ?",
                         (username, username + "@%")).fetchone()
    if not row:
        return False
    return row["plan"] == "pro" or bool(row["is_admin"])


@app.route("/api/live_channels", methods=["GET"])
def get_live_channels():
    # lista as lives conectadas agora no proprio Tique Bom, para a Home mostrar "canais ao vivo" e o globo
    out = []
    for uname, data in interactions.items():
        if data.get("status") != "connected":
            continue
        loc = _user_location(uname)
        # o TikTok as vezes demora (ou nunca chega) a mandar a contagem oficial de espectadores
        # (RoomUserSeqEvent); enquanto isso nao chega, usamos quem ja curtiu/comentou como estimativa,
        # so para o card nao ficar com "0 espectadores" numa live que claramente tem gente assistindo.
        # so mostramos a contagem OFICIAL do TikTok (RoomUserSeqEvent). Enquanto ela nao chega, fica 0
        # e o front mostra "—" em vez de um numero estimado.
        viewer_count = data.get("viewer_count", 0) or 0
        out.append({
            "username": uname,
            "viewer_count": viewer_count,
            "total_likes": data.get("total_likes", 0),
            "total_comments": data.get("total_comments", 0),
            "total_gifts": data.get("total_gifts", 0),
            "host_avatar": data.get("host_avatar", ""),
            "host_name": data.get("host_name", "") or uname,
            "is_pro": _user_is_pro(uname),
            "country": loc["country"], "city": loc["city"], "lat": loc["lat"], "lng": loc["lng"],
        })
    out.sort(key=lambda x: x["viewer_count"], reverse=True)
    return jsonify({
        "channels": out,
        "count": len(out),
        "total_viewers": sum(c["viewer_count"] for c in out),
        "total_gifts": sum(c["total_gifts"] for c in out),
    }), 200


@app.route("/api/interactions/<username>", methods=["GET"])
def get_interactions(username):
    username = username.strip().lstrip("@")
    data = interactions.get(username)

    if data is None:
        return jsonify({"error": "Usuario nao conectado"}), 404

    return jsonify({"interactions": data}), 200


@app.route("/api/likers/<username>", methods=["GET"])
def get_likers(username):
    username = username.strip().lstrip("@")
    data = interactions.get(username)

    if data is None:
        return jsonify({"likers": [], "status": "disconnected"}), 200

    # ordena do maior para o menor total de curtidas e devolve o top 10
    ranking = sorted(
        data["likers"].values(),
        key=lambda u: u["total_likes"],
        reverse=True,
    )[:10]

    return jsonify({
        "status": data.get("status", "disconnected"),
        "likers": [
            {"name": u["user"], "avatar": u["avatar"], "likes": u["total_likes"]}
            for u in ranking
        ]
    }), 200




# Configuracoes persistentes dos overlays (editor e OBS).
import json as overlay_json


def overlay_key(kind, username):
    return f"{kind}:{username.strip().lstrip('@').lower()}"


@app.route("/api/overlay-editor/<kind>/<username>", methods=["GET", "PUT"])
def overlay_editor(kind, username):
    if kind not in ("likes", "likers"):
        return jsonify({"error": "Overlay invalido"}), 404
    key = overlay_key(kind, username)
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS overlay_configs (id TEXT PRIMARY KEY, params TEXT NOT NULL, updated REAL NOT NULL)")
        row = c.execute("SELECT params,updated FROM overlay_configs WHERE id=?", (key,)).fetchone()
        if request.method == "PUT":
            if kind == "likers":
                with db() as auth_conn:
                    feature = auth_conn.execute("SELECT tier FROM features WHERE key='ranking_editor'").fetchone()
                if feature and feature["tier"] == "pro":
                    u = current_user()
                    if not u or (u["plan"] != "pro" and not u["is_admin"]):
                        return jsonify({"error": "A edição do ranking é exclusiva do plano Pro."}), 403
            body = request.get_json(silent=True) or {}
            params = body.get("params")
            if not isinstance(params, dict):
                return jsonify({"error": "Parametros invalidos"}), 400
            params = {str(k): str(v) for k,v in params.items() if len(str(k)) < 80 and len(str(v)) < 500}
            updated = time.time()
            c.execute("INSERT INTO overlay_configs(id,params,updated) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET params=excluded.params,updated=excluded.updated", (key, overlay_json.dumps(params), updated))
            return jsonify({"id": key, "params": params, "updated": updated})
        if row:
            return jsonify({"id": key, "params": overlay_json.loads(row["params"]), "updated": row["updated"]})
        return jsonify({"id": key, "params": {}})


@app.route("/api/overlay-config/<path:overlay_id>")
def overlay_config(overlay_id):
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS overlay_configs (id TEXT PRIMARY KEY, params TEXT NOT NULL, updated REAL NOT NULL)")
        row = c.execute("SELECT params,updated FROM overlay_configs WHERE id=?", (overlay_id,)).fetchone()
    if not row:
        return jsonify({"params": {}, "updated": 0})
    return jsonify({"params": overlay_json.loads(row["params"]), "updated": row["updated"]})


# ---------------- Contas, sessoes e recuperacao de senha ----------------
DB_PATH = os.environ.get("TB_DB", os.path.join(BASE_DIR, "tiquebom.db"))
APP_URL = os.environ.get("APP_URL", "http://localhost:5000").rstrip("/")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "231826480684-q1s3lthh7bm2u7g1ou7sfvguu9peq3qv.apps.googleusercontent.com").strip()
sha = lambda v: hashlib.sha256(v.encode()).hexdigest()


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


# ---------------- Recursos do app: Pro x Gratis ----------------
# PARA CRIAR UM RECURSO NOVO: adicione uma linha aqui (chave, nome, plano inicial).
# Ele aparece sozinho no painel Admin, onde voce troca entre Pro e Gratis com um clique.
# No HTML, coloque data-feature="chave" no bloco do recurso para ele ganhar o cadeado.
FEATURES = [
    ("overlay_likes",  "Overlay: Corações voando",        "free"),
    ("overlay_likers", "Overlay: Ranking de curtidores",  "pro"),
    ("ranking_editor", "Editor: Personalização do ranking", "pro"),
    ("ai_insights",    "IA de monetização",                "pro"),
]


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL,
          name TEXT NOT NULL, pw_hash TEXT NOT NULL, plan TEXT NOT NULL DEFAULT 'free',
          is_admin INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS resets(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL);
        """)
        # migracao: guarda o ID da conta Google (campo "sub") para quem entra com Google
        cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
        if "google_sub" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN google_sub TEXT")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_google_sub ON users(google_sub)")
        if "country" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN country TEXT")
        if "city" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN city TEXT")
        if "lat" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN lat REAL")
        if "lng" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN lng REAL")
        c.execute("CREATE TABLE IF NOT EXISTS features(key TEXT PRIMARY KEY, label TEXT NOT NULL, tier TEXT NOT NULL DEFAULT 'free')")
        c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')")
        # historico de lives: uma linha por sessao (conectar -> desconectar), usado pela aba de IA
        c.execute("""CREATE TABLE IF NOT EXISTS live_sessions(
          id INTEGER PRIMARY KEY, username TEXT NOT NULL,
          started REAL NOT NULL, ended REAL,
          total_comments INTEGER NOT NULL DEFAULT 0,
          total_gifts INTEGER NOT NULL DEFAULT 0,
          total_likes INTEGER NOT NULL DEFAULT 0,
          peak_viewers INTEGER NOT NULL DEFAULT 0,
          gifts_breakdown TEXT)""")
        for key, label, tier in FEATURES:
            # INSERT OR IGNORE: recurso novo entra com o plano inicial; os que ja existem mantem o que voce escolheu
            c.execute("INSERT OR IGNORE INTO features(key,label,tier) VALUES(?,?,?)", (key, label, tier))
            c.execute("UPDATE features SET label=? WHERE key=?", (label, key))


init_db()


def user_json(r):
    keys = r.keys() if hasattr(r, "keys") else []
    return {
        "id": r["id"], "email": r["email"], "name": r["name"], "plan": r["plan"], "admin": bool(r["is_admin"]),
        "country": (r["country"] if "country" in keys else "") or "",
        "city": (r["city"] if "city" in keys else "") or "",
        "lat": (r["lat"] if "lat" in keys else None),
        "lng": (r["lng"] if "lng" in keys else None),
    }


def new_session(c, uid):
    # login em um dispositivo novo derruba qualquer outra sessao ativa dessa conta
    # (evita que uma conta Pro seja compartilhada por varias pessoas ao mesmo tempo)
    c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO sessions VALUES(?,?,?)", (sha(token), uid, time.time()))
    return token


# MODO DEV SEM LOGIN: em localhost, todo mundo entra como admin. Desligue com DEV_NO_LOGIN=0.
DEV_NO_LOGIN = os.environ.get("DEV_NO_LOGIN", "0") == "1"
DEV_USER = {"id": 0, "email": "local@localhost", "name": "Local", "plan": "pro", "is_admin": 1}


def current_user():
    if DEV_NO_LOGIN and request.remote_addr in ("127.0.0.1", "::1"):
        return DEV_USER
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    with db() as c:
        return c.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.created>?",
            (sha(auth[7:]), time.time() - 30 * 86400),
        ).fetchone()


def require(admin=False):
    def deco(fn):
        @wraps(fn)
        def wrap(*a, **k):
            u = current_user()
            if not u:
                return jsonify({"error": "Entre na sua conta."}), 401
            if admin and not u["is_admin"]:
                return jsonify({"error": "Acesso restrito a administradores."}), 403
            return fn(u, *a, **k)
        return wrap
    return deco


def send_mail(to, subject, body):
    host = os.environ.get("SMTP_HOST")
    if not host:
        print(f"[E-MAIL NAO CONFIGURADO] Para {to} | {subject}\n{body}")
        return
    msg = EmailMessage()
    msg["From"] = os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER", "")
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(body)
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=15) as s:
        s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
        s.send_message(msg)


def body_json():
    return request.get_json(force=True, silent=True) or {}


@app.route("/api/auth/signup", methods=["POST"])
def register():
    b = body_json()
    email, name, pw = (b.get("email") or "").strip().lower(), (b.get("name") or "").strip(), b.get("password") or ""
    country = (b.get("country") or "").strip()[:80]
    city = (b.get("city") or "").strip()[:80]
    lat = b.get("lat"); lng = b.get("lng")
    try:
        lat = float(lat) if lat is not None and lat != "" else None
        lng = float(lng) if lng is not None and lng != "" else None
    except (TypeError, ValueError):
        lat = lng = None
    if not re.match(r"^\S+@\S+\.\S+$", email):
        return jsonify({"error": "Digite um e-mail valido."}), 400
    if not name:
        return jsonify({"error": "Digite seu nome."}), 400
    if len(pw) < 6:
        return jsonify({"error": "A senha precisa ter pelo menos 6 caracteres."}), 400
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            return jsonify({"error": "Esse e-mail ja tem conta. Use a aba Entrar."}), 409
        first = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        is_admin = 1 if (first or (ADMIN_EMAIL and email == ADMIN_EMAIL)) else 0
        cur = c.execute("INSERT INTO users(email,name,pw_hash,plan,is_admin,created,country,city,lat,lng) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (email, name, generate_password_hash(pw), "free", is_admin, time.time(), country, city, lat, lng))
        token = new_session(c, cur.lastrowid)
        u = c.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify({"token": token, "user": user_json(u)}), 201


@app.route("/api/auth/login", methods=["POST"])
def login():
    b = body_json()
    email, pw = (b.get("email") or "").strip().lower(), b.get("password") or ""
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not u or not check_password_hash(u["pw_hash"], pw):
            return jsonify({"error": "E-mail ou senha incorretos."}), 401
        token = new_session(c, u["id"])
    return jsonify({"token": token, "user": user_json(u)}), 200



@app.route("/api/auth/config", methods=["GET"])
def auth_config():
    # o front usa isso para saber se deve mostrar o botao do Google
    return jsonify({"google_client_id": GOOGLE_CLIENT_ID}), 200


@app.route("/api/auth/google", methods=["POST"])
def google_login():
    if not GOOGLE_CLIENT_ID:
        return jsonify({"error": "Login com Google ainda nao esta configurado."}), 503
    credential = body_json().get("credential") or ""
    try:
        # valida assinatura, validade e se o token foi emitido para o NOSSO client id
        info = google_id_token.verify_oauth2_token(credential, google_requests.Request(), GOOGLE_CLIENT_ID, clock_skew_in_seconds=30)
    except Exception as e:
        print("[Google login] Token invalido -> motivo:", repr(e), "| client_id usado no servidor:", GOOGLE_CLIENT_ID)
        return jsonify({"error": "Nao foi possivel validar sua conta Google. Tente de novo."}), 401

    if not info.get("email_verified"):
        return jsonify({"error": "Seu e-mail do Google nao esta verificado."}), 400

    sub = info["sub"]
    email = (info.get("email") or "").strip().lower()
    name = (info.get("name") or email.split("@")[0]).strip()

    with db() as c:
        u = c.execute("SELECT * FROM users WHERE google_sub=?", (sub,)).fetchone()
        if not u:
            u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if u:
                # ja tinha conta com esse e-mail (verificado pelo Google): vincula
                c.execute("UPDATE users SET google_sub=? WHERE id=?", (sub, u["id"]))
            else:
                first = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
                is_admin = 1 if (first or (ADMIN_EMAIL and email == ADMIN_EMAIL)) else 0
                # senha aleatoria: a conta so entra pelo Google (ou depois de "Esqueci minha senha")
                cur = c.execute(
                    "INSERT INTO users(email,name,pw_hash,plan,is_admin,created,google_sub) VALUES(?,?,?,?,?,?,?)",
                    (email, name, generate_password_hash(secrets.token_urlsafe(32)), "free", is_admin, time.time(), sub))
                u = c.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
        token = new_session(c, u["id"])
    return jsonify({"token": token, "user": user_json(u)}), 200

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        with db() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (sha(auth[7:]),))
    return jsonify({"ok": True}), 200


@app.route("/api/auth/me", methods=["GET"])
@require()
def me(u):
    return jsonify({"user": user_json(u), "dev_no_login": bool(DEV_NO_LOGIN and request.remote_addr in ("127.0.0.1", "::1"))}), 200


@app.route("/api/auth/location", methods=["POST"])
@require()
def set_location(u):
    b = body_json()
    country = (b.get("country") or "").strip()[:80]
    city = (b.get("city") or "").strip()[:80]
    lat = b.get("lat"); lng = b.get("lng")
    try:
        lat = float(lat) if lat is not None and lat != "" else None
        lng = float(lng) if lng is not None and lng != "" else None
    except (TypeError, ValueError):
        lat = lng = None
    if not country and not city:
        return jsonify({"error": "Informe pelo menos o pais ou a cidade."}), 400
    with db() as c:
        c.execute("UPDATE users SET country=?, city=?, lat=?, lng=? WHERE id=?", (country, city, lat, lng, u["id"]))
        u = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
    return jsonify({"user": user_json(u)}), 200


@app.route("/api/auth/forgot", methods=["POST"])
def forgot():
    email = (body_json().get("email") or "").strip().lower()
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if u:
            token = secrets.token_urlsafe(32)
            c.execute("INSERT INTO resets VALUES(?,?,?)", (sha(token), u["id"], time.time() + 3600))
    if u:
        link = f"{APP_URL}/?reset={token}"
        try:
            send_mail(email, "TIKBOM: crie uma nova senha",
                      f"Ola, {u['name']}!\n\nPara criar uma nova senha, abra o link (vale por 1 hora):\n{link}\n\nSe nao foi voce, ignore este e-mail.")
        except Exception as e:
            print("Erro ao enviar e-mail:", e)
    # resposta igual exista a conta ou nao, para nao revelar quem tem cadastro
    return jsonify({"ok": True}), 200


@app.route("/api/auth/reset", methods=["POST"])
def reset():
    b = body_json()
    token, pw = b.get("token") or "", b.get("password") or ""
    if len(pw) < 6:
        return jsonify({"error": "A senha precisa ter pelo menos 6 caracteres."}), 400
    with db() as c:
        r = c.execute("SELECT * FROM resets WHERE token_hash=? AND expires>?", (sha(token), time.time())).fetchone()
        if not r:
            return jsonify({"error": "Link invalido ou expirado. Peca um novo."}), 400
        c.execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(pw), r["user_id"]))
        c.execute("DELETE FROM resets WHERE user_id=?", (r["user_id"],))
        c.execute("DELETE FROM sessions WHERE user_id=?", (r["user_id"],))
    return jsonify({"ok": True}), 200


@app.route("/api/auth/upgrade", methods=["POST"])
@require()
def activate_pro(u):
    # Ativacao direta, so para teste. Desligue com ALLOW_FAKE_PRO=0 quando houver pagamento real.
    if os.environ.get("ALLOW_FAKE_PRO", "1") != "1":
        return jsonify({"error": "Pagamento ainda nao disponivel."}), 403
    if u["id"] == 0:  # usuario do modo dev
        return jsonify({"user": user_json(u), "dev_no_login": bool(DEV_NO_LOGIN and request.remote_addr in ("127.0.0.1", "::1"))}), 200
    with db() as c:
        c.execute("UPDATE users SET plan='pro' WHERE id=?", (u["id"],))
        u = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
    return jsonify({"user": user_json(u), "dev_no_login": bool(DEV_NO_LOGIN and request.remote_addr in ("127.0.0.1", "::1"))}), 200


@app.route("/api/admin/users", methods=["GET"])
@require(admin=True)
def admin_users(u):
    with db() as c:
        rows = c.execute("SELECT * FROM users ORDER BY id").fetchall()
    return jsonify({"users": [user_json(r) for r in rows]}), 200


@app.route("/api/admin/users/<int:uid>/plan", methods=["POST"])
@require(admin=True)
def admin_set_plan(u, uid):
    plan = body_json().get("plan")
    if plan not in ("free", "pro"):
        return jsonify({"error": "Plano invalido."}), 400
    with db() as c:
        c.execute("UPDATE users SET plan=? WHERE id=?", (plan, uid))
    return jsonify({"ok": True}), 200


SOCIAL_KEYS = ("tiktok", "youtube", "instagram", "facebook")


def read_social():
    with db() as c:
        rows = c.execute("SELECT key,value FROM settings WHERE key LIKE 'social_%'").fetchall()
    links = {k: "" for k in SOCIAL_KEYS}
    for r in rows:
        links[r["key"][7:]] = r["value"]
    return links


@app.route("/api/social", methods=["GET"])
def get_social():
    return jsonify({"links": read_social()}), 200


@app.route("/api/admin/social", methods=["POST"])
@require(admin=True)
def admin_set_social(u):
    links = (body_json().get("links") or {})
    clean = {}
    for k in SOCIAL_KEYS:
        v = str(links.get(k, "")).strip()
        if v and not re.fullmatch(r"https?://[^\s\"'<>]+", v):
            return jsonify({"error": "Link invalido em " + k + ". Use https://..."}), 400
        clean[k] = v
    with db() as c:
        for k, v in clean.items():
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("social_" + k, v))
    return jsonify({"links": clean}), 200


@app.route("/api/features", methods=["GET"])
def list_features():
    with db() as c:
        rows = c.execute("SELECT key,label,tier FROM features ORDER BY rowid").fetchall()
    return jsonify({"features": [dict(r) for r in rows]}), 200


@app.route("/api/admin/features/<key>", methods=["POST"])
@require(admin=True)
def admin_set_feature(u, key):
    tier = body_json().get("tier")
    if tier not in ("free", "pro"):
        return jsonify({"error": "Plano invalido."}), 400
    with db() as c:
        cur = c.execute("UPDATE features SET tier=? WHERE key=?", (tier, key))
        if cur.rowcount == 0:
            return jsonify({"error": "Recurso nao encontrado."}), 404
    return jsonify({"ok": True}), 200


# Busca a foto de perfil por tras do servidor e entrega pronta pro navegador,
# pois o TikTok bloqueia essas imagens quando carregadas direto do navegador do usuario.
_AVATAR_CACHE = {}  # url -> (bytes, content_type, timestamp)
_AVATAR_TTL = 600  # 10 minutos


@app.route("/api/avatar", methods=["GET"])
def proxy_avatar():
    url = clean_url(request.args.get("url", ""))
    host = (urlparse(url).hostname or "").lower()
    allowed = any(host == h or host.endswith("." + h) for h in AVATAR_HOSTS)
    if not url.startswith("https://") or not allowed:
        print(f"avatar bloqueado: host={host!r} url={url!r}")
        return "URL invalida", 400

    cached = _AVATAR_CACHE.get(url)
    if cached and time.time() - cached[2] < _AVATAR_TTL:
        return app.response_class(cached[0], mimetype=cached[1])

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            img_bytes = resp.read()
            content_type = resp.headers.get("Content-Type", "image/jpeg")
        _AVATAR_CACHE[url] = (img_bytes, content_type, time.time())
        return app.response_class(img_bytes, mimetype=content_type)
    except Exception as e:
        print(f"erro ao buscar avatar: {e}")
        return "Erro ao buscar imagem", 502


@app.route("/api/admin/lives", methods=["GET"])
@require(admin=True)
def admin_lives(u):
    return jsonify({"lives": [
        {"username": n, "status": d["status"], "total_likes": d["total_likes"],
         "total_comments": d["total_comments"], "total_gifts": d["total_gifts"]}
        for n, d in interactions.items()
    ]}), 200


# ---------------- IA: analise de desempenho e dicas de monetizacao ----------------
import json as _json
from datetime import datetime


def _session_hour_bucket(started_ts):
    # agrupa cada live por faixa de 2 horas (0-2h, 2-4h, ... 22-24h) no horario local do servidor
    h = datetime.fromtimestamp(started_ts).hour
    start = (h // 2) * 2
    return f"{start:02d}h-{(start+2)%24:02d}h"


@app.route("/api/ai/history/<username>", methods=["GET"])
@require()
def ai_history(u, username):
    username = username.strip().lstrip("@")
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM live_sessions WHERE username=? AND ended IS NOT NULL ORDER BY started DESC LIMIT 60",
            (username,),
        ).fetchall()
    out = []
    for r in rows:
        dur_min = round((r["ended"] - r["started"]) / 60, 1) if r["ended"] else None
        out.append({
            "started": r["started"], "ended": r["ended"], "duration_min": dur_min,
            "total_comments": r["total_comments"], "total_gifts": r["total_gifts"],
            "total_likes": r["total_likes"],
            "gifts_breakdown": _json.loads(r["gifts_breakdown"]) if r["gifts_breakdown"] else {},
        })
    return jsonify({"sessions": out}), 200


@app.route("/api/ai/insights/<username>", methods=["GET"])
@require()
def ai_insights(u, username):
    username = username.strip().lstrip("@")
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM live_sessions WHERE username=? AND ended IS NOT NULL ORDER BY started DESC LIMIT 60",
            (username,),
        ).fetchall()

    # live em andamento agora: entra na analise como mais uma sessao, junto do historico
    live_now = interactions.get(username)
    live_now_entry = None
    if live_now and live_now.get("status") == "connected":
        live_now_entry = {
            "started": live_now.get("_session_started") or time.time(),
            "total_comments": live_now.get("total_comments", 0),
            "total_gifts": live_now.get("total_gifts", 0),
            "total_likes": live_now.get("total_likes", 0),
            "gifts_breakdown": _json.dumps({
                g["gift_name"]: sum(x.get("quantity", 1) for x in live_now.get("gifts", []) if x["gift_name"] == g["gift_name"])
                for g in live_now.get("gifts", [])
            }, ensure_ascii=False) if live_now.get("gifts") else None,
        }

    if not rows and not live_now_entry:
        return jsonify({
            "has_data": False,
            "message": "Ainda nao ha historico suficiente. Faca algumas lives com o Tique Bom conectado para a IA comecar a gerar dicas.",
        }), 200

    all_sessions = [dict(r) for r in rows]
    if live_now_entry:
        all_sessions.insert(0, live_now_entry)  # a live atual entra tambem no calculo

    # engajamento por sessao = comentarios + presentes*5 + curtidas/50 (presentes pesam mais p/ monetizacao)
    buckets = {}  # faixa de horario -> {"score":..,"n":..,"gifts":..,"likes":..,"comments":..}
    gift_totals = {}
    best_session = None
    for r in all_sessions:
        score = r["total_comments"] + r["total_gifts"] * 5 + r["total_likes"] / 50
        bucket = _session_hour_bucket(r["started"])
        b = buckets.setdefault(bucket, {"score": 0.0, "n": 0, "gifts": 0, "likes": 0, "comments": 0})
        b["score"] += score
        b["n"] += 1
        b["gifts"] += r["total_gifts"]
        b["likes"] += r["total_likes"]
        b["comments"] += r["total_comments"]
        if best_session is None or score > best_session["score"]:
            best_session = {"score": score, "started": r["started"], "total_gifts": r["total_gifts"]}
        if r["gifts_breakdown"]:
            for name, qty in _json.loads(r["gifts_breakdown"]).items():
                gift_totals[name] = gift_totals.get(name, 0) + qty

    # media de score por faixa de horario, para nao premiar so quem transmitiu mais vezes num horario
    ranked = sorted(
        ((bucket, v["score"] / v["n"], v) for bucket, v in buckets.items()),
        key=lambda x: x[1], reverse=True,
    )
    top_bucket = ranked[0][0] if ranked else None
    top_gifts = sorted(gift_totals.items(), key=lambda x: x[1], reverse=True)[:5]

    total_sessions = len(all_sessions)
    avg_gifts = sum(r["total_gifts"] for r in all_sessions) / total_sessions
    avg_comments = sum(r["total_comments"] for r in all_sessions) / total_sessions

    tips = []
    if live_now_entry:
        tips.append("Esta analise ja inclui a live que esta no ar agora, junto com o seu historico.")
    if top_bucket:
        tips.append(f"Seu horario com mais engajamento costuma ser entre {top_bucket}. Priorize lives nesse intervalo.")
    if top_gifts:
        nomes = ", ".join(f"{n} ({q}x)" for n, q in top_gifts[:3])
        tips.append(f"Os presentes que voce mais recebe sao: {nomes}. Vale agradecer e reagir forte quando chegarem, isso incentiva mais do mesmo.")
    if avg_comments > 0 and avg_gifts / max(avg_comments, 1) < 0.02:
        tips.append("Poucos comentarios estao virando presente. Tente lembrar a audiencia sobre os presentes de vez em quando, de forma natural, sem forcar.")
    if total_sessions < 5:
        tips.append("Seu historico ainda e pequeno. As dicas ficam mais precisas depois de algumas lives.")

    return jsonify({
        "has_data": True,
        "live_now": bool(live_now_entry),
        "total_sessions": total_sessions,
        "avg_gifts_per_live": round(avg_gifts, 1),
        "avg_comments_per_live": round(avg_comments, 1),
        "best_hour_range": top_bucket,
        "ranking_by_hour": [
            {"range": b, "avg_score": round(sc, 1), "sessions": v["n"]}
            for b, sc, v in ranked
        ],
        "top_gifts": [{"name": n, "qty": q} for n, q in top_gifts],
        "tips": tips,
    }), 200


def _chat_context(username):
    """Resumo curto do historico do streamer, pra dar contexto a IA no chat."""
    import json as _json
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM live_sessions WHERE username=? AND ended IS NOT NULL ORDER BY started DESC LIMIT 60",
            (username,),
        ).fetchall()

    live_agora = interactions.get(username)
    partes = []
    if live_agora and live_agora.get("status") == "connected":
        partes.append(
            f"Live em andamento agora: {live_agora.get('viewer_count', 0)} espectadores, "
            f"{live_agora.get('total_comments', 0)} comentarios, {live_agora.get('total_gifts', 0)} presentes, "
            f"{live_agora.get('total_likes', 0)} curtidas."
        )

    if not rows:
        partes.append("Ainda nao ha historico de lives encerradas.")
        return " ".join(partes)

    total_sessions = len(rows)
    avg_gifts = sum(r["total_gifts"] for r in rows) / total_sessions
    avg_comments = sum(r["total_comments"] for r in rows) / total_sessions
    gift_totals = {}
    for r in rows:
        if r["gifts_breakdown"]:
            for name, qty in _json.loads(r["gifts_breakdown"]).items():
                gift_totals[name] = gift_totals.get(name, 0) + qty
    top_gifts = sorted(gift_totals.items(), key=lambda x: x[1], reverse=True)[:5]

    partes.append(f"Historico: {total_sessions} lives encerradas registradas.")
    partes.append(f"Media de {avg_gifts:.1f} presentes e {avg_comments:.1f} comentarios por live.")
    if top_gifts:
        partes.append("Presentes mais recebidos: " + ", ".join(f"{n} ({q}x)" for n, q in top_gifts) + ".")
    return " ".join(partes)


@app.route("/api/ai/chat/<username>", methods=["POST"])
@require()
def ai_chat(u, username):
    import json as _json
    username = username.strip().lstrip("@")
    if not GOOGLE_API_KEY:
        return jsonify({"error": "A chave da IA nao esta configurada no servidor."}), 500

    data = request.get_json(silent=True) or {}
    messages = data.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return jsonify({"error": "Mensagem vazia."}), 400

    contexto = _chat_context(username)
    system_instruction = {
        "parts": [{"text": (
            "Voce e o Tiquinho, um assistente amigavel que ajuda streamers de TikTok Live a "
            "monetizar e crescer no Tique Bom Live. Responda sempre em portugues, de forma "
            "curta, natural e variada (nunca repita a mesma frase de abertura em mensagens "
            "diferentes). No maximo 5 frases. Use os dados reais abaixo quando forem relevantes "
            "para a pergunta, mas nao force um resumo deles se o usuario so cumprimentar ou "
            "perguntar outra coisa. Nunca invente numeros, e nunca diga que vai 'analisar a live "
            "agora': essa funcao e tratada por outro botao do site, nao pelo chat. "
            f"Dados do streamer @{username}: {contexto}"
        )}]
    }
    contents = []
    for m in messages[-12:]:
        role = "model" if m.get("role") == "assistant" else "user"
        text = str(m.get("content", "")).strip()
        if text:
            contents.append({"role": role, "parts": [{"text": text}]})
    if not contents:
        return jsonify({"error": "Mensagem vazia."}), 400

    payload = _json.dumps({
        "system_instruction": system_instruction,
        "contents": contents,
    }).encode("utf-8")

    body = None
    last_error = None
    for model in AI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        req = urllib.request.Request(url, data=payload, method="POST", headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GOOGLE_API_KEY,
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = _json.loads(resp.read().decode("utf-8"))
            break  # funcionou, para de tentar
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            print(f"[ai_chat] HTTPError {e.code} (modelo={model}): {err_body}")
            if e.code in (400, 401, 403):
                # problema na chave/requisicao: trocar de modelo nao resolve
                return jsonify({"error": "A chave da IA (GOOGLE_API_KEY) foi recusada. Confira o arquivo .env."}), 502
            last_error = e.code
            continue  # 404, 429, 5xx: tenta o proximo modelo
        except Exception as e:
            print(f"[ai_chat] erro (modelo={model}): {e}")
            last_error = str(e)
            continue

    if body is None:
        if last_error == 404:
            msg = "Nenhum dos modelos de IA configurados foi encontrado. Confira AI_MODELS no .env."
        else:
            msg = "A IA nao conseguiu responder agora. Tente de novo em instantes."
        return jsonify({"error": msg}), 502

    try:
        reply = body["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        reply = "Nao consegui gerar uma resposta agora. Tente perguntar de outro jeito."

    return jsonify({"reply": reply}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
