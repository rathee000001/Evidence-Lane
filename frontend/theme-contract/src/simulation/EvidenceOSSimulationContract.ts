import actionRegistry from "../contracts/t023-action-route-registry.json";

export type EvidencePopup =
  | "new-brain"
  | "search"
  | "pinned"
  | "source-intake"
  | "profile"
  | "settings"
  | "admin"
  | "brain-output"
  | "output-directory"
  | "codex-handoff"
  | "ollama"
  | "refresh"
  | "fuse"
  | "flash"
  | "plan-goal"
  | "version-control"
  | "task-details"
  | "rename-brain"
  | "delete-brain"
  | null;

export type EvidenceUiCommand =
  | "workspace.sidebar.expand"
  | "workspace.sidebar.collapse"
  | "brain.create"
  | "brain.select"
  | "brain.selection.timing.record"
  | "brain.rename"
  | "brain.pin"
  | "brain.unpin"
  | "brain.remove"
  | "brain.deleteToRecycleBin"
  | "chat.create"
  | "chat.select"
  | "chat.rename"
  | "chat.pin"
  | "chat.unpin"
  | "chat.markRead"
  | "workspace.search"
  | "workspace.init"
  | "workspace.outputRoot.view"
  | "workspace.outputRoot.choose"
  | "workspace.outputRoot.updateDefault"
  | "workspace.rootHistory.list"
  | "workspace.rootHistory.add"
  | "workspace.rootHistory.drop"
  | "profile.image.choose"
  | "character.glb.choose"
  | "ollama.executable.choose"
  | "brain.outputRoute.inspect"
  | "source.intake.open"
  | "source.lane.select"
  | "source.lane.customCreate"
  | "source.lane.validate"
  | "source.path.choose"
  | "source.schema.inspect"
  | "source.schema.get"
  | "source.schema.update"
  | "source.schema.reset"
  | "sources.add"
  | "sources.cloneGithub"
  | "sources.remove"
  | "sources.removeLane"
  | "brain.buildAll"
  | "brain.refresh.start"
  | "brain.refresh.fuse"
  | "brain.refresh.status"
  | "brain.delta.list"
  | "brain.delta.get"
  | "brain.planDelta.state"
  | "brain.planDelta.append"
  | "brain.planDelta.disposition"
  | "brain.planGoalPrompt.read"
  | "brain.planStateSlip.append"
  | "brain.telemetry.contract"
  | "brain.telemetry.snapshot"
  | "brain.telemetry.deltas"
  | "brain.telemetry.graph"
  | "brain.telemetry.openTarget"
  | "brain.versions.list"
  | "brain.version.copyHash"
  | "brain.version.rollback"
  | "brain.version.drop"
  | "flash.read"
  | "flash.copy"
  | "folder.open"
  | "package.open"
  | "brain.codexHandoff.status"
  | "brain.codexHandoff.create"
  | "brain.codexHandoff.openFolder"
  | "profile.update"
  | "settings.update"
  | "session.update"
  | "admin.escape"
  | "task.pause"
  | "task.fail"
  | "task.reset"
  | "task.view"
  | "task.output"
  | "web.chatgpt.open"
  | "web.gemini.open"
  | "brains.list"
  | "brains.catalog"
  | "sources.list"
  | "identity.get"
  | "settings.get"
  | "runtime.snapshot"
  | "metrics.snapshot"
  | "process.metrics.snapshot"
  | "pipeline.snapshot"
  | "topology.render"
  | "package.exportChatGPT"
  | "package.exportGemini"
  | "brain.portablePackage.status"
  | "ollama.status"
  | "ollama.launch"
  | "ollama.settings.get"
  | "ollama.settings.update"
  | "codex.status"
  | "codex.launch"
  | "codex.settings.get"
  | "shell.external.launch";

export type EvidenceLaneKind = "github" | "folder" | "file" | "custom" | "brain";

export type EvidenceLaneField = {
  key: string;
  label: string;
  placeholder: string;
  required?: boolean;
  secret?: boolean;
  full?: boolean;
};

export type EvidenceLane = {
  key: string;
  label: string;
  kind: EvidenceLaneKind;
  fileHint: string;
  actionLabel: string;
  fields: EvidenceLaneField[];
  schema: string[];
  laws: string[];
};

export const universalSourceSchema = [
  "source_registry",
  "source_file",
  "source_version",
  "source_chunk",
  "source_fts",
  "relation_edge",
  "ingestion_receipt",
];

export type EvidenceLanePickerPolicy = {
  kind: "remote" | "file" | "folder" | "file-or-folder";
  extensions: readonly string[];
};

export const evidenceLanePickerPolicy: Record<string, EvidenceLanePickerPolicy> = {
  github_code: { kind: "remote", extensions: [] },
  local_code: { kind: "folder", extensions: [] },
  chat_lineage: { kind: "file", extensions: [".docx", ".json", ".jsonl", ".md", ".txt", ".zip"] },
  discussion: { kind: "file", extensions: [".docx", ".md", ".pdf", ".txt"] },
  analysis: { kind: "file", extensions: [".docx", ".md", ".pdf", ".txt"] },
  plan: { kind: "file", extensions: [".docx", ".json", ".md", ".pdf", ".txt"] },
  mode: { kind: "file", extensions: [".docx", ".json", ".md", ".txt"] },
  docs: { kind: "file", extensions: [".doc", ".docx", ".html", ".md", ".odt", ".rst", ".rtf", ".txt", ".xml"] },
  data_excel: { kind: "file", extensions: [".csv", ".json", ".jsonl", ".parquet", ".tsv", ".xls", ".xlsm", ".xlsx"] },
  ppt: { kind: "file", extensions: [".odp", ".ppt", ".pptx"] },
  pdf_ocr: { kind: "file", extensions: [".pdf"] },
  images_ocr: { kind: "file", extensions: [".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"] },
  artifacts: { kind: "file", extensions: [".csv", ".html", ".ipynb", ".json", ".jsonl", ".md", ".mmd", ".parquet", ".svg", ".txt", ".xml"] },
  custom: { kind: "file-or-folder", extensions: [".csv", ".db", ".docx", ".html", ".json", ".jsonl", ".md", ".pdf", ".sqlite", ".sqlite3", ".tsv", ".txt", ".xml", ".yaml", ".yml", ".zip"] },
  brain_loader: { kind: "file-or-folder", extensions: [".db", ".sqlite", ".sqlite3", ".zip"] },
  research: { kind: "file", extensions: [".csv", ".docx", ".json", ".md", ".parquet", ".pdf", ".txt", ".xlsx"] },
  project_engulf: { kind: "file-or-folder", extensions: [".zip"] },
  sqlite_brain: { kind: "file-or-folder", extensions: [".db", ".sqlite", ".sqlite3", ".zip"] },
};

export const evidenceSourceLanes: EvidenceLane[] = [
  {
    key: "github_code", label: "GitHub Code", kind: "github", fileHint: "Repository URL, branch and complete reverse Git history", actionLabel: "Pull Full + Add Code Lane",
    fields: [
      { key: "repo_url", label: "Repository URL", placeholder: "https://github.com/org/repo.git", required: true, full: true },
      { key: "branch", label: "Branch/tag optional", placeholder: "blank = default branch" },
      { key: "folder_name", label: "Local folder name optional", placeholder: "repo folder name" },
      { key: "private_token", label: "Private token optional", placeholder: "used in memory only, never stored", secret: true, full: true },
    ],
    schema: ["git_repository", "git_ref", "git_commit", "git_parent", "git_file_change", "git_hunk", "code_symbol", "app_route", "dependency_manifest", "test_relation"],
    laws: ["Full commit and merge lineage", "Pull existing checkout when safe", "REF_SNAPSHOT_ONLY unless provider/reflog push is proven"],
  },
  {
    key: "local_code", label: "Local Code", kind: "folder", fileHint: "Folder, with .git history when present and synthetic snapshots otherwise", actionLabel: "Inspect + Add Local Code",
    fields: [
      { key: "folder_path", label: "Project folder", placeholder: "D:\\path\\to\\coded-project", required: true, full: true },
      { key: "previous_good", label: "Previous good brain optional", placeholder: "path or immutable brain ID" },
      { key: "git_mode", label: "Git detection", placeholder: "automatic: use .git when present" },
    ],
    schema: ["code_snapshot", "code_file", "code_version", "git_commit", "git_file_change", "synthetic_snapshot_diff", "code_symbol", "app_route", "dependency_manifest", "test_relation"],
    laws: ["Reverse Git history when .git exists", "SNAPSHOT_DIFF_NOT_GIT_HISTORY otherwise", "Topology only after coded lanes are loaded"],
  },
  {
    key: "chat_lineage", label: "Chat Lineage", kind: "file", fileHint: "MD, TXT, JSON or exported chat with exact append-only linkage", actionLabel: "Validate + Append Lineage",
    fields: [{ key: "chat_path", label: "Chat/export file", placeholder: "MD, TXT, JSON or export path", required: true, full: true }, { key: "namespace", label: "Stable chat namespace", placeholder: "project/chat identity" }],
    schema: ["chat_thread", "chat_message", "prompt_output_link", "lineage_event", "chat_attachment", "chat_fts"],
    laws: ["Exact prompt/output/link writeback", "Append-only stable identities", "Zero duplicate FTS rows"],
  },
  {
    key: "discussion", label: "Discussion", kind: "file", fileHint: "Structured decisions, deltas and gates", actionLabel: "Add Discussion",
    fields: [{ key: "discussion_path", label: "Discussion source", placeholder: "file or folder path", required: true, full: true }, { key: "decision_scope", label: "Decision scope", placeholder: "project, feature or gate" }],
    schema: ["discussion", "decision", "decision_delta", "decision_gate", "discussion_relation"], laws: ["Structured decisions", "Deterministic deltas and gates", "Idempotent re-ingestion"],
  },
  {
    key: "analysis", label: "Analysis", kind: "file", fileHint: "Evidence relationships, risks and alternatives", actionLabel: "Add Analysis",
    fields: [{ key: "analysis_path", label: "Analysis source", placeholder: "analysis file or folder", required: true, full: true }, { key: "evidence_scope", label: "Evidence scope", placeholder: "question or evidence set" }],
    schema: ["analysis", "evidence_claim", "risk", "alternative", "analysis_relation"], laws: ["Evidence-linked findings", "Risks and alternatives preserved", "Idempotent re-ingestion"],
  },
  {
    key: "plan", label: "Plan", kind: "file", fileHint: "Phases, milestones, dependencies and transitions", actionLabel: "Add Plan",
    fields: [{ key: "plan_path", label: "Plan source", placeholder: "plan file or folder", required: true, full: true }, { key: "plan_scope", label: "Plan scope", placeholder: "project or milestone" }],
    schema: ["plan", "plan_phase", "milestone", "dependency", "status_transition"], laws: ["Ordered phases", "Explicit dependencies", "Idempotent status history"],
  },
  {
    key: "mode", label: "Mode", kind: "file", fileHint: "Scope, gates, allowed/blocked actions and supersession", actionLabel: "Add Mode Contract",
    fields: [{ key: "mode_path", label: "Mode source", placeholder: "mode file or directive", required: true, full: true }, { key: "mode_name", label: "Mode identity", placeholder: "stable mode name" }],
    schema: ["mode_contract", "scope_relation", "action_gate", "supersession_relation"], laws: ["Allowed and blocked actions", "Named gates", "Explicit supersession"],
  },
  {
    key: "docs", label: "Documents", kind: "file", fileHint: "DOCX structure, styles, tables, links and embedded metadata", actionLabel: "Parse + Add Document",
    fields: [{ key: "document_path", label: "Document file(s)", placeholder: "DOCX or supported text path", required: true, full: true }, { key: "language", label: "Language optional", placeholder: "auto detect" }],
    schema: ["document", "document_section", "heading", "paragraph", "list_item", "doc_table", "doc_cell", "hyperlink", "header_footer", "embedded_image"], laws: ["Structure-linked chunks", "Merged-cell preservation", "Idempotent re-ingestion"],
  },
  {
    key: "data_excel", label: "Data / Excel", kind: "file", fileHint: "Dispatch XLSX/XLSM, CSV/TSV, JSON/JSONL and Parquet by actual type", actionLabel: "Detect Type + Add Data",
    fields: [{ key: "data_path", label: "Data/workbook file(s)", placeholder: "XLSX, XLSM, CSV, TSV, JSON, JSONL or Parquet", required: true, full: true }, { key: "format_override", label: "Format override optional", placeholder: "automatic by actual type" }],
    schema: ["data_source", "workbook", "worksheet", "cell", "formula_dependency", "data_table", "named_range", "chart", "delimited_row", "json_node", "parquet_metadata"], laws: ["Actual-type dispatch", "CSV never registers as workbook", "Zero row growth on identical re-ingestion"],
  },
  {
    key: "ppt", label: "PowerPoint", kind: "file", fileHint: "PPTX slides, shapes, notes, tables and image metadata", actionLabel: "Parse + Add Presentation",
    fields: [{ key: "ppt_path", label: "Presentation file(s)", placeholder: "PPTX path", required: true, full: true }, { key: "slide_range", label: "Slide range optional", placeholder: "all slides" }],
    schema: ["presentation", "slide", "slide_text", "shape", "slide_table", "speaker_note", "slide_image", "slide_relation"], laws: ["Stable slide identities", "Deterministic chunks", "Duplicate-free FTS"],
  },
  {
    key: "pdf_ocr", label: "PDF / OCR", kind: "file", fileHint: "Text, scanned or mixed PDF with OCR review routing", actionLabel: "Inspect + Add PDF",
    fields: [{ key: "pdf_path", label: "PDF file(s)", placeholder: "text, scanned or mixed PDF", required: true, full: true }, { key: "ocr_language", label: "OCR language", placeholder: "eng or configured language" }],
    schema: ["pdf_document", "pdf_page", "text_block", "ocr_block", "image_region", "ocr_confidence", "review_route"], laws: ["Text/scanned/mixed detection", "Confidence retained", "Low-confidence review routing"],
  },
  {
    key: "images_ocr", label: "Images / OCR", kind: "file", fileHint: "Image metadata, regions, lines, confidence and review state", actionLabel: "OCR + Add Images",
    fields: [{ key: "image_path", label: "Image file(s)", placeholder: "PNG, JPEG, TIFF or image folder", required: true, full: true }, { key: "ocr_language", label: "OCR language", placeholder: "eng or configured language" }],
    schema: ["image_source", "image_metadata", "image_region", "ocr_block", "ocr_line", "ocr_confidence", "review_route"], laws: ["Deterministic regions", "Confidence retained", "FTS synchronized without duplicates"],
  },
  {
    key: "artifacts", label: "Artifacts", kind: "file", fileHint: "Governed binary metadata only", actionLabel: "Register Artifact Metadata",
    fields: [{ key: "artifact_path", label: "Artifact file(s)", placeholder: "binary artifact path", required: true, full: true }, { key: "media_type", label: "Media type optional", placeholder: "auto detect" }],
    schema: ["artifact", "artifact_hash", "artifact_metadata", "artifact_relation"], laws: ["Binary metadata only", "No reference UI copied as app truth", "Deterministic relation edges"],
  },
  {
    key: "custom", label: "Custom", kind: "custom", fileHint: "Schema-first deterministic custom ingestion", actionLabel: "Validate Schema + Add Custom",
    fields: [{ key: "schema_name", label: "Schema contract name", placeholder: "stable custom schema ID", required: true }, { key: "custom_path", label: "Source path or pasted contract", placeholder: "file, folder or structured text", required: true, full: true }],
    schema: ["custom_schema", "custom_entity", "custom_field", "custom_relation", "custom_chunk"], laws: ["Schema-first contract", "Deterministic identities", "Idempotent indexing"],
  },
  {
    key: "brain_loader", label: "Brain Loader", kind: "brain", fileHint: "Detect ZIP, folder or SQLite package without silent mutation", actionLabel: "Inspect + Register Brain",
    fields: [{ key: "brain_package", label: "Brain ZIP/folder/SQLite", placeholder: "package or brain path", required: true, full: true }, { key: "import_policy", label: "Import policy", placeholder: "inspect, carry-forward or quarantine" }],
    schema: ["source_brain", "brain_manifest", "brain_database", "brain_table_inventory", "schema_compatibility", "import_decision", "package_pointer"], laws: ["Manifest and .uepc_* pointer inspection", "Hash and source identity proof", "No silent Env/UOP/project mutation"],
  },
  {
    key: "research", label: "Research", kind: "file", fileHint: "Source, question, hypothesis, method, evidence and findings", actionLabel: "Add Research Record",
    fields: [{ key: "research_source", label: "Research source", placeholder: "paper, URL export, dataset or notes", required: true, full: true }, { key: "research_question", label: "Research question", placeholder: "question being tested", required: true, full: true }],
    schema: ["research_source", "research_question", "hypothesis", "method", "evidence", "finding", "limitation", "citation", "open_question", "research_fts"], laws: ["Source pointers retained", "Findings linked to evidence", "Research FTS synchronized"],
  },
  {
    key: "project_engulf", label: "Project Engulf", kind: "folder", fileHint: "Governed project package mapped only into the project sector", actionLabel: "Inspect + Engulf Project",
    fields: [{ key: "project_package", label: "Project package/folder", placeholder: "governed project source", required: true, full: true }, { key: "project_sector", label: "Target project sector", placeholder: "one of the 14 governed sectors", required: true }],
    schema: ["engulf_package", "project_origin", "source_inventory", "schema_mapping", "conflict_quarantine", "accepted_object", "skipped_object", "blocked_object", "topology_update"], laws: ["Project-sector target only", "Conflicts quarantined", "Never promote project content into Env/UOP law"],
  },
  {
    key: "sqlite_brain", label: "SQLite Brain", kind: "brain", fileHint: "Read-only SQLite inventory and compatibility mapping", actionLabel: "Inventory + Add SQLite Brain",
    fields: [{ key: "sqlite_path", label: "SQLite/brain package", placeholder: "database, folder or ZIP path", required: true, full: true }, { key: "query_mode", label: "Query interface", placeholder: "read-only" }],
    schema: ["sqlite_database", "sqlite_table", "sqlite_view", "sqlite_index", "sqlite_trigger", "foreign_key", "fts_table", "schema_hash", "compatibility_status", "project_sector_mapping"], laws: ["Integrity and foreign-key checks", "Read-only query interface", "Pointer and compatibility relations"],
  },
];

export type SimulationReceipt = {
  id: string;
  command: EvidenceUiCommand;
  summary: string;
  timestamp: string;
  mode: "backend" | "ui-only";
  backend_connected: boolean;
  ok: boolean;
  result?: unknown;
  error?: string;
};

export type EvidenceUiAdapter = {
  execute: (command: EvidenceUiCommand, payload?: Record<string, unknown>) => Promise<SimulationReceipt>;
};

export type EvidenceNativeCommandResult = {
  ok: boolean;
  result?: unknown;
  error?: string;
};

declare global {
  interface Window {
    __EVIDENCE_OS_NATIVE_COMMAND_TRANSPORT__?: (
      command: EvidenceUiCommand,
      payload: Record<string, unknown>,
    ) => Promise<EvidenceNativeCommandResult>;
  }
}

export const evidenceActionRegistry = actionRegistry;
const backendCommands = new Set<EvidenceUiCommand>(actionRegistry.backend_commands as EvidenceUiCommand[]);

export function createNativeEvidenceAdapter(onReceipt: (receipt: SimulationReceipt) => void): EvidenceUiAdapter {
  let sequence = 0;
  return {
    async execute(command, payload = {}) {
      sequence += 1;
      const detail = typeof payload.label === "string" ? `: ${payload.label}` : "";
      const base = {
        id: `ui-${String(sequence).padStart(4, "0")}`,
        command,
        summary: `${command}${detail}`,
        timestamp: new Date().toISOString(),
      };
      let receipt: SimulationReceipt;
      if (!backendCommands.has(command)) {
        receipt = { ...base, mode: "ui-only", backend_connected: false, ok: true };
        onReceipt(receipt);
        return receipt;
      }
      try {
        const nativeTransport = window.__EVIDENCE_OS_NATIVE_COMMAND_TRANSPORT__;
        if (!nativeTransport) {
          receipt = {
            ...base,
            mode: "backend",
            backend_connected: false,
            ok: false,
            error: "NATIVE_BACKEND_TRANSPORT_UNAVAILABLE",
          };
          onReceipt(receipt);
          return receipt;
        }
        const body = await nativeTransport(command, payload);
        receipt = {
          ...base,
          mode: "backend",
          backend_connected: body.ok === true,
          ok: body.ok === true,
          result: body.result,
          error: body.ok === true ? undefined : body.error || "NATIVE_BACKEND_COMMAND_FAILED",
        };
      } catch (error) {
        receipt = {
          ...base,
          mode: "backend",
          backend_connected: false,
          ok: false,
          error: String(error instanceof Error ? error.message : error),
        };
      }
      onReceipt(receipt);
      return receipt;
    },
  };
}
