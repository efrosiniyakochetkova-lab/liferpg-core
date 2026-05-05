"""
Life RPG — Living Narrative OS v4
Parchment · Knowledge Graph · Колесо Миров · AI Архивариус
"""
import json, uuid, re, subprocess, time, threading, os, hashlib, hmac, secrets
import urllib.request as _ur
from datetime import datetime
from pathlib import Path

import kuzu
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

_DATA_DIR = Path("/data") if Path("/data").exists() else Path(__file__).parent
APP_CFG_FILE = _DATA_DIR / "app_config.json"

def _app_cfg():
    if APP_CFG_FILE.exists():
        try: return json.loads(APP_CFG_FILE.read_text())
        except: pass
    return {}

def _get_api_key():
    return _app_cfg().get("api_key") or os.environ.get("ANTHROPIC_API_KEY","")

def _get_gigachat_key():
    return _app_cfg().get("gigachat_key") or os.environ.get("GIGACHAT_API_KEY","")

def _get_gigachat_scope():
    return _app_cfg().get("gigachat_scope") or os.environ.get("GIGACHAT_SCOPE","GIGACHAT_API_PERS")

_gc_token_cache = {"token": "", "expires": 0.0}

def _get_gigachat_token(key: str, scope: str) -> str:
    global _gc_token_cache
    if _gc_token_cache["token"] and time.time() < _gc_token_cache["expires"] - 60:
        return _gc_token_cache["token"]
    try:
        import httpx, ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        r = httpx.post(
            "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
            headers={"Authorization": f"Basic {key}",
                     "RqUID": str(uuid.uuid4()),
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"scope": scope},
            verify=False, timeout=15)
        d = r.json()
        tok = d.get("access_token","")
        exp = d.get("expires_at", 0) / 1000.0  # ms → s
        _gc_token_cache = {"token": tok, "expires": exp or time.time()+1700}
        return tok
    except Exception as e:
        print(f"[gigachat_token] {e}"); return ""

def _call_gigachat(prompt_text: str) -> str:
    key = _get_gigachat_key()
    scope = _get_gigachat_scope()
    if not key: return ""
    tok = _get_gigachat_token(key, scope)
    if not tok: return ""
    try:
        import httpx
        r = httpx.post(
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            json={"model":"GigaChat","messages":[{"role":"user","content":prompt_text}],
                  "max_tokens":2048,"temperature":0.3},
            verify=False, timeout=60)
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[gigachat] {e}"); return ""

def call_ai_extract(raw: str, user_id: str = "admin") -> dict:
    """Call AI (Anthropic or GigaChat). Returns parsed dict or None."""
    ent_ctx, miss_ctx = build_context(user_id)
    p = PROMPT.format(raw=raw, entities_ctx=ent_ctx or "нет",
                      missions_ctx=miss_ctx or "нет активных путей")
    # Try Anthropic first
    ant_key = _get_api_key()
    if ant_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ant_key)
            msg = client.messages.create(model="claude-haiku-4-5", max_tokens=2048,
                messages=[{"role":"user","content":p}])
            text = msg.content[0].text if msg.content else ""
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m: return json.loads(m.group())
        except Exception as e: print(f"[anthropic] {e}")
    # Try GigaChat
    if _get_gigachat_key():
        try:
            text = _call_gigachat(p)
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m: return json.loads(m.group())
        except Exception as e: print(f"[gigachat_parse] {e}")
    return None

def call_claude_extract(raw: str, user_id: str = "admin") -> dict:  # backward compat alias
    return call_ai_extract(raw, user_id)

# ── Auth ─────────────────────────────────────────────────────────────────────
SESSIONS_FILE = _DATA_DIR / "sessions.json"
_SECRET = os.environ.get("SESSION_SECRET", "liferpg-secret-change-in-prod")

def _hash_password(pw: str) -> str:
    return hashlib.sha256((pw + _SECRET).encode()).hexdigest()

def _sessions() -> dict:
    if SESSIONS_FILE.exists():
        try: return json.loads(SESSIONS_FILE.read_text())
        except: pass
    return {}

def _save_sessions(s: dict):
    SESSIONS_FILE.write_text(json.dumps(s, ensure_ascii=False))

def _create_token(user_id: str, login: str) -> str:
    tok = secrets.token_hex(32)
    s = _sessions(); s[tok] = {"user_id": user_id, "login": login}
    _save_sessions(s); return tok

def _token_to_user(token: str) -> dict | None:
    return _sessions().get(token)

_bearer = HTTPBearer(auto_error=False)

def current_user(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    if cred:
        u = _token_to_user(cred.credentials)
        if u: return u
    raise HTTPException(401, "Необходима авторизация")

def optional_user(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict | None:
    if cred: return _token_to_user(cred.credentials)
    return None

def _uid(user: dict | None) -> str:
    return user["user_id"] if user else "admin"

def _scoped_id(base_id: str, user_id: str) -> str:
    return base_id if user_id == "admin" else f"{user_id}:{base_id}"

def _entity_id(name: str, user_id: str) -> str:
    return _scoped_id(slug(name), user_id)

def _mission_entity_id(mid: str, title: str, user_id: str) -> str:
    if user_id == "admin":
        return "mission_" + slug(title)
    return _scoped_id(f"mission:{mid}", user_id)

def _user_json_file(base_name: str, user_id: str) -> Path:
    if user_id == "admin":
        return _DATA_DIR / base_name
    stem, suffix = base_name.rsplit(".", 1)
    return _DATA_DIR / f"{stem}.{user_id}.{suffix}"

# ─────────────────────────────────────────────────────────────────────────────
DB_PATH   = str(_DATA_DIR / "liferpg.db")
_VER_F    = _DATA_DIR / ".schema_v"
_SCHEMA   = "5"

_db   = kuzu.Database(DB_PATH)
_conn = kuzu.Connection(_db)

def _drop_all():
    for t in ["LINKED","MENTIONS"]:      # rels first
        try: _conn.execute(f"DROP TABLE {t}")
        except: pass
    for t in ["Entry","Entity","Mission","Task","Finance","Mode"]:
        try: _conn.execute(f"DROP TABLE {t}")
        except: pass

def _setup():
    cv = _VER_F.read_text().strip() if _VER_F.exists() else "0"
    if cv != _SCHEMA:
        if os.environ.get("LIFERPG_RESET_SCHEMA") == "1":
            _drop_all()
        _VER_F.write_text(_SCHEMA)
    _conn.execute("""CREATE NODE TABLE IF NOT EXISTS Entry(
        id STRING, ts STRING, raw_text STRING, narrative STRING,
        archivist_note STRING, PRIMARY KEY(id))""")
    _conn.execute("""CREATE NODE TABLE IF NOT EXISTS Entity(
        id STRING, name STRING, type STRING, summary STRING, tags STRING,
        PRIMARY KEY(id))""")
    _conn.execute("CREATE REL TABLE IF NOT EXISTS MENTIONS(FROM Entry TO Entity)")
    _conn.execute("""CREATE REL TABLE IF NOT EXISTS LINKED(
        FROM Entity TO Entity, label STRING, entry_id STRING)""")
    _conn.execute("""CREATE NODE TABLE IF NOT EXISTS Mission(
        id STRING, title STRING, description STRING, status STRING,
        ts STRING, PRIMARY KEY(id))""")
    _conn.execute("""CREATE NODE TABLE IF NOT EXISTS Task(
        id STRING, mission_id STRING, title STRING, status STRING,
        ts STRING, entry_id STRING, PRIMARY KEY(id))""")
    _conn.execute("""CREATE NODE TABLE IF NOT EXISTS Finance(
        id STRING, amount DOUBLE, direction STRING, category STRING,
        note STRING, ts STRING, PRIMARY KEY(id))""")
    _conn.execute("""CREATE NODE TABLE IF NOT EXISTS Mode(
        id STRING, name STRING, description STRING, active STRING,
        started_ts STRING, PRIMARY KEY(id))""")
    _conn.execute("""CREATE NODE TABLE IF NOT EXISTS User(
        id STRING, login STRING, password_hash STRING, ts STRING,
        PRIMARY KEY(id))""")
_setup()

def _migrate_user_columns():
    """Add user_id to all node tables (idempotent)."""
    for tbl in ("Entry","Entity","Mission","Task","Finance","Mode"):
        try: _conn.execute(f"ALTER TABLE {tbl} ADD user_id STRING DEFAULT 'admin'")
        except: pass

def _migrate_task_columns():
    """Add new columns without dropping data (idempotent)."""
    for col, dtype, default in [
        ("task_type",      "STRING", "'once'"),
        ("reset_hours",    "INT64",  "24"),
        ("required_iters", "INT64",  "1"),
        ("current_iters",  "INT64",  "0"),
        ("last_reset_ts",  "STRING", "''"),
        ("streak",         "INT64",  "0"),
        ("best_streak",    "INT64",  "0"),
    ]:
        try: _conn.execute(f"ALTER TABLE Task ADD {col} {dtype} DEFAULT {default}")
        except: pass
    try: _conn.execute("ALTER TABLE Mission ADD lore STRING DEFAULT ''")
    except: pass
    try: _conn.execute("ALTER TABLE Task ADD completed_ts STRING DEFAULT ''")
    except: pass
_migrate_task_columns()

def _maybe_reset_task(t: dict) -> dict:
    """Check if repeatable task cycle is over; update DB and return updated dict."""
    if t.get("task_type") != "repeat" or not t.get("last_reset_ts"):
        return t
    try:
        from datetime import timedelta
        last = datetime.strptime(t["last_reset_ts"], "%Y-%m-%d %H:%M")
        due  = last + timedelta(hours=int(t.get("reset_hours", 24)))
        if datetime.now() < due:
            return t
        # Reset cycle
        cur  = int(t.get("current_iters", 0))
        req  = int(t.get("required_iters", 1))
        stk  = int(t.get("streak", 0))
        best = int(t.get("best_streak", 0))
        if cur >= req: stk += 1; best = max(best, stk)
        else:          stk = 0
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M")
        _conn.execute(
            "MATCH (t:Task) WHERE t.id=$id "
            "SET t.current_iters=0,t.last_reset_ts=$ts,t.streak=$s,t.best_streak=$b",
            {"id": t["id"], "ts": now_s, "s": stk, "b": best})
        t.update({"current_iters": 0, "last_reset_ts": now_s, "streak": stk, "best_streak": best})
    except: pass
    return t

# ── Helpers ───────────────────────────────────────────────────────────────────
def slug(name):
    s = re.sub(r"[\s\-]+","_", name.lower().strip())
    return re.sub(r"[^a-z0-9а-яё_]","",s) or "unknown"

def kuzu_rows(r):
    rows=[]
    while r.has_next(): rows.append(r.get_next())
    return rows

def _ensure_admin_user():
    r = kuzu_rows(_conn.execute("MATCH (u:User) WHERE u.login='admin' RETURN u.id"))
    if not r:
        _conn.execute("CREATE (:User {id:'admin',login:'admin',password_hash:$ph,ts:$ts})",
                      {"ph": _hash_password("admin"), "ts": datetime.now().strftime("%Y-%m-%d %H:%M")})
_migrate_user_columns()
_ensure_admin_user()

def entity_exists(eid, user_id: str | None = None):
    if user_id:
        r=_conn.execute("MATCH (e:Entity) WHERE e.id=$id AND e.user_id=$uid RETURN count(e)",
                        {"id":eid,"uid":user_id})
    else:
        r=_conn.execute("MATCH (e:Entity) WHERE e.id=$id RETURN count(e)",{"id":eid})
    return r.get_next()[0]>0 if r.has_next() else False

def build_context(user_id: str = "admin"):
    ents = kuzu_rows(_conn.execute(
        "MATCH (e:Entity) WHERE e.user_id=$uid RETURN e.name,e.type,e.summary LIMIT 40",
        {"uid":user_id}))
    missions = kuzu_rows(_conn.execute(
        "MATCH (m:Mission) WHERE m.status='active' AND m.user_id=$uid RETURN m.id,m.title LIMIT 10",
        {"uid":user_id}))
    ent_ctx  = "\n".join(f"- {r[0]} ({r[1]}): {r[2]}" for r in ents)
    miss_ctx = "\n".join(f"  ID={r[0]}: {r[1]}" for r in missions)
    return ent_ctx, miss_ctx

PROMPT = """Ты — Архивариус, хранитель Живой Летописи. Анализируй запись Героя и верни ТОЛЬКО валидный JSON без markdown.

АКТИВНЫЕ МЕЧТЫ ГЕРОЯ:
{missions_ctx}

ИЗВЕСТНЫЕ СУЩНОСТИ В ЛЕТОПИСИ:
{entities_ctx}

Верни JSON строго в таком формате:
{{
  "narrative": "2-4 предложения от третьего лица — стиль Morrowind, хроника, величие, конкретные детали",
  "entities": [
    {{"name":"Имя","type":"person|place|concept|project|event|object","summary":"одно ёмкое предложение","tags":[]}}
  ],
  "relations": [
    {{"from_entity":"Имя1","to_entity":"Имя2","label":"глагол отношения"}}
  ],
  "quests": [
    {{"title":"конкретный шаг или задача","mission_id":"id пути если явно связано или пустая строка","description":"контекст","task_type":"once или repeat","reset_hours":24,"required_iters":1}}
  ],
  "archivist_note": "1-2 предложения — мудрость Архивариуса о значении этой записи для судьбы Героя. Нарративно, эпически.",
  "mission_analysis": [
    {{"mission_id":"id","insight":"что эта запись даёт или блокирует","lore":"1-2 предложения в стиле летописи Морровинда — что этот Путь значит для Героя, конкретно и нарративно"}}
  ]
}}

ПРАВИЛА:
- entities: только реально упомянутые люди, места, идеи, проекты, предметы — не выдумывай; object — физические предметы и инструменты (самокат, камера, ноутбук)
- relations: только если связь явно следует из текста
- quests: только конкретные задачи/обязательства, явно упоминаемые
- Если сущность уже есть в летописи — обновляй summary, не дублируй
- mission_id в quests: точный UUID из списка путей выше, или "" если неясно
- task_type: "repeat" если задача ежедневная/регулярная/повторяемая, иначе "once"
- reset_hours: интервал в часах между сбросами (24 = ежедневно, 168 = еженедельно и т.д.)
- required_iters: сколько раз нужно выполнить задачу за один цикл (обычно 1, но может быть больше)
- Никогда не добавляй поля вне схемы

ЗАПИСЬ ГЕРОЯ:
{raw}"""

def extract(raw, user_id: str = "admin"):
    ent_ctx, miss_ctx = build_context(user_id)
    p = PROMPT.format(raw=raw,
                      entities_ctx=ent_ctx or "нет",
                      missions_ctx=miss_ctx or "нет активных путей")
    try:
        r = subprocess.run(["claude","-p",p], capture_output=True, text=True, timeout=90)
        if r.returncode == 0:
            m = re.search(r'\{.*\}', r.stdout.strip(), re.DOTALL)
            if m: return json.loads(m.group())
    except Exception:
        pass
    return {"narrative":raw,"entities":[],"relations":[],"quests":[],
            "archivist_note":"","mission_analysis":[],"_ai_pending":True}

def write_entry(raw, data, user_id: str = "admin"):
    eid = str(uuid.uuid4())
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M")
    an  = data.get("archivist_note","")
    _conn.execute(
        "CREATE (:Entry {id:$id,ts:$ts,raw_text:$r,narrative:$n,archivist_note:$an,user_id:$uid})",
        {"id":eid,"ts":ts,"r":raw,"n":data.get("narrative",raw),"an":an,"uid":user_id})
    for ent in data.get("entities",[]):
        sid  = _entity_id(ent["name"], user_id)
        tags = json.dumps(ent.get("tags",[]), ensure_ascii=False)
        if entity_exists(sid, user_id):
            _conn.execute(
                "MATCH (e:Entity) WHERE e.id=$id AND e.user_id=$uid SET e.summary=$s, e.tags=$t",
                {"id":sid,"uid":user_id,"s":ent["summary"],"t":tags})
        else:
            _conn.execute(
                "CREATE (:Entity {id:$id,name:$name,type:$type,summary:$summary,tags:$tags,user_id:$uid})",
                {"id":sid,"name":ent["name"],"type":ent.get("type","concept"),
                 "summary":ent["summary"],"tags":tags,"uid":user_id})
        try:
            _conn.execute(
                "MATCH (en:Entry) WHERE en.id=$eid AND en.user_id=$uid "
                "MATCH (et:Entity) WHERE et.id=$etid AND et.user_id=$uid"
                " CREATE (en)-[:MENTIONS]->(et)",
                {"eid":eid,"etid":sid,"uid":user_id})
        except: pass
    for rel in data.get("relations",[]):
        f = _entity_id(rel.get("from_entity",""), user_id)
        t = _entity_id(rel.get("to_entity",""), user_id)
        if f and t and entity_exists(f, user_id) and entity_exists(t, user_id):
            try:
                _conn.execute(
                    "MATCH (a:Entity) WHERE a.id=$f AND a.user_id=$uid "
                    "MATCH (b:Entity) WHERE b.id=$t AND b.user_id=$uid"
                    " CREATE (a)-[:LINKED{label:$l,entry_id:$eid}]->(b)",
                    {"f":f,"t":t,"uid":user_id,"l":rel.get("label","связан с"),"eid":eid})
            except: pass
    return eid

# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI()

# ── Auth endpoints ───────────────────────────────────────────────────────────
class AuthReq(BaseModel): login: str; password: str

@app.post("/register")
def register(req: AuthReq):
    login = req.login.strip().lower()
    if not login or len(login) < 2: raise HTTPException(400, "Логин слишком короткий")
    if not req.password or len(req.password) < 4: raise HTTPException(400, "Пароль слишком короткий")
    existing = kuzu_rows(_conn.execute("MATCH (u:User) WHERE u.login=$l RETURN u.id", {"l": login}))
    if existing: raise HTTPException(409, "Логин занят")
    uid = str(uuid.uuid4())
    _conn.execute("CREATE (:User {id:$id,login:$l,password_hash:$ph,ts:$ts})",
                  {"id": uid, "l": login, "ph": _hash_password(req.password),
                   "ts": datetime.now().strftime("%Y-%m-%d %H:%M")})
    return {"token": _create_token(uid, login), "login": login, "user_id": uid}

@app.post("/login")
def login_ep(req: AuthReq):
    login = req.login.strip().lower()
    rows = kuzu_rows(_conn.execute(
        "MATCH (u:User) WHERE u.login=$l RETURN u.id,u.password_hash", {"l": login}))
    if not rows or rows[0][1] != _hash_password(req.password):
        raise HTTPException(401, "Неверный логин или пароль")
    uid, _ = rows[0]
    return {"token": _create_token(uid, login), "login": login, "user_id": uid}

@app.get("/me")
def me(u: dict = Depends(current_user)):
    return {"user_id": u["user_id"], "login": u["login"]}

@app.get("/export")
def export_data(u: dict = Depends(current_user)):
    uid = _uid(u)
    entries = kuzu_rows(_conn.execute(
        "MATCH (e:Entry) WHERE e.user_id=$uid RETURN e.id,e.ts,e.raw_text,e.narrative,e.archivist_note",
        {"uid":uid}))
    entities = kuzu_rows(_conn.execute(
        "MATCH (e:Entity) WHERE e.user_id=$uid RETURN e.id,e.name,e.type,e.summary,e.tags",
        {"uid":uid}))
    missions = kuzu_rows(_conn.execute(
        "MATCH (m:Mission) WHERE m.user_id=$uid RETURN m.id,m.title,m.description,m.status,m.ts,m.lore",
        {"uid":uid}))
    tasks = kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.user_id=$uid RETURN t.id,t.mission_id,t.title,t.status,t.ts,"
        "t.task_type,t.reset_hours,t.required_iters,t.current_iters,"
        "t.last_reset_ts,t.streak,t.best_streak,t.entry_id",
        {"uid":uid}))
    finances = kuzu_rows(_conn.execute(
        "MATCH (f:Finance) WHERE f.user_id=$uid RETURN f.id,f.amount,f.direction,f.category,f.note,f.ts",
        {"uid":uid}))
    links = kuzu_rows(_conn.execute(
        "MATCH (a:Entity)-[r:LINKED]->(b:Entity) WHERE a.user_id=$uid AND b.user_id=$uid "
        "RETURN a.id,r.label,b.id,r.entry_id",
        {"uid":uid}))
    mentions = kuzu_rows(_conn.execute(
        "MATCH (e:Entry)-[:MENTIONS]->(n:Entity) WHERE e.user_id=$uid AND n.user_id=$uid RETURN e.id,n.id",
        {"uid":uid}))
    char = _char_data(uid)
    return {
        "version": 2,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "entries":  [{"id":r[0],"ts":r[1],"raw_text":r[2],"narrative":r[3],"archivist_note":r[4]} for r in entries],
        "entities": [{"id":r[0],"name":r[1],"type":r[2],"summary":r[3],"tags":r[4] or "[]"} for r in entities],
        "missions": [{"id":r[0],"title":r[1],"description":r[2],"status":r[3],"ts":r[4],"lore":r[5] or ""} for r in missions],
        "tasks":    [{"id":r[0],"mission_id":r[1],"title":r[2],"status":r[3],"ts":r[4],
                      "task_type":r[5] or "once","reset_hours":r[6] or 24,
                      "required_iters":r[7] or 1,"current_iters":r[8] or 0,
                      "last_reset_ts":r[9] or "","streak":r[10] or 0,"best_streak":r[11] or 0,
                      "entry_id":r[12] or ""} for r in tasks],
        "finances": [{"id":r[0],"amount":r[1],"direction":r[2],"category":r[3],"note":r[4],"ts":r[5]} for r in finances],
        "links":    [{"from":r[0],"label":r[1],"to":r[2],"entry_id":r[3] or ""} for r in links],
        "mentions": [{"entry_id":r[0],"entity_id":r[1]} for r in mentions],
        "character": char,
    }

class ImportReq(BaseModel):
    data: dict

@app.post("/import")
def import_data(req: ImportReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    d = req.data
    if d.get("version",1) < 1:
        raise HTTPException(400, "Неверный формат")
    imported = {"entries":0,"entities":0,"missions":0,"tasks":0,"finances":0,"links":0}
    # Entries
    for e in d.get("entries",[]):
        try:
            _conn.execute("MATCH (x:Entry) WHERE x.id=$id RETURN x.id",{"id":e["id"]})
            rows=kuzu_rows(_conn.execute("MATCH (x:Entry) WHERE x.id=$id RETURN x.id",{"id":e["id"]}))
            if not rows:
                _conn.execute("CREATE (:Entry {id:$id,ts:$ts,raw_text:$r,narrative:$n,archivist_note:$a,user_id:$uid})",
                    {"id":e["id"],"ts":e.get("ts",""),"r":e.get("raw_text",""),
                     "n":e.get("narrative",""),"a":e.get("archivist_note",""),"uid":uid})
                imported["entries"]+=1
        except: pass
    # Entities
    for e in d.get("entities",[]):
        try:
            if not entity_exists(e["id"]):
                _conn.execute("CREATE (:Entity {id:$id,name:$n,type:$t,summary:$s,tags:$tg,user_id:$uid})",
                    {"id":e["id"],"n":e["name"],"t":e.get("type","concept"),
                     "s":e.get("summary",""),"tg":e.get("tags","[]"),"uid":uid})
                imported["entities"]+=1
        except: pass
    # Missions
    try: _conn.execute("ALTER TABLE Mission ADD lore STRING DEFAULT ''")
    except: pass
    for m in d.get("missions",[]):
        try:
            rows=kuzu_rows(_conn.execute("MATCH (x:Mission) WHERE x.id=$id RETURN x.id",{"id":m["id"]}))
            if not rows:
                try:
                    _conn.execute("CREATE (:Mission {id:$id,title:$t,description:$desc,status:$s,ts:$ts,lore:$l,user_id:$uid})",
                        {"id":m["id"],"t":m["title"],"desc":m.get("description",""),
                         "s":m.get("status","active"),"ts":m.get("ts",""),"l":m.get("lore",""),"uid":uid})
                except:
                    _conn.execute("CREATE (:Mission {id:$id,title:$t,description:$desc,status:$s,ts:$ts,user_id:$uid})",
                        {"id":m["id"],"t":m["title"],"desc":m.get("description",""),
                         "s":m.get("status","active"),"ts":m.get("ts",""),"uid":uid})
                imported["missions"]+=1
        except: pass
    # Tasks
    for t in d.get("tasks",[]):
        try:
            rows=kuzu_rows(_conn.execute("MATCH (x:Task) WHERE x.id=$id RETURN x.id",{"id":t["id"]}))
            if not rows:
                _conn.execute(
                    "CREATE (:Task {id:$id,mission_id:$mid,title:$ti,status:$s,ts:$ts,"
                    "task_type:$tt,reset_hours:$rh,required_iters:$ri,current_iters:$ci,"
                    "last_reset_ts:$lr,streak:$st,best_streak:$bs,entry_id:$eid,user_id:$uid})",
                    {"id":t["id"],"mid":t.get("mission_id",""),"ti":t["title"],
                     "s":t.get("status","active"),"ts":t.get("ts",""),
                     "tt":t.get("task_type","once"),"rh":int(t.get("reset_hours",24)),
                     "ri":int(t.get("required_iters",1)),"ci":int(t.get("current_iters",0)),
                     "lr":t.get("last_reset_ts",""),"st":int(t.get("streak",0)),
                     "bs":int(t.get("best_streak",0)),"eid":t.get("entry_id",""),"uid":uid})
                imported["tasks"]+=1
        except: pass
    # Finances
    for f in d.get("finances",[]):
        try:
            rows=kuzu_rows(_conn.execute("MATCH (x:Finance) WHERE x.id=$id RETURN x.id",{"id":f["id"]}))
            if not rows:
                _conn.execute("CREATE (:Finance {id:$id,amount:$a,direction:$dir,category:$c,note:$n,ts:$ts,user_id:$uid})",
                    {"id":f["id"],"a":float(f.get("amount",0)),"dir":f.get("direction","out"),
                     "c":f.get("category",""),"n":f.get("note",""),"ts":f.get("ts",""),"uid":uid})
                imported["finances"]+=1
        except: pass
    # Links
    for lnk in d.get("links",[]):
        try:
            if entity_exists(lnk["from"], uid) and entity_exists(lnk["to"], uid):
                _conn.execute(
                    "MATCH (a:Entity) WHERE a.id=$f AND a.user_id=$uid "
                    "MATCH (b:Entity) WHERE b.id=$t AND b.user_id=$uid "
                    "CREATE (a)-[:LINKED{label:$l,entry_id:$e}]->(b)",
                    {"f":lnk["from"],"t":lnk["to"],"uid":uid,"l":lnk.get("label",""),"e":lnk.get("entry_id","import")})
                imported["links"]+=1
        except: pass
    # Mentions
    for mn in d.get("mentions",[]):
        try:
            erows=kuzu_rows(_conn.execute("MATCH (x:Entry) WHERE x.id=$id AND x.user_id=$uid RETURN x.id",
                                          {"id":mn["entry_id"],"uid":uid}))
            if erows and entity_exists(mn["entity_id"], uid):
                _conn.execute(
                    "MATCH (e:Entry) WHERE e.id=$eid AND e.user_id=$uid "
                    "MATCH (n:Entity) WHERE n.id=$nid AND n.user_id=$uid "
                    "CREATE (e)-[:MENTIONS]->(n)",{"eid":mn["entry_id"],"nid":mn["entity_id"],"uid":uid})
        except: pass
    # Character
    if d.get("character"):
        _save_char(d["character"], uid)
    return {"ok": True, "imported": imported}

# ─────────────────────────────────────────────────────────────────────────────
_moon_cache: dict = {"data":None,"ts":0.0}

@app.get("/moonphase")
def moonphase():
    global _moon_cache
    now = time.time()
    if _moon_cache["data"] and now-_moon_cache["ts"]<21600:
        return _moon_cache["data"]
    import ssl
    try:
        ctx = ssl.create_default_context()
        try:
            import certifi; ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
        req = _ur.Request("https://wttr.in/?format=j1",
            headers={"User-Agent":"LifeRPG/1.0","Accept":"application/json"})
        with _ur.urlopen(req,timeout=8,context=ctx) as resp:
            j=json.loads(resp.read()); a=j["weather"][0]["astronomy"][0]
            result={"phase_en":a["moon_phase"],"illumination":int(a["moon_illumination"]),
                    "moonrise":a.get("moonrise"),"moonset":a.get("moonset"),
                    "sunrise":a.get("sunrise"),"sunset":a.get("sunset"),
                    "source":"wttr.in","cached_at":datetime.now().strftime("%Y-%m-%d %H:%M")}
        _moon_cache={"data":result,"ts":now}; return result
    except Exception as ex:
        return {"phase_en":None,"illumination":None,"source":"math_fallback","error":str(ex)}

class IngestReq(BaseModel): text: str
class SaveReq(BaseModel):
    raw_text: str; narrative: str; entities: list; relations: list
    quests: list = []; archivist_note: str = ""; mission_analysis: list = []
class MissionReq(BaseModel): title: str; description: str = ""
class TaskReq(BaseModel):
    mission_id: str; title: str
    task_type: str = "once"; reset_hours: int = 24; required_iters: int = 1
class FinanceReq(BaseModel): amount: float; direction: str; category: str=""; note: str=""
class ModeReq(BaseModel): name: str; description: str = ""

INBOX_FILE = _DATA_DIR / "inbox.json"

@app.post("/ingest")
def ingest(req: IngestReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    eid = write_entry(req.text, {"narrative": req.text, "entities": [], "relations": [],
                                  "archivist_note": "", "quests": []}, uid)
    if _has_any_ai():
        # AI available — process in background
        threading.Thread(target=_process_entry_bg, args=(eid, req.text, uid), daemon=True).start()
        # Still write to inbox so JS polling works (removed on completion)
        try:
            inbox = json.loads(INBOX_FILE.read_text()) if INBOX_FILE.exists() else []
            inbox.append({"id": eid, "text": req.text, "user_id": uid, "ts": datetime.now().strftime("%Y-%m-%d %H:%M")})
            INBOX_FILE.write_text(json.dumps(inbox, ensure_ascii=False, indent=2))
        except: pass
        return {"entry_id": eid, "status": "processing", "_ai_pending": True}
    else:
        # No API key — write to inbox for manual processing
        try:
            inbox = json.loads(INBOX_FILE.read_text()) if INBOX_FILE.exists() else []
            inbox.append({"id": eid, "text": req.text, "user_id": uid, "ts": datetime.now().strftime("%Y-%m-%d %H:%M")})
            INBOX_FILE.write_text(json.dumps(inbox, ensure_ascii=False, indent=2))
        except: pass
        return {"entry_id": eid, "status": "pending", "_ai_pending": True}

@app.get("/inbox")
def get_inbox(u: dict = Depends(current_user)):
    uid = _uid(u)
    if not INBOX_FILE.exists(): return []
    try:
        inbox = json.loads(INBOX_FILE.read_text())
        return [i for i in inbox if i.get("user_id","admin") == uid]
    except: return []

@app.post("/inbox/clear")
def clear_inbox(u: dict = Depends(current_user)):
    uid = _uid(u)
    if INBOX_FILE.exists():
        try:
            inbox = json.loads(INBOX_FILE.read_text())
            inbox = [i for i in inbox if i.get("user_id","admin") != uid]
            INBOX_FILE.write_text(json.dumps(inbox, ensure_ascii=False, indent=2))
        except: INBOX_FILE.write_text("[]")
    return {"ok": True}

@app.post("/inbox/process-pending")
def process_pending(u: dict = Depends(current_user)):
    """Trigger background processing for all stuck inbox entries (no new entries created)."""
    uid = _uid(u)
    if not _has_any_ai():
        return {"ok": False, "reason": "no_ai"}
    try:
        inbox = json.loads(INBOX_FILE.read_text()) if INBOX_FILE.exists() else []
        pending = [i for i in inbox if i.get("user_id","admin") == uid and (not i.get("type") or i.get("type") == "entry")]
        for item in pending:
            eid = item.get("id","")
            text = item.get("text","") or item.get("raw","")
            if eid and text:
                threading.Thread(target=_process_entry_bg, args=(eid, text, uid), daemon=True).start()
        return {"ok": True, "started": len(pending)}
    except Exception as e:
        return {"ok": False, "reason": str(e)}

def _apply_analysis(eid: str, data: dict, user_id: str = "admin"):
    """Apply AI analysis result to DB (shared by update_entry and auto-processing)."""
    narrative = data.get("narrative","")
    an = data.get("archivist_note","")
    if narrative:
        try:
            _conn.execute(
                "MATCH (e:Entry) WHERE e.id=$id AND e.user_id=$uid SET e.narrative=$n, e.archivist_note=$an",
                {"id": eid, "uid": user_id, "n": narrative, "an": an})
        except: pass
    for ent in data.get("entities",[]):
        sid = _entity_id(ent["name"], user_id)
        tags = json.dumps(ent.get("tags",[]), ensure_ascii=False)
        if entity_exists(sid, user_id):
            _conn.execute("MATCH (e:Entity) WHERE e.id=$id AND e.user_id=$uid SET e.summary=$s, e.tags=$t",
                          {"id":sid,"uid":user_id,"s":ent["summary"],"t":tags})
        else:
            _conn.execute("CREATE (:Entity {id:$id,name:$name,type:$type,summary:$summary,tags:$tags,user_id:$uid})",
                          {"id":sid,"name":ent["name"],"type":ent.get("type","concept"),
                           "summary":ent["summary"],"tags":tags,"uid":user_id})
        try:
            _conn.execute(
                "MATCH (en:Entry) WHERE en.id=$eid AND en.user_id=$uid "
                "MATCH (et:Entity) WHERE et.id=$etid AND et.user_id=$uid"
                " CREATE (en)-[:MENTIONS]->(et)", {"eid":eid,"etid":sid,"uid":user_id})
        except: pass
    for rel in data.get("relations",[]):
        f=_entity_id(rel.get("from_entity",""), user_id); t=_entity_id(rel.get("to_entity",""), user_id)
        if f and t and entity_exists(f, user_id) and entity_exists(t, user_id):
            try:
                _conn.execute(
                    "MATCH (a:Entity) WHERE a.id=$f AND a.user_id=$uid "
                    "MATCH (b:Entity) WHERE b.id=$t AND b.user_id=$uid"
                    " CREATE (a)-[:LINKED{label:$l,entry_id:$eid}]->(b)",
                    {"f":f,"t":t,"uid":user_id,"l":rel.get("label","связан с"),"eid":eid})
            except: pass
    for q in data.get("quests",[]):
        tid=str(uuid.uuid4()); ts=datetime.now().strftime("%Y-%m-%d %H:%M")
        tt=q.get("task_type","once"); rh=int(q.get("reset_hours",24)); ri=int(q.get("required_iters",1))
        lr=ts if tt=="repeat" else ""
        try:
            _conn.execute(
                "CREATE (:Task {id:$id,mission_id:$mid,title:$t,status:'active',ts:$ts,entry_id:$eid,"
                "task_type:$tt,reset_hours:$rh,required_iters:$ri,current_iters:0,last_reset_ts:$lr,streak:0,best_streak:0,completed_ts:'',user_id:$uid})",
                {"id":tid,"mid":q.get("mission_id",""),"t":q["title"],"ts":ts,"eid":eid,
                 "tt":tt,"rh":rh,"ri":ri,"lr":lr,"uid":user_id})
        except: pass
    for ma in data.get("mission_analysis",[]):
        lore = ma.get("lore","").strip()
        if lore and ma.get("mission_id"):
            try:
                _conn.execute("MATCH (m:Mission) WHERE m.id=$id AND m.user_id=$uid SET m.lore=$l",
                              {"id":ma["mission_id"],"uid":user_id,"l":lore})
            except: pass

def _process_entry_bg(eid: str, raw: str, user_id: str = "admin"):
    """Background thread: call Claude, apply analysis, remove from inbox."""
    try:
        data = call_claude_extract(raw, user_id)
        if data:
            _apply_analysis(eid, data, user_id)
    except Exception as e:
        print(f"[process_bg] {e}")
    finally:
        # Remove from inbox regardless of success
        try:
            inbox = json.loads(INBOX_FILE.read_text()) if INBOX_FILE.exists() else []
            inbox = [i for i in inbox if not (i.get("id") == eid and i.get("user_id","admin") == user_id)]
            INBOX_FILE.write_text(json.dumps(inbox, ensure_ascii=False, indent=2))
        except: pass

@app.post("/diary/{eid}/update")
def update_entry(eid: str, req: SaveReq, u: dict = Depends(current_user)):
    """Архивариус (manual/external) обновляет запись после анализа"""
    data = {"narrative": req.narrative, "archivist_note": req.archivist_note,
            "entities": req.entities, "relations": req.relations,
            "quests": req.quests, "mission_analysis": req.mission_analysis}
    _apply_analysis(eid, data, _uid(u))
    return {"updated": eid, "quests_created": len(req.quests)}

@app.post("/save")
def save(req: SaveReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    data={"narrative":req.narrative,"entities":req.entities,"relations":req.relations,
          "quests":req.quests,"archivist_note":req.archivist_note}
    eid=write_entry(req.raw_text,data,uid)
    for q in req.quests:
        tid=str(uuid.uuid4()); ts=datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            _conn.execute(
                "CREATE (:Task {id:$id,mission_id:$mid,title:$t,status:'active',ts:$ts,entry_id:$eid,user_id:$uid})",
                {"id":tid,"mid":q.get("mission_id",""),"t":q["title"],"ts":ts,"eid":eid,"uid":uid})
        except: pass
    return {"entry_id":eid,"quests_created":len(req.quests)}

@app.get("/diary")
def diary(limit: int=60, u: dict = Depends(current_user)):
    uid = _uid(u)
    rows=kuzu_rows(_conn.execute(
        "MATCH (e:Entry) WHERE e.user_id=$uid RETURN e.id,e.ts,e.narrative,e.raw_text,e.archivist_note"
        " ORDER BY e.ts DESC LIMIT $l",{"l":limit,"uid":uid}))
    return [{"id":r[0],"ts":r[1],"narrative":r[2],"raw":r[3],"archivist_note":r[4]} for r in rows]

@app.post("/diary/{eid}/delete")
def delete_entry(eid: str, u: dict = Depends(current_user)):
    uid = _uid(u)
    try: _conn.execute("MATCH (e:Entry)-[r:MENTIONS]->() WHERE e.id=$id AND e.user_id=$uid DELETE r",{"id":eid,"uid":uid})
    except: pass
    try: _conn.execute("MATCH (e:Entry) WHERE e.id=$id AND e.user_id=$uid DELETE e",{"id":eid,"uid":uid})
    except: pass
    return {"deleted":eid}

@app.get("/entities")
def entities(type: str="", u: dict = Depends(current_user)):
    uid = _uid(u)
    if type:
        rows=kuzu_rows(_conn.execute(
            "MATCH (e:Entity) WHERE e.type=$t AND e.user_id=$uid RETURN e.id,e.name,e.type,e.summary,e.tags ORDER BY e.name",
            {"t":type,"uid":uid}))
    else:
        rows=kuzu_rows(_conn.execute(
            "MATCH (e:Entity) WHERE e.user_id=$uid RETURN e.id,e.name,e.type,e.summary,e.tags ORDER BY e.type,e.name",
            {"uid":uid}))
    result=[]
    for r in rows:
        try: tags=json.loads(r[4]) if r[4] else []
        except: tags=r[4].split(',') if r[4] else []
        result.append({"id":r[0],"name":r[1],"type":r[2],"summary":r[3],"tags":tags})
    return result

@app.get("/entity/{name}")
def entity_card(name: str, u: dict = Depends(current_user)):
    uid = _uid(u)
    eid=_entity_id(name, uid)
    base=kuzu_rows(_conn.execute(
        "MATCH (e:Entity) WHERE e.id=$id AND e.user_id=$uid RETURN e.name,e.type,e.summary,e.tags",{"id":eid,"uid":uid}))
    if not base: raise HTTPException(404,"Not found")
    out=kuzu_rows(_conn.execute(
        "MATCH (a:Entity)-[r:LINKED]->(b:Entity) WHERE a.id=$id AND a.user_id=$uid AND b.user_id=$uid RETURN a.name,r.label,b.name",
        {"id":eid,"uid":uid}))
    inp=kuzu_rows(_conn.execute(
        "MATCH (a:Entity)-[r:LINKED]->(b:Entity) WHERE b.id=$id AND a.user_id=$uid AND b.user_id=$uid RETURN a.name,r.label,b.name",
        {"id":eid,"uid":uid}))
    ment=kuzu_rows(_conn.execute(
        "MATCH (en:Entry)-[:MENTIONS]->(et:Entity) WHERE et.id=$id"
        " AND en.user_id=$uid AND et.user_id=$uid RETURN en.ts,en.narrative,en.archivist_note ORDER BY en.ts DESC LIMIT 5",
        {"id":eid,"uid":uid}))
    return {"name":base[0][0],"type":base[0][1],"summary":base[0][2],
            "tags":json.loads(base[0][3]) if base[0][3] else [],
            "links_out":[{"from":r[0],"label":r[1],"to":r[2]} for r in out],
            "links_in": [{"from":r[0],"label":r[1],"to":r[2]} for r in inp],
            "mentions":  [{"ts":r[0],"narrative":r[1],"archivist_note":r[2]} for r in ment]}

class EntityCreateReq(BaseModel):
    name: str
    type: str = "concept"
    summary: str = ""

@app.post("/entities")
def create_entity(req: EntityCreateReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    eid = _entity_id(req.name, uid)
    if entity_exists(eid, uid):
        _conn.execute("MATCH (e:Entity) WHERE e.id=$id AND e.user_id=$uid SET e.summary=$s",
                      {"id":eid,"uid":uid,"s":req.summary})
    else:
        _conn.execute(
            "CREATE (:Entity {id:$id,name:$name,type:$type,summary:$s,tags:$t,user_id:$uid})",
            {"id":eid,"name":req.name,"type":req.type,"s":req.summary,"t":"[]","uid":uid})
    return {"id":eid,"name":req.name}

class LinkEntityReq(BaseModel):
    entity_name: str
    label: str = "связан с"

@app.post("/missions/{mid}/link-entity")
def link_entity_to_mission(mid: str, req: LinkEntityReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    rows=kuzu_rows(_conn.execute("MATCH (m:Mission) WHERE m.id=$id AND m.user_id=$uid RETURN m.title",{"id":mid,"uid":uid}))
    if not rows: raise HTTPException(404)
    meid=_mission_entity_id(mid, rows[0][0], uid)
    eeid=_entity_id(req.entity_name, uid)
    if not entity_exists(eeid, uid):
        _conn.execute("CREATE (:Entity {id:$id,name:$n,type:'concept',summary:'',tags:'[]',user_id:$uid})",
                      {"id":eeid,"n":req.entity_name,"uid":uid})
    if not entity_exists(meid, uid): _sync_mission_entity(mid,rows[0][0],"","active",uid)
    try:
        _conn.execute(
            "MATCH (a:Entity) WHERE a.id=$f AND a.user_id=$uid MATCH (b:Entity) WHERE b.id=$t AND b.user_id=$uid "
            "CREATE (a)-[:LINKED{label:$l,entry_id:'manual'}]->(b)",
            {"f":meid,"t":eeid,"uid":uid,"l":req.label})
    except: pass
    return {"ok":True}

@app.get("/graph")
def graph(u: dict = Depends(current_user)):
    uid = _uid(u)
    rows=kuzu_rows(_conn.execute(
        "MATCH (a:Entity)-[r:LINKED]->(b:Entity) WHERE a.user_id=$uid AND b.user_id=$uid RETURN a.name,r.label,b.name",
        {"uid":uid}))
    return [{"from":r[0],"label":r[1],"to":r[2]} for r in rows]

def _sync_mission_entity(mid: str, title: str, description: str, status: str="active", user_id: str = "admin"):
    """Keep Mission mirrored as Entity node of type 'mission'."""
    eid = _mission_entity_id(mid, title, user_id)
    summary = description.strip() if description else title
    if entity_exists(eid, user_id):
        _conn.execute("MATCH (e:Entity) WHERE e.id=$id AND e.user_id=$uid SET e.name=$name,e.summary=$s",
                      {"id":eid,"uid":user_id,"name":title,"s":summary})
    else:
        try:
            _conn.execute(
                "CREATE (:Entity {id:$id,name:$name,type:'mission',summary:$s,tags:$t,user_id:$uid})",
                {"id":eid,"name":title,"s":summary,"t":json.dumps(["mission",status],ensure_ascii=False),"uid":user_id})
        except: pass

@app.get("/missions")
def get_missions(u: dict = Depends(current_user)):
    uid = _uid(u)
    rows=kuzu_rows(_conn.execute(
        "MATCH (m:Mission) WHERE m.user_id=$uid RETURN m.id,m.title,m.description,m.status,m.ts,m.lore ORDER BY m.ts",
        {"uid":uid}))
    result=[]
    for r in rows:
        mid=r[0]
        tasks=kuzu_rows(_conn.execute(
            "MATCH (t:Task) WHERE t.mission_id=$mid AND t.user_id=$uid "
            "RETURN t.id,t.title,t.status,t.ts,"
            "t.task_type,t.reset_hours,t.required_iters,t.current_iters,"
            "t.last_reset_ts,t.streak,t.best_streak ORDER BY t.ts",
            {"mid":mid,"uid":uid}))
        task_list=[]
        for t in tasks:
            td={"id":t[0],"title":t[1],"status":t[2],"ts":t[3],
                "task_type":t[4] or "once","reset_hours":int(t[5] or 24),
                "required_iters":int(t[6] or 1),"current_iters":int(t[7] or 0),
                "last_reset_ts":t[8] or "","streak":int(t[9] or 0),"best_streak":int(t[10] or 0)}
            td=_maybe_reset_task(td)
            task_list.append(td)
        # Linked entities
        eid=_mission_entity_id(mid, r[1], uid)
        linked=kuzu_rows(_conn.execute(
            "MATCH (m:Entity)-[r:LINKED]->(e:Entity) WHERE m.id=$id AND m.user_id=$uid AND e.user_id=$uid RETURN e.id,e.name,e.type,e.summary",
            {"id":eid,"uid":uid}))
        linked+= kuzu_rows(_conn.execute(
            "MATCH (e:Entity)-[r:LINKED]->(m:Entity) WHERE m.id=$id AND m.user_id=$uid AND e.user_id=$uid RETURN e.id,e.name,e.type,e.summary",
            {"id":eid,"uid":uid}))
        seen_ids=set()
        entity_tags=[]
        for x in linked:
            if x[0] not in seen_ids:
                seen_ids.add(x[0])
                entity_tags.append({"id":x[0],"name":x[1],"type":x[2],"summary":x[3] or ""})
        result.append({"id":mid,"title":r[1],"description":r[2],"status":r[3],"ts":r[4],
                        "lore":r[5] or "","tasks":task_list,"entities":entity_tags})
    return result

class MissionDescReq(BaseModel):
    description: str

@app.patch("/missions/{mid}/description")
def update_mission_description(mid: str, req: MissionDescReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    rows=kuzu_rows(_conn.execute(
        "MATCH (m:Mission) WHERE m.id=$id AND m.user_id=$uid RETURN m.title,m.status",{"id":mid,"uid":uid}))
    if not rows: raise HTTPException(404)
    title,status=rows[0]
    _conn.execute("MATCH (m:Mission) WHERE m.id=$id AND m.user_id=$uid SET m.description=$d",
                  {"id":mid,"uid":uid,"d":req.description})
    _sync_mission_entity(mid,title,req.description,status,uid)
    return {"ok":True}

@app.post("/missions")
def add_mission(req: MissionReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    mid=str(uuid.uuid4())
    ts=datetime.now().strftime("%Y-%m-%d %H:%M")
    _conn.execute("CREATE (:Mission {id:$id,title:$t,description:$d,status:'active',ts:$ts,lore:'',user_id:$uid})",
                  {"id":mid,"t":req.title,"d":req.description,"ts":ts,"uid":uid})
    _sync_mission_entity(mid,req.title,req.description,"active",uid)
    return {"id":mid,"title":req.title,"status":"active","tasks":[],"entities":[]}

@app.post("/missions/{mid}/complete")
def complete_mission(mid: str, u: dict = Depends(current_user)):
    uid = _uid(u)
    _conn.execute("MATCH (m:Mission) WHERE m.id=$id AND m.user_id=$uid SET m.status='done'",{"id":mid,"uid":uid})
    if _has_any_ai():
        threading.Thread(target=_gen_epilogue_bg,args=(mid,uid),daemon=True).start()
    return {"ok":True}

@app.post("/missions/{mid}/delete")
def delete_mission(mid: str, u: dict = Depends(current_user)):
    uid = _uid(u)
    try: _conn.execute("MATCH (t:Task) WHERE t.mission_id=$id AND t.user_id=$uid DELETE t",{"id":mid,"uid":uid})
    except: pass
    try: _conn.execute("MATCH (m:Mission) WHERE m.id=$id AND m.user_id=$uid DELETE m",{"id":mid,"uid":uid})
    except: pass
    return {"deleted":mid}

@app.post("/tasks")
def add_task(req: TaskReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    exists=kuzu_rows(_conn.execute("MATCH (m:Mission) WHERE m.id=$id AND m.user_id=$uid RETURN m.id",
                                   {"id":req.mission_id,"uid":uid}))
    if not exists: raise HTTPException(404)
    tid=str(uuid.uuid4()); ts=datetime.now().strftime("%Y-%m-%d %H:%M")
    init_reset = ts if req.task_type == "repeat" else ""
    _conn.execute(
        "CREATE (:Task {id:$id,mission_id:$mid,title:$t,status:'active',ts:$ts,entry_id:'',"
        "task_type:$tt,reset_hours:$rh,required_iters:$ri,"
        "current_iters:0,last_reset_ts:$lr,streak:0,best_streak:0,user_id:$uid})",
        {"id":tid,"mid":req.mission_id,"t":req.title,"ts":ts,
         "tt":req.task_type,"rh":req.reset_hours,"ri":req.required_iters,"lr":init_reset,"uid":uid})
    return {"id":tid,"title":req.title,"status":"active","task_type":req.task_type}

class TaskParamsReq(BaseModel):
    task_type: str = "once"; reset_hours: int = 24; required_iters: int = 1

@app.post("/tasks/{tid}/set-params")
def set_task_params(tid: str, req: TaskParamsReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    _conn.execute(
        "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET t.task_type=$tt,t.reset_hours=$rh,t.required_iters=$ri",
        {"id":tid,"uid":uid,"tt":req.task_type,"rh":req.reset_hours,"ri":req.required_iters})
    return {"ok":True}

@app.post("/tasks/{tid}/tick")
def tick_task(tid: str, u: dict = Depends(current_user)):
    uid = _uid(u)
    rows=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid "
        "RETURN t.current_iters,t.required_iters,t.last_reset_ts",{"id":tid,"uid":uid}))
    if not rows: return {"error":"not found"}
    cur=int(rows[0][0] or 0); req=int(rows[0][1] or 1); lr=rows[0][2] or ""
    new_cur=min(cur+1,req)
    now_s=datetime.now().strftime("%Y-%m-%d %H:%M")
    if not lr: lr=now_s
    cts=now_s if new_cur>=req else ""
    _conn.execute("MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET t.current_iters=$c,t.last_reset_ts=$lr,t.completed_ts=$cts",
                  {"id":tid,"uid":uid,"c":new_cur,"lr":lr,"cts":cts})
    return {"current":new_cur,"required":req,"completed":new_cur>=req}

@app.post("/tasks/{tid}/complete")
def complete_task(tid: str, u: dict = Depends(current_user)):
    uid = _uid(u)
    now_s=datetime.now().strftime("%Y-%m-%d %H:%M")
    _conn.execute("MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET t.status='done',t.completed_ts=$ts",
                  {"id":tid,"uid":uid,"ts":now_s})
    return {"ok":True}

@app.post("/tasks/{tid}/delete")
def delete_task(tid: str, u: dict = Depends(current_user)):
    uid = _uid(u)
    try: _conn.execute("MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid DELETE t",{"id":tid,"uid":uid})
    except: pass
    return {"deleted":tid}

@app.get("/tasks/completed-today")
def completed_today(u: dict = Depends(current_user)):
    uid = _uid(u)
    today=datetime.now().strftime("%Y-%m-%d")
    # Completed once-tasks
    r1=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.user_id=$uid AND t.completed_ts STARTS WITH $d RETURN count(t)",
        {"d":today,"uid":uid}))
    done=int(r1[0][0]) if r1 else 0
    # Repeat tasks ticked today (any progress, even partial) — exclude already counted
    r2=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.task_type='repeat' AND t.last_reset_ts STARTS WITH $d "
        "AND t.user_id=$uid AND t.current_iters > 0 AND (t.completed_ts IS NULL OR NOT t.completed_ts STARTS WITH $d) "
        "RETURN count(t)",{"d":today,"uid":uid}))
    partial=int(r2[0][0]) if r2 else 0
    return {"count": done+partial}

@app.post("/entities/{eid}/delete")
def delete_entity(eid: str, u: dict = Depends(current_user)):
    uid = _uid(u)
    es=_entity_id(eid, uid)
    try: _conn.execute("MATCH (e:Entity)-[r:LINKED]-() WHERE e.id=$id AND e.user_id=$uid DELETE r",{"id":es,"uid":uid})
    except: pass
    try: _conn.execute("MATCH ()-[r:LINKED]->(e:Entity) WHERE e.id=$id AND e.user_id=$uid DELETE r",{"id":es,"uid":uid})
    except: pass
    try: _conn.execute("MATCH ()-[r:MENTIONS]->(e:Entity) WHERE e.id=$id AND e.user_id=$uid DELETE r",{"id":es,"uid":uid})
    except: pass
    try: _conn.execute("MATCH (e:Entity) WHERE e.id=$id AND e.user_id=$uid DELETE e",{"id":es,"uid":uid})
    except: pass
    return {"deleted":es}

class MergeEntityReq(BaseModel):
    keep_name: str
    drop_name: str

@app.post("/entities/merge")
def merge_entities(req: MergeEntityReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    kid=_entity_id(req.keep_name, uid); did=_entity_id(req.drop_name, uid)
    if not entity_exists(kid, uid): raise HTTPException(404, f"keep not found: {req.keep_name}")
    if not entity_exists(did, uid): raise HTTPException(404, f"drop not found: {req.drop_name}")
    if kid==did: raise HTTPException(400, "same entity")
    _merge_entity(kid, did, uid)
    return {"ok":True,"kept":req.keep_name,"removed":req.drop_name}

class MissionUpdateReq(BaseModel): title: str = ""; description: str = ""
class TaskUpdateReq(BaseModel): title: str = ""

@app.post("/missions/{mid}/update")
def update_mission(mid: str, req: MissionUpdateReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    if req.title:
        _conn.execute("MATCH (m:Mission) WHERE m.id=$id AND m.user_id=$uid SET m.title=$t, m.description=$d",
                      {"id":mid,"uid":uid,"t":req.title,"d":req.description})
        _sync_mission_entity(mid, req.title, req.description, "active", uid)
    return {"ok":True}

class MissionLoreReq(BaseModel): lore: str

@app.post("/missions/{mid}/lore")
def set_mission_lore(mid: str, req: MissionLoreReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    _conn.execute("MATCH (m:Mission) WHERE m.id=$id AND m.user_id=$uid SET m.lore=$l",{"id":mid,"uid":uid,"l":req.lore})
    return {"ok":True}

@app.post("/tasks/{tid}/update")
def update_task(tid: str, req: TaskUpdateReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    if req.title:
        _conn.execute("MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET t.title=$t",{"id":tid,"uid":uid,"t":req.title})
    return {"ok":True}

_REANALYZE_PROMPT = """Ты — Архивариус. Обнови лор каждого активного Пути Героя в стиле летописи Морровинда.
Лор — 1-2 лаконичных нарративных предложения: что этот Путь значит для Героя, конкретно.

АКТИВНЫЕ ПУТИ:
{missions}

ПОСЛЕДНИЕ ЗАПИСИ:
{entries}

Верни ТОЛЬКО валидный JSON без markdown:
{{"missions":[{{"id":"...","lore":"..."}}]}}"""

def _has_any_ai() -> bool:
    return bool(_get_api_key() or _get_gigachat_key())

def _call_any_ai(prompt_text: str) -> str:
    """Try Anthropic then GigaChat, return raw text."""
    ant_key = _get_api_key()
    if ant_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ant_key)
            msg = client.messages.create(model="claude-haiku-4-5", max_tokens=1024,
                messages=[{"role":"user","content":prompt_text}])
            return msg.content[0].text if msg.content else ""
        except Exception as e: print(f"[anthropic_any] {e}")
    return _call_gigachat(prompt_text)

def _merge_entity(keep_id: str, drop_id: str, user_id: str = "admin"):
    """Redirect all LINKED rels from drop_id to keep_id, then delete drop node."""
    try:
        rels_out=kuzu_rows(_conn.execute(
            "MATCH (a:Entity)-[r:LINKED]->(b:Entity) WHERE a.id=$id AND a.user_id=$uid AND b.user_id=$uid RETURN b.id,r.label,r.entry_id",
            {"id":drop_id,"uid":user_id}))
        rels_in=kuzu_rows(_conn.execute(
            "MATCH (a:Entity)-[r:LINKED]->(b:Entity) WHERE b.id=$id AND a.user_id=$uid AND b.user_id=$uid RETURN a.id,r.label,r.entry_id",
            {"id":drop_id,"uid":user_id}))
        for r in rels_out:
            if r[0]!=keep_id:
                try: _conn.execute(
                    "MATCH (a:Entity) WHERE a.id=$f AND a.user_id=$uid MATCH (b:Entity) WHERE b.id=$t AND b.user_id=$uid "
                    "CREATE (a)-[:LINKED{label:$l,entry_id:$e}]->(b)",
                    {"f":keep_id,"t":r[0],"uid":user_id,"l":r[1],"e":r[2] or "merge"})
                except: pass
        for r in rels_in:
            if r[0]!=keep_id:
                try: _conn.execute(
                    "MATCH (a:Entity) WHERE a.id=$f AND a.user_id=$uid MATCH (b:Entity) WHERE b.id=$t AND b.user_id=$uid "
                    "CREATE (a)-[:LINKED{label:$l,entry_id:$e}]->(b)",
                    {"f":r[0],"t":keep_id,"uid":user_id,"l":r[1],"e":r[2] or "merge"})
                except: pass
        _conn.execute("MATCH (e:Entity) WHERE e.id=$id AND e.user_id=$uid DETACH DELETE e",{"id":drop_id,"uid":user_id})
    except Exception as ex: print(f"[merge_entity] {ex}")

def _run_reanalyze_bg(user_id: str = "admin"):
    try:
        # 0. AI: deduplicate + retype entities
        if _has_any_ai():
            all_ents=kuzu_rows(_conn.execute(
                "MATCH (e:Entity) WHERE e.user_id=$uid RETURN e.id,e.name,e.type,e.summary LIMIT 80",
                {"uid":user_id}))
            if len(all_ents)>1:
                ent_list="\n".join(f"NAME={r[1]} TYPE={r[2]} DESC={r[3] or ''}" for r in all_ents)
                dedup_p=f"""Ты — Архивариус. Проверь список сущностей и сделай два дела:

1. Найди семантически одинаковые сущности (Самокат / Электросамокат, TikTok / Тик-ток, Игорь / Игорь Заказчик). Выбери лучшее имя для оставшейся.
2. Исправь неверные типы. Типы: person (люди), place (места), project (проекты/блоги/каналы), concept (идеи/практики), object (физические предметы), event (события).

Сущности:
{ent_list}

Верни ТОЛЬКО JSON:
{{
  "merges": [{{"keep_name": "имя_оставить", "drop_name": "имя_удалить"}}],
  "retypes": [{{"name": "имя_сущности", "new_type": "правильный_тип"}}]
}}
Если нечего делать — пустые списки."""
                t0=_call_any_ai(dedup_p)
                m0=re.search(r'\{.*\}',t0,re.DOTALL)
                if m0:
                    try:
                        parsed=json.loads(m0.group())
                        for mg in parsed.get("merges",[]):
                            kname=mg.get("keep_name",""); dname=mg.get("drop_name","")
                            kid=_entity_id(kname, user_id); did=_entity_id(dname, user_id)
                            if kname and dname and kid!=did and entity_exists(kid, user_id) and entity_exists(did, user_id):
                                print(f"[dedup] merging '{dname}' → '{kname}'")
                                _merge_entity(kid,did,user_id)
                            elif kname and dname and not entity_exists(kid, user_id) and entity_exists(did, user_id):
                                # keep_name doesn't exist yet — create it, merge drop into it
                                row=kuzu_rows(_conn.execute(
                                    "MATCH (e:Entity) WHERE e.id=$id AND e.user_id=$uid RETURN e.type,e.summary,e.tags",
                                    {"id":did,"uid":user_id}))
                                t2,s2,tg2=row[0] if row else ("concept","","[]")
                                try: _conn.execute(
                                    "CREATE (:Entity {id:$id,name:$n,type:$t,summary:$s,tags:$tg,user_id:$uid})",
                                    {"id":kid,"n":kname,"t":t2,"s":s2,"tg":tg2 or "[]","uid":user_id})
                                except: pass
                                _merge_entity(kid,did,user_id)
                                print(f"[dedup] renamed '{dname}' → '{kname}'")
                        for rt in parsed.get("retypes",[]):
                            eid=_entity_id(rt.get("name",""), user_id)
                            ntype=rt.get("new_type","")
                            valid={"person","place","project","concept","object","event","mission"}
                            if eid and ntype in valid and entity_exists(eid, user_id):
                                _conn.execute("MATCH (e:Entity) WHERE e.id=$id AND e.user_id=$uid SET e.type=$t",
                                              {"id":eid,"uid":user_id,"t":ntype})
                                print(f"[retype] '{rt['name']}' → {ntype}")
                    except Exception as ex: print(f"[dedup] {ex}")

        # 1. Sync ALL missions to Entity nodes
        all_missions=kuzu_rows(_conn.execute(
            "MATCH (m:Mission) WHERE m.user_id=$uid RETURN m.id,m.title,m.description,m.status",
            {"uid":user_id}))
        for r in all_missions:
            try: _sync_mission_entity(r[0],r[1],r[2] or "",r[3] or "active",user_id)
            except: pass

        if not _has_any_ai(): return

        # 2. AI: update lore + find entity links for each mission
        entries=kuzu_rows(_conn.execute(
            "MATCH (e:Entry) WHERE e.user_id=$uid RETURN e.ts,e.narrative ORDER BY e.ts DESC LIMIT 15",
            {"uid":user_id}))
        entities=kuzu_rows(_conn.execute(
            "MATCH (e:Entity) WHERE e.type<>'mission' AND e.user_id=$uid RETURN e.name,e.type LIMIT 40",
            {"uid":user_id}))
        miss_txt="\n".join(f"ID={r[0]} TITLE={r[1]} DESC={r[2] or ''}" for r in all_missions)
        ent_txt="\n".join(f"- {r[0]} ({r[1]})" for r in entities)
        entry_txt="\n".join(f"[{r[0]}] {r[1]}" for r in entries)

        p=_REANALYZE_PROMPT.format(missions=miss_txt or "нет", entries=entry_txt or "нет")
        text=_call_any_ai(p)
        m=re.search(r'\{.*\}',text,re.DOTALL)
        if m:
            data=json.loads(m.group())
            for item in data.get("missions",[]):
                lore=item.get("lore","").strip()
                if lore and item.get("id"):
                    try: _conn.execute("MATCH (m:Mission) WHERE m.id=$id AND m.user_id=$uid SET m.lore=$l",
                                       {"id":item["id"],"uid":user_id,"l":lore})
                    except: pass

        # 3. AI: find connections between entities and missions
        if entities and all_missions:
            p2=f"""Ты — Архивариус. Найди связи между сущностями и Путями Героя.

Пути:
{miss_txt}

Известные сущности:
{ent_txt}

Записи:
{entry_txt}

Верни ТОЛЬКО JSON: {{"links": [{{"mission_title":"название пути","entity_name":"имя сущности","label":"тип связи (1-2 слова)"}}]}}
Только явные связи из записей. Максимум 15."""
            t2=_call_any_ai(p2)
            m2=re.search(r'\{.*\}',t2,re.DOTALL)
            if m2:
                links=json.loads(m2.group()).get("links",[])
                for lnk in links:
                    mtitle=lnk.get("mission_title",""); ename=lnk.get("entity_name","")
                    label=lnk.get("label","связан с")
                    if not mtitle or not ename: continue
                    mid_row = next((r for r in all_missions if r[1] == mtitle), None)
                    meid=_mission_entity_id(mid_row[0], mtitle, user_id) if mid_row else _entity_id("mission_"+mtitle, user_id)
                    eeid=_entity_id(ename, user_id)
                    if entity_exists(meid, user_id) and entity_exists(eeid, user_id):
                        try:
                            _conn.execute(
                                "MATCH (a:Entity) WHERE a.id=$f AND a.user_id=$uid MATCH (b:Entity) WHERE b.id=$t AND b.user_id=$uid "
                                "CREATE (a)-[:LINKED{label:$l,entry_id:'reanalyze'}]->(b)",
                                {"f":meid,"t":eeid,"uid":user_id,"l":label})
                        except: pass
    except Exception as e: print(f"[reanalyze_bg] {e}")
    finally:
        try:
            inbox=json.loads(INBOX_FILE.read_text()) if INBOX_FILE.exists() else []
            inbox=[i for i in inbox if not (i.get("type")=="reanalyze" and i.get("user_id","admin")==user_id)]
            INBOX_FILE.write_text(json.dumps(inbox,ensure_ascii=False,indent=2))
        except: pass

@app.post("/reanalyze")
def do_reanalyze(u: dict = Depends(current_user)):
    uid = _uid(u)
    if _has_any_ai():
        try:
            inbox=json.loads(INBOX_FILE.read_text()) if INBOX_FILE.exists() else []
            inbox=[i for i in inbox if not (i.get("type")=="reanalyze" and i.get("user_id","admin")==uid)]
            rid="reanalyze_"+datetime.now().strftime("%Y%m%d_%H%M%S")
            inbox.append({"id":rid,"type":"reanalyze","user_id":uid,"ts":datetime.now().strftime("%Y-%m-%d %H:%M")})
            INBOX_FILE.write_text(json.dumps(inbox,ensure_ascii=False,indent=2))
        except: pass
        threading.Thread(target=_run_reanalyze_bg,args=(uid,),daemon=True).start()
        return {"ok":True,"status":"processing"}
    else:
        return {"ok":False,"status":"no_api_key"}

class GigaChatKeyReq(BaseModel): gigachat_key: str; gigachat_scope: str = "GIGACHAT_API_PERS"

@app.post("/config/gigachat")
def save_gigachat(req: GigaChatKeyReq, u: dict = Depends(current_user)):
    cfg=_app_cfg()
    cfg["gigachat_key"]=req.gigachat_key.strip()
    cfg["gigachat_scope"]=req.gigachat_scope.strip()
    APP_CFG_FILE.write_text(json.dumps(cfg,ensure_ascii=False))
    return {"ok":True}

@app.get("/reanalyze/status")
def reanalyze_status(u: dict = Depends(current_user)):
    uid = _uid(u)
    try:
        inbox=json.loads(INBOX_FILE.read_text()) if INBOX_FILE.exists() else []
        running=any(i.get("type")=="reanalyze" and i.get("user_id","admin")==uid for i in inbox)
    except: running=False
    return {"running": running, "has_key": bool(_get_api_key())}

class ApiKeyReq(BaseModel): api_key: str

@app.post("/config/api-key")
def save_api_key(req: ApiKeyReq, u: dict = Depends(current_user)):
    cfg=_app_cfg(); cfg["api_key"]=req.api_key.strip()
    APP_CFG_FILE.write_text(json.dumps(cfg,ensure_ascii=False))
    return {"ok":True,"has_key":bool(cfg["api_key"])}

@app.get("/today-narrative")
def today_narrative(u: dict = Depends(current_user)):
    uid = _uid(u)
    today=datetime.now().strftime("%Y-%m-%d")
    rows=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.user_id=$uid AND t.task_type='repeat' AND t.last_reset_ts STARTS WITH $d AND t.current_iters > 0 "
        "RETURN t.title, t.current_iters, t.required_iters",{"d":today,"uid":uid}))
    rows2=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.user_id=$uid AND t.completed_ts STARTS WITH $d RETURN t.title, t.required_iters",
        {"d":today,"uid":uid}))
    all_tasks=rows+[[r[0],r[1],r[1]] for r in rows2]
    if not all_tasks: return {"narrative":""}
    task_lines="\n".join(f"- {r[0]} ({r[1]}/{r[2]})" for r in all_tasks)
    p=f"""Ты — Архивариус. Одним абзацем (2-3 предложения) в стиле летописи Морровинда опиши достижения Героя за сегодня.
Лаконично, эпично, от третьего лица. Без лишних слов.

Задания сегодня:
{task_lines}

Верни только текст летописи, без кавычек и заголовков."""
    text=_call_any_ai(p) if _has_any_ai() else ""
    return {"narrative":text.strip()}

class OracleReq(BaseModel):
    mechanic_type: str  # "moon"|"season"|"patron"|"epoch"|"moon_name"
    mechanic_value: str
    mechanic_effect: str = ""

@app.post("/oracle")
def oracle(req: OracleReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    if not _has_any_ai():
        return {"text":""}
    # Last 5 diary entries
    entries=kuzu_rows(_conn.execute(
        "MATCH (e:Entry) WHERE e.user_id=$uid RETURN e.narrative, e.raw_text, e.ts ORDER BY e.ts DESC LIMIT 5",
        {"uid":uid}))
    entry_lines="\n".join(
        f"[{r[2]}] {r[0] or r[1]}" for r in entries if r[0] or r[1]) or "нет записей"
    # Active missions
    missions=kuzu_rows(_conn.execute(
        "MATCH (m:Mission) WHERE m.status='active' AND m.user_id=$uid RETURN m.title, m.description LIMIT 6",
        {"uid":uid}))
    mission_lines="\n".join(f"- {r[0]}: {r[1] or ''}" for r in missions) or "нет активных путей"

    type_labels={"moon":"Фаза луны","season":"Сезон","patron":"Покровитель",
                 "epoch":"Эпоха","moon_name":"Луна сезона","stat":"Стат Героя"}
    label=type_labels.get(req.mechanic_type, req.mechanic_type)
    effect_line=f"\nЛорное значение: {req.mechanic_effect}" if req.mechanic_effect else ""

    if req.mechanic_type=="stat":
        stat_meta=next((s for s in HERO_STATS if s["id"]==req.mechanic_value),None)
        stat_name=stat_meta["name"] if stat_meta else req.mechanic_value
        stat_ai=stat_meta["ai"] if stat_meta else ""
        char=_char_data(uid); score=char.get("stats",{}).get(req.mechanic_value,None)
        score_line=f"\nТекущий показатель Героя: {score}/100" if score is not None else ""
        p=f"""Ты — Архивариус. Говори как наставник — кратко, честно, от второго лица. Без вступлений.

Стат: {stat_name}
Суть: {stat_ai}{score_line}

Нарратив Героя:
{entry_lines}

Пути:
{mission_lines}

Дай откровение (3-4 предложения): что именно в этой области сдерживает или усиливает этого Героя прямо сейчас. Будь конкретен — ссылайся на его реальные дела. Скажи, что он мог бы сделать иначе."""
    else:
        p=f"""Ты — Архивариус. Говори как пророк — кратко, образно, от второго лица. Без вступлений и заголовков.

Сейчас {label}: {req.mechanic_value}.{effect_line}

Нарратив Героя (последние записи):
{entry_lines}

Активные Пути Героя:
{mission_lines}

Дай откровение (3-4 предложения): что означает {req.mechanic_value} для этого конкретного Героя прямо сейчас. Связь с его реальными делами и путями обязательна."""
    text=_call_any_ai(p)
    return {"text":text.strip()}

@app.get("/config/status")
def config_status():
    return {"has_key": bool(_get_api_key()), "has_gigachat": bool(_get_gigachat_key()),
            "active": "anthropic" if _get_api_key() else ("gigachat" if _get_gigachat_key() else "none")}

CHAR_FILE = _DATA_DIR / "character.json"

HERO_STATS = [
    {"id":"will",    "name":"Воля",      "sub":"Ты движешь судьбу — или она тебя?",
     "ai":"насколько Герой действует из своей воли, а не под давлением внешних обстоятельств"},
    {"id":"temper",  "name":"Закалка",   "sub":"Металл, из которого куют легенды",
     "ai":"способность восстанавливаться после трудностей и продолжать путь"},
    {"id":"flame",   "name":"Пламя",     "sub":"Зачем ты идёшь — ты знаешь?",
     "ai":"ощущение смысла и цели за действиями, внутренний огонь"},
    {"id":"mastery", "name":"Мастерство","sub":"Острота, что точится через действие",
     "ai":"конкретный рост, навыки, достижения, выполненные задания"},
    {"id":"threads", "name":"Нити",      "sub":"Связи, которые держат мир",
     "ai":"качество и глубина связей с другими людьми"},
    {"id":"shadow",  "name":"Тень",      "sub":"То, что идёт рядом и просит имени",
     "ai":"осознание своих теней, принятие сложных частей себя, самоосознанность"},
]

def _char_data(user_id: str = "admin"):
    f = _user_json_file("character.json", user_id)
    if f.exists():
        try: return json.loads(f.read_text())
        except: pass
    return {"stats":{},"antagonist_name":"","antagonist_desc":"",
            "last_analyzed":"","mission_epilogues":{}}

def _save_char(data, user_id: str = "admin"):
    _user_json_file("character.json", user_id).write_text(json.dumps(data, ensure_ascii=False, indent=2))

@app.get("/character/data")
def get_character_data(u: dict = Depends(current_user)):
    return _char_data(_uid(u))

@app.get("/character/stats-schema")
def stats_schema():
    return [{"id":s["id"],"name":s["name"],"sub":s["sub"]} for s in HERO_STATS]

@app.post("/character/analyze")
def analyze_character(u: dict = Depends(current_user)):
    uid = _uid(u)
    if not _has_any_ai(): return {"ok":False,"reason":"no_ai"}
    entries=kuzu_rows(_conn.execute(
        "MATCH (e:Entry) WHERE e.user_id=$uid RETURN e.narrative, e.ts ORDER BY e.ts DESC LIMIT 20",
        {"uid":uid}))
    if len(entries)<3: return {"ok":False,"reason":"not_enough_entries"}
    entry_lines="\n".join(f"[{r[1]}] {r[0]}" for r in entries if r[0])
    missions=kuzu_rows(_conn.execute(
        "MATCH (m:Mission) WHERE m.user_id=$uid RETURN m.title, m.status LIMIT 10",
        {"uid":uid}))
    mission_lines="\n".join(f"- {r[0]} ({r[1]})" for r in missions) or "нет"
    stat_desc="\n".join(f"- {s['id']}: {s['ai']}" for s in HERO_STATS)
    def _bg():
        # Stats scoring
        try:
            t1=_call_any_ai(f"""Ты — Архивариус. Оцени Героя по 6 измерениям от 0 до 100. Будь строг и честен: 50 = средний человек, 80+ = реально высокий уровень.

Измерения:
{stat_desc}

Записи Героя:
{entry_lines}

Пути:
{mission_lines}

Верни ТОЛЬКО JSON без пояснений: {{"will":65,"temper":42,"flame":78,"mastery":55,"threads":60,"shadow":35}}""")
            m1=re.search(r'\{[^{}]+\}',t1)
            stats=json.loads(m1.group()) if m1 else {}
        except: stats={}
        # Antagonist
        try:
            t2=_call_any_ai(f"""Ты — Архивариус. Найди главное повторяющееся препятствие Героя. Назови его мифическим именем (2-3 слова) и опиши в 1-2 предложениях.
Записи:\n{entry_lines}\nПути:\n{mission_lines}
Верни ТОЛЬКО JSON: {{"name":"Имя антагониста","desc":"описание"}}""")
            m2=re.search(r'\{.*\}',t2,re.DOTALL)
            antag=json.loads(m2.group()) if m2 else {"name":"","desc":""}
        except: antag={"name":"","desc":""}
        data=_char_data(uid)
        data["stats"]=stats
        data["antagonist_name"]=antag.get("name","")
        data["antagonist_desc"]=antag.get("desc","")
        data["last_analyzed"]=datetime.now().strftime("%Y-%m-%d")
        _save_char(data, uid)
    threading.Thread(target=_bg,daemon=True).start()
    return {"ok":True,"status":"analyzing"}

@app.get("/chronicle/past-moon")
def past_moon_entry(u: dict = Depends(current_user)):
    uid = _uid(u)
    from datetime import timedelta
    today=datetime.now()
    ws=(today-timedelta(days=35)).strftime("%Y-%m-%d %H:%M")
    we=(today-timedelta(days=25)).strftime("%Y-%m-%d %H:%M")
    rows=kuzu_rows(_conn.execute(
        "MATCH (e:Entry) WHERE e.user_id=$uid AND e.ts >= $s AND e.ts <= $e "
        "RETURN e.narrative, e.ts ORDER BY e.ts DESC LIMIT 1",{"s":ws,"e":we,"uid":uid}))
    if not rows: return {"entry":None}
    return {"entry":{"narrative":rows[0][0],"ts":rows[0][1]}}

def _gen_epilogue_bg(mid: str, user_id: str = "admin"):
    rows=kuzu_rows(_conn.execute(
        "MATCH (m:Mission) WHERE m.id=$id AND m.user_id=$uid RETURN m.title, m.description",
        {"id":mid,"uid":user_id}))
    if not rows: return
    title,desc=rows[0][0],rows[0][1] or ""
    tasks=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.mission_id=$id AND t.user_id=$uid RETURN t.title",
        {"id":mid,"uid":user_id}))
    task_lines="\n".join(f"- {r[0]}" for r in tasks) or "задания не записаны"
    p=f"""Ты — Архивариус. Путь завершён. Напиши эпическую эпитафию этому отрезку жизни Героя.
2-3 предложения. Торжественная летопись. Дата завершения: {datetime.now().strftime("%Y-%m-%d")}.
Путь: {title}\nОписание: {desc}\nЗадания:\n{task_lines}
Верни только текст эпитафии, без кавычек."""
    text=_call_any_ai(p)
    data=_char_data(user_id)
    data.setdefault("mission_epilogues",{})[mid]=text.strip()
    _save_char(data, user_id)

@app.get("/finances")
def get_finances(u: dict = Depends(current_user)):
    uid = _uid(u)
    rows=kuzu_rows(_conn.execute(
        "MATCH (f:Finance) WHERE f.user_id=$uid RETURN f.id,f.amount,f.direction,f.category,f.note,f.ts"
        " ORDER BY f.ts DESC LIMIT 60",{"uid":uid}))
    items=[{"id":r[0],"amount":r[1],"direction":r[2],"category":r[3],"note":r[4],"ts":r[5]} for r in rows]
    inc=sum(i["amount"] for i in items if i["direction"]=="доход")
    exp=sum(i["amount"] for i in items if i["direction"]=="расход")
    return {"balance":inc-exp,"income":inc,"expense":exp,"items":items}

@app.post("/finances")
def add_finance(req: FinanceReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    fid=str(uuid.uuid4())
    _conn.execute(
        "CREATE (:Finance {id:$id,amount:$a,direction:$d,category:$c,note:$n,ts:$ts,user_id:$uid})",
        {"id":fid,"a":req.amount,"d":req.direction,"c":req.category,
         "n":req.note,"ts":datetime.now().strftime("%Y-%m-%d %H:%M"),"uid":uid})
    return {"id":fid}

# ── Pocket ───────────────────────────────────────────────────────────────────
POCKET_CFG = _DATA_DIR / "pocket_config.json"

def _pocket_cfg(user_id: str = "admin"):
    f = _user_json_file("pocket_config.json", user_id)
    if f.exists():
        try: return json.loads(f.read_text())
        except: pass
    return {"reserve_pct": 20}

def _save_pocket_cfg(cfg: dict, user_id: str = "admin"):
    _user_json_file("pocket_config.json", user_id).write_text(json.dumps(cfg,ensure_ascii=False))

class PocketIncomeReq(BaseModel): amount: float; source: str = ""
class PocketExpenseReq(BaseModel): amount: float; note: str = ""; from_deferred: bool = False
class PocketCfgReq(BaseModel): reserve_pct: int

@app.get("/pocket")
def get_pocket(u: dict = Depends(current_user)):
    uid = _uid(u)
    cfg=_pocket_cfg(uid)
    rows=kuzu_rows(_conn.execute(
        "MATCH (f:Finance) WHERE f.category='pocket' AND f.user_id=$uid "
        "RETURN f.id,f.amount,f.direction,f.note,f.ts ORDER BY f.ts DESC",
        {"uid":uid}))
    items=[{"id":r[0],"amount":float(r[1]),"direction":r[2],"note":r[3],"ts":r[4]} for r in rows]
    balance=sum(i["amount"] for i in items if i["direction"]=="p_income")
    balance-=sum(i["amount"] for i in items if i["direction"]=="p_expense")
    balance+=sum(i["amount"] for i in items if i["direction"]=="p_adjust")
    deferred=sum(i["amount"] for i in items if i["direction"]=="p_deferred")
    deferred-=sum(i["amount"] for i in items if i["direction"]=="p_deferred_spend")
    deferred+=sum(i["amount"] for i in items if i["direction"]=="p_deferred_adjust")
    return {"balance":round(balance,2),"deferred":round(deferred,2),
            "reserve_pct":cfg["reserve_pct"],"transactions":items[:40]}

@app.post("/pocket/income")
def pocket_income(req: PocketIncomeReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    cfg=_pocket_cfg(uid); pct=cfg["reserve_pct"]/100
    ts=datetime.now().strftime("%Y-%m-%d %H:%M")
    deferred=round(req.amount*pct,2); spendable=round(req.amount-deferred,2)
    for d,a,n in [("p_income",spendable,req.source or "пополнение"),
                  ("p_deferred",deferred,f"резерв {cfg['reserve_pct']}% от {req.amount}")]:
        _conn.execute(
            "CREATE (:Finance {id:$id,amount:$a,direction:$d,category:'pocket',note:$n,ts:$ts,user_id:$uid})",
            {"id":str(uuid.uuid4()),"a":a,"d":d,"n":n,"ts":ts,"uid":uid})
    return {"ok":True,"spendable":spendable,"deferred":deferred}

@app.post("/pocket/expense")
def pocket_expense(req: PocketExpenseReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    direction="p_deferred_spend" if req.from_deferred else "p_expense"
    _conn.execute(
        "CREATE (:Finance {id:$id,amount:$a,direction:$d,category:'pocket',note:$n,ts:$ts,user_id:$uid})",
        {"id":str(uuid.uuid4()),"a":req.amount,"d":direction,
         "n":req.note or "расход","ts":datetime.now().strftime("%Y-%m-%d %H:%M"),"uid":uid})
    return {"ok":True}

@app.post("/pocket/config")
def pocket_config(req: PocketCfgReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    cfg=_pocket_cfg(uid); cfg["reserve_pct"]=max(0,min(99,req.reserve_pct))
    _save_pocket_cfg(cfg, uid)
    return {"ok":True,"reserve_pct":cfg["reserve_pct"]}

class PocketAdjustReq(BaseModel): amount: float; note: str = ""; target: str = "balance"

@app.post("/pocket/adjust")
def pocket_adjust(req: PocketAdjustReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    direction = "p_adjust" if req.target == "balance" else "p_deferred_adjust"
    note = req.note or ("корректировка баланса" if req.target == "balance" else "корректировка резерва")
    _conn.execute(
        "CREATE (:Finance {id:$id,amount:$a,direction:$d,category:'pocket',note:$n,ts:$ts,user_id:$uid})",
        {"id":str(uuid.uuid4()),"a":req.amount,"d":direction,"n":note,
         "ts":datetime.now().strftime("%Y-%m-%d %H:%M"),"uid":uid})
    return {"ok":True}

@app.get("/modes")
def get_modes(u: dict = Depends(current_user)):
    uid = _uid(u)
    rows=kuzu_rows(_conn.execute(
        "MATCH (m:Mode) WHERE m.user_id=$uid RETURN m.id,m.name,m.description,m.active,m.started_ts ORDER BY m.started_ts DESC",
        {"uid":uid}))
    return [{"id":r[0],"name":r[1],"description":r[2],"active":r[3]=="true","started_ts":r[4]} for r in rows]

@app.post("/modes")
def add_mode(req: ModeReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    mid=str(uuid.uuid4())
    _conn.execute(
        "CREATE (:Mode {id:$id,name:$n,description:$d,active:'true',started_ts:$ts,user_id:$uid})",
        {"id":mid,"n":req.name,"d":req.description,"ts":datetime.now().strftime("%Y-%m-%d %H:%M"),"uid":uid})
    return {"id":mid,"name":req.name,"active":True}

@app.post("/modes/{mid}/toggle")
def toggle_mode(mid: str, u: dict = Depends(current_user)):
    uid = _uid(u)
    rows=kuzu_rows(_conn.execute("MATCH (m:Mode) WHERE m.id=$id AND m.user_id=$uid RETURN m.active",{"id":mid,"uid":uid}))
    if rows:
        new="false" if rows[0][0]=="true" else "true"
        _conn.execute("MATCH (m:Mode) WHERE m.id=$id AND m.user_id=$uid SET m.active=$a",{"id":mid,"uid":uid,"a":new})
    return {"ok":True}

# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Life RPG</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html{height:100%;-webkit-text-size-adjust:100%}
:root{
  --page:#f0e9dc; --paper:#fdf8f0; --paper2:#e8ddd0;
  --ink:#2c2318; --ink2:#5a3e28; --ink3:#8a6a4a;
  --red:#8b2e0f; --blue:#1a4a6b; --gold:#8a5c2a; --green:#2d5c14;
  --border:#c8b89a; --border2:#ddd0b8; --shadow:rgba(44,35,24,.10);
}
body{background:var(--page);color:var(--ink);font-family:'Georgia',serif;
  display:grid;grid-template-columns:240px minmax(0,1fr);grid-template-rows:52px minmax(0,1fr) auto;
  width:100%;height:100vh;height:100dvh;overflow:hidden}

/* ── TOPBAR ── */
.topbar{grid-column:1/-1;grid-row:1;background:var(--paper2);
  border-bottom:1.5px solid var(--border);display:flex;align-items:center;
  padding:0 20px;gap:0;box-shadow:0 1px 5px var(--shadow);z-index:10}
.topbar-logo{font-size:15px;color:var(--ink2);letter-spacing:2px;font-style:italic;
  padding-right:22px;border-right:1px solid var(--border2);margin-right:4px;white-space:nowrap}
.topbar-tagline{font-size:9px;color:var(--ink3);font-family:sans-serif;
  letter-spacing:1.5px;text-transform:uppercase;margin-left:6px;margin-right:12px}
nav{display:flex;align-items:center;min-width:0}
.nav-item{padding:0 15px;height:52px;cursor:pointer;font-family:sans-serif;font-size:13px;
  color:var(--ink3);display:flex;align-items:center;gap:7px;
  border-bottom:2px solid transparent;transition:all .12s;white-space:nowrap}
.nav-item:hover{color:var(--ink2)}
.nav-item.active{color:var(--ink);border-bottom-color:var(--gold)}
.topbar-right{margin-left:auto;display:flex;align-items:center;gap:14px;min-width:0}
.topbar-date{font-size:12px;color:var(--ink3);font-family:sans-serif}
.topbar-settings{cursor:pointer;font-size:14px;color:var(--ink3);
  padding:4px 8px;border-radius:3px;transition:color .12s;font-family:sans-serif}
.topbar-settings:hover{color:var(--ink)}
#nav-api-status{font-size:10px;font-family:sans-serif;color:var(--ink3)}

/* ── SIDEBAR ── */
aside{grid-column:1;grid-row:2/4;background:var(--paper2);
  border-right:1.5px solid var(--border);display:flex;flex-direction:column;
  overflow-y:auto;padding:20px 16px}
.cal-widget{padding-bottom:14px;margin-bottom:14px;border-bottom:1px solid var(--border2)}
.cal-season{font-size:16px;color:var(--ink);margin-bottom:5px;font-family:'Georgia',serif}
.cal-moon{font-size:12px;color:var(--ink2);font-family:sans-serif;margin-bottom:3px}
.cal-patron{font-size:11px;color:var(--gold);font-family:sans-serif;margin-bottom:2px}
.cal-year{font-size:10px;color:var(--ink3);font-family:sans-serif;letter-spacing:.8px;margin-bottom:4px}
.cal-time{font-size:12px;color:var(--ink3);font-family:sans-serif}
.cal-sun{font-size:10px;color:var(--ink3);font-family:sans-serif;margin-top:3px}
.cal-effect{font-size:10px;color:var(--ink3);font-family:sans-serif;font-style:italic;
  margin-top:4px;opacity:.8;line-height:1.4}
.aside-bottom{margin-top:auto;padding-top:12px;border-top:1px solid var(--border2)}
.aside-bottom-link{cursor:pointer;font-size:11px;font-family:sans-serif;
  color:var(--ink3);padding:6px 4px;transition:color .12s}
.aside-bottom-link:hover{color:var(--gold)}

/* ── MAIN ── */
main{grid-column:2;grid-row:2;overflow:hidden;position:relative;min-width:0;min-height:0}
section{display:none;height:100%;overflow-y:auto;min-width:0}
section.active{display:block}

/* ── INPUT BAR ── */
#input-bar{grid-column:2;grid-row:3;background:var(--paper2);border-top:2px solid var(--border);
  padding:12px 24px;display:flex;gap:12px;align-items:flex-end;
  box-shadow:0 -2px 8px var(--shadow)}
#txt{flex:1;background:var(--paper);border:1px solid var(--border);color:var(--ink);
  font-family:'Georgia',serif;font-size:15px;padding:11px 16px;border-radius:4px;
  resize:none;height:60px;outline:none;line-height:1.55}
#txt:focus{border-color:var(--gold);box-shadow:0 0 0 2px rgba(139,105,20,.12)}
#txt::placeholder{color:var(--border)}
#send-btn{background:var(--gold);border:none;color:#fff;font-family:'Georgia',serif;
  font-size:14px;padding:0 28px;height:60px;border-radius:4px;cursor:pointer;
  white-space:nowrap;letter-spacing:.4px}
#send-btn:hover{background:#a07820}
#send-btn:disabled{background:var(--border2);color:var(--ink3);cursor:not-allowed}
/* Settings modal */
#settings-modal{display:none;position:fixed;inset:0;background:rgba(20,12,4,.65);z-index:500;
  align-items:center;justify-content:center}
#settings-modal.open{display:flex}
#settings-box{background:var(--paper);border:2px solid var(--border);border-radius:4px;
  padding:28px 32px;min-width:360px;max-width:480px}
.settings-title{font-size:16px;color:var(--ink);margin-bottom:18px}
.settings-label{font-size:11px;font-family:sans-serif;color:var(--ink3);
  letter-spacing:1px;text-transform:uppercase;margin-bottom:6px}
.settings-input{width:100%;box-sizing:border-box;background:var(--paper2);
  border:1px solid var(--border);color:var(--ink);font-family:'Courier New',monospace;
  font-size:12px;padding:8px 11px;border-radius:3px;outline:none;margin-bottom:12px}
.settings-input:focus{border-color:var(--gold)}
.settings-hint{font-size:11px;font-family:sans-serif;color:var(--ink3);margin-bottom:16px;line-height:1.6}
.settings-status{font-size:12px;font-family:sans-serif;margin-bottom:12px}
.settings-status.ok{color:var(--green)} .settings-status.missing{color:var(--red)}

/* ── JOURNAL ── */
#s-journal{padding:0}
.journal-main{overflow-y:auto;height:100%;padding:40px 56px 60px;background:var(--paper)}
.day-block{margin-bottom:0}
.day-heading{font-size:11px;color:var(--red);font-family:sans-serif;letter-spacing:3px;
  text-transform:uppercase;font-weight:700;margin:44px 0 4px;
  display:flex;align-items:center;gap:12px}
.day-heading::after{content:'';flex:1;height:1px;
  background:linear-gradient(to right,var(--border),transparent)}
.day-sub{font-size:10px;color:var(--ink3);font-family:sans-serif;letter-spacing:1.5px;
  margin-bottom:20px;font-style:italic}
.day-block:first-child .day-heading{margin-top:4px}
.entry{margin-bottom:24px}
.entry-text{font-size:15px;line-height:1.95;color:var(--ink);text-align:justify;hyphens:auto}
.entry+.entry::before{content:'✦';display:block;text-align:center;color:var(--border2);
  font-size:9px;margin-bottom:24px;letter-spacing:10px}
.entry-raw{font-size:12px;color:var(--ink3);font-style:italic;margin-top:10px;
  padding:8px 0 0 14px;border-left:2px solid var(--border2);line-height:1.6}
.entry-archivist{font-size:11px;color:var(--gold);font-family:sans-serif;font-style:italic;
  margin-top:10px;padding:6px 10px;border-left:2px solid rgba(139,105,20,.4);
  background:rgba(139,105,20,.04);line-height:1.6}
.entry-time{font-size:10px;color:var(--ink3);font-family:sans-serif;
  margin-top:6px;text-align:right;opacity:.65}
.ent-link{cursor:pointer;border-bottom:1px dotted currentColor;transition:opacity .1s}
.ent-link:hover{opacity:.7}
.ent-link.type-person{color:var(--blue)}
.ent-link.type-project{color:var(--green)}
.ent-link.type-concept{color:#7b4fa0}
.ent-link.type-event{color:var(--gold)}
.ent-link.type-place{color:#6b4a22}
.aside-section{margin-bottom:20px}
.aside-label{font-size:9px;letter-spacing:3px;color:var(--ink3);font-family:sans-serif;
  text-transform:uppercase;margin-bottom:8px;padding-bottom:6px;
  border-bottom:1px solid var(--border2)}
.aside-mission{padding:7px 10px;border:1px solid var(--border2);border-radius:3px;
  margin-bottom:5px;cursor:pointer;background:var(--paper);transition:border-color .12s}
.aside-mission:hover{border-color:var(--gold)}
.aside-mission-t{font-size:13px;color:var(--ink)}
.aside-entity{display:flex;align-items:center;gap:8px;padding:6px 0;cursor:pointer;
  border-bottom:.5px solid var(--border2)}
.aside-entity:last-child{border-bottom:none}
.aside-entity:hover .aside-entity-name{color:var(--blue)}
.aside-entity-ic{font-size:14px;flex-shrink:0}
.aside-entity-name{font-size:13px;color:var(--ink)}
.aside-entity-sub{font-size:10px;color:var(--ink3);font-family:sans-serif}

/* ── MISSIONS ── */
#s-missions{padding:0}
.missions-wrap{max-width:780px;margin:0 auto;padding:40px 40px 80px}
.missions-topbar{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:8px}
.missions-eyebrow{font-size:10px;letter-spacing:4px;text-transform:uppercase;
  color:var(--red);font-family:sans-serif}
.missions-heading{font-size:22px;color:var(--ink);margin-bottom:4px}
.missions-sub{font-size:12px;color:var(--ink3);font-family:sans-serif;margin-bottom:36px}
.mission-lore{font-size:12px;color:var(--ink3);font-style:italic;margin-top:3px;line-height:1.5;
  font-family:'Georgia',serif;opacity:.85}
.mission-block{margin-bottom:36px;padding-bottom:32px;border-bottom:1px solid var(--border2)}
.mission-block:last-child{border-bottom:none;padding-bottom:0}
.mission-block-hdr{display:flex;align-items:flex-start;gap:10px;cursor:pointer;
  padding:2px 0;user-select:none}
.mission-star{color:var(--gold);font-size:20px;flex-shrink:0;margin-top:1px;line-height:1}
.mission-block-info{flex:1}
.mission-block-title{font-size:19px;color:var(--ink);line-height:1.3}
.mission-block.done .mission-block-title{color:var(--ink3);text-decoration:line-through}
.mission-progress-badge{display:inline-block;font-size:10px;font-family:sans-serif;
  padding:2px 10px;border-radius:10px;margin-top:4px;
  background:rgba(139,105,20,.1);color:var(--gold);border:1px solid rgba(139,105,20,.25)}
.mission-block-chevron{font-size:11px;color:var(--border);margin-top:6px;flex-shrink:0;
  transition:transform .2s}
.mission-block-chevron.open{transform:rotate(180deg)}
.mission-desc-text{font-size:13px;color:var(--ink3);font-family:sans-serif;
  line-height:1.65;margin:10px 0 0 30px}
.quest-chain{margin:16px 0 0 28px;padding-left:16px;
  border-left:1.5px solid var(--border2);display:none}
.quest-chain.open{display:block}
.quest-item{display:flex;align-items:flex-start;gap:10px;padding:9px 0;
  border-bottom:.5px solid rgba(200,164,122,.25)}
.quest-item:last-child{border-bottom:none}
.quest-cb{width:16px;height:16px;border:1.5px solid var(--border);border-radius:2px;
  flex-shrink:0;margin-top:2px;cursor:pointer;display:flex;align-items:center;
  justify-content:center;font-size:9px;color:var(--green);transition:all .15s}
.quest-cb:hover{border-color:var(--gold)}
.quest-cb.done{background:var(--green);border-color:var(--green);color:#fff}
.quest-info{flex:1}
.quest-title{font-size:14px;color:var(--ink);line-height:1.45}
.quest-title.done{text-decoration:line-through;color:var(--ink3)}
.quest-ts{font-size:10px;color:var(--ink3);font-family:sans-serif;margin-top:2px}
.quest-del{background:none;border:none;color:var(--border2);cursor:pointer;
  font-size:14px;flex-shrink:0;padding:2px;transition:color .12s}
.quest-del:hover{color:var(--red)}
.archivist-warning{margin:12px 0;padding:12px 14px;
  background:rgba(139,46,15,.05);border-left:2px solid rgba(139,46,15,.4);
  font-size:12px;color:var(--red);font-family:sans-serif;line-height:1.7;font-style:italic}
.archivist-wisdom{margin:12px 0;padding:10px 14px;
  background:rgba(139,105,20,.05);border-left:2px solid rgba(139,105,20,.3);
  font-size:12px;color:var(--ink2);font-family:sans-serif;line-height:1.7;font-style:italic}
.quest-add-row{padding:12px 0 0}
.btn-quest-add{background:none;border:1px dashed var(--border2);color:var(--ink3);
  font-family:sans-serif;font-size:12px;padding:5px 14px;border-radius:3px;
  cursor:pointer;transition:all .15s}
.btn-quest-add:hover{border-color:var(--gold);color:var(--gold)}
.mission-actions{margin-top:14px;margin-left:28px;display:flex;gap:8px;flex-wrap:wrap}

/* ── BASE (знаний) ── */
#s-base{padding:0}
.base-wrap{padding:36px 40px 60px;overflow-y:auto;height:100%}
.base-topbar{display:flex;align-items:center;justify-content:space-between;
  margin-bottom:28px;padding-bottom:16px;border-bottom:2px solid var(--border)}
.base-heading{font-size:21px;color:var(--ink)}
.base-topbar-right{display:flex;gap:10px;align-items:center}
.ent-merge-btn{background:none;border:1px solid var(--border2);color:var(--ink3);
  font-family:'Georgia',serif;font-size:12px;padding:5px 12px;border-radius:3px;cursor:pointer;transition:all .12s}
.ent-merge-btn:hover{border-color:var(--blue);color:var(--blue)}
.merge-suggest{font-size:11px;font-family:sans-serif;padding:2px 9px;border-radius:10px;
  background:rgba(26,74,107,.08);color:var(--blue);border:1px solid rgba(26,74,107,.2);
  cursor:pointer;transition:background .12s}
.merge-suggest:hover{background:rgba(26,74,107,.18)}
.btn-add-entity{background:none;border:1px dashed var(--border2);color:var(--ink3);
  font-family:'Georgia',serif;font-size:12px;padding:4px 12px;border-radius:3px;cursor:pointer;transition:all .12s}
.btn-add-entity:hover{border-color:var(--gold);color:var(--gold)}
.btn-link-entity{background:none;border:1px dashed var(--border2);color:var(--ink3);
  font-size:11px;font-family:sans-serif;padding:2px 8px;border-radius:10px;cursor:pointer;transition:all .12s;margin-left:4px}
.btn-link-entity:hover{border-color:var(--gold);color:var(--gold)}
.btn-reanalyze{background:var(--red);border:none;color:#fff;font-family:'Georgia',serif;
  font-size:13px;padding:7px 18px;border-radius:3px;cursor:pointer;letter-spacing:.4px}
.btn-reanalyze:hover{background:#a03010}
.base-section{margin-bottom:40px}
.base-sec-hdr{display:flex;align-items:baseline;gap:10px;margin-bottom:14px;
  padding-bottom:8px;border-bottom:1.5px solid var(--border2)}
.base-sec-title{font-size:12px;letter-spacing:3px;font-family:sans-serif;
  text-transform:uppercase;font-weight:700}
.base-sec-count{font-size:11px;color:var(--ink3);font-family:sans-serif}
.base-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:14px}
.bcard{background:var(--paper);border:1px solid var(--border2);border-radius:3px;
  padding:18px 18px 14px 22px;cursor:pointer;transition:all .15s;
  box-shadow:1px 2px 6px var(--shadow);position:relative;overflow:hidden}
.bcard:hover{border-color:var(--gold);box-shadow:3px 4px 14px var(--shadow);transform:translateY(-1px)}
.bcard-stripe{position:absolute;left:0;top:0;bottom:0;width:3px}
.bcard-name{font-size:16px;color:var(--ink);margin-bottom:2px}
.bcard-type{font-size:9px;letter-spacing:2.5px;font-family:sans-serif;
  text-transform:uppercase;margin-bottom:10px;font-weight:600}
.bcard-summary{font-size:12px;color:var(--ink2);font-family:sans-serif;
  line-height:1.6;margin-bottom:10px}
.bcard-rels{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px}
.bcard-rel{font-size:10px;font-family:sans-serif;padding:2px 8px;border-radius:10px;
  background:rgba(139,105,20,.08);color:var(--gold);border:1px solid rgba(139,105,20,.2);
  cursor:pointer;transition:background .12s}
.bcard-rel:hover{background:rgba(139,105,20,.18)}
.bcard-tags{display:flex;flex-wrap:wrap;gap:3px;margin-top:4px}
.bcard-tag{font-size:9px;font-family:sans-serif;padding:1px 7px;border-radius:8px;
  background:var(--border2);color:var(--ink3)}
.bcard-footer{font-size:10px;color:var(--ink3);font-family:sans-serif;
  padding-top:8px;border-top:1px solid var(--border2);margin-top:4px}

/* ── ENTITY MODAL ── */
#ent-modal{display:none;position:fixed;inset:0;background:rgba(20,12,4,.55);z-index:300;
  align-items:center;justify-content:center}
#ent-modal.open{display:flex}
#ent-box{background:var(--paper);border:2px solid var(--border);border-radius:4px;
  padding:28px;width:560px;max-height:84vh;overflow-y:auto;position:relative;
  box-shadow:0 10px 36px rgba(0,0,0,.3)}
#ent-close{position:absolute;top:14px;right:14px;background:none;border:none;
  font-size:22px;cursor:pointer;color:var(--ink3)}
#ent-close:hover{color:var(--ink)}
.ent-name{font-size:22px;color:var(--ink);margin-bottom:2px}
.ent-type{font-size:9px;color:var(--gold);font-family:sans-serif;letter-spacing:3px;
  text-transform:uppercase;margin-bottom:12px}
.ent-summary{font-size:13px;color:var(--ink2);line-height:1.7;margin-bottom:18px;
  padding-bottom:14px;border-bottom:1px solid var(--border2)}
.ent-tags{display:flex;flex-wrap:wrap;gap:4px;margin-top:-10px;margin-bottom:14px}
.ent-tag{font-size:10px;font-family:sans-serif;padding:2px 8px;border-radius:8px;
  background:var(--border2);color:var(--ink3)}
.ent-sec{font-size:9px;letter-spacing:3px;color:var(--ink3);font-family:sans-serif;
  text-transform:uppercase;margin:14px 0 7px}
.rel-row{font-size:13px;color:var(--ink2);padding:5px 0;display:flex;gap:6px;
  align-items:baseline;flex-wrap:wrap;border-bottom:.5px solid var(--border2)}
.rel-row:last-child{border-bottom:none}
.rel-badge{font-size:11px;font-family:sans-serif;padding:1px 9px;border-radius:10px;
  background:rgba(139,105,20,.1);color:var(--gold);border:1px solid rgba(139,105,20,.25)}
.rel-name{cursor:pointer;color:var(--blue);border-bottom:1px dotted var(--blue)}
.rel-name:hover{opacity:.75}
.ment-ts{font-size:10px;color:var(--ink3);font-family:sans-serif;margin-top:10px;margin-bottom:3px}
.ment-text{font-size:13px;color:var(--ink2);line-height:1.6;padding-bottom:10px;
  border-bottom:.5px solid var(--border2)}
.ment-archivist{font-size:11px;color:var(--gold);font-family:sans-serif;
  font-style:italic;padding:4px 8px;background:rgba(139,105,20,.06);margin-top:4px}
.ent-del{background:none;border:1px solid rgba(139,46,15,.3);color:var(--red);
  font-family:sans-serif;font-size:11px;padding:4px 12px;border-radius:3px;cursor:pointer;
  margin-top:16px}
.ent-del:hover{background:var(--red);color:#fff}

/* ── REPEAT TASKS ── */
.quest-item.repeat-task{background:rgba(139,105,20,.04);border-left:2px solid rgba(139,105,20,.28);padding-left:10px}
.repeat-badge{font-size:9px;font-family:sans-serif;padding:1px 8px;border-radius:8px;letter-spacing:.8px;
  background:rgba(139,105,20,.1);color:var(--gold);border:1px solid rgba(139,105,20,.25);flex-shrink:0}
.repeat-progress{display:flex;align-items:center;gap:7px;margin-top:5px;flex-wrap:wrap}
.iter-btn{background:var(--gold);border:none;color:#fff;font-family:sans-serif;font-size:11px;
  padding:2px 11px;border-radius:10px;cursor:pointer;transition:background .12s;flex-shrink:0}
.iter-btn:hover{background:#a07820}
.iter-btn:disabled{background:var(--green);cursor:default}
.iter-count{font-size:13px;font-family:sans-serif;font-weight:700;color:var(--ink2)}
.iter-count.done{color:var(--green)}
.streak-display{font-size:11px;font-family:sans-serif;color:var(--gold)}
.streak-best{font-size:10px;font-family:sans-serif;color:var(--ink3)}
.reset-hint{font-size:10px;font-family:sans-serif;color:var(--ink3);opacity:.7}

/* ── INLINE EDIT ── */
.btn-edit-inline{background:none;border:none;color:var(--border);cursor:pointer;
  font-size:13px;padding:2px 6px;opacity:0;transition:opacity .15s;flex-shrink:0}
.mission-block-hdr:hover .btn-edit-inline{opacity:1}
.quest-item:hover .btn-edit-inline{opacity:1}
.btn-edit-inline:hover{color:var(--gold)}
.inline-edit-input{background:var(--paper2);border:1px solid var(--gold);color:var(--ink);
  font-family:'Georgia',serif;font-size:inherit;padding:2px 8px;border-radius:3px;
  outline:none;width:100%;box-shadow:0 0 0 2px rgba(139,105,20,.12)}

/* ── DIALOGS ── */
.dlg{display:none;position:fixed;inset:0;background:rgba(20,12,4,.5);z-index:400;
  align-items:center;justify-content:center}
.dlg.open{display:flex}
.dlg-box{background:var(--paper);border:2px solid var(--border);border-radius:4px;
  padding:26px;width:440px;box-shadow:0 8px 28px rgba(0,0,0,.2)}
.dlg-title{font-size:18px;color:var(--ink);margin-bottom:16px}
.dlg-input{width:100%;background:var(--paper2);border:1px solid var(--border);color:var(--ink);
  font-family:'Georgia',serif;font-size:14px;padding:10px 12px;border-radius:3px;
  outline:none;margin-bottom:10px}
.dlg-input:focus{border-color:var(--gold)}
.dlg-textarea{width:100%;background:var(--paper2);border:1px solid var(--border);color:var(--ink);
  font-family:'Georgia',serif;font-size:14px;padding:10px 12px;border-radius:3px;
  outline:none;resize:none;height:80px;margin-bottom:10px}
.dlg-textarea:focus{border-color:var(--gold)}
.dlg-btns{display:flex;gap:10px;justify-content:flex-end;margin-top:6px}
.btn-primary{background:var(--gold);border:none;color:#fff;font-family:'Georgia',serif;
  font-size:14px;padding:8px 24px;border-radius:3px;cursor:pointer}
.btn-primary:hover{background:#a07820}
.btn-cancel{background:none;border:1px solid var(--border);color:var(--ink3);
  font-family:'Georgia',serif;font-size:14px;padding:8px 20px;border-radius:3px;cursor:pointer}
.btn-cancel:hover{border-color:var(--ink2);color:var(--ink)}
.btn-add{background:none;border:1px solid var(--gold);color:var(--gold);
  font-family:'Georgia',serif;font-size:13px;padding:6px 18px;border-radius:3px;cursor:pointer}
.btn-add:hover{background:var(--gold);color:#fff}
.btn-sm{font-size:11px;font-family:sans-serif;padding:4px 12px;border-radius:3px;
  cursor:pointer;border:1px solid;background:none}
.btn-ok{border-color:var(--green);color:var(--green)}
.btn-ok:hover{background:var(--green);color:#fff}
.btn-danger{border-color:rgba(139,46,15,.4);color:var(--red)}
.btn-danger:hover{background:var(--red);color:#fff}
.empty{padding:48px 24px;text-align:center;color:var(--ink3);font-family:sans-serif;font-size:13px}

/* ── REANALYZE MODAL ── */
#reanalyze-modal{display:none;position:fixed;inset:0;background:rgba(20,12,4,.65);z-index:500;
  align-items:flex-start;justify-content:center;padding-top:40px}
#reanalyze-modal.open{display:flex}
#reanalyze-box{background:var(--paper);border:2px solid var(--border);border-radius:4px;
  width:680px;max-height:80vh;display:flex;flex-direction:column;
  box-shadow:0 16px 48px rgba(0,0,0,.35)}
.reanalyze-hdr{padding:20px 24px 14px;border-bottom:1px solid var(--border2);
  display:flex;align-items:center;justify-content:space-between}
.reanalyze-title{font-size:17px;color:var(--ink)}
.reanalyze-close{background:none;border:none;font-size:20px;cursor:pointer;color:var(--ink3)}
.reanalyze-body{padding:16px 24px;overflow-y:auto;flex:1}
.reanalyze-intro{font-size:13px;color:var(--ink3);font-family:sans-serif;
  line-height:1.65;margin-bottom:14px}
#reanalyze-prompt{width:100%;height:280px;background:var(--paper2);
  border:1px solid var(--border);color:var(--ink);font-family:'Courier New',monospace;
  font-size:11.5px;padding:12px;border-radius:3px;resize:none;outline:none;line-height:1.5}
.reanalyze-ftr{padding:14px 24px;border-top:1px solid var(--border2);
  display:flex;gap:10px;justify-content:flex-end}

::-webkit-scrollbar{width:5px}
::-webkit-scrollbar-track{background:var(--page)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}

/* ── POCKET ── */
#s-pocket{padding:40px 56px 60px;overflow-y:auto;background:var(--paper)}
.pocket-cards{display:grid;grid-template-columns:1fr 1fr;gap:18px;max-width:640px;margin-bottom:36px}
.pocket-card{background:var(--paper2);border:1px solid var(--border2);border-radius:4px;padding:20px 24px}
.pocket-card-label{font-size:10px;letter-spacing:3px;text-transform:uppercase;
  font-family:sans-serif;color:var(--ink3);margin-bottom:6px}
.pocket-card-amount{font-size:28px;color:var(--ink);letter-spacing:-.5px}
.pocket-card.deferred .pocket-card-amount{color:var(--gold)}
.pocket-reserve-row{display:flex;align-items:center;gap:10px;margin-bottom:30px;
  font-size:13px;font-family:sans-serif;color:var(--ink3)}
.pocket-reserve-row input{width:52px;background:var(--paper2);border:1px solid var(--border);
  color:var(--ink);font-family:sans-serif;font-size:13px;padding:4px 8px;border-radius:3px;
  text-align:center;outline:none}
.pocket-actions{display:flex;gap:12px;margin-bottom:32px;flex-wrap:wrap}
.pocket-btn{background:var(--gold);border:none;color:#fff;font-family:'Georgia',serif;
  font-size:13px;padding:9px 22px;border-radius:4px;cursor:pointer;letter-spacing:.3px}
.pocket-btn:hover{background:#a07820}
.pocket-btn.secondary{background:var(--paper2);color:var(--ink2);border:1px solid var(--border)}
.pocket-btn.secondary:hover{background:var(--border2)}
.pocket-btn.danger{background:var(--red)}
.pocket-btn.danger:hover{background:#6b1e08}
.pocket-form{background:var(--paper2);border:1px solid var(--border2);border-radius:4px;
  padding:18px 22px;max-width:480px;margin-bottom:24px;display:none}
.pocket-form.open{display:block}
.pocket-form-title{font-size:13px;letter-spacing:1px;color:var(--ink2);margin-bottom:12px}
.pocket-form input,.pocket-form textarea{width:100%;box-sizing:border-box;
  background:var(--paper);border:1px solid var(--border);color:var(--ink);
  font-family:'Georgia',serif;font-size:13px;padding:8px 11px;border-radius:3px;
  outline:none;margin-bottom:9px}
.pocket-form input:focus,.pocket-form textarea:focus{border-color:var(--gold)}
.pocket-form-row{display:flex;gap:9px}
.pocket-tx-list{max-width:640px}
.pocket-tx-item{display:flex;justify-content:space-between;align-items:flex-start;
  padding:9px 0;border-bottom:1px solid var(--border2);font-family:sans-serif;font-size:13px}
.pocket-tx-item:last-child{border-bottom:none}
.pocket-tx-note{color:var(--ink2);flex:1}
.pocket-tx-ts{font-size:10px;color:var(--ink3);margin-top:2px}
.pocket-tx-amount{font-weight:700;white-space:nowrap;margin-left:12px}
.pocket-tx-amount.income{color:var(--green)}
.pocket-tx-amount.expense{color:var(--red)}
.pocket-tx-amount.deferred{color:var(--gold)}
.pocket-section-title{font-size:11px;letter-spacing:3px;text-transform:uppercase;
  font-family:sans-serif;color:var(--ink3);margin-bottom:14px}
/* ── SOUND TOGGLE ── */
.sound-btn{background:none;border:1px solid var(--border2);color:var(--ink3);
  font-family:sans-serif;font-size:11px;padding:3px 10px;border-radius:10px;
  cursor:pointer;transition:all .15s;white-space:nowrap}
.sound-btn:hover{border-color:var(--gold);color:var(--gold)}
.sound-btn.on{border-color:var(--gold);color:var(--gold);background:rgba(138,92,42,.07)}
/* ── CHARACTER STATS SIDEBAR ── */
.char-section{margin-top:4px;padding-top:14px;border-top:1px solid var(--border2)}
.stat-row{display:flex;align-items:center;gap:8px;padding:7px 0;
  cursor:pointer;border-bottom:.5px solid var(--border2);transition:opacity .12s}
.stat-row:last-of-type{border-bottom:none}
.stat-row:hover{opacity:.75}
.stat-name{font-size:12px;color:var(--ink);font-family:sans-serif;
  width:82px;flex-shrink:0;font-weight:600}
.stat-bar-wrap{flex:1;height:3px;background:var(--border2);border-radius:2px;overflow:hidden}
.stat-bar{height:3px;border-radius:2px;transition:width .8s ease;width:0%}
.stat-val{font-size:10px;font-family:sans-serif;color:var(--ink3);
  width:24px;text-align:right;flex-shrink:0}
.char-analyze-btn{font-size:10px;font-family:sans-serif;color:var(--ink3);
  cursor:pointer;padding:6px 0 2px;transition:color .12s;display:block}
.char-analyze-btn:hover{color:var(--gold)}
/* ── PAST MOON MEMORY ── */
.past-moon-block{margin:0 0 28px;padding:12px 16px;background:var(--paper2);
  border-left:2px solid var(--border2);font-family:'Georgia',serif}
.past-moon-label{font-size:9px;letter-spacing:2px;text-transform:uppercase;
  color:var(--ink3);font-family:sans-serif;margin-bottom:6px}
.past-moon-text{font-size:13px;color:var(--ink3);line-height:1.6;font-style:italic}
.past-moon-ts{font-size:10px;color:var(--border);font-family:sans-serif;margin-top:5px}
/* ── ANTAGONIST CARD ── */
.antag-card{background:rgba(139,46,15,.04);border:1px solid rgba(139,46,15,.2);
  border-radius:3px;padding:18px 20px;margin-bottom:32px;position:relative}
.antag-card::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;
  background:var(--red);border-radius:3px 0 0 3px}
.antag-title{font-size:10px;letter-spacing:2px;text-transform:uppercase;
  font-family:sans-serif;color:var(--red);margin-bottom:8px}
.antag-name{font-size:20px;color:var(--ink);margin-bottom:6px}
.antag-desc{font-size:13px;color:var(--ink2);font-family:sans-serif;
  line-height:1.65;font-style:italic}
.antag-hint{font-size:10px;font-family:sans-serif;color:var(--ink3);margin-top:10px}
/* ── MISSION ENTITIES (tags on path card) ── */
.mission-entities{display:flex;flex-wrap:wrap;gap:5px;margin:10px 0 0 28px}
.mission-ent-tag{font-size:10px;font-family:sans-serif;padding:2px 9px;border-radius:10px;
  background:rgba(139,105,20,.08);color:var(--gold);border:1px solid rgba(139,105,20,.2);
  cursor:pointer;transition:background .12s}
.mission-ent-tag:hover{background:rgba(139,105,20,.18)}
/* ── MISSION DESCRIPTION ── */
.mission-desc-view{margin:6px 0 0 28px;font-size:12.5px;color:var(--ink2);
  font-family:'Georgia',serif;line-height:1.5;cursor:pointer;padding:4px 8px;
  border-radius:3px;transition:background .12s}
.mission-desc-view:hover{background:rgba(139,105,20,.06)}
.mission-desc-empty{color:var(--ink3);font-style:italic}
.mission-desc-edit{margin:6px 0 0 28px;width:calc(100% - 28px);box-sizing:border-box}
.mission-desc-edit textarea{width:100%;box-sizing:border-box;background:var(--paper2);
  border:1px solid var(--border);color:var(--ink);font-family:'Georgia',serif;
  font-size:13px;padding:8px 11px;border-radius:3px;outline:none;
  resize:none;display:block}
.mission-desc-edit textarea:focus{border-color:var(--gold)}
/* ── EPILOGUE ── */
.mission-epilogue{margin:14px 0 0 28px;padding:12px 14px;
  background:rgba(139,105,20,.05);border-left:2px solid rgba(139,105,20,.35);
  font-size:12px;color:var(--ink2);font-family:'Georgia',serif;
  font-style:italic;line-height:1.7}
.mission-epilogue-label{font-size:9px;letter-spacing:2px;text-transform:uppercase;
  font-family:sans-serif;color:var(--gold);margin-bottom:6px}
/* ── ORACLE MODAL ── */
#oracle-modal{display:none;position:fixed;inset:0;background:rgba(44,35,24,.55);z-index:600;
  align-items:center;justify-content:center}
#oracle-modal.open{display:flex}
#oracle-box{background:var(--paper);border:2px solid var(--border);border-radius:4px;
  padding:32px 36px;width:520px;max-width:90vw;position:relative;
  box-shadow:0 16px 48px rgba(44,35,24,.25)}
.oracle-eyebrow{font-size:9px;letter-spacing:3px;text-transform:uppercase;
  color:var(--ink3);font-family:sans-serif;margin-bottom:8px}
.oracle-title{font-size:20px;color:var(--ink);margin-bottom:6px}
.oracle-effect{font-size:12px;color:var(--ink3);font-family:sans-serif;
  font-style:italic;margin-bottom:20px;padding-bottom:16px;border-bottom:1px solid var(--border2)}
.oracle-body{font-size:15px;color:var(--ink2);line-height:1.85;font-style:italic;
  min-height:80px}
.oracle-loading{color:var(--ink3);font-family:sans-serif;font-size:13px;animation:pulse 1.4s infinite}
@keyframes pulse{0%,100%{opacity:.5}50%{opacity:1}}
#oracle-close{position:absolute;top:14px;right:16px;background:none;border:none;
  font-size:22px;cursor:pointer;color:var(--ink3);line-height:1}
#oracle-close:hover{color:var(--ink)}
/* ── MECHANIC CARDS ── */
.mech-section{margin-top:48px;padding-top:32px;border-top:2px solid var(--border2)}
.mech-section-hdr{margin-bottom:20px}
.mech-section-title{font-size:11px;letter-spacing:3px;font-family:sans-serif;
  text-transform:uppercase;font-weight:700;color:var(--ink3)}
.mech-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.mcard{background:var(--paper2);border:1px solid var(--border2);border-radius:3px;
  padding:16px 18px;cursor:pointer;transition:all .15s;position:relative;overflow:hidden}
.mcard:hover{border-color:var(--gold);box-shadow:2px 3px 12px var(--shadow);transform:translateY(-1px)}
.mcard.current{border-color:var(--gold);background:var(--paper)}
.mcard.current::after{content:'сейчас';position:absolute;top:10px;right:12px;
  font-size:9px;font-family:sans-serif;letter-spacing:1.5px;color:var(--gold);
  text-transform:uppercase}
.mcard-emoji{font-size:22px;margin-bottom:8px}
.mcard-name{font-size:15px;color:var(--ink);margin-bottom:4px}
.mcard-sub{font-size:11px;color:var(--gold);font-family:sans-serif;margin-bottom:6px}
.mcard-effect{font-size:12px;color:var(--ink3);font-family:sans-serif;
  line-height:1.5;font-style:italic}
.mcard-hint{font-size:10px;font-family:sans-serif;color:var(--border);margin-top:10px;
  border-top:1px solid var(--border2);padding-top:6px}
.mcard:hover .mcard-hint{color:var(--gold)}
/* sidebar clickable */
.cal-clickable{cursor:pointer;border-bottom:1px dotted var(--border);
  transition:color .12s;display:inline}
.cal-clickable:hover{color:var(--gold)}
/* ── JOURNAL daily count ── */
.daily-done-badge{display:inline-flex;align-items:center;gap:6px;
  font-family:sans-serif;font-size:12px;color:var(--green);
  background:rgba(45,92,20,.08);border:1px solid rgba(45,92,20,.2);
  border-radius:10px;padding:2px 10px;margin-left:10px}

/* ── RESPONSIVE LAYOUT ── */
@media (max-width: 920px){
  body{
    grid-template-columns:minmax(0,1fr);
    grid-template-rows:auto auto minmax(0,1fr) auto;
  }
  .topbar{
    grid-row:1;padding:8px 12px;min-height:0;height:auto;gap:8px 10px;
    align-items:center;flex-wrap:wrap;
  }
  .topbar-logo{padding-right:12px;margin-right:0}
  .topbar-tagline{margin:0;font-size:8px}
  nav{
    order:3;width:100%;overflow-x:auto;overflow-y:hidden;
    -webkit-overflow-scrolling:touch;scrollbar-width:none;
  }
  nav::-webkit-scrollbar{display:none}
  .nav-item[data-s="base"]{display:flex!important}
  .nav-item{height:38px;padding:0 12px;font-size:12px;flex:0 0 auto}
  .topbar-right{gap:8px;flex-wrap:wrap;justify-content:flex-end}
  .topbar-date{font-size:11px}
  #nav-api-status{font-size:9px}
  aside{
    grid-column:1;grid-row:2;border-right:none;border-bottom:1.5px solid var(--border);
    padding:10px 12px;display:flex;flex-direction:row;gap:12px;
    overflow-x:auto;overflow-y:hidden;max-height:178px;
    -webkit-overflow-scrolling:touch;
  }
  aside > *{flex:0 0 210px;max-height:154px;overflow-y:auto}
  .cal-widget{padding-bottom:0;margin-bottom:0;border-bottom:none}
  .aside-section,.char-section,.aside-bottom{
    margin:0;padding:0;border:1px solid var(--border2);background:rgba(253,248,240,.45);
    border-radius:4px;padding:10px;
  }
  .aside-bottom{flex:0 0 150px;display:flex;align-items:center}
  .stat-name{width:74px}
  main{grid-column:1;grid-row:3}
  #input-bar{
    grid-column:1;grid-row:4;padding:10px 12px calc(10px + env(safe-area-inset-bottom));
    gap:10px;align-items:stretch;
  }
  #txt{height:52px;min-height:52px;font-size:16px;padding:9px 12px}
  #send-btn{height:52px;padding:0 18px}
  .journal-main{padding:28px 22px 44px}
  .entry-text{text-align:left;line-height:1.8}
  .missions-wrap{max-width:none;padding:30px 22px 56px}
  .missions-sub{margin-bottom:24px}
  .base-wrap,#s-pocket{padding:28px 22px 48px}
  .base-topbar{gap:12px;align-items:flex-start}
  .base-grid{grid-template-columns:repeat(auto-fill,minmax(min(100%,240px),1fr))}
  .mech-grid{grid-template-columns:repeat(auto-fill,minmax(min(100%,210px),1fr))}
  .pocket-cards{grid-template-columns:repeat(2,minmax(0,1fr));max-width:none}
  .pocket-form,.pocket-tx-list{max-width:none}
  .btn-edit-inline{opacity:1}
  .dlg,#ent-modal,#settings-modal,#oracle-modal{padding:12px;align-items:center}
  .dlg-box,#settings-box,#ent-box,#oracle-box{
    width:100%;max-width:calc(100vw - 24px);max-height:calc(100dvh - 24px);
    overflow-y:auto;padding:22px;
  }
  #reanalyze-modal{padding:12px;align-items:center}
  #reanalyze-box{width:100%;max-width:calc(100vw - 24px);max-height:calc(100dvh - 24px)}
}

@media (max-width: 560px){
  .topbar{padding:7px 10px}
  .topbar-logo{font-size:14px;letter-spacing:1.5px}
  .topbar-tagline{display:none}
  .sound-btn{padding:3px 8px;font-size:10px}
  .topbar-settings{padding:4px 6px}
  aside{max-height:150px;padding:8px 10px;gap:10px}
  aside > *{flex-basis:190px;max-height:130px}
  .cal-season{font-size:14px}
  .journal-main{padding:22px 16px 36px}
  .day-heading{letter-spacing:2px;gap:8px;align-items:flex-start;flex-wrap:wrap}
  .day-heading::after{min-width:80px}
  .daily-done-badge{margin-left:0;font-size:11px}
  .missions-wrap,.base-wrap,#s-pocket{padding:22px 16px 40px}
  .missions-topbar,.base-topbar{align-items:stretch;flex-direction:column}
  .base-topbar-right,.mission-actions,.dlg-btns,.pocket-form-row{
    width:100%;justify-content:flex-start;flex-wrap:wrap;
  }
  .btn-add,.btn-reanalyze,.btn-add-entity,.btn-primary,.btn-cancel,.pocket-btn{
    min-height:38px;
  }
  .quest-chain{margin-left:10px;padding-left:10px}
  .mission-desc-view,.mission-desc-edit,.mission-entities,.mission-epilogue,.mission-actions{margin-left:0}
  .mission-block-title{font-size:17px}
  .quest-item{gap:8px}
  .repeat-progress{gap:6px}
  .pocket-cards{grid-template-columns:1fr;gap:12px;margin-bottom:24px}
  .pocket-card{padding:16px 18px}
  .pocket-card-amount{font-size:24px;letter-spacing:0}
  .pocket-reserve-row{align-items:flex-start;flex-wrap:wrap}
  .pocket-actions{gap:9px;margin-bottom:24px}
  .pocket-btn{padding:8px 14px}
  .pocket-tx-item{gap:8px}
  .pocket-tx-amount{margin-left:0}
  .bcard{padding:16px 16px 13px 20px}
  .mcard.current::after{position:static;display:inline-block;margin-top:8px}
  #settings-box .pocket-btn{width:100%;margin-bottom:8px}
}

@media (max-width: 420px){
  .topbar-right{width:100%;justify-content:space-between}
  #txt{height:54px;min-height:54px}
  #send-btn{height:54px;padding:0 14px}
  .entry-text{font-size:14.5px;line-height:1.75}
  .entry-raw{padding-left:10px}
  .mission-block-hdr{gap:8px}
  .quest-item.repeat-task{padding-left:8px}
  .iter-btn{padding:3px 10px}
  .dlg-box,#settings-box,#ent-box,#oracle-box{padding:18px}
}

/* ══════════════════════════════════════════════════════════════
   MOBILE APP LAYOUT  ≤ 768px
   ══════════════════════════════════════════════════════════════ */

/* hamburger + bottom-nav hidden by default (desktop) */
#mob-hamburger{ display:none; }
#bottom-nav{ display:none; }
#drawer-backdrop{ display:none; }

@media (max-width: 768px){

  /* ── Grid: topbar + content + bottom nav ── */
  body{
    grid-template-columns: 1fr;
    grid-template-rows: 52px minmax(0,1fr) calc(56px + env(safe-area-inset-bottom));
    height: 100dvh;
    overflow: hidden;
  }

  /* ── Topbar: compact, no nav tabs ── */
  .topbar{
    grid-row:1; grid-column:1;
    display:flex; align-items:center; justify-content:space-between;
    padding:0 14px; height:52px; flex-wrap:nowrap; gap:0;
    border-bottom:1px solid var(--border);
  }
  .topbar > nav{ display:none !important; }
  .topbar-logo{ font-size:15px; letter-spacing:2px; padding-right:0; margin-right:0; }
  .topbar-tagline{ display:none; }
  .topbar-right{ gap:6px; flex-wrap:nowrap; }
  .topbar-date{ display:none; }
  #nav-api-status{ display:none; }
  .sound-btn{ padding:3px 8px; font-size:10px; }
  .topbar-settings{ padding:5px 7px; font-size:15px; }

  /* hamburger button visible on mobile */
  #mob-hamburger{
    display:flex; align-items:center; justify-content:center;
    width:36px; height:36px; cursor:pointer; font-size:20px;
    border-radius:6px; transition:background .15s; user-select:none;
    -webkit-tap-highlight-color:transparent;
  }
  #mob-hamburger:active{ background:rgba(0,0,0,.08); }

  /* ── Sidebar → slide-in drawer ── */
  aside{
    position:fixed; top:52px; left:0; bottom:0;
    width:82vw; max-width:320px;
    height:calc(100dvh - 52px); max-height:none;
    display:flex !important; flex-direction:column !important;
    overflow-y:auto; overflow-x:hidden;
    z-index:1200; transform:translateX(-105%);
    transition:transform .28s cubic-bezier(.4,0,.2,1);
    border-right:1.5px solid var(--border); border-bottom:none;
    background:var(--paper2); padding:20px 18px 32px;
    gap:18px; box-shadow: 4px 0 24px rgba(0,0,0,.18);
    grid-row:unset; grid-column:unset;
  }
  aside > *{ flex:0 0 auto; max-height:none; }
  aside.drawer-open{ transform:translateX(0); }
  .cal-widget{ padding-bottom:0; margin-bottom:0; border-bottom:none; }
  .aside-section,.char-section,.aside-bottom{
    margin:0; border:1px solid var(--border2); background:var(--paper);
    border-radius:6px; padding:12px;
  }
  .aside-bottom{ display:flex; align-items:center; }

  /* drawer backdrop */
  #drawer-backdrop{
    display:block; position:fixed; top:52px; left:0; right:0; bottom:0; z-index:1100;
    background:rgba(0,0,0,.50); backdrop-filter:blur(2px);
    opacity:0; pointer-events:none;
    transition:opacity .25s;
  }
  #drawer-backdrop.visible{ opacity:1; pointer-events:auto; }

  /* ── Main scrollable area ── */
  main{
    grid-row:2; grid-column:1;
    overflow-y:auto; -webkit-overflow-scrolling:touch;
  }

  /* ── Input bar: fixed above bottom nav ── */
  #input-bar{
    position:fixed;
    bottom:calc(56px + env(safe-area-inset-bottom));
    left:0; right:0;
    padding:8px 14px;
    gap:10px; align-items:stretch;
    z-index:80;
    display:none; /* shown only on journal tab */
  }
  #input-bar.mob-visible{ display:flex; }
  #txt{ height:48px; min-height:48px; font-size:16px; padding:9px 12px; }
  #send-btn{ height:48px; padding:0 18px; white-space:nowrap; }

  /* ── Bottom navigation bar ── */
  #bottom-nav{
    display:flex;
    position:fixed; bottom:0; left:0; right:0;
    height:calc(56px + env(safe-area-inset-bottom));
    padding-bottom:env(safe-area-inset-bottom);
    background:var(--paper2);
    border-top:1.5px solid var(--border);
    z-index:900;
    align-items:stretch;
    box-shadow:0 -2px 12px rgba(0,0,0,.10);
  }
  .bnav-item{
    flex:1; display:flex; flex-direction:column;
    align-items:center; justify-content:center;
    gap:3px; cursor:pointer;
    font-size:9px; letter-spacing:.5px; text-transform:uppercase;
    color:var(--muted); transition:color .15s;
    -webkit-tap-highlight-color:transparent;
    padding:6px 4px 4px;
  }
  .bnav-item .bnav-icon{ font-size:22px; line-height:1; }
  .bnav-item.active{ color:var(--accent); }
  .bnav-item:active{ opacity:.6; }

  /* ── Content padding: bottom = bottom-nav + input-bar height ── */
  .journal-main{ padding:18px 16px 176px; }
  .missions-wrap,.base-wrap,#s-pocket{ padding:18px 16px 80px; }
  .missions-sub{ margin-bottom:20px; }

  /* ── Cards ── */
  .base-grid{ grid-template-columns:repeat(auto-fill,minmax(min(100%,240px),1fr)); }
  .mech-grid{ grid-template-columns:repeat(auto-fill,minmax(min(100%,210px),1fr)); }
  .pocket-cards{ grid-template-columns:1fr; max-width:none; gap:12px; margin-bottom:20px; }
  .pocket-form,.pocket-tx-list{ max-width:none; }

  /* ── Full-screen sheet modals ── */
  .dlg,#ent-modal,#settings-modal,#oracle-modal,#reanalyze-modal{
    padding:0; align-items:flex-end;
    z-index:1300 !important; /* above drawer(1200) and bottom-nav(900) */
  }
  .dlg-box,#settings-box,#ent-box,#oracle-box{
    width:100%; max-width:100%; border-radius:18px 18px 0 0;
    max-height:88dvh; overflow-y:auto; padding:24px 20px 36px;
  }
  #reanalyze-box{
    width:100%; max-width:100%; border-radius:18px 18px 0 0;
    max-height:88dvh; overflow-y:auto;
  }

  /* ── Touch targets ── */
  .btn-add,.btn-reanalyze,.btn-add-entity,.btn-primary,.btn-cancel,.pocket-btn{ min-height:44px; }
  .quest-item{ min-height:44px; }

  /* ── Topbar actions ── */
  .missions-topbar,.base-topbar{ align-items:stretch; flex-direction:column; }
  .base-topbar-right,.mission-actions,.dlg-btns,.pocket-form-row{
    width:100%; justify-content:flex-start; flex-wrap:wrap;
  }

  /* ── Misc ── */
  .btn-edit-inline{ opacity:1; }
  .bcard{ padding:16px 16px 13px 20px; }
  .mcard.current::after{ position:static; display:inline-block; margin-top:8px; }
  #settings-box .pocket-btn{ width:100%; margin-bottom:8px; }
  .entry-text{ text-align:left; line-height:1.8; }
  .mission-block-title{ font-size:17px; }
  .quest-chain{ margin-left:10px; padding-left:10px; }
  .mission-desc-view,.mission-desc-edit,.mission-entities,.mission-epilogue,.mission-actions{ margin-left:0; }
  .pocket-card{ padding:16px 18px; }
  .pocket-card-amount{ font-size:24px; letter-spacing:0; }
}

</style>
</head>
<body>

<div class="topbar">
  <div id="mob-hamburger" onclick="toggleDrawer()">☰</div>
  <div class="topbar-logo">Life RPG</div>
  <div class="topbar-tagline">живая летопись</div>
  <nav>
    <div class="nav-item active" data-s="journal" onclick="nav(this)">🗺️ Дневник</div>
    <div class="nav-item" data-s="missions" onclick="nav(this)">⚔️ Пути</div>
    <div class="nav-item" data-s="pocket" onclick="nav(this)">💰 Карман</div>
    <div class="nav-item" data-s="base" onclick="nav(this)" style="display:none">🗄️ База знаний</div>
  </nav>
  <div class="topbar-right">
    <div id="nav-api-status"></div>
    <div class="topbar-date" id="hdr-date"></div>
    <button class="sound-btn" id="sound-btn" onclick="toggleSound()">♪ амбиент</button>
    <div class="topbar-settings" onclick="openSettings()">⚙</div>
    <div class="topbar-settings" onclick="doLogout()" title="Выйти из аккаунта" style="font-size:11px;letter-spacing:.5px">Выход</div>
  </div>
</div>

<!-- Mobile drawer backdrop -->
<div id="drawer-backdrop" onclick="closeDrawer()"></div>

<aside id="sidebar">
  <div class="cal-widget" id="sidebar-cal"></div>
  <div class="aside-section">
    <div class="aside-label">Активные пути</div>
    <div id="aside-missions"></div>
  </div>
  <div class="char-section" id="char-sidebar"></div>
  <div class="aside-bottom">
    <div class="aside-bottom-link" onclick="nav(document.querySelector('[data-s=base]'));closeDrawer()">🗄️ База знаний →</div>
  </div>
</aside>

<!-- Bottom navigation (mobile only) -->
<nav id="bottom-nav">
  <div class="bnav-item active" data-s="journal" onclick="navMob(this)">
    <span class="bnav-icon">🗺️</span><span>Дневник</span>
  </div>
  <div class="bnav-item" data-s="missions" onclick="navMob(this)">
    <span class="bnav-icon">⚔️</span><span>Пути</span>
  </div>
  <div class="bnav-item" data-s="pocket" onclick="navMob(this)">
    <span class="bnav-icon">💰</span><span>Карман</span>
  </div>
  <div class="bnav-item" data-s="base" onclick="navMob(this)">
    <span class="bnav-icon">🗄️</span><span>База</span>
  </div>
</nav>

<main>

  <!-- JOURNAL -->
  <section id="s-journal" class="active">
    <div class="journal-main" id="journal-main">
      <div class="empty">Дневник пуст. Напиши первую запись ↓</div>
    </div>
  </section>

  <!-- MISSIONS -->
  <section id="s-missions">
    <div class="missions-wrap">
      <div class="missions-topbar">
        <div>
          <div class="missions-eyebrow">Пути Героя</div>
          <div class="missions-heading">Пути</div>
        </div>
        <button class="btn-add" onclick="openDlg('mission-dlg')">+ Новый путь</button>
      </div>
      <div class="missions-sub">Пути, по которым идёт Герой. Задания рождаются из записей.</div>
      <div id="missions-list"></div>
    </div>
  </section>

  <!-- BASE OF KNOWLEDGE -->
  <section id="s-base">
    <div class="base-wrap" id="base-wrap">
      <div class="base-topbar">
        <div class="base-heading">🗄️ База знаний</div>
        <div class="base-topbar-right">
          <button class="btn-reanalyze" onclick="reanalyze()">⟳ Переосмыслить</button>
          <button class="btn-add-entity" onclick="openEntityDlg('')">+ сущность</button>
        </div>
      </div>
      <div id="base-content">
        <div class="empty">Загрузка базы знаний...</div>
      </div>
    </div>
  </section>

  <section id="s-pocket">
    <div style="margin-bottom:8px;display:flex;align-items:baseline;gap:14px">
      <div style="font-size:10px;letter-spacing:4px;text-transform:uppercase;color:var(--red);font-family:sans-serif">КАРМАН ГЕРОЯ</div>
    </div>
    <div style="font-size:22px;color:var(--ink);margin-bottom:4px">Карман</div>
    <div style="font-size:12px;color:var(--ink3);font-family:sans-serif;margin-bottom:28px">Учёт средств · баланс · резерв</div>

    <div class="pocket-cards" id="pocket-cards">
      <div class="pocket-card">
        <div class="pocket-card-label">Баланс</div>
        <div class="pocket-card-amount" id="pc-balance">—</div>
      </div>
      <div class="pocket-card deferred">
        <div class="pocket-card-label">Отложено</div>
        <div class="pocket-card-amount" id="pc-deferred">—</div>
      </div>
    </div>

    <div class="pocket-reserve-row">
      <span>Резерв:</span>
      <input type="number" id="pc-reserve-pct" min="0" max="99" value="20" style="width:52px">
      <span>% от каждого пополнения</span>
      <button class="pocket-btn secondary" style="padding:4px 14px;font-size:12px" onclick="savePocketCfg()">Сохранить</button>
    </div>

    <div class="pocket-actions">
      <button class="pocket-btn" onclick="togglePocketForm('income')">+ Пополнить</button>
      <button class="pocket-btn secondary" onclick="togglePocketForm('expense')">− Потратить</button>
      <button class="pocket-btn danger" onclick="togglePocketForm('deferred-spend')" style="font-size:12px">⚑ Из резерва</button>
      <button class="pocket-btn secondary" onclick="togglePocketForm('adjust')" style="font-size:12px">✎ Корректировка</button>
    </div>

    <div class="pocket-form" id="pf-income">
      <div class="pocket-form-title">ПОПОЛНЕНИЕ</div>
      <input type="number" id="pf-income-amount" placeholder="Сумма" min="0" step="0.01">
      <input type="text" id="pf-income-source" placeholder="Откуда пришли деньги">
      <div class="pocket-form-row">
        <button class="pocket-btn" onclick="submitPocketIncome()">Добавить</button>
        <button class="pocket-btn secondary" onclick="closePocketForms()">Отмена</button>
      </div>
    </div>

    <div class="pocket-form" id="pf-expense">
      <div class="pocket-form-title">РАСХОД</div>
      <input type="number" id="pf-expense-amount" placeholder="Сумма" min="0" step="0.01">
      <input type="text" id="pf-expense-note" placeholder="На что потрачено">
      <div class="pocket-form-row">
        <button class="pocket-btn secondary" onclick="submitPocketExpense(false)">Потратить</button>
        <button class="pocket-btn secondary" onclick="closePocketForms()">Отмена</button>
      </div>
    </div>

    <div class="pocket-form" id="pf-deferred-spend">
      <div class="pocket-form-title">РАСХОД ИЗ РЕЗЕРВА</div>
      <div style="font-size:12px;font-family:sans-serif;color:var(--ink3);margin-bottom:10px">Только на значимое для тебя.</div>
      <input type="number" id="pf-ds-amount" placeholder="Сумма" min="0" step="0.01">
      <input type="text" id="pf-ds-note" placeholder="Что это значит для тебя">
      <div class="pocket-form-row">
        <button class="pocket-btn danger" onclick="submitPocketExpense(true)">Потратить из резерва</button>
        <button class="pocket-btn secondary" onclick="closePocketForms()">Отмена</button>
      </div>
    </div>

    <div class="pocket-form" id="pf-adjust">
      <div class="pocket-form-title">КОРРЕКТИРОВКА</div>
      <div style="font-size:12px;font-family:sans-serif;color:var(--ink3);margin-bottom:10px">Положительное число увеличивает, отрицательное — уменьшает.</div>
      <select id="pf-adjust-target" style="width:100%;margin-bottom:8px;background:var(--paper);border:1px solid var(--border);color:var(--ink);padding:7px;border-radius:2px">
        <option value="balance">Баланс</option>
        <option value="deferred">Резерв (отложено)</option>
      </select>
      <input type="number" id="pf-adjust-amount" placeholder="Сумма (может быть отрицательной)" step="0.01">
      <input type="text" id="pf-adjust-note" placeholder="Причина корректировки">
      <div class="pocket-form-row">
        <button class="pocket-btn secondary" onclick="submitPocketAdjust()">Применить</button>
        <button class="pocket-btn secondary" onclick="closePocketForms()">Отмена</button>
      </div>
    </div>

    <div class="pocket-section-title">ИСТОРИЯ</div>
    <div class="pocket-tx-list" id="pocket-tx-list"><div class="empty">Загрузка...</div></div>
  </section>

</main>

<div id="input-bar">
  <textarea id="txt" placeholder="Что произошло? Говори свободно... (Cmd+Enter)"></textarea>
  <button id="send-btn" onclick="sendEntry()">Записать →</button>
</div>

<!-- Oracle Modal -->
<div id="oracle-modal">
  <div id="oracle-box">
    <button id="oracle-close" onclick="closeOracle()">×</button>
    <div class="oracle-eyebrow" id="oracle-eyebrow">Откровение Архивариуса</div>
    <div class="oracle-title" id="oracle-title"></div>
    <div class="oracle-effect" id="oracle-effect"></div>
    <div class="oracle-body" id="oracle-body">
      <span class="oracle-loading">Архивариус читает знаки...</span>
    </div>
  </div>
</div>

<!-- Settings Modal -->
<div id="settings-modal">
  <div id="settings-box">
    <div class="settings-title">⚙ Настройки · Архивариус</div>
    <div id="settings-key-status" class="settings-status missing" style="margin-bottom:16px">Проверка...</div>

    <div class="settings-label">GigaChat (Сбер)</div>
    <input class="settings-input" id="settings-gc-key" type="password" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx">
    <div class="settings-hint">Авторизационный ключ из <b>developers.sber.ru</b> → GigaChat API</div>
    <div style="display:flex;gap:10px;margin-bottom:18px">
      <button class="pocket-btn" onclick="saveGigaChat()">Сохранить GigaChat</button>
    </div>

    <div class="settings-label">Anthropic Claude (опционально)</div>
    <input class="settings-input" id="settings-api-key" type="password" placeholder="sk-ant-...">
    <div class="settings-hint" style="margin-bottom:14px">console.anthropic.com → API Keys</div>
    <div style="display:flex;gap:10px">
      <button class="pocket-btn" onclick="saveApiKey()">Сохранить Anthropic</button>
      <button class="pocket-btn secondary" onclick="closeSettings()">Закрыть</button>
    </div>

    <div class="settings-label" style="margin-top:22px">Данные</div>
    <div class="settings-hint" style="margin-bottom:10px">Экспорт — скачать все данные одним файлом. Импорт — залить на другой сервер.</div>
    <div style="display:flex;gap:10px;align-items:center">
      <button class="pocket-btn" onclick="exportData()">⬇ Экспорт</button>
      <label class="pocket-btn" style="cursor:pointer;margin:0">⬆ Импорт
        <input type="file" accept=".json" style="display:none" onchange="importData(this)">
      </label>
      <span id="import-status" style="font-size:11px;font-family:sans-serif;color:var(--ink3)"></span>
    </div>
  </div>
</div>

<!-- Entity Modal -->
<div id="ent-modal">
  <div id="ent-box">
    <button id="ent-close" onclick="closeEnt()">✕</button>
    <div id="ent-content"></div>
  </div>
</div>


<!-- Dialogs -->
<div class="dlg" id="merge-dlg">
  <div class="dlg-box">
    <div class="dlg-title">Объединить сущности</div>
    <div style="font-size:12px;color:var(--ink3);font-family:sans-serif;margin-bottom:10px">Оставшаяся сущность поглотит все связи удалённой</div>
    <div style="font-size:13px;color:var(--ink2);margin-bottom:6px">Оставить: <strong id="merge-keep-label"></strong></div>
    <input class="dlg-input" id="merge-drop-input" placeholder="Удалить и влить в неё: введи имя сущности">
    <div id="merge-suggestions" style="display:flex;flex-wrap:wrap;gap:5px;margin-top:6px"></div>
    <div class="dlg-btns" style="margin-top:12px">
      <button class="btn-sm btn-ok" onclick="doMerge()">Объединить</button>
      <button class="btn-sm" onclick="closeDlg('merge-dlg')">Отмена</button>
    </div>
  </div>
</div>
<div class="dlg" id="entity-dlg">
  <div class="dlg-box">
    <div class="dlg-title" id="entity-dlg-title">Новая сущность</div>
    <input class="dlg-input" id="ent-name" placeholder="Название (Электросамокат, Камера...)">
    <select class="dlg-input" id="ent-type" style="font-family:'Georgia',serif">
      <option value="concept">💡 Концепция / идея</option>
      <option value="person">👤 Человек</option>
      <option value="place">📍 Место</option>
      <option value="project">📁 Проект</option>
      <option value="object">🔧 Предмет / инструмент</option>
      <option value="event">📅 Событие</option>
    </select>
    <textarea class="dlg-textarea" id="ent-summary" placeholder="Одно предложение — что это значит для Героя" rows="2"></textarea>
    <input class="dlg-input" id="ent-link-mid" type="hidden">
    <div class="dlg-btns">
      <button class="btn-sm btn-ok" onclick="saveEntity()">Создать</button>
      <button class="btn-sm" onclick="closeDlg('entity-dlg')">Отмена</button>
    </div>
  </div>
</div>
<div class="dlg" id="mission-dlg">
  <div class="dlg-box">
    <div class="dlg-title">Новый путь</div>
    <input class="dlg-input" id="m-title" placeholder="Куда ведёт этот путь?">
    <textarea class="dlg-textarea" id="m-desc" placeholder="Зачем ты идёшь этим путём?"></textarea>
    <div class="dlg-btns">
      <button class="btn-cancel" onclick="closeDlg('mission-dlg')">Отмена</button>
      <button class="btn-primary" onclick="saveMission()">Создать</button>
    </div>
  </div>
</div>

<div class="dlg" id="task-dlg">
  <div class="dlg-box">
    <div class="dlg-title">Добавить задание</div>
    <input class="dlg-input" id="t-title" placeholder="Название задания">
    <div style="display:flex;gap:16px;margin-bottom:12px;font-family:sans-serif;font-size:13px;color:var(--ink2)">
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer">
        <input type="radio" name="t-type" value="once" checked onchange="toggleRepeatOpts(this)"> Разовое
      </label>
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer">
        <input type="radio" name="t-type" value="repeat" onchange="toggleRepeatOpts(this)"> Повторяемое
      </label>
    </div>
    <div id="t-repeat-opts" style="display:none;gap:8px;margin-bottom:10px;flex-wrap:wrap">
      <div style="display:flex;align-items:center;gap:6px">
        <input class="dlg-input" id="t-iters" type="number" min="1" value="1"
          style="width:90px;margin-bottom:0" placeholder="Раз">
        <span style="font-size:12px;font-family:sans-serif;color:var(--ink3)">раз за цикл</span>
      </div>
      <div style="display:flex;align-items:center;gap:6px">
        <input class="dlg-input" id="t-hours" type="number" min="1" value="24"
          style="width:90px;margin-bottom:0" placeholder="Часов">
        <span style="font-size:12px;font-family:sans-serif;color:var(--ink3)">ч до сброса</span>
      </div>
    </div>
    <input type="hidden" id="t-mid">
    <div class="dlg-btns">
      <button class="btn-cancel" onclick="closeDlg('task-dlg')">Отмена</button>
      <button class="btn-primary" onclick="saveTask()">Добавить</button>
    </div>
  </div>
</div>


<script>
// ── Auth ─────────────────────────────────────────────────────────────────────
(function(){
  const _orig=window.fetch.bind(window);
  window.fetch=function(url,opts={}){
    const tok=localStorage.getItem('lrpg_token');
    if(tok&&typeof url==='string'&&!url.startsWith('http')){
      opts={...opts,headers:{...(opts.headers||{}),'Authorization':'Bearer '+tok}};
    }
    return _orig(url,opts);
  };
})();

async function authInit(){
  const tok=localStorage.getItem('lrpg_token');
  if(!tok){showLogin();return;}
  try{
    const r=await fetch('/me');
    if(r.ok){const u=await r.json();window._me=u;hideLogin();}
    else{localStorage.removeItem('lrpg_token');showLogin();}
  }catch{showLogin();}
}
let _lsMode='login';
function lsTab(mode){
  _lsMode=mode;
  const isReg=mode==='reg';
  document.getElementById('ls-pw2').style.display=isReg?'block':'none';
  document.getElementById('ls-btn').textContent=isReg?'Создать аккаунт':'Войти';
  document.getElementById('ls-tab-login').style.background=isReg?'transparent':'rgba(180,130,60,.8)';
  document.getElementById('ls-tab-login').style.color=isReg?'#7a6040':'#e8d5b0';
  document.getElementById('ls-tab-reg').style.background=isReg?'rgba(180,130,60,.8)':'transparent';
  document.getElementById('ls-tab-reg').style.color=isReg?'#e8d5b0':'#7a6040';
  document.getElementById('ls-err').textContent='';
}
function showLogin(){document.getElementById('login-screen').style.display='flex';lsTab('login');LoginAtmo.start();}
function hideLogin(){document.getElementById('login-screen').style.display='none';LoginAtmo.stop();}
function lsSubmit(){_lsMode==='reg'?doRegister():doLogin();}
async function doLogin(){
  const login=document.getElementById('ls-login').value.trim();
  const pw=document.getElementById('ls-pw').value;
  const err=document.getElementById('ls-err');
  err.textContent='';
  try{
    const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({login,password:pw})});
    if(r.ok){
      const d=await r.json();
      localStorage.setItem('lrpg_token',d.token);
      window._me=d; hideLogin();
      loadJournal();loadAsides();loadCharacter();
    } else { const d=await r.json(); err.textContent=d.detail||'Ошибка'; }
  }catch{err.textContent='Нет связи с сервером';}
}
async function doRegister(){
  const login=document.getElementById('ls-login').value.trim();
  const pw=document.getElementById('ls-pw').value;
  const pw2=document.getElementById('ls-pw2').value;
  const err=document.getElementById('ls-err');
  err.textContent='';
  if(pw!==pw2){err.textContent='Пароли не совпадают';return;}
  try{
    const r=await fetch('/register',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({login,password:pw})});
    if(r.ok){
      const d=await r.json();
      localStorage.setItem('lrpg_token',d.token);
      window._me=d; hideLogin();
      loadJournal();loadAsides();loadCharacter();
    } else { const d=await r.json(); err.textContent=d.detail||'Ошибка'; }
  }catch{err.textContent='Нет связи с сервером';}
}
function doLogout(){
  localStorage.removeItem('lrpg_token');
  document.getElementById('ls-login').value='';
  document.getElementById('ls-pw').value='';
  document.getElementById('ls-pw2').value='';
  showLogin();
}
// ── State ────────────────────────────────────────────────────────────────────
let allEntities = [];
let _openMissions = new Set();   // expanded mission blocks
let _closedMissions = new Set(); // manually collapsed by user

const ICONS = {person:'👤',place:'📍',project:'📁',concept:'💡',event:'📅'};
const TYPE_COLORS = {
  person:'var(--blue)', place:'#6b4a22', project:'var(--green)',
  concept:'#7b4fa0', event:'var(--gold)', quest:'var(--red)'
};

// ── Live astro (wttr.in) ─────────────────────────────────────────────────────
let _liveAstro = null;
const PHASE_EN_RU = {
  'New Moon':        {ru:'Новолуние',          emoji:'🌑'},
  'Waxing Crescent': {ru:'Молодой месяц',      emoji:'🌒'},
  'First Quarter':   {ru:'Первая четверть',    emoji:'🌓'},
  'Waxing Gibbous':  {ru:'Прибывающая луна',   emoji:'🌔'},
  'Full Moon':       {ru:'Полнолуние',          emoji:'🌕'},
  'Waning Gibbous':  {ru:'Убывающая луна',     emoji:'🌖'},
  'Last Quarter':    {ru:'Последняя четверть', emoji:'🌗'},
  'Waning Crescent': {ru:'Старый месяц',       emoji:'🌘'},
};

async function fetchAstroData() {
  try {
    const r = await fetch('https://wttr.in/?format=j1',{headers:{'Accept':'application/json'}});
    const d = await r.json();
    const a = d.weather[0].astronomy[0];
    _liveAstro = {phase_en:a.moon_phase, illumination:parseInt(a.moon_illumination),
                  moonrise:a.moonrise, moonset:a.moonset,
                  sunrise:a.sunrise, sunset:a.sunset, source:'wttr.in'};
    tickClock();
    if(document.querySelector('#s-journal.active')) loadJournal();
    return;
  } catch(e) {}
  try {
    const r=await fetch('/moonphase'); const d=await r.json();
    if(d.phase_en){ _liveAstro=d; tickClock(); }
  } catch(e) {}
}
fetchAstroData(); setInterval(fetchAstroData, 6*60*60*1000);

// ── КОЛЕСО МИРОВ ─────────────────────────────────────────────────────────────
const WoW = {
  EPOCH_START:2026, EPOCH_START_MONTH:5, EPOCH_START_DAY:4, EPOCH_NAME:'Эпоха Пепла',
  SEASONS:[
    {name:'Пробуждение',element:'Воздух',emoji:'🌬️',moons:['Луна Первого Ветра','Луна Семян','Луна Голоса']},
    {name:'Зной',element:'Огонь',emoji:'🔥',moons:['Луна Меча','Луна Пепла','Луна Жажды']},
    {name:'Жатва',element:'Земля',emoji:'🌾',moons:['Луна Весов','Луна Золота','Луна Договора']},
    {name:'Угасание',element:'Эфир',emoji:'🌫️',moons:['Луна Завесы','Луна Теней','Луна Памяти']},
    {name:'Мороз',element:'Вода',emoji:'❄️',moons:['Луна Прилива','Луна Молчания','Луна Возврата']},
  ],
  PHASES:[
    {name:'Новолуние',emoji:'🌑',lo:0.000,hi:0.033,effect:'Мёртвая вода. Тайны открываются.'},
    {name:'Молодой месяц',emoji:'🌒',lo:0.033,hi:0.240,effect:'Малый прилив. Торговые суда выходят.'},
    {name:'Первая четверть',emoji:'🌓',lo:0.240,hi:0.285,effect:'Равновесие сил. День решений.'},
    {name:'Прибывающая луна',emoji:'🌔',lo:0.285,hi:0.465,effect:'Прилив нарастает. Хорошее время.'},
    {name:'Полнолуние',emoji:'🌕',lo:0.465,hi:0.535,effect:'Большой прилив. Ритуалы. Чудовища у берегов.'},
    {name:'Убывающая луна',emoji:'🌖',lo:0.535,hi:0.715,effect:'Отлив начинается. Артефакты на дне.'},
    {name:'Последняя четверть',emoji:'🌗',lo:0.715,hi:0.760,effect:'День переосмысления.'},
    {name:'Старый месяц',emoji:'🌘',lo:0.760,hi:1.000,effect:'Духи говорят в тишине.'},
  ],
  PATRONS:[
    {name:'Феникс',emoji:'🦅',months:[3,4]},
    {name:'Страж',emoji:'⚔️',months:[5,6]},
    {name:'Дракон',emoji:'🐉',months:[7,8]},
    {name:'Весы',emoji:'⚖️',months:[9,10]},
    {name:'Левиафан',emoji:'🐋',months:[11]},
    {name:'Пустота',emoji:'🌌',months:[12,1]},
    {name:'Сеть',emoji:'🕸️',months:[2]},
  ],
  _jd(y,m,d){
    let Y=y,M=m; if(M<=2){Y--;M+=12;}
    const A=Math.floor(Y/100),B=2-A+Math.floor(A/4);
    return Math.floor(365.25*(Y+4716))+Math.floor(30.6001*(M+1))+d+B-1524.5;
  },
  _moonPhase(y,m,d){
    const JD=this._jd(y,m,d+0.5),REF=2451550.1,SYN=29.530588853;
    return (((JD-REF)/SYN%1)+1)%1;
  },
  _illumination(p){return Math.round((1-Math.cos(2*Math.PI*p))/2*100);},
  _season(y,doy){
    const lp=((y%4===0&&y%100!==0)||(y%400===0))?1:0;
    const sp=79+lp,su=172+lp,au=266+lp,wi=355+lp;
    if(doy>=sp&&doy<su)return{real:'spring',prog:(doy-sp)/(su-sp)};
    if(doy>=su&&doy<au)return{real:'summer',prog:(doy-su)/(au-su)};
    if(doy>=au&&doy<wi)return{real:'autumn',prog:(doy-au)/(wi-au)};
    return{real:'winter',prog:doy>=wi?(doy-wi)/(sp+365-wi):(doy+(365+lp-wi))/(sp+365-wi)};
  },
  _patron(m,d){
    const z=(m==3&&d>=21)||(m==4&&d<=19)?3:(m==4&&d>=20)||(m==5&&d<=20)?4:
            (m==5&&d>=21)||(m==6&&d<=20)?5:(m==6&&d>=21)||(m==7&&d<=22)?6:
            (m==7&&d>=23)||(m==8&&d<=22)?7:(m==8&&d>=23)||(m==9&&d<=22)?8:
            (m==9&&d>=23)||(m==10&&d<=22)?9:(m==10&&d>=23)||(m==11&&d<=21)?10:
            (m==11&&d>=22)||(m==12&&d<=21)?11:(m==12&&d>=22)||(m==1&&d<=19)?12:2;
    return this.PATRONS.find(p=>p.months.includes(z))||this.PATRONS[0];
  },
  convert(dateStr){
    const parts=dateStr.split(' ');
    const [y,m,d]=parts[0].split('-').map(Number);
    const phase=this._moonPhase(y,m,d);
    const illum=this._illumination(phase);
    const dSinceNew=phase*29.530588853;
    const dayInMoon=Math.floor(dSinceNew)+1;
    const phaseObj=this.PHASES.find(p=>phase>=p.lo&&phase<p.hi)||this.PHASES[7];
    const jan1=new Date(y,0,1);
    const doy=Math.round((new Date(y,m-1,d)-jan1)/86400000)+1;
    // Сезоны по месяцам: Пробуждение=3-4, Зной=5-6, Жатва=7-8, Угасание=9-10, Мороз=11-2
    const sIdxByMonth=[4,4,0,0,1,1,2,2,3,3,4,4]; // jan=0..dec=11
    const sIdx=sIdxByMonth[m-1];
    const season=this.SEASONS[sIdx];
    // Луна внутри сезона по дню месяца
    const mIdx=d<=10?0:d<=20?1:2;
    const moon=season.moons[mIdx];
    const patron=this._patron(m,d);
    const epochStart=new Date(this.EPOCH_START,this.EPOCH_START_MONTH-1,this.EPOCH_START_DAY);
    const nowDate=new Date(y,m-1,d);
    const epochYear=Math.floor((nowDate-epochStart)/(365.25*24*3600*1000))+1;
    const todayStr=(()=>{const n=new Date();return `${n.getFullYear()}-${String(n.getMonth()+1).padStart(2,'0')}-${String(n.getDate()).padStart(2,'0')}`;})();
    const isToday=parts[0]===todayStr;
    let phaseName,phaseEmoji,phaseEffect,finalIllum;
    if(isToday&&_liveAstro&&_liveAstro.phase_en){
      const p=PHASE_EN_RU[_liveAstro.phase_en]||{ru:_liveAstro.phase_en,emoji:'🌙'};
      phaseName=p.ru; phaseEmoji=p.emoji;
      phaseEffect=WoW.PHASES.find(x=>x.name===p.ru)?.effect||'';
      finalIllum=_liveAstro.illumination;
    } else {
      phaseName=phaseObj.name; phaseEmoji=phaseObj.emoji;
      phaseEffect=phaseObj.effect; finalIllum=illum;
    }
    const liveInfo=(isToday&&_liveAstro)?_liveAstro:null;
    const sunStr=liveInfo?`☀️ ${liveInfo.sunrise} → ${liveInfo.sunset}`:'';
    return {
      epochYear,epochName:this.EPOCH_NAME,
      season:season.name,element:season.element,seasonEmoji:season.emoji,
      moon,moonIdx:mIdx,phase:phaseName,phaseEmoji,phaseEffect,
      phaseVal:phase,illumination:finalIllum,dayInMoon,
      patron:patron.name,patronEmoji:patron.emoji,
      isLive:isToday&&!!_liveAstro,
      sunrise:liveInfo?.sunrise,sunset:liveInfo?.sunset,
      moonrise:liveInfo?.moonrise,moonset:liveInfo?.moonset,
      heading:moon.toUpperCase()+` · ДЕНЬ ${dayInMoon}`,
      sub:`${season.name} ${season.emoji} · ${phaseEmoji} ${phaseName} · ${finalIllum}%`+
          ` · ${patron.emoji} ${patron.name} · ${epochYear}-й год ${this.EPOCH_NAME}`+
          (sunStr?` · ${sunStr}`:''),
      short:`${moon} · ${phaseEmoji} ${finalIllum}% · День ${dayInMoon}`,
    };
  },
  now(){const n=new Date();return this.convert(`${n.getFullYear()}-${String(n.getMonth()+1).padStart(2,'0')}-${String(n.getDate()).padStart(2,'0')}`);}
};

// ── Clock ────────────────────────────────────────────────────────────────────
function _oc(type,value,effect,label){
  return `onclick="openOracle('${type}','${value.replace(/'/g,"\\'")}','${(effect||'').replace(/'/g,"\\'")}','${(label||value).replace(/'/g,"\\'")}')"`;}
function tickClock(){
  const cal=WoW.now();
  const t=new Date().toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});
  const hdrDate=document.getElementById('hdr-date');
  if(hdrDate) hdrDate.innerHTML=
    `<span class="cal-clickable" ${_oc('moon',cal.phase,cal.phaseEffect,cal.phaseEmoji+' '+cal.phase)} style="color:var(--red)">${cal.phaseEmoji} ${cal.phase}</span>`+
    `<span style="color:var(--ink3)"> · ${t}</span>`;
  const calEl=document.getElementById('sidebar-cal');
  if(calEl) calEl.innerHTML=
    `<div class="cal-season"><span class="cal-clickable" ${_oc('season',cal.season,'Сезон: '+cal.element,cal.seasonEmoji+' '+cal.season)}>${cal.seasonEmoji} ${cal.season}</span></div>`+
    `<div class="cal-moon"><span class="cal-clickable" ${_oc('moon',cal.phase,cal.phaseEffect,cal.phaseEmoji+' '+cal.phase)}>${cal.phaseEmoji} ${cal.phase} · ${cal.illumination}%</span></div>`+
    `<div class="cal-patron"><span class="cal-clickable" ${_oc('patron',cal.patron,'',cal.patronEmoji+' '+cal.patron)}>${cal.patronEmoji} ${cal.patron}</span></div>`+
    `<div class="cal-year">${cal.epochYear}-й год · ${cal.epochName}</div>`+
    `<div class="cal-time">${t}</div>`+
    (cal.phaseEffect?`<div class="cal-effect"><span class="cal-clickable" ${_oc('moon',cal.phase,cal.phaseEffect,cal.phaseEmoji+' '+cal.phase)}>${cal.phaseEffect}</span></div>`:'')+
    (cal.moon?`<div class="cal-sun" style="margin-top:4px"><span class="cal-clickable" ${_oc('moon_name',cal.moon,'',cal.moon)}>${cal.moon}</span></div>`:'')+
    (cal.sunrise?`<div class="cal-sun">☀️ ${cal.sunrise} — ${cal.sunset}</div>`:'');
}
tickClock(); setInterval(tickClock,30000);

// ── Oracle ───────────────────────────────────────────────────────────────────
function openOracle(type,value,effect,label){
  const m=document.getElementById('oracle-modal');
  document.getElementById('oracle-title').textContent=label||value;
  document.getElementById('oracle-effect').textContent=effect||'';
  document.getElementById('oracle-body').innerHTML='<span class="oracle-loading">Архивариус читает знаки...</span>';
  m.classList.add('open');
  fetch('/oracle',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mechanic_type:type,mechanic_value:value,mechanic_effect:effect||''})})
    .then(r=>r.json()).then(d=>{
      const body=document.getElementById('oracle-body');
      if(d.text) body.textContent=d.text;
      else body.innerHTML='<span style="color:var(--ink3);font-family:sans-serif;font-size:13px">Архивариус молчит — настройте ИИ в ⚙ Настройках.</span>';
    }).catch(()=>{
      document.getElementById('oracle-body').innerHTML='<span style="color:var(--red);font-size:12px">Связь с Архивариусом прервана.</span>';
    });
}
function closeOracle(){document.getElementById('oracle-modal').classList.remove('open');}
function oracleClick(type,value,effect){openOracle(type,value,effect||'',value);}
document.getElementById('oracle-modal').addEventListener('click',e=>{if(e.target.id==='oracle-modal')closeOracle();});

// ── Mechanics section in База знаний ─────────────────────────────────────────
function loadMechanics(){
  const cal=WoW.now();
  const sections=[
    {
      title:'Фазы Луны',
      hint:'Луна меняется каждые ~3.5 дня. Каждая фаза задаёт тон времени.',
      items:WoW.PHASES.map(p=>({
        emoji:p.emoji,name:p.name,sub:'',effect:p.effect,
        current:cal.phase===p.name,
        type:'moon',value:p.name,label:p.emoji+' '+p.name
      }))
    },
    {
      title:'Сезоны Мира',
      hint:'Пять сезонов сменяют друг друга. Каждый — своё настроение и стихия.',
      items:WoW.SEASONS.map(s=>({
        emoji:s.emoji,name:s.name,sub:s.element,
        effect:'Луны: '+s.moons.join(' · '),
        current:cal.season===s.name,
        type:'season',value:s.name,label:s.emoji+' '+s.name
      }))
    },
    {
      title:'Покровители',
      hint:'Покровитель меняется каждые ~1.5 месяца. Определяет архетип периода.',
      items:WoW.PATRONS.map(p=>({
        emoji:p.emoji,name:p.name,sub:'месяцы '+p.months.join(', '),effect:'',
        current:cal.patron===p.name,
        type:'patron',value:p.name,label:p.emoji+' '+p.name
      }))
    }
  ];
  return `<div class="mech-section">
    <div class="mech-section-hdr">
      <div class="mech-section-title">⚙ Механики Мира</div>
    </div>
    ${sections.map(s=>`
      <div style="margin-bottom:32px">
        <div style="font-size:13px;color:var(--ink2);margin-bottom:4px;font-family:'Georgia',serif">${s.title}</div>
        <div style="font-size:11px;color:var(--ink3);font-family:sans-serif;margin-bottom:12px">${s.hint}</div>
        <div class="mech-grid">
          ${s.items.map(it=>`
            <div class="mcard${it.current?' current':''}" onclick="openOracle('${it.type}','${it.value.replace(/'/g,"\\'")}','${(it.effect||'').replace(/'/g,"\\'")}','${it.label.replace(/'/g,"\\'")}')">
              <div class="mcard-emoji">${it.emoji}</div>
              <div class="mcard-name">${it.name}</div>
              ${it.sub?`<div class="mcard-sub">${it.sub}</div>`:''}
              ${it.effect?`<div class="mcard-effect">${it.effect}</div>`:''}
              <div class="mcard-hint">нажми → откровение Архивариуса</div>
            </div>`).join('')}
        </div>
      </div>`).join('')}
  </div>`;
}

// ── Ambient Sound Engine ─────────────────────────────────────────────────────
const Amb={
  ctx:null,_nodes:[],_mg:null,_on:false,_season:null,
  _init(){
    if(this.ctx)return;
    this.ctx=new(window.AudioContext||window.webkitAudioContext)();
    this._mg=this.ctx.createGain(); this._mg.gain.value=0.45;
    this._mg.connect(this.ctx.destination);
  },
  _buf(sec=4){
    const b=this.ctx.createBuffer(1,this.ctx.sampleRate*sec,this.ctx.sampleRate);
    const d=b.getChannelData(0); for(let i=0;i<d.length;i++)d[i]=Math.random()*2-1;
    const s=this.ctx.createBufferSource(); s.buffer=b; s.loop=true; return s;
  },
  _bq(type,freq,Q=1){const f=this.ctx.createBiquadFilter();f.type=type;f.frequency.value=freq;f.Q.value=Q;return f;},
  _g(v){const g=this.ctx.createGain();g.gain.value=v;return g;},
  _osc(freq,type='sine'){const o=this.ctx.createOscillator();o.type=type;o.frequency.value=freq;return o;},
  _lfo(freq,depth,target){const l=this._osc(freq),g=this._g(depth);l.connect(g);g.connect(target);return l;},
  _add(...n){this._nodes.push(...n);},
  _start(...n){n.forEach(x=>x.start());this._add(...n);},
  stop(){this._nodes.forEach(n=>{try{n.stop?.();n.disconnect();}catch(e){}});this._nodes=[];},
  _wire(src,dst){src.connect(dst);return src;},

  play(season){
    this._init();
    if(this.ctx.state==='suspended')this.ctx.resume();
    this.stop(); this._season=season;
    if(!this._on)return;
    const fn=this['_'+season]; if(fn)fn.call(this);
  },

  /* ПРОБУЖДЕНИЕ — spring wind + bright air */
  _Пробуждение(){
    const w=this._buf(4),lp=this._bq('lowpass',450,.4),g=this._g(.05);
    const lfo=this._lfo(.07,.03,g.gain);
    w.connect(lp);lp.connect(g);g.connect(this._mg);
    const b=this._buf(2),bp=this._bq('bandpass',2800,2.5),g2=this._g(.006);
    const lfo2=this._lfo(.14,.005,g2.gain);
    b.connect(bp);bp.connect(g2);g2.connect(this._mg);
    this._start(w,lfo,b,lfo2);
  },

  /* ЗНОЙ — crickets (AM noise) + heat haze */
  _Зной(){
    const n=this._buf(2),bp=this._bq('bandpass',5800,10),g=this._g(.0);
    const am=this._lfo(15,.025,g.gain);
    const sw=this._lfo(.04,.015,g.gain);
    n.connect(bp);bp.connect(g);g.connect(this._mg);
    const w=this._buf(4),lp=this._bq('lowpass',280,.3),gw=this._g(.018);
    const lwfo=this._lfo(.05,.015,gw.gain);
    w.connect(lp);lp.connect(gw);gw.connect(this._mg);
    this._start(n,am,sw,w,lwfo);
  },

  /* ЖАТВА — deeper wind + rustle */
  _Жатва(){
    const w=this._buf(4),lp=this._bq('lowpass',320,.5),g=this._g(.055);
    const lfo=this._lfo(.045,.028,g.gain);
    w.connect(lp);lp.connect(g);g.connect(this._mg);
    const r=this._buf(1),bp=this._bq('bandpass',1800,3),g2=this._g(.012);
    const lfo2=this._lfo(.22,.011,g2.gain);
    r.connect(bp);bp.connect(g2);g2.connect(this._mg);
    this._start(w,lfo,r,lfo2);
  },

  /* УГАСАНИЕ — haunting drone + slow wind */
  _Угасание(){
    const dr=this._osc(55,'triangle'),g=this._g(.038);
    const lfo=this._lfo(.025,.02,g.gain);
    dr.connect(g);g.connect(this._mg);
    const w=this._buf(4),lp=this._bq('lowpass',250,.4),gw=this._g(.042);
    const lwfo=this._lfo(.055,.03,gw.gain);
    w.connect(lp);lp.connect(gw);gw.connect(this._mg);
    this._start(dr,lfo,w,lwfo);
  },

  /* МОРОЗ — deep cold wind + sub bass pulse */
  _Мороз(){
    const w=this._buf(8),lp=this._bq('lowpass',180,.3),g=this._g(.032);
    const lfo=this._lfo(.022,.028,g.gain);
    w.connect(lp);lp.connect(g);g.connect(this._mg);
    const sub=this._osc(38,'sine'),sg=this._g(.022);
    const slfo=this._lfo(.035,.018,sg.gain);
    sub.connect(sg);sg.connect(this._mg);
    this._start(w,lfo,sub,slfo);
  }
};

function toggleSound(){
  const btn=document.getElementById('sound-btn');
  Amb._on=!Amb._on;
  if(Amb._on){
    Amb.play(WoW.now().season);
    btn.textContent='♪ амбиент'; btn.classList.add('on');
  } else {
    Amb.stop();
    btn.textContent='♪ амбиент'; btn.classList.remove('on');
  }
}

// ── Streak Mythology ──────────────────────────────────────────────────────────
function streakMythName(n){
  if(!n||n<3)return n>0?`🔥 ${n}`:'';
  const tiers=[[100,'Бессмертия'],[60,'Легенды'],[30,'Вечного Пламени'],[14,'Пылающей Цепи'],[7,'Непрерывного Огня'],[3,'Зарождения']];
  const tier=tiers.find(t=>n>=t[0]);
  return `🔥 ${n}-й день ${tier?tier[1]:'Начала'}`;
}

// ── Visual Aging ──────────────────────────────────────────────────────────────
function entryAgeStyle(tsStr){
  const days=(Date.now()-new Date(tsStr.replace(' ','T')).getTime())/86400000;
  if(days<3) return '';
  if(days<8) return 'opacity:.92;filter:sepia(12%)';
  if(days<30) return 'opacity:.85;filter:sepia(28%)';
  if(days<90) return 'opacity:.78;filter:sepia(45%)';
  return 'opacity:.70;filter:sepia(62%)';
}

// ── Character Stats ───────────────────────────────────────────────────────────
const STATS_META=[
  {id:'will',    name:'Воля',       sub:'Ты движешь судьбу — или она тебя?'},
  {id:'temper',  name:'Закалка',    sub:'Металл, из которого куют легенды'},
  {id:'flame',   name:'Пламя',      sub:'Зачем ты идёшь — ты знаешь?'},
  {id:'mastery', name:'Мастерство', sub:'Острота, что точится через действие'},
  {id:'threads', name:'Нити',       sub:'Связи, которые держат мир'},
  {id:'shadow',  name:'Тень',       sub:'То, что идёт рядом и просит имени'},
];
function _statColor(v){
  if(v>=80) return 'var(--green)';
  if(v>=55) return 'var(--gold)';
  if(v>=30) return 'var(--ink3)';
  return 'var(--red)';
}
async function loadCharacter(){
  const d=await(await fetch('/character/data')).json();
  const el=document.getElementById('char-sidebar');
  if(!el) return;
  const stats=d.stats||{};
  const hasStats=STATS_META.some(s=>stats[s.id]!=null);
  el.innerHTML=`<div class="aside-label" style="margin-bottom:8px">Статы Героя</div>`+
    STATS_META.map(s=>{
      const v=stats[s.id];
      const pct=v!=null?Math.min(v,100):null;
      const color=pct!=null?_statColor(pct):'var(--border2)';
      return `<div class="stat-row" onclick="openOracle('stat','${s.id}','${s.sub.replace(/'/g,"\\'")}','${s.name}')" title="${s.sub}">
        <div class="stat-name">${s.name}</div>
        <div class="stat-bar-wrap"><div class="stat-bar" style="width:${pct??0}%;background:${color}" data-target="${pct??0}"></div></div>
        <div class="stat-val" style="color:${color}">${pct!=null?pct:'—'}</div>
      </div>`;
    }).join('')+
    `<span class="char-analyze-btn" onclick="triggerAnalyze()">⟳ ${d.last_analyzed?'обновить · '+d.last_analyzed:'Архивариус изучает характер...'}</span>`;
  // Animate bars after render
  requestAnimationFrame(()=>requestAnimationFrame(()=>{
    el.querySelectorAll('.stat-bar').forEach(b=>{
      b.style.width=b.dataset.target+'%';
    });
  }));
}
async function triggerAnalyze(){
  const el=document.getElementById('char-sidebar');
  const btn=el?.querySelector('.char-analyze-btn');
  if(btn) btn.textContent='⟳ Архивариус анализирует...';
  await fetch('/character/analyze',{method:'POST'});
  setTimeout(()=>loadCharacter(),14000);
}

// ── Nav ──────────────────────────────────────────────────────────────────────
const TITLES={journal:'Дневник',missions:'Пути',base:'База знаний',pocket:'Карман'};
function nav(el){
  document.querySelectorAll('.nav-item').forEach(i=>i.classList.remove('active'));
  el.classList.add('active');
  const s=el.dataset.s;
  document.querySelectorAll('section').forEach(i=>i.classList.remove('active'));
  document.getElementById('s-'+s).classList.add('active');
  // sync bottom nav
  document.querySelectorAll('.bnav-item').forEach(i=>{
    i.classList.toggle('active', i.dataset.s===s);
  });
  // input bar: only on journal (mobile)
  const _ib=document.getElementById('input-bar');
  if(window.innerWidth<=768) _ib.classList.toggle('mob-visible', s==='journal');
  if(s==='journal'){loadJournal();loadAsides();}
  if(s==='missions') loadMissions();
  if(s==='base') loadBase();
  if(s==='pocket') loadPocket();
}

function navMob(el){
  // Navigate via bottom nav; sync topbar nav-item
  const s=el.dataset.s;
  const topEl=document.querySelector(`.nav-item[data-s="${s}"]`);
  if(topEl) nav(topEl); else{
    document.querySelectorAll('.bnav-item').forEach(i=>i.classList.remove('active'));
    el.classList.add('active');
    document.querySelectorAll('section').forEach(i=>i.classList.remove('active'));
    document.getElementById('s-'+s).classList.add('active');
    if(s==='journal'){loadJournal();loadAsides();}
    if(s==='missions') loadMissions();
    if(s==='base') loadBase();
    if(s==='pocket') loadPocket();
  }
}

function toggleDrawer(){
  const aside=document.getElementById('sidebar');
  const bd=document.getElementById('drawer-backdrop');
  const open=aside.classList.toggle('drawer-open');
  bd.classList.toggle('visible',open);
}
function closeDrawer(){
  document.getElementById('sidebar').classList.remove('drawer-open');
  document.getElementById('drawer-backdrop').classList.remove('visible');
}

// ── Linkify ──────────────────────────────────────────────────────────────────
function linkify(text){
  if(!allEntities.length) return text;
  const sorted=[...allEntities].sort((a,b)=>b.name.length-a.name.length);
  let out=text;
  for(const ent of sorted){
    const esc=ent.name.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
    const re=new RegExp(`(?<![a-zA-Zа-яёА-ЯЁ])(${esc})(?![a-zA-Zа-яёА-ЯЁ])`,'g');
    const cls='ent-link type-'+(ent.type||'concept');
    out=out.replace(re,`<span class="${cls}" onclick="openEnt('${ent.name.replace(/'/g,"\\'")}')">$1</span>`);
  }
  return out;
}

// ── Journal ──────────────────────────────────────────────────────────────────
async function loadJournal(){
  const [dr,er,ir,doneR,mr,narR,pmR]=await Promise.all([fetch('/diary'),fetch('/entities'),fetch('/inbox'),fetch('/tasks/completed-today'),fetch('/missions'),fetch('/today-narrative'),fetch('/chronicle/past-moon')]);
  const diary=await dr.json(); allEntities=await er.json();
  const inboxRaw=await ir.json();
  const doneToday=(await doneR.json()).count||0;
  const missions=await mr.json();
  const todayNarrative=(await narR.json()).narrative||'';
  const pastMoon=(await pmR.json()).entry||null;
  const today=new Date().toISOString().slice(0,10);
  const todayTasks=missions.flatMap(m=>m.tasks.filter(t=>
    (t.task_type==='repeat'&&t.current_iters>0)
  ));
  // Only entries actually in the inbox queue are truly "pending"
  const pendingIds=new Set(inboxRaw.filter(i=>!i.type||i.type==='entry').map(i=>i.id));
  const el=document.getElementById('journal-main');
  if(!diary.length){
    el.innerHTML='<div class="empty">Дневник пуст. Напиши первую запись внизу ↓</div>';
    return;
  }
  const byDate={};
  for(const e of diary){
    const d=e.ts.split(' ')[0];
    if(!byDate[d])byDate[d]=[];
    byDate[d].push(e);
  }
  const calNow=WoW.now();
  const pastMoonHtml=pastMoon?`<div class="past-moon-block">
    <div class="past-moon-label">${calNow.phaseEmoji} В прошлую ${calNow.phase}</div>
    <div class="past-moon-text">${pastMoon.narrative?.slice(0,200)}${pastMoon.narrative?.length>200?'…':''}</div>
    <div class="past-moon-ts">${pastMoon.ts?.split(' ')[0]||''}</div>
  </div>`:'';
  el.innerHTML=pastMoonHtml+Object.entries(byDate).map(([date,entries])=>{
    const cal=WoW.convert(date);
    const items=entries.map(e=>{
      const timeStr=e.ts.includes(' ')?e.ts.split(' ')[1]:'';
      const isPending = pendingIds.has(e.id);
      const ageStyle=entryAgeStyle(e.ts);
      const archivistHtml=e.archivist_note?
        `<div class="entry-archivist">◆ ${e.archivist_note}</div>`:'';
      const pendingHtml=isPending?
        `<div style="font-size:11px;color:var(--border);font-family:sans-serif;font-style:italic;margin-top:8px">
          ⏳ Архивариус обрабатывает запись...
        </div>`:'';
      return `<div class="entry" style="${ageStyle}">
        <div class="entry-text">${linkify(e.narrative)}</div>
        ${!isPending&&e.raw&&e.raw!==e.narrative?`<div class="entry-raw">«${e.raw}»</div>`:''}
        ${archivistHtml}
        ${pendingHtml}
        ${timeStr?`<div class="entry-time">${timeStr} · ${cal.phaseEmoji} ${cal.phase}</div>`:''}
      </div>`;
    }).join('');
    const isToday=date===new Date().toISOString().slice(0,10);
    const doneBadge=isToday&&doneToday>0?`<span class="daily-done-badge">✓ ${doneToday} заданий сегодня</span>`:'';
    const progressBlock=isToday&&todayTasks.length?`<div style="margin:10px 0 16px;padding:14px 16px;background:var(--paper2);border:1px solid var(--border2);border-radius:3px">
      <div style="font-size:9px;letter-spacing:2px;color:var(--ink3);font-family:sans-serif;margin-bottom:10px">ХРОНИКИ ДНЯ</div>
      ${todayNarrative?`<div style="font-size:13px;color:var(--ink);font-style:italic;margin-bottom:12px;line-height:1.6">${todayNarrative}</div>`:''}
      ${todayTasks.map(t=>{
        const pct=Math.round(t.current_iters/Math.max(t.required_iters,1)*100);
        return `<div style="margin-bottom:6px">
          <div style="display:flex;justify-content:space-between;font-size:12px;font-family:sans-serif;color:var(--ink2);margin-bottom:3px">
            <span>${t.title}</span><span style="color:var(--gold)">${t.current_iters}/${t.required_iters}</span>
          </div>
          <div style="height:3px;background:var(--border2);border-radius:2px">
            <div style="height:3px;background:var(--gold);border-radius:2px;width:${Math.min(pct,100)}%"></div>
          </div>
        </div>`;
      }).join('')}
    </div>`:'';
    return `<div class="day-block">
      <div class="day-heading">${cal.heading}${doneBadge}</div>
      <div class="day-sub">${cal.sub}</div>
      ${progressBlock}
      ${items}
    </div>`;
  }).join('');
}

function _taskUrgency(t){
  if(t.task_type!=='repeat'||!t.last_reset_ts) return Infinity;
  const due=new Date(t.last_reset_ts.replace(' ','T')).getTime()+(t.reset_hours||24)*3600000;
  return due-Date.now();
}
async function loadAsides(){
  const mr=await fetch('/missions');
  const missions=await mr.json();
  const active=missions.filter(m=>m.status==='active');
  document.getElementById('aside-missions').innerHTML=active.length
    ?active.map(m=>{
      const incompleteTasks=m.tasks.filter(t=>t.status!=='done')
        .sort((a,b)=>_taskUrgency(a)-_taskUrgency(b));
      const tasksHtml=incompleteTasks.map(t=>{
        const isRepeat=t.task_type==='repeat';
        const prog=isRepeat?`${t.current_iters}/${t.required_iters}`:'';
        const timeLeft=isRepeat&&t.last_reset_ts?fmtCountdown(t.last_reset_ts,t.reset_hours):'';
        return `<div style="padding:4px 8px 4px 14px;font-size:12px;font-family:sans-serif;color:var(--ink2);border-left:2px solid var(--border2);margin:3px 0">
          <span>${t.title}</span>
          ${prog?`<span style="color:var(--gold);margin-left:6px">${prog}</span>`:''}
          ${timeLeft?`<span style="color:var(--ink3);font-size:10px;margin-left:4px">${timeLeft}</span>`:''}
        </div>`;
      }).join('');
      return `<div class="aside-mission" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='none'?'block':'none'">
        <div class="aside-mission-t">⚔️ ${m.title}</div>
      </div>
      <div style="display:none">${tasksHtml||'<div style="padding:4px 14px;font-size:11px;color:var(--ink3);font-family:sans-serif">нет заданий</div>'}</div>`;
    }).join('')
    :'<div style="font-size:12px;color:var(--ink3);font-family:sans-serif">нет</div>';
}

// ── Ingest ───────────────────────────────────────────────────────────────────
async function sendEntry(){
  const txt=document.getElementById('txt'),
        btn=document.getElementById('send-btn');
  if(!txt.value.trim()) return;
  btn.disabled=true; btn.textContent='⏳...';
  try {
    const r=await fetch('/ingest',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text:txt.value})});
    const d=await r.json();
    txt.value='';
    const active=document.querySelector('section.active');
    if(active?.id==='s-journal'){loadJournal();loadAsides();}
    if(active?.id==='s-missions') loadMissions();
  } catch(e){ console.error('ingest error',e); }
  finally{ btn.disabled=false; btn.textContent='Записать →'; }
}
document.getElementById('txt').addEventListener('keydown',e=>{
  if((e.ctrlKey||e.metaKey)&&e.key==='Enter') sendEntry();
});

// ── Missions ─────────────────────────────────────────────────────────────────
function toggleMission(mid){
  const chain=document.getElementById('qchain-'+mid);
  const chev=document.getElementById('chev-'+mid);
  if(!chain) return;
  const isOpen=chain.classList.contains('open');
  if(isOpen){
    chain.classList.remove('open'); chev.classList.remove('open');
    _openMissions.delete(mid); _closedMissions.add(mid);
  } else {
    chain.classList.add('open'); chev.classList.add('open');
    _openMissions.add(mid); _closedMissions.delete(mid);
  }
}

async function loadMissions(){
  const [r,cd]=await Promise.all([fetch('/missions'),fetch('/character/data')]);
  const ms=await r.json(); const charData=await cd.json();
  const epilogues=charData.mission_epilogues||{};
  const el=document.getElementById('missions-list');
  if(!ms.length){
    el.innerHTML='<div class="empty">Нет путей. Добавь первый путь ↗</div>';
    return;
  }
  ms.forEach(m=>{ if(!_closedMissions.has(m.id)) _openMissions.add(m.id); });
  el.innerHTML=ms.map(m=>{
    const onceDone=m.tasks.filter(t=>t.task_type!=='repeat'&&t.status==='done').length;
    const onceTotal=m.tasks.filter(t=>t.task_type!=='repeat').length;
    const repeatTotal=m.tasks.filter(t=>t.task_type==='repeat').length;
    const totalCount=m.tasks.length;
    const wasOpen=_openMissions.has(m.id);

    const epilogueHtml=epilogues[m.id]?`<div class="mission-epilogue">
      <div class="mission-epilogue-label">✦ Эпилог</div>
      ${epilogues[m.id]}
    </div>`:'';
    let archivistHtml='';
    if(m.status!=='done'){
      if(totalCount===0){
        archivistHtml=`<div class="archivist-warning">⚠ Путь во мгле. Архивариус не видит ни одного задания. Поведай о своих намерениях — и путь прояснится.</div>`;
      } else if(onceDone===onceTotal&&onceTotal>0&&repeatTotal===0){
        archivistHtml=`<div class="archivist-wisdom">✦ Все разовые задания пройдены. Архивариус полагает: цель близко. Скажи слово — и мир запишет победу.</div>`;
      }
    }

    const tasks=m.tasks.map(t=>{
      const isRepeat=t.task_type==='repeat';
      const cycled=isRepeat&&t.current_iters>=t.required_iters;
      if(isRepeat){
        const hrs=t.reset_hours===24?'24ч (ежедн.)':t.reset_hours===1?'1ч':t.reset_hours+'ч';
        return `
        <div class="quest-item repeat-task">
          <div class="quest-info" style="flex:1">
            <div style="display:flex;align-items:center;gap:7px">
              <span class="repeat-badge">🔄 повтор</span>
              <div class="quest-title" id="qtitle-${t.id}">${t.title}</div>
              <button class="btn-edit-inline" onclick="editTask('${t.id}');event.stopPropagation()" title="Редактировать">✎</button>
            </div>
            <div class="repeat-progress">
              <button class="iter-btn" onclick="tickTask('${t.id}','${m.id}')" ${cycled?'disabled':''}>+1</button>
              <span class="iter-count ${cycled?'done':''}">${t.current_iters}/${t.required_iters}</span>
              <span class="streak-display">${streakMythName(t.streak)}</span>
              <span class="streak-best">рекорд: ${t.best_streak}</span>
              <span class="reset-hint" data-reset-ts="${t.last_reset_ts||''}" data-reset-hours="${t.reset_hours||24}">· ${fmtCountdown(t.last_reset_ts,t.reset_hours)}</span>
            </div>
          </div>
          <button class="quest-del" onclick="deleteTask('${t.id}','${m.id}')" title="Удалить">×</button>
        </div>`;
      }
      const isDone=t.status==='done';
      return `
      <div class="quest-item">
        <div class="quest-cb ${isDone?'done':''}" onclick="doneTask('${t.id}','${m.id}')">${isDone?'✓':''}</div>
        <div class="quest-info">
          <div class="quest-title ${isDone?'done':''}" id="qtitle-${t.id}">${t.title}</div>
          <div class="quest-ts">${t.ts||''}</div>
        </div>
        <button class="btn-edit-inline" onclick="editTask('${t.id}');event.stopPropagation()" title="Редактировать">✎</button>
        <button class="quest-del" onclick="deleteTask('${t.id}','${m.id}')" title="Удалить">×</button>
      </div>`;
    }).join('');

    const badgeText=onceTotal?`${onceDone}/${onceTotal} разовых`:'';
    const repeatBadge=repeatTotal?`${repeatTotal} повтор.`:'';
    const badge=[badgeText,repeatBadge].filter(Boolean).join(' · ');

    const entTags=(m.entities||[]).map(e=>`<span class="mission-ent-tag" onclick="openEnt('${(e.name||'').replace(/'/g,"\\'")}');event.stopPropagation()" title="${e.summary||''}">${e.name}</span>`).join('');
    const descHtml=m.description?`<div class="mission-desc-view" id="mdesc-view-${m.id}" onclick="editMissionDesc('${m.id}')" title="Нажми чтобы изменить описание">${m.description}</div>`
      :`<div class="mission-desc-view mission-desc-empty" id="mdesc-view-${m.id}" onclick="editMissionDesc('${m.id}')" title="Добавить описание">+ добавить описание пути...</div>`;

    return `
    <div class="mission-block ${m.status==='done'?'done':''}">
      <div class="mission-block-hdr" onclick="toggleMission('${m.id}')">
        <div class="mission-star">${m.status==='done'?'✓':'✦'}</div>
        <div class="mission-block-info">
          <div class="mission-block-title" id="mtitle-${m.id}">${m.title}</div>
          ${badge?`<div class="mission-progress-badge">${badge}</div>`:''}
        </div>
        <button class="btn-edit-inline" onclick="editMission('${m.id}');event.stopPropagation()" title="Переименовать">✎</button>
        <div class="mission-block-chevron ${wasOpen?'open':''}" id="chev-${m.id}">▾</div>
      </div>
      ${entTags?`<div class="mission-entities">${entTags}</div>`:''}
      ${descHtml}
      <div class="mission-desc-edit" id="mdesc-edit-${m.id}" style="display:none">
        <textarea id="mdesc-ta-${m.id}" rows="3" placeholder="Описание пути — что это значит для Героя, зачем он идёт...">${m.description||''}</textarea>
        <div style="display:flex;gap:6px;margin-top:5px">
          <button class="btn-sm btn-ok" onclick="saveMissionDesc('${m.id}')">Сохранить</button>
          <button class="btn-sm" onclick="cancelMissionDesc('${m.id}')">Отмена</button>
        </div>
      </div>
      ${m.lore?`<div class="mission-lore">${m.lore}</div>`:''}
      <div class="quest-chain ${wasOpen?'open':''}" id="qchain-${m.id}">
        ${archivistHtml}
        ${tasks}
      </div>
      ${epilogueHtml}
      <div class="mission-actions">
        ${m.status!=='done'?`<button class="btn-quest-add" onclick="openTaskDlg('${m.id}')">+ добавить задание</button>`:''}
        <button class="btn-link-entity" onclick="openEntityDlg('${m.id}')" title="Привязать сущность к Пути">+ сущность</button>
        ${m.status!=='done'?`<button class="btn-sm btn-ok" onclick="doneMission('${m.id}')">✓ Путь пройден</button>`:''}
        <button class="btn-sm btn-danger" onclick="deleteMission('${m.id}')">× удалить</button>
      </div>
    </div>`;
  }).join('');
  _openMissions.forEach(mid=>{
    const chain=document.getElementById('qchain-'+mid);
    const chev=document.getElementById('chev-'+mid);
    if(chain) chain.classList.add('open');
    if(chev) chev.classList.add('open');
  });
}

function editMissionDesc(mid){
  document.getElementById('mdesc-view-'+mid).style.display='none';
  const ed=document.getElementById('mdesc-edit-'+mid);
  ed.style.display='block';
  setTimeout(()=>document.getElementById('mdesc-ta-'+mid).focus(),30);
}
function cancelMissionDesc(mid){
  document.getElementById('mdesc-edit-'+mid).style.display='none';
  document.getElementById('mdesc-view-'+mid).style.display='';
}
async function saveMissionDesc(mid){
  const val=document.getElementById('mdesc-ta-'+mid).value.trim();
  await fetch(`/missions/${mid}/description`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({description:val})});
  loadMissions();
}
function openEntityDlg(mid){
  document.getElementById('entity-dlg-title').textContent=mid?'Привязать сущность к Пути':'Новая сущность';
  document.getElementById('ent-name').value='';
  document.getElementById('ent-summary').value='';
  document.getElementById('ent-link-mid').value=mid||'';
  openDlg('entity-dlg');
  setTimeout(()=>document.getElementById('ent-name').focus(),50);
}
async function saveEntity(){
  const name=document.getElementById('ent-name').value.trim();
  if(!name) return;
  const type=document.getElementById('ent-type').value;
  const summary=document.getElementById('ent-summary').value.trim();
  const mid=document.getElementById('ent-link-mid').value;
  await fetch('/entities',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name,type,summary})});
  if(mid){
    await fetch(`/missions/${mid}/link-entity`,{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({entity_name:name,label:'связан с'})});
    loadMissions();
  }
  closeDlg('entity-dlg');
  if(document.querySelector('#s-base.active')) loadBase();
}
async function saveMission(){
  const t=document.getElementById('m-title').value.trim();
  const d=document.getElementById('m-desc').value.trim();
  if(!t) return;
  const res=await fetch('/missions',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:t,description:d})});
  const data=await res.json();
  closeDlg('mission-dlg');
  document.getElementById('m-title').value='';
  document.getElementById('m-desc').value='';
  if(data.id) _openMissions.add(data.id); // auto-expand new mission
  loadMissions();
}
function openTaskDlg(mid){
  document.getElementById('t-mid').value=mid;
  openDlg('task-dlg');
  setTimeout(()=>document.getElementById('t-title').focus(),50);
}
async function saveTask(){
  const t=document.getElementById('t-title').value.trim();
  const mid=document.getElementById('t-mid').value;
  if(!t) return;
  const typeEl=document.querySelector('input[name="t-type"]:checked');
  const taskType=typeEl?typeEl.value:'once';
  const iters=parseInt(document.getElementById('t-iters')?.value)||1;
  const hours=parseInt(document.getElementById('t-hours')?.value)||24;
  await fetch('/tasks',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mission_id:mid,title:t,task_type:taskType,required_iters:iters,reset_hours:hours})});
  closeDlg('task-dlg');
  document.getElementById('t-title').value='';
  const onceRadio=document.querySelector('input[name="t-type"][value="once"]');
  if(onceRadio) onceRadio.checked=true;
  const repeatOpts=document.getElementById('t-repeat-opts');
  if(repeatOpts) repeatOpts.style.display='none';
  _openMissions.add(mid);
  loadMissions();
}
async function tickTask(tid,mid){
  await fetch(`/tasks/${tid}/tick`,{method:'POST'});
  _openMissions.add(mid); loadMissions();
}
function toggleRepeatOpts(el){
  const opts=document.getElementById('t-repeat-opts');
  if(opts) opts.style.display=el.value==='repeat'?'flex':'none';
}
async function doneMission(id){
  await fetch(`/missions/${id}/complete`,{method:'POST'}); loadMissions();
}
async function deleteMission(id){
  if(!confirm('Удалить этот путь и все его задания?')) return;
  await fetch(`/missions/${id}/delete`,{method:'POST'}); loadMissions();
}
async function doneTask(tid,mid){
  await fetch(`/tasks/${tid}/complete`,{method:'POST'});
  _openMissions.add(mid); loadMissions();
}
async function deleteTask(tid,mid){
  await fetch(`/tasks/${tid}/delete`,{method:'POST'});
  _openMissions.add(mid); loadMissions();
}

// ── Inline edit ───────────────────────────────────────────────────────────────
function editMission(mid){
  const el=document.getElementById('mtitle-'+mid);
  if(!el||el.querySelector('input')) return;
  const cur=el.textContent;
  el.innerHTML=`<input class="inline-edit-input" id="medit-${mid}"
    value="${cur.replace(/"/g,'&quot;')}"
    onblur="saveMissionEdit('${mid}')"
    onkeydown="if(event.key==='Enter'){event.preventDefault();saveMissionEdit('${mid}');}
               if(event.key==='Escape'){event.preventDefault();loadMissions();}">`;
  const inp=document.getElementById('medit-'+mid);
  inp.focus(); inp.select();
}
async function saveMissionEdit(mid){
  const inp=document.getElementById('medit-'+mid);
  if(!inp) return;
  const val=inp.value.trim();
  if(!val){loadMissions();return;}
  // Get current description
  await fetch(`/missions/${mid}/update`,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:val,description:''})});
  loadMissions();
}

function editTask(tid){
  const el=document.getElementById('qtitle-'+tid);
  if(!el||el.querySelector('input')) return;
  const cur=el.textContent;
  el.innerHTML=`<input class="inline-edit-input" id="tedit-${tid}"
    value="${cur.replace(/"/g,'&quot;')}"
    onblur="saveTaskEdit('${tid}')"
    onkeydown="if(event.key==='Enter'){event.preventDefault();saveTaskEdit('${tid}');}
               if(event.key==='Escape'){event.preventDefault();loadMissions();}">`;
  const inp=document.getElementById('tedit-'+tid);
  inp.focus(); inp.select();
}
async function saveTaskEdit(tid){
  const inp=document.getElementById('tedit-'+tid);
  if(!inp) return;
  const val=inp.value.trim();
  if(!val){loadMissions();return;}
  await fetch(`/tasks/${tid}/update`,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:val})});
  loadMissions();
}

// ── Base of Knowledge ─────────────────────────────────────────────────────────
async function loadBase(){
  const [er,gr,cd]=await Promise.all([fetch('/entities'),fetch('/graph'),fetch('/character/data')]);
  const entities=await er.json(); allEntities=entities;
  const graph=await gr.json(); const charData=await cd.json();

  // Build link map: entity name → outgoing relations
  const linkMap={};
  for(const r of graph){
    if(!linkMap[r.from])linkMap[r.from]=[];
    linkMap[r.from].push(r);
  }

  // Group by type
  const order=['person','place','project','concept','event'];
  const labels={person:'👤 Люди',place:'📍 Места',project:'📁 Проекты',
                concept:'💡 Концепции',event:'📅 События'};
  const groups={};
  for(const e of entities){
    const t=e.type||'concept';
    if(!groups[t])groups[t]=[];
    groups[t].push(e);
  }

  const el=document.getElementById('base-content');
  const typeOrder=[...order,...Object.keys(groups).filter(k=>!order.includes(k))];
  const html=typeOrder.filter(t=>groups[t]?.length).map(type=>{
    const ents=groups[type];
    const clr=TYPE_COLORS[type]||'var(--ink3)';
    const cards=ents.map(e=>{
      const rels=(linkMap[e.name]||[]).slice(0,4);
      const relsHtml=rels.map(r=>`<span class="bcard-rel" onclick="event.stopPropagation();openEnt('${r.to.replace(/'/g,"\\'")}')">→ ${r.to}</span>`).join('');
      const tagsArr=Array.isArray(e.tags)?e.tags:typeof e.tags==='string'&&e.tags?e.tags.split(',').filter(Boolean):[];
      const tagsHtml=tagsArr.map(t=>`<span class="bcard-tag">${t}</span>`).join('');
      return `<div class="bcard" onclick="openEnt('${e.name.replace(/'/g,"\\'")}')">
        <div class="bcard-stripe" style="background:${clr}"></div>
        <div class="bcard-name">${e.name}</div>
        <div class="bcard-type" style="color:${clr}">${labels[type]||type}</div>
        <div class="bcard-summary">${e.summary||''}</div>
        ${relsHtml?`<div class="bcard-rels">${relsHtml}</div>`:''}
        ${tagsHtml?`<div class="bcard-tags">${tagsHtml}</div>`:''}
        <div class="bcard-footer">нажми → полная карточка</div>
      </div>`;
    }).join('');
    return `<div class="base-section">
      <div class="base-sec-hdr">
        <div class="base-sec-title" style="color:${clr}">${labels[type]||type}</div>
        <div class="base-sec-count">${ents.length} записей</div>
      </div>
      <div class="base-grid">${cards}</div>
    </div>`;
  }).join('');

  const antagonistHtml=charData.antagonist_name?`<div class="antag-card">
    <div class="antag-title">⚔ Антагонист Героя</div>
    <div class="antag-name">${charData.antagonist_name}</div>
    <div class="antag-desc">${charData.antagonist_desc}</div>
    <div class="antag-hint">Архивариус выявил это из твоих записей · ${charData.last_analyzed||''}</div>
  </div>`:'';

  // Separate Пути section — mission entities
  const missionR=await fetch('/missions'); const missions=await missionR.json();
  const missionCards=missions.map(m=>{
    const clr='var(--red)';
    const entTags=(m.entities||[]).map(e=>`<span class="bcard-rel" onclick="event.stopPropagation();openEnt('${(e.name||'').replace(/'/g,"\\'")}');" style="cursor:pointer">${e.name}</span>`).join('');
    const statusBadge=m.status==='done'?'<span style="font-size:9px;font-family:sans-serif;color:var(--ink3);margin-left:6px">завершён</span>':'';
    return `<div class="bcard" onclick="nav(document.querySelector('[data-s=missions]'))">
      <div class="bcard-stripe" style="background:${clr}"></div>
      <div class="bcard-name">${m.title}${statusBadge}</div>
      <div class="bcard-type" style="color:${clr}">⚔ Путь</div>
      <div class="bcard-summary">${m.description||m.lore||'Описание не задано'}</div>
      ${entTags?`<div class="bcard-rels">${entTags}</div>`:''}
      <div class="bcard-footer">${m.tasks.length} заданий · нажми чтобы перейти к Путям</div>
    </div>`;
  }).join('');
  const pathsSection=missions.length?`<div class="base-section">
    <div class="base-sec-hdr">
      <div class="base-sec-title" style="color:var(--red)">⚔ Пути Героя</div>
      <div class="base-sec-count">${missions.length} путей</div>
    </div>
    <div class="base-grid">${missionCards}</div>
  </div>`:'';

  el.innerHTML=antagonistHtml+pathsSection+(html||'<div class="empty">База пуста. Записи в журнале породят сущности здесь.</div>')+loadMechanics();
}

// ── Переосмыслить ────────────────────────────────────────────────────────────
async function reanalyze(){
  const btn=document.querySelector('.btn-reanalyze');
  btn.disabled=true; btn.textContent='⟳ Запрос отправлен...';
  try{
    const d=await (await fetch('/reanalyze',{method:'POST'})).json();
    if(d.status==='no_api_key'){
      btn.textContent='⟳ Переосмыслить'; btn.disabled=false;
      openSettings(); return;
    }
    btn.textContent='⟳ Архивариус анализирует...';
    _pollReanalyze();
  }catch(e){
    btn.textContent='⟳ Переосмыслить'; btn.disabled=false;
  }
}
function _pollReanalyze(){
  const btn=document.querySelector('.btn-reanalyze');
  let dots=0;
  const labels=['⟳ Архивариус анализирует','⟳ Архивариус анализирует·','⟳ Архивариус анализирует··','⟳ Архивариус анализирует···'];
  const iv=setInterval(async()=>{
    dots=(dots+1)%4; btn.textContent=labels[dots];
    try{
      const s=await fetch('/reanalyze/status');
      const sd=await s.json();
      if(!sd.running){
        clearInterval(iv);
        btn.textContent='⟳ Переосмыслить'; btn.disabled=false;
        loadMissions(); loadAsides();
        if(document.querySelector('#s-base.active')) loadBase();
      }
    }catch(e){ clearInterval(iv); btn.textContent='⟳ Переосмыслить'; btn.disabled=false; }
  },5000);
  setTimeout(()=>{ clearInterval(iv); btn.textContent='⟳ Переосмыслить'; btn.disabled=false; },300000);
}

// ── Entity Modal ─────────────────────────────────────────────────────────────
async function openEnt(name){
  const r=await fetch('/entity/'+encodeURIComponent(name));
  if(!r.ok) return;
  const e=await r.json();
  const clr=TYPE_COLORS[e.type]||'var(--ink3)';
  const out=e.links_out.map(r=>
    `<div class="rel-row">
      <span class="rel-badge">${r.label}</span>
      <span class="rel-name" onclick="closeEnt();openEnt('${r.to.replace(/'/g,"\\'")}')">→ ${r.to}</span>
    </div>`).join('')||'<div style="color:var(--ink3);font-size:12px;font-family:sans-serif">нет</div>';
  const inp=e.links_in.map(r=>
    `<div class="rel-row">
      <span class="rel-name" onclick="closeEnt();openEnt('${r.from.replace(/'/g,"\\'")}')">← ${r.from}</span>
      <span class="rel-badge">${r.label}</span>
    </div>`).join('')||'<div style="color:var(--ink3);font-size:12px;font-family:sans-serif">нет</div>';
  const ments=e.mentions.map(m=>
    `<div class="ment-ts">${m.ts}</div>
    <div class="ment-text">${m.narrative}</div>
    ${m.archivist_note?`<div class="ment-archivist">◆ ${m.archivist_note}</div>`:''}`).join('');
  const entTagsArr=Array.isArray(e.tags)?e.tags:typeof e.tags==='string'&&e.tags?e.tags.split(',').filter(Boolean):[];
  const tagsHtml=entTagsArr.length
    ?`<div class="ent-tags">${entTagsArr.map(t=>`<span class="ent-tag">${t}</span>`).join('')}</div>`:'';

  document.getElementById('ent-content').innerHTML=`
    <div class="ent-name" style="color:${clr}">${ICONS[e.type]||'◆'} ${e.name}</div>
    <div class="ent-type" style="color:${clr}">${e.type}</div>
    ${tagsHtml}
    <div class="ent-summary">${e.summary}</div>
    <div class="ent-sec">Связи →</div>${out}
    <div class="ent-sec">← Упоминается</div>${inp}
    <div class="ent-sec">Из дневника</div>
    ${ments||'<div style="color:var(--ink3);font-size:12px;font-family:sans-serif">нет записей</div>'}
    <div style="display:flex;gap:8px;margin-top:18px">
      <button class="ent-merge-btn" onclick="openMergeDlg('${e.name.replace(/'/g,"\\'")}')">⇔ Объединить с...</button>
      <button class="ent-del" onclick="deleteEntity('${e.name.replace(/'/g,"\\'")}')">× Удалить</button>
    </div>`;
  document.getElementById('ent-modal').classList.add('open');
}
function closeEnt(){document.getElementById('ent-modal').classList.remove('open');}

function openMergeDlg(keepName){
  document.getElementById('merge-keep-label').textContent=keepName;
  document.getElementById('merge-drop-input').value='';
  // Show other entities as clickable suggestions
  const suggs=document.getElementById('merge-suggestions');
  suggs.innerHTML=(allEntities||[])
    .filter(e=>e.name!==keepName)
    .map(e=>`<span class="merge-suggest" onclick="document.getElementById('merge-drop-input').value='${e.name.replace(/'/g,"\\'")}';">${e.name}</span>`)
    .join('');
  openDlg('merge-dlg');
}
async function doMerge(){
  const keepName=document.getElementById('merge-keep-label').textContent;
  const dropName=document.getElementById('merge-drop-input').value.trim();
  if(!dropName){alert('Укажи что удалить'); return;}
  const r=await fetch('/entities/merge',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({keep_name:keepName,drop_name:dropName})});
  if(!r.ok){const d=await r.json(); alert(d.detail||'Ошибка'); return;}
  closeDlg('merge-dlg'); closeEnt(); loadBase();
  if(document.querySelector('#s-missions.active')) loadMissions();
}
async function deleteEntity(name){
  if(!confirm(`Удалить "${name}" из базы знаний?`)) return;
  const id=name.toLowerCase().replace(/[\s\-]+/g,'_').replace(/[^a-z0-9а-яё_]/g,'');
  await fetch(`/entities/${encodeURIComponent(id)}/delete`,{method:'POST'});
  closeEnt(); loadBase();
}


// ── Dialog helpers ────────────────────────────────────────────────────────────
function openDlg(id){document.getElementById(id).classList.add('open');}
function closeDlg(id){document.getElementById(id).classList.remove('open');}

// ── Auto-refresh journal (picks up Archivist updates) ─────────────────────────
// Reload whenever inbox count changes — including when it drops to 0 (processing done)
function fmtCountdown(lastTs, resetHours){
  if(!lastTs) return `сброс ${resetHours||24}ч`;
  const last=new Date(lastTs.replace(' ','T'));
  const due=new Date(last.getTime()+(resetHours||24)*3600*1000);
  const diff=due-Date.now();
  if(diff<=0) return 'сброс скоро';
  const h=Math.floor(diff/3600000);
  const m=Math.floor((diff%3600000)/60000);
  if(h>=1) return `сброс через ${h}ч ${m}м`;
  return `сброс через ${m}м`;
}
function tickCountdowns(){
  document.querySelectorAll('.reset-hint[data-reset-ts]').forEach(el=>{
    const ts=el.dataset.resetTs; const rh=parseInt(el.dataset.resetHours)||24;
    el.textContent='· '+fmtCountdown(ts,rh);
  });
}
setInterval(tickCountdowns,30000);

let _prevInboxCount = -1;
setInterval(async ()=>{
  try{
    const r=await fetch('/inbox'); const inbox=await r.json();
    const count=inbox.filter(i=>!i.type||i.type==='entry').length;
    if(count !== _prevInboxCount){
      _prevInboxCount=count;
      if(document.querySelector('#s-journal.active')){loadJournal();loadAsides();}
      if(document.querySelector('#s-missions.active')) loadMissions();
    }
  }catch(e){}
}, 5000);

// ── Pocket ───────────────────────────────────────────────────────────────────
function fmt(n){return n.toLocaleString('ru-RU',{minimumFractionDigits:0,maximumFractionDigits:2});}
async function loadPocket(){
  const d=await (await fetch('/pocket')).json();
  document.getElementById('pc-balance').textContent=fmt(d.balance)+' ₽';
  document.getElementById('pc-deferred').textContent=fmt(d.deferred)+' ₽';
  document.getElementById('pc-reserve-pct').value=d.reserve_pct;
  const DIR={p_income:'income',p_expense:'expense',p_deferred:'deferred',p_deferred_spend:'expense',p_adjust:'income',p_deferred_adjust:'deferred'};
  const LABEL={p_income:'+ доход',p_expense:'− расход',p_deferred:'→ резерв',p_deferred_spend:'← из резерва',p_adjust:'✎ корректировка баланса',p_deferred_adjust:'✎ корректировка резерва'};
  const SIGN={p_income:'+',p_expense:'−',p_deferred:'→',p_deferred_spend:'←',p_adjust:'±',p_deferred_adjust:'±'};
  document.getElementById('pocket-tx-list').innerHTML=d.transactions.length?
    d.transactions.map(t=>`
    <div class="pocket-tx-item">
      <div>
        <div class="pocket-tx-note">${LABEL[t.direction]||t.direction} · ${t.note}</div>
        <div class="pocket-tx-ts">${t.ts}</div>
      </div>
      <div class="pocket-tx-amount ${DIR[t.direction]||''}">${SIGN[t.direction]||''}${fmt(t.amount)} ₽</div>
    </div>`).join(''):
    '<div class="empty" style="padding:20px 0">Транзакций пока нет</div>';
}
async function savePocketCfg(){
  const pct=parseInt(document.getElementById('pc-reserve-pct').value)||20;
  await fetch('/pocket/config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({reserve_pct:pct})});
  loadPocket();
}
function togglePocketForm(which){
  ['income','expense','deferred-spend','adjust'].forEach(f=>{
    const el=document.getElementById('pf-'+f);
    el.classList.toggle('open',f===which&&!el.classList.contains('open'));
  });
}
function closePocketForms(){
  ['income','expense','deferred-spend','adjust'].forEach(f=>document.getElementById('pf-'+f).classList.remove('open'));
}
async function submitPocketIncome(){
  const amount=parseFloat(document.getElementById('pf-income-amount').value)||0;
  const source=document.getElementById('pf-income-source').value.trim();
  if(!amount) return;
  await fetch('/pocket/income',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({amount,source})});
  document.getElementById('pf-income-amount').value='';
  document.getElementById('pf-income-source').value='';
  closePocketForms(); loadPocket();
}
async function submitPocketExpense(fromDeferred){
  const amountId=fromDeferred?'pf-ds-amount':'pf-expense-amount';
  const noteId=fromDeferred?'pf-ds-note':'pf-expense-note';
  const amount=parseFloat(document.getElementById(amountId).value)||0;
  const note=document.getElementById(noteId).value.trim();
  if(!amount) return;
  await fetch('/pocket/expense',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({amount,note,from_deferred:fromDeferred})});
  document.getElementById(amountId).value='';
  document.getElementById(noteId).value='';
  closePocketForms(); loadPocket();
}

async function submitPocketAdjust(){
  const amount=parseFloat(document.getElementById('pf-adjust-amount').value);
  const note=document.getElementById('pf-adjust-note').value.trim();
  const target=document.getElementById('pf-adjust-target').value;
  if(!amount) return;
  await fetch('/pocket/adjust',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({amount,note,target})});
  document.getElementById('pf-adjust-amount').value='';
  document.getElementById('pf-adjust-note').value='';
  closePocketForms(); loadPocket();
}

// ── Settings ─────────────────────────────────────────────────────────────────
async function checkApiStatus(){
  try{
    const d=await (await fetch('/config/status')).json();
    const el=document.getElementById('nav-api-status');
    const active=d.active;
    const labels={anthropic:'✓ Claude активен',gigachat:'✓ GigaChat активен',none:'⚠ ИИ не настроен'};
    const colors={anthropic:'#6a9940',gigachat:'#6a9940',none:'#c06030'};
    if(el){el.textContent=labels[active]||labels.none; el.style.color=colors[active]||colors.none;}
    return active!=='none';
  }catch(e){ return false; }
}
function openSettings(){
  fetch('/config/status').then(r=>r.json()).then(d=>{
    const st=document.getElementById('settings-key-status');
    const msgs={anthropic:'✓ Anthropic Claude активен',gigachat:'✓ GigaChat активен',none:'ИИ не настроен — Архивариус не работает автономно'};
    st.textContent=msgs[d.active]||msgs.none;
    st.className='settings-status '+(d.active!=='none'?'ok':'missing');
  });
  document.getElementById('settings-modal').classList.add('open');
}
function closeSettings(){ document.getElementById('settings-modal').classList.remove('open'); }

async function exportData(){
  const r=await fetch('/export');
  const d=await r.json();
  const blob=new Blob([JSON.stringify(d,null,2)],{type:'application/json'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download='liferpg-export-'+new Date().toISOString().slice(0,10)+'.json';
  a.click();
}
async function importData(input){
  const file=input.files[0]; if(!file) return;
  const st=document.getElementById('import-status');
  st.textContent='Загрузка...'; st.style.color='var(--ink3)';
  try{
    const text=await file.text();
    const data=JSON.parse(text);
    const r=await fetch('/import',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({data})});
    const res=await r.json();
    if(res.ok){
      const imp=res.imported;
      st.textContent=`✓ Записей: ${imp.entries}, сущностей: ${imp.entities}, путей: ${imp.missions}`;
      st.style.color='var(--green)';
      loadJournal(); loadAsides(); loadCharacter();
    } else { st.textContent='Ошибка: '+(res.detail||'?'); st.style.color='var(--red)'; }
  }catch(e){ st.textContent='Неверный файл'; st.style.color='var(--red)'; }
  input.value='';
}
async function saveApiKey(){
  const key=document.getElementById('settings-api-key').value.trim();
  if(!key) return;
  const d=await (await fetch('/config/api-key',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:key})})).json();
  if(d.ok){
    document.getElementById('settings-api-key').value='';
    checkApiStatus(); openSettings(); processPendingInbox();
  }
}
async function saveGigaChat(){
  const key=document.getElementById('settings-gc-key').value.trim();
  if(!key) return;
  const d=await (await fetch('/config/gigachat',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({gigachat_key:key,gigachat_scope:'GIGACHAT_API_PERS'})})).json();
  if(d.ok){
    document.getElementById('settings-gc-key').value='';
    checkApiStatus(); openSettings(); processPendingInbox();
  }
}
async function processPendingInbox(){
  try{
    const d=await (await fetch('/inbox/process-pending',{method:'POST'})).json();
    if(d.ok && d.started>0){
      // Poll until inbox clears
      let tries=0;
      const iv=setInterval(async()=>{
        tries++;
        const inbox=await (await fetch('/inbox')).json();
        const rem=inbox.filter(i=>!i.type||i.type==='entry').length;
        if(rem===0||tries>24){ clearInterval(iv); loadJournal(); loadAsides(); }
      },5000);
    }
  }catch(e){}
}

// ── Login screen: Canvas + Hang Drum ─────────────────────────────────────────
const LoginAtmo = {
  _raf: null, _actx: null, _running: false,
  _particles: [], _drops: [],

  // Hang drum pentatonic (D minor: D3 F3 G3 A3 C4 D4 F4 A4)
  _notes: [146.83, 174.61, 196.00, 220.00, 261.63, 293.66, 349.23, 440.00],

  _ctx() {
    if (!this._actx) this._actx = new (window.AudioContext || window.webkitAudioContext)();
    return this._actx;
  },

  _reverb(ctx) {
    const conv = ctx.createConvolver();
    const len = ctx.sampleRate * 3;
    const buf = ctx.createBuffer(2, len, ctx.sampleRate);
    for (let c = 0; c < 2; c++) {
      const d = buf.getChannelData(c);
      for (let i = 0; i < len; i++) d[i] = (Math.random()*2-1) * Math.pow(1 - i/len, 2.5);
    }
    conv.buffer = buf; return conv;
  },

  _hangNote(freq, vol=0.18) {
    const ctx = this._ctx(); const now = ctx.currentTime;
    const rev = this._reverb(ctx);
    const gain = ctx.createGain(); gain.connect(rev); rev.connect(ctx.destination);
    // Primary tone
    const o1 = ctx.createOscillator(); o1.type = 'sine'; o1.frequency.value = freq;
    const g1 = ctx.createGain(); o1.connect(g1); g1.connect(gain);
    g1.gain.setValueAtTime(0, now); g1.gain.linearRampToValueAtTime(vol, now+0.008);
    g1.gain.exponentialRampToValueAtTime(0.001, now+4.5);
    // Octave harmonic (quieter)
    const o2 = ctx.createOscillator(); o2.type = 'sine'; o2.frequency.value = freq*2;
    const g2 = ctx.createGain(); o2.connect(g2); g2.connect(gain);
    g2.gain.setValueAtTime(0, now); g2.gain.linearRampToValueAtTime(vol*0.28, now+0.006);
    g2.gain.exponentialRampToValueAtTime(0.001, now+3.2);
    // Minor 3rd partial (metallic shimmer)
    const o3 = ctx.createOscillator(); o3.type = 'sine'; o3.frequency.value = freq*2.76;
    const g3 = ctx.createGain(); o3.connect(g3); g3.connect(gain);
    g3.gain.setValueAtTime(0, now); g3.gain.linearRampToValueAtTime(vol*0.10, now+0.004);
    g3.gain.exponentialRampToValueAtTime(0.001, now+1.8);
    [o1,o2,o3].forEach(o=>{o.start(now);o.stop(now+5);});
    gain.gain.setValueAtTime(1, now);
  },

  _drop() {
    const ctx = this._ctx(); const now = ctx.currentTime;
    const osc = ctx.createOscillator(); osc.type = 'sine';
    osc.frequency.setValueAtTime(1200, now);
    osc.frequency.exponentialRampToValueAtTime(180, now+0.08);
    const g = ctx.createGain(); g.gain.setValueAtTime(0.12, now);
    g.gain.exponentialRampToValueAtTime(0.001, now+0.18);
    osc.connect(g); g.connect(ctx.destination);
    osc.start(now); osc.stop(now+0.2);
  },

  _metalHit() {
    const ctx = this._ctx(); const now = ctx.currentTime;
    const buf = ctx.createBuffer(1, ctx.sampleRate*0.4, ctx.sampleRate);
    const d = buf.getChannelData(0);
    for (let i=0;i<d.length;i++) d[i]=(Math.random()*2-1)*Math.pow(1-i/d.length,1.4);
    const src = ctx.createBufferSource(); src.buffer = buf;
    const bp = ctx.createBiquadFilter(); bp.type='bandpass'; bp.frequency.value=320; bp.Q.value=8;
    const g = ctx.createGain(); g.gain.setValueAtTime(0.08,now); g.gain.exponentialRampToValueAtTime(0.001,now+0.5);
    src.connect(bp); bp.connect(g); g.connect(ctx.destination);
    src.start(now);
  },

  _initParticles(W, H) {
    this._particles = [];
    for (let i=0;i<60;i++) this._particles.push({
      x: Math.random()*W, y: Math.random()*H,
      r: Math.random()*1.8+0.3,
      vx: (Math.random()-.5)*0.12,
      vy: -Math.random()*0.18-0.04,
      a: Math.random()*0.35+0.05
    });
  },

  _drawFrame(canvas, ctx2d, t) {
    const W=canvas.width, H=canvas.height;
    ctx2d.clearRect(0,0,W,H);

    // Fog layers
    for (let l=0;l<3;l++) {
      const spd=[0.00004,0.00007,0.00012][l];
      const grd=ctx2d.createRadialGradient(
        W*(0.3+0.4*Math.sin(t*spd+l*2)), H*(0.4+0.3*Math.cos(t*spd*0.7+l)), 0,
        W*(0.3+0.4*Math.sin(t*spd+l*2)), H*(0.4+0.3*Math.cos(t*spd*0.7+l)), W*0.55);
      const a=[0.055,0.04,0.03][l];
      grd.addColorStop(0,`rgba(140,90,30,${a})`);
      grd.addColorStop(1,'rgba(0,0,0,0)');
      ctx2d.fillStyle=grd; ctx2d.fillRect(0,0,W,H);
    }

    // Particles
    this._particles.forEach(p=>{
      p.x+=p.vx; p.y+=p.vy;
      if(p.y<-4){p.y=H+4; p.x=Math.random()*W;}
      if(p.x<-4)p.x=W+4; if(p.x>W+4)p.x=-4;
      const flicker=0.6+0.4*Math.sin(t*0.001*Math.random()+p.x);
      ctx2d.beginPath();
      ctx2d.arc(p.x,p.y,p.r,0,Math.PI*2);
      ctx2d.fillStyle=`rgba(200,160,80,${p.a*flicker})`;
      ctx2d.fill();
    });

    // Rain drops
    this._drops = this._drops.filter(dr=>{
      dr.y += dr.vy; dr.a -= 0.018;
      if(dr.a<=0) return false;
      ctx2d.beginPath(); ctx2d.arc(dr.x,dr.y,dr.r,0,Math.PI*2);
      ctx2d.fillStyle=`rgba(120,160,200,${dr.a})`; ctx2d.fill();
      // ripple
      if(dr.vy<0.5){
        ctx2d.beginPath(); ctx2d.arc(dr.x,dr.y,dr.r*3*(1-dr.a),0,Math.PI*2);
        ctx2d.strokeStyle=`rgba(120,160,200,${dr.a*0.4})`; ctx2d.lineWidth=0.5; ctx2d.stroke();
      }
      return true;
    });
  },

  _schedule() {
    if (!this._running) return;
    // Hang drum note every 3-9s
    const hangDelay = (3000 + Math.random()*6000);
    setTimeout(()=>{
      if(!this._running) return;
      const freq = this._notes[Math.floor(Math.random()*this._notes.length)];
      // Sometimes play 2 notes close together
      this._hangNote(freq);
      if(Math.random()<0.3) setTimeout(()=>this._hangNote(this._notes[Math.floor(Math.random()*this._notes.length)],0.12), 400+Math.random()*600);
      this._schedule();
    }, hangDelay);
    // Water drop every 4-12s
    setTimeout(()=>{
      if(!this._running) return;
      this._drop();
      const canvas=document.getElementById('ls-canvas');
      if(canvas) this._drops.push({x:Math.random()*canvas.width,y:Math.random()*canvas.height*0.7,r:2.5,vy:0.1,a:0.7});
    }, 2000+Math.random()*10000);
    // Metal hit every 8-20s
    setTimeout(()=>{
      if(!this._running) return;
      this._metalHit();
    }, 5000+Math.random()*15000);
  },

  start() {
    if(this._running) return;
    this._running = true;
    const canvas = document.getElementById('ls-canvas');
    if(!canvas) return;
    const resize=()=>{canvas.width=window.innerWidth;canvas.height=window.innerHeight;
      this._initParticles(canvas.width,canvas.height);};
    resize(); window.addEventListener('resize',resize);
    const ctx2d = canvas.getContext('2d');
    let t=0;
    const loop=()=>{
      if(!this._running){ctx2d.clearRect(0,0,canvas.width,canvas.height);return;}
      t+=16; this._drawFrame(canvas,ctx2d,t); this._raf=requestAnimationFrame(loop);
    };
    this._raf=requestAnimationFrame(loop);
    // Start audio on first user gesture (autoplay policy)
    const startAudio=()=>{
      this._ctx(); // resume context
      if(this._actx.state==='suspended') this._actx.resume();
      this._schedule();
      document.removeEventListener('click',startAudio);
      document.removeEventListener('keydown',startAudio);
    };
    document.addEventListener('click',startAudio);
    document.addEventListener('keydown',startAudio);
  },

  stop() {
    this._running=false;
    if(this._raf) cancelAnimationFrame(this._raf);
    if(this._actx) { this._actx.suspend(); }
  }
};

// ── Init ─────────────────────────────────────────────────────────────────────
checkApiStatus();
authInit().then(()=>{
  if(localStorage.getItem('lrpg_token')){
    loadJournal(); loadAsides(); loadCharacter();
    fetch('/character/data').then(r=>r.json()).then(d=>{
      const last=d.last_analyzed;
      const stale=!last||(Date.now()-new Date(last).getTime())/86400000>7;
      if(stale) fetch('/character/analyze',{method:'POST'}).then(()=>setTimeout(loadCharacter,15000));
    });
  }
});
// Mobile: show input bar on journal tab by default
(function(){
  if(window.innerWidth<=768){
    document.getElementById('input-bar').classList.add('mob-visible');
  }
})();
</script>
<div id="login-screen" style="display:none;position:fixed;inset:0;z-index:9999;
  background:#0e0a06;align-items:center;justify-content:center;flex-direction:column">
  <canvas id="ls-canvas" style="position:absolute;inset:0;width:100%;height:100%;pointer-events:none"></canvas>
  <div style="text-align:center;margin-bottom:28px;position:relative;z-index:1">
    <div style="font-size:32px;font-family:'Georgia',serif;color:#e8d5b0;letter-spacing:4px;text-shadow:0 0 40px rgba(180,130,60,.5)">Life RPG</div>
    <div style="font-size:10px;color:#7a6040;letter-spacing:6px;text-transform:uppercase;margin-top:6px">живая летопись</div>
  </div>
  <div style="background:rgba(20,14,6,.85);border:1px solid rgba(180,140,70,.25);border-radius:6px;
    padding:32px 40px;width:300px;box-shadow:0 8px 40px rgba(0,0,0,.6);position:relative;z-index:1;backdrop-filter:blur(8px)">
    <div style="display:flex;gap:0;margin-bottom:20px;border:1px solid rgba(180,140,70,.2);border-radius:3px;overflow:hidden">
      <button id="ls-tab-login" onclick="lsTab('login')" style="flex:1;padding:7px;border:none;
        background:rgba(180,130,60,.8);color:#e8d5b0;font-family:'Georgia',serif;font-size:12px;cursor:pointer">Войти</button>
      <button id="ls-tab-reg" onclick="lsTab('reg')" style="flex:1;padding:7px;border:none;
        background:transparent;color:#7a6040;font-family:'Georgia',serif;font-size:12px;cursor:pointer">Создать аккаунт</button>
    </div>
    <input id="ls-login" placeholder="Логин" style="width:100%;box-sizing:border-box;background:rgba(255,255,255,.05);
      border:1px solid rgba(180,140,70,.2);color:#e8d5b0;font-family:'Georgia',serif;font-size:14px;
      padding:9px 12px;border-radius:3px;outline:none;margin-bottom:10px"
      onkeydown="if(event.key==='Enter')document.getElementById('ls-pw').focus()">
    <input id="ls-pw" type="password" placeholder="Пароль" style="width:100%;box-sizing:border-box;background:rgba(255,255,255,.05);
      border:1px solid rgba(180,140,70,.2);color:#e8d5b0;font-family:'Georgia',serif;font-size:14px;
      padding:9px 12px;border-radius:3px;outline:none;margin-bottom:10px"
      onkeydown="if(event.key==='Enter')lsSubmit()">
    <input id="ls-pw2" type="password" placeholder="Повтори пароль" style="display:none;width:100%;box-sizing:border-box;
      background:rgba(255,255,255,.05);border:1px solid rgba(180,140,70,.2);color:#e8d5b0;
      font-family:'Georgia',serif;font-size:14px;padding:9px 12px;border-radius:3px;outline:none;margin-bottom:10px"
      onkeydown="if(event.key==='Enter')lsSubmit()">
    <div id="ls-err" style="font-size:12px;color:#c0614a;font-family:sans-serif;min-height:18px;margin-bottom:8px"></div>
    <button id="ls-btn" onclick="lsSubmit()" style="width:100%;background:rgba(180,130,60,.85);color:#e8d5b0;
      border:none;padding:11px;font-family:'Georgia',serif;font-size:14px;letter-spacing:2px;
      border-radius:3px;cursor:pointer;transition:background .2s">Войти</button>
  </div>
</div>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
def root(): return HTML
