#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔═════════════════════════════════════════════════════╗
║   CONFIGURAÇÃO INICIAL — Execute UMA vez no PC      ║
║   Gera as credenciais do OneDrive para o Railway    ║
╚═════════════════════════════════════════════════════╝

Faz login pelo NAVEGADOR (fluxo Authorization Code + PKCE) usando o
aplicativo público oficial da Microsoft. Não precisa registrar nada no Azure.

Pré-requisito:  pip install requests
Como usar:      python setup_auth.py
                (ou dê 2 cliques em GERAR-CREDENCIAIS.bat)
"""

import base64
import hashlib
import http.server
import os
import secrets
import subprocess
import tempfile
import urllib.parse
import webbrowser

import requests

# Caminho da pasta matriz no OneDrive (conta dedicada do projeto).
# É a pasta que contém os portais (BBMNET, BLL, COMPRASNET, etc.).
PASTA_MATRIZ = "DRM MATRIZ - ESCOLAS E PLANILHAS/CENTRAL DE LICITAÇÕES/PREGÕES EM DESENVOLVIMENTO"

# Client público oficial da Microsoft ("Microsoft Graph Command Line Tools").
# É o mesmo client que o Microsoft Graph PowerShell usa para login no navegador.
CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"

# Endpoint "consumers" = somente contas Microsoft pessoais.
AUTH_BASE = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
SCOPE = "Files.Read offline_access"


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    """Recebe o redirecionamento do navegador com o código de autorização."""

    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = params.get("code", [None])[0]
        error = params.get("error_description", params.get("error", [None]))[0]
        if not code and not error:
            self.send_response(404)
            self.end_headers()
            return
        self.server.auth_code = code
        self.server.auth_error = error
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = ("Tudo certo! Pode fechar esta aba e voltar para o programa."
               if code else f"Erro no login: {error}")
        self.wfile.write(
            ("<html><body style='font-family:sans-serif;text-align:center;"
             f"margin-top:60px'><h2>{msg}</h2></body></html>").encode("utf-8")
        )

    def log_message(self, *args):
        pass  # silencia o log padrão do servidor


def _pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


print()
print("=" * 60)
print("  CONFIGURAÇÃO DO ONEDRIVE — Monitor de Licitações")
print("=" * 60)
print()
print("  IMPORTANTE: faça o login com a CONTA DEDICADA do projeto")
print("  (a conta dona da pasta de licitações no OneDrive).")
print()

verifier, challenge = _pkce_pair()
state = secrets.token_urlsafe(16)

# Servidor local que recebe o código de volta do navegador (porta livre).
httpd = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)
httpd.auth_code = None
httpd.auth_error = None
redirect_uri = f"http://localhost:{httpd.server_address[1]}"

authorize_url = f"{AUTH_BASE}/authorize?" + urllib.parse.urlencode({
    "client_id":             CLIENT_ID,
    "response_type":         "code",
    "redirect_uri":          redirect_uri,
    "response_mode":         "query",
    "scope":                 SCOPE,
    "code_challenge":        challenge,
    "code_challenge_method": "S256",
    "state":                 state,
    "prompt":                "select_account",
})

print("  Abrindo o navegador para você fazer login...")
print()
print("  Se NÃO abrir sozinho, copie e cole este endereço no navegador:")
print(f"  {authorize_url}")
print()
webbrowser.open(authorize_url)
print("  Aguardando o login no navegador...", flush=True)

# Processa requisições até receber o código (ignora favicon e afins).
while httpd.auth_code is None and httpd.auth_error is None:
    httpd.handle_request()

if httpd.auth_error:
    print(f"\n  ERRO no login: {httpd.auth_error}")
    raise SystemExit(1)

# ── Troca o código pelos tokens (PKCE) ──────────────────────────────────────
tok_resp = requests.post(
    f"{AUTH_BASE}/token",
    data={
        "client_id":     CLIENT_ID,
        "grant_type":    "authorization_code",
        "code":          httpd.auth_code,
        "redirect_uri":  redirect_uri,
        "code_verifier": verifier,
        "scope":         SCOPE,
    },
    timeout=30,
)
token = tok_resp.json()

if "access_token" not in token:
    print(f"\n  ERRO ao obter token: {token.get('error_description', token)}")
    raise SystemExit(1)

print()
print("  ✅  Login feito com sucesso!")

access_token  = token["access_token"]
refresh_token = token.get("refresh_token", "")

# Mostra qual conta foi conectada (confirme que é a CONTA DEDICADA do projeto).
HEADERS = {"Authorization": f"Bearer {access_token}"}
try:
    me = requests.get(
        "https://graph.microsoft.com/v1.0/me?$select=displayName,userPrincipalName,mail",
        headers=HEADERS, timeout=15,
    ).json()
    conta = me.get("userPrincipalName") or me.get("mail") or "?"
    print(f"      Conta conectada: {me.get('displayName', '?')}  <{conta}>")
    print("      ⚠️  Confira: é a CONTA DEDICADA do projeto (a dona da pasta)?")
except Exception:
    pass

# ── Localizar a pasta matriz e pegar o ID ───────────────────────────────────
print()
print(f"  Pasta matriz configurada: {PASTA_MATRIZ}")
resp_caminho = input(
    "  Pressione ENTER para confirmar, ou cole outro caminho: "
).strip() or PASTA_MATRIZ

# Aceita também um caminho local do Windows colado (ex.: C:\\...\\OneDrive\\...).
resp_caminho = resp_caminho.replace("\\", "/")
low = resp_caminho.lower()
if "onedrive/" in low:
    resp_caminho = resp_caminho[low.index("onedrive/") + len("onedrive/"):]
resp_caminho = resp_caminho.strip("/")

encoded = urllib.parse.quote(resp_caminho, safe="/")
meta_resp = requests.get(
    f"https://graph.microsoft.com/v1.0/me/drive/root:/{encoded}?$select=id,name",
    headers={"Authorization": f"Bearer {access_token}"},
    timeout=15,
)

if meta_resp.status_code == 200:
    folder      = meta_resp.json()
    folder_id   = folder["id"]
    folder_name = folder["name"]

    child_resp = requests.get(
        f"https://graph.microsoft.com/v1.0/me/drive/items/{folder_id}/children"
        "?$select=name,folder&$top=200&$orderby=name",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    subpastas = [c["name"] for c in child_resp.json().get("value", []) if "folder" in c]

    print()
    print(f"  ✅  Pasta encontrada: {folder_name}")
    print(f"      {len(subpastas)} subpasta(s) dentro dela:")
    for sf in subpastas[:20]:
        print(f"        📁 {sf}")
    if len(subpastas) > 20:
        print(f"        ... e mais {len(subpastas) - 20}")
else:
    print()
    print(f"  ⚠️  Não encontrei a pasta pelo caminho '{resp_caminho}' (código {meta_resp.status_code}).")
    folder_id   = "VERIFIQUE_O_CAMINHO"
    folder_name = "?"

    # Mostra o que existe na RAIZ desta conta (ajuda a entender onde a pasta está).
    print()
    print("  Pastas na RAIZ desta conta do OneDrive:")
    raiz = requests.get(
        "https://graph.microsoft.com/v1.0/me/drive/root/children"
        "?$select=name,folder&$top=200&$orderby=name",
        headers=HEADERS, timeout=15,
    ).json().get("value", [])
    achou_raiz = False
    for c in raiz:
        if "folder" in c:
            achou_raiz = True
            print(f"        📁 {c['name']}")
    if not achou_raiz:
        print("        (nenhuma pasta — esta conta pode estar vazia ou ser a conta errada)")

    # Procura automaticamente a pasta pelo nome em toda a conta.
    leaf = PASTA_MATRIZ.rstrip("/").split("/")[-1]   # ex.: "PREGÕES EM DESENVOLVIMENTO"
    print()
    print(f"  Procurando uma pasta chamada '{leaf}' na conta inteira...")
    achados = requests.get(
        "https://graph.microsoft.com/v1.0/me/drive/root/search(q='"
        + urllib.parse.quote(leaf) + "')?$select=name,id,folder,parentReference&$top=25",
        headers=HEADERS, timeout=15,
    ).json().get("value", [])
    candidatos = [a for a in achados
                  if "folder" in a and a["name"].strip().upper() == leaf.strip().upper()]
    if not candidatos:
        candidatos = [a for a in achados
                      if "folder" in a and leaf.split()[0].upper() in a["name"].upper()]
    for a in candidatos:
        caminho = a.get("parentReference", {}).get("path", "").split("root:", 1)[-1] or "/"
        print(f"        ✅ Encontrada: {a['name']}  (dentro de: {caminho or '/'})")
    if len(candidatos) == 1:
        folder_id   = candidatos[0]["id"]
        folder_name = candidatos[0]["name"]
        print()
        print("  ✅  Achei a pasta sozinho — vou usar o ID dela automaticamente.")
    elif len(candidatos) > 1:
        print()
        print("  ⚠️  Há mais de uma pasta com esse nome. Me diga (pelo caminho) qual é a certa.")
    else:
        print("        (não encontrei nada parecido — provavelmente é a conta errada)")

# ── Confirma que a renovação automática (refresh token) funciona ────────────
# É exatamente o que o servidor no Railway vai usar 24/7.
refresh_ok = False
if refresh_token:
    rt_resp = requests.post(
        f"{AUTH_BASE}/token",
        data={
            "client_id":     CLIENT_ID,
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
            "scope":         SCOPE,
        },
        timeout=30,
    )
    rt = rt_resp.json()
    refresh_ok = "access_token" in rt
    if refresh_ok and rt.get("refresh_token"):
        refresh_token = rt["refresh_token"]  # usa o token mais recente (rotacionado)

print()
print()
print("  ✅  Pronto! Copie os valores abaixo para as variáveis do Railway:")
print()
print("  ┌──────────────────────────────────────────────────────────────────┐")
print(f"  │ AZURE_CLIENT_ID     = {CLIENT_ID}")
print(f"  │ AZURE_REFRESH_TOKEN = {refresh_token}")
print(f"  │ ONEDRIVE_FOLDER_ID  = {folder_id}")
print(f"  │                       ^ pasta: {folder_name}")
print("  └──────────────────────────────────────────────────────────────────┘")
print()
if refresh_ok:
    print("  ✅  Testei a renovação automática do acesso: funcionando.")
else:
    print("  ⚠️  Não consegui validar a renovação automática — me avise se aparecer este aviso.")
print()
print("  Guarde também em um lugar seguro — o AZURE_REFRESH_TOKEN é sua senha.")
print()

# Abre o token no Bloco de Notas para copiar sem erro (o terminal quebra a linha).
_token_file = os.path.join(tempfile.gettempdir(), "AZURE_REFRESH_TOKEN.txt")
try:
    with open(_token_file, "w", encoding="utf-8") as f:
        f.write(refresh_token)
    print("  📋  Vou abrir o AZURE_REFRESH_TOKEN no Bloco de Notas para você copiar.")
    print("      Lá é só apertar Ctrl+A e Ctrl+C, e colar no Railway.")
    print(f"      (apague esse arquivo depois: {_token_file})")
    subprocess.Popen(["notepad.exe", _token_file])
except Exception as e:
    print(f"  (não consegui abrir o Bloco de Notas automaticamente: {e})")
print()
