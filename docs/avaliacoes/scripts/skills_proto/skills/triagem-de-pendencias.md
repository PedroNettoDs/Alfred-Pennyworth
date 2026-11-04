---
nome: triagem-de-pendencias
descricao: Ver as pendências de e-mail e WhatsApp que esperam resposta e preparar a resposta das mais urgentes.
gatilhos: ["o que preciso responder", "tem alguma pendência urgente", "quem está esperando resposta"]
ferramentas: [listar_pendencias, ver_pendencia, responder_pendencia]
perigosa: true
---
## Quando usar
Pedro quer saber o que espera resposta ou quer despachar as pendências.

## Passos
1. `listar_pendencias` (aguardando).
2. Para as 3 mais antigas ou urgentes: `ver_pendencia`.
3. Proponha um texto curto de resposta para cada uma.
4. Só chame `responder_pendencia` depois que Pedro confirmar o texto (ferramenta perigosa: pede aprovação).

## Cuidados
- Nunca responda sem confirmação explícita.

## Verificação
- Cada pendência citada tem o id devolvido pelo listar_pendencias.
