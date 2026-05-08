"""
Life RPG — Living Narrative OS v4
Parchment · Knowledge Graph · Колесо Миров · AI Архивариус
"""
import json, uuid, re, subprocess, time, threading, os, hashlib, hmac, secrets, math
import urllib.request as _ur
from datetime import datetime, timedelta
from pathlib import Path

import kuzu
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import HTMLResponse, Response
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

def _clean_journal_text(text: str) -> str:
    """Keep old saved AI output readable without exposing prompt/meta artifacts."""
    if not text: return ""
    text = str(text).strip()
    text = re.sub(r'^\s*[«"]?quest:[^»"\n]+[»"]?\s*$', "", text, flags=re.I)
    text = re.sub(r'\s*◆\s*Запись создана по действию квеста\.?', "", text, flags=re.I)
    text = re.sub(r'(?:\*\*)?\s*Значение:\s*(?:\*\*)?\s*[-+]?\d+(?:[.,]\d+)?\s*', " ", text, flags=re.I)
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'__(.*?)__', r'\1', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'[ \t]+', " ", text)
    text = re.sub(r'\n{3,}', "\n\n", text)
    return text.strip(" \n\t\"«»")

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

def _path_entity_context_id(mid: str, entity_id: str, user_id: str) -> str:
    return _scoped_id(f"pathctx:{mid}:{entity_id}", user_id)

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
    for t in ["Entry","Entity","Mission","Task","QuestBranch","QuestEvent","PathEntityContext","Finance","Mode"]:
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
    _conn.execute("""CREATE NODE TABLE IF NOT EXISTS QuestBranch(
        id STRING, mission_id STRING, title STRING, status STRING,
        position INT64, ts STRING, user_id STRING, PRIMARY KEY(id))""")
    _conn.execute("""CREATE NODE TABLE IF NOT EXISTS QuestEvent(
        id STRING, task_id STRING, mission_id STRING, event_type STRING,
        value DOUBLE, note STRING, ts STRING, user_id STRING, PRIMARY KEY(id))""")
    _conn.execute("""CREATE NODE TABLE IF NOT EXISTS PathEntityContext(
        id STRING, mission_id STRING, entity_id STRING, note STRING,
        ai_note STRING, updated_ts STRING, user_id STRING, PRIMARY KEY(id))""")
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
    for tbl in ("Entry","Entity","Mission","Task","QuestBranch","QuestEvent","PathEntityContext","Finance","Mode"):
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

def _migrate_quest_engine_columns():
    """Quest Engine columns: branches, tree structure, timers, counters, unlocks."""
    for col, dtype, default in [
        ("quest_kind",          "STRING", "'task'"),
        ("branch_id",           "STRING", "''"),
        ("parent_id",           "STRING", "''"),
        ("position",            "INT64",  "0"),
        ("progress_mode",       "STRING", "'check'"),
        ("target_value",        "DOUBLE", "1"),
        ("progress_value",      "DOUBLE", "0"),
        ("timer_total_seconds", "INT64",  "0"),
        ("timer_started_ts",    "STRING", "''"),
        ("unlock_rule",         "STRING", "''"),
        ("unlock_payload",      "STRING", "''"),
        ("locked",              "STRING", "'false'"),
        ("notes",               "STRING", "''"),
        ("is_current",          "STRING", "'false'"),
        ("quest_description",   "STRING", "''"),
        ("description_updated_ts","STRING","''"),
        ("record_enabled",      "STRING", "'false'"),
        ("record_value",        "DOUBLE", "0"),
        ("record_label",        "STRING", "''"),
        ("timer_record_mode",   "STRING", "'none'"),
        ("timer_period_hours",  "INT64",  "24"),
        ("timer_period_started_ts","STRING","''"),
        ("timer_period_seconds","INT64",  "0"),
        ("timer_last_period_seconds","INT64","0"),
        ("timer_best_period_seconds","INT64","0"),
        ("timer_last_session_seconds","INT64","0"),
        ("timer_best_session_seconds","INT64","0"),
    ]:
        try: _conn.execute(f"ALTER TABLE Task ADD {col} {dtype} DEFAULT {default}")
        except: pass
_migrate_quest_engine_columns()

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
        if (t.get("progress_mode") or "") == "timed_sessions":
            _conn.execute(
                "MATCH (t:Task) WHERE t.id=$id "
                "SET t.current_iters=0,t.progress_value=0,t.last_reset_ts=$ts,t.streak=$s,t.best_streak=$b",
                {"id": t["id"], "ts": now_s, "s": stk, "b": best})
            t["progress_value"] = 0
        else:
            _conn.execute(
                "MATCH (t:Task) WHERE t.id=$id "
                "SET t.current_iters=0,t.last_reset_ts=$ts,t.streak=$s,t.best_streak=$b",
                {"id": t["id"], "ts": now_s, "s": stk, "b": best})
        t.update({"current_iters": 0, "last_reset_ts": now_s, "streak": stk, "best_streak": best})
    except: pass
    return t

def _now_s() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")

def _default_branch_id(mission_id: str) -> str:
    return f"{mission_id}:main"

def _ensure_default_branch(mission_id: str, user_id: str) -> str:
    bid = _default_branch_id(mission_id)
    rows = kuzu_rows(_conn.execute(
        "MATCH (b:QuestBranch) WHERE b.id=$id AND b.user_id=$uid RETURN b.id",
        {"id": bid, "uid": user_id}))
    if not rows:
        try:
            _conn.execute(
                "CREATE (:QuestBranch {id:$id,mission_id:$mid,title:'Основная',status:'active',position:0,ts:$ts,user_id:$uid})",
                {"id": bid, "mid": mission_id, "ts": _now_s(), "uid": user_id})
        except: pass
    return bid

def _ensure_mission_quest_engine(mission_id: str, user_id: str) -> str:
    bid = _ensure_default_branch(mission_id, user_id)
    rows = kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.mission_id=$mid AND t.user_id=$uid "
        "RETURN t.id,t.task_type,t.quest_kind,t.branch_id,t.progress_mode,t.target_value,t.progress_value,t.current_iters,t.required_iters",
        {"mid": mission_id, "uid": user_id}))
    for r in rows:
        tid, task_type, quest_kind, branch_id, progress_mode, target_value, progress_value, current_iters, required_iters = r
        kind = quest_kind or ("ritual" if task_type == "repeat" else "task")
        mode = progress_mode or ("count" if task_type == "repeat" else "check")
        target = float(target_value or required_iters or 1)
        progress = float(progress_value or current_iters or 0)
        if task_type == "repeat":
            kind = "ritual"
            if mode not in ("count", "timed_sessions"):
                mode = "count"
            target = float(target_value or (required_iters if mode == "count" else 5400) or 1)
            progress = float(progress_value or 0) if mode == "timed_sessions" else float(current_iters or progress or 0)
        if not branch_id:
            branch_id = bid
        try:
            _conn.execute(
                "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid "
                "SET t.branch_id=$bid,t.quest_kind=$kind,t.progress_mode=$mode,t.target_value=$target,t.progress_value=$progress",
                {"id": tid, "uid": user_id, "bid": branch_id, "kind": kind,
                 "mode": mode, "target": target, "progress": progress})
        except: pass
    return bid

def _record_quest_event(task_id: str, mission_id: str, user_id: str, event_type: str,
                        value: float = 0.0, note: str = ""):
    try:
        _conn.execute(
            "CREATE (:QuestEvent {id:$id,task_id:$tid,mission_id:$mid,event_type:$type,value:$value,note:$note,ts:$ts,user_id:$uid})",
            {"id": str(uuid.uuid4()), "tid": task_id, "mid": mission_id,
             "type": event_type, "value": float(value), "note": note,
             "ts": _now_s(), "uid": user_id})
    except Exception as e:
        print(f"[quest_event] {e}")

_FORCE_LEX = {
    "receiving": {
        "хочу": 1.0, "получ": 1.1, "взять": .95, "деньг": 1.25, "заработ": 1.25,
        "рост": .9, "прокач": .95, "навык": .85, "опыт": .85, "сил": .8,
        "вниман": 1.05, "охват": 1.05, "просмотр": 1.0, "подпис": 1.0, "аудитор": .55,
        "игра": .75, "майн": .8, "minecraft": .8, "стрим": .8, "трансляц": .8,
        "отдых": .85, "кайф": .85, "удоволь": .85, "куп": .75, "статус": .85,
        "контент": .65, "продукт": .6, "исслед": .65, "понял": .5
    },
    "giving": {
        "отда": 1.2, "помог": 1.25, "польз": 1.2, "дели": .95, "подел": .95,
        "науч": 1.05, "обуч": 1.05, "поддерж": 1.05, "забот": 1.0, "служ": 1.05,
        "созд": .82, "улучш": .82, "вдохнов": 1.0, "переда": 1.0, "смысл": .75,
        "чест": .85, "команд": .8, "семь": .8, "клиент": .85, "люд": .95,
        "аудитор": .8, "ответ": .8, "дело": .75, "благо": 1.1, "текст": .55
    },
    "concrete": {
        "сдел": .9, "напис": .9, "созд": .9, "пров": .8, "помог": .9,
        "игра": .65, "работ": .8, "уч": .65, "сня": .8, "вылож": .9,
        "разработ": .9, "собра": .8, "проч": .65, "запуст": .8, "законч": .9,
        "обнов": .8, "исправ": .85, "добав": .75, "поговор": .75
    },
    "mist": {
        "долж": .8, "надо": .55, "обязан": .8, "застав": .85, "срыв": .8,
        "убега": .8, "прокраст": .9, "пуст": .65, "бессмыс": .75, "винов": .75,
        "стыд": .75, "обман": .9, "наеб": .9, "ненавиж": .8
    }
}

def _force_tokens(text: str) -> list[str]:
    return re.findall(r"[a-zа-яё0-9]{3,}", (text or "").lower())

def _force_hits(tokens: list[str], lex: dict) -> float:
    score = 0.0
    for tok in set(tokens):
        for stem, weight in lex.items():
            if tok.startswith(stem):
                score += weight
                break
    return score

def _parse_ts(ts: str):
    try: return datetime.strptime((ts or "")[:16], "%Y-%m-%d %H:%M")
    except: return datetime.now()

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

def _entity_force_profile(name: str, summary: str = "", tags: str = "") -> dict:
    text = " ".join([name or "", summary or "", tags or ""])
    toks = _force_tokens(text)
    r = _force_hits(toks, _FORCE_LEX["receiving"])
    g = _force_hits(toks, _FORCE_LEX["giving"])
    return {
        "receiving": _clamp(.35 + r * .08, .15, .95),
        "giving": _clamp(.35 + g * .08, .15, .95)
    }

def _text_force_profile(text: str, source: str = "entry") -> dict:
    toks = _force_tokens(text)
    if not toks:
        return {"receiving": .05, "giving": .05, "trust": .25}
    unique = len(set(toks))
    r = _force_hits(toks, _FORCE_LEX["receiving"])
    g = _force_hits(toks, _FORCE_LEX["giving"])
    concrete = _force_hits(toks, _FORCE_LEX["concrete"])
    mist = _force_hits(toks, _FORCE_LEX["mist"])
    length_factor = _clamp(math.log1p(unique) / math.log(34), .24, 1.0)
    concrete_factor = _clamp(.42 + concrete * .13, .36, 1.0)
    fog_penalty = _clamp(1.0 - max(0, mist - concrete * .55) * .045, .68, 1.0)
    base = .10 if source == "entry" else .07
    receive = _clamp(base + min(.62, r * .065) + min(.12, concrete * .015), .03, .94)
    give = _clamp(base + min(.64, g * .07) + min(.10, concrete * .012), .03, .94)
    trust = _clamp(length_factor * concrete_factor * fog_penalty, .14, 1.0)
    return {"receiving": receive, "giving": give, "trust": trust}

def _force_score_to_pct(total: float, evidence: float) -> int:
    if evidence <= 0:
        return 0
    avg = _clamp(total / evidence, 0, 1)
    confidence = _clamp(1 - math.exp(-evidence / 18.0), .28, 1.0)
    return int(round(_clamp((avg / .34) * 100 * (.62 + .38 * confidence), 0, 100)))

def _force_snapshot(user_id: str = "admin") -> dict:
    now = datetime.now()
    signals = []
    entity_rows = kuzu_rows(_conn.execute(
        "MATCH (e:Entity) WHERE e.user_id=$uid RETURN e.name,e.summary,e.tags LIMIT 220",
        {"uid": user_id}))
    entity_profiles = []
    for name, summary, tags in entity_rows:
        entity_profiles.append({"name": name or "", "profile": _entity_force_profile(name or "", summary or "", tags or "")})

    entries = kuzu_rows(_conn.execute(
        "MATCH (e:Entry) WHERE e.user_id=$uid RETURN e.raw_text,e.narrative,e.archivist_note,e.ts "
        "ORDER BY e.ts DESC LIMIT 140", {"uid": user_id}))
    seen = {}
    for raw, narrative, note, ts in entries:
        text = _clean_journal_text(raw or "")
        if text.lower().startswith("quest:"):
            continue
        if not text: continue
        toks = _force_tokens(text)
        key = " ".join(toks[:28])
        seen[key] = seen.get(key, 0) + 1
        prof = _text_force_profile(text, "entry")
        ent_boost_r = ent_boost_g = 0.0
        nodes = []
        low = text.lower()
        for ep in entity_profiles:
            n = ep["name"]
            if len(n) >= 3 and n.lower() in low:
                ent_boost_r += ep["profile"]["receiving"] * .045
                ent_boost_g += ep["profile"]["giving"] * .045
                nodes.append(n)
        dup_penalty = 1 / math.sqrt(seen[key])
        signals.append({
            "kind": "entry", "ts": ts, "day": (ts or "")[:10], "source": "дневник",
            "receiving": _clamp(prof["receiving"] + ent_boost_r, .05, 1.0),
            "giving": _clamp(prof["giving"] + ent_boost_g, .05, 1.0),
            "weight": 1.05 * dup_penalty, "trust": prof["trust"],
            "text": text[:180], "nodes": nodes[:4]
        })

    tasks = {r[0]: {"title": r[1] or "", "kind": r[2] or "task"} for r in kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.user_id=$uid RETURN t.id,t.title,t.quest_kind LIMIT 500", {"uid": user_id}))}
    missions = {r[0]: r[1] or "" for r in kuzu_rows(_conn.execute(
        "MATCH (m:Mission) WHERE m.user_id=$uid RETURN m.id,m.title LIMIT 200", {"uid": user_id}))}
    qrows = kuzu_rows(_conn.execute(
        "MATCH (q:QuestEvent) WHERE q.user_id=$uid RETURN q.task_id,q.mission_id,q.event_type,q.value,q.note,q.ts "
        "ORDER BY q.ts DESC LIMIT 220", {"uid": user_id}))
    event_weight = {
        "completed": 1.55, "timer_stopped": 1.25, "tick": .58, "progress": .82,
        "note": .62, "created": .22, "moved": .18, "timer_started": .10
    }
    for tid, mid, etype, value, note, ts in qrows:
        task = tasks.get(tid or "", {})
        title = task.get("title", "")
        mission = missions.get(mid or "", "")
        text = " ".join([title, mission, note or "", etype or ""])
        prof = _text_force_profile(text, "quest")
        val = float(value or 0)
        kind = task.get("kind", "task")
        w = event_weight.get(etype or "", .45)
        if etype == "timer_stopped":
            minutes = max(1, val / 60.0)
            w *= _clamp(math.log1p(minutes) / math.log(91), .38, 1.65)
            prof["receiving"] = _clamp(prof["receiving"] + .16, .05, 1.0)
        if etype in ("completed", "progress", "tick"):
            prof["giving"] = _clamp(prof["giving"] + (0.08 if kind in ("task","ritual") else 0.04), .05, 1.0)
        if kind == "counter":
            prof["receiving"] = _clamp(prof["receiving"] + .06, .05, 1.0)
        signals.append({
            "kind": "quest", "ts": ts, "day": (ts or "")[:10], "source": "квест",
            "receiving": prof["receiving"], "giving": prof["giving"],
            "weight": w, "trust": _clamp(.62 + prof["trust"] * .38, .25, 1.0),
            "text": (title or etype or "квест")[:180], "nodes": [mission] if mission else []
        })

    day_totals = {}
    for s in signals:
        day_totals[s["day"]] = day_totals.get(s["day"], 0) + s["weight"]
    total_r = total_g = total_w = suspicion = 0.0
    node_power = {}
    day_set, quest_texts = set(), set()
    for s in signals:
        dt = _parse_ts(s["ts"])
        age = max(0, (now - dt).days)
        decay = math.exp(-age / 46)
        cap = min(1.0, 14.0 / max(1.0, day_totals.get(s["day"], 1)))
        trust = s["trust"]
        w = s["weight"] * decay * cap * (.48 + .52 * trust)
        total_r += s["receiving"] * w
        total_g += s["giving"] * w
        total_w += w
        suspicion += (1 - trust) * w
        if s["day"]: day_set.add(s["day"])
        if s["kind"] == "quest": quest_texts.add(s["text"])
        for n in s.get("nodes", []):
            if n:
                np = node_power.setdefault(n, {"receiving": 0.0, "giving": 0.0})
                np["receiving"] += s["receiving"] * w
                np["giving"] += s["giving"] * w
    diversity = _clamp((len(day_set) ** .45 + len(quest_texts) ** .32) / 4.2, .55, 1.08)
    integrity = _clamp(1 - (suspicion / max(total_w, .001)) * .75, .35, 1.0)
    force_scale = diversity * (.70 + .30 * integrity)
    receiving = _force_score_to_pct(total_r * force_scale, total_w)
    giving = _force_score_to_pct(total_g * force_scale, total_w)
    top_nodes = sorted(
        [{"name": k, "receiving": round(v["receiving"], 2), "giving": round(v["giving"], 2)}
         for k, v in node_power.items()],
        key=lambda x: x["receiving"] + x["giving"], reverse=True)[:5]
    balance = "равновесие"
    if receiving - giving >= 14: balance = "сильное получение"
    elif giving - receiving >= 14: balance = "сильная отдача"
    return {
        "receiving": receiving, "giving": giving, "balance": balance,
        "integrity": int(round(integrity * 100)), "signals": len(signals),
        "evidence": round(total_w, 2),
        "days": len(day_set), "top_nodes": top_nodes,
        "updated_ts": datetime.now().strftime("%Y-%m-%d %H:%M")
    }

def _timer_effective_seconds(total: int, started_ts: str) -> int:
    total = int(total or 0)
    if not started_ts:
        return total
    started = _dt_from_s(started_ts)
    try:
        if not started:
            return total
        return max(total, total + int((datetime.now() - started).total_seconds()))
    except:
        return total

def _dt_from_s(ts: str) -> datetime | None:
    if not ts: return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try: return datetime.strptime(ts, fmt)
        except: pass
    return None

def _s_from_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")

def _timer_now_s() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _timer_current_period_effective(task: dict) -> int:
    seconds=int(task.get("timer_period_seconds") or 0)
    if task.get("timer_record_mode") != "period":
        return seconds
    started=_dt_from_s(task.get("timer_started_ts") or "")
    period_started=_dt_from_s(task.get("timer_period_started_ts") or "")
    if started and period_started:
        seg_start=max(started, period_started)
        if datetime.now() > seg_start:
            seconds += int((datetime.now() - seg_start).total_seconds())
    return max(0, seconds)

def _sync_timer_period_state(task: dict, user_id: str) -> dict:
    """Roll elapsed timer periods, preserving historical stats in Task columns."""
    if (task.get("timer_record_mode") or "none") != "period":
        if int(task.get("timer_best_session_seconds") or 0) <= 0 and float(task.get("record_value") or 0) > 0:
            task["timer_best_session_seconds"]=int(float(task.get("record_value") or 0))
        return task
    now=datetime.now()
    period_hours=max(1, int(task.get("timer_period_hours") or 24))
    period_len=timedelta(hours=period_hours)
    period_started=_dt_from_s(task.get("timer_period_started_ts") or "")
    if not period_started or period_started > now:
        period_started=now
        task["timer_period_started_ts"]=_s_from_dt(period_started)
        try:
            _conn.execute("MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET t.timer_period_started_ts=$ts",
                          {"id":task["id"],"uid":user_id,"ts":task["timer_period_started_ts"]})
        except: pass
        return task

    raw_total=int(task.get("timer_total_seconds") or 0)
    period_seconds=int(task.get("timer_period_seconds") or 0)
    last_period=int(task.get("timer_last_period_seconds") or 0)
    best_period=int(task.get("timer_best_period_seconds") or 0)
    started=_dt_from_s(task.get("timer_started_ts") or "")
    total_add=0
    rolled=False

    while now >= period_started + period_len:
        period_end=period_started + period_len
        final_seconds=period_seconds
        if started and started < period_end:
            seg_start=max(started, period_started)
            if period_end > seg_start:
                seg=int((period_end - seg_start).total_seconds())
                final_seconds += seg
                total_add += seg
        last_period=max(0, final_seconds)
        best_period=max(best_period, last_period)
        period_started=period_end
        period_seconds=0
        rolled=True

    if rolled:
        if total_add and started:
            raw_total += total_add
            task["timer_started_ts"]=_s_from_dt(period_started)
        task.update({
            "timer_total_seconds":raw_total,
            "timer_period_started_ts":_s_from_dt(period_started),
            "timer_period_seconds":period_seconds,
            "timer_last_period_seconds":last_period,
            "timer_best_period_seconds":best_period,
            "record_value":float(best_period),
        })
        try:
            _conn.execute(
                "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET "
                "t.timer_total_seconds=$total,t.timer_started_ts=$started,"
                "t.timer_period_started_ts=$period_started,t.timer_period_seconds=$period_seconds,"
                "t.timer_last_period_seconds=$last_period,t.timer_best_period_seconds=$best_period,"
                "t.record_value=$record",
                {"id":task["id"],"uid":user_id,"total":raw_total,
                 "started":task.get("timer_started_ts") or "",
                 "period_started":task["timer_period_started_ts"],
                 "period_seconds":period_seconds,"last_period":last_period,
                 "best_period":best_period,"record":float(best_period)})
        except Exception as e: print(f"[timer_period_roll] {e}")
    return task

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
        "MATCH (m:Mission) WHERE m.status='active' AND m.user_id=$uid RETURN m.id,m.title,m.description LIMIT 10",
        {"uid":user_id}))
    ent_ctx  = "\n".join(f"- {r[0]} ({r[1]}): {r[2]}" for r in ents)
    task_rows = kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.user_id=$uid RETURN t.mission_id,t.title,t.quest_kind,t.is_current,t.status ORDER BY t.position,t.ts LIMIT 80",
        {"uid":user_id}))
    tasks_by_mid={}
    for t in task_rows:
        tasks_by_mid.setdefault(t[0],[]).append(f"{'★ ' if (t[3] or '')=='true' else ''}{t[1]} ({t[2] or 'task'}, {t[4]})")
    miss_ctx = "\n".join(
        f"  ID={r[0]}: {r[1]} — {r[2] or ''}\n    Квесты: " + "; ".join(tasks_by_mid.get(r[0],[])[:12])
        for r in missions)
    return ent_ctx, miss_ctx

def _knowledge_context(user_id: str, limit: int = 80) -> str:
    ents=kuzu_rows(_conn.execute(
        "MATCH (e:Entity) WHERE e.user_id=$uid RETURN e.name,e.type,e.summary ORDER BY e.name LIMIT $l",
        {"uid":user_id,"l":limit}))
    return "\n".join(f"- {r[0]} ({r[1]}): {r[2] or ''}" for r in ents) or "нет"

def _paths_context(user_id: str, limit: int = 120) -> str:
    missions=kuzu_rows(_conn.execute(
        "MATCH (m:Mission) WHERE m.user_id=$uid RETURN m.id,m.title,m.description,m.status ORDER BY m.ts",
        {"uid":user_id}))
    tasks=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.user_id=$uid RETURN t.mission_id,t.title,t.quest_kind,t.is_current,t.status,t.parent_id ORDER BY t.position,t.ts LIMIT $l",
        {"uid":user_id,"l":limit}))
    by_mid={}
    for t in tasks:
        mark="★ " if (t[3] or "")=="true" else ""
        child=" ↳" if t[5] else ""
        by_mid.setdefault(t[0],[]).append(f"{child}{mark}{t[1]} ({t[2] or 'task'}, {t[4]})")
    lines=[]
    for m in missions:
        lines.append(f"Путь {m[1]} [{m[3]}]: {m[2] or ''}")
        for task_line in by_mid.get(m[0],[])[:20]:
            lines.append(f"  - {task_line}")
    return "\n".join(lines) or "нет"

def _duration_ru(seconds: int | float) -> str:
    seconds=max(0,int(seconds or 0))
    h=seconds//3600
    m=(seconds%3600)//60
    if h and m: return f"{h} ч {m} мин"
    if h: return f"{h} ч"
    if m: return f"{m} мин"
    return "меньше минуты"

def _quest_snapshot(tid: str, user_id: str) -> dict | None:
    rows=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid RETURN "
        "t.id,t.mission_id,t.title,t.status,t.quest_kind,t.branch_id,t.parent_id,t.progress_mode,"
        "t.target_value,t.progress_value,t.current_iters,t.required_iters,t.timer_total_seconds,"
        "t.timer_started_ts,t.quest_description,t.notes,t.record_enabled,t.record_value,t.record_label,"
        "t.timer_record_mode,t.timer_period_hours,t.timer_period_started_ts,t.timer_period_seconds,"
        "t.timer_last_period_seconds,t.timer_best_period_seconds,t.timer_last_session_seconds,t.timer_best_session_seconds",
        {"id":tid,"uid":user_id}))
    if not rows: return None
    t=rows[0]; mid=t[1]
    mrows=kuzu_rows(_conn.execute(
        "MATCH (m:Mission) WHERE m.id=$mid AND m.user_id=$uid RETURN m.title,m.description,m.lore",
        {"mid":mid,"uid":user_id}))
    brows=kuzu_rows(_conn.execute(
        "MATCH (b:QuestBranch) WHERE b.id=$bid AND b.user_id=$uid RETURN b.title",
        {"bid":t[5] or "","uid":user_id}))
    eid=_mission_entity_id(mid, mrows[0][0] if mrows else "", user_id)
    linked=kuzu_rows(_conn.execute(
        "MATCH (m:Entity)-[r:LINKED]->(e:Entity) WHERE m.id=$id AND m.user_id=$uid AND e.user_id=$uid RETURN e.name,e.type,e.summary",
        {"id":eid,"uid":user_id}))
    task={"id":t[0],"mission_id":mid,"title":t[2],"status":t[3],"quest_kind":t[4] or "task",
          "branch_id":t[5] or "","parent_id":t[6] or "","progress_mode":t[7] or "check",
          "target_value":float(t[8] or 1),"progress_value":float(t[9] or 0),
          "current_iters":int(t[10] or 0),"required_iters":int(t[11] or 1),
          "timer_total_seconds":int(t[12] or 0),"timer_started_ts":t[13] or "",
          "quest_description":t[14] or "",
          "notes":t[15] or "","record_enabled":(t[16] or "false")=="true",
          "record_value":float(t[17] or 0),"record_label":t[18] or "",
          "timer_record_mode":t[19] or ("session" if (t[16] or "false")=="true" and (t[4] or "")=="timer" else "none"),
          "timer_period_hours":int(t[20] or 24),"timer_period_started_ts":t[21] or "",
          "timer_period_seconds":int(t[22] or 0),
          "timer_last_period_seconds":int(t[23] or 0),
          "timer_best_period_seconds":int(t[24] or 0),
          "timer_last_session_seconds":int(t[25] or 0),
          "timer_best_session_seconds":int(t[26] or t[17] or 0)}
    task=_sync_timer_period_state(task, user_id)
    task["timer_current_period_seconds"]=_timer_current_period_effective(task)
    if task.get("timer_record_mode") == "period":
        task["timer_best_period_seconds"]=max(int(task.get("timer_best_period_seconds") or 0),
                                              int(task.get("timer_current_period_seconds") or 0))
    task["timer_total_seconds"]=_timer_effective_seconds(int(task.get("timer_total_seconds") or 0), task.get("timer_started_ts") or "")
    return {
        "task":task,
        "mission":{"title":mrows[0][0] if mrows else "Путь","description":mrows[0][1] if mrows else "",
                   "lore":mrows[0][2] if mrows else ""},
        "branch":{"title":brows[0][0] if brows else "Основная"},
        "entities":[{"name":r[0],"type":r[1],"summary":r[2] or ""} for r in linked],
    }

def _quest_description_fallback(snap: dict) -> str:
    task=snap["task"]; mission=snap["mission"]; branch=snap["branch"]
    kind={"ritual":"ритуал","timer":"испытание времени","counter":"счётчик продвижения","task":"поручение"}.get(task["quest_kind"],"поручение")
    bits=[f"«{task['title']}» — {kind} в Пути «{mission['title']}»."]
    if branch.get("title"): bits.append(f"Ветвь: {branch['title']}.")
    if task.get("notes"): bits.append(task["notes"].split("\n")[-1])
    elif mission.get("lore"): bits.append(mission["lore"])
    elif mission.get("description"): bits.append(mission["description"])
    return " ".join(bits).strip()

def _generate_quest_description(snap: dict, user_id: str, player_note: str = "") -> str:
    fallback=_quest_description_fallback(snap)
    if not _has_any_ai():
        return player_note or fallback
    ents="\n".join(f"- {e['name']} ({e['type']}): {e['summary']}" for e in snap.get("entities",[])) or "нет"
    prompt=f"""Ты — квест-дизайнер Life RPG в духе дневника Morrowind и карточек заданий World of Warcraft.
Создай короткое описание задания: 2-4 предложения, конкретно, без пафосной воды, но с ощущением эпоса.

Задание: {snap['task']['title']}
Тип: {snap['task']['quest_kind']}
Путь: {snap['mission']['title']}
Ветвь: {snap['branch']['title']}
Лор пути: {snap['mission'].get('lore') or snap['mission'].get('description') or ''}
Сущности Пути:
{ents}
Пояснение игрока: {player_note or snap['task'].get('notes') or ''}

База знаний:
{_knowledge_context(user_id)}

Стратегическая карта Путей:
{_paths_context(user_id)}

Верни только текст описания."""
    text=_call_any_ai(prompt).strip()
    return text or player_note or fallback

def _quest_journal_fallback(snap: dict, event_type: str, value: float = 0.0) -> str:
    task=snap["task"]; mission=snap["mission"]
    linked=", ".join(e["name"] for e in snap.get("entities",[])[:4])
    path=f" на Пути «{mission['title']}»"
    context=f" Это связано с {linked}." if linked else ""
    if event_type=="timer_stopped":
        if task.get("progress_mode") == "timed_sessions":
            target=_duration_ru(float(task.get("target_value") or 5400))
            partial=_duration_ru(float(task.get("progress_value") or 0))
            return f"Я держал ритуал «{task['title']}» {_duration_ru(value)}{path}. Сейчас отмечено {task['current_iters']}/{task['required_iters']} подходов по {target}; в следующем подходе уже собрано {partial}. Я учусь превращать время в форму действия, которая потом сможет стать пользой для других.{context}"
        return f"Я занимался «{task['title']}» {_duration_ru(value)}{path}. Важно помнить: время становится силой, когда я направляю его не только на свой результат, но и на пользу, которую смогу передать дальше.{context}"
    if event_type=="tick":
        return f"Я сделал шаг в ритуале «{task['title']}»: {task['current_iters']}/{task['required_iters']}{path}. Малое повторение собирает намерение: я учусь превращать привычку в действие ради большего, чем просто отметка.{context}"
    if event_type=="progress":
        return f"Я продвинул задачу «{task['title']}» до {task['progress_value']:g}/{task['target_value']:g}{path}. Это не просто число: я проверяю, становится ли движение точнее, честнее и полезнее.{context}"
    if event_type=="completed":
        return f"Я завершил «{task['title']}»{path}. Этот шаг закрыт, но его смысл надо унести дальше: взять из него ясность, дисциплину и больше готовности отдавать через дело.{context}"
    return f"Я сделал шаг в задании «{task['title']}»{path}. Смысл шага в том, чтобы моё действие стало чуть менее случайным и чуть более направленным.{context}"

def _quest_event_meaning(event_type: str, value: float) -> str:
    if event_type=="timer_stopped": return f"остановлен таймер, сессия длилась {_duration_ru(value)}"
    if event_type=="tick": return "отмечен шаг повторяемого ритуала"
    if event_type=="progress": return f"обновлён прогресс до {value:g}"
    if event_type=="completed": return "задание выполнено"
    return "сделан шаг в задании"

def _improve_quest_journal_entry(entry_id: str, snap: dict, event_type: str, value: float, user_id: str):
    if not _has_any_ai(): return
    prompt=f"""Ты — тихий редактор дневника Life RPG.
Перепиши действие в запись ОТ ПЕРВОГО ЛИЦА: "Я сделал...", "Я занимался...", "Я понял...".
Стиль: ясный дневник Героя, не поэма, не религиозная проповедь, без слов "каббала", "Творец", "божественный", "святость".
Скрытый смысл: действие должно мягко приближать Героя к намерению отдавать, приносить пользу, очищать мотив от "получить ради себя" к "сделать ради смысла и пользы".
1-2 предложения. Конкретно: что сделано, зачем это важно, короткая рефлексия-наставление себе.
Не используй markdown. Не пиши технические поля вроде "Значение", сырые числа таймера или названия event_type.

Действие: {_quest_event_meaning(event_type,value)}
Задание: {snap['task']['title']}
Описание задания: {snap['task'].get('quest_description') or _quest_description_fallback(snap)}
Путь: {snap['mission']['title']}
Ветвь: {snap['branch']['title']}
Сущности: {', '.join(e['name'] for e in snap.get('entities',[])) or 'нет'}

База знаний:
{_knowledge_context(user_id)}

Карта Путей:
{_paths_context(user_id)}

Верни только текст записи."""
    text=_clean_journal_text(_call_any_ai(prompt).strip())
    if text:
        try:
            _conn.execute("MATCH (e:Entry) WHERE e.id=$id AND e.user_id=$uid SET e.narrative=$n,e.archivist_note=''",
                          {"id":entry_id,"uid":user_id,"n":text})
        except Exception as ex: print(f"[quest_journal_update] {ex}")

def _write_quest_journal_event(tid: str, user_id: str, event_type: str, value: float = 0.0):
    snap=_quest_snapshot(tid, user_id)
    if not snap: return
    raw=""
    narrative=_quest_journal_fallback(snap, event_type, value)
    data={"narrative":narrative,"archivist_note":"","entities":[],"relations":[]}
    eid=write_entry(raw, data, user_id)
    for ent in snap.get("entities",[]):
        sid=_entity_id(ent["name"], user_id)
        if entity_exists(sid, user_id):
            try:
                _conn.execute(
                    "MATCH (en:Entry) WHERE en.id=$eid AND en.user_id=$uid "
                    "MATCH (et:Entity) WHERE et.id=$etid AND et.user_id=$uid"
                    " CREATE (en)-[:MENTIONS]->(et)",
                    {"eid":eid,"etid":sid,"uid":user_id})
            except: pass
    if _has_any_ai():
        threading.Thread(target=_improve_quest_journal_entry,
                         args=(eid,snap,event_type,value,user_id),daemon=True).start()

PROMPT = """Ты — Архивариус, хранитель Живой Летописи. Анализируй запись Героя и верни ТОЛЬКО валидный JSON без markdown.

АКТИВНЫЕ МЕЧТЫ ГЕРОЯ:
{missions_ctx}

ИЗВЕСТНЫЕ СУЩНОСТИ В ЛЕТОПИСИ:
{entities_ctx}

Верни JSON строго в таком формате:
{{
  "narrative": "1-3 предложения ОТ ПЕРВОГО ЛИЦА: что я сделал, зачем это важно, какую точную мысль беру себе дальше",
  "entities": [
    {{"name":"Имя","type":"person|place|concept|project|event|object","summary":"одно ёмкое предложение","tags":[]}}
  ],
  "relations": [
    {{"from_entity":"Имя1","to_entity":"Имя2","label":"глагол отношения"}}
  ],
  "quests": [
    {{"title":"конкретный шаг или задача","mission_id":"id пути если явно связано или пустая строка","description":"контекст","task_type":"once или repeat","reset_hours":24,"required_iters":1}}
  ],
  "archivist_note": "короткая мысль-наставление от первого лица или пустая строка, без пафоса",
  "mission_analysis": [
    {{"mission_id":"id","insight":"что эта запись даёт или блокирует","lore":"1-2 предложения: что этот Путь помогает мне отдать миру через дело, конкретно и без религиозных терминов"}}
  ]
}}

ПРАВИЛА:
- narrative всегда от первого лица: "я сделал", "я понял", "мне стало ясно"; не "Герой сделал"
- скрытый смысл: я учусь менять намерение с личной выгоды на пользу, точность, отдачу, служение делу; без слов "каббала", "Творец", "религия", "святость"
- стиль живой и честный: меньше эпической пыли, больше конкретики действия и спокойной внутренней мысли
- не используй markdown, жирный шрифт, списки и технические поля вроде "Значение"
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
    an  = _clean_journal_text(data.get("archivist_note",""))
    narrative = _clean_journal_text(data.get("narrative", raw))
    _conn.execute(
        "CREATE (:Entry {id:$id,ts:$ts,raw_text:$r,narrative:$n,archivist_note:$an,user_id:$uid})",
        {"id":eid,"ts":ts,"r":raw,"n":narrative,"an":an,"uid":user_id})
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
        "t.last_reset_ts,t.streak,t.best_streak,t.entry_id,t.completed_ts,"
        "t.quest_kind,t.branch_id,t.parent_id,t.position,t.progress_mode,t.target_value,t.progress_value,"
        "t.timer_total_seconds,t.timer_started_ts,t.unlock_rule,t.unlock_payload,t.locked,t.notes,"
        "t.is_current,t.quest_description,t.description_updated_ts,t.record_enabled,t.record_value,t.record_label,"
        "t.timer_record_mode,t.timer_period_hours,t.timer_period_started_ts,t.timer_period_seconds,"
        "t.timer_last_period_seconds,t.timer_best_period_seconds,t.timer_last_session_seconds,t.timer_best_session_seconds",
        {"uid":uid}))
    branches = kuzu_rows(_conn.execute(
        "MATCH (b:QuestBranch) WHERE b.user_id=$uid RETURN b.id,b.mission_id,b.title,b.status,b.position,b.ts",
        {"uid":uid}))
    events = kuzu_rows(_conn.execute(
        "MATCH (ev:QuestEvent) WHERE ev.user_id=$uid RETURN ev.id,ev.task_id,ev.mission_id,ev.event_type,ev.value,ev.note,ev.ts",
        {"uid":uid}))
    path_contexts = kuzu_rows(_conn.execute(
        "MATCH (pc:PathEntityContext) WHERE pc.user_id=$uid RETURN pc.id,pc.mission_id,pc.entity_id,pc.note,pc.ai_note,pc.updated_ts",
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
        "branches": [{"id":r[0],"mission_id":r[1],"title":r[2],"status":r[3],"position":r[4] or 0,"ts":r[5]} for r in branches],
        "tasks":    [{"id":r[0],"mission_id":r[1],"title":r[2],"status":r[3],"ts":r[4],
                      "task_type":r[5] or "once","reset_hours":r[6] or 24,
                      "required_iters":r[7] or 1,"current_iters":r[8] or 0,
                      "last_reset_ts":r[9] or "","streak":r[10] or 0,"best_streak":r[11] or 0,
                      "entry_id":r[12] or "","completed_ts":r[13] or "",
                      "quest_kind":r[14] or "task","branch_id":r[15] or "",
                      "parent_id":r[16] or "","position":r[17] or 0,"progress_mode":r[18] or "check",
                      "target_value":r[19] or 1,"progress_value":r[20] or 0,
                      "timer_total_seconds":r[21] or 0,"timer_started_ts":r[22] or "",
                      "unlock_rule":r[23] or "","unlock_payload":r[24] or "",
                      "locked":r[25] or "false","notes":r[26] or "",
                      "is_current":r[27] or "false","quest_description":r[28] or "",
                      "description_updated_ts":r[29] or "",
                      "record_enabled":r[30] or "false","record_value":r[31] or 0,
                      "record_label":r[32] or "",
                      "timer_record_mode":r[33] or "none","timer_period_hours":r[34] or 24,
                      "timer_period_started_ts":r[35] or "",
                      "timer_period_seconds":r[36] or 0,
                      "timer_last_period_seconds":r[37] or 0,
                      "timer_best_period_seconds":r[38] or 0,
                      "timer_last_session_seconds":r[39] or 0,
                      "timer_best_session_seconds":r[40] or 0} for r in tasks],
        "events":   [{"id":r[0],"task_id":r[1],"mission_id":r[2],"event_type":r[3],
                      "value":r[4] or 0,"note":r[5] or "","ts":r[6]} for r in events],
        "path_entity_contexts": [{"id":r[0],"mission_id":r[1],"entity_id":r[2],
                                  "note":r[3] or "","ai_note":r[4] or "",
                                  "updated_ts":r[5] or ""} for r in path_contexts],
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
            if not entity_exists(e["id"], uid):
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
    # Quest branches
    for b in d.get("branches",[]):
        try:
            rows=kuzu_rows(_conn.execute("MATCH (x:QuestBranch) WHERE x.id=$id AND x.user_id=$uid RETURN x.id",
                                         {"id":b["id"],"uid":uid}))
            if not rows:
                _conn.execute(
                    "CREATE (:QuestBranch {id:$id,mission_id:$mid,title:$title,status:$status,position:$pos,ts:$ts,user_id:$uid})",
                    {"id":b["id"],"mid":b.get("mission_id",""),"title":b.get("title","Основная"),
                     "status":b.get("status","active"),"pos":int(b.get("position",0)),
                     "ts":b.get("ts",""),"uid":uid})
        except: pass
    # Tasks
    for t in d.get("tasks",[]):
        try:
            rows=kuzu_rows(_conn.execute("MATCH (x:Task) WHERE x.id=$id RETURN x.id",{"id":t["id"]}))
            if not rows:
                branch_id=t.get("branch_id","") or (_ensure_default_branch(t.get("mission_id",""), uid) if t.get("mission_id","") else "")
                _conn.execute(
                    "CREATE (:Task {id:$id,mission_id:$mid,title:$ti,status:$s,ts:$ts,"
                    "task_type:$tt,reset_hours:$rh,required_iters:$ri,current_iters:$ci,"
                    "last_reset_ts:$lr,streak:$st,best_streak:$bs,entry_id:$eid,completed_ts:$cts,user_id:$uid,"
                    "quest_kind:$kind,branch_id:$bid,parent_id:$parent,position:$pos,progress_mode:$pmode,"
                    "target_value:$target,progress_value:$progress,timer_total_seconds:$timer,timer_started_ts:$timer_started,"
                    "unlock_rule:$unlock_rule,unlock_payload:$unlock_payload,locked:$locked,notes:$notes,"
                    "is_current:$current,quest_description:$qdesc,description_updated_ts:$dts,"
                    "record_enabled:$record_enabled,record_value:$record_value,record_label:$record_label,"
                    "timer_record_mode:$timer_record_mode,timer_period_hours:$timer_period_hours,"
                    "timer_period_started_ts:$timer_period_started_ts,timer_period_seconds:$timer_period_seconds,"
                    "timer_last_period_seconds:$timer_last_period_seconds,timer_best_period_seconds:$timer_best_period_seconds,"
                    "timer_last_session_seconds:$timer_last_session_seconds,timer_best_session_seconds:$timer_best_session_seconds})",
                    {"id":t["id"],"mid":t.get("mission_id",""),"ti":t["title"],
                     "s":t.get("status","active"),"ts":t.get("ts",""),
                     "tt":t.get("task_type","once"),"rh":int(t.get("reset_hours",24)),
                     "ri":int(t.get("required_iters",1)),"ci":int(t.get("current_iters",0)),
                     "lr":t.get("last_reset_ts",""),"st":int(t.get("streak",0)),
                     "bs":int(t.get("best_streak",0)),"eid":t.get("entry_id",""),
                     "cts":t.get("completed_ts",""),"uid":uid,
                     "kind":t.get("quest_kind","ritual" if t.get("task_type")=="repeat" else "task"),
                     "bid":branch_id,"parent":t.get("parent_id",""),"pos":int(t.get("position",0)),
                     "pmode":t.get("progress_mode","count" if t.get("task_type")=="repeat" else "check"),
                     "target":float(t.get("target_value",t.get("required_iters",1) or 1)),
                     "progress":float(t.get("progress_value",t.get("current_iters",0) or 0)),
                     "timer":int(t.get("timer_total_seconds",0)),"timer_started":t.get("timer_started_ts",""),
                     "unlock_rule":t.get("unlock_rule",""),"unlock_payload":t.get("unlock_payload",""),
                     "locked":t.get("locked","false"),"notes":t.get("notes",""),
                     "current":t.get("is_current","false"),"qdesc":t.get("quest_description",""),
                     "dts":t.get("description_updated_ts",""),
                     "record_enabled":t.get("record_enabled","false"),
                     "record_value":float(t.get("record_value",0) or 0),
                     "record_label":t.get("record_label",""),
                     "timer_record_mode":t.get("timer_record_mode","session" if t.get("record_enabled")=="true" and t.get("quest_kind")=="timer" else "none"),
                     "timer_period_hours":int(t.get("timer_period_hours",24) or 24),
                     "timer_period_started_ts":t.get("timer_period_started_ts",""),
                     "timer_period_seconds":int(t.get("timer_period_seconds",0) or 0),
                     "timer_last_period_seconds":int(t.get("timer_last_period_seconds",0) or 0),
                     "timer_best_period_seconds":int(t.get("timer_best_period_seconds",0) or 0),
                     "timer_last_session_seconds":int(t.get("timer_last_session_seconds",0) or 0),
                     "timer_best_session_seconds":int(t.get("timer_best_session_seconds",t.get("record_value",0)) or 0)})
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
    # Quest events
    for ev in d.get("events",[]):
        try:
            rows=kuzu_rows(_conn.execute("MATCH (x:QuestEvent) WHERE x.id=$id AND x.user_id=$uid RETURN x.id",
                                         {"id":ev["id"],"uid":uid}))
            if not rows:
                _conn.execute(
                    "CREATE (:QuestEvent {id:$id,task_id:$tid,mission_id:$mid,event_type:$type,value:$value,note:$note,ts:$ts,user_id:$uid})",
                    {"id":ev["id"],"tid":ev.get("task_id",""),"mid":ev.get("mission_id",""),
                     "type":ev.get("event_type","import"),"value":float(ev.get("value",0)),
                     "note":ev.get("note",""),"ts":ev.get("ts",""),"uid":uid})
        except: pass
    # Path-specific entity contexts
    for pc in d.get("path_entity_contexts",[]):
        try:
            rows=kuzu_rows(_conn.execute("MATCH (x:PathEntityContext) WHERE x.id=$id AND x.user_id=$uid RETURN x.id",
                                         {"id":pc["id"],"uid":uid}))
            if not rows:
                _conn.execute(
                    "CREATE (:PathEntityContext {id:$id,mission_id:$mid,entity_id:$eid,note:$note,ai_note:$ai,updated_ts:$ts,user_id:$uid})",
                    {"id":pc["id"],"mid":pc.get("mission_id",""),"eid":pc.get("entity_id",""),
                     "note":pc.get("note",""),"ai":pc.get("ai_note",""),
                     "ts":pc.get("updated_ts",""),"uid":uid})
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
    quest_kind: str = "task"; progress_mode: str = "check"
    branch_id: str = ""; parent_id: str = ""
    target_value: float = 1; timer_total_seconds: int = 0
    unlock_rule: str = ""; unlock_payload: str = ""; locked: bool = False
    notes: str = ""
    is_current: bool = False; quest_description: str = ""
    record_enabled: bool = False; record_label: str = ""
    timer_record_mode: str = "none"; timer_period_hours: int = 24
class BranchReq(BaseModel):
    title: str
class PathEntityContextReq(BaseModel):
    entity_name: str
    note: str = ""
    improve: bool = False
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
    narrative = _clean_journal_text(data.get("narrative",""))
    an = _clean_journal_text(data.get("archivist_note",""))
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
        mid=q.get("mission_id","")
        bid=_ensure_default_branch(mid, user_id) if mid else ""
        qkind="ritual" if tt=="repeat" else "task"
        pmode="count" if tt=="repeat" else "check"
        try:
            _conn.execute(
                "CREATE (:Task {id:$id,mission_id:$mid,title:$t,status:'active',ts:$ts,entry_id:$eid,"
                "task_type:$tt,reset_hours:$rh,required_iters:$ri,current_iters:0,last_reset_ts:$lr,streak:0,best_streak:0,completed_ts:'',"
                "quest_kind:$kind,branch_id:$bid,parent_id:'',position:0,progress_mode:$pmode,target_value:$target,progress_value:0,"
                "timer_total_seconds:0,timer_started_ts:'',unlock_rule:'',unlock_payload:'',locked:'false',notes:'',user_id:$uid})",
                {"id":tid,"mid":mid,"t":q["title"],"ts":ts,"eid":eid,
                 "tt":tt,"rh":rh,"ri":ri,"lr":lr,"kind":qkind,"bid":bid,
                 "pmode":pmode,"target":float(ri),"uid":user_id})
            _record_quest_event(tid, mid, user_id, "created", 0, "from_ai")
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
        mid=q.get("mission_id","")
        bid=_ensure_default_branch(mid, uid) if mid else ""
        try:
            _conn.execute(
                "CREATE (:Task {id:$id,mission_id:$mid,title:$t,status:'active',ts:$ts,entry_id:$eid,user_id:$uid,"
                "task_type:'once',reset_hours:24,required_iters:1,current_iters:0,last_reset_ts:'',streak:0,best_streak:0,completed_ts:'',"
                "quest_kind:'task',branch_id:$bid,parent_id:'',position:0,progress_mode:'check',target_value:1,progress_value:0,"
                "timer_total_seconds:0,timer_started_ts:'',unlock_rule:'',unlock_payload:'',locked:'false',notes:''})",
                {"id":tid,"mid":mid,"t":q["title"],"ts":ts,"eid":eid,"uid":uid,"bid":bid})
            _record_quest_event(tid, mid, uid, "created", 0, "from_save")
        except: pass
    return {"entry_id":eid,"quests_created":len(req.quests)}

@app.get("/diary")
def diary(limit: int=60, u: dict = Depends(current_user)):
    uid = _uid(u)
    rows=kuzu_rows(_conn.execute(
        "MATCH (e:Entry) WHERE e.user_id=$uid RETURN e.id,e.ts,e.narrative,e.raw_text,e.archivist_note"
        " ORDER BY e.ts DESC LIMIT $l",{"l":limit,"uid":uid}))
    return [{"id":r[0],"ts":r[1],"narrative":_clean_journal_text(r[2] or r[3] or ""),
             "raw":r[3],"archivist_note":_clean_journal_text(r[4] or "")} for r in rows]

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
    try: tags=json.loads(base[0][3]) if base[0][3] else []
    except: tags=base[0][3].split(',') if base[0][3] else []
    return {"name":base[0][0],"type":base[0][1],"summary":base[0][2],
            "tags":tags,
            "force_profile":_entity_force_profile(base[0][0] or "", base[0][2] or "", json.dumps(tags,ensure_ascii=False)),
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

@app.get("/missions/{mid}/entity-context")
def get_path_entity_context(mid: str, entity_name: str, u: dict = Depends(current_user)):
    uid = _uid(u)
    mrows=kuzu_rows(_conn.execute(
        "MATCH (m:Mission) WHERE m.id=$id AND m.user_id=$uid RETURN m.title,m.description",
        {"id":mid,"uid":uid}))
    if not mrows: raise HTTPException(404)
    eid=_entity_id(entity_name, uid)
    erows=kuzu_rows(_conn.execute(
        "MATCH (e:Entity) WHERE e.id=$id AND e.user_id=$uid RETURN e.name,e.type,e.summary",
        {"id":eid,"uid":uid}))
    if not erows: raise HTTPException(404, "entity not found")
    cid=_path_entity_context_id(mid, eid, uid)
    crows=kuzu_rows(_conn.execute(
        "MATCH (pc:PathEntityContext) WHERE pc.id=$id AND pc.user_id=$uid RETURN pc.note,pc.ai_note,pc.updated_ts",
        {"id":cid,"uid":uid}))
    note,ai_note,updated=("", "", "")
    if crows:
        note,ai_note,updated=crows[0][0] or "", crows[0][1] or "", crows[0][2] or ""
    return {"mission":{"id":mid,"title":mrows[0][0],"description":mrows[0][1] or ""},
            "entity":{"id":eid,"name":erows[0][0],"type":erows[0][1],"summary":erows[0][2] or ""},
            "note":note,"ai_note":ai_note,"updated_ts":updated}

@app.post("/missions/{mid}/entity-context")
def save_path_entity_context(mid: str, req: PathEntityContextReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    mrows=kuzu_rows(_conn.execute(
        "MATCH (m:Mission) WHERE m.id=$id AND m.user_id=$uid RETURN m.title,m.description",
        {"id":mid,"uid":uid}))
    if not mrows: raise HTTPException(404)
    name=req.entity_name.strip()
    if not name: raise HTTPException(400, "entity_name required")
    eid=_entity_id(name, uid)
    if not entity_exists(eid, uid):
        _conn.execute("CREATE (:Entity {id:$id,name:$n,type:'concept',summary:'',tags:'[]',user_id:$uid})",
                      {"id":eid,"n":name,"uid":uid})
    erows=kuzu_rows(_conn.execute(
        "MATCH (e:Entity) WHERE e.id=$id AND e.user_id=$uid RETURN e.name,e.type,e.summary",
        {"id":eid,"uid":uid}))
    entity_name,etype,summary=erows[0][0],erows[0][1],erows[0][2] or ""
    cid=_path_entity_context_id(mid, eid, uid)
    old=kuzu_rows(_conn.execute(
        "MATCH (pc:PathEntityContext) WHERE pc.id=$id AND pc.user_id=$uid RETURN pc.ai_note",
        {"id":cid,"uid":uid}))
    ai_note=old[0][0] if old else ""
    note=req.note.strip()
    if req.improve and note and _has_any_ai():
        prompt=f"""Ты — стратег Life RPG. Улучши заметку о сущности внутри конкретного Пути.
Сделай её практичной: что публиковать/делать, зачем это нужно Пути, какие следующие действия.
Пиши кратко, по-русски, без воды.

Путь: {mrows[0][0]}
Описание пути: {mrows[0][1] or ""}
Сущность: {entity_name} ({etype})
Сводка сущности: {summary}
Заметка игрока: {note}
"""
        ai_note=_call_any_ai(prompt).strip()
    ts=_now_s()
    if old:
        _conn.execute(
            "MATCH (pc:PathEntityContext) WHERE pc.id=$id AND pc.user_id=$uid SET pc.note=$note,pc.ai_note=$ai,pc.updated_ts=$ts",
            {"id":cid,"uid":uid,"note":note,"ai":ai_note or "","ts":ts})
    else:
        _conn.execute(
            "CREATE (:PathEntityContext {id:$id,mission_id:$mid,entity_id:$eid,note:$note,ai_note:$ai,updated_ts:$ts,user_id:$uid})",
            {"id":cid,"mid":mid,"eid":eid,"note":note,"ai":ai_note or "","ts":ts,"uid":uid})
    return {"ok":True,"note":note,"ai_note":ai_note or "","updated_ts":ts,
            "entity":{"id":eid,"name":entity_name,"type":etype,"summary":summary}}

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
        _ensure_mission_quest_engine(mid, uid)
        branches_rows=kuzu_rows(_conn.execute(
            "MATCH (b:QuestBranch) WHERE b.mission_id=$mid AND b.user_id=$uid "
            "RETURN b.id,b.title,b.status,b.position,b.ts ORDER BY b.position,b.ts",
            {"mid":mid,"uid":uid}))
        branches=[{"id":b[0],"title":b[1],"status":b[2],"position":int(b[3] or 0),"ts":b[4]} for b in branches_rows]
        tasks=kuzu_rows(_conn.execute(
            "MATCH (t:Task) WHERE t.mission_id=$mid AND t.user_id=$uid "
            "RETURN t.id,t.title,t.status,t.ts,"
            "t.task_type,t.reset_hours,t.required_iters,t.current_iters,"
            "t.last_reset_ts,t.streak,t.best_streak,t.completed_ts,"
            "t.quest_kind,t.branch_id,t.parent_id,t.position,t.progress_mode,t.target_value,t.progress_value,"
            "t.timer_total_seconds,t.timer_started_ts,t.unlock_rule,t.unlock_payload,t.locked,t.notes,"
            "t.is_current,t.quest_description,t.description_updated_ts,t.record_enabled,t.record_value,t.record_label,"
            "t.timer_record_mode,t.timer_period_hours,t.timer_period_started_ts,t.timer_period_seconds,"
            "t.timer_last_period_seconds,t.timer_best_period_seconds,t.timer_last_session_seconds,t.timer_best_session_seconds "
            "ORDER BY t.position,t.ts",
            {"mid":mid,"uid":uid}))
        task_list=[]
        for t in tasks:
            pmode=t[16] or ("count" if (t[4] or "")=="repeat" else "check")
            raw_progress=t[18]
            progress_value=float(raw_progress if raw_progress is not None else (0 if pmode == "timed_sessions" else (t[7] or 0)))
            td={"id":t[0],"title":t[1],"status":t[2],"ts":t[3],
                "task_type":t[4] or "once","reset_hours":int(t[5] or 24),
                "required_iters":int(t[6] or 1),"current_iters":int(t[7] or 0),
                "last_reset_ts":t[8] or "","streak":int(t[9] or 0),"best_streak":int(t[10] or 0),
                "completed_ts":t[11] or "","quest_kind":t[12] or ("ritual" if (t[4] or "")=="repeat" else "task"),
                "branch_id":t[13] or _default_branch_id(mid),"parent_id":t[14] or "",
                "position":int(t[15] or 0),"progress_mode":pmode,
                "target_value":float(t[17] or t[6] or 1),"progress_value":progress_value,
                "timer_total_seconds":int(t[19] or 0),
                "timer_started_ts":t[20] or "","unlock_rule":t[21] or "",
                "unlock_payload":t[22] or "","locked":(t[23] or "false")=="true","notes":t[24] or "",
                "is_current":(t[25] or "false")=="true","quest_description":t[26] or "",
                "description_updated_ts":t[27] or "",
                "record_enabled":(t[28] or "false")=="true","record_value":float(t[29] or 0),
                "record_label":t[30] or "",
                "timer_record_mode":t[31] or ("session" if (t[28] or "false")=="true" and (t[12] or "")=="timer" else "none"),
                "timer_period_hours":int(t[32] or 24),"timer_period_started_ts":t[33] or "",
                "timer_period_seconds":int(t[34] or 0),
                "timer_last_period_seconds":int(t[35] or 0),
                "timer_best_period_seconds":int(t[36] or 0),
                "timer_last_session_seconds":int(t[37] or 0),
                "timer_best_session_seconds":int(t[38] or t[29] or 0)}
            td=_sync_timer_period_state(td, uid)
            td["timer_current_period_seconds"]=_timer_current_period_effective(td)
            if td.get("timer_record_mode") == "period":
                td["timer_best_period_seconds"]=max(int(td.get("timer_best_period_seconds") or 0),
                                                    int(td.get("timer_current_period_seconds") or 0))
            td["timer_total_seconds"]=_timer_effective_seconds(int(td.get("timer_total_seconds") or 0), td.get("timer_started_ts") or "")
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
                        "lore":r[5] or "","branches":branches,"tasks":task_list,"entities":entity_tags})
    return result

class MissionDescReq(BaseModel):
    description: str

@app.post("/missions/{mid}/branches")
def add_branch(mid: str, req: BranchReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    exists=kuzu_rows(_conn.execute("MATCH (m:Mission) WHERE m.id=$id AND m.user_id=$uid RETURN m.id",
                                   {"id":mid,"uid":uid}))
    if not exists: raise HTTPException(404)
    title=req.title.strip() or "Новая ветвь"
    rows=kuzu_rows(_conn.execute(
        "MATCH (b:QuestBranch) WHERE b.mission_id=$mid AND b.user_id=$uid RETURN count(b)",
        {"mid":mid,"uid":uid}))
    pos=int(rows[0][0]) if rows else 0
    bid=str(uuid.uuid4())
    _conn.execute(
        "CREATE (:QuestBranch {id:$id,mission_id:$mid,title:$title,status:'active',position:$pos,ts:$ts,user_id:$uid})",
        {"id":bid,"mid":mid,"title":title,"pos":pos,"ts":_now_s(),"uid":uid})
    return {"id":bid,"title":title,"status":"active","position":pos}

@app.post("/branches/{bid}/update")
def update_branch(bid: str, req: BranchReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    title=req.title.strip()
    if title:
        _conn.execute("MATCH (b:QuestBranch) WHERE b.id=$id AND b.user_id=$uid SET b.title=$title",
                      {"id":bid,"uid":uid,"title":title})
    return {"ok":True}

@app.post("/branches/{bid}/delete")
def delete_branch(bid: str, u: dict = Depends(current_user)):
    uid = _uid(u)
    rows=kuzu_rows(_conn.execute(
        "MATCH (b:QuestBranch) WHERE b.id=$id AND b.user_id=$uid RETURN b.mission_id,b.title",
        {"id":bid,"uid":uid}))
    if not rows: raise HTTPException(404)
    mid,title=rows[0]
    if bid == _default_branch_id(mid):
        raise HTTPException(400, "Основную ветвь нельзя удалить")
    default_bid=_ensure_default_branch(mid, uid)
    _conn.execute(
        "MATCH (t:Task) WHERE t.branch_id=$bid AND t.user_id=$uid SET t.branch_id=$default_bid",
        {"bid":bid,"uid":uid,"default_bid":default_bid})
    _conn.execute("MATCH (b:QuestBranch) WHERE b.id=$id AND b.user_id=$uid DELETE b",
                  {"id":bid,"uid":uid})
    return {"ok":True,"moved_to":default_bid,"deleted":title}

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
    bid=_ensure_default_branch(mid, uid)
    return {"id":mid,"title":req.title,"status":"active","branches":[{"id":bid,"title":"Основная","status":"active","position":0,"ts":ts}],"tasks":[],"entities":[]}

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
    try: _conn.execute("MATCH (ev:QuestEvent) WHERE ev.mission_id=$id AND ev.user_id=$uid DELETE ev",{"id":mid,"uid":uid})
    except: pass
    try: _conn.execute("MATCH (b:QuestBranch) WHERE b.mission_id=$id AND b.user_id=$uid DELETE b",{"id":mid,"uid":uid})
    except: pass
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
    branch_id=req.branch_id or _ensure_default_branch(req.mission_id, uid)
    kind=req.quest_kind or ("ritual" if req.task_type == "repeat" else "task")
    progress_mode=req.progress_mode or ("count" if req.task_type == "repeat" else "check")
    task_type = "repeat" if kind == "ritual" or req.task_type == "repeat" else "once"
    timer_record_mode = (req.timer_record_mode or "none").strip().lower()
    if timer_record_mode not in ("none","session","period"):
        timer_record_mode = "none"
    if kind == "timer" and req.record_enabled and timer_record_mode == "none":
        timer_record_mode = "session"
    if kind != "timer":
        timer_record_mode = "none"
    timer_period_hours = max(1, int(req.timer_period_hours or 24))
    timer_period_started_ts = ts if timer_record_mode == "period" else ""
    init_reset = ts if task_type == "repeat" else ""
    target_value = float(req.target_value or req.required_iters or 1)
    if task_type == "repeat":
        if progress_mode == "timed_sessions":
            target_value = float(req.target_value or 5400)
        else:
            target_value = float(req.required_iters or target_value or 1)
    pos_rows=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.mission_id=$mid AND t.branch_id=$bid AND t.user_id=$uid RETURN count(t)",
        {"mid":req.mission_id,"bid":branch_id,"uid":uid}))
    position=int(pos_rows[0][0]) if pos_rows else 0
    _conn.execute(
        "CREATE (:Task {id:$id,mission_id:$mid,title:$t,status:'active',ts:$ts,entry_id:'',"
        "task_type:$tt,reset_hours:$rh,required_iters:$ri,"
        "current_iters:0,last_reset_ts:$lr,streak:0,best_streak:0,completed_ts:'',"
        "quest_kind:$kind,branch_id:$bid,parent_id:$parent,position:$position,progress_mode:$pmode,"
        "target_value:$target,progress_value:0,timer_total_seconds:$timer,timer_started_ts:'',"
        "unlock_rule:$unlock_rule,unlock_payload:$unlock_payload,locked:$locked,notes:$notes,"
        "is_current:$current,quest_description:$qdesc,description_updated_ts:$dts,"
        "record_enabled:$record_enabled,record_value:0,record_label:$record_label,"
        "timer_record_mode:$timer_record_mode,timer_period_hours:$timer_period_hours,"
        "timer_period_started_ts:$timer_period_started_ts,timer_period_seconds:0,"
        "timer_last_period_seconds:0,timer_best_period_seconds:0,"
        "timer_last_session_seconds:0,timer_best_session_seconds:0,user_id:$uid})",
        {"id":tid,"mid":req.mission_id,"t":req.title,"ts":ts,
         "tt":task_type,"rh":req.reset_hours,"ri":req.required_iters,"lr":init_reset,
         "kind":kind,"bid":branch_id,"parent":req.parent_id,"position":position,
         "pmode":progress_mode,"target":target_value,"timer":int(req.timer_total_seconds or 0),
         "unlock_rule":req.unlock_rule,"unlock_payload":req.unlock_payload,
         "locked":"true" if req.locked else "false","notes":req.notes,
         "current":"true" if req.is_current else "false","qdesc":req.quest_description,
         "dts":_now_s() if req.quest_description else "",
         "record_enabled":"true" if (req.record_enabled or timer_record_mode != "none") else "false",
         "record_label":req.record_label,"timer_record_mode":timer_record_mode,
         "timer_period_hours":timer_period_hours,"timer_period_started_ts":timer_period_started_ts,"uid":uid})
    _record_quest_event(tid, req.mission_id, uid, "created", 0, kind)
    return {"id":tid,"title":req.title,"status":"active","task_type":task_type,"quest_kind":kind,"branch_id":branch_id}

class TaskParamsReq(BaseModel):
    task_type: str = "once"; reset_hours: int = 24; required_iters: int = 1
class TaskProgressReq(BaseModel):
    delta: float = 0
    value: float | None = None
    note: str = ""
class TaskNoteReq(BaseModel):
    note: str = ""
class TaskCardReq(BaseModel):
    description: str = ""
    improve: bool = False
class TaskCurrentReq(BaseModel):
    is_current: bool = True
class TaskTimerRecordReq(BaseModel):
    mode: str = "none"
    period_hours: int = 24
class TaskRitualSettingsReq(BaseModel):
    progress_mode: str = "count"
    required_iters: int = 1
    reset_hours: int = 24
    session_minutes: int = 90
class TaskMoveReq(BaseModel):
    branch_id: str
    parent_id: str = ""
    before_task_id: str = ""

def _task_descendant_ids(task_id: str, user_id: str) -> list[str]:
    ids=[]; queue=[task_id]
    while queue:
        parent=queue.pop(0)
        rows=kuzu_rows(_conn.execute(
            "MATCH (t:Task) WHERE t.parent_id=$pid AND t.user_id=$uid RETURN t.id",
            {"pid":parent,"uid":user_id}))
        for r in rows:
            cid=r[0]
            if cid not in ids:
                ids.append(cid)
                queue.append(cid)
    return ids

@app.post("/tasks/{tid}/set-params")
def set_task_params(tid: str, req: TaskParamsReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    _conn.execute(
        "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET t.task_type=$tt,t.reset_hours=$rh,t.required_iters=$ri",
        {"id":tid,"uid":uid,"tt":req.task_type,"rh":req.reset_hours,"ri":req.required_iters})
    return {"ok":True}

@app.get("/tasks/{tid}/card")
def get_task_card(tid: str, u: dict = Depends(current_user)):
    uid = _uid(u)
    snap=_quest_snapshot(tid, uid)
    if not snap: raise HTTPException(404)
    desc=snap["task"].get("quest_description") or _quest_description_fallback(snap)
    return {"task":snap["task"],"mission":snap["mission"],"branch":snap["branch"],
            "entities":snap.get("entities",[]),"description":desc,
            "ai_available":_has_any_ai()}

@app.post("/tasks/{tid}/card")
def save_task_card(tid: str, req: TaskCardReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    snap=_quest_snapshot(tid, uid)
    if not snap: raise HTTPException(404)
    desc=req.description.strip()
    if req.improve:
        desc=_generate_quest_description(snap, uid, desc)
    elif not desc:
        desc=_quest_description_fallback(snap)
    _conn.execute(
        "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET t.quest_description=$d,t.description_updated_ts=$ts",
        {"id":tid,"uid":uid,"d":desc,"ts":_now_s()})
    snap["task"]["quest_description"]=desc
    return {"ok":True,"description":desc}

@app.post("/tasks/{tid}/current")
def set_task_current(tid: str, req: TaskCurrentReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    rows=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid RETURN t.mission_id",
        {"id":tid,"uid":uid}))
    if not rows: raise HTTPException(404)
    _conn.execute("MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET t.is_current=$cur",
                  {"id":tid,"uid":uid,"cur":"true" if req.is_current else "false"})
    return {"ok":True,"is_current":req.is_current}

@app.get("/tasks/{tid}/timer-record")
def get_timer_record(tid: str, u: dict = Depends(current_user)):
    uid = _uid(u)
    snap=_quest_snapshot(tid, uid)
    if not snap: raise HTTPException(404)
    task=snap["task"]
    if task.get("quest_kind") != "timer":
        raise HTTPException(400, "Это не таймер")
    return {"task":task}

@app.post("/tasks/{tid}/timer-record")
def set_timer_record(tid: str, req: TaskTimerRecordReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    mode=(req.mode or "none").strip().lower()
    if mode not in ("none","session","period"):
        raise HTTPException(400, "Неверный режим рекорда")
    hours=max(1, int(req.period_hours or 24))
    rows=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid RETURN t.quest_kind,t.timer_record_mode,t.timer_period_hours,t.record_value,t.timer_best_session_seconds",
        {"id":tid,"uid":uid}))
    if not rows: raise HTTPException(404)
    kind,old_mode,old_hours,old_record,old_best_session=rows[0]
    if (kind or "") != "timer":
        _conn.execute("MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET t.quest_kind='timer',t.progress_mode='timer'",
                      {"id":tid,"uid":uid})
    now_s=_now_s()
    record_enabled="true" if mode != "none" else "false"
    best_session=int(old_best_session or old_record or 0)
    if mode == "period":
        _conn.execute(
            "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET "
            "t.timer_record_mode='period',t.timer_period_hours=$hours,t.record_enabled='true',"
            "t.timer_period_started_ts=$started,t.timer_period_seconds=0,"
            "t.timer_last_period_seconds=$last,t.timer_best_period_seconds=$best,t.record_value=$record",
            {"id":tid,"uid":uid,"hours":hours,
             "started":now_s,"last":0,"best":0,"record":0.0})
    else:
        record_value=float(best_session if mode == "session" else 0)
        _conn.execute(
            "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET "
            "t.timer_record_mode=$mode,t.timer_period_hours=$hours,t.record_enabled=$enabled,"
            "t.record_value=$record,t.timer_best_session_seconds=$best_session",
            {"id":tid,"uid":uid,"mode":mode,"hours":hours,"enabled":record_enabled,
             "record":record_value,"best_session":best_session})
    snap=_quest_snapshot(tid, uid)
    return {"ok":True,"task":snap["task"] if snap else {}}

@app.post("/tasks/{tid}/ritual-settings")
def set_ritual_settings(tid: str, req: TaskRitualSettingsReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    mode=(req.progress_mode or "count").strip().lower()
    if mode not in ("count","timed_sessions"):
        raise HTTPException(400, "Неверный режим ритуала")
    required=max(1, int(req.required_iters or 1))
    reset_hours=max(1, int(req.reset_hours or 24))
    session_seconds=max(60, int(req.session_minutes or 90) * 60)
    rows=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid RETURN t.last_reset_ts,t.timer_started_ts,t.current_iters",
        {"id":tid,"uid":uid}))
    if not rows: raise HTTPException(404)
    last_reset=rows[0][0] or _now_s()
    timer_started=rows[0][1] or ""
    current_iters=int(rows[0][2] or 0)
    target=float(session_seconds if mode == "timed_sessions" else required)
    progress_value=0.0 if mode == "timed_sessions" else float(current_iters)
    updates = (
        "t.task_type='repeat',t.quest_kind='ritual',t.progress_mode=$mode,"
        "t.required_iters=$required,t.reset_hours=$reset,t.target_value=$target,"
        "t.progress_value=$progress,t.last_reset_ts=$last_reset"
    )
    params={"id":tid,"uid":uid,"mode":mode,"required":required,"reset":reset_hours,
            "target":target,"progress":progress_value,"last_reset":last_reset}
    if mode == "count":
        updates += ",t.timer_started_ts=''"
    elif not timer_started:
        updates += ",t.timer_started_ts=''"
    _conn.execute(f"MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET {updates}", params)
    snap=_quest_snapshot(tid, uid)
    return {"ok":True,"task":snap["task"] if snap else {}}

@app.post("/tasks/{tid}/move")
def move_task(tid: str, req: TaskMoveReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    task_rows=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid RETURN t.mission_id,t.parent_id,t.branch_id",
        {"id":tid,"uid":uid}))
    if not task_rows: raise HTTPException(404)
    mid,old_parent,old_branch=task_rows[0]
    branch_rows=kuzu_rows(_conn.execute(
        "MATCH (b:QuestBranch) WHERE b.id=$id AND b.user_id=$uid RETURN b.mission_id",
        {"id":req.branch_id,"uid":uid}))
    if not branch_rows: raise HTTPException(404, "Ветвь не найдена")
    if branch_rows[0][0] != mid:
        raise HTTPException(400, "Квест нельзя перенести в другой Путь")

    parent_id=req.parent_id or ""
    subtree=[tid]+_task_descendant_ids(tid, uid)
    if parent_id:
        if parent_id in subtree:
            raise HTTPException(400, "Нельзя вложить квест в самого себя")
        parent_rows=kuzu_rows(_conn.execute(
            "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid RETURN t.mission_id",
            {"id":parent_id,"uid":uid}))
        if not parent_rows or parent_rows[0][0] != mid:
            raise HTTPException(400, "Родительский квест не найден")

    siblings=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.mission_id=$mid AND t.branch_id=$bid AND t.parent_id=$parent AND t.user_id=$uid "
        "RETURN t.id ORDER BY t.position,t.ts",
        {"mid":mid,"bid":req.branch_id,"parent":parent_id,"uid":uid}))
    order=[r[0] for r in siblings if r[0] != tid]
    before=req.before_task_id or ""
    if before and before != tid:
        if before not in order:
            raise HTTPException(400, "Целевое место не найдено")
        order.insert(order.index(before), tid)
    else:
        order.append(tid)

    for moved_id in subtree:
        _conn.execute("MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET t.branch_id=$bid",
                      {"id":moved_id,"uid":uid,"bid":req.branch_id})
    _conn.execute("MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET t.parent_id=$parent",
                  {"id":tid,"uid":uid,"parent":parent_id})
    for pos, task_id in enumerate(order):
        _conn.execute("MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET t.position=$pos",
                      {"id":task_id,"uid":uid,"pos":pos})
    _record_quest_event(tid, mid or "", uid, "moved", 0, f"{old_branch or ''}->{req.branch_id}")
    return {"ok":True,"branch_id":req.branch_id,"parent_id":parent_id,"position":order.index(tid)}

@app.post("/tasks/{tid}/tick")
def tick_task(tid: str, u: dict = Depends(current_user)):
    uid = _uid(u)
    rows=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid "
        "RETURN t.current_iters,t.required_iters,t.last_reset_ts,t.mission_id,t.progress_value",{"id":tid,"uid":uid}))
    if not rows: return {"error":"not found"}
    cur=int(rows[0][0] or 0); req=int(rows[0][1] or 1); lr=rows[0][2] or ""; mid=rows[0][3] or ""
    new_cur=min(cur+1,req)
    new_progress=float((rows[0][4] or 0) + 1)
    now_s=datetime.now().strftime("%Y-%m-%d %H:%M")
    if not lr: lr=now_s
    cts=now_s if new_cur>=req else ""
    _conn.execute("MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET t.current_iters=$c,t.progress_value=$p,t.last_reset_ts=$lr,t.completed_ts=$cts",
                  {"id":tid,"uid":uid,"c":new_cur,"p":new_progress,"lr":lr,"cts":cts})
    _record_quest_event(tid, mid, uid, "tick", 1)
    _write_quest_journal_event(tid, uid, "tick", 1)
    return {"current":new_cur,"required":req,"completed":new_cur>=req}

@app.post("/tasks/{tid}/progress")
def update_task_progress(tid: str, req: TaskProgressReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    rows=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid "
        "RETURN t.mission_id,t.progress_value,t.target_value,t.required_iters,t.progress_mode,t.record_enabled,t.record_value",
        {"id":tid,"uid":uid}))
    if not rows: raise HTTPException(404)
    mid,current,target,required,mode,record_enabled,record_value=rows[0]
    new_value=float(req.value if req.value is not None else (current or 0) + req.delta)
    target=float(target or required or 1)
    completed = bool(target and new_value >= target and mode != "timer")
    now_s=_now_s() if completed else ""
    _conn.execute(
        "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET t.progress_value=$v,t.completed_ts=$cts",
        {"id":tid,"uid":uid,"v":new_value,"cts":now_s})
    if (record_enabled or "false")=="true" and new_value > float(record_value or 0):
        _conn.execute("MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET t.record_value=$v",
                      {"id":tid,"uid":uid,"v":new_value})
    if completed:
        _conn.execute("MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET t.status='done',t.is_current='false'",
                      {"id":tid,"uid":uid})
    _record_quest_event(tid, mid or "", uid, "progress", float(req.delta or 0), req.note)
    _write_quest_journal_event(tid, uid, "progress", new_value)
    return {"ok":True,"progress_value":new_value,"completed":completed}

@app.post("/tasks/{tid}/timer/start")
def start_task_timer(tid: str, u: dict = Depends(current_user)):
    uid = _uid(u)
    rows=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid RETURN "
        "t.mission_id,t.timer_started_ts,t.quest_kind,t.progress_mode,t.current_iters,t.required_iters",
        {"id":tid,"uid":uid}))
    if not rows: raise HTTPException(404)
    mid,started,kind,mode,cur,req=rows[0]
    if (mode or "") == "timed_sessions" and int(cur or 0) >= int(req or 1):
        return {"ok":True,"already_complete":True}
    if not started:
        if (kind or "") == "ritual" or (mode or "") == "timed_sessions":
            _conn.execute(
                "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET t.timer_started_ts=$ts,t.task_type='repeat',t.quest_kind='ritual'",
                {"id":tid,"uid":uid,"ts":_timer_now_s()})
        else:
            _conn.execute(
                "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET t.timer_started_ts=$ts,t.quest_kind='timer',t.progress_mode='timer'",
                {"id":tid,"uid":uid,"ts":_timer_now_s()})
        _record_quest_event(tid, mid or "", uid, "timer_started", 0)
    return {"ok":True}

@app.post("/tasks/{tid}/timer/stop")
def stop_task_timer(tid: str, u: dict = Depends(current_user)):
    uid = _uid(u)
    rows=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid RETURN "
        "t.mission_id,t.timer_total_seconds,t.timer_started_ts,t.record_enabled,t.record_value,"
        "t.timer_record_mode,t.timer_period_hours,t.timer_period_started_ts,t.timer_period_seconds,"
        "t.timer_last_period_seconds,t.timer_best_period_seconds,t.timer_last_session_seconds,t.timer_best_session_seconds,"
        "t.quest_kind,t.progress_mode,t.current_iters,t.required_iters,t.target_value,t.progress_value,t.last_reset_ts",
        {"id":tid,"uid":uid}))
    if not rows: raise HTTPException(404)
    (mid,total,started,record_enabled,record_value,mode,period_hours,period_started,period_seconds,
     last_period,best_period,last_session,best_session,quest_kind,progress_mode,current_iters,required_iters,
     target_value,progress_value,last_reset_ts)=rows[0]
    task={"id":tid,"timer_total_seconds":int(total or 0),"timer_started_ts":started or "",
          "record_enabled":(record_enabled or "false")=="true","record_value":float(record_value or 0),
          "timer_record_mode":mode or ("session" if (record_enabled or "false")=="true" else "none"),
          "timer_period_hours":int(period_hours or 24),"timer_period_started_ts":period_started or "",
          "timer_period_seconds":int(period_seconds or 0),
          "timer_last_period_seconds":int(last_period or 0),
          "timer_best_period_seconds":int(best_period or 0),
          "timer_last_session_seconds":int(last_session or 0),
          "timer_best_session_seconds":int(best_session or record_value or 0)}
    task=_sync_timer_period_state(task, uid)
    total=int(task.get("timer_total_seconds") or 0)
    started=task.get("timer_started_ts") or ""
    new_total=_timer_effective_seconds(int(total or 0), started or "")
    session_delta=max(0, new_total - int(total or 0))
    mode=task.get("timer_record_mode") or "none"
    best_session=max(int(task.get("timer_best_session_seconds") or 0), int(session_delta))
    period_seconds=int(task.get("timer_period_seconds") or 0)
    best_period=int(task.get("timer_best_period_seconds") or 0)
    record_value=float(task.get("record_value") or 0)
    if mode == "period":
        period_seconds += int(session_delta)
        best_period=max(best_period, period_seconds)
        record_value=float(best_period)
    elif mode == "session":
        record_value=float(best_session)
    is_timed_ritual = (quest_kind or "") == "ritual" and (progress_mode or "") == "timed_sessions"
    new_iters = int(current_iters or 0)
    new_progress = float(progress_value or 0)
    completed_ts = ""
    if is_timed_ritual:
        target_seconds = max(1, int(float(target_value or 5400)))
        required = max(1, int(required_iters or 1))
        available = max(0, int(new_progress) + int(session_delta))
        gained = min(max(0, required - new_iters), available // target_seconds)
        if gained > 0:
            new_iters += int(gained)
            available -= int(gained) * target_seconds
        new_progress = float(0 if new_iters >= required else available)
        completed_ts = _now_s() if new_iters >= required else ""
        if not last_reset_ts:
            last_reset_ts = _now_s()
    _conn.execute(
        "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET "
        "t.timer_total_seconds=$total,t.timer_started_ts='',t.progress_value=$hours,"
        "t.timer_last_session_seconds=$last_session,t.timer_best_session_seconds=$best_session,"
        "t.timer_period_seconds=$period_seconds,t.timer_best_period_seconds=$best_period,t.record_value=$record",
        {"id":tid,"uid":uid,"total":new_total,"hours":round(new_total/3600, 3),
         "last_session":int(session_delta),"best_session":best_session,
         "period_seconds":period_seconds,"best_period":best_period,"record":record_value})
    if is_timed_ritual:
        _conn.execute(
            "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET "
            "t.current_iters=$iters,t.progress_value=$progress,t.last_reset_ts=$reset,t.completed_ts=$completed",
            {"id":tid,"uid":uid,"iters":new_iters,"progress":new_progress,
             "reset":last_reset_ts or _now_s(),"completed":completed_ts})
    _record_quest_event(tid, mid or "", uid, "timer_stopped", float(session_delta))
    _write_quest_journal_event(tid, uid, "timer_stopped", float(session_delta))
    return {"ok":True,"timer_total_seconds":new_total,"last_session_seconds":int(session_delta),
            "best_session_seconds":best_session,"current_period_seconds":period_seconds,
            "best_period_seconds":best_period,"current_iters":new_iters,
            "progress_value":new_progress}

@app.post("/tasks/{tid}/note")
def save_task_note(tid: str, req: TaskNoteReq, u: dict = Depends(current_user)):
    uid = _uid(u)
    rows=kuzu_rows(_conn.execute(
        "MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid RETURN t.mission_id,t.notes",
        {"id":tid,"uid":uid}))
    if not rows: raise HTTPException(404)
    mid,old=rows[0]
    note=req.note.strip()
    combined=((old or "") + ("\n" if old and note else "") + note).strip()
    _conn.execute("MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET t.notes=$notes",
                  {"id":tid,"uid":uid,"notes":combined})
    if note:
        _record_quest_event(tid, mid or "", uid, "note", 0, note)
    return {"ok":True,"notes":combined}

@app.post("/tasks/{tid}/complete")
def complete_task(tid: str, u: dict = Depends(current_user)):
    uid = _uid(u)
    now_s=datetime.now().strftime("%Y-%m-%d %H:%M")
    rows=kuzu_rows(_conn.execute("MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid RETURN t.mission_id",
                                 {"id":tid,"uid":uid}))
    _conn.execute("MATCH (t:Task) WHERE t.id=$id AND t.user_id=$uid SET t.status='done',t.completed_ts=$ts,t.is_current='false'",
                  {"id":tid,"uid":uid,"ts":now_s})
    _record_quest_event(tid, rows[0][0] if rows else "", uid, "completed", 1)
    _write_quest_journal_event(tid, uid, "completed", 1)
    return {"ok":True}

@app.post("/tasks/{tid}/delete")
def delete_task(tid: str, u: dict = Depends(current_user)):
    uid = _uid(u)
    try: _conn.execute("MATCH (ev:QuestEvent) WHERE ev.task_id=$id AND ev.user_id=$uid DELETE ev",{"id":tid,"uid":uid})
    except: pass
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
    p=f"""Ты — тихий редактор дневника Life RPG. Одним абзацем (1-2 предложения) подведи живой итог сегодняшних квестов ОТ ПЕРВОГО ЛИЦА.
Стиль: ясный дневник Героя, не поэма и не религиозная проповедь.
Скрытый смысл: я учусь превращать действие из личной выгоды в пользу, точность, заботу и отдачу через дело.
Без слов "каббала", "Творец", "божественный", "святость". Без markdown и технических полей.

Задания сегодня:
{task_lines}

Верни только текст записи, без кавычек и заголовков."""
    text=_call_any_ai(p) if _has_any_ai() else ""
    return {"narrative":_clean_journal_text(text.strip())}

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
                 "epoch":"Эпоха","moon_name":"Луна сезона","stat":"Стат Героя",
                 "force":"Сила Героя"}
    label=type_labels.get(req.mechanic_type, req.mechanic_type)
    effect_line=f"\nЛорное значение: {req.mechanic_effect}" if req.mechanic_effect else ""

    if req.mechanic_type=="force":
        forces=_force_snapshot(uid)
        force_name="Получение" if req.mechanic_value=="receiving" else "Отдача"
        other_name="Отдача" if req.mechanic_value=="receiving" else "Получение"
        current=forces.get(req.mechanic_value,0)
        other=forces.get("giving" if req.mechanic_value=="receiving" else "receiving",0)
        nodes=", ".join(n["name"] for n in forces.get("top_nodes",[])[:4]) or "нет явных узлов"
        p=f"""Ты — наставник Life RPG. Это не мораль и не запрет. Говори как мастер, который показывает Герою силу выбранного пути.

Выбранная сила: {force_name} ({current}/100)
Другая сила: {other_name} ({other}/100)
Равновесие: {forces.get('balance')}
Узлы, через которые идут силы: {nodes}

Последние записи Героя:
{entry_lines}

Пути:
{mission_lines}

Дай наставление Пути {force_name}: 3-5 предложений, без списков, без "надо", без религиозных слов. Не запрещай противоположную силу. Покажи, как этой силой можно играть глубже и сильнее, чтобы Герой сам захотел сделать следующий ход."""
    elif req.mechanic_type=="stat":
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

@app.get("/forces")
def forces(u: dict = Depends(current_user)):
    return _force_snapshot(_uid(u))

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

/* ── LOGIN GATE ── */
#login-screen{display:none;position:fixed;inset:0;z-index:9999;align-items:center;justify-content:center;
  overflow:hidden;background:#080704;color:#e4c37a;font-family:'Georgia',serif}
#login-screen::before{content:"";position:absolute;inset:0;pointer-events:none;
  background:
    linear-gradient(rgba(173,124,38,.06) 1px,transparent 1px),
    linear-gradient(90deg,rgba(173,124,38,.06) 1px,transparent 1px),
    radial-gradient(circle at 50% 42%,rgba(155,105,28,.18),transparent 34%),
    #080704;
  background-size:24px 24px,24px 24px,100% 100%,100% 100%}
.login-pixels{position:absolute;inset:0;pointer-events:none;opacity:.9;
  background:
    linear-gradient(45deg,transparent 47%,rgba(204,151,55,.26) 48%,rgba(204,151,55,.26) 52%,transparent 53%) center/128px 128px no-repeat,
    linear-gradient(-45deg,transparent 47%,rgba(235,184,83,.18) 48%,rgba(235,184,83,.18) 52%,transparent 53%) center/128px 128px no-repeat,
    radial-gradient(circle at center,rgba(224,166,64,.16),transparent 34%)}
.login-shell{position:relative;z-index:2;width:min(92vw,760px);display:grid;grid-template-columns:minmax(0,1fr) 312px;
  gap:42px;align-items:center;padding:32px}
.login-sigil{justify-self:center;text-align:center;min-width:0}
.login-mark{font-size:clamp(34px,5vw,68px);line-height:.96;letter-spacing:6px;color:#efcf8a;
  text-shadow:3px 3px 0 #2c1d08}
.login-sub{margin-top:16px;font-family:sans-serif;font-size:10px;letter-spacing:7px;text-transform:uppercase;
  color:#9d7836}
.login-oath{margin:30px auto 0;max-width:410px;color:#caa35c;font-size:16px;line-height:1.75}
.login-oath span{color:#f1d9a3}
.login-panel{position:relative;background:#120d07;border:2px solid #70501e;box-shadow:6px 6px 0 #050403;
  padding:24px 26px 26px;border-radius:0}
.login-tabs{display:flex;margin-bottom:20px;border-bottom:1px solid #5a421b}
.login-tab{flex:1;padding:0 0 10px;border:none;background:transparent;color:#8e713a;
  font-family:'Georgia',serif;font-size:13px;cursor:pointer;letter-spacing:.7px}
.login-tab.active{color:#f2d59a}
.login-input{width:100%;background:#090704;border:1px solid #594019;color:#f2dfbb;
  font-family:'Georgia',serif;font-size:15px;padding:12px 13px;border-radius:0;outline:none;margin-bottom:11px}
.login-input:focus{border-color:#b48736}
.login-input::placeholder{color:#806537}
#ls-err{font-size:12px;color:#d87b62;font-family:sans-serif;min-height:18px;margin-bottom:9px}
#ls-btn{width:100%;border:1px solid #d3a14c;background:#8f651f;color:#f7e7bf;padding:12px;
  font-family:'Georgia',serif;font-size:14px;letter-spacing:2.6px;border-radius:0;cursor:pointer}
#ls-btn:hover{background:#a57427}
.login-hint{margin-top:16px;text-align:center;font-family:sans-serif;font-size:10px;letter-spacing:2px;text-transform:uppercase;
  color:#806537}
@media(max-width:820px){
  .login-shell{grid-template-columns:1fr;gap:28px;padding:28px 18px}
  .login-oath{display:none}
  .login-panel{width:min(100%,340px);justify-self:center}
}

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
.entry-stamp{font-size:10px;color:var(--ink3);font-family:sans-serif;margin-bottom:7px;
  letter-spacing:1px}
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
.aside-quest{padding:7px 10px 7px 12px;border-left:2px solid var(--gold);
  margin:5px 0;background:rgba(139,105,20,.06);cursor:pointer;border-radius:0 3px 3px 0}
.aside-quest:hover{background:rgba(139,105,20,.12)}
.aside-quest-title{font-size:12px;color:var(--ink);font-family:sans-serif;line-height:1.35}
.aside-quest-meta{font-size:10px;color:var(--ink3);font-family:sans-serif;margin-top:3px;line-height:1.35}
.aside-quest-actions{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:7px}
.aside-action{border:1px solid var(--border2);background:var(--paper);color:var(--ink2);
  font-family:sans-serif;font-size:10px;line-height:1;padding:4px 8px;border-radius:3px;
  cursor:pointer;min-height:22px}
.aside-action:hover{border-color:var(--gold);color:var(--gold)}
.aside-action.primary{background:var(--gold);border-color:var(--gold);color:#fff}
.aside-action.primary:hover{background:#a07820;color:#fff}
.aside-action.running{background:var(--red);border-color:var(--red);color:#fff}
.aside-action.done{background:var(--green);border-color:var(--green);color:#fff}
.aside-action:disabled{opacity:.45;cursor:default}
.aside-progress{height:4px;background:rgba(200,184,154,.42);border-radius:999px;
  overflow:hidden;margin-top:6px}
.aside-progress-fill{height:100%;background:linear-gradient(90deg,var(--blue),var(--gold));
  border-radius:999px;transition:width .25s}
.aside-entity{display:flex;align-items:center;gap:8px;padding:6px 0;cursor:pointer;
  border-bottom:.5px solid var(--border2)}
.aside-entity:last-child{border-bottom:none}
.aside-entity:hover .aside-entity-name{color:var(--blue)}
.aside-entity-ic{font-size:14px;flex-shrink:0}
.aside-entity-name{font-size:13px;color:var(--ink)}
.aside-entity-sub{font-size:10px;color:var(--ink3);font-family:sans-serif}

/* ── MISSIONS ── */
#s-missions{padding:0}
.missions-wrap{max-width:920px;margin:0 auto;padding:40px 40px 80px}
.missions-topbar{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:28px}
.missions-eyebrow{font-size:10px;letter-spacing:4px;text-transform:uppercase;
  color:var(--red);font-family:sans-serif}
.missions-heading{font-size:22px;color:var(--ink);margin-bottom:4px}
.mission-lore{font-size:12px;color:var(--ink3);font-style:italic;margin-top:3px;line-height:1.5;
  font-family:'Georgia',serif;opacity:.85}
.mission-block{margin-bottom:36px;padding-bottom:32px;border-bottom:1px solid var(--border2);position:relative}
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
.mission-more{position:relative;flex-shrink:0;margin-top:-3px}
.mission-menu-btn{width:28px;height:28px;border:1px solid transparent;background:none;
  color:var(--ink3);font-family:sans-serif;font-size:18px;line-height:1;border-radius:4px;
  cursor:pointer;display:flex;align-items:center;justify-content:center}
.mission-menu-btn:hover{border-color:var(--border2);color:var(--ink)}
.mission-menu{display:none;position:absolute;right:0;top:31px;z-index:30;background:var(--paper);
  border:1px solid var(--border);border-radius:4px;box-shadow:0 8px 24px rgba(44,35,24,.18);
  min-width:150px;padding:5px}
.mission-menu.open{display:block}
.mission-menu button{width:100%;background:none;border:none;text-align:left;padding:7px 9px;
  font-family:sans-serif;font-size:12px;color:var(--ink2);border-radius:3px;cursor:pointer}
.mission-menu button:hover{background:rgba(139,105,20,.08);color:var(--ink)}
.mission-menu button.danger{color:var(--red)}
.mission-desc-text{font-size:13px;color:var(--ink3);font-family:sans-serif;
  line-height:1.65;margin:10px 0 0 30px}
.quest-chain{margin:18px 0 0 28px;padding-left:16px;
  border-left:1.5px solid var(--border2);display:none}
.quest-chain.open{display:block}
.quest-branch{margin-bottom:18px}
.quest-branch:last-child{margin-bottom:0}
.quest-branch-head{display:flex;align-items:center;justify-content:space-between;gap:12px;
  padding:0 0 8px;border-bottom:1px solid rgba(200,184,154,.55)}
.quest-branch-title{font-size:11px;letter-spacing:2px;text-transform:uppercase;
  color:var(--ink3);font-family:sans-serif;font-weight:700}
.quest-branch-count{font-size:10px;color:var(--border);font-family:sans-serif;margin-left:8px}
.quest-branch-actions{display:flex;align-items:center;gap:6px;flex-shrink:0}
.quest-branch-btn{background:none;border:1px dashed var(--border2);color:var(--ink3);
  font-family:sans-serif;font-size:11px;padding:3px 10px;border-radius:3px;cursor:pointer}
.quest-branch-btn:hover{border-color:var(--gold);color:var(--gold)}
.quest-branch-body{padding-top:7px;min-height:28px;border-radius:4px;transition:background .12s,box-shadow .12s}
.quest-branch-body.drag-over{background:rgba(139,105,20,.07);box-shadow:inset 0 0 0 1px rgba(139,105,20,.25)}
.quest-empty{font-size:12px;color:var(--ink3);font-family:sans-serif;font-style:italic;
  padding:10px 0}
.quest-node{position:relative}
.quest-node[draggable="true"]{cursor:grab}
.quest-node.dragging{opacity:.45}
.quest-node.current>.quest-item{background:rgba(139,105,20,.07)}
.quest-children{margin-left:22px;padding-left:12px;border-left:1px solid rgba(200,184,154,.6)}
.quest-item{display:flex;align-items:flex-start;gap:10px;padding:9px 0;
  border-bottom:.5px solid rgba(200,164,122,.25);transition:background .12s,box-shadow .12s}
.quest-item.drop-before{box-shadow:inset 0 2px 0 var(--gold);background:rgba(139,105,20,.05)}
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
.quest-meta{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:2px}
.quest-kind{font-size:9px;font-family:sans-serif;padding:1px 7px;border-radius:8px;letter-spacing:.7px;
  border:1px solid rgba(139,105,20,.25);background:rgba(139,105,20,.08);color:var(--gold)}
.quest-kind.timer{border-color:rgba(26,74,107,.25);background:rgba(26,74,107,.08);color:var(--blue)}
.quest-kind.counter{border-color:rgba(45,92,20,.25);background:rgba(45,92,20,.08);color:var(--green)}
.quest-note-preview{font-size:10px;color:var(--ink3);font-family:sans-serif;margin-top:4px;
  line-height:1.45;white-space:pre-wrap}
.quest-tools{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:6px}
.quest-tool{background:none;border:1px solid var(--border2);color:var(--ink3);
  font-family:sans-serif;font-size:10px;padding:2px 8px;border-radius:3px;cursor:pointer}
.quest-tool:hover{border-color:var(--gold);color:var(--gold)}
.quest-tool.primary{background:var(--gold);border-color:var(--gold);color:#fff}
.quest-tool.primary:hover{background:#a07820;color:#fff}
.quest-tool.running{background:var(--red);border-color:var(--red);color:#fff}
.quest-tool.done{background:var(--green);border-color:var(--green);color:#fff}
.quest-tool.current{border-color:var(--gold);background:rgba(139,105,20,.12);color:var(--gold)}
.quest-progress-line{display:flex;align-items:center;gap:8px;margin-top:5px;
  font-size:12px;font-family:sans-serif;color:var(--ink2);flex-wrap:wrap}
.focus-line{margin-bottom:5px}
.focus-mini{height:5px;background:rgba(200,184,154,.38);border-radius:999px;
  overflow:hidden;max-width:360px;margin:2px 0 7px}
.focus-mini-fill{height:100%;background:linear-gradient(90deg,var(--blue),var(--gold));
  border-radius:999px;transition:width .25s}
.quest-progress-count{font-weight:700;color:var(--ink)}
.quest-progress-count.done{color:var(--green)}
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
.pathctx-textarea{width:100%;box-sizing:border-box;background:var(--paper2);
  border:1px solid var(--border);color:var(--ink);font-family:'Georgia',serif;
  font-size:13px;padding:10px 12px;border-radius:3px;outline:none;resize:vertical;
  min-height:96px;margin:8px 0 10px}
.pathctx-textarea:focus{border-color:var(--gold)}
.pathctx-ai{font-size:12px;color:var(--ink2);line-height:1.65;background:rgba(139,105,20,.06);
  border-left:2px solid rgba(139,105,20,.35);padding:10px 12px;margin-top:8px;
  white-space:pre-wrap}
.quest-card-textarea{width:100%;box-sizing:border-box;background:var(--paper2);
  border:1px solid var(--border);color:var(--ink);font-family:'Georgia',serif;
  font-size:13px;padding:10px 12px;border-radius:3px;outline:none;resize:vertical;
  min-height:130px;margin:8px 0 10px;line-height:1.55}
.quest-card-textarea:focus{border-color:var(--gold)}
.quest-card-meta{font-size:11px;color:var(--ink3);font-family:sans-serif;line-height:1.6;margin-bottom:10px}
.quest-record{font-size:10px;color:var(--gold);font-family:sans-serif;margin-left:2px}
.timer-record-panel{border:1px solid var(--border2);background:rgba(139,105,20,.05);
  padding:10px 12px;margin:8px 0 12px;border-radius:3px}
.timer-record-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:8px}
.timer-record-cell{background:var(--paper);border:1px solid var(--border2);padding:7px 8px;border-radius:3px}
.timer-record-label{font-size:9px;letter-spacing:1.2px;text-transform:uppercase;color:var(--ink3);font-family:sans-serif}
.timer-record-value{font-size:13px;color:var(--ink);font-family:sans-serif;margin-top:3px}

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
.dlg-label{font-size:10px;letter-spacing:2px;text-transform:uppercase;
  color:var(--ink3);font-family:sans-serif;margin:2px 0 7px}
.dlg-hint{font-size:12px;color:var(--ink3);font-family:sans-serif;line-height:1.5}
.quest-kind-picker{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px;
  font-family:sans-serif;font-size:13px;color:var(--ink2)}
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
/* ── TWO FORCES SIDEBAR ── */
.char-section{margin-top:4px;padding-top:14px;border-top:1px solid var(--border2)}
.force-card{position:relative;padding:10px 0 2px}
.force-row{cursor:pointer;margin-bottom:13px}
.force-top{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:6px}
.force-name{font-size:12px;color:var(--ink);font-family:sans-serif;font-weight:700;letter-spacing:.4px}
.force-val{font-size:11px;font-family:sans-serif;color:var(--ink3);font-weight:700}
.force-bar-wrap{height:9px;background:rgba(44,35,24,.09);border:1px solid var(--border2);
  border-radius:2px;overflow:hidden;box-shadow:inset 0 1px 2px rgba(44,35,24,.12)}
.force-bar{height:100%;width:0%;transition:width .7s ease}
.force-bar.receiving{background:linear-gradient(90deg,#59140f,#a92f22,#d78348)}
.force-bar.giving{background:linear-gradient(90deg,#102b4a,#1f628f,#d5a64a)}
.force-row:hover .force-bar-wrap{border-color:var(--gold)}
.force-balance{display:flex;justify-content:space-between;gap:8px;margin-top:2px;
  color:var(--ink3);font-family:sans-serif;font-size:10px;line-height:1.35}
.force-node{margin-top:7px;color:var(--ink3);font-family:sans-serif;font-size:10px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
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
  .force-card{padding-top:4px}
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
    <div class="dlg-title" id="task-dlg-title">Добавить квест</div>
    <input class="dlg-input" id="t-title" placeholder="Название задания">
    <div class="dlg-label">Тип квеста</div>
    <div class="quest-kind-picker">
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer">
        <input type="radio" name="t-kind" value="task" checked onchange="toggleTaskKindOpts()"> Квест
      </label>
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer">
        <input type="radio" name="t-kind" value="ritual" onchange="toggleTaskKindOpts()"> Ритуал
      </label>
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer">
        <input type="radio" name="t-kind" value="timer" onchange="toggleTaskKindOpts()"> Таймер
      </label>
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer">
        <input type="radio" name="t-kind" value="counter" onchange="toggleTaskKindOpts()"> Счётчик
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
      <div style="display:flex;align-items:center;gap:6px;flex-basis:100%">
        <select class="dlg-input" id="t-ritual-mode" onchange="toggleTaskKindOpts()" style="max-width:220px;margin-bottom:0;font-family:'Georgia',serif">
          <option value="count">Засчитывать кнопкой +1</option>
          <option value="timed_sessions">Подходы по таймеру</option>
        </select>
        <span style="font-size:12px;font-family:sans-serif;color:var(--ink3)">как считать ритуал</span>
      </div>
      <div id="t-session-row" style="display:none;align-items:center;gap:6px;flex-basis:100%">
        <input class="dlg-input" id="t-session-minutes" type="number" min="1" value="90"
          style="width:90px;margin-bottom:0" placeholder="Минут">
        <span style="font-size:12px;font-family:sans-serif;color:var(--ink3)">минут в подходе</span>
      </div>
    </div>
    <div id="t-counter-opts" style="display:none;margin-bottom:10px">
      <input class="dlg-input" id="t-target" type="number" min="1" value="1" placeholder="Цель по счётчику">
    </div>
    <div id="t-timer-opts" class="dlg-hint" style="display:none;margin-bottom:10px">
      Таймер будет копить общее время. Его можно запускать и останавливать прямо в ветви.
    </div>
    <div id="t-timer-record-row" style="display:none;margin-bottom:12px">
      <div class="dlg-label">Рекорд таймера</div>
      <select class="dlg-input" id="t-timer-record-mode" onchange="toggleTaskKindOpts()" style="font-family:'Georgia',serif">
        <option value="none">Без рекорда</option>
        <option value="session">Сессии: последняя / лучшая</option>
        <option value="period">Периоды: текущий / прошлый / лучший</option>
      </select>
      <div id="t-timer-period-row" style="display:none;align-items:center;gap:6px;margin-top:8px">
        <input class="dlg-input" id="t-period-hours" type="number" min="1" value="24"
          style="width:90px;margin-bottom:0" placeholder="Часов">
        <span style="font-size:12px;font-family:sans-serif;color:var(--ink3)">часов в периоде</span>
      </div>
    </div>
    <label style="display:flex;align-items:center;gap:7px;margin:6px 0 8px;font-family:sans-serif;font-size:12px;color:var(--ink2);cursor:pointer">
      <input type="checkbox" id="t-current"> актуально сейчас
    </label>
    <label id="t-record-row" style="display:none;align-items:center;gap:7px;margin:0 0 12px;font-family:sans-serif;font-size:12px;color:var(--ink2);cursor:pointer">
      <input type="checkbox" id="t-record"> вести рекорд счётчика
    </label>
    <input type="hidden" id="t-mid">
    <input type="hidden" id="t-branch">
    <input type="hidden" id="t-parent">
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
  document.getElementById('ls-tab-login').classList.toggle('active',!isReg);
  document.getElementById('ls-tab-reg').classList.toggle('active',isReg);
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

const ICONS = {person:'👤',place:'📍',project:'📁',concept:'💡',event:'📅',quest:'⚔'};
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

// ── Two Forces ────────────────────────────────────────────────────────────────
function htmlesc(s){
  return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
async function loadCharacter(){
  const d=await(await fetch('/forces')).json();
  const el=document.getElementById('char-sidebar');
  if(!el) return;
  const receiving=Math.max(0,Math.min(100,parseInt(d.receiving||0)));
  const giving=Math.max(0,Math.min(100,parseInt(d.giving||0)));
  const nodes=(d.top_nodes||[]).map(n=>n.name).filter(Boolean).slice(0,3).join(' · ');
  el.innerHTML=`<div class="aside-label" style="margin-bottom:8px">Силы Героя</div>
    <div class="force-card">
      <div class="force-row" onclick="openOracle('force','receiving','${htmlesc(d.balance||'')}','Получение')">
        <div class="force-top"><div class="force-name">Получение</div><div class="force-val">${receiving}</div></div>
        <div class="force-bar-wrap"><div class="force-bar receiving" data-target="${receiving}"></div></div>
      </div>
      <div class="force-row" onclick="openOracle('force','giving','${htmlesc(d.balance||'')}','Отдача')">
        <div class="force-top"><div class="force-name">Отдача</div><div class="force-val">${giving}</div></div>
        <div class="force-bar-wrap"><div class="force-bar giving" data-target="${giving}"></div></div>
      </div>
      <div class="force-balance"><span>${htmlesc(d.balance||'равновесие')}</span><span>${d.signals||0} следов</span></div>
      ${nodes?`<div class="force-node">${htmlesc(nodes)}</div>`:''}
    </div>`;
  requestAnimationFrame(()=>requestAnimationFrame(()=>{
    el.querySelectorAll('.force-bar').forEach(b=>{
      b.style.width=b.dataset.target+'%';
    });
  }));
}
async function triggerAnalyze(){
  await loadCharacter();
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
function cleanJournalText(text){
  let out=String(text||'').trim();
  out=out.replace(/^\s*[«"]?quest:[^»"\n]+[»"]?\s*$/i,'');
  out=out.replace(/\s*◆\s*Запись создана по действию квеста\.?/gi,'');
  out=out.replace(/(?:\*\*)?\s*Значение:\s*(?:\*\*)?\s*[-+]?\d+(?:[.,]\d+)?\s*/gi,' ');
  out=out.replace(/\*\*(.*?)\*\*/g,'$1').replace(/__(.*?)__/g,'$1').replace(/`([^`]+)`/g,'$1');
  return out.replace(/[ \t]+/g,' ').replace(/\n{3,}/g,'\n\n').replace(/^[\s"«»]+|[\s"«»]+$/g,'');
}
function linkify(text){
  text=cleanJournalText(text);
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
  const todayNarrative=cleanJournalText((await narR.json()).narrative||'');
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
    const items=entries
      .slice()
      .sort((a,b)=>String(b.ts||'').localeCompare(String(a.ts||'')))
      .map(e=>{
      const timeStr=e.ts.includes(' ')?e.ts.split(' ')[1]:'';
      const isPending = pendingIds.has(e.id);
      const ageStyle=entryAgeStyle(e.ts);
      const rawText=String(e.raw||'');
      const isQuestAction=rawText.startsWith('quest:');
      const archivistHtml=e.archivist_note&&!isQuestAction?
        `<div class="entry-archivist">◆ ${e.archivist_note}</div>`:'';
      const pendingHtml=isPending?
        `<div style="font-size:11px;color:var(--border);font-family:sans-serif;font-style:italic;margin-top:8px">
          ⏳ Архивариус обрабатывает запись...
        </div>`:'';
      return `<div class="entry" style="${ageStyle}">
        ${timeStr?`<div class="entry-stamp">${timeStr}</div>`:''}
        <div class="entry-text">${linkify(e.narrative)}</div>
        ${!isPending&&!isQuestAction&&e.raw&&e.raw!==e.narrative?`<div class="entry-raw">«${e.raw}»</div>`:''}
        ${archivistHtml}
        ${pendingHtml}
      </div>`;
    }).join('');
    const isToday=date===new Date().toISOString().slice(0,10);
    const doneBadge=isToday&&doneToday>0?`<span class="daily-done-badge">✓ ${doneToday} заданий сегодня</span>`:'';
    const progressBlock=isToday&&todayTasks.length?`<div style="margin:10px 0 16px;padding:14px 16px;background:var(--paper2);border:1px solid var(--border2);border-radius:3px">
      <div style="font-size:9px;letter-spacing:2px;color:var(--ink3);font-family:sans-serif;margin-bottom:10px">ХРОНИКИ ДНЯ</div>
      ${todayNarrative?`<div style="font-size:13px;color:var(--ink);font-style:italic;margin-bottom:12px;line-height:1.6">${linkify(todayNarrative)}</div>`:''}
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
  const active=missions
    .filter(m=>m.status==='active')
    .map(m=>({
      ...m,
      currentTasks:(m.tasks||[]).filter(t=>t.status!=='done'&&t.is_current)
        .sort((a,b)=>_taskUrgency(a)-_taskUrgency(b))
    }))
    .filter(m=>m.currentTasks.length);
  const questMeta=t=>{
    const kind=t.quest_kind||((t.task_type||'')==='repeat'?'ritual':'task');
    if(kind==='ritual'){
      const timeLeft=t.last_reset_ts?fmtCountdown(t.last_reset_ts,t.reset_hours):'';
      if((t.progress_mode||'')==='timed_sessions'){
        const s=timedRitualState(t);
        const state=s.finished?'готово':`${s.done}/${s.required} · ${s.running?'подход идёт':`подход ${s.next}/${s.required}`}`;
        return `${state} по ${fmtDuration(s.target)}${timeLeft?' · '+timeLeft:''}`;
      }
      return `${t.current_iters||0}/${t.required_iters||1}${timeLeft?' · '+timeLeft:''}`;
    }
    if(kind==='timer'){
      const rec=timerRecordSummary(t,true);
      return `таймер · всего ${fmtDuration(t.timer_total_seconds||0)}${rec?' · '+rec:''}`;
    }
    if(kind==='counter'){
      const cur=Number(t.progress_value||0);
      const target=Number(t.target_value||1);
      const shown=Number.isInteger(cur)?String(cur):cur.toFixed(1);
      const shownTarget=Number.isInteger(target)?String(target):target.toFixed(1);
      const rec=t.record_enabled?` · рекорд ${Number(t.record_value||0).toLocaleString('ru-RU')}`:'';
      return `счётчик · ${shown}/${shownTarget}${rec}`;
    }
    return t.parent_id?'шаг квеста':'квест';
  };
  const pct=(cur,target)=>Math.max(0,Math.min(100,(Number(cur||0)/Math.max(1,Number(target||1)))*100));
  const asideControls=(t,mid)=>{
    const kind=t.quest_kind||((t.task_type||'')==='repeat'?'ritual':'task');
    const tid=_jsEsc(t.id), midArg=_jsEsc(mid);
    if(kind==='ritual'&&(t.progress_mode||'')==='timed_sessions'){
      const s=timedRitualState(t);
      const cycled=s.done>=s.required;
      const label=cycled?'готово':(s.running?'стоп':'старт');
      const btnClass=cycled?'done':(s.running?'running':'primary');
      return `<div class="aside-quest-actions">
          <button class="aside-action ${btnClass}" onclick="${s.running?`stopTimer('${tid}','${midArg}')`:`startTimer('${tid}','${midArg}')`};event.stopPropagation()" ${cycled?'disabled':''}>${label}</button>
          <span class="aside-quest-meta">${s.done}/${s.required}</span>
          <span class="aside-quest-meta timed-ritual-clock" ${timedRitualAttrs(t)}>${timedRitualLabel(s)}</span>
        </div>
        <div class="aside-progress"><div class="aside-progress-fill timed-ritual-fill" ${timedRitualAttrs(t)} style="width:${s.pct}%"></div></div>`;
    }
    if(kind==='ritual'){
      const cycled=(t.current_iters||0)>=(t.required_iters||1);
      return `<div class="aside-quest-actions">
          <button class="aside-action ${cycled?'done':'primary'}" onclick="tickTask('${tid}','${midArg}');event.stopPropagation()" ${cycled?'disabled':''}>${cycled?'готово':'+1'}</button>
          <span class="aside-quest-meta">${t.current_iters||0}/${t.required_iters||1}</span>
        </div>
        <div class="aside-progress"><div class="aside-progress-fill" style="width:${pct(t.current_iters,t.required_iters)}%"></div></div>`;
    }
    if(kind==='timer'){
      const running=!!t.timer_started_ts;
      return `<div class="aside-quest-actions">
          <button class="aside-action ${running?'running':'primary'}" onclick="${running?`stopTimer('${tid}','${midArg}')`:`startTimer('${tid}','${midArg}')`};event.stopPropagation()">${running?'стоп':'старт'}</button>
          <span class="aside-quest-meta">всего ${fmtDuration(t.timer_total_seconds||0)}</span>
        </div>`;
    }
    if(kind==='counter'){
      const cur=Number(t.progress_value||0), target=Number(t.target_value||1);
      const done=target>0&&cur>=target;
      return `<div class="aside-quest-actions">
          <button class="aside-action ${done?'done':'primary'}" onclick="progressTask('${tid}','${midArg}',1);event.stopPropagation()" ${done?'disabled':''}>${done?'готово':'+1'}</button>
          <span class="aside-quest-meta">${Number.isInteger(cur)?cur:cur.toFixed(1)}/${Number.isInteger(target)?target:target.toFixed(1)}</span>
        </div>
        <div class="aside-progress"><div class="aside-progress-fill" style="width:${pct(cur,target)}%"></div></div>`;
    }
    return `<div class="aside-quest-actions">
      <button class="aside-action done" onclick="doneTask('${tid}','${midArg}');event.stopPropagation()">✓ выполнить</button>
    </div>`;
  };
  document.getElementById('aside-missions').innerHTML=active.length
    ?active.map(m=>{
      const tasksHtml=m.currentTasks.map(t=>{
        return `<div class="aside-quest" onclick="openTaskCard('${_jsEsc(t.id)}')">
          <div class="aside-quest-title">${_entEsc(t.title)}</div>
          <div class="aside-quest-meta">${_entEsc(questMeta(t))}</div>
          ${asideControls(t,m.id)}
        </div>`;
      }).join('');
      return `<div class="aside-mission" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='none'?'block':'none'">
        <div class="aside-mission-t">⚔ ${_entEsc(m.title)}</div>
      </div>
      <div>${tasksHtml}</div>`;
    }).join('')
    :'<div style="font-size:12px;color:var(--ink3);font-family:sans-serif;line-height:1.5">нет актуальных квестов</div>';
}

function questKindName(kind, taskType=''){
  const k=kind||((taskType||'')==='repeat'?'ritual':'task');
  return {task:'квест',ritual:'ритуал',timer:'таймер',counter:'счётчик'}[k]||'квест';
}
function runningElapsedSeconds(ts){
  if(!ts) return 0;
  const started=new Date(String(ts).replace(' ','T')).getTime();
  if(!started||Number.isNaN(started)) return 0;
  return Math.max(0,Math.floor((Date.now()-started)/1000));
}
function timedRitualState(t){
  const target=Math.max(1,Number(t.target_value||5400));
  const running=!!t.timer_started_ts;
  const partial=Number(t.progress_value||0)+runningElapsedSeconds(t.timer_started_ts);
  const required=Math.max(1,Number(t.required_iters||1));
  const done=Math.max(0,Number(t.current_iters||0));
  const finished=done>=required;
  const current=finished?target:Math.min(target,Math.max(0,partial));
  const next=finished?required:Math.min(required,done+1);
  const remaining=finished?0:Math.max(0,target-current);
  return {target,current,remaining,required,done,next,running,finished,pct:Math.max(0,Math.min(100,(current/target)*100))};
}
function fmtClock(seconds){
  seconds=Math.max(0,Math.ceil(Number(seconds||0)));
  const h=Math.floor(seconds/3600);
  const m=Math.floor((seconds%3600)/60);
  const s=seconds%60;
  const pad=n=>String(n).padStart(2,'0');
  if(h>0) return `${h}:${pad(m)}:${pad(s)}`;
  return `${m}:${pad(s)}`;
}
function timedRitualLabel(s){
  if(s.finished) return 'готово';
  if(s.running) return `подход ${s.next}/${s.required}: осталось ${fmtClock(s.remaining)}`;
  return `подход ${s.next}/${s.required}: ${fmtDuration(s.current)}/${fmtDuration(s.target)}`;
}
function timedRitualMeta(t){
  const s=timedRitualState(t);
  return `${s.done}/${s.required} · ${timedRitualLabel(s)}`;
}
function timedRitualAttrs(t){
  return `data-started-ts="${_entEsc(t.timer_started_ts||'')}" data-progress="${Number(t.progress_value||0)}" data-target="${Math.max(1,Number(t.target_value||5400))}" data-done="${Number(t.current_iters||0)}" data-required="${Math.max(1,Number(t.required_iters||1))}"`;
}
function timedRitualStateFromEl(el){
  return timedRitualState({
    timer_started_ts:el.dataset.startedTs||'',
    progress_value:Number(el.dataset.progress||0),
    target_value:Number(el.dataset.target||5400),
    current_iters:Number(el.dataset.done||0),
    required_iters:Number(el.dataset.required||1)
  });
}
function tickTimedRitualClocks(){
  document.querySelectorAll('.timed-ritual-clock').forEach(el=>{
    el.textContent=timedRitualLabel(timedRitualStateFromEl(el));
  });
  document.querySelectorAll('.timed-ritual-fill').forEach(el=>{
    el.style.width=timedRitualStateFromEl(el).pct+'%';
  });
}
function timerRecordMode(task){
  return task?.timer_record_mode || (task?.record_enabled?'session':'none');
}
function timerRecordSummary(task,compact=false){
  const mode=timerRecordMode(task);
  if(mode==='period'){
    const cur=fmtDuration(task.timer_current_period_seconds||task.timer_period_seconds||0);
    const last=fmtDuration(task.timer_last_period_seconds||0);
    const best=fmtDuration(task.timer_best_period_seconds||0);
    return compact
      ?`период ${task.timer_period_hours||24}ч: ${cur} · рекорд ${best}`
      :`Период ${task.timer_period_hours||24}ч: сейчас ${cur}, прошлый ${last}, лучший ${best}`;
  }
  if(mode==='session'){
    const last=fmtDuration(task.timer_last_session_seconds||0);
    const best=fmtDuration(task.timer_best_session_seconds||task.record_value||0);
    return compact?`сессия ${last} · рекорд ${best}`:`Сессия: последняя ${last}, лучшая ${best}`;
  }
  return '';
}
function questRecordText(task){
  if((task?.quest_kind||'')==='timer') return timerRecordSummary(task,false);
  if(!task?.record_enabled) return '';
  const kind=task.quest_kind||'task';
  return `Рекорд: ${Number(task.record_value||0).toLocaleString('ru-RU')}`;
}
function timerRecordPanel(task){
  if((task?.quest_kind||'')!=='timer') return '';
  const mode=timerRecordMode(task);
  const modeLabel={none:'без рекорда',session:'сессии',period:'периоды'}[mode]||'без рекорда';
  const cells=mode==='period'
    ?[
      ['текущий период',fmtDuration(task.timer_current_period_seconds||task.timer_period_seconds||0)],
      ['прошлый период',fmtDuration(task.timer_last_period_seconds||0)],
      ['лучший период',fmtDuration(task.timer_best_period_seconds||0)]
    ]
    :[
      ['последняя сессия',fmtDuration(task.timer_last_session_seconds||0)],
      ['лучшая сессия',fmtDuration(task.timer_best_session_seconds||task.record_value||0)],
      ['всего',fmtDuration(task.timer_total_seconds||0)]
    ];
  return `<div class="timer-record-panel">
    <div class="quest-card-meta">Режим рекорда: ${modeLabel}${mode==='period'?` · период ${task.timer_period_hours||24}ч`:''}</div>
    <div class="timer-record-grid">${cells.map(c=>`<div class="timer-record-cell">
      <div class="timer-record-label">${c[0]}</div><div class="timer-record-value">${c[1]}</div>
    </div>`).join('')}</div>
  </div>`;
}
function timerRecordSettings(task){
  if((task?.quest_kind||'')!=='timer') return '';
  const mode=timerRecordMode(task);
  return `<div class="ent-sec">Рекорды таймера</div>
    ${timerRecordPanel(task)}
    <div class="timer-record-panel">
      <select class="dlg-input" id="timer-card-mode" onchange="toggleTimerCardPeriodRow()" style="font-family:'Georgia',serif;margin-bottom:8px">
        <option value="none" ${mode==='none'?'selected':''}>Без рекорда</option>
        <option value="session" ${mode==='session'?'selected':''}>Сессии: последняя / лучшая</option>
        <option value="period" ${mode==='period'?'selected':''}>Периоды: текущий / прошлый / лучший</option>
      </select>
      <div id="timer-card-period-row" style="display:${mode==='period'?'flex':'none'};align-items:center;gap:6px;margin-bottom:10px">
        <input class="dlg-input" id="timer-card-period-hours" type="number" min="1" value="${task.timer_period_hours||24}"
          style="width:90px;margin-bottom:0">
        <span style="font-size:12px;font-family:sans-serif;color:var(--ink3)">часов в периоде</span>
      </div>
      <button class="ent-merge-btn" onclick="saveTimerRecordSettings('${_jsEsc(task.id)}','${_jsEsc(task.mission_id||'')}')">Сохранить режим рекорда</button>
    </div>`;
}
function ritualSettings(task){
  if((task?.quest_kind||'')!=='ritual') return '';
  const mode=(task.progress_mode||'count')==='timed_sessions'?'timed_sessions':'count';
  const minutes=Math.max(1,Math.round(Number(task.target_value||5400)/60));
  const timed=mode==='timed_sessions';
  const state=timed?timedRitualState(task):null;
  return `<div class="ent-sec">Настройки ритуала</div>
    <div class="timer-record-panel">
      ${timed?`<div class="quest-card-meta">${state.done}/${state.required} · <span class="timed-ritual-clock" ${timedRitualAttrs(task)}>${timedRitualLabel(state)}</span></div>
      <div class="focus-mini" style="max-width:none"><div class="focus-mini-fill timed-ritual-fill" ${timedRitualAttrs(task)} style="width:${state.pct}%"></div></div>`:''}
      <select class="dlg-input" id="ritual-card-mode" onchange="toggleRitualCardSessionRow()" style="font-family:'Georgia',serif;margin-bottom:8px">
        <option value="count" ${mode==='count'?'selected':''}>Ручной ритуал: кнопка +1</option>
        <option value="timed_sessions" ${mode==='timed_sessions'?'selected':''}>Таймерные подходы</option>
      </select>
      <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:8px">
        <input class="dlg-input" id="ritual-card-iters" type="number" min="1" value="${task.required_iters||1}"
          style="width:86px;margin-bottom:0">
        <span style="font-size:12px;font-family:sans-serif;color:var(--ink3)">раз за цикл</span>
        <input class="dlg-input" id="ritual-card-hours" type="number" min="1" value="${task.reset_hours||24}"
          style="width:86px;margin-bottom:0">
        <span style="font-size:12px;font-family:sans-serif;color:var(--ink3)">ч до сброса</span>
      </div>
      <div id="ritual-card-session-row" style="display:${timed?'flex':'none'};align-items:center;gap:6px;margin-bottom:10px">
        <input class="dlg-input" id="ritual-card-session-minutes" type="number" min="1" value="${minutes}"
          style="width:86px;margin-bottom:0">
        <span style="font-size:12px;font-family:sans-serif;color:var(--ink3)">минут в подходе</span>
      </div>
      <button class="ent-merge-btn" onclick="saveRitualSettings('${_jsEsc(task.id)}','${_jsEsc(task.mission_id||'')}')">Сохранить ритуал</button>
    </div>`;
}
function toggleTimerCardPeriodRow(){
  const mode=document.getElementById('timer-card-mode')?.value||'none';
  const row=document.getElementById('timer-card-period-row');
  if(row) row.style.display=mode==='period'?'flex':'none';
}
function toggleRitualCardSessionRow(){
  const mode=document.getElementById('ritual-card-mode')?.value||'count';
  const row=document.getElementById('ritual-card-session-row');
  if(row) row.style.display=mode==='timed_sessions'?'flex':'none';
}
async function saveTimerRecordSettings(tid,mid=''){
  const mode=document.getElementById('timer-card-mode')?.value||'none';
  const period_hours=parseInt(document.getElementById('timer-card-period-hours')?.value)||24;
  const r=await fetch(`/tasks/${tid}/timer-record`,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode,period_hours})});
  if(!r.ok) return;
  if(mid) _openMissions.add(mid);
  const active=document.querySelector('section.active');
  if(active?.id==='s-missions') loadMissions();
  loadAsides();
  openTaskCard(tid);
}
async function saveRitualSettings(tid,mid=''){
  const progress_mode=document.getElementById('ritual-card-mode')?.value||'count';
  const required_iters=parseInt(document.getElementById('ritual-card-iters')?.value)||1;
  const reset_hours=parseInt(document.getElementById('ritual-card-hours')?.value)||24;
  const session_minutes=parseInt(document.getElementById('ritual-card-session-minutes')?.value)||90;
  const r=await fetch(`/tasks/${tid}/ritual-settings`,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({progress_mode,required_iters,reset_hours,session_minutes})});
  if(!r.ok) return;
  if(mid) _openMissions.add(mid);
  const active=document.querySelector('section.active');
  if(active?.id==='s-missions') loadMissions();
  loadAsides();
  openTaskCard(tid);
}
async function toggleTaskCurrent(tid,mid='',isCurrent=true){
  const r=await fetch(`/tasks/${tid}/current`,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({is_current:!!isCurrent})});
  if(!r.ok) return;
  if(mid) _openMissions.add(mid);
  const active=document.querySelector('section.active');
  if(active?.id==='s-missions') loadMissions();
  loadAsides();
  if(document.getElementById('ent-modal').classList.contains('open')&&document.getElementById('quest-card-desc')){
    openTaskCard(tid);
  }
}
async function openTaskCard(tid){
  const r=await fetch(`/tasks/${tid}/card`);
  if(!r.ok) return;
  const d=await r.json();
  const task=d.task||{}, mission=d.mission||{}, branch=d.branch||{};
  const mid=task.mission_id||'';
  const kind=questKindName(task.quest_kind,task.task_type);
  const rec=questRecordText(task);
  const entities=(d.entities||[]).map(e=>
    `<span class="mission-ent-tag" onclick="openPathEntity('${_jsEsc(mid)}','${_jsEsc(e.name||'')}')">${_entEsc(e.name||'')}</span>`
  ).join('');
  const aiBtn=d.ai_available
    ?`<button class="ent-merge-btn" onclick="saveTaskCard('${_jsEsc(tid)}',true)">ИИ улучшить</button>`
    :`<button class="ent-merge-btn" disabled title="Подключи ключ ИИ в настройках">ИИ недоступен</button>`;
  document.getElementById('ent-content').innerHTML=`
    <div class="ent-name" style="color:var(--red)">${ICONS.quest||'◆'} ${_entEsc(task.title||'Квест')}</div>
    <div class="ent-type" style="color:var(--red)">${_entEsc(kind)} · ${_entEsc(mission.title||'Путь')} · ${_entEsc(branch.title||'Основная')}</div>
    <div class="quest-card-meta">${task.is_current?'актуально сейчас':'не в активных'}${rec?' · '+_entEsc(rec):''}</div>
    ${entities?`<div class="mission-entities" style="margin:4px 0 12px">${entities}</div>`:''}
    ${task.notes?`<div class="ent-sec">Заметки игрока</div><div class="ent-summary">${_entEsc(task.notes)}</div>`:''}
    ${ritualSettings(task)}
    ${timerRecordSettings(task)}
    <div class="ent-sec">Карточка задания</div>
    <textarea class="quest-card-textarea" id="quest-card-desc" placeholder="Что происходит в этом квесте, зачем он нужен, что важно помнить...">${_entEsc(d.description||'')}</textarea>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="ent-merge-btn" onclick="saveTaskCard('${_jsEsc(tid)}',false)">Сохранить</button>
      ${aiBtn}
      <button class="ent-merge-btn" onclick="toggleTaskCurrent('${_jsEsc(tid)}','${_jsEsc(mid)}',${task.is_current?'false':'true'})">${task.is_current?'Убрать из актуальных':'В актуальные'}</button>
    </div>`;
  document.getElementById('ent-modal').classList.add('open');
}
async function saveTaskCard(tid,improve=false){
  const ta=document.getElementById('quest-card-desc');
  if(!ta) return;
  const old=ta.value;
  if(improve) ta.value='Архивариус переписывает карточку...';
  const r=await fetch(`/tasks/${tid}/card`,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({description:old,improve})});
  if(!r.ok){ta.value=old;return;}
  const d=await r.json();
  ta.value=d.description||old;
  const active=document.querySelector('section.active');
  if(active?.id==='s-missions') loadMissions();
  loadAsides();
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
    loadCharacter();
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
function closeMissionMenus(){
  document.querySelectorAll('.mission-menu.open').forEach(m=>m.classList.remove('open'));
}
function toggleMissionMenu(mid,event){
  event?.stopPropagation();
  const menu=document.getElementById('mmenu-'+mid);
  if(!menu) return;
  const open=menu.classList.contains('open');
  closeMissionMenus();
  if(!open) menu.classList.add('open');
}
document.addEventListener('click',closeMissionMenus);

let _dragTask=null;
function clearDragTargets(){
  document.querySelectorAll('.quest-branch-body.drag-over').forEach(el=>el.classList.remove('drag-over'));
  document.querySelectorAll('.quest-item.drop-before').forEach(el=>el.classList.remove('drop-before'));
}
function dragTaskStart(event,tid,mid){
  if(event.target.closest('button,input,textarea,select,label')){
    event.preventDefault();
    return;
  }
  _dragTask={tid,mid};
  event.dataTransfer.effectAllowed='move';
  event.dataTransfer.setData('text/plain',tid);
  event.currentTarget.classList.add('dragging');
}
function dragTaskEnd(event){
  event.currentTarget.classList.remove('dragging');
  _dragTask=null;
  clearDragTargets();
}
function dragTaskOverBranch(event){
  if(!_dragTask) return;
  event.preventDefault();
  event.dataTransfer.dropEffect='move';
  event.currentTarget.classList.add('drag-over');
}
function dragTaskLeaveBranch(event){
  if(!event.currentTarget.contains(event.relatedTarget)){
    event.currentTarget.classList.remove('drag-over');
  }
}
function dragTaskOverTask(event,tid){
  if(!_dragTask||_dragTask.tid===tid) return;
  event.preventDefault();
  event.stopPropagation();
  const item=event.currentTarget.children[0];
  if(item) item.classList.add('drop-before');
}
function dragTaskLeaveTask(event){
  if(!event.currentTarget.contains(event.relatedTarget)){
    const item=event.currentTarget.children[0];
    if(item) item.classList.remove('drop-before');
  }
}
async function moveDraggedTask(tid,mid,branchId,parentId='',beforeTaskId=''){
  const r=await fetch(`/tasks/${tid}/move`,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({branch_id:branchId,parent_id:parentId||'',before_task_id:beforeTaskId||''})});
  if(!r.ok){
    const d=await r.json().catch(()=>({detail:'Не удалось перенести квест'}));
    alert(d.detail||'Не удалось перенести квест');
    return;
  }
  _openMissions.add(mid);
  loadMissions();
}
async function dropTaskOnBranch(event,mid,branchId){
  if(!_dragTask) return;
  event.preventDefault();
  event.stopPropagation();
  const tid=_dragTask.tid;
  clearDragTargets();
  await moveDraggedTask(tid,mid,branchId,'','');
}
async function dropTaskBefore(event,mid,branchId,parentId,beforeTaskId){
  if(!_dragTask||_dragTask.tid===beforeTaskId) return;
  event.preventDefault();
  event.stopPropagation();
  const tid=_dragTask.tid;
  clearDragTargets();
  await moveDraggedTask(tid,mid,branchId,parentId||'',beforeTaskId);
}

async function loadMissions(){
  const [r,cd]=await Promise.all([fetch('/missions'),fetch('/character/data')]);
  const ms=await r.json(); const charData=await cd.json();
  const el=document.getElementById('missions-list');
  if(!ms.length){
    el.innerHTML='<div class="empty">Нет путей. Добавь первый путь ↗</div>';
    return;
  }
  ms.forEach(m=>{ if(!_closedMissions.has(m.id)) _openMissions.add(m.id); });
  const htmlEsc=v=>String(v??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const jsArg=v=>String(v??'').replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/\r?\n/g,'\\n');
  const defaultBranchId=m=>`${m.id}:main`;
  const questKind=t=>t.quest_kind||((t.task_type||'')==='repeat'?'ritual':'task');
  const kindTitle=k=>({task:'квест',ritual:'ритуал',timer:'таймер',counter:'счётчик'}[k]||'квест');
  const kindClass=k=>k==='timer'?'timer':k==='counter'?'counter':'';
  const taskProgress=t=>{
    const cur=Number(t.progress_value||t.current_iters||0);
    const target=Number(t.target_value||t.required_iters||1);
    const shown=Number.isInteger(cur)?cur:cur.toFixed(1);
    const shownTarget=Number.isInteger(target)?target:target.toFixed(1);
    return {cur,target,shown,shownTarget,done:target>0&&cur>=target};
  };
  const taskTree=tasks=>{
    const ids=new Set(tasks.map(t=>t.id));
    const map={};
    tasks.forEach(t=>{
      const p=(t.parent_id&&ids.has(t.parent_id))?t.parent_id:'';
      if(!map[p]) map[p]=[];
      map[p].push(t);
    });
    return map;
  };
  const renderTask=(t,m,tree)=>{
    const kind=questKind(t);
    const isDone=t.status==='done';
    const notes=(t.notes||'').trim();
    const branchId=t.branch_id||defaultBranchId(m);
    const childHtml=(tree[t.id]||[]).map(ch=>renderTask(ch,m,tree)).join('');
    const badge=`<span class="quest-kind ${kindClass(kind)}">${kindTitle(kind)}</span>`;
    const focusBadge=(kind==='ritual'&&(t.progress_mode||'')==='timed_sessions')?`<span class="quest-kind timer">подходы</span>`:'';
    const currentBadge=t.is_current?`<span class="quest-kind">актуально</span>`:'';
    const title=`<div class="quest-title ${isDone?'done':''}" id="qtitle-${t.id}">${htmlEsc(t.title)}</div>`;
    const noteHtml=notes?`<div class="quest-note-preview">${htmlEsc(notes.split('\n').slice(-1)[0])}</div>`:'';
    const addChild=`<button class="quest-tool" onclick="openTaskDlg('${jsArg(m.id)}','${jsArg(branchId)}','${jsArg(t.id)}');event.stopPropagation()">+ шаг</button>`;
    const noteBtn=`<button class="quest-tool" onclick="addTaskNote('${jsArg(t.id)}','${jsArg(m.id)}');event.stopPropagation()">заметка</button>`;
    const cardBtn=`<button class="quest-tool" onclick="openTaskCard('${jsArg(t.id)}');event.stopPropagation()">карточка</button>`;
    const currentBtn=`<button class="quest-tool ${t.is_current?'current':''}" onclick="toggleTaskCurrent('${jsArg(t.id)}','${jsArg(m.id)}',${t.is_current?'false':'true'});event.stopPropagation()">${t.is_current?'★ актуально':'☆ в актуальные'}</button>`;
    const editBtn=`<button class="btn-edit-inline" onclick="editTask('${jsArg(t.id)}');event.stopPropagation()" title="Переименовать">✎</button>`;
    const delBtn=`<button class="quest-del" onclick="deleteTask('${jsArg(t.id)}','${jsArg(m.id)}')" title="Удалить">×</button>`;
    const timerRec=kind==='timer'?timerRecordSummary(t,true):'';
    const recordHtml=kind==='timer'
      ?(timerRec?`<span class="quest-record">${timerRec}</span>`:'')
      :(t.record_enabled?`<span class="quest-record">рекорд: ${Number(t.record_value||0).toLocaleString('ru-RU')}</span>`:'');
    let lead='';
    let controls=`<div class="quest-tools">${addChild}${cardBtn}${currentBtn}${noteBtn}</div>`;

    if(kind==='ritual'){
      const cycled=t.current_iters>=t.required_iters;
      if((t.progress_mode||'')==='timed_sessions'){
        const s=timedRitualState(t);
        const label=cycled?'готово':(s.running?'стоп':'старт');
        const btnClass=cycled?'done':(s.running?'running':'primary');
        controls=`<div class="quest-progress-line focus-line">
          <button class="quest-tool ${btnClass}" onclick="${s.running?`stopTimer('${jsArg(t.id)}','${jsArg(m.id)}')`:`startTimer('${jsArg(t.id)}','${jsArg(m.id)}')`};event.stopPropagation()" ${cycled?'disabled':''}>${label}</button>
          <span class="quest-progress-count ${cycled?'done':''}">${s.done}/${s.required}</span>
          <span class="quest-record timed-ritual-clock" ${timedRitualAttrs(t)}>${timedRitualLabel(s)}</span>
          <span class="reset-hint" data-reset-ts="${t.last_reset_ts||''}" data-reset-hours="${t.reset_hours||24}">· ${fmtCountdown(t.last_reset_ts,t.reset_hours)}</span>
        </div>
        <div class="focus-mini"><div class="focus-mini-fill timed-ritual-fill" ${timedRitualAttrs(t)} style="width:${s.pct}%"></div></div>
        <div class="quest-tools">${addChild}${cardBtn}${currentBtn}${noteBtn}</div>`;
      } else {
        controls=`<div class="quest-progress-line">
          <button class="iter-btn" onclick="tickTask('${jsArg(t.id)}','${jsArg(m.id)}')" ${cycled?'disabled':''}>+1</button>
          <span class="quest-progress-count ${cycled?'done':''}">${t.current_iters||0}/${t.required_iters||1}</span>
          <span class="streak-display">${streakMythName(t.streak)}</span>
          <span class="streak-best">рекорд: ${t.best_streak||0}</span>
          <span class="reset-hint" data-reset-ts="${t.last_reset_ts||''}" data-reset-hours="${t.reset_hours||24}">· ${fmtCountdown(t.last_reset_ts,t.reset_hours)}</span>
        </div><div class="quest-tools">${addChild}${cardBtn}${currentBtn}${noteBtn}</div>`;
      }
    } else if(kind==='timer'){
      const running=!!t.timer_started_ts;
      controls=`<div class="quest-progress-line">
        <button class="quest-tool ${running?'running':'primary'}" onclick="${running?`stopTimer('${jsArg(t.id)}','${jsArg(m.id)}')`:`startTimer('${jsArg(t.id)}','${jsArg(m.id)}')`};event.stopPropagation()">${running?'стоп':'старт'}</button>
        <span>всего ${fmtDuration(t.timer_total_seconds||0)}</span>${recordHtml}
      </div><div class="quest-tools">${addChild}${cardBtn}${currentBtn}${noteBtn}</div>`;
    } else if(kind==='counter'){
      const p=taskProgress(t);
      controls=`<div class="quest-progress-line">
        <button class="quest-tool primary" onclick="progressTask('${jsArg(t.id)}','${jsArg(m.id)}',1);event.stopPropagation()">+1</button>
        <span class="quest-progress-count ${p.done?'done':''}">${p.shown}/${p.shownTarget}</span>${recordHtml}
      </div><div class="quest-tools">${addChild}${cardBtn}${currentBtn}${noteBtn}</div>`;
    } else {
      lead=`<div class="quest-cb ${isDone?'done':''}" onclick="doneTask('${jsArg(t.id)}','${jsArg(m.id)}')">${isDone?'✓':''}</div>`;
    }

    return `<div class="quest-node ${t.is_current?'current':''}" draggable="true"
      ondragstart="dragTaskStart(event,'${jsArg(t.id)}','${jsArg(m.id)}')"
      ondragend="dragTaskEnd(event)"
      ondragover="dragTaskOverTask(event,'${jsArg(t.id)}')"
      ondragleave="dragTaskLeaveTask(event)"
      ondrop="dropTaskBefore(event,'${jsArg(m.id)}','${jsArg(branchId)}','${jsArg(t.parent_id||'')}','${jsArg(t.id)}')">
      <div class="quest-item ${kind==='ritual'?'repeat-task':''}">
        ${lead}
        <div class="quest-info">
          <div class="quest-meta">${badge}${focusBadge}${currentBadge}${t.locked?'<span class="quest-kind">закрыто</span>':''}</div>
          ${title}
          ${noteHtml}
          ${controls}
        </div>
        ${editBtn}
        ${delBtn}
      </div>
      ${childHtml?`<div class="quest-children">${childHtml}</div>`:''}
    </div>`;
  };
  const branchMenu=(m,b,isDefault)=>`<div class="quest-branch-actions">
    <button class="quest-branch-btn" onclick="openTaskDlg('${jsArg(m.id)}','${jsArg(b.id)}','')">+ квест</button>
    <button class="quest-branch-btn" onclick="renameBranch('${jsArg(b.id)}','${jsArg(b.title)}','${jsArg(m.id)}')" title="Переименовать">✎</button>
    ${isDefault?'':`<button class="quest-branch-btn" onclick="deleteBranch('${jsArg(b.id)}','${jsArg(m.id)}')" title="Удалить ветвь">×</button>`}
  </div>`;
  el.innerHTML=ms.map(m=>{
    const branches=(m.branches&&m.branches.length)?m.branches:[{id:defaultBranchId(m),title:'Основная',status:'active',position:0}];
    const defaultBid=defaultBranchId(m);
    const doneCount=m.tasks.filter(t=>t.status==='done').length;
    const ritualCount=m.tasks.filter(t=>questKind(t)==='ritual').length;
    const timerCount=m.tasks.filter(t=>questKind(t)==='timer').length;
    const wasOpen=_openMissions.has(m.id);
    const badge=[`${branches.length} ветв.`,`${m.tasks.length} квестов`,doneCount?`${doneCount} выполн.`:'',ritualCount?`${ritualCount} ритуал.`:'',timerCount?`${timerCount} таймер.`:''].filter(Boolean).join(' · ');
    const entTags=(m.entities||[]).map(e=>`<span class="mission-ent-tag" onclick="openPathEntity('${jsArg(m.id)}','${jsArg(e.name||'')}');event.stopPropagation()" title="${htmlEsc(e.summary||'')}">${htmlEsc(e.name)}</span>`).join('');
    const branchHtml=branches.map(b=>{
      const branchTasks=m.tasks.filter(t=>(t.branch_id||defaultBid)===b.id);
      const tree=taskTree(branchTasks);
      const body=(tree['']||[]).map(t=>renderTask(t,m,tree)).join('')||`<div class="quest-empty">Пустая ветвь.</div>`;
      const isDefault=b.id===defaultBid;
      return `<div class="quest-branch">
        <div class="quest-branch-head">
          <div>
            <span class="quest-branch-title">${htmlEsc(b.title||'Ветвь')}</span>
            <span class="quest-branch-count">${branchTasks.length}</span>
          </div>
          ${branchMenu(m,b,isDefault)}
        </div>
        <div class="quest-branch-body"
          ondragover="dragTaskOverBranch(event)"
          ondragleave="dragTaskLeaveBranch(event)"
          ondrop="dropTaskOnBranch(event,'${jsArg(m.id)}','${jsArg(b.id)}')">${body}</div>
      </div>`;
    }).join('');

    return `
    <div class="mission-block ${m.status==='done'?'done':''}">
      <div class="mission-block-hdr" onclick="toggleMission('${m.id}')">
        <div class="mission-star">${m.status==='done'?'✓':'✦'}</div>
        <div class="mission-block-info">
          <div class="mission-block-title" id="mtitle-${m.id}">${htmlEsc(m.title)}</div>
          ${badge?`<div class="mission-progress-badge">${badge}</div>`:''}
        </div>
        <button class="btn-edit-inline" onclick="editMission('${m.id}');event.stopPropagation()" title="Переименовать">✎</button>
        <div class="mission-more" onclick="event.stopPropagation()">
          <button class="mission-menu-btn" onclick="toggleMissionMenu('${m.id}',event)" title="Настройки пути">⋯</button>
          <div class="mission-menu" id="mmenu-${m.id}">
            <button onclick="editMission('${m.id}');closeMissionMenus()">Переименовать</button>
            <button class="danger" onclick="deleteMission('${m.id}');closeMissionMenus()">Удалить путь</button>
          </div>
        </div>
        <div class="mission-block-chevron ${wasOpen?'open':''}" id="chev-${m.id}">▾</div>
      </div>
      ${entTags?`<div class="mission-entities">${entTags}</div>`:''}
      <div class="quest-chain ${wasOpen?'open':''}" id="qchain-${m.id}">
        ${branchHtml}
      </div>
      <div class="mission-actions">
        ${m.status!=='done'?`<button class="btn-quest-add" onclick="addBranch('${m.id}')">+ ветвь</button>`:''}
        <button class="btn-link-entity" onclick="openEntityDlg('${m.id}')" title="Привязать сущность к Пути">+ сущность</button>
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
function openTaskDlg(mid,branchId='',parentId=''){
  document.getElementById('t-mid').value=mid;
  document.getElementById('t-branch').value=branchId||'';
  document.getElementById('t-parent').value=parentId||'';
  document.getElementById('task-dlg-title').textContent=parentId?'Добавить шаг':'Добавить квест';
  document.getElementById('t-title').value='';
  document.getElementById('t-iters').value='1';
  document.getElementById('t-hours').value='24';
  document.getElementById('t-target').value='1';
  document.getElementById('t-ritual-mode').value='count';
  document.getElementById('t-session-minutes').value='90';
  document.getElementById('t-timer-record-mode').value='none';
  document.getElementById('t-period-hours').value='24';
  document.getElementById('t-current').checked=false;
  document.getElementById('t-record').checked=false;
  const taskRadio=document.querySelector('input[name="t-kind"][value="task"]');
  if(taskRadio) taskRadio.checked=true;
  toggleTaskKindOpts();
  openDlg('task-dlg');
  setTimeout(()=>document.getElementById('t-title').focus(),50);
}
async function saveTask(){
  const t=document.getElementById('t-title').value.trim();
  const mid=document.getElementById('t-mid').value;
  const branchId=document.getElementById('t-branch').value;
  const parentId=document.getElementById('t-parent').value;
  if(!t) return;
  const kindEl=document.querySelector('input[name="t-kind"]:checked');
  const kind=kindEl?kindEl.value:'task';
  const iters=parseInt(document.getElementById('t-iters')?.value)||1;
  const hours=parseInt(document.getElementById('t-hours')?.value)||24;
  const target=parseFloat(document.getElementById('t-target')?.value)||1;
  const ritualMode=document.getElementById('t-ritual-mode')?.value||'count';
  const sessionMinutes=parseInt(document.getElementById('t-session-minutes')?.value)||90;
  const taskType=kind==='ritual'?'repeat':'once';
  const progressMode=kind==='ritual'?ritualMode:kind==='timer'?'timer':kind==='counter'?'number':'check';
  const targetValue=kind==='ritual'?(ritualMode==='timed_sessions'?sessionMinutes*60:iters):(kind==='counter'?target:1);
  const current=document.getElementById('t-current')?.checked||false;
  const timerRecordMode=kind==='timer'?(document.getElementById('t-timer-record-mode')?.value||'none'):'none';
  const periodHours=parseInt(document.getElementById('t-period-hours')?.value)||24;
  const record=(kind==='timer'&&timerRecordMode!=='none') || (!!document.getElementById('t-record')?.checked&&kind==='counter');
  await fetch('/tasks',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      mission_id:mid,title:t,task_type:taskType,required_iters:iters,reset_hours:hours,
      quest_kind:kind,progress_mode:progressMode,branch_id:branchId,parent_id:parentId,
      target_value:targetValue,is_current:current,record_enabled:record,
      timer_record_mode:timerRecordMode,timer_period_hours:periodHours
    })});
  closeDlg('task-dlg');
  document.getElementById('t-title').value='';
  const taskRadio=document.querySelector('input[name="t-kind"][value="task"]');
  if(taskRadio) taskRadio.checked=true;
  toggleTaskKindOpts();
  _openMissions.add(mid);
  loadMissions(); loadAsides(); loadCharacter();
}
async function tickTask(tid,mid){
  await fetch(`/tasks/${tid}/tick`,{method:'POST'});
  _openMissions.add(mid); loadMissions(); loadAsides(); loadCharacter();
}
function toggleTaskKindOpts(){
  const kind=document.querySelector('input[name="t-kind"]:checked')?.value||'task';
  const opts=document.getElementById('t-repeat-opts');
  if(opts) opts.style.display=kind==='ritual'?'flex':'none';
  const ritualMode=document.getElementById('t-ritual-mode')?.value||'count';
  const sessionRow=document.getElementById('t-session-row');
  if(sessionRow) sessionRow.style.display=(kind==='ritual'&&ritualMode==='timed_sessions')?'flex':'none';
  const counter=document.getElementById('t-counter-opts');
  if(counter) counter.style.display=kind==='counter'?'block':'none';
  const timer=document.getElementById('t-timer-opts');
  if(timer) timer.style.display=kind==='timer'?'block':'none';
  const timerRecord=document.getElementById('t-timer-record-row');
  if(timerRecord) timerRecord.style.display=kind==='timer'?'block':'none';
  const timerRecordMode=document.getElementById('t-timer-record-mode')?.value||'none';
  const periodRow=document.getElementById('t-timer-period-row');
  if(periodRow) periodRow.style.display=(kind==='timer'&&timerRecordMode==='period')?'flex':'none';
  const record=document.getElementById('t-record-row');
  if(record) record.style.display=kind==='counter'?'flex':'none';
}
function toggleRepeatOpts(){
  toggleTaskKindOpts();
}
async function addBranch(mid){
  const title=prompt('Название ветви');
  if(!title||!title.trim()) return;
  await fetch(`/missions/${mid}/branches`,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:title.trim()})});
  _openMissions.add(mid); loadMissions();
}
async function renameBranch(bid,current,mid){
  const title=prompt('Новое название ветви',current||'');
  if(!title||!title.trim()) return;
  await fetch(`/branches/${bid}/update`,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:title.trim()})});
  _openMissions.add(mid); loadMissions();
}
async function deleteBranch(bid,mid){
  if(!confirm('Удалить ветвь? Задания переедут в Основную.')) return;
  const r=await fetch(`/branches/${bid}/delete`,{method:'POST'});
  if(!r.ok){
    const d=await r.json().catch(()=>({detail:'Не удалось удалить ветвь'}));
    alert(d.detail||'Не удалось удалить ветвь');
    return;
  }
  _openMissions.add(mid); loadMissions();
}
async function progressTask(tid,mid,delta){
  await fetch(`/tasks/${tid}/progress`,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({delta})});
  _openMissions.add(mid); loadMissions(); loadAsides(); loadCharacter();
}
async function startTimer(tid,mid){
  await fetch(`/tasks/${tid}/timer/start`,{method:'POST'});
  _openMissions.add(mid); loadMissions(); loadAsides(); loadCharacter();
}
async function stopTimer(tid,mid){
  await fetch(`/tasks/${tid}/timer/stop`,{method:'POST'});
  _openMissions.add(mid); loadMissions(); loadAsides(); loadCharacter();
}
async function addTaskNote(tid,mid){
  const note=prompt('Заметка к квесту');
  if(!note||!note.trim()) return;
  await fetch(`/tasks/${tid}/note`,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({note:note.trim()})});
  _openMissions.add(mid); loadMissions(); loadAsides(); loadCharacter();
}
function fmtDuration(seconds){
  seconds=Math.max(0,parseInt(seconds||0));
  const h=Math.floor(seconds/3600);
  const m=Math.floor((seconds%3600)/60);
  if(h>=1) return `${h}ч ${m}м`;
  if(m>=1) return `${m}м`;
  return '0м';
}
async function doneMission(id){
  await fetch(`/missions/${id}/complete`,{method:'POST'}); loadMissions(); loadAsides(); loadCharacter();
}
async function deleteMission(id){
  if(!confirm('Удалить этот путь и все его задания?')) return;
  await fetch(`/missions/${id}/delete`,{method:'POST'}); loadMissions(); loadAsides();
}
async function doneTask(tid,mid){
  await fetch(`/tasks/${tid}/complete`,{method:'POST'});
  _openMissions.add(mid); loadMissions(); loadAsides(); loadCharacter();
}
async function deleteTask(tid,mid){
  await fetch(`/tasks/${tid}/delete`,{method:'POST'});
  _openMissions.add(mid); loadMissions(); loadAsides();
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
  loadMissions(); loadAsides();
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
function _entEsc(v){
  return String(v??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function _jsEsc(v){
  return String(v??'').replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/\r?\n/g,'\\n');
}
async function openPathEntity(mid,name){
  const r=await fetch(`/missions/${mid}/entity-context?entity_name=${encodeURIComponent(name)}`);
  if(!r.ok){openEnt(name);return;}
  const d=await r.json();
  const e=d.entity||{};
  const m=d.mission||{};
  const clr=TYPE_COLORS[e.type]||'var(--ink3)';
  document.getElementById('ent-content').innerHTML=`
    <div class="ent-name" style="color:${clr}">${ICONS[e.type]||'◆'} ${_entEsc(e.name)}</div>
    <div class="ent-type" style="color:${clr}">внутри Пути: ${_entEsc(m.title)}</div>
    <div class="ent-summary">${_entEsc(e.summary||'Пока нет общей сводки в базе знаний.')}</div>
    <div class="ent-sec">Контекст в этом Пути</div>
    <textarea class="pathctx-textarea" id="pathctx-note" placeholder="Например: какой контент здесь выкладывать, какую роль играет сущность, что попробовать дальше...">${_entEsc(d.note||'')}</textarea>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="ent-merge-btn" onclick="savePathEntityContext('${_jsEsc(mid)}','${_jsEsc(e.name)}',false)">Сохранить</button>
      <button class="ent-merge-btn" onclick="savePathEntityContext('${_jsEsc(mid)}','${_jsEsc(e.name)}',true)">ИИ улучшить</button>
      <button class="ent-merge-btn" onclick="openEnt('${_jsEsc(e.name)}')">Открыть в базе</button>
    </div>
    ${d.ai_note?`<div class="ent-sec">Версия ИИ</div><div class="pathctx-ai">${_entEsc(d.ai_note)}</div>`:''}`;
  document.getElementById('ent-modal').classList.add('open');
}
async function savePathEntityContext(mid,name,improve){
  const note=document.getElementById('pathctx-note')?.value||'';
  const r=await fetch(`/missions/${mid}/entity-context`,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({entity_name:name,note,improve})});
  if(r.ok) openPathEntity(mid,name);
}
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
  const fp=e.force_profile||{};
  const er=Math.round((fp.receiving||0)*100), eg=Math.round((fp.giving||0)*100);
  const forceHtml=`<div class="ent-sec">Силы узла</div>
    <div class="force-row" style="cursor:default;margin-bottom:8px">
      <div class="force-top"><div class="force-name">Получение</div><div class="force-val">${er}</div></div>
      <div class="force-bar-wrap"><div class="force-bar receiving" style="width:${er}%"></div></div>
    </div>
    <div class="force-row" style="cursor:default;margin-bottom:8px">
      <div class="force-top"><div class="force-name">Отдача</div><div class="force-val">${eg}</div></div>
      <div class="force-bar-wrap"><div class="force-bar giving" style="width:${eg}%"></div></div>
    </div>`;

  document.getElementById('ent-content').innerHTML=`
    <div class="ent-name" style="color:${clr}">${ICONS[e.type]||'◆'} ${e.name}</div>
    <div class="ent-type" style="color:${clr}">${e.type}</div>
    ${tagsHtml}
    <div class="ent-summary">${e.summary}</div>
    ${forceHtml}
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
setInterval(tickTimedRitualClocks,1000);
setInterval(()=>{
  if(document.querySelector('#s-missions.active .quest-tool.running')){
    loadMissions();
    loadAsides();
  }
},30000);

let _prevInboxCount = -1;
setInterval(async ()=>{
  try{
    const r=await fetch('/inbox'); const inbox=await r.json();
    const count=inbox.filter(i=>!i.type||i.type==='entry').length;
    if(count !== _prevInboxCount){
      _prevInboxCount=count;
      if(document.querySelector('#s-journal.active')){loadJournal();loadAsides();}
      if(document.querySelector('#s-missions.active')) loadMissions();
      loadCharacter();
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

// ── Login screen: ultra-light pixel gate ─────────────────────────────────────
const LoginAtmo = {
  start() {},
  stop() {},
  toggleAudio() {}
};

// ── Init ─────────────────────────────────────────────────────────────────────
function bootLifeRpg(){
  checkApiStatus();
  authInit().then(()=>{
    if(localStorage.getItem('lrpg_token')){
      loadJournal(); loadAsides(); loadCharacter();
    }
  });
  if(window.innerWidth<=768){
    document.getElementById('input-bar').classList.add('mob-visible');
  }
}
if(document.readyState==='loading'){
  document.addEventListener('DOMContentLoaded',bootLifeRpg);
}else{
  bootLifeRpg();
}
</script>
<div id="login-screen">
  <div class="login-pixels"></div>
  <div class="login-shell">
    <div class="login-sigil">
      <div class="login-mark">Life RPG</div>
      <div class="login-sub">порог пути</div>
      <div class="login-oath">Стоит войти один раз, и мир начнет помнить <span>твои шаги</span>.</div>
    </div>
    <div class="login-panel">
      <div class="login-tabs">
        <button id="ls-tab-login" class="login-tab active" onclick="lsTab('login')">Войти</button>
        <button id="ls-tab-reg" class="login-tab" onclick="lsTab('reg')">Создать героя</button>
      </div>
      <input id="ls-login" class="login-input" placeholder="Логин"
        onkeydown="if(event.key==='Enter')document.getElementById('ls-pw').focus()">
      <input id="ls-pw" class="login-input" type="password" placeholder="Пароль"
        onkeydown="if(event.key==='Enter')lsSubmit()">
      <input id="ls-pw2" class="login-input" type="password" placeholder="Повтори пароль" style="display:none"
        onkeydown="if(event.key==='Enter')lsSubmit()">
      <div id="ls-err"></div>
      <button id="ls-btn" onclick="lsSubmit()">Войти</button>
      <div class="login-hint">войти в путь</div>
    </div>
  </div>
</div>
</body>
</html>"""

@app.get("/health")
def health():
    try:
        kuzu_rows(_conn.execute("MATCH (u:User) RETURN count(u)"))
        db = "ok"
    except Exception as e:
        db = str(e)
    return {"ok": db == "ok", "db": db}

@app.head("/health")
def health_head():
    return Response(status_code=200)

@app.head("/")
def root_head():
    return Response(status_code=200, headers={"Cache-Control":"no-store"})

@app.get("/", response_class=HTMLResponse)
def root():
    return HTMLResponse(HTML, headers={"Cache-Control":"no-store"})
