"""Studio UI Control Plane Bridge for RegisterHub Open-Source Community Edition.

Pure Python standard library adapter layer that exposes the AutoRegister Studio
(`edition="community"`) API contract on top of RegisterHub Lite's existing
single-thread engines (`core/ai_agent.py`, `core/llm_client.py`, `core/doctor.py`,
`core/ip_scorer.py`, `core/calibrator.py`, `core/exporter.py`, `core/scaffold.py`)
with zero additional external dependencies and 100% backward compatibility.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

_HEALTH_CACHE_LOCK = threading.Lock()
_HEALTH_CACHE: Dict[str, Any] = {"ts": 0.0, "payload": None}
_HEALTH_CACHE_TTL_S = 30.0

_BUILTIN_STORE_LOCK = threading.Lock()
_BUILTIN_MAIL_STORES: Dict[str, Any] = {}
_BUILTIN_PROXY_STORES: Dict[str, Any] = {}


def _get_builtin_mail_store(srv: Any) -> Any:
    from core.mail_builtin import BuiltinMailStore

    db_path = (Path(srv.RESULTS_DIR) / "builtin_mail.db").resolve()
    key = str(db_path)
    with _BUILTIN_STORE_LOCK:
        store = _BUILTIN_MAIL_STORES.get(key)
        if store is None or not db_path.exists():
            store = BuiltinMailStore(db_path)
            _BUILTIN_MAIL_STORES[key] = store
        return store


def _get_builtin_proxy_store(srv: Any) -> Any:
    from core.proxy_builtin import BuiltinProxyStore

    db_path = (Path(srv.RESULTS_DIR) / "builtin_proxies.db").resolve()
    key = str(db_path)
    with _BUILTIN_STORE_LOCK:
        store = _BUILTIN_PROXY_STORES.get(key)
        if store is None or not db_path.exists():
            store = BuiltinProxyStore(db_path)
            _BUILTIN_PROXY_STORES[key] = store
        return store


def _is_loopback_client(handler: Any) -> bool:
    addr = getattr(handler, "client_address", None)
    host = str(addr[0] if isinstance(addr, (tuple, list)) and addr else "").strip().lower()
    return host in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost")


def _community_kernel_status(srv: Any, store: Any) -> Dict[str, Any]:
    bin_path = Path(srv.ROOT_DIR) / "bin" / "easy_proxies.exe"
    stats = store.get_stats()
    return {
        "running": False,
        "binary_exists": bin_path.is_file(),
        "binary_label": "bin/easy_proxies.exe",
        "pid": 0,
        "subscription_count": int(stats.get("subscription_sources") or 0),
        "node_count": int(stats.get("subscription_nodes") or 0),
        "available_nodes": 0,
        "started_at": 0,
        "last_error": "",
    }


def _parse_int_param(raw: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(raw)))
    except Exception:
        return default


def _parse_bool_qs(qs: Dict[str, List[str]], key: str) -> bool:
    return str((qs.get(key) or ["0"])[0]).strip().lower() in ("1", "true", "yes", "on")


from core import __version__ as _COMMUNITY_VERSION

COMMUNITY_META: Dict[str, Any] = {
    "edition": "community",
    "brand": "RegisterHub",
    "subtitle": "开源社区工作台",
    "version": _COMMUNITY_VERSION,
    "features": {
        "fleet": True,
        "materials": False,
        "recycle": False,
        "mailhub_3100": False,
        "easy_proxies": False,
        "magpie": False,
        "editions_sync": False,
        "har_studio": True,
        "compound": False,
        "keys_vault": True,
        "notify": False,
        "pro_sites": True,
        "pro_adapters": True,
        "adapter_editor": True,
        "scheduler": False,
        "proxy_import": True,
        "accounts": True,
        "accounts_delete": True,
        "tools": True,
        "tools_extras": True,
        "password_change": False,
        "ai_agent": True,
        "builtin_mail": True,
        "builtin_proxies": True,
        "single_thread_only": True,
        "demo_mode": False,
    },
}


def _mask_key(val: str) -> str:
    s = str(val or "").strip()
    if not s:
        return ""
    if len(s) <= 10:
        return "••••••••"
    return f"{s[:6]}...{s[-4:]}"


def _classify_key(val: str) -> str:
    s = str(val or "").strip()
    if not s:
        return "普通账号"
    if s.startswith(("sk-", "gsk_", "rk_", "pk_", "api_")):
        return "API Key"
    if s.startswith("eyJ") and s.count(".") == 2:
        return "JWT Token"
    return "API Key"


_HINT_STOPWORDS = frozenset({
    "http", "https", "www", "com", "org", "net", "api", "site", "sites", "adapter",
    "base", "core", "class", "return", "import", "from", "true", "false", "none",
    "self", "page", "email", "token", "auth", "user", "name", "data", "json",
    "code", "test", "demo", "form", "signup", "register", "login", "connect",
    "byjwt", "client", "server", "account", "accounts", "password", "username",
    "config", "proxy", "mail", "html", "text", "utf", "dict", "list", "str",
    "flow", "only", "required", "protocol", "browser", "interaction",
})

_DEMO_EMAIL_DOMAINS = frozenset({
    "demo-community.org",
    "testbench-community.org",
    "example.com",
})

_SAFE_OPEN_EXTS = frozenset({".txt", ".json", ".csv", ".log", ".md", ".py", ".yaml", ".yml", ".ini"})
_UNSAFE_REL_CHARS = set("<>|*?\"\x00")


def _load_accounts_meta(srv: Any) -> Dict[str, Dict[str, Dict[str, str]]]:
    meta_path = Path(srv.ACCOUNTS_FILE).with_name("accounts_meta.json")
    if not meta_path.is_file():
        return {"by_key": {}, "by_email": {}}
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {
                "by_key": raw.get("by_key") if isinstance(raw.get("by_key"), dict) else {},
                "by_email": raw.get("by_email") if isinstance(raw.get("by_email"), dict) else {},
            }
    except Exception:
        pass
    return {"by_key": {}, "by_email": {}}


def _decode_jwt_claims_unverified(token: str) -> Dict[str, Any]:
    s = str(token or "").strip()
    if not (s.startswith("eyJ") and s.count(".") == 2):
        return {}
    try:
        seg = s.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        payload = json.loads(base64.urlsafe_b64decode(seg).decode("utf-8", errors="ignore"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _format_ts_value(val: Any) -> str:
    if val is None or val == "":
        return ""
    if isinstance(val, (int, float)) and val > 1_000_000_000:
        try:
            sec = float(val) / 1000.0 if val > 10_000_000_000 else float(val)
            return datetime.datetime.fromtimestamp(sec).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""
    s = str(val).strip()
    if len(s) >= 19 and s[4] == "-" and s[7] == "-":
        return s[:19].replace("T", " ")
    return s[:32]


def _build_adapter_hints(srv: Any, adapters: List[Dict[str, Any]]) -> Dict[str, set[str]]:
    hints: Dict[str, set[str]] = {}
    root = Path(srv.ROOT_DIR)
    for ad in adapters:
        name = str(ad.get("name") or "").strip().lower()
        if not name:
            continue
        tokens: set[str] = {name, name.replace("_", ""), name.replace("_", "-")}
        for part in re.split(r"[^a-z0-9]+", name):
            if len(part) >= 3 and part not in _HINT_STOPWORDS:
                tokens.add(part)
        url = str(ad.get("target_url") or "").strip()
        if url.startswith(("http://", "https://")):
            host = (urlparse(url).hostname or "").lower()
            if host:
                tokens.add(host)
                for part in re.split(r"[^a-z0-9]+", host):
                    if len(part) >= 3 and part not in _HINT_STOPWORDS:
                        tokens.add(part)
        rel_file = str(ad.get("file") or f"sites/{name}.py")
        fp = root / rel_file
        if fp.is_file():
            try:
                head = fp.read_text(encoding="utf-8", errors="ignore")[:1500].lower()
                for w in re.findall(r"[a-z][a-z0-9]{3,28}", head):
                    if w not in _HINT_STOPWORDS:
                        tokens.add(w)
            except Exception:
                pass
        hints[name] = tokens
    return hints


def _canonicalize_site_name(raw_site: str, adapter_names: set[str]) -> str:
    clean = str(raw_site or "").strip().lower().replace("-", "_")
    if not clean:
        return ""
    if clean in adapter_names:
        return clean
    if f"{clean}_site" in adapter_names:
        return f"{clean}_site"
    if clean.endswith("_site") and clean[:-5] in adapter_names:
        return clean[:-5]
    return clean


def _infer_project_and_time(
    acc: Dict[str, Any],
    adapters: List[Dict[str, Any]],
    meta_map: Optional[Dict[str, Dict[str, Dict[str, str]]]] = None,
    adapter_hints: Optional[Dict[str, set[str]]] = None,
) -> Tuple[str, str]:
    adapter_names = {str(ad.get("name") or "").lower() for ad in adapters if ad.get("name")}
    email_low = str(acc.get("email") or "").strip().lower()
    api_key = str(acc.get("api_key") or "").strip()
    created_at = _format_ts_value(acc.get("created_at"))

    # 1. Authoritative sidecar metadata recorded by StandardRunner.save_success
    if meta_map:
        by_key = meta_map.get("by_key") or {}
        by_email = meta_map.get("by_email") or {}
        rec = (by_key.get(f"{email_low}|{api_key}") if api_key else None) or by_email.get(email_low)
        if isinstance(rec, dict) and rec.get("site"):
            site = _canonicalize_site_name(str(rec.get("site") or ""), adapter_names)
            ts = _format_ts_value(rec.get("created_at")) or created_at
            if site:
                return site, ts

    # 2. Explicit site field if present
    explicit_site = _canonicalize_site_name(str(acc.get("site") or acc.get("project") or ""), adapter_names)
    if explicit_site:
        return explicit_site, created_at

    # 3. Retroactive JWT claim matching (e.g. provider ES256 JWT iss="acmecloud:jwt" -> acme_cloud)
    if api_key.startswith("eyJ") and api_key.count(".") == 2:
        claims = _decode_jwt_claims_unverified(api_key)
        if not created_at:
            created_at = _format_ts_value(claims.get("create_time") or claims.get("iat"))
        claim_vals: List[str] = []
        for k in ("iss", "aud", "azp", "domain", "provider"):
            v = claims.get(k)
            if isinstance(v, str):
                claim_vals.append(v)
            elif isinstance(v, list):
                claim_vals.extend(str(x) for x in v if x)
        jwt_tokens = {
            t for v in claim_vals for t in re.split(r"[^a-z0-9]+", v.lower())
            if len(t) >= 4 and t not in _HINT_STOPWORDS
        }
        if jwt_tokens and adapter_hints:
            best_name = ""
            best_score = 0
            for ad in adapters:
                aname = str(ad.get("name") or "").lower()
                if not aname:
                    continue
                overlap = len(jwt_tokens & adapter_hints.get(aname, set()))
                if overlap > best_score:
                    best_score = overlap
                    best_name = aname
            if best_name:
                return best_name, created_at

    # 4. Built-in demo / testbench email domains
    if "@" in email_low:
        dom = email_low.rsplit("@", 1)[-1]
        if dom in _DEMO_EMAIL_DOMAINS:
            demo_target = "demo_site" if "demo_site" in adapter_names else ("demo" if "demo" in adapter_names else "demo_site")
            return demo_target, created_at

    # 5. Never falsely attribute unknown historical rows to adapters[0]
    return "未归属", created_at


def _infer_project_from_account(
    acc: Dict[str, Any],
    adapters: List[Dict[str, Any]],
    meta_map: Optional[Dict[str, Dict[str, Dict[str, str]]]] = None,
    adapter_hints: Optional[Dict[str, set[str]]] = None,
) -> str:
    proj, _ = _infer_project_and_time(acc, adapters, meta_map=meta_map, adapter_hints=adapter_hints)
    return proj


def _collect_community_project_files(srv: Any, ad: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    root = Path(srv.ROOT_DIR).resolve()
    files: List[Dict[str, Any]] = []

    def _add(fp: Path, rel: str, label: str, kind: str) -> None:
        if not fp.is_file():
            return
        try:
            sz = fp.stat().st_size
        except OSError:
            sz = 0
        files.append({
            "rel": rel,
            "path": str(fp.resolve()),
            "name": fp.name,
            "label": label,
            "kind": kind,
            "size_bytes": sz,
            "exists": True,
            "is_dir": False,
        })

    ad_rel = str(ad.get("file") or f"sites/{ad['name']}.py").replace("\\", "/")
    _add(Path(srv.ACCOUNTS_FILE), "results/accounts.txt", "成号总台账", "accounts")
    _add(root / ad_rel, ad_rel, "站点适配器脚本", "extra")
    _add(Path(srv.FAILED_FILE), "results/failed.txt", "失败记录", "failed")
    _add(Path(srv.CONFIG_FILE), "config.json", "全局运行配置", "config")

    primary = next((f for f in files if f["rel"] == ad_rel), None) or (files[0] if files else None)
    if primary is None:
        primary = {
            "rel": "",
            "path": str(root),
            "name": root.name,
            "label": "工程目录",
            "exists": True,
            "is_dir": True,
            "size_bytes": 0,
        }
    return primary, files


def _open_local_path_community(target: Path, mode: str) -> None:
    if os.name == "nt":
        norm_target = os.path.normpath(str(target))
        if mode == "reveal" and target.is_file():
            subprocess.Popen(["explorer.exe", f"/select,{norm_target}"])
        else:
            os.startfile(norm_target)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        cmd = ["open", "-R", str(target)] if (mode == "reveal" and target.is_file()) else ["open", str(target)]
        subprocess.Popen(cmd)
    else:
        dest = target.parent if (mode == "reveal" and target.is_file()) else target
        subprocess.Popen(["xdg-open", str(dest)])


def enrich_agent_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(summary)
    usage = out.get("usage") if isinstance(out.get("usage"), dict) else {}
    in_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or out.get("input_tokens") or 0)
    out_tok = int(usage.get("completion_tokens") or usage.get("output_tokens") or out.get("output_tokens") or 0)
    cr_tok = int(out.get("cache_read_tokens") or usage.get("cache_read_tokens") or 0)
    cw_tok = int(out.get("cache_write_tokens") or usage.get("cache_write_tokens") or 0)
    ctx_tok = int(out.get("context_tokens") or usage.get("context_tokens") or 0)
    last_tok_s = float(out.get("last_tok_s") or usage.get("last_tok_s") or 0.0)
    last_ttft_ms = float(out.get("last_ttft_ms") or usage.get("last_ttft_ms") or 0.0)
    out["input_tokens"] = in_tok
    out["output_tokens"] = out_tok
    out["cache_read_tokens"] = cr_tok
    out["cache_write_tokens"] = cw_tok
    out["context_tokens"] = ctx_tok
    out["last_tok_s"] = last_tok_s
    out["last_ttft_ms"] = last_ttft_ms
    out.setdefault("model", "")
    out.setdefault("steps_count", int(out.get("steps") or out.get("steps_count") or 0))
    out.setdefault("queued_count", len(out.get("queued_messages") or []))
    out.setdefault("goal_active", False)
    return out


def enrich_agent_detail(detail: Dict[str, Any]) -> Dict[str, Any]:
    """Add Studio AgentSessionDetail fields (`messages`, `plan_steps`, `last_seq`, telemetry, compaction, /btw, queue, goal)."""
    out = enrich_agent_summary(detail)
    events = out.get("events") if isinstance(out.get("events"), list) else []
    messages: List[Dict[str, Any]] = []
    tool_by_call: Dict[str, Dict[str, Any]] = {}
    approval_by_id: Dict[str, Dict[str, Any]] = {}
    max_seq = 0

    for idx, ev in enumerate(events):
        if not isinstance(ev, dict):
            continue
        seq = ev.get("seq")
        if isinstance(seq, int) and seq > max_seq:
            max_seq = seq
        et = str(ev.get("type") or "")
        ts = str(ev.get("ts") or "")
        if et == "user_msg":
            messages.append({
                "id": str(ev.get("id") or f"m_u_{idx}"),
                "role": "user",
                "content": str(ev.get("content") or ""),
                "steered": bool(ev.get("steered")),
                "goal": bool(ev.get("goal")),
                "ts": ts,
            })
        elif et == "assistant_msg":
            msg_item: Dict[str, Any] = {
                "id": str(ev.get("id") or f"m_a_{idx}"),
                "role": "assistant",
                "content": str(ev.get("content") or ""),
                "ts": ts,
            }
            if ev.get("thinking"):
                msg_item["thinking"] = str(ev["thinking"])
            if isinstance(ev.get("metrics"), dict):
                msg_item["metrics"] = ev["metrics"]
                if ev["metrics"].get("model"):
                    msg_item["model"] = str(ev["metrics"]["model"])
            messages.append(msg_item)
        elif et == "compaction":
            item = ev.get("item") if isinstance(ev.get("item"), dict) else ev
            messages.append({
                "id": str(item.get("id") or f"m_cmp_{idx}"),
                "role": "compaction",
                "content": str(item.get("content") or ""),
                "tokens_before": int(item.get("tokens_before") or 0),
                "tokens_after": int(item.get("tokens_after") or 0),
                "dropped_messages": int(item.get("dropped_messages") or 0),
                "kept_messages": int(item.get("kept_messages") or 0),
                "touched_sites": item.get("touched_sites") or [],
                "touched_files": item.get("touched_files") or [],
                "auto": bool(item.get("auto", False)),
                "custom_instructions": str(item.get("custom_instructions") or ""),
                "model": str(item.get("model") or ""),
                "ts": str(item.get("ts") or ts),
            })
        elif et == "system_msg":
            messages.append({
                "id": f"m_sys_{idx}",
                "role": "system",
                "content": str(ev.get("content") or ""),
                "ts": ts,
            })
        elif et == "approval_required":
            aid = str(ev.get("approval_id") or f"app_{idx}")
            app_msg = {
                "id": aid,
                "approval_id": aid,
                "role": "approval",
                "tool_name": str(ev.get("tool_name") or ""),
                "rule_name": str(ev.get("rule_name") or ""),
                "tool_args": ev.get("tool_args") if isinstance(ev.get("tool_args"), dict) else {},
                "status": str(ev.get("status") or "pending"),
                "ts": ts,
            }
            approval_by_id[aid] = app_msg
            messages.append(app_msg)
        elif et == "approval_resolved":
            aid = str(ev.get("approval_id") or "")
            if aid and aid in approval_by_id:
                approval_by_id[aid]["status"] = str(ev.get("status") or "rejected")
        elif et == "tool_start":
            cid = str(ev.get("call_id") or f"tc_{idx}")
            entry = {
                "id": cid,
                "role": "tool",
                "tool_call_id": cid,
                "call_id": cid,
                "tool_name": str(ev.get("name") or "tool"),
                "tool_args": ev.get("args") if isinstance(ev.get("args"), dict) else {},
                "status": "running",
                "ts": ts,
            }
            tool_by_call[cid] = entry
            messages.append(entry)
        elif et == "tool_end":
            cid = str(ev.get("call_id") or f"tc_{idx}")
            res_obj = ev.get("result")
            res_str = res_obj if isinstance(res_obj, str) else json.dumps(res_obj or {}, ensure_ascii=False, indent=2)
            ok = ev.get("ok", True) is not False
            cp_id = str(ev.get("checkpoint_id") or (res_obj.get("checkpoint_id") if isinstance(res_obj, dict) else "") or "")
            existing = tool_by_call.get(cid)
            if existing is not None:
                existing["status"] = "done" if ok else "failed"
                existing["ok"] = ok
                existing["duration_ms"] = ev.get("duration_ms")
                existing["tool_result"] = res_str
                if ev.get("thinking"):
                    existing["thinking"] = str(ev["thinking"])
                if cp_id:
                    existing["checkpoint_id"] = cp_id
                if not ok:
                    existing["error"] = res_str[:300]
            else:
                t_entry: Dict[str, Any] = {
                    "id": f"m_t_{cid}_{idx}",
                    "role": "tool",
                    "tool_call_id": cid,
                    "tool_name": str(ev.get("name") or "tool"),
                    "status": "done" if ok else "failed",
                    "ok": ok,
                    "duration_ms": ev.get("duration_ms"),
                    "tool_result": res_str,
                    "ts": ts,
                }
                if ev.get("thinking"):
                    t_entry["thinking"] = str(ev["thinking"])
                if cp_id:
                    t_entry["checkpoint_id"] = cp_id
                messages.append(t_entry)

    raw_plan = out.get("plan") if isinstance(out.get("plan"), list) else []
    plan_steps = []
    for i, st in enumerate(raw_plan):
        if isinstance(st, dict):
            raw_s = str(st.get("status") or "pending")
            mapped_s = "running" if raw_s == "in_progress" else raw_s
            plan_steps.append({
                "id": f"p_{i}",
                "title": str(st.get("title") or ""),
                "status": mapped_s,
            })

    in_tok = int(out.get("input_tokens") or 0)
    out_tok = int(out.get("output_tokens") or 0)
    cr_tok = int(out.get("cache_read_tokens") or 0)
    cw_tok = int(out.get("cache_write_tokens") or 0)
    ctx_tok = int(out.get("context_tokens") or 0)
    last_tok_s = float(out.get("last_tok_s") or 0.0)
    last_ttft_ms = float(out.get("last_ttft_ms") or 0.0)
    out["messages"] = messages
    out["plan_steps"] = plan_steps
    raw_next_seq = out.get("next_seq")
    next_based = (int(raw_next_seq) - 1) if (raw_next_seq is not None and int(raw_next_seq) > 0) else -1
    out["last_seq"] = max(max_seq, next_based)
    out.setdefault("side_questions", [])
    out.setdefault("uploads", [])
    out.setdefault("queued_messages", [])
    out.setdefault("goal_state", None)
    out["usage"] = {
        "prompt_tokens": in_tok,
        "completion_tokens": out_tok,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": in_tok + out_tok,
        "cache_read_tokens": cr_tok,
        "cache_write_tokens": cw_tok,
        "context_tokens": ctx_tok,
        "last_tok_s": last_tok_s,
        "last_ttft_ms": last_ttft_ms,
    }
    return out


def _build_community_profiles_meta() -> Dict[str, Dict[str, Any]]:
    pmeta: Dict[str, Dict[str, Any]] = {}
    try:
        from core import llm_profiles as _lp

        plist = (_lp.list_profiles_response() or {}).get("profiles") or []
        for p in plist:
            if isinstance(p, dict) and p.get("id"):
                pmeta[str(p["id"])] = {
                    "name": str(p.get("name") or p["id"]),
                    "format": str(p.get("format") or "openai"),
                    "base_url": str(p.get("base_url") or ""),
                    "icon_url": "",
                }
    except Exception:
        pass
    return pmeta


def handle_studio_get(handler: Any, srv: Any, path: str, qs: Dict[str, List[str]]) -> bool:
    """Handle Studio GET routes. Returns True if handled, False to fall through."""
    if path == "/api/console/meta":
        handler._send_json(COMMUNITY_META)
        return True

    if path == "/api/dashboard":
        from core.exporter import parse_accounts_file

        adapters = srv.list_community_adapters()
        accounts = parse_accounts_file(srv.ACCOUNTS_FILE)
        meta_map = _load_accounts_meta(srv)
        adapter_hints = _build_adapter_hints(srv, adapters)
        resolved_accounts: List[Tuple[Dict[str, str], str, str]] = [
            (a, *_infer_project_and_time(a, adapters, meta_map=meta_map, adapter_hints=adapter_hints))
            for a in accounts
        ]
        fails = srv.list_failures()
        jobs = srv.job_manager.list_jobs()
        act = srv.job_manager.active_job()

        with_key_rows = [(a, p, ts) for (a, p, ts) in resolved_accounts if str(a.get("api_key") or "").strip()]
        dead_cnt = sum(1 for f in fails if f.get("category") == "dead")
        soft_cnt = sum(1 for f in fails if f.get("category") in ("soft", "captcha"))

        # 14-day trend (deduplicate failed job vs failed.txt line per day, matching /api/overview)
        today = datetime.date.today()
        day_map: Dict[str, Dict[str, int]] = {}
        job_failed_by_day: Dict[str, int] = {}
        ledger_failed_by_day: Dict[str, int] = {}
        for offset in range(13, -1, -1):
            d = today - datetime.timedelta(days=offset)
            dk = d.strftime("%Y-%m-%d")
            day_map[dk] = {"success": 0, "failed": 0}
            job_failed_by_day[dk] = 0
            ledger_failed_by_day[dk] = 0
        for j in jobs:
            day_k = str(j.get("started_at") or "")[:10]
            if day_k in day_map:
                if j.get("status") == "done":
                    day_map[day_k]["success"] += int(j.get("count") or 1)
                elif j.get("status") == "failed":
                    job_failed_by_day[day_k] += 1
        for f in fails:
            day_k = str(f.get("time") or "")[:10]
            if day_k in day_map:
                ledger_failed_by_day[day_k] += 1
        for dk in day_map:
            day_map[dk]["failed"] = max(job_failed_by_day[dk], ledger_failed_by_day[dk])

        trend = [
            {"date": k[5:], "full_date": k, "success": v["success"], "failed": v["failed"]}
            for k, v in day_map.items()
        ]
        today_k = today.strftime("%Y-%m-%d")

        key_dist: Dict[str, int] = {}
        for a in accounts:
            kind = _classify_key(str(a.get("api_key") or ""))
            key_dist[kind] = key_dist.get(kind, 0) + 1

        durations = [float(j.get("duration_s") or 0) for j in jobs if j.get("status") == "done" and j.get("duration_s")]
        avg_dur = round(sum(durations) / len(durations), 1) if durations else 0.0

        recent_jobs = [
            {**j, "project": j.get("adapter") or "demo_site", "concurrency": 1}
            for j in jobs[:10]
        ]
        recent_accounts = [
            {
                "project": proj,
                "email": a.get("email", ""),
                "has_key": bool(str(a.get("api_key") or "").strip()),
                "kind": _classify_key(str(a.get("api_key") or "")),
                "masked_key": _mask_key(str(a.get("api_key") or "")),
                "credential": str(a.get("api_key") or ""),
                "created_at": created_at,
            }
            for (a, proj, created_at) in resolved_accounts[::-1][:8]
        ]

        top_projects = [
            {
                "project": ad["name"],
                "ok_total": sum(1 for (_, p, _) in resolved_accounts if p == ad["name"]),
                "valid_keys": sum(1 for (_, p, _) in with_key_rows if p == ad["name"]),
                "retriable": 0,
                "dead": sum(1 for f in fails if f.get("site") == ad["name"] and f.get("category") == "dead"),
            }
            for ad in adapters
        ]

        llm_kpi: Dict[str, Any] = {
            "llm_tokens_7d": 0,
            "llm_tokens_today": 0,
            "llm_cache_hit_pct": None,
            "llm_calls_7d": 0,
            "llm_cost_7d_usd": 0.0,
            "llm_saved_7d_usd": 0.0,
        }
        try:
            llm_kpi = srv.llm_client.get_dashboard_llm_kpi()
        except Exception:
            pass

        handler._send_json({
            "kpi": {
                "projects_total": len(adapters),
                "active_jobs": 1 if act else 0,
                "total_ok": len(accounts),
                "valid_keys": len(with_key_rows),
                "retriable_count": soft_cnt,
                "dead_count": dead_cnt,
                "avg_duration_s": avg_dur,
                "today_ok": day_map[today_k]["success"],
                "today_failed": day_map[today_k]["failed"],
                **llm_kpi,
            },
            "daily_trend": trend,
            "key_distribution": key_dist,
            "top_projects": top_projects,
            "all_projects": [ad["name"] for ad in adapters],
            "recent_jobs": recent_jobs,
            "recent_accounts": recent_accounts,
            "external_running": [],
        })
        return True

    if path == "/api/services/health":
        force = (qs.get("force") or ["0"])[0] in ("1", "true", "yes")
        now = time.time()
        if not force:
            with _HEALTH_CACHE_LOCK:
                cached = _HEALTH_CACHE.get("payload")
                if cached is not None and (now - float(_HEALTH_CACHE.get("ts") or 0.0)) < _HEALTH_CACHE_TTL_S:
                    handler._send_json(cached)
                    return True
        report = srv.collect_doctor_report(include_network=force)
        checks = report.get("checks") or []
        services = []
        online_cnt = 0
        for idx, c in enumerate(checks):
            st = str(c.get("state") or "pass")
            mapped = "online" if st == "pass" else "degraded" if st == "warn" else "offline"
            if mapped == "online":
                online_cnt += 1
            detail = str(c.get("detail") or "")
            hint = str(c.get("hint") or "")
            services.append({
                "id": f"doc_{idx}",
                "name": str(c.get("name") or f"Check #{idx + 1}"),
                "category": "环境探针",
                "endpoint": "local",
                "status": mapped,
                "latency_ms": 0,
                "recoverable": False,
                "metrics": {},
                "detail": f"{detail} ({hint})" if hint else detail,
            })
        payload = {
            "checked_at": report.get("checked_at") or time.strftime("%H:%M:%S"),
            "summary": {
                "total": len(services),
                "online": online_cnt,
                "offline": sum(1 for s in services if s["status"] == "offline"),
                "all_core_ready": bool(report.get("ready")),
            },
            "services": services,
        }
        with _HEALTH_CACHE_LOCK:
            _HEALTH_CACHE["ts"] = time.time()
            _HEALTH_CACHE["payload"] = payload
        handler._send_json(payload)
        return True

    if path == "/api/fleet":
        from core.exporter import parse_accounts_file

        adapters = srv.list_community_adapters()
        accounts = parse_accounts_file(srv.ACCOUNTS_FILE)
        meta_map = _load_accounts_meta(srv)
        adapter_hints = _build_adapter_hints(srv, adapters)
        adapter_names = {ad["name"] for ad in adapters}
        resolved_accounts = [
            (a, *_infer_project_and_time(a, adapters, meta_map=meta_map, adapter_hints=adapter_hints))
            for a in accounts
        ]
        fails = srv.list_failures()
        snaps = srv.list_snapshots()

        # Attribute failures strictly by recorded site tag; keep unattributed legacy
        # snapshots visible on demo_site (or first adapter if no demo_site) without
        # falsely inflating failed_lines/dead_count on unrelated adapters.
        fails_by_site: Dict[str, List[Dict[str, Any]]] = {}
        unattributed_fails: List[Dict[str, Any]] = []
        for f in fails:
            fsite = _canonicalize_site_name(str(f.get("site") or ""), adapter_names)
            if fsite and fsite in adapter_names:
                fails_by_site.setdefault(fsite, []).append(f)
            else:
                unattributed_fails.append(f)

        legacy_snap_owner = "demo_site" if "demo_site" in adapter_names else (adapters[0]["name"] if adapters else "")

        projects = []
        for ad in adapters:
            name = ad["name"]
            matched = [(a, ts) for (a, p, ts) in resolved_accounts if p == name]
            valid_k = sum(1 for (a, _) in matched if str(a.get("api_key") or "").strip())
            site_fails = fails_by_site.get(name, [])
            err_src = list(site_fails)
            if name == legacy_snap_owner and unattributed_fails:
                err_src.extend(unattributed_fails)
            site_errors = [
                {
                    "dir": f.get("snapshot") or f"fail_{f.get('id', 0)}",
                    "reason": str(f.get("reason") or ""),
                    "has_screenshot": bool(f.get("snapshot_url")),
                    "screenshot_url": str(f.get("snapshot_url") or ""),
                }
                for f in err_src[:8]
            ]
            if not site_errors and name == legacy_snap_owner and snaps:
                site_errors = [
                    {
                        "dir": s["name"],
                        "reason": f"失败现场截图 ({s['mtime']})",
                        "has_screenshot": True,
                        "screenshot_url": s["url"],
                    }
                    for s in snaps[:8]
                ]
            primary_file, output_files = _collect_community_project_files(srv, ad)
            projects.append({
                "project": name,
                "target_url": ad.get("target_url") or "",
                "project_dir": str(srv.ROOT_DIR),
                "primary_file": primary_file,
                "output_files": output_files,
                "queue_count": 0,
                "ok_total": len(matched),
                "valid_keys": valid_k,
                "failed_lines": len(err_src),
                "retriable_count": 0,
                "dead_count": sum(1 for f in err_src if f.get("category") == "dead"),
                "unknown_count": 0,
                "top_reasons": [],
                "metrics": {"count": len(matched), "success_rate": None, "avg_duration_s": None},
                "recent_errors": site_errors,
            })
        handler._send_json({"projects": projects, "total": len(projects)})
        return True

    if path == "/api/fleet/external-running":
        handler._send_json({"items": []})
        return True

    if path == "/api/fleet/queue":
        handler._send_json({"queue_text": "", "count": 0})
        return True

    m_act = re.fullmatch(r"/api/fleet/([^/]+)/activity", path)
    if m_act:
        from core.exporter import parse_accounts_file

        adapters = srv.list_community_adapters()
        adapter_names = {ad["name"] for ad in adapters}
        proj = _canonicalize_site_name(m_act.group(1), adapter_names) or m_act.group(1)
        accounts = parse_accounts_file(srv.ACCOUNTS_FILE)
        meta_map = _load_accounts_meta(srv)
        adapter_hints = _build_adapter_hints(srv, adapters)
        matched_accounts = [
            (a, ts)
            for a in accounts
            for (p, ts) in [_infer_project_and_time(a, adapters, meta_map=meta_map, adapter_hints=adapter_hints)]
            if p == proj
        ]
        fails = srv.list_failures()
        matched_fails = [
            f for f in fails
            if _canonicalize_site_name(str(f.get("site") or ""), adapter_names) == proj
        ]
        rows = [
            {
                "email": a.get("email", ""),
                "password": a.get("password", ""),
                "has_key": bool(str(a.get("api_key") or "").strip()),
                "kind": _classify_key(str(a.get("api_key") or "")),
                "masked_key": _mask_key(str(a.get("api_key") or "")),
                "credential": str(a.get("api_key") or ""),
                "ip": "",
                "created_at": ts,
            }
            for (a, ts) in matched_accounts[::-1][:20]
        ]
        handler._send_json({
            "project": proj,
            "ok_total": len(matched_accounts),
            "valid_keys": sum(1 for (a, _) in matched_accounts if str(a.get("api_key") or "").strip()),
            "recent_accounts": rows,
            "recent_failures": [
                {"line": f"{f.get('time', '')} | {f.get('reason', '')}", "category": f.get("category") or "unknown"}
                for f in matched_fails[:20]
            ],
            "has_layout": srv.GEOMETRY_FILE.exists(),
            "notify_enabled": False,
            "notify_platform": "",
        })
        return True

    if path == "/api/keys/vault":
        from core.exporter import parse_accounts_file

        adapters = srv.list_community_adapters()
        accounts = parse_accounts_file(srv.ACCOUNTS_FILE)
        meta_map = _load_accounts_meta(srv)
        adapter_hints = _build_adapter_hints(srv, adapters)
        items = []
        by_project: Dict[str, int] = {}
        by_kind: Dict[str, int] = {}
        for a in accounts[::-1]:
            key = str(a.get("api_key") or "").strip()
            if not key:
                continue
            proj, created_at = _infer_project_and_time(a, adapters, meta_map=meta_map, adapter_hints=adapter_hints)
            kind = _classify_key(key)
            by_project[proj] = by_project.get(proj, 0) + 1
            by_kind[kind] = by_kind.get(kind, 0) + 1
            items.append({
                "project": proj,
                "email": a.get("email", ""),
                "password": a.get("password", ""),
                "key": key,
                "masked_key": _mask_key(key),
                "kind": kind,
                "created_at": created_at,
                "status": "unchecked",
                "detail": "",
            })
        handler._send_json({"total": len(items), "by_project": by_project, "by_kind": by_kind, "items": items})
        return True

    if path == "/api/accounts/ledger":
        from core.exporter import parse_accounts_file

        adapters = srv.list_community_adapters()
        accounts = parse_accounts_file(srv.ACCOUNTS_FILE)
        meta_map = _load_accounts_meta(srv)
        adapter_hints = _build_adapter_hints(srv, adapters)
        items = []
        by_project: Dict[str, int] = {}
        with_key = 0
        for idx, a in enumerate(accounts[::-1]):
            key = str(a.get("api_key") or "").strip()
            has_k = bool(key)
            if has_k:
                with_key += 1
            proj, created_at = _infer_project_and_time(a, adapters, meta_map=meta_map, adapter_hints=adapter_hints)
            by_project[proj] = by_project.get(proj, 0) + 1
            items.append({
                "id": f"acc_{idx}_{a.get('email', '')}",
                "project": proj,
                "email": a.get("email", ""),
                "password": a.get("password", ""),
                "credential": key,
                "has_key": has_k,
                "kind": _classify_key(key),
                "ip": "",
                "created_at": created_at,
            })
        handler._send_json({
            "total": len(items),
            "with_key": with_key,
            "without_key": len(items) - with_key,
            "by_project": by_project,
            "items": items,
        })
        return True

    if path == "/api/services/proxies":
        db_p = Path(srv.RESULTS_DIR) / "builtin_proxies.db"
        p_stats: Dict[str, Any] = {}
        if db_p.is_file():
            try:
                p_stats = _get_builtin_proxy_store(srv).get_stats()
            except Exception:
                p_stats = {}
        handler._send_json({
            "source": "builtin" if p_stats.get("total") else "community",
            "online": True,
            "latency_ms": 0,
            "total": int(p_stats.get("total") or 0),
            "available": int(p_stats.get("available") or 0),
            "clean_count": int(p_stats.get("browser_compatible") or 0),
            "nodes": [],
        })
        return True

    if path == "/api/services/ip-score/current":
        from core.ip_scorer import score_ip

        force = (qs.get("force") or ["0"])[0] in ("1", "true", "yes")
        now = time.time()
        with srv._IP_SCORE_CACHE_LOCK:
            if not force and srv._IP_SCORE_CACHE["data"] is not None and (now - float(srv._IP_SCORE_CACHE["ts"])) < srv._IP_SCORE_TTL_S:
                handler._send_json({**srv._IP_SCORE_CACHE["data"], "cached": True})
                return True
        cfg = srv._load_raw_config_dict()
        px = str((cfg.get("browser") or {}).get("proxy") or "").strip()
        res = score_ip(ip="", proxy_url=px, target_url="")
        with srv._IP_SCORE_CACHE_LOCK:
            srv._IP_SCORE_CACHE["ts"] = time.time()
            srv._IP_SCORE_CACHE["data"] = res
        handler._send_json({**res, "cached": False})
        return True

    if path == "/api/studio/adapters":
        adapters = srv.list_community_adapters()
        mapped = []
        for a in adapters:
            tier_raw = str(a.get("tier") or "template")
            tier = "verified" if tier_raw == "verified" else "partial" if tier_raw == "oauth" else "template"
            cls = "".join(p.capitalize() for p in str(a["name"]).split("_")) + "Adapter"
            mapped.append({
                "name": a["name"],
                "class_name": cls,
                "target_url": a.get("target_url") or "",
                "file": a.get("file") or f"sites/{a['name']}.py",
                "lines": int(a.get("lines") or 0),
                "tier": tier,
            })
        handler._send_json({"total": len(mapped), "adapters": mapped})
        return True

    m_st_code = re.fullmatch(r"/api/studio/adapters/([A-Za-z0-9_%-]+)/code", path)
    if m_st_code:
        name = unquote(m_st_code.group(1)).lower()
        adapters = srv.list_community_adapters()
        ad = next((a for a in adapters if a["name"] == name or a["name"] == f"{name}_site"), None)
        if not ad:
            handler._send_json({"error": f"Adapter not found: {name}"}, status=404)
            return True
        fp = (srv.ROOT_DIR / ad["file"]).resolve()
        code = fp.read_text(encoding="utf-8", errors="replace") if fp.is_file() else ""
        handler._send_json({"ok": True, "name": name, "file": ad["file"], "code": code})
        return True

    if path == "/api/layout/presets":
        if srv.GEOMETRY_FILE.exists():
            try:
                data = json.loads(srv.GEOMETRY_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        else:
            from core.calibrator import auto_generate_layout

            data = auto_generate_layout(save_path=srv.GEOMETRY_FILE)
        handler._send_json(data)
        return True

    if path == "/api/settings/config":
        cfg = srv.load_config_dict()
        b = cfg.get("browser") or {}
        d = cfg.get("defaults") or {}
        m = cfg.get("mail") or {}
        has_proxy = bool(str(b.get("proxy") or "").strip())
        has_imap = bool(str(m.get("imap_server") or "").strip() and not d.get("use_disposable_mail", True))
        proxy_mode = str(b.get("proxy_mode") or ("proxy" if has_proxy else "direct")).strip().lower()
        if proxy_mode not in ("direct", "proxy", "builtin"):
            proxy_mode = "proxy" if has_proxy else "direct"
        mail_provider = str(m.get("provider") or ("imap" if has_imap else "tempmail")).strip().lower()
        if mail_provider not in ("tempmail", "imap", "builtin"):
            mail_provider = "imap" if has_imap else "tempmail"
        handler._send_json({
            "edition": "community",
            "proxy_mode": proxy_mode,
            "proxy_residential_url": str(b.get("proxy") or ""),
            "proxy_cdp_endpoint": str(b.get("cdp_endpoint") or ""),
            "mail_provider": mail_provider,
            "mail_hub_api": "",
            "mail_cf_wildcard_domain": "",
            "mail_imap_server": str(m.get("imap_server") or ""),
            "mail_imap_port": int(m.get("imap_port") or 993),
            "mail_imap_user": str(m.get("imap_user") or ""),
            "mail_imap_password": str(m.get("imap_password") or ""),
            "run_headless": bool(b.get("headless", False)),
            "run_delay_between_s": round(float(d.get("delay_between_s") or 0.0), 2),
            "captcha_enable_cv_slider": bool(d.get("captcha_enable_cv_slider", True)),
        })
        return True

    if path == "/api/llm/profiles":
        from core import llm_profiles

        handler._send_json(llm_profiles.list_profiles_response())
        return True

    m_err_det = re.fullmatch(r"/api/fleet/([^/]+)/error-detail/([^/]+)", path)
    if m_err_det:
        err_dir = m_err_det.group(2)
        fails = srv.list_failures()
        matched = next(
            (f for f in fails if f.get("snapshot") == err_dir or f"fail_{f.get('id', 0)}" == err_dir),
            None,
        )
        handler._send_json({
            "dir": err_dir,
            "detail": matched or {"reason": f"失败快照 {err_dir}"},
            "html_excerpt": "",
        })
        return True

    if path == "/api/llm/calls":
        try:
            lim = max(1, min(500, int((qs.get("limit") or ["200"])[0])))
        except Exception:
            lim = 200
        calls = srv.llm_client.list_llm_calls(limit=lim)
        true_total = max(len(calls), srv.llm_client.count_llm_calls())
        handler._send_json({"ok": True, "calls": calls, "total": true_total})
        return True

    m_call_id = re.fullmatch(r"/api/llm/calls/([^/]+)", path)
    if m_call_id:
        item = srv.llm_client.get_llm_call(m_call_id.group(1))
        if item is None:
            handler._send_json({"ok": False, "error": "Call log not found"}, status=404)
            return True
        handler._send_json({"ok": True, "call": item})
        return True

    if path == "/api/llm/magpie/status":
        handler._send_json({"available": False, "providers": []})
        return True

    if path == "/api/llm/usage":
        rk = (qs.get("range") or ["7d"])[0]
        gran = (qs.get("granularity") or ["auto"])[0]
        pid = (qs.get("profile_id") or [""])[0]
        mdl = (qs.get("model") or [""])[0]
        try:
            pmeta = _build_community_profiles_meta()
            payload = srv.llm_client.get_usage_analytics(
                range_key=rk,
                granularity=gran,
                profile_id=pid,
                model=mdl,
                profiles_meta=pmeta,
            )
            handler._send_json(payload)
        except Exception as exc:
            handler._send_json({"ok": False, "error": f"LLM 用量统计计算失败: {exc}"}, status=500)
        return True

    if path == "/api/agent/config":
        from core.ai_agent import get_available_tools_meta, list_skills, load_agent_config

        acfg = load_agent_config()
        handler._send_json({
            "agent": acfg,
            "available_tools": get_available_tools_meta(),
            "skills": list_skills(acfg.get("enabled_skills")),
            "guard_rules": srv.agent_manager.list_guard_rules(),
            "approvals": srv.agent_manager.list_approvals(),
        })
        return True

    m_ag_skill = re.fullmatch(r"/api/agent/skills/([A-Za-z0-9_-]+)", path)
    if m_ag_skill:
        from core.ai_agent import read_skill

        try:
            handler._send_json(read_skill(m_ag_skill.group(1)))
        except ValueError as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        except KeyError as e:
            handler._send_json({"ok": False, "error": str(e)}, status=404)
        return True

    m_ag_cps = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)/checkpoints", path)
    if m_ag_cps:
        try:
            handler._send_json(srv.agent_manager.list_checkpoints(m_ag_cps.group(1)))
        except KeyError as e:
            handler._send_json({"ok": False, "error": str(e)}, status=404)
        return True

    m_ag_evs = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)/events", path)
    if m_ag_evs:
        sess = srv.agent_manager.get_session(m_ag_evs.group(1))
        if not sess:
            handler._send_json({"ok": False, "error": "Agent session not found"}, status=404)
            return True
        try:
            after_seq = int((qs.get("after_seq") or ["0"])[0])
        except Exception:
            after_seq = 0
        limit = _parse_int_param((qs.get("limit") or ["200"])[0], 200, 1, 1000)
        detail = sess.to_detail()
        all_after = [
            e
            for e in (detail.get("events") or [])
            if isinstance(e, dict) and int(e.get("seq") or 0) > after_seq
        ]
        has_more = len(all_after) > limit
        page_evs = all_after[:limit]
        next_seq_val = int(detail.get("next_seq") or 0)
        if page_evs:
            last_seq = int(page_evs[-1].get("seq") or 0)
        else:
            last_seq = max(after_seq, max(0, next_seq_val - 1))
        handler._send_json({"ok": True, "events": page_evs, "last_seq": last_seq, "has_more": has_more})
        return True

    if path == "/api/mail/builtin/formats":
        from core.mail_builtin import IMPORT_FORMATS

        handler._send_json({"formats": list(IMPORT_FORMATS.values())})
        return True

    if path == "/api/mail/builtin/accounts":
        from core.mail_builtin import IMPORT_FORMATS

        store = _get_builtin_mail_store(srv)
        status = str((qs.get("status") or ["all"])[0] or "all")
        provider_kind = str((qs.get("provider_kind") or ["all"])[0] or "all")
        query = str((qs.get("query") or [""])[0] or "")
        limit = _parse_int_param((qs.get("limit") or ["200"])[0], 200, 1, 1000)
        offset = _parse_int_param((qs.get("offset") or ["0"])[0], 0, 0, 1_000_000)
        data = store.list_accounts(
            status=status,
            provider_kind=provider_kind,
            query=query,
            limit=limit,
            offset=offset,
        )
        data["formats"] = list(IMPORT_FORMATS.values())
        handler._send_json(data)
        return True

    if path == "/api/proxies/builtin/presets":
        from core.proxy_builtin import RESIDENTIAL_PRESETS

        handler._send_json({"presets": list(RESIDENTIAL_PRESETS.values())})
        return True

    if path == "/api/proxies/builtin/entries":
        from core.proxy_builtin import RESIDENTIAL_PRESETS

        store = _get_builtin_proxy_store(srv)
        enabled_only = _parse_bool_qs(qs, "enabled_only")
        data = store.list_entries(enabled_only=enabled_only)
        data["presets"] = list(RESIDENTIAL_PRESETS.values())
        data["kernel"] = _community_kernel_status(srv, store)
        handler._send_json(data)
        return True

    if path == "/api/proxies/builtin/sources":
        store = _get_builtin_proxy_store(srv)
        kind = str((qs.get("kind") or [""])[0] or "")
        handler._send_json({
            "sources": store.list_sources(kind=kind),
            "kernel": _community_kernel_status(srv, store),
            "stats": store.get_stats(),
        })
        return True

    if path == "/api/proxies/builtin/kernel/status":
        store = _get_builtin_proxy_store(srv)
        handler._send_json({"kernel": _community_kernel_status(srv, store)})
        return True

    if path == "/api/proxies/builtin/runtime-entries":
        if not _is_loopback_client(handler):
            handler._send_json({"detail": "仅允许本机回环进程访问代理运行态 Broker 接口"}, status=403)
            return True
        store = _get_builtin_proxy_store(srv)
        browser_only = _parse_bool_qs(qs, "browser_only")
        auto_replenish = _parse_bool_qs(qs, "auto_replenish")
        handler._send_json({
            "items": store.list_runtime_entries(
                browser_only=browser_only,
                auto_replenish=auto_replenish,
            )
        })
        return True

    return False


def handle_studio_post(handler: Any, srv: Any, path: str, body: Dict[str, Any]) -> bool:
    """Handle Studio POST routes. Returns True if handled, False to fall through."""
    if path == "/api/jobs/start":
        from sites import discover_adapters

        adapter = str(body.get("project") or body.get("site") or body.get("adapter") or "").strip().lower()
        try:
            count = max(1, min(500, int(body.get("count") or 1)))
        except Exception:
            count = 1
        raw_hf = body.get("headful")
        hl_override: Optional[bool] = (not raw_hf) if isinstance(raw_hf, bool) else None
        known = set(discover_adapters().keys()) | {a["name"] for a in srv.list_community_adapters()}
        if adapter not in known:
            handler._send_json({"ok": False, "error": f"Unknown adapter: {adapter}"}, status=400)
            return True
        if srv.agent_manager.active_session_id():
            handler._send_json({"ok": False, "error": "单线程模式已有运行中的 AI 会话"}, status=409)
            return True
        try:
            job = srv.job_manager.start(adapter, count=count, headless_override=hl_override)
            jd = job.to_dict()
            jd["project"] = adapter
            jd["concurrency"] = 1
            handler._send_json({"ok": True, "job": jd})
        except RuntimeError as e:
            handler._send_json({"ok": False, "error": str(e)}, status=409)
        return True

    m_open_path = re.fullmatch(r"/api/fleet/([^/]+)/open-path", path)
    if m_open_path:
        if not _is_loopback_client(handler):
            handler._send_json({"detail": "仅允许本机回环请求打开本地路径"}, status=403)
            return True
        root = Path(srv.ROOT_DIR).resolve()
        rel_raw = str(body.get("rel_path") or "").strip().replace("\\", "/")
        mode = str(body.get("mode") or "reveal").strip().lower()
        if mode not in ("reveal", "open"):
            mode = "reveal"
        if rel_raw:
            if (
                rel_raw.startswith("/")
                or ":" in rel_raw
                or ".." in rel_raw.split("/")
                or any(ch in _UNSAFE_REL_CHARS for ch in rel_raw)
            ):
                handler._send_json({"detail": "非法的相对文件路径"}, status=400)
                return True
            target = (root / rel_raw).resolve()
            if target != root and root not in target.parents:
                handler._send_json({"detail": "路径越界：仅允许访问工程目录内的文件"}, status=400)
                return True
        else:
            target = root
        if not target.exists():
            handler._send_json({"detail": f"目标文件或目录不存在: {rel_raw or root.name}"}, status=404)
            return True
        if mode == "open" and target.is_file() and target.suffix.lower() not in _SAFE_OPEN_EXTS:
            handler._send_json({"detail": "仅允许直接打开文本/代码/数据文件，其余请使用定位模式"}, status=400)
            return True
        try:
            _open_local_path_community(target, mode)
        except OSError as exc:
            handler._send_json({"detail": f"拉起本机文件管理器失败: {exc}"}, status=500)
            return True
        handler._send_json({
            "ok": True,
            "project": m_open_path.group(1),
            "mode": mode,
            "rel_path": rel_raw,
            "path": str(target),
            "is_dir": target.is_dir(),
        })
        return True

    if path == "/api/settings/config":
        updates: Dict[str, Any] = {}
        if "proxy_mode" in body:
            mode = str(body.get("proxy_mode") or "direct").strip().lower()
            if mode in ("direct", "proxy", "builtin"):
                updates["proxy_mode"] = mode
        if "proxy_residential_url" in body:
            px = str(body.get("proxy_residential_url") or "").strip()
            eff_mode = updates.get("proxy_mode")
            updates["proxy"] = "" if eff_mode == "direct" else px
        elif updates.get("proxy_mode") == "direct":
            updates["proxy"] = ""
        if "proxy_cdp_endpoint" in body:
            updates["cdp_endpoint"] = str(body.get("proxy_cdp_endpoint") or "").strip()
        if "mail_provider" in body:
            prov = str(body.get("mail_provider") or "tempmail").strip().lower()
            if prov in ("tempmail", "imap", "builtin"):
                updates["mail_provider"] = prov
                updates["use_disposable_mail"] = prov == "tempmail"
        for k in ("mail_imap_server", "mail_imap_port", "mail_imap_user", "mail_imap_password"):
            if k in body:
                updates[k.replace("mail_", "")] = body[k]
        if "run_headless" in body:
            updates["headless"] = bool(body["run_headless"])
        if "run_delay_between_s" in body:
            try:
                updates["delay_between_s"] = round(max(0.0, min(120.0, float(body["run_delay_between_s"]))), 2)
            except Exception:
                pass
        if "captcha_enable_cv_slider" in body:
            updates["captcha_enable_cv_slider"] = bool(body["captcha_enable_cv_slider"])
        srv.save_config_dict(updates)
        handle_studio_get(handler, srv, "/api/settings/config", {})
        return True

    if path == "/api/services/ip-score":
        from core.ip_scorer import score_ip
        from core.net_guard import PrivateAddressBlockedError, validate_proxy_url, validate_public_http_url

        ip_val = str(body.get("ip") or "").strip()
        px = str(body.get("proxy_url") or "").strip()
        if "***@" in px:
            raw_cfg = srv._load_raw_config_dict()
            saved_px = str((raw_cfg.get("browser") or {}).get("proxy") or "").strip()
            if saved_px:
                px = saved_px
        target_url = str(body.get("target_url") or "").strip()
        try:
            if target_url:
                target_url = validate_public_http_url(target_url, resolve_dns=True)
            if px:
                px = validate_proxy_url(px)
        except (PrivateAddressBlockedError, ValueError) as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
            return True
        if not ip_val and not px:
            cfg = srv._load_raw_config_dict()
            px = str((cfg.get("browser") or {}).get("proxy") or "").strip()
        handler._send_json(score_ip(ip=ip_val, proxy_url=px, target_url=target_url))
        return True

    if path == "/api/services/ip-score/batch":
        from core.ip_scorer import score_batch
        from core.net_guard import PrivateAddressBlockedError, validate_proxy_url, validate_public_http_url

        source = str(body.get("source") or "custom").strip().lower()
        lim = _parse_int_param(body.get("limit"), 10, 1, 20)
        target_url = str(body.get("target_url") or "").strip()
        if target_url:
            try:
                target_url = validate_public_http_url(target_url, resolve_dns=True)
            except (PrivateAddressBlockedError, ValueError) as e:
                handler._send_json({"ok": False, "error": str(e)}, status=400)
                return True

        entries: List[Dict[str, str]] = []
        invalid_items: List[Dict[str, Any]] = []
        if source == "pool":
            store = _get_builtin_proxy_store(srv)
            pool_entries = store.list_runtime_entries(browser_only=False)
            for pe in pool_entries[:lim]:
                val = str(pe.get("proxy_url") or "").strip()
                if val:
                    entries.append({"label": str(pe.get("label") or pe.get("display_url") or val), "value": val})
            if not entries:
                cfg = srv._load_raw_config_dict()
                saved_px = str((cfg.get("browser") or {}).get("proxy") or "").strip()
                if saved_px:
                    entries.append({"label": "默认代理", "value": saved_px})
        else:
            raw_items = body.get("items") if isinstance(body.get("items"), list) else []
            for raw in raw_items[:lim]:
                s = str(raw or "").strip()
                if not s:
                    continue
                if "://" in s:
                    try:
                        s = validate_proxy_url(s)
                    except ValueError as e:
                        invalid_items.append({
                            "ok": False, "ip": "", "proxy": s, "label": s,
                            "score": 0, "grade": "D", "error": str(e), "latency_ms": 0.0,
                        })
                        continue
                entries.append({"label": s, "value": s})

        scored = score_batch(entries, target_url=target_url)
        all_items = list(scored.get("items") or []) + invalid_items
        all_items.sort(key=lambda r: (r.get("score", 0), 1 if r.get("ok") else 0), reverse=True)
        grades = {"S": 0, "A": 0, "B": 0, "C": 0, "D": 0}
        for r in all_items:
            g = str(r.get("grade") or "D")
            if g in grades:
                grades[g] += 1
        avg_score = round(sum(float(r.get("score") or 0) for r in all_items) / len(all_items), 1) if all_items else 0.0
        out_payload: Dict[str, Any] = {
            "ok": True,
            "total": len(all_items),
            "avg_score": avg_score,
            "grades": grades,
            "items": all_items,
            "results": all_items,
        }
        if scored.get("target_warning"):
            out_payload["target_warning"] = scored["target_warning"]
        handler._send_json(out_payload)
        return True

    if path == "/api/services/test-proxy":
        from core.net_guard import PrivateAddressBlockedError, build_safe_urllib_opener, validate_proxy_url, validate_public_http_url
        from core.secrets import mask_proxy_url

        cfg = srv._load_raw_config_dict()
        saved_px = str((cfg.get("browser") or {}).get("proxy") or "").strip()
        if "proxy_url" in body and body.get("proxy_url") is not None:
            px = str(body.get("proxy_url") or "").strip()
            if "***@" in px and saved_px:
                px = saved_px
        else:
            px = saved_px
        target = str(body.get("target_url") or "https://api.ipify.org?format=json").strip()

        t0 = time.perf_counter()
        try:
            clean_target = validate_public_http_url(target, resolve_dns=True)
            clean_px = validate_proxy_url(px) if px else ""
            opener = build_safe_urllib_opener(proxy=clean_px or "")
            req = urllib.request.Request(clean_target, headers={"User-Agent": "RegisterHub/2.1"})
            with opener.open(req, timeout=8) as r:
                txt = r.read(512).decode("utf-8", errors="ignore").strip()
                handler._send_json({
                    "ok": True,
                    "proxy": mask_proxy_url(clean_px) if clean_px else "(direct)",
                    "status_code": r.status,
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "body": txt[:200],
                })
        except (PrivateAddressBlockedError, ValueError, Exception) as e:
            handler._send_json({"ok": False, "error": str(e), "latency_ms": round((time.perf_counter() - t0) * 1000, 1)})
        return True

    if path == "/api/services/test-mail":
        cfg = srv._load_raw_config_dict()
        mc = cfg.get("mail") or {}
        d = cfg.get("defaults") or {}
        req_email = str(body.get("email") or "").strip()
        has_explicit_imap = bool(str(body.get("imap_server") or "").strip())
        provider = str(mc.get("provider") or ("tempmail" if d.get("use_disposable_mail", True) else "imap")).strip().lower()
        t0 = time.perf_counter()

        # 1. Explicit IMAP params or configured IMAP provider without a builtin email target
        if has_explicit_imap or (provider == "imap" and not req_email):
            from core.imap_mail import SingleImapMailClient

            raw_pw = str(body.get("imap_password") or "")
            eff_pw = str(mc.get("imap_password") or "") if (not raw_pw or raw_pw == "***") else raw_pw
            try:
                eff_port = int(body.get("imap_port") or mc.get("imap_port") or 993)
            except Exception:
                eff_port = 993
            client = SingleImapMailClient(
                server=str(body.get("imap_server") or mc.get("imap_server") or "").strip(),
                port=max(1, min(65535, eff_port)),
                user=str(body.get("imap_user") or mc.get("imap_user") or "").strip(),
                password=eff_pw,
            )
            handler._send_json(client.test_connection())
            return True

        # 2. Built-in Mail Pool (either configured provider == builtin or specified email exists in builtin store)
        if provider == "builtin" or req_email:
            store = _get_builtin_mail_store(srv)
            if req_email:
                try:
                    res = store.verify_batch(emails=[req_email], limit=1)
                    items = res.get("items") or []
                    if items:
                        first = items[0]
                        ok = str(first.get("status") or "") == "valid"
                        handler._send_json({
                            "ok": ok,
                            "provider": "builtin",
                            "email": req_email,
                            "detail": first.get("detail") or first.get("error") or ("内置邮箱验活通过" if ok else "内置邮箱验活失败"),
                            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                        })
                        return True
                except Exception as e:
                    if provider == "builtin":
                        handler._send_json({"ok": False, "provider": "builtin", "error": str(e), "latency_ms": round((time.perf_counter() - t0) * 1000, 1)})
                        return True
            if provider == "builtin":
                stats = store.get_stats()
                ok = int(stats.get("total") or 0) > 0
                handler._send_json({
                    "ok": ok,
                    "provider": "builtin",
                    "stats": stats,
                    "detail": f"内置邮箱池共 {stats.get('total', 0)} 个账号（有效 {stats.get('valid', 0)} / 可用 {stats.get('available', 0)}）"
                    if ok else "内置邮箱池为空，请先在「邮箱池」导入账号",
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                })
                return True

        # 3. Default Disposable / TempMail provider
        try:
            from core.mail_client import DisposableMailClient

            cli = DisposableMailClient()
            addr = cli.create_mailbox()
            prov_name = getattr(cli, "provider_name", None) or type(getattr(cli, "_backend", None)).__name__
            handler._send_json({
                "ok": True,
                "provider": f"tempmail ({prov_name})",
                "email": addr,
                "detail": f"临时邮箱通道连通正常，已申请测试信箱 {addr}",
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            })
        except Exception as e:
            handler._send_json({
                "ok": False,
                "provider": "tempmail",
                "error": f"临时邮箱通道探测失败: {e}",
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            })
        return True

    if path == "/api/tools/profile":
        from core.identity import generate_identity

        us_states_map: Dict[str, Tuple[str, str, str]] = {
            "DE": ("Delaware", "Wilmington", "19801"),
            "OR": ("Oregon", "Portland", "97204"),
            "MT": ("Montana", "Bozeman", "59715"),
            "NH": ("New Hampshire", "Manchester", "03101"),
            "AK": ("Alaska", "Anchorage", "99501"),
            "CA": ("California", "San Francisco", "94105"),
            "NY": ("New York", "New York", "10001"),
            "TX": ("Texas", "Austin", "78701"),
            "WA": ("Washington", "Seattle", "98101"),
            "FL": ("Florida", "Miami", "33131"),
            "CO": ("Colorado", "Denver", "80202"),
            "MA": ("Massachusetts", "Boston", "02110"),
        }
        country_geo: Dict[str, Tuple[str, str, str, str, str, str]] = {
            "US": ("United States", "Delaware", "DE", "Wilmington", "19801", "+1"),
            "UK": ("United Kingdom", "Greater London", "GL", "London", "EC1A 1BB", "+44"),
            "JP": ("Japan", "Tokyo", "13", "Shibuya-ku", "150-0002", "+81"),
            "DE": ("Germany", "Berlin", "BE", "Berlin", "10115", "+49"),
            "CA": ("Canada", "Ontario", "ON", "Toronto", "M5V 2T6", "+1"),
            "SG": ("Singapore", "Central", "SG", "Singapore", "048619", "+65"),
            "AU": ("Australia", "New South Wales", "NSW", "Sydney", "2000", "+61"),
            "FR": ("France", "Ile-de-France", "IDF", "Paris", "75008", "+33"),
            "HK": ("Hong Kong", "Hong Kong Island", "HK", "Central", "999077", "+852"),
        }
        streets = [
            "Market St", "Pine Ave", "Cedar Ln", "Oakridge Blvd", "Harbor View Rd",
            "Parkside Way", "Summit Dr", "Linden St", "Meadowbrook Ave", "Riverside Ter",
        ]
        orgs = [
            "Northstar Labs LLC", "CedarWorks Studio", "Harborline Systems", "Summit Field Digital",
            "Maplecraft Research", "Quartzline Interactive", "CivicGrid Consulting",
        ]
        roles = [
            "Software Engineer", "Product Designer", "Data Analyst", "DevOps Engineer",
            "Frontend Architect", "QA Automation Lead", "Solutions Consultant",
        ]
        req_country = str(body.get("country") or "US").strip().upper()
        if req_country not in country_geo:
            req_country = "US"
        req_state = str(body.get("state") or "").strip().upper()
        cnt = _parse_int_param(body.get("count"), 4, 1, 10)
        custom_email = str(body.get("email") or "").strip()
        ident_loc = req_country if req_country in ("US", "UK", "DE") else "US"
        c_name, def_state, def_st_code, def_city, def_zip, dial = country_geo[req_country]

        profiles: List[Dict[str, Any]] = []
        for _ in range(cnt):
            ident = generate_identity(locale=ident_loc, use_disposable=False)
            first = str(ident.get("first_name") or "Alex")
            last = str(ident.get("last_name") or "Morgan")
            st_name, city_name, zip_code, st_code = def_state, def_city, def_zip, def_st_code
            if req_country == "US":
                chosen_st = req_state if req_state in us_states_map else random.choice(list(us_states_map.keys()))
                st_name, city_name, zip_code = us_states_map[chosen_st]
                st_code = chosen_st
            elif ident_loc == req_country:
                city_name = str(ident.get("city") or def_city)
                st_code = str(ident.get("state") or def_st_code)
                zip_code = str(ident.get("postal_code") or def_zip)
            num_a = random.randint(201, 899)
            num_b = random.randint(200, 999)
            num_c = random.randint(1000, 9999)
            phone_str = f"{dial} ({num_a}) {num_b}-{num_c}" if dial == "+1" else f"{dial} {num_a}{num_b}{num_c}"
            profiles.append({
                "first_name": first,
                "last_name": last,
                "full_name": f"{first} {last}",
                "country": c_name,
                "country_code": req_country,
                "state": st_name,
                "state_code": st_code,
                "city": city_name,
                "address": f"{random.randint(100, 9899)} {random.choice(streets)}",
                "address_line2": f"Suite {random.randint(100, 880)}" if random.random() < 0.45 else "",
                "zip_code": zip_code,
                "phone": phone_str,
                "email": custom_email or str(ident.get("email") or ""),
                "birthday": str(ident.get("birth_date") or "1995-06-14"),
                "organization": random.choice(orgs),
                "job_title": random.choice(roles),
            })
        handler._send_json({
            "ok": True,
            "count": len(profiles),
            "us_states": list(us_states_map.keys()),
            "profiles": profiles,
            **profiles[0],
        })
        return True

    if path == "/api/tools/extras":
        kind = str(body.get("kind") or "tax").strip().lower()
        cc = str(body.get("country") or "US").strip().upper()
        st = str(body.get("state") or "DE").strip().upper()
        platform = str(body.get("platform") or "win_nvidia").strip().lower()
        if kind not in ("tax", "phone", "idcard", "hardware"):
            handler._send_json({"ok": False, "error": f"不支持的生成类型: {kind}"}, status=400)
            return True

        def _digits(n: int) -> str:
            return "".join(str(random.randint(0, 9)) for _ in range(n))

        def _letters(n: int) -> str:
            return "".join(random.choice("ABCDEFGHJKLMNPRSTUVWXYZ") for _ in range(n))

        data: Dict[str, Any]
        if kind == "tax":
            duns = f"{_digits(2)}-{_digits(3)}-{_digits(4)}"
            if cc == "US":
                p1, p2 = f"{random.randint(10, 98)}", _digits(7)
                data = {
                    "country_code": "US",
                    "tax_type": "US EIN (Federal Employer ID)",
                    "tax_id": f"{p1}{p2}",
                    "tax_id_formatted": f"{p1}-{p2}",
                    "duns_number": duns,
                    "company_reg_number": f"{st or 'DE'}-{_digits(7)}",
                    "vat_number": "",
                }
            elif cc in ("UK", "DE", "FR"):
                num9 = _digits(9)
                vat_prefix = "GB" if cc == "UK" else cc
                data = {
                    "country_code": cc,
                    "tax_type": f"{vat_prefix} VAT / Tax ID",
                    "tax_id": f"{vat_prefix}{num9}",
                    "tax_id_formatted": f"{vat_prefix} {num9[:3]} {num9[3:7]} {num9[7:]}",
                    "duns_number": duns,
                    "company_reg_number": f"{vat_prefix}{_digits(8)}",
                    "vat_number": f"{vat_prefix}{num9}",
                }
            else:
                num10 = _digits(10)
                data = {
                    "country_code": cc,
                    "tax_type": f"{cc} Business Tax ID",
                    "tax_id": f"{cc}{num10}",
                    "tax_id_formatted": f"{cc}-{num10[:4]}-{num10[4:]}",
                    "duns_number": duns,
                    "company_reg_number": f"REG-{cc}-{_digits(7)}",
                    "vat_number": f"{cc}{_digits(9)}",
                }
        elif kind == "phone":
            phone_meta = {
                "US": ("United States", "+1", "AT&T / T-Mobile Mobile"),
                "CA": ("Canada", "+1", "Rogers / Bell Mobile"),
                "UK": ("United Kingdom", "+44", "EE / Vodafone UK"),
                "DE": ("Germany", "+49", "Telekom / Vodafone DE"),
                "JP": ("Japan", "+81", "NTT Docomo / SoftBank"),
                "SG": ("Singapore", "+65", "Singtel / StarHub"),
                "AU": ("Australia", "+61", "Telstra / Optus"),
                "FR": ("France", "+33", "Orange / SFR"),
                "HK": ("Hong Kong", "+852", "CSL / SmarTone"),
            }
            cname, dial, carrier = phone_meta.get(cc, ("United States", "+1", "Mobile Carrier"))
            if dial == "+1":
                a, b, c = f"{random.randint(201, 899)}", f"{random.randint(200, 999)}", _digits(4)
                nat = f"{a}{b}{c}"
                fmt = f"({a}) {b}-{c}"
            elif dial in ("+65", "+852"):
                nat = f"{random.choice(['8', '9'])}{_digits(7)}"
                fmt = f"{nat[:4]} {nat[4:]}"
            else:
                nat = f"{random.choice(['7', '6', '4'])}{_digits(9)}"
                fmt = f"{nat[:3]} {nat[3:6]} {nat[6:]}"
            data = {
                "country_name": cname,
                "country_code": dial,
                "e164": f"{dial}{nat}",
                "national_number": nat,
                "formatted_national": fmt,
                "carrier_hint": carrier,
            }
        elif kind == "idcard":
            if cc == "US":
                g1, g2, g3 = f"{random.randint(100, 665)}", f"{random.randint(10, 99)}", f"{random.randint(1000, 9999)}"
                data = {
                    "country_code": "US",
                    "state_code": st or "DE",
                    "id_type": "US Synthetic ID (Mock)",
                    "id_number": f"{g1}{g2}{g3}",
                    "id_number_formatted": f"{g1}-{g2}-{g3}",
                    "passport_number": f"A{_digits(8)}",
                    "driver_license": f"{st or 'DE'}-{_letters(1)}{_digits(7)}",
                }
            else:
                raw_id = f"{cc}{_letters(2)}{_digits(8)}"
                data = {
                    "country_code": cc,
                    "state_code": "",
                    "id_type": f"{cc} National ID (Mock)",
                    "id_number": raw_id,
                    "id_number_formatted": f"{raw_id[:4]}-{raw_id[4:8]}-{raw_id[8:]}",
                    "passport_number": f"{_letters(2)}{_digits(7)}",
                    "driver_license": f"DL-{cc}-{_digits(8)}",
                }
        else:
            hw_presets = {
                "win_nvidia": {
                    "os_platform": "Win32",
                    "cpu_cores": random.choice([8, 12, 16]),
                    "device_memory_gb": random.choice([16, 32]),
                    "screen_width": random.choice([1920, 2560]),
                    "screen_height": random.choice([1080, 1440]),
                    "device_pixel_ratio": random.choice([1, 1.25, 1.5]),
                    "timezone_id": "America/New_York",
                    "locale": "en-US",
                    "webgl_vendor": "Google Inc. (NVIDIA)",
                    "webgl_renderer": random.choice([
                        "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
                        "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)",
                    ]),
                    "audio_sample_rate": 48000,
                },
                "mac_apple_silicon": {
                    "os_platform": "MacIntel",
                    "cpu_cores": random.choice([8, 10, 12]),
                    "device_memory_gb": random.choice([16, 24, 32]),
                    "screen_width": random.choice([1512, 1728, 2560]),
                    "screen_height": random.choice([982, 1117, 1440]),
                    "device_pixel_ratio": 2,
                    "timezone_id": "America/Los_Angeles",
                    "locale": "en-US",
                    "webgl_vendor": "Apple Inc.",
                    "webgl_renderer": random.choice(["Apple M2 Pro", "Apple M3", "Apple M3 Pro"]),
                    "audio_sample_rate": 48000,
                },
                "win_intel": {
                    "os_platform": "Win32",
                    "cpu_cores": random.choice([8, 12]),
                    "device_memory_gb": random.choice([8, 16]),
                    "screen_width": 1920,
                    "screen_height": 1080,
                    "device_pixel_ratio": random.choice([1, 1.25]),
                    "timezone_id": "America/Chicago",
                    "locale": "en-US",
                    "webgl_vendor": "Google Inc. (Intel)",
                    "webgl_renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
                    "audio_sample_rate": 48000,
                },
            }
            data = hw_presets.get(platform, hw_presets["win_nvidia"])
        handler._send_json({"ok": True, "kind": kind, "data": data})
        return True

    m_st_code_post = re.fullmatch(r"/api/studio/adapters/([A-Za-z0-9_-]+)/code", path)
    if m_st_code_post:
        from core.ai_agent import tool_write_adapter

        name = m_st_code_post.group(1)
        code = str(body.get("code") or "")
        if not code.strip():
            handler._send_json({"ok": False, "error": "源码内容不能为空"}, status=400)
            return True
        res = tool_write_adapter(name, code)
        if not res.get("ok"):
            handler._send_json({"ok": False, "error": res.get("error") or "保存失败"}, status=400)
            return True
        handler._send_json({
            "ok": True,
            "name": res["name"],
            "file": res["file"],
            "lines": int(res.get("lines") or len(code.splitlines())),
        })
        return True

    m_ag_skill_post = re.fullmatch(r"/api/agent/skills/([A-Za-z0-9_-]+)", path)
    if m_ag_skill_post:
        from core.ai_agent import write_skill

        try:
            res = write_skill(m_ag_skill_post.group(1), str(body.get("content") or ""))
            handler._send_json(res)
        except ValueError as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True

    if path == "/api/studio/har":
        har_text = str(body.get("har_text") or "")
        site_name = str(body.get("site_name") or "").strip()
        try:
            res = srv._parse_har_community(har_text, site_name=site_name)
            handler._send_json({
                "har_file": "upload.har",
                "site": res.get("site") or res.get("site_name") or site_name or "custom_site",
                "origin": res.get("origin") or "",
                "total_entries": int(res.get("total_entries") or len(res.get("api_steps") or [])),
                "api_steps": res.get("api_steps") or [],
                "signals": res.get("signals") or [],
                "skeleton": res.get("skeleton") or "",
                "written_path": "",
            })
        except Exception as e:
            handler._send_json({"error": f"HAR 解析失败: {e}"}, status=400)
        return True

    if path == "/api/keys/verify":
        from core.net_guard import (
            PrivateAddressBlockedError,
            build_safe_urllib_opener,
            validate_proxy_url,
            validate_public_http_url,
        )

        items = body.get("keys") if isinstance(body.get("keys"), list) else []
        base_url = str(body.get("base_url") or "").strip().rstrip("/")
        ping_chat = bool(body.get("ping_chat", False))
        explicit_model = str(body.get("model") or "").strip()
        cfg = srv._load_raw_config_dict()
        raw_px = str((cfg.get("browser") or {}).get("proxy") or "").strip()

        clean_base = ""
        base_err = ""
        opener = None
        if base_url:
            try:
                clean_base = validate_public_http_url(base_url, resolve_dns=True).rstrip("/")
                px = validate_proxy_url(raw_px) if raw_px else ""
                opener = build_safe_urllib_opener(proxy=px or "")
            except (PrivateAddressBlockedError, ValueError) as e:
                base_err = str(e)

        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()

        live_cnt = 0
        dead_cnt = 0
        unverifiable_cnt = 0
        batch = items[:200]
        _NON_CHAT_TOKENS = ("embedding", "embed", "whisper", "tts", "dall-e", "moderation", "rerank", "bge-", "audio", "realtime")
        try:
            for idx, it in enumerate(batch, 1):
                k = str((it.get("key") if isinstance(it, dict) else it) or "").strip()
                proj = str((it.get("project") if isinstance(it, dict) else "") or "demo")
                if not base_url:
                    unverifiable_cnt += 1
                    ev = {
                        "type": "item",
                        "index": idx,
                        "key": k,
                        "project": proj,
                        "ok": False,
                        "status": "unverifiable",
                        "detail": "请先填写 Base URL 再执行在线验活",
                    }
                elif base_err or opener is None:
                    dead_cnt += 1
                    ev = {
                        "type": "item",
                        "index": idx,
                        "key": k,
                        "project": proj,
                        "ok": False,
                        "status": "invalid",
                        "detail": base_err or "Base URL 校验失败",
                    }
                else:
                    models_url = f"{clean_base}/models" if clean_base.endswith("/v1") else f"{clean_base}/v1/models"
                    req = urllib.request.Request(
                        models_url,
                        headers={"Authorization": f"Bearer {k}", "Accept": "application/json", "User-Agent": "RegisterHub/2.1"},
                    )
                    t0 = time.perf_counter()
                    try:
                        with opener.open(req, timeout=10) as r:
                            raw = r.read(65536).decode("utf-8", errors="ignore")
                            cnt = 0
                            first_model = ""
                            first_chat_model = ""
                            try:
                                j = json.loads(raw)
                                if isinstance(j, dict) and isinstance(j.get("data"), list):
                                    cnt = len(j["data"])
                                    for m_item in j["data"]:
                                        mid = ""
                                        if isinstance(m_item, dict):
                                            mid = str(m_item.get("id") or "").strip()
                                        elif isinstance(m_item, str):
                                            mid = m_item.strip()
                                        if not mid:
                                            continue
                                        if not first_model:
                                            first_model = mid
                                        if not first_chat_model and not any(t in mid.lower() for t in _NON_CHAT_TOKENS):
                                            first_chat_model = mid
                            except Exception:
                                pass
                            ms = round((time.perf_counter() - t0) * 1000, 1)
                            if ping_chat:
                                chat_url = f"{clean_base}/chat/completions" if clean_base.endswith("/v1") else f"{clean_base}/v1/chat/completions"
                                chosen_model = explicit_model or first_chat_model or first_model or "gpt-4o-mini"
                                chat_payload = json.dumps({
                                    "model": chosen_model,
                                    "messages": [{"role": "user", "content": "hi"}],
                                    "max_tokens": 1,
                                }).encode("utf-8")
                                chat_req = urllib.request.Request(
                                    chat_url,
                                    data=chat_payload,
                                    headers={
                                        "Authorization": f"Bearer {k}",
                                        "Content-Type": "application/json",
                                        "Accept": "application/json",
                                        "User-Agent": "RegisterHub/2.1",
                                    },
                                    method="POST",
                                )
                                try:
                                    with opener.open(chat_req, timeout=12) as cr:
                                        cr.read(4096)
                                        ms = round((time.perf_counter() - t0) * 1000, 1)
                                        live_cnt += 1
                                        ev = {
                                            "type": "item",
                                            "index": idx,
                                            "key": k,
                                            "project": proj,
                                            "ok": True,
                                            "status": "valid",
                                            "detail": f"HTTP {r.status} · {cnt} 模型 · Chat OK ({cr.status}) · {ms}ms",
                                        }
                                except urllib.error.HTTPError as che:
                                    dead_cnt += 1
                                    ev = {
                                        "type": "item",
                                        "index": idx,
                                        "key": k,
                                        "project": proj,
                                        "ok": False,
                                        "status": "invalid",
                                        "detail": f"Models OK ({cnt}) 但 Chat 失败: HTTP {che.code}",
                                    }
                                except Exception as ce:
                                    dead_cnt += 1
                                    ev = {
                                        "type": "item",
                                        "index": idx,
                                        "key": k,
                                        "project": proj,
                                        "ok": False,
                                        "status": "invalid",
                                        "detail": f"Models OK ({cnt}) 但 Chat 异常: {str(ce)[:80]}",
                                    }
                            else:
                                live_cnt += 1
                                ev = {
                                    "type": "item",
                                    "index": idx,
                                    "key": k,
                                    "project": proj,
                                    "ok": True,
                                    "status": "valid",
                                    "detail": f"HTTP {r.status} · {cnt} 模型 · {ms}ms",
                                }
                    except urllib.error.HTTPError as he:
                        dead_cnt += 1
                        ev = {
                            "type": "item",
                            "index": idx,
                            "key": k,
                            "project": proj,
                            "ok": False,
                            "status": "invalid",
                            "detail": f"HTTP {he.code}",
                        }
                    except Exception as e:
                        dead_cnt += 1
                        ev = {
                            "type": "item",
                            "index": idx,
                            "key": k,
                            "project": proj,
                            "ok": False,
                            "status": "invalid",
                            "detail": str(e)[:120],
                        }
                handler.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                handler.wfile.flush()
            done_ev = {
                "type": "done",
                "total": len(batch),
                "live": live_cnt,
                "dead": dead_cnt,
                "unverifiable": unverifiable_cnt,
            }
            handler.wfile.write(f"data: {json.dumps(done_ev, ensure_ascii=False)}\n\n".encode("utf-8"))
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        return True

    if path == "/api/keys/oneapi":
        raw_keys = body.get("keys") if isinstance(body.get("keys"), list) else []
        clean_keys = [str(k).strip() for k in raw_keys if str(k or "").strip()]
        prefix = str(body.get("name_prefix") or "RegisterHub").strip() or "RegisterHub"
        base = str(body.get("base_url") or "https://api.openai.com").strip()
        raw_models = body.get("models")
        if isinstance(raw_models, list):
            models_str = ",".join(str(m).strip() for m in raw_models if str(m or "").strip()) or "gpt-4o-mini"
        else:
            models_str = str(raw_models or "").strip() or "gpt-4o-mini"
        aggregate = bool(body.get("aggregate", False))
        if aggregate and clean_keys:
            channels = [{
                "name": prefix,
                "type": 1,
                "key": "\n".join(clean_keys),
                "base_url": base,
                "models": models_str,
            }]
        else:
            channels = [
                {"name": f"{prefix}-{i + 1}", "type": 1, "key": k, "base_url": base, "models": models_str}
                for i, k in enumerate(clean_keys)
            ]
        handler._send_json({"ok": True, "count": len(channels), "key_count": len(clean_keys), "channels": channels})
        return True

    if path == "/api/keys/prune":
        items = body.get("items") if isinstance(body.get("items"), list) else []
        targets: List[Tuple[str, str]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            k = str(it.get("key") or "").strip()
            em = str(it.get("email") or "").strip().lower()
            if len(k) >= 6:
                targets.append((em, k))
        removed = 0
        if targets and srv.ACCOUNTS_FILE.exists():
            from core.locks import atomic_write_text_safe, interprocess_file_lock

            with interprocess_file_lock(srv.ACCOUNTS_FILE):
                lines = srv.ACCOUNTS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
                kept = []
                for ln in lines:
                    s = ln.strip()
                    if not s or s.startswith("#") or "----" not in s:
                        kept.append(ln)
                        continue
                    parts = [p.strip() for p in s.split("----", 3)]
                    em = (parts[0] if parts else "").lower()
                    pwd_col = parts[1] if len(parts) > 1 else ""
                    user_col = parts[2] if len(parts) > 2 else ""
                    ak = parts[3] if len(parts) >= 4 else ""
                    if not ak:
                        if pwd_col.startswith(("sk-", "gsk_", "rk_", "api_")):
                            ak = pwd_col
                        elif len(parts) == 3 and user_col.startswith(("sk-", "gsk_", "rk_", "api_")):
                            ak = user_col
                    matched_rm = any(
                        ((not te) or em == te) and (ak == tk)
                        for te, tk in targets
                    )
                    if matched_rm:
                        removed += 1
                    else:
                        kept.append(ln)
                if removed:
                    srv.ACCOUNTS_FILE.with_suffix(".txt.bak").write_text("\n".join(lines) + "\n", encoding="utf-8")
                    atomic_write_text_safe(srv.ACCOUNTS_FILE, "\n".join(kept) + ("\n" if kept else ""))
        handler._send_json({"ok": True, "removed": removed, "skipped_projects": []})
        return True

    if path == "/api/accounts/export":
        import csv as _csv
        import io as _io
        from core.exporter import _safe_csv_cell, parse_accounts_file

        fmt = str(body.get("format") or "csv").strip().lower()
        if fmt not in ("txt", "csv", "json", "bitwarden"):
            handler._send_json({"ok": False, "error": f"Unsupported format: {fmt}"}, status=400)
            return True
        proj_filter = str(body.get("project") or "").strip().lower()
        only_with_key = bool(body.get("only_with_key", False))
        key_filter = str(body.get("key_filter") or "").strip().lower()
        if only_with_key and not key_filter:
            key_filter = "with_key"
        search_q = str(body.get("search") or "").strip().lower()

        accounts = parse_accounts_file(srv.ACCOUNTS_FILE) if srv.ACCOUNTS_FILE.exists() else []
        filtered: List[Dict[str, Any]] = []
        for acc in accounts:
            em = str(acc.get("email") or "").strip()
            pw = str(acc.get("password") or "").strip()
            un = str(acc.get("username") or "").strip()
            ak = str(acc.get("api_key") or "").strip()
            site = str(acc.get("site") or "").strip().lower()
            if proj_filter and proj_filter != "all":
                if site and site != proj_filter and site.replace("-", "_") != proj_filter.replace("-", "_"):
                    continue
            if key_filter == "with_key" and not ak:
                continue
            if key_filter == "without_key" and ak:
                continue
            if search_q and search_q not in f"{em} {un} {ak} {site}".lower():
                continue
            filtered.append(acc)

        if fmt == "txt":
            content = "\n".join(
                f"{a.get('email', '')}----{a.get('password', '')}----{a.get('username', '')}----{a.get('api_key', '')}"
                for a in filtered
            ) + ("\n" if filtered else "")
            filename = "registerhub_accounts.txt"
        elif fmt == "json":
            content = json.dumps(filtered, ensure_ascii=False, indent=2)
            filename = "accounts.json"
        elif fmt == "bitwarden":
            buf = _io.StringIO()
            w = _csv.writer(buf)
            w.writerow([
                "folder", "favorite", "type", "name", "notes", "fields",
                "reprompt", "login_uri", "login_username", "login_password", "login_totp",
            ])
            for a in filtered:
                w.writerow([
                    "RegisterHub", "", "login",
                    _safe_csv_cell(a.get("site") or a.get("email") or ""),
                    _safe_csv_cell(f"API Key: {a.get('api_key', '')}" if a.get("api_key") else ""),
                    "", "0", "",
                    _safe_csv_cell(a.get("email") or ""),
                    _safe_csv_cell(a.get("password") or ""),
                    "",
                ])
            content = buf.getvalue()
            filename = "accounts_bitwarden.csv"
        else:
            buf = _io.StringIO()
            w = _csv.writer(buf)
            w.writerow(["email", "password", "username", "api_key"])
            for a in filtered:
                w.writerow([
                    _safe_csv_cell(a.get("email") or ""),
                    _safe_csv_cell(a.get("password") or ""),
                    _safe_csv_cell(a.get("username") or ""),
                    _safe_csv_cell(a.get("api_key") or ""),
                ])
            content = buf.getvalue()
            filename = "accounts.csv"

        handler._send_json({"ok": True, "count": len(filtered), "filename": filename, "content": content})
        return True

    if path == "/api/llm/profiles":
        from core import llm_profiles

        try:
            prof = llm_profiles.upsert_profile(body)
            handler._send_json({"ok": True, "id": prof["id"], "profile": prof})
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True

    if path == "/api/llm/models":
        from core import llm_profiles

        self_err = srv._check_not_self_llm_url(handler, str(body.get("base_url") or ""))
        if self_err:
            handler._send_json({"ok": False, "models": [], "error": self_err}, status=400)
            return True
        pid = str(body.get("profile_id") or "").strip()
        ov = llm_profiles.resolve_runtime_override(profile_id=pid, extra_overrides=body) if pid else (body if body else None)
        res = srv.llm_client.discover_models(overrides=ov)
        saved_flag = False
        if res.get("ok") and bool(body.get("save_catalog")) and pid and res.get("models"):
            updated_prof = llm_profiles.save_catalog_models(pid, res["models"], ctx_map=res.get("model_context_windows"))
            if updated_prof:
                saved_flag = True
                explicit_m = str(body.get("model") or "").strip()
                if not explicit_m and res.get("detected_context_window_k") is None:
                    inferred_u = llm_profiles.infer_model_context_window_k(
                        str(updated_prof.get("model") or ""),
                        updated_prof.get("model_context_windows"),
                    )
                    if inferred_u is not None:
                        res["detected_context_window_k"] = inferred_u
        handler._send_json({**res, "saved": saved_flag})
        return True

    if path == "/api/llm/import-vault":
        from core import llm_profiles
        from core.exporter import parse_accounts_file

        proj_filter = str(body.get("project") or "").strip().lower()
        exclude_invalid = bool(body.get("exclude_invalid", True))
        _BAD_PLACEHOLDERS = frozenset({"ok", "active", "none", "null", "n/a", "demo", "test", "pending", "registered"})
        accounts = parse_accounts_file(srv.ACCOUNTS_FILE)
        keys: List[str] = []
        for a in accounts:
            site = str(a.get("site") or "").strip().lower()
            if proj_filter and proj_filter != "all":
                if site and site != proj_filter and site.replace("-", "_") != proj_filter.replace("-", "_"):
                    continue
            ak = str(a.get("api_key") or "").strip()
            if not ak:
                continue
            if exclude_invalid and (len(ak) < 8 or ak.lower() in _BAD_PLACEHOLDERS):
                continue
            keys.append(ak)
        keys = list(dict.fromkeys(keys))
        if not keys:
            handler._send_json({"ok": False, "error": "本地账号台账中暂无可导入的 API Key"}, status=400)
            return True
        pid = str(body.get("profile_id") or "").strip()
        try:
            if pid and not bool(body.get("replace_keys")):
                prof, added = llm_profiles.mutate_profile_keys(pid, "add", keys=keys)
                if prof is None:
                    upsert_body = dict(body)
                    upsert_body["id"] = pid
                    upsert_body["api_keys"] = keys
                    prof = llm_profiles.upsert_profile(upsert_body)
                    added = len(keys)
            else:
                upsert_body = dict(body)
                if pid:
                    upsert_body["id"] = pid
                upsert_body["api_keys"] = keys
                if bool(body.get("replace_keys")):
                    upsert_body["clear_api_key"] = False
                prof = llm_profiles.upsert_profile(upsert_body)
                added = len(keys)
            handler._send_json({"ok": True, "imported_keys": added, "total_keys": prof.get("key_count", added), "profile": prof})
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True

    if path == "/api/llm/pool/config":
        handler._send_json({"ok": True, "pool": {"enabled": False}, "retry_policy": body.get("retry_policy") or {}})
        return True

    if path == "/api/llm/pool/reset":
        from core import llm_profiles

        cnt = llm_profiles.reset_pool_cooldown(str(body.get("profile_id") or ""))
        handler._send_json({"ok": True, "reset": cnt})
        return True

    m_prof_probe_reason = re.fullmatch(r"/api/llm/profiles/([^/]+)/probe-reasoning", path)
    if m_prof_probe_reason:
        from core import llm_profiles

        pid = unquote(m_prof_probe_reason.group(1))
        try:
            res = llm_profiles.probe_reasoning_capability(
                pid,
                model=str(body.get("model") or ""),
                force=bool(body.get("force", False)),
            )
            handler._send_json(res)
        except KeyError as ke:
            handler._send_json({"ok": False, "error": str(ke)}, status=404)
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True

    m_prof_export = re.fullmatch(r"/api/llm/profiles/([^/]+)/export-credentials", path)
    if m_prof_export:
        from core import llm_profiles

        pid = unquote(m_prof_export.group(1))
        kid = str(body.get("key_id") or "").strip()
        try:
            res = llm_profiles.export_profile_credentials(pid, key_id=kid)
            if res is None:
                handler._send_json({"ok": False, "error": f"供应商不存在: {pid}"}, status=404)
                return True
            handler._send_json(res)
        except KeyError as ke:
            handler._send_json({"ok": False, "error": str(ke)}, status=404)
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True

    m_prof_keys = re.fullmatch(r"/api/llm/profiles/([^/]+)/keys", path)
    if m_prof_keys:
        from core import llm_profiles

        pid = unquote(m_prof_keys.group(1))
        action = str(body.get("action") or "add").strip().lower()
        raw_keys = body.get("keys") if isinstance(body.get("keys"), list) else ([body["key"]] if body.get("key") else [])
        try:
            prof, delta = llm_profiles.mutate_profile_keys(
                pid,
                action,
                keys=[str(k) for k in raw_keys],
                key_id=str(body.get("key_id") or ""),
            )
            if prof is None:
                handler._send_json({"ok": False, "error": f"供应商不存在: {pid}"}, status=404)
                return True
            handler._send_json({"ok": True, "added": delta, "removed": delta, "profile": prof})
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True

    m_prof_act = re.fullmatch(r"/api/llm/profiles/([^/]+)/(test|activate)", path)
    if m_prof_act:
        from core import llm_profiles

        pid = unquote(m_prof_act.group(1))
        action = m_prof_act.group(2)
        if action == "activate":
            prof = llm_profiles.activate_profile(pid)
            if prof is None:
                handler._send_json({"ok": False, "error": f"供应商不存在: {pid}"}, status=404)
                return True
            handler._send_json({"ok": True, "active_id": pid, "profile": prof})
        else:
            self_err = srv._check_not_self_llm_url(handler, str(body.get("base_url") or ""))
            if self_err:
                handler._send_json({"ok": False, "error": self_err}, status=400)
                return True
            ov = llm_profiles.resolve_runtime_override(
                profile_id=pid,
                model_override=str(body.get("model") or ""),
                key_id=str(body.get("key_id") or ""),
                extra_overrides=body if body else None,
            )
            res = srv.llm_client.test_connection(overrides=ov)
            llm_profiles.record_profile_test_result(
                pid,
                ok=bool(res.get("ok")),
                latency_ms=float(res.get("latency_ms") or 0.0),
                error=str(res.get("error") or ""),
            )
            handler._send_json(res)
        return True

    if path == "/api/agent/sessions/import-handoff":
        raw_md = str(body.get("markdown") or "").strip()
        if not raw_md:
            handler._send_json({"ok": False, "error": "接力单内容不能为空"}, status=400)
            return True
        if len(raw_md) > 300_000:
            handler._send_json({"ok": False, "error": "接力单内容超过 300KB 上限"}, status=400)
            return True
        try:
            res = srv.agent_manager.import_session_handoff(
                raw_md,
                auto_start=bool(body.get("auto_start", False)),
                llm_profile_id=str(body.get("llm_profile_id") or body.get("profile_id") or "").strip(),
                llm_model=str(body.get("llm_model") or "").strip(),
            )
            if isinstance(res.get("session"), dict):
                res["session"] = enrich_agent_detail(res["session"])
            handler._send_json(res)
        except ValueError as ve:
            handler._send_json({"ok": False, "error": str(ve)}, status=400)
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=500)
        return True

    m_ag_share = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)/share", path)
    if m_ag_share:
        sid = m_ag_share.group(1)
        try:
            res = srv.agent_manager.export_session_handoff(
                sid,
                include_code=bool(body.get("include_code", True)),
                use_llm_refine=bool(body.get("use_llm_refine", False)),
            )
            handler._send_json(res)
        except KeyError as ke:
            handler._send_json({"ok": False, "error": str(ke)}, status=404)
        except ValueError as ve:
            handler._send_json({"ok": False, "error": str(ve)}, status=400)
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=500)
        return True

    m_ag_btw = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)/side-question/stream", path)
    if m_ag_btw:
        sid = m_ag_btw.group(1)
        sess = srv.agent_manager.get_session(sid)
        if not sess:
            handler._send_json({"ok": False, "error": "Agent session not found"}, status=404)
            return True
        question = str(body.get("question") or "").strip()
        if not question:
            handler._send_json({"ok": False, "error": "提问内容不能为空"}, status=400)
            return True

        prof_id = str(body.get("llm_profile_id") or body.get("profile_id") or "").strip()
        mod_ov = str(body.get("llm_model") or "").strip()

        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()

        stop_ev = threading.Event()

        def _write_sse(ev: Dict[str, Any]) -> None:
            try:
                handler.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                stop_ev.set()

        try:
            srv.agent_manager.stream_side_question(
                sid,
                question,
                llm_profile_id=prof_id,
                llm_model=mod_ov,
                on_event=_write_sse,
                stop_event=stop_ev,
            )
        except Exception as e:
            _write_sse({"type": "error", "ok": False, "error": str(e)})
        return True

    m_ag_btw_sync = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)/side-question", path)
    if m_ag_btw_sync:
        sid = m_ag_btw_sync.group(1)
        try:
            res = srv.agent_manager.ask_side_question(
                sid,
                str(body.get("question") or ""),
                llm_profile_id=str(body.get("llm_profile_id") or body.get("profile_id") or ""),
                llm_model=str(body.get("llm_model") or ""),
            )
            handler._send_json(res)
        except KeyError as e:
            handler._send_json({"ok": False, "error": str(e)}, status=404)
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True

    m_ag_up = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)/upload", path)
    if m_ag_up:
        import base64

        sid = m_ag_up.group(1)
        sess = srv.agent_manager.get_session(sid)
        if not sess:
            handler._send_json({"ok": False, "error": "Agent session not found"}, status=404)
            return True
        raw_name = Path(str(body.get("filename") or body.get("name") or "upload.bin")).name
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name).strip("._") or "upload.bin"
        b64 = str(body.get("content_b64") or body.get("content_base64") or "")
        try:
            data = base64.b64decode(b64, validate=False)
        except Exception as e:
            handler._send_json({"ok": False, "error": f"附件编码非法: {e}"}, status=400)
            return True
        if len(data) > 5 * 1024 * 1024:
            handler._send_json({"ok": False, "error": "附件超过 5MB 上限"}, status=400)
            return True
        up_dir = (srv.RESULTS_DIR / "agent_uploads" / sid).resolve()
        up_dir.mkdir(parents=True, exist_ok=True)
        dest = (up_dir / safe_name).resolve()
        if dest.parent != up_dir:
            handler._send_json({"ok": False, "error": "非法附件路径"}, status=400)
            return True
        dest.write_bytes(data)
        rel_path = f"results/agent_uploads/{sid}/{safe_name}"
        file_meta = {
            "name": safe_name,
            "path": rel_path,
            "size_bytes": len(data),
            "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with sess._lock:
            sess.uploads = [u for u in sess.uploads if u.get("name") != safe_name]
            sess.uploads.append(file_meta)
        sess.emit("file_uploaded", file=dict(file_meta))
        sess.persist()
        handler._send_json({"ok": True, "file": file_meta})
        return True

    m_ag_compact = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)/compact", path)
    if m_ag_compact:
        sid = m_ag_compact.group(1)
        try:
            raw_keep = body.get("keep_recent_tokens")
            keep_tok = int(raw_keep) if raw_keep not in (None, "") else None
            res = srv.agent_manager.compact_session(
                sid,
                custom_instructions=str(body.get("custom_instructions") or ""),
                keep_recent_tokens=keep_tok,
                llm_profile_id=str(body.get("llm_profile_id") or body.get("profile_id") or ""),
                llm_model=str(body.get("llm_model") or ""),
            )
            if isinstance(res.get("session"), dict):
                res["session"] = enrich_agent_detail(res["session"])
            handler._send_json(res)
        except KeyError as e:
            handler._send_json({"ok": False, "error": str(e)}, status=404)
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True

    m_ag_cp_restore = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)/checkpoints/([A-Za-z0-9_-]+)/restore", path)
    if m_ag_cp_restore:
        try:
            res = srv.agent_manager.restore_checkpoint(m_ag_cp_restore.group(1), m_ag_cp_restore.group(2))
            handler._send_json(res)
        except KeyError as e:
            handler._send_json({"ok": False, "error": str(e)}, status=404)
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True

    m_ag_q_steer = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)/queue/([A-Za-z0-9_-]+)/steer", path)
    if m_ag_q_steer:
        try:
            res = srv.agent_manager.promote_queued_message(m_ag_q_steer.group(1), m_ag_q_steer.group(2))
            if isinstance(res.get("session"), dict):
                res["session"] = enrich_agent_detail(res["session"])
            handler._send_json(res)
        except KeyError as e:
            handler._send_json({"ok": False, "error": str(e)}, status=404)
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True

    if path == "/api/agent/guard":
        try:
            handler._send_json(srv.agent_manager.upsert_guard_rule(body))
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True

    if path == "/api/agent/guard/test":
        handler._send_json(srv.agent_manager.test_guard_rule(str(body.get("tool") or "*"), body.get("args")))
        return True

    m_ag_app = re.fullmatch(r"/api/agent/approvals/([A-Za-z0-9_-]+)", path)
    if m_ag_app:
        try:
            res = srv.agent_manager.resolve_approval(m_ag_app.group(1), str(body.get("decision") or "reject"))
            handler._send_json(res)
        except KeyError as e:
            handler._send_json({"ok": False, "error": str(e)}, status=404)
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True

    # ------------------------------------------------------------------
    # 内置邮箱池 (/api/mail/builtin/*)
    # ------------------------------------------------------------------
    if path == "/api/mail/builtin/preview":
        from core.secrets import sanitize_error_text

        text = str(body.get("text") or "")
        if not text.strip() or len(text) > 2_000_000:
            handler._send_json({"detail": "待预览文本不能为空且不能超过 2MB"}, status=400)
            return True
        store = _get_builtin_mail_store(srv)
        try:
            res = store.preview_import(
                text,
                fmt=str(body.get("format") or "auto")[:64],
                delimiter=str(body.get("delimiter") or "auto")[:16],
                limit=_parse_int_param(body.get("limit"), 8, 1, 20),
            )
            handler._send_json(res)
        except ValueError as e:
            handler._send_json({"detail": sanitize_error_text(str(e))}, status=400)
        return True

    if path == "/api/mail/builtin/import":
        from core.secrets import sanitize_error_text

        text = str(body.get("text") or "")
        if not text.strip() or len(text) > 2_000_000:
            handler._send_json({"detail": "待导入文本不能为空且不能超过 2MB"}, status=400)
            return True
        store = _get_builtin_mail_store(srv)
        try:
            res = store.import_accounts(
                text,
                fmt=str(body.get("format") or "auto")[:64],
                delimiter=str(body.get("delimiter") or "auto")[:16],
                overwrite_existing=bool(body.get("overwrite_existing", False)),
            )
            handler._send_json(res)
        except ValueError as e:
            handler._send_json({"detail": sanitize_error_text(str(e))}, status=400)
        except Exception as e:
            handler._send_json({"detail": sanitize_error_text(str(e))}, status=500)
        return True

    if path == "/api/mail/builtin/verify":
        store = _get_builtin_mail_store(srv)
        emails = body.get("emails") if isinstance(body.get("emails"), list) else []
        clean_emails = [str(e) for e in emails[:200] if str(e or "").strip()]
        lim = _parse_int_param(body.get("limit"), 50, 1, 200)
        handler._send_json(store.verify_batch(emails=clean_emails or None, limit=lim))
        return True

    if path == "/api/mail/builtin/inbox":
        from core.secrets import sanitize_error_text

        em = str(body.get("email") or "").strip()
        if len(em) < 3 or len(em) > 254:
            handler._send_json({"detail": "邮箱地址格式非法"}, status=400)
            return True
        store = _get_builtin_mail_store(srv)
        try:
            res = store.inspect_inbox(
                em,
                top=_parse_int_param(body.get("top"), 8, 1, 20),
                force=bool(body.get("force", False)),
            )
            handler._send_json(res)
        except ValueError as e:
            handler._send_json({"detail": str(e)}, status=404)
        except Exception as e:
            handler._send_json({"detail": sanitize_error_text(str(e))}, status=502)
        return True

    if path == "/api/mail/builtin/message":
        from core.secrets import sanitize_error_text

        em = str(body.get("email") or "").strip()
        mid = str(body.get("msg_id") or "").strip()
        if not em or not mid:
            handler._send_json({"detail": "缺少 email 或 msg_id"}, status=400)
            return True
        store = _get_builtin_mail_store(srv)
        try:
            handler._send_json(store.inspect_message(em, mid))
        except (ValueError, KeyError) as e:
            handler._send_json({"detail": str(e)}, status=404)
        except Exception as e:
            handler._send_json({"detail": sanitize_error_text(str(e))}, status=502)
        return True

    if path == "/api/mail/builtin/messages":
        from core.secrets import sanitize_error_text

        em = str(body.get("email") or "").strip()
        if len(em) < 3:
            handler._send_json({"detail": "邮箱地址格式非法"}, status=400)
            return True
        try:
            since_val = max(0.0, float(body.get("since") or 0.0))
        except Exception:
            since_val = 0.0
        store = _get_builtin_mail_store(srv)
        try:
            msgs = store.list_messages(em, top=_parse_int_param(body.get("top"), 8, 1, 20), since=since_val)
            handler._send_json({"email": em.lower(), "messages": msgs})
        except ValueError as e:
            handler._send_json({"detail": str(e)}, status=404)
        except Exception as e:
            handler._send_json({"detail": sanitize_error_text(str(e))}, status=502)
        return True

    if path == "/api/mail/builtin/delete":
        store = _get_builtin_mail_store(srv)
        emails = body.get("emails") if isinstance(body.get("emails"), list) else []
        deleted = store.delete_accounts([str(e) for e in emails[:500]])
        handler._send_json({"deleted": deleted, "stats": store.get_stats()})
        return True

    if path == "/api/mail/builtin/cleanup-dead":
        store = _get_builtin_mail_store(srv)
        deleted = store.cleanup_dead_accounts()
        handler._send_json({"deleted": deleted, "stats": store.get_stats()})
        return True

    if path == "/api/mail/builtin/reset":
        store = _get_builtin_mail_store(srv)
        emails = body.get("emails") if isinstance(body.get("emails"), list) else []
        clean_emails = [str(e) for e in emails[:500] if str(e or "").strip()]
        reset_n = store.reset_accounts(clean_emails or None)
        handler._send_json({"reset": reset_n, "stats": store.get_stats()})
        return True

    if path == "/api/mail/builtin/lease":
        store = _get_builtin_mail_store(srv)
        owner = str(body.get("owner") or "runner").strip()[:80] or "runner"
        ttl_s = _parse_int_param(body.get("ttl_s"), 900, 30, 7200)
        include_unknown = bool(body.get("include_unknown", False))
        acc = store.lease_account(owner=owner, ttl_s=ttl_s, include_unknown=include_unknown)
        handler._send_json({"account": acc, "stats": store.get_stats()})
        return True

    if path == "/api/mail/builtin/release":
        store = _get_builtin_mail_store(srv)
        em = str(body.get("email") or "").strip()
        if len(em) < 3:
            handler._send_json({"detail": "邮箱地址格式非法"}, status=400)
            return True
        mark = body.get("mark_status")
        if mark is not None and str(mark) not in ("valid", "dead", "unknown"):
            handler._send_json({"detail": "mark_status 仅支持 valid / dead / unknown"}, status=400)
            return True
        ok = store.release_lease(
            em,
            owner=str(body.get("owner") or "")[:80],
            mark_status=str(mark) if mark is not None else None,
        )
        handler._send_json({"released": ok, "stats": store.get_stats()})
        return True

    # ------------------------------------------------------------------
    # 统一内置代理池 (/api/proxies/builtin/*)
    # ------------------------------------------------------------------
    if path == "/api/proxies/builtin/import":
        from core.secrets import sanitize_error_text

        text = str(body.get("text") or "")
        if not text.strip() or len(text) > 1_000_000:
            handler._send_json({"detail": "代理文本不能为空且不能超过 1MB"}, status=400)
            return True
        scheme = str(body.get("default_scheme") or "http").strip().lower()
        if scheme not in ("http", "https", "socks5", "socks5h"):
            handler._send_json({"detail": "default_scheme 仅支持 http / https / socks5 / socks5h"}, status=400)
            return True
        store = _get_builtin_proxy_store(srv)
        try:
            handler._send_json(store.import_static_proxies(text, default_scheme=scheme))
        except ValueError as e:
            handler._send_json({"detail": sanitize_error_text(str(e))}, status=400)
        return True

    if path == "/api/proxies/builtin/gateway":
        from core.secrets import sanitize_error_text

        gw_url = str(body.get("gateway_url") or "").strip()
        if len(gw_url) < 5 or len(gw_url) > 1024:
            handler._send_json({"detail": "请填写合法的动态住宅代理网关地址"}, status=400)
            return True
        store = _get_builtin_proxy_store(srv)
        try:
            res = store.add_residential_gateway(
                gateway_url=gw_url,
                label=str(body.get("label") or "")[:80],
                preset=str(body.get("preset") or "smartproxy")[:32],
                session_template=str(body.get("session_template") or "")[:160],
                country=str(body.get("country") or "us")[:16],
            )
            handler._send_json(res)
        except ValueError as e:
            handler._send_json({"detail": sanitize_error_text(str(e))}, status=400)
        return True

    if path == "/api/proxies/builtin/probe":
        from core.net_guard import validate_public_http_url

        target_url = str(body.get("target_url") or "").strip()
        if target_url:
            try:
                target_url = validate_public_http_url(target_url, resolve_dns=True)
            except ValueError as e:
                handler._send_json({"detail": str(e)}, status=400)
                return True
        raw_ids = body.get("ids") if isinstance(body.get("ids"), list) else []
        clean_ids: List[int] = []
        for x in raw_ids[:200]:
            try:
                clean_ids.append(int(x))
            except Exception:
                pass
        lim = _parse_int_param(body.get("limit"), 50, 1, 200)
        store = _get_builtin_proxy_store(srv)
        handler._send_json(store.probe_batch(ids=clean_ids or None, target_url=target_url, limit=lim))
        return True

    if path == "/api/proxies/builtin/toggle":
        raw_ids = body.get("ids") if isinstance(body.get("ids"), list) else []
        clean_ids = [int(x) for x in raw_ids[:500] if str(x).isdigit()]
        if not clean_ids:
            handler._send_json({"detail": "请至少选择一条代理"}, status=400)
            return True
        store = _get_builtin_proxy_store(srv)
        updated = store.toggle_entries(clean_ids, enabled=bool(body.get("enabled", True)))
        handler._send_json({"updated": updated, "stats": store.get_stats()})
        return True

    if path == "/api/proxies/builtin/delete":
        raw_ids = body.get("ids") if isinstance(body.get("ids"), list) else []
        clean_ids = [int(x) for x in raw_ids[:500] if str(x).isdigit()]
        if not clean_ids:
            handler._send_json({"detail": "请至少选择一条代理"}, status=400)
            return True
        store = _get_builtin_proxy_store(srv)
        deleted = store.delete_entries(clean_ids)
        handler._send_json({"deleted": deleted, "stats": store.get_stats()})
        return True

    if path == "/api/proxies/builtin/cleanup-dead":
        store = _get_builtin_proxy_store(srv)
        deleted = store.cleanup_unavailable_entries()
        handler._send_json({"deleted": deleted, "stats": store.get_stats()})
        return True

    if path == "/api/proxies/builtin/sources":
        from core.net_guard import PrivateAddressBlockedError, validate_public_http_url
        from core.secrets import sanitize_error_text

        kind = str(body.get("kind") or "").strip().lower()
        url = str(body.get("url") or "").strip()
        scheme = str(body.get("default_scheme") or "http").strip().lower()
        if kind not in ("api_extract", "subscription"):
            handler._send_json({"detail": "kind 仅支持 api_extract 或 subscription"}, status=400)
            return True
        if len(url) < 8 or len(url) > 2048:
            handler._send_json({"detail": "请填写合法的代理源 URL"}, status=400)
            return True
        try:
            validate_public_http_url(url, resolve_dns=False)
        except (PrivateAddressBlockedError, ValueError) as e:
            handler._send_json({"detail": sanitize_error_text(str(e))}, status=400)
            return True
        if scheme not in ("http", "https", "socks5", "socks5h"):
            handler._send_json({"detail": "default_scheme 仅支持 http / https / socks5 / socks5h"}, status=400)
            return True
        store = _get_builtin_proxy_store(srv)
        try:
            res = store.add_source(
                kind=kind,
                url=url,
                label=str(body.get("label") or "")[:80],
                default_scheme=scheme,
                min_available=_parse_int_param(body.get("min_available"), 3, 1, 100),
                ttl_minutes=_parse_int_param(body.get("ttl_minutes"), 15, 1, 1440),
            )
        except ValueError as e:
            handler._send_json({"detail": sanitize_error_text(str(e))}, status=400)
            return True
        if bool(body.get("pull_now", False)) and kind == "api_extract" and res.get("source"):
            pull_res = store.pull_api_source(int(res["source"]["id"]))
            res["pull"] = pull_res
            res["sources"] = store.list_sources()
            res["stats"] = store.get_stats()
        res["kernel"] = _community_kernel_status(srv, store)
        handler._send_json(res)
        return True

    m_px_src_toggle = re.fullmatch(r"/api/proxies/builtin/sources/(\d+)/toggle", path)
    if m_px_src_toggle:
        sid = int(m_px_src_toggle.group(1))
        store = _get_builtin_proxy_store(srv)
        ok = store.toggle_source(sid, enabled=bool(body.get("enabled", True)))
        if not ok:
            handler._send_json({"detail": "代理源不存在"}, status=404)
            return True
        handler._send_json({
            "ok": True,
            "sources": store.list_sources(),
            "kernel": _community_kernel_status(srv, store),
            "stats": store.get_stats(),
        })
        return True

    m_px_src_pull = re.fullmatch(r"/api/proxies/builtin/sources/(\d+)/pull", path)
    if m_px_src_pull:
        sid = int(m_px_src_pull.group(1))
        store = _get_builtin_proxy_store(srv)
        res = store.pull_api_source(sid)
        if not res.get("ok") and res.get("error") == "API 提取源不存在":
            handler._send_json({"detail": "API 提取源不存在"}, status=404)
            return True
        handler._send_json(res)
        return True

    if path in ("/api/proxies/builtin/kernel/start", "/api/proxies/builtin/kernel/stop", "/api/proxies/builtin/kernel/sync"):
        store = _get_builtin_proxy_store(srv)
        handler._send_json({
            "ok": False,
            "error": "开源社区版采用零外部依赖设计：静态代理、动态住宅网关与 API 提取链接已全部开箱可用；订阅托管内核需额外配置 bin/easy_proxies.exe。",
            "kernel": _community_kernel_status(srv, store),
        })
        return True

    if path == "/api/proxies/builtin/materialize-gateway":
        if not _is_loopback_client(handler):
            handler._send_json({"detail": "仅允许本机回环进程访问代理运行态 Broker 接口"}, status=403)
            return True
        try:
            eid = int(body.get("id") or 0)
        except Exception:
            eid = 0
        if eid < 1:
            handler._send_json({"detail": "缺少合法的网关 id"}, status=400)
            return True
        store = _get_builtin_proxy_store(srv)
        raw_sid = body.get("session_id")
        entry = store.materialize_gateway_by_id(
            eid,
            session_id=str(raw_sid)[:32] if raw_sid is not None else None,
        )
        if not entry:
            handler._send_json({"detail": "动态住宅网关不存在或已禁用"}, status=404)
            return True
        handler._send_json({"entry": entry})
        return True

    if path == "/api/services/proxies/import":
        from core.secrets import sanitize_error_text

        store = _get_builtin_proxy_store(srv)
        raw_text = str(body.get("text") or "")
        if not raw_text.strip() or len(raw_text) > 1_000_000:
            handler._send_json({"ok": False, "detail": "请粘贴至少一条代理记录且不超过 1MB"}, status=400)
            return True
        scheme = str(body.get("default_scheme") or "http").strip().lower()
        if scheme not in ("http", "https", "socks5", "socks5h"):
            scheme = "http"
        try:
            res = store.import_static_proxies(raw_text, default_scheme=scheme)
        except ValueError as e:
            handler._send_json({"ok": False, "detail": sanitize_error_text(str(e))}, status=400)
            return True
        stats = res.get("stats") or store.get_stats()
        added = int(res.get("added", res.get("imported", 0))) + int(res.get("updated", 0))
        handler._send_json({
            "ok": True,
            "added": added,
            "updated": int(res.get("updated", 0)),
            "skipped": int(res.get("skipped", 0)),
            "invalid": len(res.get("errors") or []),
            "total": int(stats.get("total", 0)),
            "stats": stats,
        })
        return True

    if path == "/api/agent/config":
        from core.ai_agent import get_available_tools_meta, list_skills, save_agent_config

        try:
            saved = save_agent_config(body)
            handler._send_json({
                "ok": True,
                "agent": saved,
                "available_tools": get_available_tools_meta(),
                "skills": list_skills(saved.get("enabled_skills")),
                "guard_rules": srv.agent_manager.list_guard_rules(),
                "approvals": srv.agent_manager.list_approvals(),
            })
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True

    return False


def handle_studio_put(handler: Any, srv: Any, path: str, body: Dict[str, Any]) -> bool:
    """Handle Studio PUT routes."""
    m_prof = re.fullmatch(r"/api/llm/profiles/([^/]+)", path)
    if m_prof:
        from core import llm_profiles

        pid = unquote(m_prof.group(1))
        try:
            prof = llm_profiles.upsert_profile({**body, "id": pid})
            handler._send_json({"ok": True, "id": prof["id"], "profile": prof})
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True
    return False


def handle_studio_patch(handler: Any, srv: Any, path: str, body: Dict[str, Any]) -> bool:
    """Handle Studio PATCH routes."""
    m_prof = re.fullmatch(r"/api/llm/profiles/([^/]+)", path)
    if m_prof:
        from core import llm_profiles

        pid = unquote(m_prof.group(1))
        try:
            prof = llm_profiles.upsert_profile({**body, "id": pid})
            handler._send_json({"ok": True, "id": prof["id"], "profile": prof})
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True
    return False


def handle_studio_delete(
    handler: Any,
    srv: Any,
    path: str,
    qs: Optional[Dict[str, List[str]]] = None,
) -> bool:
    """Handle Studio DELETE routes."""
    m_prof = re.fullmatch(r"/api/llm/profiles/([^/]+)", path)
    if m_prof:
        from core import llm_profiles

        pid = unquote(m_prof.group(1))
        ok = llm_profiles.delete_profile(pid)
        if not ok:
            handler._send_json({"ok": False, "error": f"供应商不存在: {pid}"}, status=404)
            return True
        handler._send_json({"ok": True, "deleted": pid})
        return True

    m_ag_guard_del = re.fullmatch(r"/api/agent/guard/([A-Za-z0-9_-]+)", path)
    if m_ag_guard_del:
        try:
            handler._send_json(srv.agent_manager.delete_guard_rule(m_ag_guard_del.group(1)))
        except KeyError as e:
            handler._send_json({"ok": False, "error": str(e)}, status=404)
        return True

    m_ag_q_del = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)/queue/([A-Za-z0-9_-]+)", path)
    if m_ag_q_del:
        try:
            handler._send_json(srv.agent_manager.remove_queued_message(m_ag_q_del.group(1), m_ag_q_del.group(2)))
        except KeyError as e:
            handler._send_json({"ok": False, "error": str(e)}, status=404)
        return True

    m_px_src_del = re.fullmatch(r"/api/proxies/builtin/sources/(\d+)", path)
    if m_px_src_del:
        sid = int(m_px_src_del.group(1))
        store = _get_builtin_proxy_store(srv)
        res = store.delete_source(sid)
        if not res.get("deleted"):
            handler._send_json({"detail": "代理源不存在"}, status=404)
            return True
        res["kernel"] = _community_kernel_status(srv, store)
        handler._send_json(res)
        return True

    m_fl_err = re.fullmatch(r"/api/fleet/([^/]+)/errors", path)
    if m_fl_err:
        from core.locks import atomic_write_text_safe, interprocess_file_lock

        adapters = srv.list_community_adapters()
        adapter_names = {ad["name"] for ad in adapters}
        raw_proj = unquote(m_fl_err.group(1))
        req_proj = _canonicalize_site_name(raw_proj, adapter_names) or raw_proj.strip().lower()
        legacy_snap_owner = "demo_site" if "demo_site" in adapter_names else (adapters[0]["name"] if adapters else "")
        keep_latest = 0
        if qs and "keep_latest" in qs:
            keep_latest = _parse_int_param((qs.get("keep_latest") or ["0"])[0], 0, 0, 5000)

        def _line_site_and_snap(raw_line: str) -> Tuple[str, str]:
            parts = [p.strip() for p in raw_line.split("|")]
            l_site = ""
            l_snap = ""
            for p in parts[2:]:
                pl = p.lower()
                if pl.startswith("site:"):
                    l_site = _canonicalize_site_name(p.split(":", 1)[1].strip(), adapter_names)
                elif pl.startswith("snapshot:"):
                    l_snap = p.split(":", 1)[1].strip()
            return l_site, l_snap

        removed_lines = 0
        kept_proj_lines = 0
        removed_snaps: set[str] = set()
        kept_snaps: set[str] = set()
        if srv.FAILED_FILE.exists():
            with interprocess_file_lock(srv.FAILED_FILE):
                if srv.FAILED_FILE.exists():
                    lines = srv.FAILED_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
                    proj_indices: List[int] = []
                    parsed_meta: Dict[int, Tuple[str, str]] = {}
                    for idx_ln, ln in enumerate(lines):
                        s = ln.strip()
                        if not s or s.startswith("#"):
                            continue
                        l_site, l_snap = _line_site_and_snap(s)
                        parsed_meta[idx_ln] = (l_site, l_snap)
                        if (l_site == req_proj) or (not l_site and req_proj == legacy_snap_owner):
                            proj_indices.append(idx_ln)
                    # File is appended chronologically -> latest lines are at the tail
                    keep_idx_set = set(proj_indices[-keep_latest:]) if keep_latest > 0 else set()
                    kept: List[str] = []
                    for idx_ln, ln in enumerate(lines):
                        if idx_ln not in parsed_meta:
                            kept.append(ln)
                            continue
                        l_site, l_snap = parsed_meta[idx_ln]
                        belongs_to_proj = (l_site == req_proj) or (not l_site and req_proj == legacy_snap_owner)
                        if belongs_to_proj and idx_ln not in keep_idx_set:
                            removed_lines += 1
                            if l_snap:
                                removed_snaps.add(l_snap)
                        else:
                            if belongs_to_proj:
                                kept_proj_lines += 1
                            kept.append(ln)
                            if l_snap:
                                kept_snaps.add(l_snap)
                    if removed_lines > 0:
                        atomic_write_text_safe(srv.FAILED_FILE, "\n".join(kept) + ("\n" if kept else ""))

        snaps_removed = 0
        orphan_removed = 0
        snaps_to_unlink = removed_snaps - kept_snaps
        if srv.SNAPSHOTS_DIR.exists():
            snap_root = srv.SNAPSHOTS_DIR.resolve()
            for sname in snaps_to_unlink:
                fp = (snap_root / Path(sname).name).resolve()
                if fp.parent == snap_root and fp.is_file():
                    try:
                        fp.unlink()
                        snaps_removed += 1
                    except OSError:
                        pass
            # Also prune orphan PNGs displayed under legacy_snap_owner
            if req_proj == legacy_snap_owner:
                orphan_pngs = sorted(
                    [p for p in snap_root.glob("*.png") if p.is_file() and p.name not in kept_snaps],
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                orphan_keep = max(0, keep_latest - kept_proj_lines) if keep_latest > 0 else 0
                for fp in orphan_pngs[orphan_keep:]:
                    try:
                        fp.unlink()
                        orphan_removed += 1
                        snaps_removed += 1
                    except OSError:
                        pass
                kept_proj_lines += min(len(orphan_pngs), orphan_keep)

        handler._send_json({
            "ok": True,
            "project": req_proj,
            "removed": removed_lines + orphan_removed,
            "kept": kept_proj_lines,
            "snapshots_removed": snaps_removed,
        })
        return True

    return False
