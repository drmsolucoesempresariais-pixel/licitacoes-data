#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor de Licitações — Servidor Cloud
Lê pastas do OneDrive via Microsoft Graph API (OAuth 2.0).
Deploy: Railway

Variáveis de ambiente:
  AZURE_CLIENT_ID      — client ID do app registrado no portal.azure.com
  AZURE_REFRESH_TOKEN  — refresh token gerado pelo setup_auth.py (uma vez)
  ONEDRIVE_FOLDER_ID   — ID da pasta raiz de licitações no OneDrive
  GITHUB_TOKEN         — token para salvar meta.json
  GITHUB_USER          — usuário do repositório de dados
  GITHUB_REPO          — repositório de dados (default: licitacoes-data)
  CLAUDE_API_KEY       — (opcional) habilita análise inteligente com IA
"""

import os, json, threading, re, sys, time, unicodedata
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import urllib.request, urllib.error, urllib.parse, base64

# ════════════════════════════════════════════════════
# CONFIGURAÇÃO
# ════════════════════════════════════════════════════
GITHUB_TOKEN         = os.environ.get("GITHUB_TOKEN",         "")
GITHUB_USER          = os.environ.get("GITHUB_USER",          "")
GITHUB_REPO          = os.environ.get("GITHUB_REPO",          "licitacoes-data")
PORT                 = int(os.environ.get("PORT",              8765))
# Client público oficial da Microsoft ("Microsoft Graph Command Line Tools").
# Default permite rodar sem registrar app próprio no Azure.
AZURE_CLIENT_ID      = os.environ.get("AZURE_CLIENT_ID",      "14d82eec-204b-4c2f-b7e8-296a70dab67e")
AZURE_REFRESH_TOKEN  = os.environ.get("AZURE_REFRESH_TOKEN",  "")
ONEDRIVE_FOLDER_ID   = os.environ.get("ONEDRIVE_FOLDER_ID",   "")
CLAUDE_API_KEY       = os.environ.get("CLAUDE_API_KEY",       "")   # habilita análise IA

HTML_FILE        = Path(__file__).resolve().parent / "PAINEL DE LICITAÇÕES.html"
REFRESH_INTERVAL = 30    # segundos entre scans do OneDrive
MIME_FOLDER      = "onedrive/folder"   # marcador interno para distinguir pasta de arquivo

# Palavras-chave que identificam pastas de histórico/finalizados (jamais são portais)
_HIST_KEYS = (
    "ACONTECIDOS", "JA ACONTECIDO", "JÁ ACONTECIDO",
    "FINALIZADO", "FINALIZADOS", "HISTÓRICO", "HISTORICO",
    "PREGOES FINALIZADOS", "PREGÕES FINALIZADOS",
)

# Arquivos de sistema que devem ser ignorados
_SKIP_NAMES = {"DESKTOP.INI", "THUMBS.DB", ".DS_STORE"}

# Ruído no nome da pasta que não faz parte do nome do município/órgão
_NOISE = re.compile(
    r'\b('
    r'BBMNET|BLL|COMPRASNET|'
    r'COMPRAS\s+PUBLICAS?|PORTAL\s+DE\s+COMPRAS\s+PUBLICAS?|'
    r'PORTAL|LICITAR\s+DIGITAL|PROPRIO|PROPRIOS|DIRETO|'
    r'PA|MG|SP|RJ|RS|SC|TO|BA|PB|GO|MS|MT|ES|CE|RN|PE|AL|SE|PI|MA|AP|AM|RO|RR|AC'
    r')\b',
    re.IGNORECASE,
)


# ════════════════════════════════════════════════════
# ONEDRIVE CLIENT (Microsoft Graph API)
# ════════════════════════════════════════════════════
class OneDriveClient:
    TOKEN_URL  = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
    GRAPH_BASE = "https://graph.microsoft.com/v1.0"

    def __init__(self):
        self._access_token  = None
        self._token_expires = 0.0
        self._refresh_token = AZURE_REFRESH_TOKEN   # rotaciona em memória a cada renovação

    def _refresh_access_token(self):
        """Troca o refresh_token por um novo access_token (e rotaciona o refresh_token)."""
        data = urllib.parse.urlencode({
            "client_id":     AZURE_CLIENT_ID,
            "grant_type":    "refresh_token",
            "refresh_token": self._refresh_token,
            "scope":         "Files.Read offline_access",
        }).encode()
        req = urllib.request.Request(
            self.TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            tok = json.loads(r.read())
        if "access_token" not in tok:
            raise RuntimeError(f"Falha ao renovar token OneDrive: {tok.get('error_description', tok)}")
        self._access_token  = tok["access_token"]
        self._token_expires = time.time() + tok.get("expires_in", 3600) - 60
        # Client público rotaciona o refresh_token a cada uso — guarda o mais novo.
        if tok.get("refresh_token"):
            self._refresh_token = tok["refresh_token"]

    def _token(self) -> str:
        if not self._access_token or time.time() >= self._token_expires:
            self._refresh_access_token()
        return self._access_token

    def list_folder(self, folder_id: str) -> list:
        """Lista arquivos e subpastas dentro de um item do OneDrive."""
        results = []
        url = (
            f"{self.GRAPH_BASE}/me/drive/items/{folder_id}/children"
            "?$select=id,name,folder,file&$top=1000"
        )
        headers = {"Authorization": f"Bearer {self._token()}"}
        while url:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            for item in data.get("value", []):
                results.append({
                    "id":       item["id"],
                    "name":     item["name"],
                    "mimeType": MIME_FOLDER if "folder" in item else "file",
                })
            url = data.get("@odata.nextLink")
        return results

    def download_item(self, item_id: str) -> bytes:
        """
        Baixa o conteúdo binário de um arquivo do OneDrive (ex.: o edital em PDF).

        O endpoint /content responde com um 302 para uma URL pré-assinada de download
        (em outro domínio de armazenamento). Se seguíssemos esse redirect REENVIANDO o
        header Authorization, a Microsoft devolve 401 — a URL pré-assinada já carrega a
        própria credencial. Por isso seguimos o redirect MANUALMENTE, sem o Authorization.
        """
        url = f"{self.GRAPH_BASE}/me/drive/items/{item_id}/content"

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None  # não segue automaticamente; deixa o 302 virar HTTPError

        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token()}"})
        try:
            with opener.open(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                location = e.headers.get("Location")
                if not location:
                    raise
                # Baixa da URL pré-assinada SEM cabeçalho Authorization
                with urllib.request.urlopen(location, timeout=60) as r2:
                    return r2.read()
            raise


onedrive = OneDriveClient()


# ════════════════════════════════════════════════════
# GITHUB CLIENT (salva/lê meta.json)
# ════════════════════════════════════════════════════
class GitHubClient:
    API = "https://api.github.com"

    def _headers(self):
        return {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept":        "application/vnd.github+json",
            "Content-Type":  "application/json",
        }

    def read_json(self, filename: str):
        url = f"{self.API}/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{filename}"
        try:
            req = urllib.request.Request(url, headers=self._headers())
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
                return json.loads(base64.b64decode(data["content"]).decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
            raise
        except Exception as e:
            print(f"  [GitHub] Erro ao ler {filename}: {e}", flush=True)
            return {}

    def write_json(self, filename: str, data: dict):
        url = f"{self.API}/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{filename}"
        sha = None
        try:
            req = urllib.request.Request(url, headers=self._headers())
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
            url, data=json.dumps(body).encode(), headers=self._headers(), method="PUT"
        )
        urllib.request.urlopen(req, timeout=15)


gh = GitHubClient()


# ════════════════════════════════════════════════════
# ESTADO GLOBAL (thread-safe)
# ════════════════════════════════════════════════════
_lock  = threading.Lock()
_state = {"data": [], "last_update": ""}
_hist  = {"data": [], "last_update": ""}


# ════════════════════════════════════════════════════
# PARSING INTELIGENTE DE NOMES DE PASTA
# ════════════════════════════════════════════════════
def _strip_accents(s: str) -> str:
    """Remove acentos de uma string."""
    return ''.join(
        c for c in unicodedata.normalize('NFD', s)
        if unicodedata.category(c) != 'Mn'
    )


def _parse_name(name: str) -> tuple:
    """
    Extrai (city, date_str) do nome de uma pasta de licitação.

    Padrões de data aceitos:
      DD-MM  |  DD/MM  |  DD-MM-YY  |  DD-MM-YYYY  |  DD/MM/YYYY

    Extração do município/órgão:
      - Tudo antes da data
      - Remove termos de ruído (portais, UFs, etc.)
      - Title case

    Exemplos:
      "CERQUEIRA CEZAR - 26-05 BBMNET"  → ("Cerqueira Cezar", "2026-05-26")
      "INDAIATUBA - 03-06 - BANCOS"     → ("Indaiatuba", "2026-06-03")
      "SAO PAULO 05-06"                 → ("São Paulo", "2026-06-05")
      "PREFEITURA MUNICIPAL"            → ("Prefeitura Municipal", "")
    """
    m = re.search(r'(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?', name)
    if m:
        day   = m.group(1).zfill(2)
        month = m.group(2).zfill(2)
        yr    = m.group(3)
        if yr:
            year = ("20" + yr) if len(yr) == 2 else yr
        else:
            # Se o mês já passou no ano atual, assume próximo ano
            today = datetime.now()
            m_int = int(month)
            d_int = int(day)
            candidate = datetime(today.year, m_int, d_int)
            year = str(today.year + 1 if candidate < today else today.year)
        date_str = f"{year}-{month}-{day}"
        city_raw = name[:m.start()]
    else:
        date_str = ""
        city_raw = name

    # Remove ruído da string de cidade
    city = _NOISE.sub('', city_raw)
    city = re.sub(r'\s+', ' ', city).strip(' \t\r\n-_|')

    # Fallback: pega o primeiro segmento antes de " - "
    if not city or len(city) < 2:
        city = re.split(r'\s*[-–]\s*', name.strip())[0].strip()
    if not city:
        city = name.strip()

    return city.title(), date_str


def _detect_status(items: list) -> str:
    """
    Detecta o status da licitação pelos nomes de arquivos e subpastas.

    Prioridade (maior para menor):
      ganho > recurso > habilitacao > republicar > enviado > andamento
    """
    folders = [i["name"].lower() for i in items if i["mimeType"] == MIME_FOLDER]
    files   = [i["name"].lower() for i in items if i["mimeType"] != MIME_FOLDER]
    all_n   = folders + files

    # Ganho: nota de empenho ou autorização de fornecimento
    for n in files:
        if "nota de empenho" in n:
            return "ganho"
        if "autoriza" in n and "fornecimento" in n:
            return "ganho"

    # Recurso administrativo
    for n in all_n:
        if "recurso" in n:
            return "recurso"

    # Habilitação (subpasta ou arquivo)
    for n in folders:
        if "habilita" in n:
            return "habilitacao"
    for n in files:
        if "habilita" in n:
            return "habilitacao"

    # Republicação
    for n in all_n:
        if any(k in n for k in ("republicar", "republicação", "repuplicar", "republicad")):
            return "republicar"

    # Proposta registrada no portal
    for n in files:
        if "proposta registrada" in n:
            return "enviado"

    return "andamento"


def _scan_portal(portal_id: str, portal_name: str, category: str) -> list:
    """
    Escaneia as subpastas de um portal e retorna uma lista de licitações.
    Cada subpasta = uma oportunidade/licitação.
    """
    results  = []
    pnorm    = _strip_accents(portal_name.upper())

    try:
        for folder in onedrive.list_folder(portal_id):
            if folder["mimeType"] != MIME_FOLDER:
                continue
            if folder["name"].upper() in _SKIP_NAMES:
                continue

            fname    = folder["name"]
            city, dt = _parse_name(fname)
            contents = onedrive.list_folder(folder["id"])
            status   = _detect_status(contents)
            key      = f"{pnorm}|{fname}"

            results.append({
                "id":       key,
                "key":      key,
                "portal":   pnorm,
                "folder":   fname,
                "drive_id": folder["id"],   # ID OneDrive → usado na análise IA
                "city":     city,
                "date":     dt,
                "status":   status,
                "category": category,
            })

    except Exception as e:
        print(f"  [OneDrive] Erro ao escanear portal '{portal_name}': {e}", flush=True)

    return results


def scan_drive() -> tuple:
    """
    Escaneia a pasta raiz do OneDrive com detecção automática de portais.

    Regras aplicadas em ordem:
      1. Pasta cujo nome contém palavras-chave de histórico → category recente/historico
      2. Pasta cujo nome contém data (DD-MM) → licitação direta (portal próprio)
      3. Qualquer outra pasta → portal automático; seus filhos = licitações

    NÃO há lista fixa de portais. Criar "PORTAL TESTE" na raiz do OneDrive
    é suficiente para que o sistema o reconheça e escaneie suas subpastas.
    """
    if not ONEDRIVE_FOLDER_ID or not AZURE_CLIENT_ID or not AZURE_REFRESH_TOKEN:
        return [], []

    ativas, historico = [], []

    try:
        root_items = onedrive.list_folder(ONEDRIVE_FOLDER_ID)
    except Exception as e:
        print(f"  [OneDrive] Erro ao listar pasta raiz: {e}", flush=True)
        return [], []

    for item in root_items:
        name = item["name"]
        mid  = item["mimeType"]

        # Ignora arquivos e nomes de sistema
        if mid != MIME_FOLDER:
            continue
        if name.upper() in _SKIP_NAMES:
            continue

        upper = name.upper()

        # ── Regra 1a: Já Acontecidos → Acompanhamentos ───────────────────────
        # Suporta estrutura plana (licitações diretas) e aninhada (portal → licitações)
        if any(k in upper for k in ("ACONTECIDOS", "JA ACONTECIDO", "JÁ ACONTECIDO")):
            try:
                ja_sub = onedrive.list_folder(item["id"])
            except Exception as e:
                print(f"  [OneDrive] Erro ao listar Já Acontecidos: {e}", flush=True)
                continue
            for ja in ja_sub:
                if ja["mimeType"] != MIME_FOLDER:
                    continue
                jname = ja["name"]
                if jname.upper() in _SKIP_NAMES:
                    continue
                if re.search(r'\d{1,2}[/\-]\d{1,2}', jname):
                    # Licitação direta (tem data no nome)
                    city, dt = _parse_name(jname)
                    try:
                        contents = onedrive.list_folder(ja["id"])
                        status   = _detect_status(contents)
                    except Exception:
                        status = "andamento"
                    key = f"RECENTE|{jname}"
                    historico.append({
                        "id":       key,
                        "key":      key,
                        "portal":   "PORTAL PROPRIO",
                        "folder":   jname,
                        "drive_id": ja["id"],
                        "city":     city,
                        "date":     dt,
                        "status":   status,
                        "category": "recente",
                    })
                else:
                    # Subpasta sem data → é um portal; seus filhos são licitações
                    sub = _scan_portal(ja["id"], jname, "recente")
                    if sub:
                        print(f"  [OneDrive] Acomp. portal '{jname}' → {len(sub)} licitação(ões)", flush=True)
                    historico.extend(sub)
            continue

        # ── Regra 1b: Finalizados / Histórico antigo ─────────────────────────
        if any(k in upper for k in ("FINALIZADO", "HISTÓRICO", "HISTORICO")):
            historico.extend(_scan_portal(item["id"], "HISTORICO", "historico"))
            continue

        # ── Regra 2: pasta com data no nome → licitação direta ───────────────
        if re.search(r'\d{1,2}[/\-]\d{1,2}', name):
            city, dt = _parse_name(name)
            contents = onedrive.list_folder(item["id"])
            status   = _detect_status(contents)
            key      = f"DIRETO|{name}"
            ativas.append({
                "id":       key,
                "key":      key,
                "portal":   "PORTAL PROPRIO",
                "folder":   name,
                "drive_id": item["id"],
                "city":     city,
                "date":     dt,
                "status":   status,
                "category": "ativo",
            })
            continue

        # ── Regra 3: qualquer outra pasta → portal (automático) ──────────────
        # Cobre portais conhecidos (BBMNET, BLL…) e portais novos criados pelo usuário
        sub = _scan_portal(item["id"], name, "ativo")
        if sub:
            print(
                f"  [OneDrive] Portal '{name}' → {len(sub)} licitação(ões) detectada(s)",
                flush=True,
            )
        ativas.extend(sub)

    return ativas, historico


# ════════════════════════════════════════════════════
# META (GitHub)
# ════════════════════════════════════════════════════
def load_meta() -> dict:
    result = gh.read_json("meta.json")
    return result if isinstance(result, dict) else {}


def save_meta(data: dict):
    gh.write_json("meta.json", data)


def _apply_meta(items: list, meta: dict) -> list:
    """Mescla dados editados manualmente (meta.json) sobre os dados do OneDrive."""
    return [{**item, **meta.get(item.get("key", ""), {})} for item in items]


# ════════════════════════════════════════════════════
# ANÁLISE INTELIGENTE COM CLAUDE AI
# ════════════════════════════════════════════════════
def _call_claude(prompt: str) -> str:
    """Envia prompt para a API da Anthropic e retorna o texto da resposta."""
    body = json.dumps({
        "model":      "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "messages":   [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key":         CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        data = json.loads(r.read())
    return data["content"][0]["text"].strip()


def _call_claude_pdf(pdf_b64: str, prompt: str,
                     model: str = "claude-haiku-4-5-20251001",
                     max_tokens: int = 1024) -> str:
    """
    Envia um PDF (base64) + pergunta para o Claude e retorna o texto da resposta.
    model/max_tokens são parametrizáveis: a detecção de data usa Haiku (barato),
    o checklist de habilitação usa Sonnet (mais preciso, pega 'pegadinhas').
    """
    body = json.dumps({
        "model":      model,
        "max_tokens": max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key":         CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        # Revela o motivo EXATO que a Anthropic devolve junto do erro
        # (ex.: "prompt is too long: N tokens > 200000"), em vez de só "400".
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        detalhe = ""
        try:
            detalhe = json.loads(body).get("error", {}).get("message", "")
        except Exception:
            detalhe = body[:300]
        print(f"  [Claude] HTTP {e.code} modelo={model}: {detalhe}", flush=True)
        raise RuntimeError(f"Claude {e.code}: {detalhe or e.reason}")
    return data["content"][0]["text"].strip()


def _call_claude_text(texto: str, prompt: str,
                      model: str = "claude-haiku-4-5-20251001",
                      max_tokens: int = 1024) -> str:
    """
    Envia o TEXTO do edital (não o PDF) + pergunta para o Claude.
    Muito mais leve/barato que mandar o PDF (que conta cada página como imagem),
    e sem o limite de ~100 páginas. Usado quando o edital tem texto extraível.
    """
    conteudo = f"{prompt}\n\n━━━ TEXTO DO EDITAL ━━━\n{texto}"
    body = json.dumps({
        "model":      model,
        "max_tokens": max_tokens,
        "messages":   [{"role": "user", "content": conteudo}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key":         CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        try:
            detalhe = json.loads(body).get("error", {}).get("message", "")
        except Exception:
            detalhe = body[:300]
        print(f"  [Claude] HTTP {e.code} modelo={model} (texto): {detalhe}", flush=True)
        raise RuntimeError(f"Claude {e.code}: {detalhe or e.reason}")
    return data["content"][0]["text"].strip()


def _ler_edital_ia(raw: bytes, prompt: str, model: str, max_tokens: int,
                   max_paginas: int = 0, max_chars: int = 600_000) -> tuple:
    """
    Lê o edital com a IA usando a melhor estratégia disponível:
      1) Extrai o TEXTO do PDF (leve, barato) e envia o texto.
      2) Se o PDF não tiver texto extraível (ex.: edital escaneado/imagem),
         cai para o método antigo: envia o PDF (cortado em ~95 págs).
    max_paginas>0 lê apenas as primeiras N páginas (a data da sessão fica no início
    do edital; isso mantém o envio pequeno e dentro de limites de conta menores).
    Retorna (resposta_texto, modo) onde modo ∈ {"texto", "pdf"}.
    """
    texto = ""
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        paginas = reader.pages if max_paginas <= 0 else reader.pages[:max_paginas]
        partes = []
        for p in paginas:
            try:
                partes.append(p.extract_text() or "")
            except Exception:
                pass
        texto = "\n".join(partes).strip()
    except Exception as e:
        print(f"  [PDF] Falha ao extrair texto ({e}); usarei o PDF.", flush=True)

    # Texto suficiente? (editais escaneados extraem quase nada → usa PDF)
    if len(texto) >= 800:
        # margem de segurança: limita o texto p/ não estourar a memória/limite do modelo
        if len(texto) > max_chars:
            texto = texto[:max_chars]
        return _call_claude_text(texto, prompt, model=model, max_tokens=max_tokens), "texto"

    # Reserva: edital sem texto (imagem) → envia o PDF cortado
    raw_cortado, _ = _limitar_paginas(raw)
    pdf_b64 = base64.b64encode(raw_cortado).decode("ascii")
    return _call_claude_pdf(pdf_b64, prompt, model=model, max_tokens=max_tokens), "pdf"


def analyze_licitacao_drive(key: str) -> dict:
    """
    Analisa uma licitação usando Claude AI.

    Fluxo:
      1. Encontra o item no estado atual pelo key
      2. Usa o drive_id para listar arquivos/subpastas (profundidade 2) via OneDrive
      3. Monta prompt rico com contexto da DRM e dados da pasta
      4. Chama Claude e retorna análise estruturada em JSON
    """
    if not CLAUDE_API_KEY:
        return {
            "ok":    False,
            "error": (
                "Análise IA não configurada.\n"
                "Adicione a variável CLAUDE_API_KEY nas configurações do Railway."
            ),
        }

    # Busca o item no estado em memória
    with _lock:
        all_items = list(_state["data"]) + list(_hist["data"])
    item = next((i for i in all_items if i.get("key") == key), None)

    if not item:
        return {"ok": False, "error": "Licitação não encontrada. Atualize o painel e tente novamente."}

    drive_id = item.get("drive_id", "")
    if not drive_id:
        return {
            "ok":    False,
            "error": "ID da pasta no OneDrive não disponível. Aguarde a próxima atualização automática (2 min).",
        }

    # Lista conteúdo da pasta (profundidade 2)
    file_list = []
    try:
        contents = onedrive.list_folder(drive_id)
        for entry in sorted(contents, key=lambda x: x["name"]):
            icon = "📁" if entry["mimeType"] == MIME_FOLDER else "📄"
            file_list.append(f"{icon} {entry['name']}")
            if entry["mimeType"] == MIME_FOLDER:
                try:
                    for sub in sorted(onedrive.list_folder(entry["id"]), key=lambda x: x["name"]):
                        sub_icon = "📁" if sub["mimeType"] == MIME_FOLDER else "📄"
                        file_list.append(f"   └─ {sub_icon} {sub['name']}")
                except Exception:
                    pass
    except Exception as e:
        return {"ok": False, "error": f"Erro ao acessar a pasta no OneDrive: {e}"}

    conteudo = "\n".join(file_list) if file_list else "(pasta vazia ou sem arquivos acessíveis)"

    prompt = f"""Você é especialista em licitações públicas brasileiras (pregão eletrônico).
A empresa DRM Soluções Empresariais participa de pregões eletrônicos, vendendo principalmente mobiliário, informática e produtos para órgãos públicos.

Analise a licitação abaixo e determine com precisão o estado atual do processo.

━━━ DADOS DA LICITAÇÃO ━━━
Nome da pasta   : {item.get('folder', key)}
Município/Órgão : {item.get('city', '?')}
Portal          : {item.get('portal', '?')}
Data de abertura: {item.get('date') or 'não identificada'}
Status atual    : {item.get('status', '?')}
Observações     : {item.get('obs') or '(sem observações)'}

━━━ CONTEÚDO DA PASTA NO ONEDRIVE ━━━
{conteudo}

━━━ CONVENÇÕES DE NOMES USADAS PELA DRM ━━━
Subpastas:
  • HABILITACAO / HABILITAÇÃO → fase de análise de documentos de habilitação
  • RECURSO → recurso administrativo interposto

Arquivos:
  • "proposta registrada" → proposta já inserida no portal (status: enviado)
  • "nota de empenho"     → licitação ganha, empenho emitido (status: ganho)
  • "autorização de fornecimento" → licitação ganha (status: ganho)
  • "recurso"             → recurso administrativo em andamento
  • "planilha" / "preços" → proposta em preparação
  • "edital"              → fase de preparação/análise

━━━ TAREFA ━━━
Responda SOMENTE em JSON válido (sem markdown, sem bloco de código), com exatamente estes campos:
{{
  "fase": "Nome curto da fase (ex: Preparação, Proposta Enviada, Aguardando Resultado, Ag. Habilitação, Recurso Administrativo, Ganho, Perdido)",
  "status_sugerido": "andamento|enviado|habilitacao|recurso|ganho|perdido|republicar",
  "resumo": "2 a 3 frases descrevendo o estado atual e o que já foi realizado",
  "proximos_passos": ["Ação 1", "Ação 2"],
  "alertas": []
}}"""

    try:
        text = _call_claude(prompt)
        # Remove eventual bloco markdown se Claude ignorar a instrução
        text = re.sub(r'^```[a-z]*\n?', '', text.strip())
        text = re.sub(r'\n?```$', '', text.strip())
        result = json.loads(text)
        result["ok"] = True
        return result

    except json.JSONDecodeError:
        # Claude respondeu algo mas não em JSON — devolve como resumo livre
        return {
            "ok":              True,
            "fase":            "Análise concluída",
            "status_sugerido": item.get("status", "andamento"),
            "resumo":          text,
            "proximos_passos": [],
            "alertas":         [],
        }
    except Exception as e:
        return {"ok": False, "error": f"Erro na API Claude: {e}"}


# ════════════════════════════════════════════════════
# DETECÇÃO DA DATA/HORA DA SESSÃO (lê o edital com IA)
# ════════════════════════════════════════════════════
_EDITAL_HINTS = ("edital", "aviso", "pregao", "pregão", "termo de referencia", "tr")


def _pick_edital(contents: list) -> dict | None:
    """Escolhe o PDF mais provável de ser o edital dentro da pasta da licitação."""
    pdfs = [
        c for c in contents
        if c["mimeType"] != MIME_FOLDER and c["name"].lower().endswith(".pdf")
    ]
    if not pdfs:
        return None
    for hint in _EDITAL_HINTS:
        for c in pdfs:
            if hint in c["name"].lower():
                return c
    return pdfs[0]


def _limitar_paginas(raw: bytes, max_paginas: int = 95) -> tuple:
    """
    Se o PDF tiver mais páginas que o limite da IA (~100), corta nas primeiras
    `max_paginas` (onde quase sempre estão a data da sessão e a habilitação).
    Defensivo: se o pypdf não existir ou falhar, devolve o PDF ORIGINAL —
    nunca quebra o fluxo (no pior caso, comporta-se como antes).
    Retorna (bytes_para_enviar, info_dict).
    """
    info = {"cortado": False, "paginas_total": None, "paginas_enviadas": None}
    try:
        import io
        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(io.BytesIO(raw))
        total  = len(reader.pages)
        info["paginas_total"] = total
        if total <= max_paginas:
            info["paginas_enviadas"] = total
            return raw, info
        writer = PdfWriter()
        for i in range(max_paginas):
            writer.add_page(reader.pages[i])
        out = io.BytesIO()
        writer.write(out)
        info["cortado"] = True
        info["paginas_enviadas"] = max_paginas
        return out.getvalue(), info
    except Exception as e:
        # Sem pypdf ou PDF problemático: segue com o original (comportamento antigo)
        print(f"  [PDF] Não foi possível cortar páginas ({e}); enviando original.", flush=True)
        return raw, info


def get_edital_pdf(key: str) -> dict:
    """
    Localiza e baixa o PDF do edital de uma licitação no OneDrive.
    Retorna {"ok": True, "bytes": ..., "name": ...} ou {"ok": False, "error": ...}.
    Usado para ABRIR o edital no navegador (não usa IA).
    """
    with _lock:
        all_items = list(_state["data"]) + list(_hist["data"])
    item = next((i for i in all_items if i.get("key") == key), None)
    if not item:
        return {"ok": False, "error": "Licitação não encontrada. Atualize o painel e tente de novo."}

    drive_id = item.get("drive_id", "")
    if not drive_id:
        return {"ok": False, "error": "Pasta no OneDrive ainda não disponível. Aguarde a próxima atualização."}

    try:
        contents = onedrive.list_folder(drive_id)
    except Exception as e:
        return {"ok": False, "error": f"Erro ao abrir a pasta no OneDrive: {e}"}

    edital = _pick_edital(contents)
    if not edital:
        return {"ok": False, "error": "Nenhum PDF (edital) foi encontrado na pasta desta licitação."}

    try:
        raw = onedrive.download_item(edital["id"])
    except Exception as e:
        return {"ok": False, "error": f"Erro ao baixar o edital: {e}"}

    return {"ok": True, "bytes": raw, "name": edital["name"]}


def detect_session_datetime(key: str) -> dict:
    """
    Lê o edital (PDF) da licitação com a IA e tenta achar a data/hora da sessão de disputa.
    NÃO salva nada — apenas devolve a sugestão para o usuário confirmar no painel.
    """
    if not CLAUDE_API_KEY:
        return {"ok": False, "error": "Análise IA não configurada. Adicione CLAUDE_API_KEY no Railway."}

    with _lock:
        all_items = list(_state["data"]) + list(_hist["data"])
    item = next((i for i in all_items if i.get("key") == key), None)
    if not item:
        return {"ok": False, "error": "Licitação não encontrada. Atualize o painel e tente de novo."}

    drive_id = item.get("drive_id", "")
    if not drive_id:
        return {"ok": False, "error": "Pasta no OneDrive ainda não disponível. Aguarde a próxima atualização."}

    try:
        contents = onedrive.list_folder(drive_id)
    except Exception as e:
        return {"ok": False, "error": f"Erro ao abrir a pasta no OneDrive: {e}"}

    edital = _pick_edital(contents)
    if not edital:
        return {"ok": False, "error": "Nenhum PDF (edital) encontrado na pasta desta licitação."}

    try:
        raw = onedrive.download_item(edital["id"])
    except Exception as e:
        return {"ok": False, "error": f"Erro ao baixar o edital: {e}"}

    if len(raw) > 25 * 1024 * 1024:
        return {"ok": False, "error": "O edital é grande demais para a leitura automática (acima de 25 MB)."}

    prompt = (
        "Você é especialista em pregões eletrônicos brasileiros. No edital abaixo, "
        "encontre a DATA e a HORA de ABERTURA DA SESSÃO PÚBLICA / INÍCIO DA DISPUTA DE LANCES "
        "(o momento em que o pregão acontece ao vivo no portal). "
        "Ignore datas de publicação, esclarecimentos, impugnação e entrega de propostas, "
        "a menos que o próprio edital diga que é a abertura da sessão de disputa. "
        "O horário normalmente está em horário de Brasília. "
        "Responda SOMENTE em JSON válido, sem markdown e sem texto fora do JSON, com estes campos:\n"
        '{"data": "AAAA-MM-DD ou vazio se não achar", '
        '"hora": "HH:MM ou vazio se não achar", '
        '"confianca": "alta|media|baixa", '
        '"trecho": "a frase exata do edital de onde você tirou a data e a hora"}'
    )

    try:
        # A data da sessão fica no INÍCIO do edital → lê só as primeiras páginas.
        # Mantém o envio pequeno (cabe em limites de conta menores, evita HTTP 429).
        text, _modo = _ler_edital_ia(raw, prompt, model="claude-haiku-4-5-20251001",
                                     max_tokens=1024, max_paginas=40, max_chars=120_000)
        text = re.sub(r'^```[a-z]*\n?', '', text.strip())
        text = re.sub(r'\n?```$', '', text.strip())
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"ok": False, "error": "A IA respondeu, mas não consegui interpretar a data. Tente de novo ou preencha manualmente."}
    except Exception as e:
        return {"ok": False, "error": f"Erro na leitura do edital pela IA: {e}"}

    parsed["ok"]      = True
    parsed["arquivo"] = edital["name"]
    return parsed


# ════════════════════════════════════════════════════
# REFRESH (atualização periódica)
# ════════════════════════════════════════════════════
def _session_passed(item) -> bool:
    """True se a data+hora da sessão (do edital, já confirmada) passou (horário de Brasília)."""
    ds = item.get("data_sessao", "")
    if not ds:
        return False
    hora = item.get("hora_sessao") or "23:59"
    try:
        y, mo, d = (int(x) for x in ds.split("-"))
        hh, mm   = (int(x) for x in hora.split(":"))
        brt  = timezone(timedelta(hours=-3))
        sess = datetime(y, mo, d, hh, mm, tzinfo=brt)
    except Exception:
        return False
    return datetime.now(brt) > sess


def refresh():
    try:
        ativas, historico = scan_drive()
        meta              = load_meta()
        ativas    = _apply_meta(ativas,    meta)
        historico = _apply_meta(historico, meta)

        # Auto-acompanhamento: licitação ativa cuja sessão (data+hora do edital,
        # confirmada pelo usuário) já passou migra sozinha para Acompanhamentos.
        # A pasta no OneDrive NÃO é movida — muda só a classificação no painel.
        keep, movidas = [], []
        for i in ativas:
            (movidas if _session_passed(i) else keep).append(i)
        if movidas:
            for i in movidas:
                i["category"]    = "recente"
                i["auto_movido"] = True
            ativas    = keep
            historico = movidas + historico

        ts = datetime.now().isoformat()
        with _lock:
            _state["data"]        = ativas
            _state["last_update"] = ts
            _hist["data"]         = historico
            _hist["last_update"]  = ts

        print(
            f"  [{datetime.now().strftime('%H:%M:%S')}] "
            f"Atualizado — {len(ativas)} ativas · {len(historico)} histórico",
            flush=True,
        )
    except Exception as e:
        print(f"  [ERRO] refresh: {e}", flush=True)


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
        pass  # suprime logs de acesso

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
            # Informa ao painel se a análise IA está disponível
            self._json({"has_claude": bool(CLAUDE_API_KEY)})

        elif p == "/api/edital":
            self._serve_edital()

        elif p in ("/", "/index.html"):
            self._file(HTML_FILE, "text/html")

        else:
            self.send_error(404)

    def _serve_edital(self):
        """Baixa o PDF do edital e o entrega INLINE para o navegador abrir/ler."""
        qs  = urllib.parse.urlparse(self.path).query
        key = urllib.parse.parse_qs(qs).get("key", [""])[0]
        info = get_edital_pdf(key)

        if not info.get("ok"):
            # Mostra uma página amigável na aba nova em vez de JSON cru
            msg  = info.get("error", "Não foi possível abrir o edital.")
            html = (
                "<!doctype html><html lang='pt-BR'><meta charset='utf-8'>"
                "<title>Edital</title>"
                "<div style='font-family:system-ui,sans-serif;max-width:520px;margin:60px auto;"
                "padding:28px;background:#1e1e1e;color:#f2f2f2;border:1px solid #383838;border-radius:12px'>"
                f"<div style='font-size:2rem;margin-bottom:10px'>📄</div>"
                f"<div style='font-size:1rem;line-height:1.6'>{msg}</div>"
                "<div style='font-size:.8rem;color:#888;margin-top:14px'>Você pode fechar esta aba.</div>"
                "</div></html>"
            ).encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type",   "text/html; charset=utf-8")
            self.send_header("Content-Length", len(html))
            self._cors()
            self.end_headers()
            self.wfile.write(html)
            return

        raw  = info["bytes"]
        safe = urllib.parse.quote(info["name"])
        self.send_response(200)
        self.send_header("Content-Type",        "application/pdf")
        self.send_header("Content-Disposition", f"inline; filename=\"edital.pdf\"; filename*=UTF-8''{safe}")
        self.send_header("Content-Length",      len(raw))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        p    = self.path.split("?")[0]
        n    = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)

        if p == "/api/save":
            try:
                payload = json.loads(body)
                # O painel pode mandar um objeto único ou uma lista com um item.
                if isinstance(payload, list):
                    payload = payload[0] if payload else {}
                meta = load_meta()
                key  = payload.get("key")
                if key:
                    # drive_id/folder/category não são editáveis — ficam de fora
                    edits = {
                        k: v for k, v in payload.items()
                        if k not in ("id", "folder", "category", "key", "drive_id")
                    }
                    meta[key] = edits
                    save_meta(meta)
                    # Aplica a edição já no estado em memória, sem esperar o próximo
                    # scan — assim o painel reflete a mudança em poucos segundos.
                    ts = datetime.now().isoformat()
                    with _lock:
                        for coll in (_state["data"], _hist["data"]):
                            for it in coll:
                                if it.get("key") == key:
                                    it.update(edits)
                        _state["last_update"] = ts
                        _hist["last_update"]  = ts
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        elif p == "/api/analyze":
            try:
                payload = json.loads(body)
                result  = analyze_licitacao_drive(payload.get("key", ""))
                self._json(result)
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        elif p == "/api/detectar-data":
            try:
                payload = json.loads(body)
                result  = detect_session_datetime(payload.get("key", ""))
                self._json(result)
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        else:
            self.send_error(404)

    # ── helpers ──────────────────────────────────────────────────────────────
    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type",   "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str):
        try:
            content = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type",   f"{ctype}; charset=utf-8")
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
    if not AZURE_CLIENT_ID or not AZURE_REFRESH_TOKEN or not ONEDRIVE_FOLDER_ID:
        print("ERRO: Defina AZURE_CLIENT_ID, AZURE_REFRESH_TOKEN e ONEDRIVE_FOLDER_ID nas variáveis de ambiente.")
        print("      Execute setup_auth.py uma vez no seu PC para obter esses valores.")
        sys.exit(1)

    print("╔══════════════════════════════════════════════╗")
    print("║   Monitor de Licitações — Servidor Cloud     ║")
    print("╚══════════════════════════════════════════════╝")
    print(f"  OneDrive Folder: {ONEDRIVE_FOLDER_ID}")
    print(f"  Porta          : {PORT}")
    print(f"  Claude AI      : {'✅ Ativada' if CLAUDE_API_KEY else '⚠️  Desativada (sem CLAUDE_API_KEY)'}")
    print(f"  Refresh        : a cada {REFRESH_INTERVAL}s")
    print("  Carregando dados do OneDrive...", flush=True)

    refresh()

    t = threading.Timer(REFRESH_INTERVAL, _schedule)
    t.daemon = True
    t.start()

    print(f"  ✅  Servidor pronto na porta {PORT}\n", flush=True)
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
