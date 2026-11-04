---
nome: pesquisa-para-o-cofre
descricao: Pesquisar um tema e deixar a nota no cofre, sem repetir pesquisa que já existe.
gatilhos: ["pesquise sobre", "estude esse assunto e guarde", "quero uma nota sobre"]
ferramentas: [buscar_cofre, pesquisar, andamento_pesquisa]
perigosa: false
---
## Quando usar
Pedro pede para pesquisar, estudar ou montar uma nota sobre um tema.

## Passos
1. `buscar_cofre` com o tema: já existe nota ou pesquisa? Se sim, mostre e pergunte se quer atualizar.
2. Se não existe: `pesquisar` (profundo=false). Para "aprofundar" ou "relatório", profundo=true.
3. Pesquisa profunda devolve job_id: diga que roda em segundo plano e como acompanhar (`andamento_pesquisa`).
4. Responda com o caminho da nota e 3 pontos principais.

## Cuidados
- O researcher já salva a nota: não use salvar_nota por cima.

## Verificação
- O caminho citado veio da resposta do pesquisar ou do buscar_cofre.
