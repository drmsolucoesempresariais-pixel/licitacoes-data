# Monitor de Licitações — Deploy na Nuvem

Lê as pastas de licitações do **OneDrive** (via Microsoft Graph) e mostra tudo num painel web.
Não precisa registrar nada no Azure — usa o app público oficial da Microsoft para login.

## Passo 1 — Obter as Credenciais (2 min)

No seu PC, com Python instalado:

```bash
cd cloud
pip install requests
python setup_auth.py
```

O script vai:
1. Mostrar um **código** e abrir uma página da Microsoft (https://www.microsoft.com/link)
2. Você digita o código e faz login **com a conta dedicada do projeto** (a dona da pasta de licitações)
3. Ele localiza a pasta `CENTRAL DE LICITAÇÕES/PREGÕES EM DESENVOLVIMENTO` e mostra as subpastas pra você confirmar
4. No final, exibe **três valores**:
   - `AZURE_CLIENT_ID`
   - `AZURE_REFRESH_TOKEN`
   - `ONEDRIVE_FOLDER_ID`

Guarde esses valores (o refresh_token é como uma senha — não compartilhe).

> Se a pasta tiver outro nome/caminho, é só editar a linha `PASTA_MATRIZ` no topo do `setup_auth.py`, ou colar o caminho quando o script perguntar.

---

## Passo 2 — Deploy no Railway

1. Acesse https://railway.app e crie uma conta (grátis)
2. **New Project** → **Deploy from GitHub repo** (ou deploy da pasta local `cloud/`)
3. Vá em **Variables** e adicione:
   ```
   AZURE_REFRESH_TOKEN  = (valor do passo 1)
   ONEDRIVE_FOLDER_ID   = (valor do passo 1)
   GITHUB_TOKEN         = (token do GitHub para salvar edições)
   GITHUB_USER          = (seu usuário no GitHub)
   GITHUB_REPO          = licitacoes-data
   ```
   `AZURE_CLIENT_ID` é opcional — o código já usa o client público da Microsoft por padrão.
   Opcional (habilita análise IA por licitação):
   ```
   CLAUDE_API_KEY       = (chave da Anthropic)
   ```
4. Railway faz o deploy automaticamente
5. **Settings** → **Networking** → **Generate Domain**
6. Sua URL será algo como: `https://monitor-licitacoes.up.railway.app`

Compartilhe essa URL com os colegas. Pronto!

---

## Estrutura esperada da pasta matriz (ONEDRIVE_FOLDER_ID)

A pasta monitorada deve conter subpastas. Qualquer subpasta é detectada automaticamente como portal:

```
PREGÕES EM DESENVOLVIMENTO/   ← esta é a pasta cujo ID vai em ONEDRIVE_FOLDER_ID
├── BBMNET/
│   ├── CIDADE - 26-05/
│   └── ...
├── BLL/
│   └── ...
├── COMPRASNET/
│   └── ...
├── JÁ ACONTECIDOS/  ← acompanhamentos recentes
└── FINALIZADOS/     ← histórico antigo
```

Portais novos são detectados automaticamente: basta criar uma nova subpasta.

---

## Atualização automática

O painel atualiza os dados do OneDrive a cada **2 minutos**.
Se você ou um colega adicionar/mover uma pasta no OneDrive, a mudança aparece no painel em até 2 min.

## Edições manuais (status, observações, etc.)

As edições feitas no painel são salvas no arquivo `meta.json` no repositório GitHub.
O OneDrive é somente **leitura** para o painel — ele detecta o estado pelas pastas/arquivos, não grava nada lá.

## Renovação do acesso

O servidor renova o acesso sozinho enquanto estiver rodando. Se o servidor ficar **desligado por mais de ~90 dias**, rode o `setup_auth.py` de novo para gerar um novo `AZURE_REFRESH_TOKEN` e atualize a variável no Railway.
