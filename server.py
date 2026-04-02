#!/usr/bin/env python3
"""
MCP Server - AutoMax & Leitos Hospitalares

Servidor MCP com dois domínios de dados via Google Sheets (mesma planilha, abas distintas):

  • AutoPeças (AutoMax) — aba configurada em SHEET_NAME
      Ferramentas: busca, listagem, detalhes, categorias, estoque, marcas.

  • Leitos Hospitalares — aba configurada em LEITOS_SHEET_NAME
      Ferramentas: listagem geral, enfermaria, disponibilidade, detalhes de leito,
      resumo de ocupação e envio de notificações por e-mail (Gmail SMTP).
      Espelha os dois agentes N8N: Agent Diretoria (acesso total + e-mail) e
      Agent Enfermaria (filtrado por Tipo_Quarto = "Enfermaria").

Configuração via .env:
    SPREADSHEET_ID          - ID da planilha Google Sheets compartilhada
    SHEET_NAME              - Aba de autopeças    (padrão: "AutoPeças")
    LEITOS_SHEET_NAME       - Aba de leitos       (padrão: "Leitos")
    GOOGLE_CREDENTIALS_PATH - Caminho para o JSON da Service Account
    GOOGLE_CREDENTIALS_JSON - JSON da Service Account como string (alternativa)
    GMAIL_USER              - E-mail remetente para notificações
    GMAIL_APP_PASSWORD      - Senha de App do Gmail (SMTP)
"""

import json
import logging
import os
import smtplib
import ssl
import sys
from email.mime.text import MIMEText
from enum import Enum
from functools import lru_cache
from typing import Any, Optional

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Configuração inicial
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("mcp-server")

# Escopos necessários para leitura do Google Sheets
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

SPREADSHEET_ID: str = os.getenv("SPREADSHEET_ID", "")

# --- AutoPeças ---
SHEET_NAME: str = os.getenv("SHEET_NAME", "AutoPeças")
COL_CODIGO: str = os.getenv("COL_CODIGO", "Código")
COL_NOME: str = os.getenv("COL_NOME", "Nome")
COL_CATEGORIA: str = os.getenv("COL_CATEGORIA", "Categoria")
COL_MARCA: str = os.getenv("COL_MARCA", "Marca")
COL_PRECO: str = os.getenv("COL_PRECO", "Preço")
COL_ESTOQUE: str = os.getenv("COL_ESTOQUE", "Estoque")
COL_FORNECEDOR: str = os.getenv("COL_FORNECEDOR", "Fornecedor")
COL_DESCRICAO: str = os.getenv("COL_DESCRICAO", "Descrição")
COL_LOCALIZACAO: str = os.getenv("COL_LOCALIZACAO", "Localização")

# --- Leitos Hospitalares ---
LEITOS_SHEET_NAME: str = os.getenv("LEITOS_SHEET_NAME", "Leitos")
LEITOS_COL_LEITO: str = os.getenv("LEITOS_COL_LEITO", "Leito")
LEITOS_COL_TIPO_QUARTO: str = os.getenv("LEITOS_COL_TIPO_QUARTO", "Tipo_Quarto")
LEITOS_COL_STATUS: str = os.getenv("LEITOS_COL_STATUS", "Status")
LEITOS_COL_PACIENTE: str = os.getenv("LEITOS_COL_PACIENTE", "Paciente")
LEITOS_COL_SETOR: str = os.getenv("LEITOS_COL_SETOR", "Setor")
LEITOS_COL_DATA_INTERNACAO: str = os.getenv("LEITOS_COL_DATA_INTERNACAO", "Data_Internacao")
LEITOS_COL_PREVISAO_ALTA: str = os.getenv("LEITOS_COL_PREVISAO_ALTA", "Previsao_Alta")
LEITOS_COL_MEDICO: str = os.getenv("LEITOS_COL_MEDICO", "Medico")
LEITOS_COL_OBSERVACOES: str = os.getenv("LEITOS_COL_OBSERVACOES", "Observacoes")

# --- Gmail SMTP ---
GMAIL_USER: str = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD: str = os.getenv("GMAIL_APP_PASSWORD", "")

# ---------------------------------------------------------------------------
# Cliente Google Sheets
# ---------------------------------------------------------------------------


def _build_credentials() -> Credentials:
    """Constrói credenciais Google a partir de variáveis de ambiente."""
    creds_path = os.getenv("GOOGLE_CREDENTIALS_PATH")
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")

    if creds_path:
        return Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    if creds_json:
        info = json.loads(creds_json)
        return Credentials.from_service_account_info(info, scopes=SCOPES)

    raise EnvironmentError(
        "Configure GOOGLE_CREDENTIALS_PATH ou GOOGLE_CREDENTIALS_JSON no .env"
    )


@lru_cache(maxsize=8)
def _get_worksheet(sheet_name: str) -> gspread.Worksheet:
    """Retorna um worksheet pelo nome (cacheado por nome de aba)."""
    if not SPREADSHEET_ID:
        raise EnvironmentError("SPREADSHEET_ID não configurado no .env")
    creds = _build_credentials()
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    return spreadsheet.worksheet(sheet_name)


def _get_sheet() -> gspread.Worksheet:
    """Worksheet da aba AutoPeças."""
    return _get_worksheet(SHEET_NAME)


def _get_leitos_sheet() -> gspread.Worksheet:
    """Worksheet da aba Leitos."""
    return _get_worksheet(LEITOS_SHEET_NAME)


def _get_all_records() -> list[dict[str, Any]]:
    """Todos os registros da aba AutoPeças."""
    return _get_sheet().get_all_records()


def _get_leitos_records() -> list[dict[str, Any]]:
    """Todos os registros da aba Leitos."""
    return _get_leitos_sheet().get_all_records()


# ---------------------------------------------------------------------------
# Utilitários compartilhados
# ---------------------------------------------------------------------------


def _normalizar(texto: str) -> str:
    """Normaliza texto para comparação case-insensitive sem acentos."""
    import unicodedata
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii").lower()


def _paginar(items: list, limit: int, offset: int) -> dict[str, Any]:
    """Aplica paginação e retorna metadados."""
    total = len(items)
    pagina = items[offset: offset + limit]
    has_more = total > offset + len(pagina)
    return {
        "total": total,
        "count": len(pagina),
        "offset": offset,
        "has_more": has_more,
        "next_offset": offset + len(pagina) if has_more else None,
        "items": pagina,
    }


def _handle_error(e: Exception, sheet_name: str = "") -> str:
    """Formata erros de forma clara e acionável."""
    aba = sheet_name or SHEET_NAME
    if isinstance(e, EnvironmentError):
        return f"Erro de configuração: {e}"
    if isinstance(e, gspread.exceptions.SpreadsheetNotFound):
        return "Erro: Planilha não encontrada. Verifique o SPREADSHEET_ID no .env."
    if isinstance(e, gspread.exceptions.WorksheetNotFound):
        return f"Erro: Aba '{aba}' não encontrada na planilha. Verifique as variáveis no .env."
    if isinstance(e, gspread.exceptions.APIError):
        return f"Erro na API do Google Sheets: {e}. Verifique as permissões da Service Account."
    return f"Erro inesperado ({type(e).__name__}): {e}"


# ---------------------------------------------------------------------------
# Utilitários — AutoPeças
# ---------------------------------------------------------------------------


def _filtrar_registros(
    registros: list[dict[str, Any]],
    query: Optional[str] = None,
    categoria: Optional[str] = None,
    marca: Optional[str] = None,
    apenas_em_estoque: bool = False,
) -> list[dict[str, Any]]:
    """Aplica filtros combinados sobre registros de peças."""
    resultado = registros

    if query:
        q = _normalizar(query)
        resultado = [
            r for r in resultado
            if q in _normalizar(str(r.get(COL_CODIGO, "")))
            or q in _normalizar(str(r.get(COL_NOME, "")))
            or q in _normalizar(str(r.get(COL_DESCRICAO, "")))
        ]
    if categoria:
        cat = _normalizar(categoria)
        resultado = [r for r in resultado if _normalizar(str(r.get(COL_CATEGORIA, ""))) == cat]
    if marca:
        m = _normalizar(marca)
        resultado = [r for r in resultado if _normalizar(str(r.get(COL_MARCA, ""))) == m]
    if apenas_em_estoque:
        resultado = [r for r in resultado if _estoque_disponivel(r)]

    return resultado


def _estoque_disponivel(registro: dict[str, Any]) -> bool:
    """Verifica se o registro possui estoque maior que zero."""
    try:
        return int(str(registro.get(COL_ESTOQUE, "0")).replace(",", "").strip() or "0") > 0
    except (ValueError, TypeError):
        return False


def _formatar_peca_markdown(r: dict[str, Any]) -> str:
    """Formata um registro de peça como Markdown."""
    linhas = [
        f"### {r.get(COL_NOME, 'N/A')} — `{r.get(COL_CODIGO, 'N/A')}`",
        f"- **Categoria**: {r.get(COL_CATEGORIA, 'N/A')}",
        f"- **Marca**: {r.get(COL_MARCA, 'N/A')}",
        f"- **Preço**: R$ {r.get(COL_PRECO, 'N/A')}",
        f"- **Estoque**: {r.get(COL_ESTOQUE, 'N/A')} un.",
    ]
    if r.get(COL_FORNECEDOR):
        linhas.append(f"- **Fornecedor**: {r[COL_FORNECEDOR]}")
    if r.get(COL_LOCALIZACAO):
        linhas.append(f"- **Localização**: {r[COL_LOCALIZACAO]}")
    if r.get(COL_DESCRICAO):
        linhas.append(f"- **Descrição**: {r[COL_DESCRICAO]}")
    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# Utilitários — Leitos Hospitalares
# ---------------------------------------------------------------------------


def _filtrar_leitos(
    registros: list[dict[str, Any]],
    tipo_quarto: Optional[str] = None,
    status: Optional[str] = None,
    setor: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Aplica filtros combinados sobre registros de leitos."""
    resultado = registros

    if tipo_quarto:
        tq = _normalizar(tipo_quarto)
        resultado = [r for r in resultado if _normalizar(str(r.get(LEITOS_COL_TIPO_QUARTO, ""))) == tq]
    if status:
        st = _normalizar(status)
        resultado = [r for r in resultado if _normalizar(str(r.get(LEITOS_COL_STATUS, ""))) == st]
    if setor:
        se = _normalizar(setor)
        resultado = [r for r in resultado if _normalizar(str(r.get(LEITOS_COL_SETOR, ""))) == se]

    return resultado


def _formatar_leito_markdown(r: dict[str, Any]) -> str:
    """Formata um registro de leito como Markdown."""
    status = str(r.get(LEITOS_COL_STATUS, "N/A"))
    emoji = {"disponivel": "🟢", "ocupado": "🔴", "limpeza": "🟡", "manutencao": "🔧", "reservado": "🔵"}.get(
        _normalizar(status), "⚪"
    )
    linhas = [
        f"### {emoji} Leito `{r.get(LEITOS_COL_LEITO, 'N/A')}` — {status}",
        f"- **Tipo de Quarto**: {r.get(LEITOS_COL_TIPO_QUARTO, 'N/A')}",
        f"- **Setor**: {r.get(LEITOS_COL_SETOR, 'N/A')}",
    ]
    if r.get(LEITOS_COL_PACIENTE):
        linhas.append(f"- **Paciente**: {r[LEITOS_COL_PACIENTE]}")
    if r.get(LEITOS_COL_MEDICO):
        linhas.append(f"- **Médico**: {r[LEITOS_COL_MEDICO]}")
    if r.get(LEITOS_COL_DATA_INTERNACAO):
        linhas.append(f"- **Internação**: {r[LEITOS_COL_DATA_INTERNACAO]}")
    if r.get(LEITOS_COL_PREVISAO_ALTA):
        linhas.append(f"- **Previsão de Alta**: {r[LEITOS_COL_PREVISAO_ALTA]}")
    if r.get(LEITOS_COL_OBSERVACOES):
        linhas.append(f"- **Observações**: {r[LEITOS_COL_OBSERVACOES]}")
    return "\n".join(linhas)


def _enviar_email_gmail(destinatario: str, assunto: str, mensagem: str) -> None:
    """Envia e-mail via Gmail SMTP com SSL (porta 465)."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        raise EnvironmentError(
            "Configure GMAIL_USER e GMAIL_APP_PASSWORD no .env para usar o envio de e-mail."
        )
    msg = MIMEText(mensagem, "plain", "utf-8")
    msg["Subject"] = assunto
    msg["From"] = GMAIL_USER
    msg["To"] = destinatario

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as servidor:
        servidor.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        servidor.sendmail(GMAIL_USER, destinatario, msg.as_string())


# ---------------------------------------------------------------------------
# Enums e modelos de entrada — AutoPeças
# ---------------------------------------------------------------------------


class FormatoResposta(str, Enum):
    """Formato de saída das ferramentas."""
    MARKDOWN = "markdown"
    JSON = "json"


class BuscarPecaInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    query: str = Field(
        ..., description="Texto livre para buscar por código, nome ou descrição (ex: 'filtro de óleo', 'F-1023')",
        min_length=1, max_length=200,
    )
    categoria: Optional[str] = Field(default=None, description="Filtrar por categoria (ex: 'Motor', 'Freios')", max_length=100)
    marca: Optional[str] = Field(default=None, description="Filtrar por marca (ex: 'Bosch', 'Mahle')", max_length=100)
    apenas_em_estoque: bool = Field(default=False, description="Se True, retorna apenas peças com estoque > 0")
    limit: int = Field(default=20, description="Máximo de resultados por página (1–100)", ge=1, le=100)
    offset: int = Field(default=0, description="Deslocamento para paginação", ge=0)
    formato: FormatoResposta = Field(default=FormatoResposta.MARKDOWN, description="'markdown' ou 'json'")

    @field_validator("query")
    @classmethod
    def query_nao_vazia(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("O campo query não pode ser vazio.")
        return v


class ListarPecasInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    categoria: Optional[str] = Field(default=None, description="Filtrar por categoria", max_length=100)
    marca: Optional[str] = Field(default=None, description="Filtrar por marca", max_length=100)
    apenas_em_estoque: bool = Field(default=False, description="Se True, apenas peças com estoque > 0")
    limit: int = Field(default=20, description="Máximo de resultados por página (1–100)", ge=1, le=100)
    offset: int = Field(default=0, description="Deslocamento para paginação", ge=0)
    formato: FormatoResposta = Field(default=FormatoResposta.MARKDOWN, description="'markdown' ou 'json'")


class ObterDetalhesPecaInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    codigo: str = Field(..., description="Código único da peça (ex: 'F-1023', 'BP-0042')", min_length=1, max_length=50)
    formato: FormatoResposta = Field(default=FormatoResposta.MARKDOWN, description="'markdown' ou 'json'")


class ListarCategoriasInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    formato: FormatoResposta = Field(default=FormatoResposta.MARKDOWN, description="'markdown' ou 'json'")


class VerificarEstoqueInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    codigos: Optional[list[str]] = Field(
        default=None,
        description="Lista de códigos de peças (ex: ['F-1023', 'BP-0042']). Se omitido, retorna resumo por categoria.",
        max_length=50,
    )
    categoria: Optional[str] = Field(default=None, description="Filtrar resumo por categoria", max_length=100)
    formato: FormatoResposta = Field(default=FormatoResposta.MARKDOWN, description="'markdown' ou 'json'")


class ListarMarcasInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    categoria: Optional[str] = Field(default=None, description="Filtrar marcas por categoria", max_length=100)
    formato: FormatoResposta = Field(default=FormatoResposta.MARKDOWN, description="'markdown' ou 'json'")


# ---------------------------------------------------------------------------
# Modelos de entrada — Leitos Hospitalares
# ---------------------------------------------------------------------------


class ListarLeitosInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    tipo_quarto: Optional[str] = Field(
        default=None,
        description="Filtrar por tipo de quarto (ex: 'Enfermaria', 'UTI', 'Apartamento', 'Semi-Intensivo')",
        max_length=100,
    )
    status: Optional[str] = Field(
        default=None,
        description="Filtrar por status do leito (ex: 'Disponível', 'Ocupado', 'Limpeza', 'Manutenção', 'Reservado')",
        max_length=50,
    )
    setor: Optional[str] = Field(
        default=None,
        description="Filtrar por setor/ala hospitalar (ex: 'Cardiologia', 'Ortopedia')",
        max_length=100,
    )
    limit: int = Field(default=20, description="Máximo de resultados por página (1–100)", ge=1, le=100)
    offset: int = Field(default=0, description="Deslocamento para paginação", ge=0)
    formato: FormatoResposta = Field(default=FormatoResposta.MARKDOWN, description="'markdown' ou 'json'")


class ObterDetalhesLeitoInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    leito_id: str = Field(
        ...,
        description="Identificador do leito (ex: 'A-101', 'UTI-05')",
        min_length=1,
        max_length=50,
    )
    formato: FormatoResposta = Field(default=FormatoResposta.MARKDOWN, description="'markdown' ou 'json'")


class VerificarDisponibilidadeInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    tipo_quarto: Optional[str] = Field(
        default=None,
        description="Filtrar disponibilidade por tipo de quarto (ex: 'UTI', 'Enfermaria')",
        max_length=100,
    )
    setor: Optional[str] = Field(
        default=None,
        description="Filtrar disponibilidade por setor/ala",
        max_length=100,
    )
    formato: FormatoResposta = Field(default=FormatoResposta.MARKDOWN, description="'markdown' ou 'json'")


class ResumoOcupacaoInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    setor: Optional[str] = Field(
        default=None,
        description="Filtrar resumo por setor/ala hospitalar",
        max_length=100,
    )
    formato: FormatoResposta = Field(default=FormatoResposta.MARKDOWN, description="'markdown' ou 'json'")


class ListarEnfermariaInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    status: Optional[str] = Field(
        default=None,
        description="Filtrar leitos de enfermaria por status (ex: 'Disponível', 'Ocupado', 'Limpeza')",
        max_length=50,
    )
    setor: Optional[str] = Field(
        default=None,
        description="Filtrar leitos de enfermaria por setor",
        max_length=100,
    )
    limit: int = Field(default=20, description="Máximo de resultados por página (1–100)", ge=1, le=100)
    offset: int = Field(default=0, description="Deslocamento para paginação", ge=0)
    formato: FormatoResposta = Field(default=FormatoResposta.MARKDOWN, description="'markdown' ou 'json'")


class EnviarNotificacaoInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    destinatario: str = Field(
        ...,
        description="E-mail do destinatário (ex: 'enfermeira@hospital.com.br')",
        min_length=5,
        max_length=200,
    )
    assunto: str = Field(
        ...,
        description="Assunto do e-mail (ex: 'Leito A-101 disponível para internação')",
        min_length=1,
        max_length=200,
    )
    mensagem: str = Field(
        ...,
        description="Corpo da mensagem de texto simples",
        min_length=1,
        max_length=5000,
    )

    @field_validator("destinatario")
    @classmethod
    def email_valido(cls, v: str) -> str:
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("E-mail do destinatário inválido.")
        return v.lower()


# ---------------------------------------------------------------------------
# Servidor MCP
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "autopecas_leitos_mcp",
    instructions=(
        "Você é um assistente integrado com duas bases de dados via Google Sheets:\n"
        "1. AutoPeças (AutoMax): consulte peças, estoque, categorias e marcas.\n"
        "2. Leitos Hospitalares: gerencie leitos, verifique disponibilidade, "
        "consulte ocupação por tipo de quarto e envie notificações por e-mail."
    ),
)

# ===========================================================================
# FERRAMENTAS — AutoPeças
# ===========================================================================


@mcp.tool(
    name="autopecas_buscar_peca",
    annotations={"title": "Buscar Peça por Nome, Código ou Descrição",
                 "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def autopecas_buscar_peca(params: BuscarPecaInput) -> str:
    """Busca peças na base AutoPeças por texto livre (nome, código ou descrição).

    Busca case-insensitive e sem distinção de acentos nos campos código, nome e descrição.
    Suporta filtros adicionais por categoria, marca e disponibilidade de estoque.

    Args:
        params (BuscarPecaInput):
            - query (str): Texto para busca (ex: 'filtro', 'vela', 'F-1023')
            - categoria (Optional[str]): Filtro por categoria (ex: 'Motor')
            - marca (Optional[str]): Filtro por marca (ex: 'Bosch')
            - apenas_em_estoque (bool): Se True, apenas peças com estoque disponível
            - limit (int): Máximo de resultados (padrão: 20)
            - offset (int): Paginação (padrão: 0)
            - formato (str): 'markdown' ou 'json'

    Returns:
        str: Lista de peças com código, nome, categoria, preço e estoque.

    Exemplos:
        - "Buscar filtros de óleo" → query="filtro de óleo"
        - "Velas Bosch disponíveis" → query="vela", marca="Bosch", apenas_em_estoque=True
    """
    try:
        registros = _get_all_records()
        filtrados = _filtrar_registros(
            registros, query=params.query, categoria=params.categoria,
            marca=params.marca, apenas_em_estoque=params.apenas_em_estoque,
        )
        if not filtrados:
            return f"Nenhuma peça encontrada para '{params.query}'."

        pagina = _paginar(filtrados, params.limit, params.offset)

        if params.formato == FormatoResposta.JSON:
            return json.dumps(pagina, ensure_ascii=False, indent=2)

        linhas = [
            f"# Resultados para '{params.query}'",
            f"Encontradas **{pagina['total']}** peças (exibindo {pagina['count']})", "",
        ]
        for r in pagina["items"]:
            linhas += [_formatar_peca_markdown(r), ""]
        if pagina["has_more"]:
            linhas.append(f"_Use `offset={pagina['next_offset']}` para ver mais resultados._")
        return "\n".join(linhas)

    except Exception as e:
        return _handle_error(e, SHEET_NAME)


@mcp.tool(
    name="autopecas_listar_pecas",
    annotations={"title": "Listar Todas as Peças",
                 "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def autopecas_listar_pecas(params: ListarPecasInput) -> str:
    """Lista todas as peças cadastradas na base AutoPeças com paginação e filtros opcionais.

    Args:
        params (ListarPecasInput):
            - categoria (Optional[str]): Filtrar por categoria
            - marca (Optional[str]): Filtrar por marca
            - apenas_em_estoque (bool): Apenas peças com estoque > 0
            - limit (int): Máximo de resultados (padrão: 20)
            - offset (int): Paginação (padrão: 0)
            - formato (str): 'markdown' ou 'json'

    Returns:
        str: Lista paginada de peças com metadados (total, has_more, next_offset).

    Exemplos:
        - "Listar peças de Freios" → categoria="Freios"
        - "Peças Bosch em estoque" → marca="Bosch", apenas_em_estoque=True
    """
    try:
        registros = _get_all_records()
        filtrados = _filtrar_registros(
            registros, categoria=params.categoria,
            marca=params.marca, apenas_em_estoque=params.apenas_em_estoque,
        )
        if not filtrados:
            return "Nenhuma peça encontrada com os filtros aplicados."

        pagina = _paginar(filtrados, params.limit, params.offset)

        if params.formato == FormatoResposta.JSON:
            return json.dumps(pagina, ensure_ascii=False, indent=2)

        titulo = "# Catálogo AutoPeças"
        if params.categoria:
            titulo += f" — {params.categoria}"
        if params.marca:
            titulo += f" | {params.marca}"

        linhas = [titulo, f"Total: **{pagina['total']}** peças (exibindo {pagina['count']})", ""]
        for r in pagina["items"]:
            linhas += [_formatar_peca_markdown(r), ""]
        if pagina["has_more"]:
            linhas.append(f"_Use `offset={pagina['next_offset']}` para ver mais resultados._")
        return "\n".join(linhas)

    except Exception as e:
        return _handle_error(e, SHEET_NAME)


@mcp.tool(
    name="autopecas_obter_detalhes",
    annotations={"title": "Obter Detalhes Completos de uma Peça",
                 "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def autopecas_obter_detalhes(params: ObterDetalhesPecaInput) -> str:
    """Retorna todos os dados de uma peça específica pelo código único.

    Args:
        params (ObterDetalhesPecaInput):
            - codigo (str): Código único da peça (ex: 'F-1023')
            - formato (str): 'markdown' ou 'json'

    Returns:
        str: Dados completos da peça (preço, estoque, fornecedor, localização, descrição).

    Exemplos:
        - "Detalhes de F-1023" → codigo="F-1023"
    """
    try:
        registros = _get_all_records()
        codigo_norm = _normalizar(params.codigo)
        encontrado = next(
            (r for r in registros if _normalizar(str(r.get(COL_CODIGO, ""))) == codigo_norm), None
        )
        if not encontrado:
            return (
                f"Peça '{params.codigo}' não encontrada. "
                "Use `autopecas_buscar_peca` para localizar pelo nome ou descrição."
            )
        if params.formato == FormatoResposta.JSON:
            return json.dumps(encontrado, ensure_ascii=False, indent=2)
        return f"# Detalhes da Peça\n\n{_formatar_peca_markdown(encontrado)}"

    except Exception as e:
        return _handle_error(e, SHEET_NAME)


@mcp.tool(
    name="autopecas_listar_categorias",
    annotations={"title": "Listar Categorias de Peças",
                 "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def autopecas_listar_categorias(params: ListarCategoriasInput) -> str:
    """Lista todas as categorias de peças com contagem total e em estoque por categoria.

    Args:
        params (ListarCategoriasInput):
            - formato (str): 'markdown' ou 'json'

    Returns:
        str: Tabela de categorias com total de peças e quantidade em estoque.
    """
    try:
        registros = _get_all_records()
        categorias: dict[str, dict[str, int]] = {}
        for r in registros:
            cat = str(r.get(COL_CATEGORIA, "Sem Categoria")).strip() or "Sem Categoria"
            if cat not in categorias:
                categorias[cat] = {"total": 0, "em_estoque": 0}
            categorias[cat]["total"] += 1
            if _estoque_disponivel(r):
                categorias[cat]["em_estoque"] += 1

        ordenadas = sorted(categorias.items(), key=lambda x: x[1]["total"], reverse=True)

        if params.formato == FormatoResposta.JSON:
            resultado = [{"categoria": c, **info} for c, info in ordenadas]
            return json.dumps({"categorias": resultado, "total": len(resultado)}, ensure_ascii=False, indent=2)

        linhas = ["# Categorias AutoPeças", f"Total de **{len(ordenadas)}** categorias\n",
                  "| Categoria | Total | Em Estoque |", "|-----------|-------|------------|"]
        for cat, info in ordenadas:
            linhas.append(f"| {cat} | {info['total']} | {info['em_estoque']} |")
        return "\n".join(linhas)

    except Exception as e:
        return _handle_error(e, SHEET_NAME)


@mcp.tool(
    name="autopecas_verificar_estoque",
    annotations={"title": "Verificar Estoque de Peças",
                 "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def autopecas_verificar_estoque(params: VerificarEstoqueInput) -> str:
    """Verifica estoque de peças específicas ou exibe resumo geral por categoria.

    Se `codigos` for informado, retorna estoque de cada peça listada (com alertas).
    Se omitido, retorna resumo agrupado por categoria.

    Args:
        params (VerificarEstoqueInput):
            - codigos (Optional[List[str]]): Códigos das peças a verificar
            - categoria (Optional[str]): Filtrar resumo por categoria
            - formato (str): 'markdown' ou 'json'

    Returns:
        str: Estoque por peça (com alertas ⚠️) ou resumo por categoria.

    Exemplos:
        - "Estoque de F-1023 e BP-0042" → codigos=["F-1023", "BP-0042"]
        - "Resumo do estoque de Freios" → categoria="Freios"
    """
    try:
        registros = _get_all_records()

        if params.codigos:
            resultados, nao_encontrados = [], []
            for cod in params.codigos:
                cod_norm = _normalizar(cod)
                r = next((rec for rec in registros if _normalizar(str(rec.get(COL_CODIGO, ""))) == cod_norm), None)
                (resultados if r else nao_encontrados).append(r if r else cod)

            if params.formato == FormatoResposta.JSON:
                return json.dumps(
                    {"pecas": resultados, "nao_encontrados": nao_encontrados}, ensure_ascii=False, indent=2
                )

            linhas = ["# Verificação de Estoque\n"]
            for r in resultados:
                try:
                    qtd = int(str(r.get(COL_ESTOQUE, "0")).replace(",", "").strip() or "0")
                except ValueError:
                    qtd = 0
                alerta = " ⚠️ **SEM ESTOQUE**" if qtd == 0 else (" ⚠️ Estoque baixo" if qtd <= 5 else "")
                linhas.append(f"- `{r.get(COL_CODIGO)}` — {r.get(COL_NOME)} | **{qtd} un.**{alerta}")
            if nao_encontrados:
                linhas.append(f"\n_Não encontrados: {', '.join(nao_encontrados)}_")
            return "\n".join(linhas)

        filtrados = _filtrar_registros(registros, categoria=params.categoria)
        resumo: dict[str, dict[str, int]] = {}
        for r in filtrados:
            cat = str(r.get(COL_CATEGORIA, "Sem Categoria")).strip() or "Sem Categoria"
            if cat not in resumo:
                resumo[cat] = {"total": 0, "sem_estoque": 0, "estoque_baixo": 0, "qtd_total": 0}
            resumo[cat]["total"] += 1
            try:
                qtd = int(str(r.get(COL_ESTOQUE, "0")).replace(",", "").strip() or "0")
            except ValueError:
                qtd = 0
            resumo[cat]["qtd_total"] += qtd
            if qtd == 0:
                resumo[cat]["sem_estoque"] += 1
            elif qtd <= 5:
                resumo[cat]["estoque_baixo"] += 1

        if params.formato == FormatoResposta.JSON:
            return json.dumps({"resumo_por_categoria": resumo}, ensure_ascii=False, indent=2)

        titulo = "# Resumo de Estoque" + (f" — {params.categoria}" if params.categoria else "")
        linhas = [titulo, "",
                  "| Categoria | Peças | Qtd. Total | Sem Estoque | Estoque Baixo |",
                  "|-----------|-------|-----------|-------------|---------------|"]
        for cat, info in sorted(resumo.items()):
            linhas.append(f"| {cat} | {info['total']} | {info['qtd_total']} | {info['sem_estoque']} | {info['estoque_baixo']} |")
        return "\n".join(linhas)

    except Exception as e:
        return _handle_error(e, SHEET_NAME)


@mcp.tool(
    name="autopecas_listar_marcas",
    annotations={"title": "Listar Marcas/Fabricantes de Peças",
                 "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def autopecas_listar_marcas(params: ListarMarcasInput) -> str:
    """Lista todas as marcas/fabricantes com contagem de peças por marca.

    Args:
        params (ListarMarcasInput):
            - categoria (Optional[str]): Filtrar por categoria
            - formato (str): 'markdown' ou 'json'

    Returns:
        str: Lista de marcas ordenadas por quantidade de peças.

    Exemplos:
        - "Marcas de freios" → categoria="Freios"
        - "Listar todos os fabricantes" → (sem parâmetros)
    """
    try:
        registros = _get_all_records()
        filtrados = _filtrar_registros(registros, categoria=params.categoria)
        marcas: dict[str, int] = {}
        for r in filtrados:
            marca = str(r.get(COL_MARCA, "Sem Marca")).strip() or "Sem Marca"
            marcas[marca] = marcas.get(marca, 0) + 1

        ordenadas = sorted(marcas.items(), key=lambda x: x[1], reverse=True)

        if params.formato == FormatoResposta.JSON:
            return json.dumps({"marcas": [{"marca": m, "total": t} for m, t in ordenadas],
                               "total": len(ordenadas)}, ensure_ascii=False, indent=2)

        titulo = "# Marcas / Fabricantes" + (f" — {params.categoria}" if params.categoria else "")
        linhas = [titulo, f"Total: **{len(ordenadas)}** marcas\n"]
        for marca, total in ordenadas:
            linhas.append(f"- **{marca}**: {total} peça(s)")
        return "\n".join(linhas)

    except Exception as e:
        return _handle_error(e, SHEET_NAME)


# ===========================================================================
# FERRAMENTAS — Leitos Hospitalares
# ===========================================================================


@mcp.tool(
    name="leitos_listar_leitos",
    annotations={"title": "Listar Todos os Leitos Hospitalares",
                 "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def leitos_listar_leitos(params: ListarLeitosInput) -> str:
    """Lista todos os leitos hospitalares com suporte a paginação e filtros combinados.

    Acesso equivalente ao Agent Diretoria do N8N: visão completa de todos os leitos,
    sem restrição de tipo de quarto. Filtrável por tipo, status e setor.

    Args:
        params (ListarLeitosInput):
            - tipo_quarto (Optional[str]): 'Enfermaria', 'UTI', 'Apartamento', 'Semi-Intensivo'
            - status (Optional[str]): 'Disponível', 'Ocupado', 'Limpeza', 'Manutenção', 'Reservado'
            - setor (Optional[str]): Setor/ala hospitalar (ex: 'Cardiologia')
            - limit (int): Máximo de resultados (padrão: 20)
            - offset (int): Paginação (padrão: 0)
            - formato (str): 'markdown' ou 'json'

    Returns:
        str: Lista paginada de leitos com status, tipo, setor e paciente atual.

    Exemplos:
        - "Todos os leitos de UTI" → tipo_quarto="UTI"
        - "Leitos em limpeza" → status="Limpeza"
        - "Leitos da Cardiologia" → setor="Cardiologia"
    """
    try:
        registros = _get_leitos_records()
        filtrados = _filtrar_leitos(registros, tipo_quarto=params.tipo_quarto,
                                    status=params.status, setor=params.setor)
        if not filtrados:
            return "Nenhum leito encontrado com os filtros aplicados."

        pagina = _paginar(filtrados, params.limit, params.offset)

        if params.formato == FormatoResposta.JSON:
            return json.dumps(pagina, ensure_ascii=False, indent=2)

        titulo = "# Leitos Hospitalares"
        if params.tipo_quarto:
            titulo += f" — {params.tipo_quarto}"
        if params.status:
            titulo += f" | {params.status}"

        linhas = [titulo, f"Total: **{pagina['total']}** leitos (exibindo {pagina['count']})", ""]
        for r in pagina["items"]:
            linhas += [_formatar_leito_markdown(r), ""]
        if pagina["has_more"]:
            linhas.append(f"_Use `offset={pagina['next_offset']}` para ver mais resultados._")
        return "\n".join(linhas)

    except Exception as e:
        return _handle_error(e, LEITOS_SHEET_NAME)


@mcp.tool(
    name="leitos_listar_enfermaria",
    annotations={"title": "Listar Leitos de Enfermaria",
                 "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def leitos_listar_enfermaria(params: ListarEnfermariaInput) -> str:
    """Lista apenas os leitos do tipo Enfermaria, com filtros opcionais de status e setor.

    Equivale ao Agent Enfermaria do N8N, que acessa a planilha com filtro fixo
    `Tipo_Quarto = 'Enfermaria'`. Ideal para o corpo de enfermagem gerenciar
    disponibilidade e limpeza de leitos de enfermaria.

    Args:
        params (ListarEnfermariaInput):
            - status (Optional[str]): 'Disponível', 'Ocupado', 'Limpeza', 'Manutenção'
            - setor (Optional[str]): Setor/ala hospitalar
            - limit (int): Máximo de resultados (padrão: 20)
            - offset (int): Paginação (padrão: 0)
            - formato (str): 'markdown' ou 'json'

    Returns:
        str: Lista paginada de leitos de enfermaria.

    Exemplos:
        - "Leitos de enfermaria disponíveis" → status="Disponível"
        - "Enfermaria em limpeza" → status="Limpeza"
        - "Enfermaria da Ortopedia" → setor="Ortopedia"
    """
    try:
        registros = _get_leitos_records()
        # Filtro fixo: apenas Enfermaria (equivalente ao Agent Enfermaria do N8N)
        filtrados = _filtrar_leitos(registros, tipo_quarto="Enfermaria",
                                    status=params.status, setor=params.setor)
        if not filtrados:
            return "Nenhum leito de enfermaria encontrado com os filtros aplicados."

        pagina = _paginar(filtrados, params.limit, params.offset)

        if params.formato == FormatoResposta.JSON:
            return json.dumps(pagina, ensure_ascii=False, indent=2)

        titulo = "# Leitos — Enfermaria"
        if params.status:
            titulo += f" | {params.status}"

        linhas = [titulo, f"Total: **{pagina['total']}** leitos (exibindo {pagina['count']})", ""]
        for r in pagina["items"]:
            linhas += [_formatar_leito_markdown(r), ""]
        if pagina["has_more"]:
            linhas.append(f"_Use `offset={pagina['next_offset']}` para ver mais resultados._")
        return "\n".join(linhas)

    except Exception as e:
        return _handle_error(e, LEITOS_SHEET_NAME)


@mcp.tool(
    name="leitos_verificar_disponibilidade",
    annotations={"title": "Verificar Disponibilidade de Leitos",
                 "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def leitos_verificar_disponibilidade(params: VerificarDisponibilidadeInput) -> str:
    """Exibe leitos com status 'Disponível', filtrável por tipo de quarto e setor.

    Ferramenta central para admissão de pacientes: identifica quais leitos estão
    livres para ocupação imediata, com resumo por tipo de quarto.

    Args:
        params (VerificarDisponibilidadeInput):
            - tipo_quarto (Optional[str]): Filtrar por tipo (ex: 'UTI', 'Apartamento')
            - setor (Optional[str]): Filtrar por setor/ala
            - formato (str): 'markdown' ou 'json'

    Returns:
        str: Leitos disponíveis com resumo por tipo de quarto e contagem total.

    Exemplos:
        - "Leitos de UTI disponíveis" → tipo_quarto="UTI"
        - "Leitos disponíveis na Cardiologia" → setor="Cardiologia"
        - "Quantos leitos livres temos?" → (sem parâmetros)
    """
    try:
        registros = _get_leitos_records()
        disponiveis = _filtrar_leitos(registros, tipo_quarto=params.tipo_quarto,
                                      status="Disponível", setor=params.setor)

        if not disponiveis:
            msg = "Nenhum leito disponível"
            if params.tipo_quarto:
                msg += f" do tipo '{params.tipo_quarto}'"
            if params.setor:
                msg += f" no setor '{params.setor}'"
            return msg + " no momento."

        # Agrupa por tipo de quarto para o resumo
        por_tipo: dict[str, list] = {}
        for r in disponiveis:
            tq = str(r.get(LEITOS_COL_TIPO_QUARTO, "N/A"))
            por_tipo.setdefault(tq, []).append(r)

        if params.formato == FormatoResposta.JSON:
            return json.dumps(
                {"total_disponivel": len(disponiveis),
                 "por_tipo": {tq: len(lst) for tq, lst in por_tipo.items()},
                 "leitos": disponiveis},
                ensure_ascii=False, indent=2,
            )

        titulo = "# 🟢 Leitos Disponíveis"
        if params.tipo_quarto:
            titulo += f" — {params.tipo_quarto}"

        linhas = [titulo, f"**{len(disponiveis)}** leito(s) disponível(is)\n"]
        for tq, leitos in sorted(por_tipo.items()):
            linhas.append(f"## {tq} ({len(leitos)})")
            for r in leitos:
                leito_id = r.get(LEITOS_COL_LEITO, "N/A")
                setor = r.get(LEITOS_COL_SETOR, "N/A")
                linhas.append(f"- `{leito_id}` — {setor}")
            linhas.append("")
        return "\n".join(linhas)

    except Exception as e:
        return _handle_error(e, LEITOS_SHEET_NAME)


@mcp.tool(
    name="leitos_obter_detalhes_leito",
    annotations={"title": "Obter Detalhes Completos de um Leito",
                 "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def leitos_obter_detalhes_leito(params: ObterDetalhesLeitoInput) -> str:
    """Retorna todos os dados de um leito específico pelo seu identificador.

    Args:
        params (ObterDetalhesLeitoInput):
            - leito_id (str): Identificador do leito (ex: 'A-101', 'UTI-05')
            - formato (str): 'markdown' ou 'json'

    Returns:
        str: Dados completos do leito: tipo, status, paciente, médico, datas e observações.

    Exemplos:
        - "Detalhes do leito A-101" → leito_id="A-101"
        - "Quem está no UTI-05?" → leito_id="UTI-05"
    """
    try:
        registros = _get_leitos_records()
        leito_norm = _normalizar(params.leito_id)
        encontrado = next(
            (r for r in registros if _normalizar(str(r.get(LEITOS_COL_LEITO, ""))) == leito_norm), None
        )
        if not encontrado:
            return (
                f"Leito '{params.leito_id}' não encontrado. "
                "Use `leitos_listar_leitos` para ver os identificadores disponíveis."
            )
        if params.formato == FormatoResposta.JSON:
            return json.dumps(encontrado, ensure_ascii=False, indent=2)
        return f"# Detalhes do Leito\n\n{_formatar_leito_markdown(encontrado)}"

    except Exception as e:
        return _handle_error(e, LEITOS_SHEET_NAME)


@mcp.tool(
    name="leitos_resumo_ocupacao",
    annotations={"title": "Resumo de Ocupação Hospitalar",
                 "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def leitos_resumo_ocupacao(params: ResumoOcupacaoInput) -> str:
    """Exibe dashboard de ocupação hospitalar agrupado por tipo de quarto e status.

    Equivale à visão de gestão do Agent Diretoria: panorama completo da ocupação
    com totais por categoria de status (Disponível, Ocupado, Limpeza, Manutenção).

    Args:
        params (ResumoOcupacaoInput):
            - setor (Optional[str]): Filtrar resumo por setor/ala
            - formato (str): 'markdown' ou 'json'

    Returns:
        str: Tabela de ocupação por tipo de quarto com contagens por status e taxa de ocupação.

    Exemplos:
        - "Dashboard de ocupação" → (sem parâmetros)
        - "Ocupação da UTI" → setor="UTI" (ou use leitos_listar_leitos com tipo_quarto)
    """
    try:
        registros = _get_leitos_records()
        filtrados = _filtrar_leitos(registros, setor=params.setor)

        # Agrupamento por tipo_quarto → contagem por status
        resumo: dict[str, dict[str, int]] = {}
        for r in filtrados:
            tq = str(r.get(LEITOS_COL_TIPO_QUARTO, "N/A")).strip() or "N/A"
            st = str(r.get(LEITOS_COL_STATUS, "N/A")).strip() or "N/A"
            if tq not in resumo:
                resumo[tq] = {"Total": 0, "Disponível": 0, "Ocupado": 0,
                               "Limpeza": 0, "Manutenção": 0, "Reservado": 0, "Outros": 0}
            resumo[tq]["Total"] += 1
            chave = st if st in resumo[tq] else "Outros"
            resumo[tq][chave] += 1

        if not resumo:
            return "Nenhum dado de leitos encontrado."

        if params.formato == FormatoResposta.JSON:
            return json.dumps({"resumo_por_tipo": resumo}, ensure_ascii=False, indent=2)

        total_geral = sum(v["Total"] for v in resumo.values())
        total_ocupado = sum(v["Ocupado"] for v in resumo.values())
        taxa_geral = round(total_ocupado / total_geral * 100, 1) if total_geral else 0

        titulo = "# 🏥 Dashboard de Ocupação Hospitalar"
        if params.setor:
            titulo += f" — {params.setor}"

        linhas = [
            titulo,
            f"**{total_ocupado}/{total_geral}** leitos ocupados — Taxa: **{taxa_geral}%**\n",
            "| Tipo de Quarto | Total | Disponível | Ocupado | Limpeza | Manutenção | Reservado |",
            "|----------------|-------|------------|---------|---------|------------|-----------|",
        ]
        for tq, info in sorted(resumo.items()):
            taxa = round(info["Ocupado"] / info["Total"] * 100, 0) if info["Total"] else 0
            linhas.append(
                f"| {tq} ({taxa:.0f}%) | {info['Total']} | {info['Disponível']} | "
                f"{info['Ocupado']} | {info['Limpeza']} | {info['Manutenção']} | {info['Reservado']} |"
            )
        return "\n".join(linhas)

    except Exception as e:
        return _handle_error(e, LEITOS_SHEET_NAME)


@mcp.tool(
    name="leitos_enviar_notificacao",
    annotations={"title": "Enviar Notificação por E-mail",
                 "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def leitos_enviar_notificacao(params: EnviarNotificacaoInput) -> str:
    """Envia uma notificação por e-mail via Gmail SMTP.

    Equivale à ferramenta 'Enviar' (gmailTool) do Agent Diretoria no N8N.
    Usada para alertar equipes sobre mudanças de status de leitos, disponibilidade
    para internação, solicitações de limpeza ou manutenção.

    Requer GMAIL_USER e GMAIL_APP_PASSWORD configurados no .env.
    Para criar uma Senha de App: myaccount.google.com/apppasswords

    Args:
        params (EnviarNotificacaoInput):
            - destinatario (str): E-mail do destinatário
            - assunto (str): Assunto do e-mail
            - mensagem (str): Corpo da mensagem (texto simples)

    Returns:
        str: Confirmação de envio ou mensagem de erro detalhada.

    Exemplos:
        - "Avisar enfermagem que leito A-101 está disponível"
          → destinatario="enfermagem@hospital.com.br",
            assunto="Leito A-101 disponível",
            mensagem="O leito A-101 foi liberado e está pronto para novo paciente."
    """
    try:
        _enviar_email_gmail(params.destinatario, params.assunto, params.mensagem)
        logger.info("E-mail enviado para %s | assunto: %s", params.destinatario, params.assunto)
        return (
            f"✅ E-mail enviado com sucesso para **{params.destinatario}**\n"
            f"- **Assunto**: {params.assunto}"
        )
    except EnvironmentError as e:
        return f"Erro de configuração: {e}"
    except smtplib.SMTPAuthenticationError:
        return (
            "Erro de autenticação SMTP. Verifique GMAIL_USER e GMAIL_APP_PASSWORD no .env. "
            "Certifique-se de usar uma Senha de App (não a senha comum do Gmail)."
        )
    except smtplib.SMTPException as e:
        return f"Erro ao enviar e-mail via SMTP: {e}"
    except Exception as e:
        return f"Erro inesperado ao enviar e-mail ({type(e).__name__}): {e}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
