#!/usr/bin/env python3
"""
MCP Server - AutoMax & Leitos Hospitalares

Servidor MCP com dois domínios de dados via Google Sheets (mesma planilha, abas distintas):

  • AutoPeças (AutoMax) — aba configurada em AUTOPECAS_SHEET_NAME
      Ferramentas: busca, listagem, detalhes, categorias, estoque, marcas.

  • Leitos Hospitalares — aba configurada em LEITOS_SHEET_NAME
      Ferramentas: listagem geral, enfermaria, UTI, disponibilidade, detalhes,
      resumo de ocupação, atualização de status de limpeza (escrita),
      e envio de notificações por e-mail (Gmail SMTP) e SMS (Twilio).

      Espelha os três agentes N8N definidos em mcp-all-nodes.json:
        - Agent Diretoria  → acesso total + e-mail + SMS
        - Agent Enfermaria → Tipo_Quarto = Enfermaria + e-mail + SMS
        - Agent UTI        → Tipo_Quarto = UTI + e-mail + SMS

Configuração via .env:
    SPREADSHEET_ID          - ID da planilha Google Sheets compartilhada
    AUTOPECAS_SHEET_NAME    - Aba de autopeças    (padrão: "AutoPeças")
    LEITOS_SHEET_NAME       - Aba de leitos       (padrão: "Leitos")
    GOOGLE_CREDENTIALS_PATH - Caminho para o JSON da Service Account
    GOOGLE_CREDENTIALS_JSON - JSON da Service Account como string (alternativa)
    GMAIL_USER              - E-mail remetente para notificações
    GMAIL_APP_PASSWORD      - Senha de App do Gmail (SMTP)
    TWILIO_ACCOUNT_SID      - SID da conta Twilio
    TWILIO_AUTH_TOKEN       - Auth Token da conta Twilio
    TWILIO_FROM_NUMBER      - Número remetente Twilio (ex: '+18647139932')

Nota: como o servidor inclui operações de escrita na aba Leitos
(atualização de Status_Limpeza), a Service Account deve ter permissão
de EDITOR na planilha (não apenas leitor).
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
import httpx
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

# Escopo com leitura E escrita em Sheets (necessário para atualizar Status_Limpeza)
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

SPREADSHEET_ID: str = os.getenv("SPREADSHEET_ID", "")

# --- AutoPeças ---
AUTOPECAS_SHEET_NAME: str = os.getenv("AUTOPECAS_SHEET_NAME", "AutoPeças")
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
# Nomes confirmados pelo schema do nó "Atualizar Base de Dados Hospital" (mcp-all-nodes.json)
LEITOS_SHEET_NAME: str = os.getenv("LEITOS_SHEET_NAME", "Leitos")
LEITOS_COL_LEITO: str = os.getenv("LEITOS_COL_LEITO", "ID_Leito")
LEITOS_COL_QUARTO: str = os.getenv("LEITOS_COL_QUARTO", "Quarto")
LEITOS_COL_TIPO_QUARTO: str = os.getenv("LEITOS_COL_TIPO_QUARTO", "Tipo_Quarto")
LEITOS_COL_STATUS_OCUPACAO: str = os.getenv("LEITOS_COL_STATUS_OCUPACAO", "Status_Ocupacao")
LEITOS_COL_STATUS_LIMPEZA: str = os.getenv("LEITOS_COL_STATUS_LIMPEZA", "Status_Limpeza")
LEITOS_COL_PACIENTE: str = os.getenv("LEITOS_COL_PACIENTE", "Paciente")
LEITOS_COL_ULTIMA_LIMPEZA: str = os.getenv("LEITOS_COL_ULTIMA_LIMPEZA", "Ultima_Limpeza")
# Colunas opcionais — podem existir na planilha real mas não estão no schema do update
LEITOS_COL_DATA_INTERNACAO: str = os.getenv("LEITOS_COL_DATA_INTERNACAO", "Data_Internacao")
LEITOS_COL_PREVISAO_ALTA: str = os.getenv("LEITOS_COL_PREVISAO_ALTA", "Previsao_Alta")
LEITOS_COL_MEDICO: str = os.getenv("LEITOS_COL_MEDICO", "Medico")
LEITOS_COL_OBSERVACOES: str = os.getenv("LEITOS_COL_OBSERVACOES", "Observacoes")

# --- Gmail SMTP ---
GMAIL_USER: str = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD: str = os.getenv("GMAIL_APP_PASSWORD", "")

# --- Twilio SMS ---
TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER: str = os.getenv("TWILIO_FROM_NUMBER", "")

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
    return _get_worksheet(AUTOPECAS_SHEET_NAME)


def _get_leitos_sheet() -> gspread.Worksheet:
    """Worksheet da aba Leitos (leitura e escrita)."""
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
    aba = sheet_name or AUTOPECAS_SHEET_NAME
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
    status_ocupacao: Optional[str] = None,
    status_limpeza: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Aplica filtros combinados sobre registros de leitos."""
    resultado = registros

    if tipo_quarto:
        tq = _normalizar(tipo_quarto)
        resultado = [r for r in resultado if _normalizar(str(r.get(LEITOS_COL_TIPO_QUARTO, ""))) == tq]
    if status_ocupacao:
        st = _normalizar(status_ocupacao)
        resultado = [r for r in resultado if _normalizar(str(r.get(LEITOS_COL_STATUS_OCUPACAO, ""))) == st]
    if status_limpeza:
        sl = _normalizar(status_limpeza)
        resultado = [r for r in resultado if _normalizar(str(r.get(LEITOS_COL_STATUS_LIMPEZA, ""))) == sl]

    return resultado


def _formatar_leito_markdown(r: dict[str, Any]) -> str:
    """Formata um registro de leito como Markdown."""
    status_ocup = str(r.get(LEITOS_COL_STATUS_OCUPACAO, "N/A"))
    emoji = {
        "disponivel": "🟢", "ocupado": "🔴", "reservado": "🔵",
    }.get(_normalizar(status_ocup), "⚪")

    status_limp = str(r.get(LEITOS_COL_STATUS_LIMPEZA, ""))
    emoji_limp = {"concluido": "✅", "pendente": "⚠️", "em andamento": "🔄"}.get(
        _normalizar(status_limp), ""
    )

    linhas = [
        f"### {emoji} `{r.get(LEITOS_COL_LEITO, 'N/A')}` — {r.get(LEITOS_COL_QUARTO, 'N/A')}",
        f"- **Tipo de Quarto**: {r.get(LEITOS_COL_TIPO_QUARTO, 'N/A')}",
        f"- **Ocupação**: {status_ocup}",
        f"- **Limpeza**: {emoji_limp} {status_limp}" if status_limp else "- **Limpeza**: N/A",
    ]
    if r.get(LEITOS_COL_ULTIMA_LIMPEZA):
        linhas.append(f"- **Última Limpeza**: {r[LEITOS_COL_ULTIMA_LIMPEZA]}")
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


# ---------------------------------------------------------------------------
# Utilitários — Notificações
# ---------------------------------------------------------------------------


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


async def _enviar_sms_twilio(destinatario: str, mensagem: str) -> None:
    """Envia SMS via Twilio REST API usando httpx (sem SDK Twilio)."""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_FROM_NUMBER:
        raise EnvironmentError(
            "Configure TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN e TWILIO_FROM_NUMBER no .env."
        )
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={"From": TWILIO_FROM_NUMBER, "To": destinatario, "Body": mensagem},
            timeout=30.0,
        )
        response.raise_for_status()


# ---------------------------------------------------------------------------
# Enums e modelos de entrada — compartilhados
# ---------------------------------------------------------------------------


class FormatoResposta(str, Enum):
    """Formato de saída das ferramentas."""
    MARKDOWN = "markdown"
    JSON = "json"


# ---------------------------------------------------------------------------
# Modelos de entrada — AutoPeças
# ---------------------------------------------------------------------------


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
    status_ocupacao: Optional[str] = Field(
        default=None,
        description="Filtrar por Status_Ocupacao (ex: 'Disponível', 'Ocupado', 'Reservado')",
        max_length=50,
    )
    status_limpeza: Optional[str] = Field(
        default=None,
        description="Filtrar por Status_Limpeza (ex: 'Concluído', 'Pendente', 'Em Andamento')",
        max_length=50,
    )
    limit: int = Field(default=20, description="Máximo de resultados por página (1–100)", ge=1, le=100)
    offset: int = Field(default=0, description="Deslocamento para paginação", ge=0)
    formato: FormatoResposta = Field(default=FormatoResposta.MARKDOWN, description="'markdown' ou 'json'")


class ListarEnfermariaInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    status_ocupacao: Optional[str] = Field(
        default=None,
        description="Filtrar por Status_Ocupacao (ex: 'Disponível', 'Ocupado')",
        max_length=50,
    )
    status_limpeza: Optional[str] = Field(
        default=None,
        description="Filtrar por Status_Limpeza (ex: 'Concluído', 'Pendente')",
        max_length=50,
    )
    limit: int = Field(default=20, description="Máximo de resultados por página (1–100)", ge=1, le=100)
    offset: int = Field(default=0, description="Deslocamento para paginação", ge=0)
    formato: FormatoResposta = Field(default=FormatoResposta.MARKDOWN, description="'markdown' ou 'json'")


class ListarUTIInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    status_ocupacao: Optional[str] = Field(
        default=None,
        description="Filtrar por Status_Ocupacao (ex: 'Disponível', 'Ocupado')",
        max_length=50,
    )
    status_limpeza: Optional[str] = Field(
        default=None,
        description="Filtrar por Status_Limpeza (ex: 'Concluído', 'Pendente')",
        max_length=50,
    )
    limit: int = Field(default=20, description="Máximo de resultados por página (1–100)", ge=1, le=100)
    offset: int = Field(default=0, description="Deslocamento para paginação", ge=0)
    formato: FormatoResposta = Field(default=FormatoResposta.MARKDOWN, description="'markdown' ou 'json'")


class ObterDetalhesLeitoInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    leito_id: str = Field(
        ...,
        description="Identificador do leito — campo ID_Leito (ex: 'A-101', 'UTI-05')",
        min_length=1,
        max_length=50,
    )
    formato: FormatoResposta = Field(default=FormatoResposta.MARKDOWN, description="'markdown' ou 'json'")


class VerificarDisponibilidadeInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    tipo_quarto: Optional[str] = Field(
        default=None,
        description="Filtrar disponibilidade por tipo de quarto (ex: 'UTI', 'Enfermaria', 'Apartamento')",
        max_length=100,
    )
    formato: FormatoResposta = Field(default=FormatoResposta.MARKDOWN, description="'markdown' ou 'json'")


class ResumoOcupacaoInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    tipo_quarto: Optional[str] = Field(
        default=None,
        description="Filtrar resumo por tipo de quarto",
        max_length=100,
    )
    formato: FormatoResposta = Field(default=FormatoResposta.MARKDOWN, description="'markdown' ou 'json'")


class AtualizarStatusLimpezaInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    leito_id: str = Field(
        ...,
        description="ID do leito a atualizar — campo ID_Leito (ex: 'A-101', 'UTI-05')",
        min_length=1,
        max_length=50,
    )
    status_limpeza: str = Field(
        ...,
        description="Novo valor para Status_Limpeza (ex: 'Concluído', 'Pendente', 'Em Andamento')",
        min_length=1,
        max_length=50,
    )


class EnviarNotificacaoInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    destinatario: str = Field(
        ..., description="E-mail do destinatário (ex: 'enfermeira@hospital.com.br')",
        min_length=5, max_length=200,
    )
    assunto: str = Field(..., description="Assunto do e-mail", min_length=1, max_length=200)
    mensagem: str = Field(..., description="Corpo da mensagem em texto simples", min_length=1, max_length=5000)

    @field_validator("destinatario")
    @classmethod
    def email_valido(cls, v: str) -> str:
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("E-mail do destinatário inválido.")
        return v.lower()


class EnviarSMSInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    destinatario: str = Field(
        ...,
        description="Número de telefone do destinatário no formato E.164 (ex: '+5511952767064')",
        min_length=8,
        max_length=20,
    )
    mensagem: str = Field(
        ...,
        description="Texto do SMS (máximo 160 caracteres para SMS simples)",
        min_length=1,
        max_length=1600,
    )

    @field_validator("destinatario")
    @classmethod
    def numero_valido(cls, v: str) -> str:
        if not v.startswith("+"):
            raise ValueError("Número deve estar no formato E.164, começando com '+' (ex: '+5511999999999').")
        return v


# ---------------------------------------------------------------------------
# Servidor MCP
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "autopecas_leitos_mcp",
    instructions=(
        "Você é um assistente integrado com duas bases de dados via Google Sheets:\n"
        "1. AutoPeças (AutoMax): consulte peças, estoque, categorias e marcas.\n"
        "2. Leitos Hospitalares: gerencie leitos por tipo (Enfermaria, UTI, Apartamento), "
        "verifique disponibilidade, acompanhe status de ocupação e limpeza, "
        "atualize Status_Limpeza e envie notificações por e-mail ou SMS."
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

        linhas = [f"# Resultados para '{params.query}'",
                  f"Encontradas **{pagina['total']}** peças (exibindo {pagina['count']})", ""]
        for r in pagina["items"]:
            linhas += [_formatar_peca_markdown(r), ""]
        if pagina["has_more"]:
            linhas.append(f"_Use `offset={pagina['next_offset']}` para ver mais resultados._")
        return "\n".join(linhas)

    except Exception as e:
        return _handle_error(e, AUTOPECAS_SHEET_NAME)


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
        return _handle_error(e, AUTOPECAS_SHEET_NAME)


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
            return (f"Peça '{params.codigo}' não encontrada. "
                    "Use `autopecas_buscar_peca` para localizar pelo nome ou descrição.")
        if params.formato == FormatoResposta.JSON:
            return json.dumps(encontrado, ensure_ascii=False, indent=2)
        return f"# Detalhes da Peça\n\n{_formatar_peca_markdown(encontrado)}"

    except Exception as e:
        return _handle_error(e, AUTOPECAS_SHEET_NAME)


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
        return _handle_error(e, AUTOPECAS_SHEET_NAME)


@mcp.tool(
    name="autopecas_verificar_estoque",
    annotations={"title": "Verificar Estoque de Peças",
                 "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def autopecas_verificar_estoque(params: VerificarEstoqueInput) -> str:
    """Verifica estoque de peças específicas ou exibe resumo geral por categoria.

    Se `codigos` for informado, retorna estoque de cada peça (com alertas ⚠️).
    Se omitido, retorna resumo agrupado por categoria.

    Args:
        params (VerificarEstoqueInput):
            - codigos (Optional[List[str]]): Códigos das peças a verificar
            - categoria (Optional[str]): Filtrar resumo por categoria
            - formato (str): 'markdown' ou 'json'

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
                return json.dumps({"pecas": resultados, "nao_encontrados": nao_encontrados},
                                  ensure_ascii=False, indent=2)

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
            linhas.append(f"| {cat} | {info['total']} | {info['qtd_total']} "
                          f"| {info['sem_estoque']} | {info['estoque_baixo']} |")
        return "\n".join(linhas)

    except Exception as e:
        return _handle_error(e, AUTOPECAS_SHEET_NAME)


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
        return _handle_error(e, AUTOPECAS_SHEET_NAME)


# ===========================================================================
# FERRAMENTAS — Leitos Hospitalares
# ===========================================================================


@mcp.tool(
    name="leitos_listar_leitos",
    annotations={"title": "Listar Todos os Leitos Hospitalares",
                 "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def leitos_listar_leitos(params: ListarLeitosInput) -> str:
    """Lista todos os leitos hospitalares com paginação e filtros combinados.

    Visão completa equivalente ao Agent Diretoria do N8N — sem restrição de tipo
    de quarto. Filtrável por tipo, Status_Ocupacao e Status_Limpeza.

    Colunas da planilha: ID_Leito, Quarto, Tipo_Quarto, Status_Ocupacao,
    Status_Limpeza, Paciente, Ultima_Limpeza.

    Args:
        params (ListarLeitosInput):
            - tipo_quarto (Optional[str]): 'Enfermaria', 'UTI', 'Apartamento', 'Semi-Intensivo'
            - status_ocupacao (Optional[str]): 'Disponível', 'Ocupado', 'Reservado'
            - status_limpeza (Optional[str]): 'Concluído', 'Pendente', 'Em Andamento'
            - limit (int): Máximo de resultados (padrão: 20)
            - offset (int): Paginação (padrão: 0)
            - formato (str): 'markdown' ou 'json'

    Exemplos:
        - "Todos os leitos de UTI" → tipo_quarto="UTI"
        - "Leitos com limpeza pendente" → status_limpeza="Pendente"
        - "Leitos disponíveis" → status_ocupacao="Disponível"
    """
    try:
        registros = _get_leitos_records()
        filtrados = _filtrar_leitos(registros, tipo_quarto=params.tipo_quarto,
                                    status_ocupacao=params.status_ocupacao,
                                    status_limpeza=params.status_limpeza)
        if not filtrados:
            return "Nenhum leito encontrado com os filtros aplicados."

        pagina = _paginar(filtrados, params.limit, params.offset)
        if params.formato == FormatoResposta.JSON:
            return json.dumps(pagina, ensure_ascii=False, indent=2)

        titulo = "# Leitos Hospitalares"
        if params.tipo_quarto:
            titulo += f" — {params.tipo_quarto}"
        if params.status_ocupacao:
            titulo += f" | {params.status_ocupacao}"

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
    """Lista apenas os leitos do tipo Enfermaria (Tipo_Quarto = 'Enfermaria').

    Equivale ao Agent Enfermaria do N8N. Suporta os três relatórios do menu:
    - Leitos Disponíveis e Ocupados → status_ocupacao="Disponível" ou "Ocupado"
    - Ocupação de Leitos → (sem filtro, ver leitos_resumo_ocupacao)
    - Status de Limpeza → status_limpeza="Pendente" ou "Concluído"

    Args:
        params (ListarEnfermariaInput):
            - status_ocupacao (Optional[str]): 'Disponível', 'Ocupado', 'Reservado'
            - status_limpeza (Optional[str]): 'Concluído', 'Pendente', 'Em Andamento'
            - limit (int): Máximo de resultados (padrão: 20)
            - offset (int): Paginação (padrão: 0)
            - formato (str): 'markdown' ou 'json'

    Exemplos:
        - "Leitos de enfermaria disponíveis" → status_ocupacao="Disponível"
        - "Limpeza pendente na enfermaria" → status_limpeza="Pendente"
    """
    try:
        registros = _get_leitos_records()
        filtrados = _filtrar_leitos(registros, tipo_quarto="Enfermaria",
                                    status_ocupacao=params.status_ocupacao,
                                    status_limpeza=params.status_limpeza)
        if not filtrados:
            return "Nenhum leito de enfermaria encontrado com os filtros aplicados."

        pagina = _paginar(filtrados, params.limit, params.offset)
        if params.formato == FormatoResposta.JSON:
            return json.dumps(pagina, ensure_ascii=False, indent=2)

        titulo = "# Leitos — Enfermaria"
        if params.status_ocupacao:
            titulo += f" | {params.status_ocupacao}"
        if params.status_limpeza:
            titulo += f" | Limpeza: {params.status_limpeza}"

        linhas = [titulo, f"Total: **{pagina['total']}** leitos (exibindo {pagina['count']})", ""]
        for r in pagina["items"]:
            linhas += [_formatar_leito_markdown(r), ""]
        if pagina["has_more"]:
            linhas.append(f"_Use `offset={pagina['next_offset']}` para ver mais resultados._")
        return "\n".join(linhas)

    except Exception as e:
        return _handle_error(e, LEITOS_SHEET_NAME)


@mcp.tool(
    name="leitos_listar_uti",
    annotations={"title": "Listar Leitos de UTI",
                 "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def leitos_listar_uti(params: ListarUTIInput) -> str:
    """Lista apenas os leitos do tipo UTI (Tipo_Quarto = 'UTI').

    Equivale ao Agent UTI do N8N. Suporta os dois relatórios do menu:
    - Relatório de Dias Internados → exibe Data_Internacao de cada paciente
    - Quantidade de Pacientes Internados → contagem de leitos Ocupados

    Args:
        params (ListarUTIInput):
            - status_ocupacao (Optional[str]): 'Disponível', 'Ocupado', 'Reservado'
            - status_limpeza (Optional[str]): 'Concluído', 'Pendente', 'Em Andamento'
            - limit (int): Máximo de resultados (padrão: 20)
            - offset (int): Paginação (padrão: 0)
            - formato (str): 'markdown' ou 'json'

    Exemplos:
        - "Pacientes na UTI" → status_ocupacao="Ocupado"
        - "Leitos de UTI disponíveis" → status_ocupacao="Disponível"
        - "Dias de internação na UTI" → (sem filtros, ver Data_Internacao no resultado)
    """
    try:
        registros = _get_leitos_records()
        filtrados = _filtrar_leitos(registros, tipo_quarto="UTI",
                                    status_ocupacao=params.status_ocupacao,
                                    status_limpeza=params.status_limpeza)
        if not filtrados:
            return "Nenhum leito de UTI encontrado com os filtros aplicados."

        pagina = _paginar(filtrados, params.limit, params.offset)

        # Contagens para o cabeçalho do relatório
        total_ocupados = sum(
            1 for r in filtrados
            if _normalizar(str(r.get(LEITOS_COL_STATUS_OCUPACAO, ""))) == "ocupado"
        )
        total_disponiveis = sum(
            1 for r in filtrados
            if _normalizar(str(r.get(LEITOS_COL_STATUS_OCUPACAO, ""))) == "disponivel"
        )

        if params.formato == FormatoResposta.JSON:
            resultado = dict(pagina)
            resultado["resumo"] = {"ocupados": total_ocupados, "disponiveis": total_disponiveis}
            return json.dumps(resultado, ensure_ascii=False, indent=2)

        titulo = "# Leitos — UTI"
        if params.status_ocupacao:
            titulo += f" | {params.status_ocupacao}"

        linhas = [
            titulo,
            f"Total: **{pagina['total']}** leitos (exibindo {pagina['count']}) "
            f"| 🔴 Ocupados: **{total_ocupados}** | 🟢 Disponíveis: **{total_disponiveis}**",
            "",
        ]
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
    """Exibe leitos com Status_Ocupacao = 'Disponível', com resumo por tipo de quarto.

    Ferramenta central para admissão de pacientes: identifica quais leitos estão
    livres para ocupação imediata.

    Args:
        params (VerificarDisponibilidadeInput):
            - tipo_quarto (Optional[str]): Filtrar por tipo (ex: 'UTI', 'Enfermaria')
            - formato (str): 'markdown' ou 'json'

    Exemplos:
        - "Leitos de UTI disponíveis" → tipo_quarto="UTI"
        - "Quantos leitos livres temos?" → (sem parâmetros)
    """
    try:
        registros = _get_leitos_records()
        disponiveis = _filtrar_leitos(registros, tipo_quarto=params.tipo_quarto,
                                      status_ocupacao="Disponível")
        if not disponiveis:
            msg = "Nenhum leito disponível"
            if params.tipo_quarto:
                msg += f" do tipo '{params.tipo_quarto}'"
            return msg + " no momento."

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
                linhas.append(f"- `{r.get(LEITOS_COL_LEITO, 'N/A')}` — {r.get(LEITOS_COL_QUARTO, 'N/A')}")
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
    """Retorna todos os dados de um leito específico pelo campo ID_Leito.

    Args:
        params (ObterDetalhesLeitoInput):
            - leito_id (str): Valor do campo ID_Leito (ex: 'A-101', 'UTI-05')
            - formato (str): 'markdown' ou 'json'

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
            return (f"Leito '{params.leito_id}' não encontrado. "
                    "Use `leitos_listar_leitos` para ver os IDs disponíveis.")
        if params.formato == FormatoResposta.JSON:
            return json.dumps(encontrado, ensure_ascii=False, indent=2)
        return f"# Detalhes do Leito\n\n{_formatar_leito_markdown(encontrado)}"

    except Exception as e:
        return _handle_error(e, LEITOS_SHEET_NAME)


@mcp.tool(
    name="leitos_resumo_ocupacao",
    annotations={"title": "Resumo de Ocupação e Limpeza Hospitalar",
                 "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def leitos_resumo_ocupacao(params: ResumoOcupacaoInput) -> str:
    """Dashboard de ocupação e limpeza agrupado por Tipo_Quarto.

    Exibe dois painéis:
    1. Ocupação: contagens de Status_Ocupacao (Disponível, Ocupado, Reservado)
    2. Limpeza: contagens de Status_Limpeza (Concluído, Pendente, Em Andamento)

    Equivale à visão do Agent Diretoria e suporta os relatórios de Ocupação e
    Status de Limpeza do Agent Enfermaria.

    Args:
        params (ResumoOcupacaoInput):
            - tipo_quarto (Optional[str]): Filtrar por tipo de quarto
            - formato (str): 'markdown' ou 'json'

    Exemplos:
        - "Dashboard geral" → (sem parâmetros)
        - "Resumo da enfermaria" → tipo_quarto="Enfermaria"
    """
    try:
        registros = _get_leitos_records()
        filtrados = _filtrar_leitos(registros, tipo_quarto=params.tipo_quarto)
        if not filtrados:
            return "Nenhum dado de leitos encontrado."

        ocupacao: dict[str, dict[str, int]] = {}
        limpeza: dict[str, dict[str, int]] = {}

        for r in filtrados:
            tq = str(r.get(LEITOS_COL_TIPO_QUARTO, "N/A")).strip() or "N/A"
            st_ocup = str(r.get(LEITOS_COL_STATUS_OCUPACAO, "N/A")).strip() or "N/A"
            st_limp = str(r.get(LEITOS_COL_STATUS_LIMPEZA, "N/A")).strip() or "N/A"

            if tq not in ocupacao:
                ocupacao[tq] = {"Total": 0, "Disponível": 0, "Ocupado": 0, "Reservado": 0, "Outros": 0}
            ocupacao[tq]["Total"] += 1
            ocupacao[tq][st_ocup if st_ocup in ocupacao[tq] else "Outros"] += 1

            if tq not in limpeza:
                limpeza[tq] = {"Total": 0, "Concluído": 0, "Pendente": 0, "Em Andamento": 0, "Outros": 0}
            limpeza[tq]["Total"] += 1
            limpeza[tq][st_limp if st_limp in limpeza[tq] else "Outros"] += 1

        if params.formato == FormatoResposta.JSON:
            return json.dumps({"ocupacao_por_tipo": ocupacao, "limpeza_por_tipo": limpeza},
                              ensure_ascii=False, indent=2)

        total_geral = sum(v["Total"] for v in ocupacao.values())
        total_ocupado = sum(v["Ocupado"] for v in ocupacao.values())
        taxa = round(total_ocupado / total_geral * 100, 1) if total_geral else 0
        total_pendente = sum(v["Pendente"] for v in limpeza.values())

        titulo = "# 🏥 Dashboard Hospitalar"
        if params.tipo_quarto:
            titulo += f" — {params.tipo_quarto}"

        linhas = [
            titulo,
            f"**{total_ocupado}/{total_geral}** leitos ocupados — Taxa: **{taxa}%** "
            f"| ⚠️ Limpezas pendentes: **{total_pendente}**\n",
            "## Ocupação por Tipo de Quarto",
            "| Tipo de Quarto | Total | Disponível | Ocupado | Reservado |",
            "|----------------|-------|------------|---------|-----------|",
        ]
        for tq, info in sorted(ocupacao.items()):
            taxa_tq = round(info["Ocupado"] / info["Total"] * 100, 0) if info["Total"] else 0
            linhas.append(f"| {tq} ({taxa_tq:.0f}%) | {info['Total']} | {info['Disponível']} "
                          f"| {info['Ocupado']} | {info['Reservado']} |")

        linhas += [
            "",
            "## Status de Limpeza por Tipo de Quarto",
            "| Tipo de Quarto | Total | Concluído | Pendente | Em Andamento |",
            "|----------------|-------|-----------|----------|--------------|",
        ]
        for tq, info in sorted(limpeza.items()):
            linhas.append(f"| {tq} | {info['Total']} | {info['Concluído']} "
                          f"| {info['Pendente']} | {info['Em Andamento']} |")

        return "\n".join(linhas)

    except Exception as e:
        return _handle_error(e, LEITOS_SHEET_NAME)


@mcp.tool(
    name="leitos_atualizar_status_limpeza",
    annotations={"title": "Atualizar Status de Limpeza de um Leito",
                 "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def leitos_atualizar_status_limpeza(params: AtualizarStatusLimpezaInput) -> str:
    """Atualiza o campo Status_Limpeza de um leito na planilha Google Sheets.

    Operação de ESCRITA — equivale ao nó 'Atualizar Base de Dados Hospital'
    do N8N (mcp-all-nodes.json). Localiza o leito pelo ID_Leito e atualiza
    apenas o campo Status_Limpeza.

    Requer que a Service Account tenha permissão de EDITOR na planilha.

    Args:
        params (AtualizarStatusLimpezaInput):
            - leito_id (str): ID do leito a atualizar (campo ID_Leito, ex: 'A-101')
            - status_limpeza (str): Novo status (ex: 'Concluído', 'Pendente', 'Em Andamento')

    Returns:
        str: Confirmação da atualização ou mensagem de erro.

    Exemplos:
        - "Marcar limpeza do A-101 como concluída"
          → leito_id="A-101", status_limpeza="Concluído"
        - "Sinalizar UTI-05 com limpeza pendente"
          → leito_id="UTI-05", status_limpeza="Pendente"
    """
    try:
        sheet = _get_leitos_sheet()
        headers = sheet.row_values(1)

        try:
            id_col_idx = headers.index(LEITOS_COL_LEITO) + 1
            status_col_idx = headers.index(LEITOS_COL_STATUS_LIMPEZA) + 1
        except ValueError as ve:
            return (f"Coluna não encontrada na planilha: {ve}. "
                    f"Verifique LEITOS_COL_LEITO='{LEITOS_COL_LEITO}' e "
                    f"LEITOS_COL_STATUS_LIMPEZA='{LEITOS_COL_STATUS_LIMPEZA}' no .env.")

        # Localiza o leito por ID_Leito para obter o número da linha
        all_records = sheet.get_all_records()
        row_number: Optional[int] = None
        for i, record in enumerate(all_records):
            if _normalizar(str(record.get(LEITOS_COL_LEITO, ""))) == _normalizar(params.leito_id):
                row_number = i + 2  # linha 1 = cabeçalho; dados iniciam na linha 2
                break

        if row_number is None:
            return (f"Leito '{params.leito_id}' não encontrado na planilha. "
                    "Use `leitos_listar_leitos` para ver os IDs disponíveis.")

        sheet.update_cell(row_number, status_col_idx, params.status_limpeza)
        logger.info("Status_Limpeza do leito %s atualizado para '%s' (linha %d)",
                    params.leito_id, params.status_limpeza, row_number)

        return (f"✅ Status de limpeza atualizado com sucesso!\n"
                f"- **Leito**: {params.leito_id}\n"
                f"- **Novo Status_Limpeza**: {params.status_limpeza}")

    except gspread.exceptions.APIError as e:
        return (f"Erro na API do Google Sheets ao escrever: {e}. "
                "Verifique se a Service Account tem permissão de EDITOR na planilha.")
    except Exception as e:
        return _handle_error(e, LEITOS_SHEET_NAME)


@mcp.tool(
    name="leitos_enviar_notificacao",
    annotations={"title": "Enviar Notificação por E-mail",
                 "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def leitos_enviar_notificacao(params: EnviarNotificacaoInput) -> str:
    """Envia notificação por e-mail via Gmail SMTP.

    Equivale ao nó 'Enviar Email' (gmailTool) disponível para todos os agentes
    (Diretoria, Enfermaria, UTI) em mcp-all-nodes.json.

    Requer GMAIL_USER e GMAIL_APP_PASSWORD no .env.
    Crie uma Senha de App em: myaccount.google.com/apppasswords

    Args:
        params (EnviarNotificacaoInput):
            - destinatario (str): E-mail do destinatário
            - assunto (str): Assunto do e-mail
            - mensagem (str): Corpo da mensagem (texto simples)

    Exemplos:
        - "Avisar que leito A-101 está disponível"
          → destinatario="equipe@hospital.com.br",
            assunto="Leito A-101 disponível",
            mensagem="O leito A-101 foi liberado e está pronto."
    """
    try:
        _enviar_email_gmail(params.destinatario, params.assunto, params.mensagem)
        logger.info("E-mail enviado para %s | assunto: %s", params.destinatario, params.assunto)
        return (f"✅ E-mail enviado com sucesso!\n"
                f"- **Para**: {params.destinatario}\n"
                f"- **Assunto**: {params.assunto}")
    except EnvironmentError as e:
        return f"Erro de configuração: {e}"
    except smtplib.SMTPAuthenticationError:
        return ("Erro de autenticação SMTP. Verifique GMAIL_USER e GMAIL_APP_PASSWORD no .env. "
                "Use uma Senha de App, não a senha comum do Gmail.")
    except smtplib.SMTPException as e:
        return f"Erro ao enviar e-mail via SMTP: {e}"
    except Exception as e:
        return f"Erro inesperado ao enviar e-mail ({type(e).__name__}): {e}"


@mcp.tool(
    name="leitos_enviar_sms",
    annotations={"title": "Enviar SMS via Twilio",
                 "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def leitos_enviar_sms(params: EnviarSMSInput) -> str:
    """Envia SMS via Twilio REST API.

    Equivale ao nó 'Enviar SMS' (twilioTool) disponível para os agentes
    Enfermaria e UTI em mcp-all-nodes.json.

    Requer TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN e TWILIO_FROM_NUMBER no .env.

    Args:
        params (EnviarSMSInput):
            - destinatario (str): Número no formato E.164 (ex: '+5511952767064')
            - mensagem (str): Texto do SMS (até 160 chars para SMS simples)

    Exemplos:
        - "Avisar por SMS que UTI-05 foi liberada"
          → destinatario="+5511999999999",
            mensagem="Leito UTI-05 disponível para nova internação."
    """
    try:
        await _enviar_sms_twilio(params.destinatario, params.mensagem)
        logger.info("SMS enviado para %s", params.destinatario)
        return (f"✅ SMS enviado com sucesso!\n"
                f"- **Para**: {params.destinatario}\n"
                f"- **Mensagem**: {params.mensagem[:80]}{'...' if len(params.mensagem) > 80 else ''}")
    except EnvironmentError as e:
        return f"Erro de configuração: {e}"
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return "Erro de autenticação Twilio. Verifique TWILIO_ACCOUNT_SID e TWILIO_AUTH_TOKEN no .env."
        return f"Erro na API Twilio (HTTP {e.response.status_code}): {e.response.text}"
    except httpx.TimeoutException:
        return "Erro: timeout ao contactar a API Twilio. Tente novamente."
    except Exception as e:
        return f"Erro inesperado ao enviar SMS ({type(e).__name__}): {e}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
