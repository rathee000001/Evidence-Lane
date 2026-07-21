PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS image_file(image_id TEXT PRIMARY KEY, file_id TEXT, path TEXT, width INTEGER, height INTEGER, format TEXT, mode TEXT, raw_file_sha256 TEXT, ocr_status TEXT, review_status TEXT);
CREATE TABLE IF NOT EXISTS image_metadata(metadata_id TEXT PRIMARY KEY, image_id TEXT, metadata_json TEXT);
CREATE TABLE IF NOT EXISTS image_ocr_run(ocr_run_id TEXT PRIMARY KEY, image_id TEXT, tool_name TEXT, tool_version TEXT, started_at TEXT, ended_at TEXT, status TEXT, confidence_avg REAL, error_message TEXT);
CREATE TABLE IF NOT EXISTS image_ocr_block(ocr_block_id TEXT PRIMARY KEY, ocr_run_id TEXT, image_id TEXT, block_order INTEGER, bbox_x INTEGER, bbox_y INTEGER, bbox_w INTEGER, bbox_h INTEGER, text TEXT, confidence REAL, text_sha256 TEXT, review_required_bool INTEGER);
CREATE TABLE IF NOT EXISTS image_ocr_line(ocr_line_id TEXT PRIMARY KEY, ocr_block_id TEXT, line_order INTEGER, text TEXT, line_sha256 TEXT, confidence REAL);
CREATE TABLE IF NOT EXISTS image_review_region(region_id TEXT PRIMARY KEY, image_id TEXT, reason TEXT, bbox_json TEXT, created_at TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS image_ocr_fts USING fts5(entity_type, entity_id, image_id, sha256, text);
