"""
Alfred — baixar uma página da web sem virar porta de entrada para a rede local (SSRF).

Usado pelo modo profundo do researcher e pela ferramenta ler_pagina (lib_ferramentas). Os dois rodam no host,
ao lado de Ollama, Painel e APIs sem senha: uma página (ou um redirect dela) não pode fazê-los buscar
http://127.0.0.1:7073/... Aqui não se extrai texto — cada um extrai do seu jeito (o researcher com BeautifulSoup,
as ferramentas com o parser da lib_mail, só biblioteca padrão).

    url_publica(url)  → True só para http/https cujo host resolve APENAS para IPs públicos
    baixar(url, ...)  → {url, tipo, charset, corpo, truncada}; redirects seguidos à mão e revalidados, no máximo
                        `max_bytes` lidos e `timeout` segundos no total. Qualquer recusa ou falha → ErroPagina
"""

import ipaddress
import socket
import time
from typing import Callable, Optional
from urllib.parse import urljoin, urlsplit

import httpx

TIMEOUT       = 15             # s — teto por página, do pedido ao último byte
MAX_BYTES     = 2_000_000      # lê no máximo isso de cada página; o resto é descartado
MAX_REDIRECTS = 3
TIPOS_TEXTO   = {"", "text/html", "application/xhtml+xml", "text/plain"}
USER_AGENT    = "Mozilla/5.0 (compatible; AlfredResearcher/3.2; +local)"

_transporte_http: Optional[httpx.BaseTransport] = None   # os testes trocam por httpx.MockTransport


class ErroPagina(Exception):
    """Página não lida: URL recusada, HTTP diferente de 200, tipo que não é texto, tempo esgotado, redirects demais."""


def resolver(host: str) -> list[str]:
    return list({info[4][0] for info in socket.getaddrinfo(host, None)})


def url_publica(url: str, dns: Optional[Callable[[str], list[str]]] = None) -> bool:
    """Só http/https para host que resolve APENAS para IPs públicos — nada de localhost, rede local,
    link-local (169.254.169.254), CGNAT. `dns` troca o resolver (testes).
    Resta a janela entre esta resolução e a conexão do httpx (DNS rebinding); para um assistente de um usuário
    só, que lê o que o SearXNG devolveu ou o que Pedro pediu, o risco foi aceito."""
    partes = urlsplit(url)
    if partes.scheme not in ("http", "https") or not partes.hostname:
        return False
    try:
        ips = (dns or resolver)(partes.hostname)
    except (OSError, UnicodeError):
        return False
    try:
        enderecos = [ipaddress.ip_address(ip.split("%", 1)[0]) for ip in ips]
    except ValueError:
        return False
    enderecos = [getattr(ip, "ipv4_mapped", None) or ip for ip in enderecos]
    return bool(enderecos) and all(ip.is_global for ip in enderecos)


def baixar(url: str, *, timeout: float = TIMEOUT, max_bytes: int = MAX_BYTES, max_redirects: int = MAX_REDIRECTS,
           tipos=TIPOS_TEXTO, user_agent: str = USER_AGENT,
           dns: Optional[Callable[[str], list[str]]] = None,
           transporte: Optional[httpx.BaseTransport] = None) -> dict:
    """Baixa a página → {url (a final, depois dos redirects), tipo, charset, corpo: bytes, truncada: bool}.

    Redirects seguidos à mão, cada destino revalidado por url_publica; lê no máximo `max_bytes` (página maior
    é truncada) e desiste depois de `timeout` segundos no total — o timeout do httpx vale por operação, e um
    servidor que pinga um byte por vez nunca o estouraria. Só os `tipos` (HTML e texto puro)."""
    limite = time.monotonic() + timeout
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, transport=transporte or _transporte_http,
                          headers={"User-Agent": user_agent}) as client:
            for _ in range(max_redirects + 1):
                if not url_publica(url, dns):
                    raise ErroPagina(f"URL recusada (não é http/https público): {url}")
                with client.stream("GET", url) as resp:
                    if resp.is_redirect and resp.headers.get("location"):
                        url = urljoin(url, resp.headers["location"])
                        continue
                    if resp.status_code != 200:
                        raise ErroPagina(f"HTTP {resp.status_code} em {url}")
                    tipo = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if tipo not in tipos:
                        raise ErroPagina(f"tipo {tipo!r} ignorado em {url}")
                    corpo, truncada = bytearray(), False
                    for pedaco in resp.iter_bytes():
                        corpo.extend(pedaco)
                        if len(corpo) >= max_bytes:
                            del corpo[max_bytes:]
                            truncada = True
                            break
                        if time.monotonic() > limite:
                            raise ErroPagina(f"{url} passou de {timeout} s — desistindo")
                    return {"url": url, "tipo": tipo, "charset": resp.charset_encoding, "corpo": bytes(corpo),
                            "truncada": truncada}
            raise ErroPagina(f"redirects demais: {url}")
    except httpx.HTTPError as e:
        raise ErroPagina(f"falha ao baixar {url}: {e!r}") from None


def decodificar(corpo: bytes, charset: Optional[str]) -> str:
    """bytes → str com o charset do cabeçalho (desconhecido ou ausente → utf-8), sem levantar."""
    try:
        return corpo.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return corpo.decode("utf-8", errors="replace")
