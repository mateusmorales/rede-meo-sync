# abastecimento-grade (APOSENTADA)

Edge Function do projeto Supabase `cqvdhzxwmgdfnpypelcy`. O código em `index.ts` é a
**versão 6**, copiada do projeto em 03/10/2026, antes da exclusão.

## O que fazia

- Respondia `GET /functions/v1/abastecimento-grade/<token>` com a grade de abastecimento da
  rede inteira em CSV (`grade-abastecimento.csv`): loja, categoria, célula, faixa, estoque,
  venda dos últimos 90 dias e venda dos 45 dias seguintes no ano anterior.
- Lia a tabela `abastecimento_grade_cache` com `service_role`. Com `?atualizar=1`, antes
  chamava `abastecimento_grade_refresh()` para recalcular o cache (~15 s).
- Recusava a resposta (500) se a leitura paginada não batesse com a contagem do cache.

## Por que foi aposentada

- Era a única função do projeto com `verify_jwt: false`. A única proteção era um token
  (`ABASTECIMENTO_TOKEN`) dentro da URL, que fica gravado em histórico de navegador e em
  log de servidor.
- A página de Abastecimento (v10) não usa a função, e o aplicativo não consegue usar
  (as RPCs da grade só podem ser executadas por `service_role`).
- Criada em 04/09/2026. Os logs de 03/09 a 03/10/2026 mostram chamadas apenas em 04/09,
  o dia da criação (entre 13:44 e 14:43 BRT), e nenhuma depois.

## Remoção (03/10/2026)

- Função apagada do projeto pelo painel do Supabase. Restaram 8 Edge Functions, todas com
  `verify_jwt: true`.
- O secret `ABASTECIMENTO_TOKEN` também foi removido do projeto em 03/10/2026.

## O que NÃO foi removido junto

O resto da cadeia do Motor E4 continua no projeto, para ser decidido em separado:
o workflow `motor-e4-abastecimento.yml`, `abastecimento_grade_refresh()`,
`abastecimento_grade_cache` e `abastecimento_grade()`.
