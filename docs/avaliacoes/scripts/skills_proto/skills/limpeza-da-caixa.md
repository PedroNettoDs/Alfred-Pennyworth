---
nome: limpeza-da-caixa
descricao: Revisar os lotes de limpeza da caixa de e-mail (promoções, transacionais) e aprovar ou recusar.
gatilhos: ["limpa minha caixa", "tem lote de limpeza", "quero arquivar as promoções"]
ferramentas: [listar_limpezas, responder_limpeza]
perigosa: true
---
## Quando usar
Pedro quer limpar a caixa de entrada ou pergunta pelos lotes de limpeza.

## Passos
1. `listar_limpezas`: lotes aguardando, quantos e-mails e de que categoria.
2. Mostre o resumo de cada lote.
3. Só chame `responder_limpeza` com a decisão que Pedro der (perigosa: pede aprovação).

## Cuidados
- Limpar move e-mails na caixa real: nada sem o "sim" de Pedro.

## Verificação
- O lote citado tem o id devolvido pelo listar_limpezas.
