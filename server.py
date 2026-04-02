#!/usr/bin/env python3
"""
MCP Server - AutoPeças AutoMax

Servidor MCP que conecta ao Google Sheets para fornecer acesso à base de dados
de autopeças da AutoMax. Expõe ferramentas para busca, listagem e consulta de
peças, categorias, estoque e fornecedores.

Configuração via variáveis de ambiente (.env):
    SPREADSHEET_ID          - ID da planilha Google Sheets
    SHEET_NAME              - Nome da aba (padrão: "AutoPeças")
    GOOGLE_CREDENTIALS_PATH - Caminho para o JSON da Service Account
    GOOGLE_CREDENTIALS_JSON - JSON da Service Account como string (alternativa)
"""

import json
import logging
import os
import sys
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
logger = logging.getLogger("mcp-autopecas")

# Escopos necessários para leitura de Google Sheets
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

SPREADSHEET_ID: str = os.getenv("SPREADSHEET_ID", "")
SHEET_NAME: str = os.getenv("SHEET_NAME", "AutoPeças")

# Mapeamento de colunas — pode ser sobrescrito via .env
COL_CODIGO: str = os.getenv("COL_CODIGO", "Código")
COL_NOME: str = os.getenv("COL_NOME", "Nome")
COL_CATEGORIA: str = os.getenv("COL_CATEGORIA", "Categoria")
COL_MARCA: str = os.getenv("COL_MARCA", "Marca")
COL_PRECO: str = os.getenv("COL_PRECO", "Preço")
COL_ESTOQUE: str = os.getenv("COL_ESTOQUE", "Estoque")
COL_FORNECEDOR: str = os.getenv("COL_FORNECEDOR", "Fornecedor")
COL_DESCRICAO: str = os.getenv("COL_DESCRICAO", "Descrição")
COL_LOCALIZACAO: str = os.getenv("COL_LOCALIZACAO", "Localização")

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


@lru_cache(maxsize=1)
def _get_sheet() -> gspread.Worksheet:
    """Retorna o worksheet (cacheado para evitar reconexões desnecessárias)."""
    if not SPREADSHEET_ID:
        raise EnvironmentError("SPREADSHEET_ID não configurado no .env")

    creds = _build_credentials()
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    return spreadsheet.worksheet(SHEET_NAME)


def _get_all_records() -> list[dict[str, Any]]:
    """Busca todos os registros da planilha como lista de dicts."""
    sheet = _get_sheet()
    return sheet.get_all_records()


# ---------------------------------------------------------------------------
# Utilitários de filtragem e formatação
# ---------------------------------------------------------------------------


def _normalizar(texto: str) -> str:
    """Normaliza texto para comparação case-insensitive sem acentos."""
    import unicodedata
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii").lower()


def _filtrar_registros(
    registros: list[dict[str, Any]],
    query: Optional[str] = None,
    categoria: Optional[str] = None,
    marca: Optional[str] = None,
    apenas_em_estoque: bool = False,
) -> list[dict[str, Any]]:
    """Aplica filtros combinados sobre a lista de registros."""
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
        resultado = [
            r for r in resultado
            if _normalizar(str(r.get(COL_CATEGORIA, ""))) == cat
        ]

    if marca:
        m = _normalizar(marca)
        resultado = [
            r for r in resultado
            if _normalizar(str(r.get(COL_MARCA, ""))) == m
        ]

    if apenas_em_estoque:
        resultado = [
            r for r in resultado
            if _estoque_disponivel(r)
        ]

    return resultado


def _estoque_disponivel(registro: dict[str, Any]) -> bool:
    """Verifica se o registro possui estoque maior que zero."""
    try:
        return int(str(registro.get(COL_ESTOQUE, "0")).replace(",", "").strip() or "0") > 0
    except (ValueError, TypeError):
        return False


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


def _handle_error(e: Exception) -> str:
    """Formata erros de forma clara e acionável."""
    if isinstance(e, EnvironmentError):
        return f"Erro de configuração: {e}"
    if isinstance(e, gspread.exceptions.SpreadsheetNotFound):
        return "Erro: Planilha não encontrada. Verifique o SPREADSHEET_ID no .env."
    if isinstance(e, gspread.exceptions.WorksheetNotFound):
        return f"Erro: Aba '{SHEET_NAME}' não encontrada na planilha. Verifique SHEET_NAME no .env."
    if isinstance(e, gspread.exceptions.APIError):
        return f"Erro na API do Google Sheets: {e}. Verifique as permissões da Service Account."
    return f"Erro inesperado ({type(e).__name__}): {e}"


# ---------------------------------------------------------------------------
# Enum e modelos de entrada
# ---------------------------------------------------------------------------


class FormatoResposta(str, Enum):
    """Formato de saída das ferramentas."""
    MARKDOWN = "markdown"
    JSON = "json"


class BuscarPecaInput(BaseModel):
    """Input para busca geral de peças."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    query: str = Field(
        ...,
        description="Texto livre para buscar por código, nome ou descrição da peça (ex: 'filtro de óleo', 'F-1023')",
        min_length=1,
        max_length=200,
    )
    categoria: Optional[str] = Field(
        default=None,
        description="Filtrar por categoria (ex: 'Motor', 'Freios', 'Suspensão')",
        max_length=100,
    )
    marca: Optional[str] = Field(
        default=None,
        description="Filtrar por marca/fabricante (ex: 'Bosch', 'Mahle', 'Monroe')",
        max_length=100,
    )
    apenas_em_estoque: bool = Field(
        default=False,
        description="Se True, retorna apenas peças com estoque disponível (> 0)",
    )
    limit: int = Field(default=20, description="Máximo de resultados por página (1–100)", ge=1, le=100)
    offset: int = Field(default=0, description="Deslocamento para paginação", ge=0)
    formato: FormatoResposta = Field(
        default=FormatoResposta.MARKDOWN,
        description="Formato de saída: 'markdown' (legível) ou 'json' (estruturado)",
    )

    @field_validator("query")
    @classmethod
    def query_nao_vazia(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("O campo query não pode ser vazio.")
        return v


class ListarPecasInput(BaseModel):
    """Input para listagem paginada de todas as peças."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    categoria: Optional[str] = Field(
        default=None,
        description="Filtrar por categoria (ex: 'Motor', 'Elétrica')",
        max_length=100,
    )
    marca: Optional[str] = Field(
        default=None,
        description="Filtrar por marca (ex: 'Bosch', 'NGK')",
        max_length=100,
    )
    apenas_em_estoque: bool = Field(
        default=False,
        description="Se True, retorna apenas peças com estoque > 0",
    )
    limit: int = Field(default=20, description="Máximo de resultados por página (1–100)", ge=1, le=100)
    offset: int = Field(default=0, description="Deslocamento para paginação", ge=0)
    formato: FormatoResposta = Field(
        default=FormatoResposta.MARKDOWN,
        description="Formato de saída: 'markdown' ou 'json'",
    )


class ObterDetalhesPecaInput(BaseModel):
    """Input para busca de uma peça específica pelo código."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    codigo: str = Field(
        ...,
        description="Código único da peça na base de dados (ex: 'F-1023', 'BP-0042')",
        min_length=1,
        max_length=50,
    )
    formato: FormatoResposta = Field(
        default=FormatoResposta.MARKDOWN,
        description="Formato de saída: 'markdown' ou 'json'",
    )


class ListarCategoriasInput(BaseModel):
    """Input para listagem de categorias."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    formato: FormatoResposta = Field(
        default=FormatoResposta.MARKDOWN,
        description="Formato de saída: 'markdown' ou 'json'",
    )


class VerificarEstoqueInput(BaseModel):
    """Input para verificação de estoque."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    codigos: Optional[list[str]] = Field(
        default=None,
        description="Lista de códigos de peças para verificar estoque (ex: ['F-1023', 'BP-0042']). Se omitido, retorna resumo geral.",
        max_length=50,
    )
    categoria: Optional[str] = Field(
        default=None,
        description="Filtrar resumo de estoque por categoria",
        max_length=100,
    )
    formato: FormatoResposta = Field(
        default=FormatoResposta.MARKDOWN,
        description="Formato de saída: 'markdown' ou 'json'",
    )


class ListarMarcasInput(BaseModel):
    """Input para listagem de marcas."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    categoria: Optional[str] = Field(
        default=None,
        description="Filtrar marcas por categoria (ex: 'Freios')",
        max_length=100,
    )
    formato: FormatoResposta = Field(
        default=FormatoResposta.MARKDOWN,
        description="Formato de saída: 'markdown' ou 'json'",
    )


# ---------------------------------------------------------------------------
# Inicialização do servidor MCP
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "autopecas_mcp",
    instructions=(
        "Você é o assistente AutoMax, especialista em autopeças. "
        "Use as ferramentas disponíveis para consultar a base de dados de peças, "
        "verificar estoque, listar categorias e encontrar peças específicas."
    ),
)

# ---------------------------------------------------------------------------
# Ferramentas MCP
# ---------------------------------------------------------------------------


@mcp.tool(
    name="autopecas_buscar_peca",
    annotations={
        "title": "Buscar Peça por Nome, Código ou Descrição",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def autopecas_buscar_peca(params: BuscarPecaInput) -> str:
    """Busca peças na base de dados AutoPeças por texto livre (nome, código ou descrição).

    Realiza busca case-insensitive, sem distinção de acentos, nos campos código,
    nome e descrição de todas as peças cadastradas no Google Sheets.
    Suporta filtros adicionais por categoria, marca e disponibilidade de estoque.

    Args:
        params (BuscarPecaInput): Parâmetros de busca:
            - query (str): Texto para busca (ex: 'filtro', 'vela', 'F-1023')
            - categoria (Optional[str]): Filtro por categoria (ex: 'Motor')
            - marca (Optional[str]): Filtro por marca (ex: 'Bosch')
            - apenas_em_estoque (bool): Se True, apenas peças disponíveis
            - limit (int): Máximo de resultados (padrão: 20)
            - offset (int): Paginação (padrão: 0)
            - formato (str): 'markdown' ou 'json'

    Returns:
        str: Lista de peças encontradas com código, nome, categoria, preço e estoque.
             Retorna mensagem informativa se nenhuma peça for encontrada.

    Exemplos de uso:
        - "Buscar filtros de óleo" → query="filtro de óleo"
        - "Velas de ignição Bosch" → query="vela", marca="Bosch"
        - "Pastilhas de freio disponíveis" → query="pastilha", apenas_em_estoque=True
    """
    try:
        registros = _get_all_records()
        filtrados = _filtrar_registros(
            registros,
            query=params.query,
            categoria=params.categoria,
            marca=params.marca,
            apenas_em_estoque=params.apenas_em_estoque,
        )

        if not filtrados:
            return f"Nenhuma peça encontrada para '{params.query}'."

        pagina = _paginar(filtrados, params.limit, params.offset)

        if params.formato == FormatoResposta.JSON:
            return json.dumps(pagina, ensure_ascii=False, indent=2)

        linhas = [
            f"# Resultados para '{params.query}'",
            f"Encontradas **{pagina['total']}** peças (exibindo {pagina['count']})",
            "",
        ]
        for r in pagina["items"]:
            linhas.append(_formatar_peca_markdown(r))
            linhas.append("")

        if pagina["has_more"]:
            linhas.append(f"_Use `offset={pagina['next_offset']}` para ver mais resultados._")

        return "\n".join(linhas)

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="autopecas_listar_pecas",
    annotations={
        "title": "Listar Todas as Peças",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def autopecas_listar_pecas(params: ListarPecasInput) -> str:
    """Lista todas as peças cadastradas na base AutoPeças com suporte a paginação e filtros.

    Retorna o catálogo completo de peças ou subconjuntos filtrados por categoria,
    marca ou disponibilidade de estoque. Ideal para navegação e exploração do catálogo.

    Args:
        params (ListarPecasInput): Parâmetros de listagem:
            - categoria (Optional[str]): Filtrar por categoria
            - marca (Optional[str]): Filtrar por marca
            - apenas_em_estoque (bool): Apenas peças com estoque > 0
            - limit (int): Máximo de resultados (padrão: 20)
            - offset (int): Paginação (padrão: 0)
            - formato (str): 'markdown' ou 'json'

    Returns:
        str: Lista paginada de peças com metadados de paginação (total, has_more, next_offset).

    Exemplos de uso:
        - "Listar todas as peças de Freios" → categoria="Freios"
        - "Peças Bosch em estoque" → marca="Bosch", apenas_em_estoque=True
        - "Ver próxima página" → offset=20
    """
    try:
        registros = _get_all_records()
        filtrados = _filtrar_registros(
            registros,
            categoria=params.categoria,
            marca=params.marca,
            apenas_em_estoque=params.apenas_em_estoque,
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

        linhas = [
            titulo,
            f"Total: **{pagina['total']}** peças (exibindo {pagina['count']})",
            "",
        ]
        for r in pagina["items"]:
            linhas.append(_formatar_peca_markdown(r))
            linhas.append("")

        if pagina["has_more"]:
            linhas.append(f"_Use `offset={pagina['next_offset']}` para ver mais resultados._")

        return "\n".join(linhas)

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="autopecas_obter_detalhes",
    annotations={
        "title": "Obter Detalhes Completos de uma Peça",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def autopecas_obter_detalhes(params: ObterDetalhesPecaInput) -> str:
    """Retorna todos os dados cadastrados de uma peça específica pelo seu código único.

    Busca correspondência exata (case-insensitive) no campo código da planilha.
    Retorna informações completas incluindo preço, estoque, fornecedor, localização
    e descrição técnica.

    Args:
        params (ObterDetalhesPecaInput): Parâmetros:
            - codigo (str): Código único da peça (ex: 'F-1023', 'BP-0042')
            - formato (str): 'markdown' ou 'json'

    Returns:
        str: Dados completos da peça ou mensagem de erro se não encontrada.

    Exemplos de uso:
        - "Detalhes da peça F-1023" → codigo="F-1023"
        - "Informações sobre BP-0042" → codigo="BP-0042"
    """
    try:
        registros = _get_all_records()
        codigo_norm = _normalizar(params.codigo)
        encontrado = next(
            (r for r in registros if _normalizar(str(r.get(COL_CODIGO, ""))) == codigo_norm),
            None,
        )

        if not encontrado:
            return (
                f"Peça com código '{params.codigo}' não encontrada. "
                "Use `autopecas_buscar_peca` para localizar pelo nome ou descrição."
            )

        if params.formato == FormatoResposta.JSON:
            return json.dumps(encontrado, ensure_ascii=False, indent=2)

        return f"# Detalhes da Peça\n\n{_formatar_peca_markdown(encontrado)}"

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="autopecas_listar_categorias",
    annotations={
        "title": "Listar Categorias de Peças",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def autopecas_listar_categorias(params: ListarCategoriasInput) -> str:
    """Lista todas as categorias de peças disponíveis na base AutoPeças com contagem por categoria.

    Útil para descobrir quais categorias existem antes de aplicar filtros em outras ferramentas.
    Cada categoria inclui o total de peças cadastradas e a quantidade com estoque disponível.

    Args:
        params (ListarCategoriasInput): Parâmetros:
            - formato (str): 'markdown' ou 'json'

    Returns:
        str: Lista de categorias com contagem de peças total e em estoque.

    Exemplos de uso:
        - "Quais categorias de peças existem?"
        - "Listar categorias disponíveis"
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
            resultado = [
                {"categoria": cat, "total": info["total"], "em_estoque": info["em_estoque"]}
                for cat, info in ordenadas
            ]
            return json.dumps({"categorias": resultado, "total": len(resultado)}, ensure_ascii=False, indent=2)

        linhas = [
            "# Categorias AutoPeças",
            f"Total de **{len(ordenadas)}** categorias\n",
            "| Categoria | Total | Em Estoque |",
            "|-----------|-------|------------|",
        ]
        for cat, info in ordenadas:
            linhas.append(f"| {cat} | {info['total']} | {info['em_estoque']} |")

        return "\n".join(linhas)

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="autopecas_verificar_estoque",
    annotations={
        "title": "Verificar Estoque de Peças",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def autopecas_verificar_estoque(params: VerificarEstoqueInput) -> str:
    """Verifica o estoque de peças específicas ou exibe um resumo geral por categoria.

    Se `codigos` for informado, retorna o estoque de cada peça listada.
    Se omitido, retorna um resumo de estoque agrupado por categoria (com filtro opcional).

    Args:
        params (VerificarEstoqueInput): Parâmetros:
            - codigos (Optional[List[str]]): Códigos das peças a verificar
            - categoria (Optional[str]): Filtrar resumo por categoria
            - formato (str): 'markdown' ou 'json'

    Returns:
        str: Estoque das peças solicitadas ou resumo geral por categoria.
             Inclui alerta para peças com estoque zero ou abaixo de 5 unidades.

    Exemplos de uso:
        - "Estoque de F-1023 e BP-0042" → codigos=["F-1023", "BP-0042"]
        - "Resumo de estoque de Freios" → categoria="Freios"
        - "Visão geral do estoque" → (sem parâmetros)
    """
    try:
        registros = _get_all_records()

        # --- Modo 1: Verificação de peças específicas ---
        if params.codigos:
            resultados = []
            nao_encontrados = []
            for cod in params.codigos:
                cod_norm = _normalizar(cod)
                r = next(
                    (rec for rec in registros if _normalizar(str(rec.get(COL_CODIGO, ""))) == cod_norm),
                    None,
                )
                if r:
                    resultados.append(r)
                else:
                    nao_encontrados.append(cod)

            if params.formato == FormatoResposta.JSON:
                return json.dumps(
                    {
                        "pecas": resultados,
                        "nao_encontrados": nao_encontrados,
                    },
                    ensure_ascii=False,
                    indent=2,
                )

            linhas = ["# Verificação de Estoque\n"]
            for r in resultados:
                qtd_raw = str(r.get(COL_ESTOQUE, "0")).replace(",", "").strip()
                try:
                    qtd = int(qtd_raw or "0")
                except ValueError:
                    qtd = 0

                alerta = ""
                if qtd == 0:
                    alerta = " ⚠️ **SEM ESTOQUE**"
                elif qtd <= 5:
                    alerta = " ⚠️ Estoque baixo"

                linhas.append(
                    f"- `{r.get(COL_CODIGO)}` — {r.get(COL_NOME)} | **{qtd} un.**{alerta}"
                )

            if nao_encontrados:
                linhas.append(f"\n_Não encontrados: {', '.join(nao_encontrados)}_")

            return "\n".join(linhas)

        # --- Modo 2: Resumo geral por categoria ---
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
            return json.dumps(
                {"resumo_por_categoria": resumo},
                ensure_ascii=False,
                indent=2,
            )

        titulo = "# Resumo de Estoque"
        if params.categoria:
            titulo += f" — {params.categoria}"

        linhas = [
            titulo,
            "",
            "| Categoria | Peças | Qtd. Total | Sem Estoque | Estoque Baixo |",
            "|-----------|-------|-----------|-------------|---------------|",
        ]
        for cat, info in sorted(resumo.items()):
            linhas.append(
                f"| {cat} | {info['total']} | {info['qtd_total']} "
                f"| {info['sem_estoque']} | {info['estoque_baixo']} |"
            )

        return "\n".join(linhas)

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="autopecas_listar_marcas",
    annotations={
        "title": "Listar Marcas/Fabricantes de Peças",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def autopecas_listar_marcas(params: ListarMarcasInput) -> str:
    """Lista todas as marcas/fabricantes disponíveis, com contagem de peças por marca.

    Permite filtrar por categoria para ver quais marcas estão disponíveis em
    um segmento específico. Útil para orientar buscas e filtros por fabricante.

    Args:
        params (ListarMarcasInput): Parâmetros:
            - categoria (Optional[str]): Filtrar por categoria
            - formato (str): 'markdown' ou 'json'

    Returns:
        str: Lista de marcas com quantidade de peças cadastradas por marca.

    Exemplos de uso:
        - "Quais marcas de freios temos?" → categoria="Freios"
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
            resultado = [{"marca": m, "total": t} for m, t in ordenadas]
            return json.dumps({"marcas": resultado, "total": len(resultado)}, ensure_ascii=False, indent=2)

        titulo = "# Marcas / Fabricantes"
        if params.categoria:
            titulo += f" — {params.categoria}"

        linhas = [titulo, f"Total: **{len(ordenadas)}** marcas\n"]
        for marca, total in ordenadas:
            linhas.append(f"- **{marca}**: {total} peça(s)")

        return "\n".join(linhas)

    except Exception as e:
        return _handle_error(e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
