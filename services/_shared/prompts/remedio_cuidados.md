---
name: remedio_cuidados
description: Cuidados de um remédio (o que não fazer e contraindicações) em frases curtas, tirados de uma pesquisa — JSON
variables:
  - nome
  - sintese
---
REGRA INVIOLÁVEL: use SOMENTE o texto da PESQUISA abaixo. Não invente cuidado, dose nem efeito que não esteja escrito nela.

Pedro vai tomar o remédio {nome} e quer ser lembrado, a cada dose, do que NÃO fazer e de quando NÃO tomar.

- "cuidados": até 5 frases curtas (no máximo 80 caracteres cada), em português, das mais importantes para as menos:
  - o que não fazer durante o uso (álcool, dirigir, alimentos, outros remédios, sol);
  - contraindicações (quem não deve tomar);
  - como tomar, só se a pesquisa disser (com água, em jejum, depois de comer).
- Formato das frases: "Não beber álcool", "Não tomar junto com leite ou antiácido", "Contraindicado para alérgicos a penicilina".
- Sem dose, sem diagnóstico e sem "consulte seu médico" (o lembrete já diz para conferir a bula).
- Se a pesquisa não falar dos cuidados deste remédio, devolva a lista vazia.

Responda APENAS com JSON:
{{"cuidados": ["", ""]}}

PESQUISA:
{sintese}
