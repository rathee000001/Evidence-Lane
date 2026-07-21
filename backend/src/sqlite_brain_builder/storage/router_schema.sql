PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS brain_manifest(
  brain_id TEXT PRIMARY KEY, brain_name TEXT, brain_slug TEXT, schema_version TEXT, created_at TEXT,
  build_status TEXT, total_sectors INTEGER DEFAULT 0, total_sources INTEGER DEFAULT 0, total_packages INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS brain_section_registry(section_id TEXT PRIMARY KEY, section_name TEXT, section_type TEXT, active_bool INTEGER);
CREATE TABLE IF NOT EXISTS source_registry(source_id TEXT PRIMARY KEY, purpose_lane TEXT, parser_lane TEXT, display_name TEXT, path TEXT, user_brief TEXT, source_hash TEXT, active_bool INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS source_byte_coverage(coverage_id TEXT PRIMARY KEY, source_id TEXT, coverage_status TEXT, bytes_total INTEGER, bytes_accounted INTEGER, reason TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS brain_sector(sector_id TEXT PRIMARY KEY, sector_name TEXT, behavior_type TEXT, active_version_id TEXT);
CREATE TABLE IF NOT EXISTS brain_sector_version(version_id TEXT PRIMARY KEY, sector_id TEXT, version_no INTEGER, db_path TEXT, status TEXT, hash_sha256 TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS sector_pointer(sector_id TEXT PRIMARY KEY, active_version_id TEXT, next_pointer TEXT, pointer_json TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS sector_supersede_ledger(ledger_id TEXT PRIMARY KEY, sector_id TEXT, old_version_id TEXT, new_version_id TEXT, reason TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS active_export_manifest(brain_id TEXT PRIMARY KEY, manifest_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS package_manifest(package_id TEXT PRIMARY KEY, package_path TEXT, package_hash TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS flash_prompt_registry(prompt_id TEXT PRIMARY KEY, prompt_path TEXT, prompt_hash TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS source_structure_signature(signature_id TEXT PRIMARY KEY, source_id TEXT, file_id TEXT, signature_type TEXT, key_metrics_json TEXT, structure_hash TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS extraction_signal(signal_id TEXT PRIMARY KEY, source_id TEXT, file_id TEXT, signal_name TEXT, signal_value TEXT, strength TEXT, tool_source TEXT);
CREATE TABLE IF NOT EXISTS unload_session(unload_session_id TEXT PRIMARY KEY, started_at TEXT, completed_at TEXT, status TEXT, user_note TEXT, receipt_hash TEXT);
CREATE TABLE IF NOT EXISTS unload_item(unload_item_id TEXT PRIMARY KEY, unload_session_id TEXT, sector_id TEXT, source_id TEXT, reason TEXT, impact_status TEXT);
CREATE TABLE IF NOT EXISTS unload_impact_edge(impact_id TEXT PRIMARY KEY, unload_item_id TEXT, affected_entity_type TEXT, affected_entity_id TEXT, relation_type TEXT, severity TEXT);
CREATE TABLE IF NOT EXISTS active_brain_view(brain_id TEXT, sector_id TEXT, active_sector_version_id TEXT, active_source_filter_hash TEXT, created_at TEXT, PRIMARY KEY(brain_id, sector_id));
