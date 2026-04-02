# MCP com N8N

## mcp-autopeças com n8n

MCP Server em Python que lê e escreve em abas de uma planilha Google Sheets via protocolo MCP — compatível com Claude Desktop, Claude Code e qualquer MCP Client (inclusive o nó `mcpClientTool` do N8N).

O servidor cobre dois domínios em um único processo, cada um mapeado para uma aba da mesma planilha:

| Domínio | Aba | Tools |
|---------|-----|-------|
| AutoPeças (AutoMax) | `AutoPeças` (gid=0) | 6 tools — somente leitura |
| Leitos Hospitalares | `Leitos` (gid=1562350974) | 9 tools — leitura, escrita, e-mail e SMS |

### Pré-requisitos

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) (recomendado) ou `pip`
- Service Account Google com permissão de **Editor** na planilha
  (necessário para `leitos_atualizar_status_limpeza`)

### Instalação

```bash
cd mcp-alura
uv sync          # ou: pip install -e .
```

### Configuração

**1. Credenciais Google (Service Account)**

No [Google Cloud Console](https://console.cloud.google.com):
1. Crie um projeto → APIs & Services → Enable **Google Sheets API**
2. Crie uma **Service Account** → gere e baixe a chave JSON
3. Compartilhe a planilha com o e-mail da service account com permissão de **Editor**

**2. Variáveis de ambiente**

```bash
cp .env.example .env
```

Edite o `.env` com no mínimo:

```env
SPREADSHEET_ID=1zt4h2v3ldK3zELNNmvyn02elEB9dHdfXD5q85ZYh2k0
AUTOPECAS_SHEET_NAME=AutoPeças
LEITOS_SHEET_NAME=Leitos
GOOGLE_CREDENTIALS_PATH=/caminho/para/service_account.json

# Para leitos_enviar_notificacao (e-mail):
GMAIL_USER=setor@hospital.com.br
GMAIL_APP_PASSWORD=xxxx_xxxx_xxxx_xxxx

# Para leitos_enviar_sms:
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_FROM_NUMBER=+18647139932
```

> O `SPREADSHEET_ID` está na URL: `docs.google.com/spreadsheets/d/**{ID}**/edit`

### Estrutura esperada da planilha

**Aba `AutoPeças`:**

| Código | Nome | Categoria | Marca | Preço | Estoque | Fornecedor | Descrição | Localização |
|--------|------|-----------|-------|-------|---------|------------|-----------|-------------|
| F-1023 | Filtro de Óleo | Motor | Bosch | 35.90 | 48 | AutoDist | ... | Prateleira A3 |

**Aba `Leitos`** — colunas confirmadas pelo schema do nó N8N:

| ID_Leito | Quarto | Tipo_Quarto | Status_Ocupacao | Status_Limpeza | Paciente | Ultima_Limpeza |
|----------|--------|-------------|-----------------|----------------|----------|----------------|
| A-101 | Quarto 10 | Enfermaria | Ocupado | Concluído | João Silva | 2025-03-28 |
| UTI-05 | UTI Norte | UTI | Disponível | Pendente | — | 2025-03-27 |

> Os nomes das colunas podem ser ajustados no `.env` com `COL_*` e `LEITOS_COL_*`.

---

### Ferramentas AutoPeças

| Tool | O que faz |
|------|-----------|
| `autopecas_buscar_peca` | Busca por nome, código ou descrição |
| `autopecas_listar_pecas` | Lista o catálogo com paginação e filtros |
| `autopecas_obter_detalhes` | Detalhes completos de uma peça pelo código |
| `autopecas_listar_categorias` | Categorias com contagem de peças |
| `autopecas_verificar_estoque` | Estoque por código ou resumo por categoria |
| `autopecas_listar_marcas` | Fabricantes com contagem de peças |

### Ferramentas Leitos Hospitalares

Mapeamento dos agentes N8N (`mcp-all-nodes.json`) para tools Python:

| Agent N8N | Filtro | Tools equivalentes |
|-----------|--------|--------------------|
| Agent Diretoria | Acesso total | `leitos_listar_leitos`, `leitos_resumo_ocupacao`, `leitos_verificar_disponibilidade`, `leitos_obter_detalhes_leito` |
| Agent Enfermaria | `Tipo_Quarto = Enfermaria` | `leitos_listar_enfermaria` |
| Agent UTI | `Tipo_Quarto = UTI` | `leitos_listar_uti` |
| Todos os agentes | — | `leitos_enviar_notificacao`, `leitos_enviar_sms` |
| Nó de escrita N8N | `row_number` → `ID_Leito` | `leitos_atualizar_status_limpeza` |

| Tool | Leitura/Escrita | O que faz |
|------|-----------------|-----------|
| `leitos_listar_leitos` | Leitura | Lista todos os leitos; filtrável por `Tipo_Quarto`, `Status_Ocupacao`, `Status_Limpeza` |
| `leitos_listar_enfermaria` | Leitura | Filtro fixo `Tipo_Quarto=Enfermaria` — relatórios de ocupação e limpeza |
| `leitos_listar_uti` | Leitura | Filtro fixo `Tipo_Quarto=UTI` — dias internados e contagem de pacientes |
| `leitos_verificar_disponibilidade` | Leitura | Leitos com `Status_Ocupacao=Disponível`, resumo por tipo |
| `leitos_obter_detalhes_leito` | Leitura | Dados completos de um leito pelo `ID_Leito` |
| `leitos_resumo_ocupacao` | Leitura | Dashboard: ocupação **e** limpeza agrupados por `Tipo_Quarto` |
| `leitos_atualizar_status_limpeza` | **Escrita** | Atualiza `Status_Limpeza` de um leito pelo `ID_Leito` |
| `leitos_enviar_notificacao` | Externo | Envia e-mail via Gmail SMTP |
| `leitos_enviar_sms` | Externo | Envia SMS via Twilio REST API |

**Status_Ocupacao:** `Disponível` 🟢 · `Ocupado` 🔴 · `Reservado` 🔵

**Status_Limpeza:** `Concluído` ✅ · `Pendente` ⚠️ · `Em Andamento` 🔄

**Tipos de quarto:** `Enfermaria` · `UTI` · `Apartamento` · `Semi-Intensivo`

#### Configurando o envio de e-mail

A tool `leitos_enviar_notificacao` usa Gmail SMTP com Senha de App:

1. Ative a verificação em duas etapas na conta Google
2. Acesse [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
3. Crie uma senha para "Email" e cole em `GMAIL_APP_PASSWORD` no `.env`

#### Configurando o envio de SMS

A tool `leitos_enviar_sms` usa a [API REST do Twilio](https://www.twilio.com/docs/sms):

1. Crie uma conta em [twilio.com](https://www.twilio.com)
2. Obtenha `Account SID` e `Auth Token` no dashboard
3. Registre ou compre um número remetente e configure `TWILIO_FROM_NUMBER`

---

### Uso no Claude Desktop

Adicione ao `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "hospital-mcp": {
      "command": "uv",
      "args": ["run", "--project", "/caminho/para/mcp-alura", "python", "server.py"],
      "env": {
        "SPREADSHEET_ID": "1zt4h2v3ldK3zELNNmvyn02elEB9dHdfXD5q85ZYh2k0",
        "AUTOPECAS_SHEET_NAME": "AutoPeças",
        "LEITOS_SHEET_NAME": "Leitos",
        "GOOGLE_CREDENTIALS_PATH": "/caminho/para/service_account.json",
        "GMAIL_USER": "setor@hospital.com.br",
        "GMAIL_APP_PASSWORD": "xxxx_xxxx_xxxx_xxxx",
        "TWILIO_ACCOUNT_SID": "ACxxxxxxxx",
        "TWILIO_AUTH_TOKEN": "xxxxxxxx",
        "TWILIO_FROM_NUMBER": "+18647139932"
      }
    }
  }
}
```

### Uso no Claude Code

```bash
claude mcp add hospital-mcp -- uv run --project /caminho/para/mcp-alura python server.py
```

### Uso no N8N (MCP Client)

Configure o nó `MCP Client Tool` apontando para o endpoint do servidor.
Os três agentes N8N podem compartilhar o mesmo servidor MCP Python,
cada um utilizando as tools adequadas ao seu papel via `include: selected`.

### Teste local

```bash
uv run python server.py
```

Para inspecionar as 15 tools com o MCP Inspector:

```bash
npx @modelcontextprotocol/inspector uv run python server.py
```
