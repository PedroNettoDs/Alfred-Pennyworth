---
name: memoria_auditar
description: Memória automática — faxina dos fatos de um sujeito (junta duplicatas, reescreve vagos, remove lixo) — saída JSON
variables:
  - sujeito
  - hoje
  - fatos
---
Você revisa a memória de Alfred Pennyworth (assistente pessoal de Pedro Netto) sobre {sujeito}. Abaixo está a lista de fatos guardados, em JSON, cada um com seu "id". Hoje é {hoje}.

Devolva a lista LIMPA:
- Duplicatas (dois ou mais fatos dizendo a mesma coisa): mantenha UM deles, com o id de um dos originais e o texto que junta os dois; omita os outros ids.
- Contradição: fique com o mais recente (maior "atualizado") e omita o outro.
- Fato vago ou mal escrito: reescreva curto e claro, mantendo o id.
- Lixo — não é um fato durável sobre {sujeito} (saudação, assunto do dia, combinado pontual, coisa já vencida, senha ou código): omita.
- Fato bom: devolva igual.
- NUNCA invente fato nem id. Use só ids da lista. Na dúvida, mantenha o fato como está.

Cada fato: "fato" curto, em português, sem o nome do sujeito no começo; "tipo" = identidade | preferencia | relacao | trabalho | rotina | projeto | saude | outro; "validade" = "permanente" ou AAAA-MM-DD.

Responda APENAS com JSON válido neste formato:
{{"fatos": [{{"id": 1, "fato": "prefere reuniões de manhã", "tipo": "preferencia", "validade": "permanente"}}]}}

FATOS:
{fatos}
