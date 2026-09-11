# Security and data handling

The application is for the current user's own logged-in desktop WeChat account. Database access is read-only and queries disposable DB/WAL snapshots. Windows initialization reads only current-user WeChat process memory; macOS uses a pinned, patched local helper. Neither normal report generation nor offline import modifies the source database.

Runtime data includes plaintext exported messages, decoded attachments, report evidence and private account identifiers. Keep the runtime directory private. File mode 0600 is used where supported; Windows access also depends on the user's profile directory ACL. Do not put runtime directories in public repositories or public hosting.

Database/media keys stay local. API credentials are read through environment variables. Codex authentication is handled by its official CLI. Cloud modes send selected message/reply text and selected decoded images to the configured provider; aliasing attribution does not remove personal information already present in message bodies or screenshots. Choose a local backend or rules mode when this transfer is inappropriate.

`local_only: true` restricts model endpoints to loopback, disables proxy inheritance for those requests, and refuses remote redirects. A model runner can have its own cloud features: use locally downloaded model weights and review its settings. Rules mode makes no model calls and never silently enables a cloud backend.

All message and image text is treated as data. Model tools are not supplied. Imported image paths must stay within the transcript directory. Results are schema-validated and source IDs checked, but models can still misinterpret source content. Review the generated report before sharing. Shared names have long number sequences removed; this is not complete anonymization.

Taildrop is optional and only sends an explicitly selected, hash-checked file to the chosen target. Uncertain deliveries are not automatically retried. The application does not send messages to arbitrary group members.

Report a vulnerability through GitHub's private vulnerability reporting if available. For a public issue provide a minimal synthetic reproduction and sanitized versions/error categories, never a real database, key config, token or private chat.
