# rede-meo-sync

Motores de sincronização de vendas Microvix (Linx) → Supabase, para o app Rede MEO.

- **Motor 2** (`motor2_conferencia.py`): conferência diária, janela de 7 dias + produtos. Roda 3h (Brasília) via GitHub Actions.
- **Motor 3** (`motor3_tempo_real.py`): vendas do dia corrente, a cada 10 min das 8h às 23h59 (Brasília).

Credenciais ficam em GitHub Secrets (`SUPABASE_SERVICE_KEY`, `LINX_CHAVE`), nunca no código.
