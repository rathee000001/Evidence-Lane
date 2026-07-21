PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS workspace_user(
  user_id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_salt TEXT NOT NULL,
  password_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_settings(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS brain_project(
  brain_id TEXT PRIMARY KEY, brain_name TEXT NOT NULL, brain_slug TEXT NOT NULL,
  output_dir TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brain_session(
  session_id TEXT PRIMARY KEY, brain_id TEXT NOT NULL, title TEXT, created_at TEXT NOT NULL,
  FOREIGN KEY(brain_id) REFERENCES brain_project(brain_id)
);
CREATE TABLE IF NOT EXISTS brain_version(
  version_id TEXT PRIMARY KEY, brain_id TEXT NOT NULL, version_no INTEGER NOT NULL, router_db_path TEXT,
  package_path TEXT, status TEXT, created_at TEXT NOT NULL, FOREIGN KEY(brain_id) REFERENCES brain_project(brain_id)
);
CREATE TABLE IF NOT EXISTS brain_source_event(event_id TEXT PRIMARY KEY, brain_id TEXT, lane TEXT, source_path TEXT, source_hash TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS brain_patch_event(event_id TEXT PRIMARY KEY, brain_id TEXT, description TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS brain_discussion_event(event_id TEXT PRIMARY KEY, brain_id TEXT, text TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS brain_build_event(event_id TEXT PRIMARY KEY, brain_id TEXT, status TEXT, receipt_path TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS brain_package_event(event_id TEXT PRIMARY KEY, brain_id TEXT, package_path TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS brain_last_state_pointer(
  brain_id TEXT PRIMARY KEY, last_session_id TEXT, last_source_event TEXT, last_build_id TEXT,
  last_package_id TEXT, last_chat_lineage_id TEXT, active_sector_id TEXT, active_panel_id TEXT,
  output_folder TEXT, next_suggested_action TEXT, last_known_status TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS recent_brain_index(brain_id TEXT PRIMARY KEY, last_opened_at TEXT);
CREATE TABLE IF NOT EXISTS workspace_capability_profile(profile_id TEXT PRIMARY KEY, profile_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fallback_tool_registry(
  tool_category TEXT PRIMARY KEY, primary_tool TEXT, fallback_tool TEXT, metadata_only_fallback TEXT,
  detected_path TEXT, version TEXT, status TEXT, last_tested TEXT, warning_message TEXT
);
CREATE TABLE IF NOT EXISTS controlled_quarantine_event(
  event_id TEXT PRIMARY KEY, severity TEXT, gate_fired TEXT, message TEXT, receipt_path TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS brain_sector(sector_id TEXT PRIMARY KEY, brain_id TEXT, sector_name TEXT, behavior_type TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS brain_sector_version(version_id TEXT PRIMARY KEY, sector_id TEXT, version_no INTEGER, db_path TEXT, status TEXT, hash_sha256 TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS sector_pointer(sector_id TEXT PRIMARY KEY, active_version_id TEXT, next_pointer TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS sector_hash_state(sector_id TEXT, version_id TEXT, hash_sha256 TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS sector_supersede_ledger(ledger_id TEXT PRIMARY KEY, sector_id TEXT, old_version_id TEXT, new_version_id TEXT, reason TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS sector_dependency_edge(edge_id TEXT PRIMARY KEY, from_sector TEXT, to_sector TEXT, relation_type TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS sector_mmd_registry(mmd_id TEXT PRIMARY KEY, sector_id TEXT, mmd_path TEXT, mmd_hash TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS sector_render_registry(render_id TEXT PRIMARY KEY, sector_id TEXT, render_type TEXT, render_path TEXT, render_hash TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS sector_package_manifest(manifest_id TEXT PRIMARY KEY, brain_id TEXT, package_path TEXT, manifest_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS active_export_manifest(brain_id TEXT PRIMARY KEY, manifest_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS source_active_state(
  active_state_id TEXT PRIMARY KEY, brain_id TEXT, sector_id TEXT, source_id TEXT, source_hash TEXT,
  active_bool INTEGER, state TEXT, reason TEXT, changed_by TEXT, changed_at TEXT
);
CREATE TABLE IF NOT EXISTS unload_session(
  unload_session_id TEXT PRIMARY KEY, brain_id TEXT, started_at TEXT, completed_at TEXT, status TEXT,
  user_note TEXT, affected_sector_count INTEGER, affected_source_count INTEGER, receipt_hash TEXT
);
CREATE TABLE IF NOT EXISTS unload_item(
  unload_item_id TEXT PRIMARY KEY, unload_session_id TEXT, sector_id TEXT, source_id TEXT, file_id TEXT,
  artifact_id TEXT, reason TEXT, impact_status TEXT
);
CREATE TABLE IF NOT EXISTS unload_impact_edge(
  impact_id TEXT PRIMARY KEY, unload_item_id TEXT, from_entity_type TEXT, from_entity_id TEXT,
  affected_entity_type TEXT, affected_entity_id TEXT, relation_type TEXT, severity TEXT
);
CREATE TABLE IF NOT EXISTS sector_rebuild_event(
  rebuild_event_id TEXT PRIMARY KEY, brain_id TEXT, sector_id TEXT, old_sector_version_id TEXT,
  new_sector_version_id TEXT, reason TEXT, started_at TEXT, completed_at TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS active_brain_view(
  brain_id TEXT, sector_id TEXT, active_sector_version_id TEXT, active_source_filter_hash TEXT, created_at TEXT,
  PRIMARY KEY(brain_id, sector_id)
);

-- Section M local identity, settings, project, chat and session schema.
CREATE TABLE IF NOT EXISTS local_identity(
  identity_id TEXT PRIMARY KEY, account_kind TEXT NOT NULL, role TEXT NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS local_profile(
  identity_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, username TEXT,
  email TEXT, avatar_ref TEXT, role_label TEXT NOT NULL, bio TEXT,
  timezone TEXT, presence_state TEXT, status_emoji TEXT, status_message TEXT,
  status_expires_at TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL, FOREIGN KEY(identity_id) REFERENCES local_identity(identity_id)
);
CREATE TABLE IF NOT EXISTS local_setting(
  identity_id TEXT NOT NULL, category TEXT NOT NULL, setting_key TEXT NOT NULL,
  value_type TEXT NOT NULL, value_json TEXT NOT NULL, schema_version INTEGER NOT NULL,
  updated_at TEXT NOT NULL, PRIMARY KEY(identity_id,category,setting_key),
  FOREIGN KEY(identity_id) REFERENCES local_identity(identity_id)
);
CREATE TABLE IF NOT EXISTS local_admin_setting(
  setting_key TEXT PRIMARY KEY, value_type TEXT NOT NULL, value_json TEXT NOT NULL,
  schema_version INTEGER NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS protected_credential_reference(
  credential_id TEXT PRIMARY KEY, identity_id TEXT NOT NULL, service_name TEXT NOT NULL,
  account_name TEXT NOT NULL, target_name TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(identity_id) REFERENCES local_identity(identity_id)
);
CREATE TABLE IF NOT EXISTS endpoint_registry(
  endpoint_id TEXT PRIMARY KEY, identity_id TEXT NOT NULL, display_name TEXT NOT NULL,
  endpoint_type TEXT NOT NULL CHECK(endpoint_type IN (
    'OLLAMA_LOCAL','OPENAI_COMPATIBLE','ENTERPRISE_GATEWAY','CUSTOM_REST','MANUAL_PACKAGE_HANDOFF'
  )),
  base_url TEXT NOT NULL, model_id TEXT NOT NULL, health_check_path TEXT NOT NULL,
  request_path TEXT NOT NULL, model_list_path TEXT, authentication_type TEXT NOT NULL,
  authentication_reference TEXT, streaming_supported INTEGER NOT NULL CHECK(streaming_supported IN (0,1)),
  tool_call_supported INTEGER NOT NULL CHECK(tool_call_supported IN (0,1)),
  file_supported INTEGER NOT NULL CHECK(file_supported IN (0,1)), context_limit INTEGER NOT NULL,
  request_mapping_json TEXT NOT NULL, response_mapping_json TEXT NOT NULL,
  usage_mapping_json TEXT NOT NULL, timeout_seconds INTEGER NOT NULL,
  local_or_remote TEXT NOT NULL CHECK(local_or_remote IN ('LOCAL','REMOTE')),
  privacy_classification TEXT NOT NULL, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
  active_prompt_bar INTEGER NOT NULL DEFAULT 0 CHECK(active_prompt_bar IN (0,1)),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(identity_id) REFERENCES local_identity(identity_id),
  FOREIGN KEY(authentication_reference) REFERENCES protected_credential_reference(credential_id),
  CHECK(active_prompt_bar=0 OR endpoint_type IN ('OPENAI_COMPATIBLE','ENTERPRISE_GATEWAY','CUSTOM_REST'))
);
CREATE UNIQUE INDEX IF NOT EXISTS endpoint_single_custom_prompt_bar_uq
  ON endpoint_registry(identity_id) WHERE active_prompt_bar=1;
CREATE INDEX IF NOT EXISTS endpoint_registry_enabled_type_idx
  ON endpoint_registry(identity_id,enabled,endpoint_type);
CREATE TABLE IF NOT EXISTS endpoint_connection_config(
  endpoint_id TEXT PRIMARY KEY, connection_mode TEXT NOT NULL CHECK(connection_mode IN ('LOCAL','EXTERNAL')),
  provider TEXT NOT NULL, api_type TEXT NOT NULL CHECK(api_type IN ('CHAT_COMPLETIONS','RESPONSES','OLLAMA','CUSTOM_REST')),
  prefix_id TEXT, model_ids_json TEXT NOT NULL DEFAULT '[]', tags_json TEXT NOT NULL DEFAULT '[]',
  headers_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(endpoint_id) REFERENCES endpoint_registry(endpoint_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS endpoint_model_cache(
  endpoint_id TEXT NOT NULL, raw_model_id TEXT NOT NULL, routed_model_id TEXT NOT NULL,
  model_json TEXT NOT NULL, discovered_at TEXT NOT NULL,
  PRIMARY KEY(endpoint_id,raw_model_id),
  FOREIGN KEY(endpoint_id) REFERENCES endpoint_registry(endpoint_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS endpoint_model_cache_routed_idx
  ON endpoint_model_cache(endpoint_id,routed_model_id);
CREATE TABLE IF NOT EXISTS endpoint_validation_event(
  validation_id TEXT PRIMARY KEY, endpoint_id TEXT NOT NULL, request_url TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PASS','FAIL')), http_status INTEGER,
  model_count INTEGER NOT NULL DEFAULT 0, error_code TEXT, detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS endpoint_validation_event_endpoint_idx
  ON endpoint_validation_event(endpoint_id,created_at DESC);
CREATE TABLE IF NOT EXISTS local_project(
  project_id TEXT PRIMARY KEY, identity_id TEXT NOT NULL, name TEXT NOT NULL,
  parent_project_id TEXT, status TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(identity_id) REFERENCES local_identity(identity_id),
  FOREIGN KEY(parent_project_id) REFERENCES local_project(project_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS local_project_identity_name_uq
  ON local_project(identity_id,name) WHERE status<>'DELETED';
CREATE TABLE IF NOT EXISTS local_chat(
  chat_id TEXT PRIMARY KEY, identity_id TEXT NOT NULL, project_id TEXT,
  brain_name TEXT, title TEXT NOT NULL, status TEXT NOT NULL,
  pinned INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
  last_read_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(identity_id) REFERENCES local_identity(identity_id),
  FOREIGN KEY(project_id) REFERENCES local_project(project_id)
);
CREATE INDEX IF NOT EXISTS local_chat_recent_idx ON local_chat(identity_id,updated_at DESC);
CREATE INDEX IF NOT EXISTS local_chat_pinned_idx ON local_chat(identity_id,pinned,updated_at DESC);
CREATE TABLE IF NOT EXISTS local_message(
  message_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, parent_message_id TEXT,
  sequence_no INTEGER NOT NULL, role TEXT NOT NULL, content_exact TEXT NOT NULL,
  content_sha256 TEXT NOT NULL, status TEXT NOT NULL, usage_json TEXT NOT NULL DEFAULT '{}',
  idempotency_key TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(chat_id,idempotency_key), UNIQUE(chat_id,sequence_no),
  FOREIGN KEY(chat_id) REFERENCES local_chat(chat_id) ON DELETE CASCADE,
  FOREIGN KEY(parent_message_id) REFERENCES local_message(message_id)
);
CREATE TABLE IF NOT EXISTS local_attachment(
  attachment_id TEXT PRIMARY KEY, identity_id TEXT NOT NULL, canonical_path TEXT NOT NULL,
  file_name TEXT NOT NULL, media_type TEXT, byte_size INTEGER NOT NULL,
  sha256 TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(identity_id,canonical_path,sha256),
  FOREIGN KEY(identity_id) REFERENCES local_identity(identity_id)
);
CREATE TABLE IF NOT EXISTS local_message_attachment(
  message_id TEXT NOT NULL, attachment_id TEXT NOT NULL, relation_kind TEXT NOT NULL,
  created_at TEXT NOT NULL, PRIMARY KEY(message_id,attachment_id,relation_kind),
  FOREIGN KEY(message_id) REFERENCES local_message(message_id) ON DELETE CASCADE,
  FOREIGN KEY(attachment_id) REFERENCES local_attachment(attachment_id)
);
CREATE TABLE IF NOT EXISTS local_lineage_event(
  lineage_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, prompt_exact TEXT NOT NULL,
  output_exact TEXT NOT NULL, links_json TEXT NOT NULL, prompt_sha256 TEXT NOT NULL,
  output_sha256 TEXT NOT NULL, links_sha256 TEXT NOT NULL, previous_event_hash TEXT NOT NULL,
  event_hash TEXT NOT NULL UNIQUE, idempotency_key TEXT NOT NULL UNIQUE,
  env15_turn_id TEXT, created_at TEXT NOT NULL,
  FOREIGN KEY(chat_id) REFERENCES local_chat(chat_id)
);
CREATE TABLE IF NOT EXISTS local_session_state(
  identity_id TEXT PRIMARY KEY, active_session_id TEXT NOT NULL,
  selected_project_id TEXT, selected_chat_id TEXT, sidebar_mode TEXT NOT NULL,
  expanded_project_ids_json TEXT NOT NULL, search_open INTEGER NOT NULL,
  search_query TEXT NOT NULL, connection_state TEXT NOT NULL,
  loading_state TEXT NOT NULL, terminal_status TEXT, terminal_error_code TEXT,
  updated_at TEXT NOT NULL, FOREIGN KEY(identity_id) REFERENCES local_identity(identity_id)
);
CREATE TABLE IF NOT EXISTS local_search_document(
  row_id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL, body TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS local_search_fts USING fts5(
  title, body, content='local_search_document', content_rowid='row_id'
);
CREATE TRIGGER IF NOT EXISTS local_search_ai AFTER INSERT ON local_search_document BEGIN
  INSERT INTO local_search_fts(rowid,title,body) VALUES(new.row_id,new.title,new.body);
END;
CREATE TRIGGER IF NOT EXISTS local_search_ad AFTER DELETE ON local_search_document BEGIN
  INSERT INTO local_search_fts(local_search_fts,rowid,title,body) VALUES('delete',old.row_id,old.title,old.body);
END;
CREATE TRIGGER IF NOT EXISTS local_search_au AFTER UPDATE ON local_search_document BEGIN
  INSERT INTO local_search_fts(local_search_fts,rowid,title,body) VALUES('delete',old.row_id,old.title,old.body);
  INSERT INTO local_search_fts(rowid,title,body) VALUES(new.row_id,new.title,new.body);
END;
CREATE TABLE IF NOT EXISTS local_operation_receipt(
  receipt_id TEXT PRIMARY KEY, operation TEXT NOT NULL, entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL, status TEXT NOT NULL, detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
