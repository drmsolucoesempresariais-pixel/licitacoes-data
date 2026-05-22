#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor de Licitações — Servidor Cloud
Lê dados do GitHub (exportados pelo servidor local).
Deploy: Railway  |  Variáveis: GITHUB_TOKEN, GITHUB_USER, GITHUB_REPO
"""

import os, json, threading, time, sys
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import urllib.request, urllib.error, base64

# ════════════════════════════════════════════════════
# CONFIGURAÇÃO
# ════════════════════════════════════════════════════
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_USER  = os.environ.get("GITHUB_USER",  "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO",  "licitacoes-data")
PORT         = int(os.environ.get("PORT", 8765))

HTML_FILE        = Path(__file__).resolve().parent / "PAINEL DE LICITAÇÕES.html"
REFRESH_INTERVAL = 120   # lê o GitHub a cada 2 minutos

# ════════════════════════════════════════════════════
# GITHUB CLIENT
# ════════════════════════════════════════════════════
class GitHubClient:
    API = "https://api.github.com"

    def _headers(self):
        return {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept":        "application/vnd.github+json",
            "Content-Type":  "application/json",
        }

    def read_json(self, filename: str) -> dict | list:
        url = f"{self.API}/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{filename}"
        try:
            req = urllib.request.Request(url, headers=self._headers())
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
                content = base64.b64decode(data["content"]).decode("utf-8")
                return json.loads(content)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
            raise
        except Exception as e:
            print(f"  [GitHub] Erro ao ler {filename}: {e}", flush=True)
            return {}

    def write_json(self, filename: str, data: dict):
        url = f"{self.API}/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{filename}"
        headers = self._headers()
        # Busca SHA atual
        sha = None
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                sha = json.loads(r.read()).get("sha")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        body = {
            "message": f"update {filename}",
            "content": base64.b64encode(
                json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            ).decode("ascii"),
        }
        if sha:
            body["sha"] = sha
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers=headers,
            method="PUT",
        )
        urllib.request.urlopen(req, timeout=15)


gh = GitHubClient()

# ════════════════════════════════════════════════════
# ESTADO GLOBAL
# ════════════════════════════════════════════════════
_lock  = threading.Lock()
_state = {"data": [], "last_update": ""}
_hist  = {"data": [], "last_update": ""}


def refresh():
    try:
        state = gh.read_json("state.json")
        hist  = gh.read_json("state_hist.json")
        with _lock:
            if isinstance(state, dict):
                _state["data"]        = state.get("data", [])
                _state["last_update"] = state.get("last_update", "")
            if isinstance(hist, dict):
                _hist["data"]        = hist.get("data", [])
                _hist["last_update"] = hist.get("last_update", "")
        print(f"  [{datetime.now().strftime('%H:%M:%S')}] 🔄 dados atualizados — "
              f"{len(_state['data'])} ativas · {len(_hist['data'])} no histórico", flush=True)
    except Exception as e:
        print(f"  [ERRO] refresh: {e}", flush=True)


def load_meta() -> dict:
    result = gh.read_json("meta.json")
    return result if isinstance(result, dict) else {}


def save_meta(data: dict):
    gh.write_json("meta.json", data)


def _schedule():
    refresh()
    t = threading.Timer(REFRESH_INTERVAL, _schedule)
    t.daemon = True
    t.start()


# ════════════════════════════════════════════════════
# HTTP HANDLER
# ════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/data":
            with _lock:
                payload = dict(_state)
            self._json(payload)
        elif p == "/api/historico":
            with _lock:
                payload = dict(_hist)
            self._json(payload)
        elif p == "/api/ping":
            with _lock:
                lu = _state["last_update"]
            self._json({"last_update": lu})
        elif p == "/api/config":
            self._json({"has_claude": False})
        elif p in ("/", "/index.html"):
            self._file(HTML_FILE, "text/html")
        else:
            self.send_error(404)

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/api/save":
            n    = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n)
            try:
                payload = json.loads(body)
                meta    = load_meta()
                key     = payload.get("key")
                if key:
                    meta[key] = {k: v for k, v in payload.items()
                                 if k not in ("id", "folder", "category", "key")}
                    save_meta(meta)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        else:
            self.send_error(404)

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str):
        try:
            content = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", len(content))
            self._cors()
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")


# ════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════
if __name__ == "__main__":
    if not GITHUB_TOKEN or not GITHUB_USER:
        print("ERRO: Defina GITHUB_TOKEN e GITHUB_USER nas variáveis de ambiente.")
        sys.exit(1)

    print("╔══════════════════════════════════════════════╗")
    print("║   Monitor de Licitações — Servidor Cloud     ║")
    print("╚══════════════════════════════════════════════╝")
    print(f"  Repositório : {GITHUB_USER}/{GITHUB_REPO}")
    print(f"  Porta       : {PORT}")
    print("  Carregando dados do GitHub...", flush=True)

    refresh()

    t = threading.Timer(REFRESH_INTERVAL, _schedule)
    t.daemon = True
    t.start()

    print(f"  ✅  Servidor pronto na porta {PORT}\n", flush=True)
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
