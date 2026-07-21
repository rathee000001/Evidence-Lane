# Credit Token Estimate Comparison

## Research Estimate Formula

T_naive = ceil(B_raw / 4) + T_prompt
T_env = T_prompt + ceil(B_query / 4) + T_receipt + T_overhead
Token_Saved = max(T_naive - T_env, 0)
Token_Saved_% = 100 * Token_Saved / max(T_naive, 1)

I_naive = max(1, ceil((T_naive / C_eff) * R_complexity) + G_conflict)
I_env = max(1, ceil((T_env / C_eff) * R_complexity))
Iteration_Saved = max(I_naive - I_env, 0)
Iteration_Saved_% = 100 * Iteration_Saved / max(I_naive, 1)

These are transparent research estimates, not benchmark claims.

## Constant Research Questions Logged For Every Prompt

1. What raw corpus would a naive long-prompt method likely need to load for this task?
2. Which SQLite/package sections did the Env method query or write instead?
3. What token and iteration savings are estimated from selective package retrieval?
4. Did this prompt preserve Env/UOP/project boundaries and the Chat-Lineage-only automatic write law?
5. What proof artifacts make this turn auditable for professor/research review?
