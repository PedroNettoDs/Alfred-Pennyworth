"""
Alfred — ponte com o WordCraft (processador de texto local), pela porta de controle dele em 127.0.0.1.

O WordCraft precisa ter sido aberto com `--control PORTA` (services/scripts/alfred-escrita.sh faz isso). Cada chamada
abre uma conexão e manda uma linha JSON com a chave da janela, lida na hora do arquivo que o app grava ao abrir: se ele
reiniciou, a chave é outra. A tela que usa esta ponte está descrita em services/inbox/ESCRITA.md.

Importar este módulo registra as ferramentas documento_* no lib_ferramentas; a conversa só as entrega ao modelo nos
turnos que têm documento aberto (FERRAMENTAS).
"""

import json
import os
import socket
from pathlib import Path
from typing import Callable

from _shared.lib_ferramentas import ferramenta

PORTA_PADRAO = 7981
TIMEOUT_S = 10
AUTOR = "Alfred"          # nome nas alterações rastreadas e nos comentários
PARTE_MAX = 10000         # caracteres por parte de documento_ler (a saída de uma ferramenta é cortada em 12000)
FERRAMENTAS = ("documento_ler", "documento_comentar", "documento_substituir")

_FECHADO = "O WordCraft não está aberto pelo atalho do Alfred (services/scripts/alfred-escrita.sh)."


class ErroWordCraft(RuntimeError):
    """A mensagem já vem pronta para Pedro e para o modelo."""


def _porta() -> int:
    return int(os.getenv("WORDCRAFT_CONTROL_PORT", "").strip() or PORTA_PADRAO)


def _arquivo_chave() -> Path:
    perfil = os.getenv("WORDCRAFT_PROFILE_DIR", "").strip()
    base = Path(perfil) if perfil else Path(os.getenv("XDG_CONFIG_HOME", "").strip() or Path.home() / ".config") / "wordcraft"
    return base / f"control-key.local-{_porta()}"


def chamar(metodo: str, **params):
    """Uma chamada ao WordCraft → o `result` da resposta. Fechado, sem resposta ou comando recusado → ErroWordCraft."""
    try:
        chave = _arquivo_chave().read_text(encoding="utf-8").strip()
    except OSError:
        raise ErroWordCraft(_FECHADO) from None
    pedido = json.dumps({"id": 1, "key": chave, "method": metodo, "params": params}, ensure_ascii=False) + "\n"
    try:
        with socket.create_connection(("127.0.0.1", _porta()), timeout=TIMEOUT_S) as s:
            s.sendall(pedido.encode("utf-8"))
            with s.makefile("r", encoding="utf-8") as f:
                linha = f.readline()
    except TimeoutError:
        raise ErroWordCraft("O WordCraft não respondeu a tempo: veja se há uma janela de diálogo aberta nele.") from None
    except OSError:
        raise ErroWordCraft(_FECHADO) from None
    if not linha:
        raise ErroWordCraft(_FECHADO)
    resposta = json.loads(linha)
    if not resposta.get("ok"):
        raise ErroWordCraft(f"O WordCraft recusou o pedido: {resposta.get('error')}")
    return resposta.get("result")


def _executar(comando: str, **params):
    return chamar("engine.execute", command=comando, params=params)


def estado() -> dict:
    """Documento ativo e seleção atual, sem mudar nada → {titulo, caminho, alterado, selecao}."""
    documentos = _executar("view.switchWindows").get("documents") or []
    ativo = next((d for d in documentos if d.get("active")), {})
    return {"titulo": ativo.get("title") or "", "caminho": ativo.get("path") or "", "alterado": bool(ativo.get("dirty")),
            "selecao": _executar("document.selection").get("text") or ""}


def texto_documento() -> str:
    """Corpo do documento ativo, sem o que o controle de alterações marcou como apagado."""
    return _executar("document.text").get("text") or ""


def _partes(texto: str) -> list[str]:
    """Texto → pedaços de até PARTE_MAX caracteres, cortados entre parágrafos sempre que dá."""
    partes, atual = [], ""
    for linha in texto.split("\n"):
        while len(linha) > PARTE_MAX:
            if atual:
                partes.append(atual)
                atual = ""
            partes.append(linha[:PARTE_MAX])
            linha = linha[PARTE_MAX:]
        if atual and len(atual) + 1 + len(linha) > PARTE_MAX:
            partes.append(atual)
            atual = ""
        atual = f"{atual}\n{linha}" if atual else linha
    return partes + [atual] if atual else partes


def _sem_espacos_extras(texto: str) -> str:
    return " ".join(texto.split())


def _selecionar(trecho: str) -> None:
    """
    Deixa `trecho` selecionado. A seleção de Pedro vale se for o mesmo texto (inclusive com vários parágrafos); fora
    disso o trecho tem de aparecer uma única vez, para a edição não cair no lugar errado.
    """
    atual = _executar("document.selection").get("text") or ""
    if atual and _sem_espacos_extras(atual) == _sem_espacos_extras(trecho):
        return
    if "\n" in trecho.strip():
        raise ErroWordCraft("O trecho tem mais de um parágrafo: peça a Pedro para selecioná-lo no documento.")
    vezes = texto_documento().count(trecho)
    if vezes == 0:
        raise ErroWordCraft("Trecho não encontrado no documento: use o texto exatamente como está escrito nele.")
    if vezes > 1:
        raise ErroWordCraft(f"O trecho aparece {vezes} vezes: use um trecho mais longo ou peça a Pedro para selecioná-lo.")
    _executar("select.text", text=trecho)


def _como_alfred(edicao: Callable[[], None]) -> None:
    """Roda `edicao` com autor Alfred e controle de alterações ligado; depois deixa os dois como Pedro tinha."""
    autor_antes = (chamar("ui.inspect").get("ui") or {}).get("author") or ""
    rastreava = bool(chamar("document.inspect").get("trackChanges"))
    _executar("file.setAuthor", name=AUTOR)
    _executar("review.trackChanges", value=True)
    try:
        edicao()
    finally:
        _executar("review.trackChanges", value=rastreava)
        if autor_antes:
            _executar("file.setAuthor", name=autor_antes)


@ferramenta("documento_ler",
            "Lê o documento aberto no WordCraft, em partes de até 10 mil caracteres. Use quando precisar de um trecho "
            "fora do que Pedro selecionou.")
def documento_ler(parte: int = 1) -> str:
    """
    :param parte: Número da parte, a partir de 1
    """
    partes = _partes(texto_documento())
    if not 1 <= parte <= len(partes):
        return f"O documento tem {len(partes)} parte(s)."
    return f"Parte {parte} de {len(partes)}:\n\n{partes[parte - 1]}"


@ferramenta("documento_comentar",
            "Adiciona um comentário do Alfred a um trecho do documento aberto, sem mudar o texto. Use para apontar "
            "problema de ABNT, de argumento ou de escrita.")
def documento_comentar(trecho: str, comentario: str) -> str:
    """
    :param trecho: Texto comentado, exatamente como está no documento
    :param comentario: O comentário
    """
    def comentar():
        _selecionar(trecho)
        _executar("review.newComment", text=comentario)
    _como_alfred(comentar)
    return "Comentário adicionado ao documento."


@ferramenta("documento_substituir",
            "Troca um trecho do documento aberto por um texto novo, como alteração rastreada do Alfred: Pedro aceita "
            "ou rejeita no WordCraft. Use só quando Pedro pedir para reescrever ou corrigir.",
            perigosa=True)
def documento_substituir(trecho_original: str, novo_texto: str) -> str:
    """
    :param trecho_original: Texto a trocar, exatamente como está no documento
    :param novo_texto: Texto que entra no lugar; uma linha por parágrafo
    """
    def trocar():
        _selecionar(trecho_original)
        _executar("text.insert", text=novo_texto, paragraphs="\n" in novo_texto)
    _como_alfred(trocar)
    return "Trecho trocado como alteração rastreada do Alfred. Nada foi salvo: Pedro revisa e salva no WordCraft."
