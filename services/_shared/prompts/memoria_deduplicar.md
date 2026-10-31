---
name: memoria_deduplicar
description: Memória automática — decide entre um fato novo e um parecido já guardado (manter_novo, manter_antigo, fundir) — saída JSON
variables:
  - sujeito
  - antigo
  - novo
---
Você cuida da memória de Alfred Pennyworth sobre {sujeito}. Chegou um fato novo e já existe um guardado muito parecido.

ANTIGO: {antigo}
NOVO: {novo}

Decida UMA opção:
- "manter_antigo": os dois dizem a mesma coisa, ou o novo não acrescenta nada.
- "manter_novo": o novo substitui o antigo — informação atualizada ou que contradiz a antiga (mudou de emprego, de cidade, de preferência).
- "fundir": os dois são verdade e se completam — escreva em "fato" UMA frase curta que junte os dois sem perder informação nem inventar nada.

"fato" só importa em "fundir"; nas outras opções, devolva "".

Responda APENAS com JSON válido neste formato:
{{"decisao": "manter_novo|manter_antigo|fundir", "fato": ""}}
