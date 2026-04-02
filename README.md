# MCP com N8N

## mcp-autopeças com n8n

MCP Server em Python que lê abas de uma planilha Google Sheets e expõe dados via protocolo MCP — compatível com Claude Desktop, Claude Code e qualquer MCP Client (inclusive o nó `mcpClientTool` do N8N).

O servidor cobre dois domínios em um único processo, cada um mapeado para uma aba da mesma planilha:

| Domínio | Aba | Tools |
|---------|-----|-------|
| AutoPeças (AutoMax) | `AutoPeças` | 6 tools de catálogo e estoque |
| Leitos Hospitalares | `Leitos` | 6 tools de gestão e notificação |

### Pré-requisitos

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) (recomendado) ou `pip`
- Conta Google com acesso à planilha
- Service Account com permissão de leitura na planilha

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
3. Compartilhe a planilha com o e-mail da service account (permissão de leitor)

**2. Variáveis de ambiente**

```bash
cp .env.example .env
```

Edite o `.env` com no mínimo:

```env
SPREADSHEET_ID=1zt4h2v3ldK3zELNNmvyn02elEB9dHdfXD5q85ZYh2k0
SHEET_NAME=AutoPeças
LEITOS_SHEET_NAME=Leitos
GOOGLE_CREDENTIALS_PATH=/caminho/para/service_account.json

# Necessário apenas para leitos_enviar_notificacao:
GMAIL_USER=setor@hospital.com.br
GMAIL_APP_PASSWORD=xxxx_xxxx_xxxx_xxxx
```

> O `SPREADSHEET_ID` está na URL da planilha:
> `docs.google.com/spreadsheets/d/**{ID}**/edit`

### Estrutura esperada da planilha

**Aba `AutoPeças`:**

| Código | Nome | Categoria | Marca | Preço | Estoque | Fornecedor | Descrição | Localização |
|--------|------|-----------|-------|-------|---------|------------|-----------|-------------|
| F-1023 | Filtro de Óleo | Motor | Bosch | 35.90 | 48 | AutoDist | ... | Prateleira A3 |

**Aba `Leitos`:**

| Leito | Tipo_Quarto | Status | Paciente | Setor | Data_Internacao | Previsao_Alta | Medico | Observacoes |
|-------|-------------|--------|----------|-------|-----------------|---------------|--------|-------------|
| A-101 | Enfermaria | Ocupado | João Silva | Ortopedia | 2025-03-28 | 2025-04-05 | Dr. Costa | Pós-op |
| UTI-03 | UTI | Disponível | — | UTI Adulto | — | — | — | — |

> Os nomes das colunas de ambas as abas podem ser ajustados no `.env` com `COL_*` e `LEITOS_COL_*`.

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

Mapeamento dos agentes N8N para tools Python:

| Agent N8N | Equivalente MCP |
|-----------|-----------------|
| Agent Diretoria (acesso total) | `leitos_listar_leitos`, `leitos_resumo_ocupacao`, `leitos_verificar_disponibilidade`, `leitos_obter_detalhes_leito` |
| Agent Diretoria (Gmail `Enviar`) | `leitos_enviar_notificacao` |
| Agent Enfermaria (filtro `Tipo_Quarto=Enfermaria`) | `leitos_listar_enfermaria` |

| Tool | O que faz |
|------|-----------|
| `leitos_listar_leitos` | Lista todos os leitos com filtros por tipo, status e setor |
| `leitos_listar_enfermaria` | Lista apenas leitos de Enfermaria (filtro fixo, como o Agent Enfermaria) |
| `leitos_verificar_disponibilidade` | Leitos com status Disponível, com resumo por tipo de quarto |
| `leitos_obter_detalhes_leito` | Dados completos de um leito pelo ID |
| `leitos_resumo_ocupacao` | Dashboard de ocupação agrupado por tipo de quarto e status |
| `leitos_enviar_notificacao` | Envia e-mail via Gmail SMTP (equivale ao `gmailTool` do N8N) |

**Status possíveis:** `Disponível` 🟢 · `Ocupado` 🔴 · `Limpeza` 🟡 · `Manutenção` 🔧 · `Reservado` 🔵

**Tipos de quarto:** `Enfermaria` · `UTI` · `Apartamento` · `Semi-Intensivo`

#### Configurando o envio de e-mail

A tool `leitos_enviar_notificacao` usa Gmail SMTP com Senha de App:

1. Ative a verificação em duas etapas na conta Google
2. Acesse [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
3. Crie uma senha para "Email" e cole em `GMAIL_APP_PASSWORD` no `.env`

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
        "SHEET_NAME": "AutoPeças",
        "LEITOS_SHEET_NAME": "Leitos",
        "GOOGLE_CREDENTIALS_PATH": "/caminho/para/service_account.json",
        "GMAIL_USER": "setor@hospital.com.br",
        "GMAIL_APP_PASSWORD": "xxxx_xxxx_xxxx_xxxx"
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

Configure o nó `MCP Client Tool` apontando para o endpoint do servidor (modo HTTP)
ou para o script local (modo stdio). Os dois agentes N8N podem compartilhar o mesmo
servidor MCP — cada um usará as tools adequadas ao seu papel:

```
# Agent Diretoria
Utilize o MCP para consultar todos os leitos e enviar notificações por e-mail.

# Agent Enfermaria
Utilize leitos_listar_enfermaria para ver apenas leitos de Enfermaria.
```

### Teste local

```bash
uv run python server.py
```

Para inspecionar as 12 tools com o MCP Inspector:

```bash
npx @modelcontextprotocol/inspector uv run python server.py
```
