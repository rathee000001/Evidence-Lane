# Chat-Lineage Schema

Core tables: `lineage_head`, `turn_prepare`, `turn_commit`, `prompt_raw_exact`, `prompt_normalized_summary`, `response_raw_visible_exact`, `response_summary`, `visible_reasoning_summary`, `entry_exit_receipt`, `file_link_registry`, `legacy_project_carry_forward`, `gate_evaluation_run`, `operator_activation_run`, `mode_classification_run`, `state_hash_chain`, and `turn_fts`.

The database includes append-only UPDATE/DELETE blockers, monotonic sequence checks, parent-turn validation, FTS synchronization triggers, and a safe parameterized query wrapper.
