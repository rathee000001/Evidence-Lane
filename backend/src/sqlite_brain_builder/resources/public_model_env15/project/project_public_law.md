# UEPC Env15 Public Project Law

Project template/router and all 14 real sector databases are public-readable.
Chat Lineage is automatic append-only read/write.
The other 13 sectors require explicit named one-turn mutation and immediate relock.

GitHub Code and Local Code preserve both current Code Snapshot and reverse Git Lineage.
History stores exact changed files, patch hunks and route/symbol/dependency/test/artifact impact.

Every successful build and backend-mediated AI mutation creates an immutable hashed snapshot.
Rollback verifies the chosen hash and restores to a new versioned output folder.
The rollback UI belongs only inside System Task Status and opens a version-selection pop-card.
