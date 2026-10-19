---
name: email_noticias_extrair
description: Extrai as notícias individuais de uma newsletter recebida por e-mail — saída JSON (lista)
variables:
  - remetente
  - assunto
  - data
  - max_itens
  - corpo
  - links
---
REGRA INVIOLÁVEL: extraia SOMENTE notícias que estejam literalmente no corpo abaixo. Não invente, não complete com conhecimento próprio, não "melhore" os fatos.

Você vai receber uma newsletter recebida por e-mail. Extraia as notícias/artigos individuais que ela contém, como se fossem itens de um feed RSS.

Para cada item:
- "titulo": o título ou manchete do item como aparece no e-mail (até 120 caracteres). Em português se o original for em português; não traduza títulos em inglês.
- "resumo": 1 a 2 frases em português brasileiro com o que o item diz. Só o que está no e-mail.
- "url": a URL do item, copiada EXATAMENTE de uma das linhas em LINKS abaixo (a que melhor corresponde ao item). Se nenhuma corresponder, use "".

IGNORE: anúncios, patrocínios, cupons, "indique um amigo", "ver no navegador", rodapé, redes sociais, cancelar inscrição, chamadas para cursos pagos.

Limite: no máximo {max_itens} itens, os mais relevantes primeiro. Se o e-mail tiver só 1 notícia, devolva 1 item. Se não tiver nenhuma notícia de verdade, devolva uma lista vazia.

Responda APENAS com JSON válido, um objeto com a chave "itens" contendo a lista:
{{"itens": [{{"titulo": "...", "resumo": "...", "url": "https://..."}}]}}

NEWSLETTER:
De: {remetente}
Assunto: {assunto}
Data: {data}

CORPO:
{corpo}

LINKS (texto do link → URL):
{links}
