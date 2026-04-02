# MCP com N8N

## mcp-autopeças com n8n

MCP Server em Python que lê uma aba de uma planilha Google Sheets e expõe os dados de autopeças via protocolo MCP — compatível com Claude Desktop, Claude Code e qualquer MCP Client (inclusive o nó `mcpClientTool` do N8N).

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

Edite o `.env`:

```env
SPREADSHEET_ID=1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms
SHEET_NAME=AutoPeças
GOOGLE_CREDENTIALS_PATH=/caminho/para/service_account.json
```

> O `SPREADSHEET_ID` está na URL da planilha:
> `docs.google.com/spreadsheets/d/**{ID}**/edit`

### Estrutura esperada da planilha

| Código | Nome | Categoria | Marca | Preço | Estoque | Fornecedor | Descrição | Localização |
|--------|------|-----------|-------|-------|---------|------------|-----------|-------------|
| F-1023 | Filtro de Óleo | Motor | Bosch | 35.90 | 48 | AutoDist | ... | Prateleira A3 |

> Os nomes das colunas podem ser ajustados no `.env` com `COL_CODIGO`, `COL_NOME`, etc.

### Ferramentas disponíveis

| Tool | O que faz |
|------|-----------|
| `autopecas_buscar_peca` | Busca por nome, código ou descrição |
| `autopecas_listar_pecas` | Lista o catálogo com paginação e filtros |
| `autopecas_obter_detalhes` | Detalhes completos de uma peça pelo código |
| `autopecas_listar_categorias` | Categorias com contagem de peças |
| `autopecas_verificar_estoque` | Estoque por código ou resumo por categoria |
| `autopecas_listar_marcas` | Fabricantes com contagem de peças |

### Uso no Claude Desktop

Adicione ao `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "autopecas": {
      "command": "uv",
      "args": ["run", "--project", "/caminho/para/mcp-alura", "python", "server.py"],
      "env": {
        "SPREADSHEET_ID": "seu_id_aqui",
        "SHEET_NAME": "AutoPeças",
        "GOOGLE_CREDENTIALS_PATH": "/caminho/para/service_account.json"
      }
    }
  }
}
```

### Uso no Claude Code

```bash
claude mcp add autopecas -- uv run --project /caminho/para/mcp-alura python server.py
```

### Uso no N8N (MCP Client)

No workflow N8N, configure o nó `MCP Client Tool` com a URL do servidor
(modo HTTP) ou aponte para o script local (modo stdio via subprocesso).
O agente AI do N8N acessa as ferramentas automaticamente pelo sistema prompt:

```
Utilize a Base de Dados AutoPeças dentro do MCP
```

### Teste local

```bash
uv run python server.py
```

Para inspecionar as tools com o MCP Inspector:

```bash
npx @modelcontextprotocol/inspector uv run python server.py
```
