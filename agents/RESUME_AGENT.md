# Dedicated resume agent

`ResumeParserAgent.parse` dispatches to `resume_graph.extract_resume` for resumes
only. Each upload owns its LangGraph state and LangChain tools; no paths can be
supplied by the model. The agent chooses and executes inspection, document reading,
structured extraction, evidence validation and finalization tools. A bounded graph
prevents indefinite loops. The existing provider router chooses Qwen 3 1.7B first
and Claude when local inference fails; image-only PDFs use the vision fallback.

PDFs and DOCX tables are read locally. Unknown facts remain null/empty. Source
quotes support section entries; unsupported contact details and entries are
omitted with warnings. Oversized text is rejected rather than silently truncated.
Prompt instructions in documents are treated as untrusted content.

Versioned, validated facts, document digest and timestamped tool trace are stored
in the owner-scoped ResumeUpload JSON record. Main profile updates and the upload
record commit atomically. Nonempty resume facts take precedence over prior profile
values; missing facts do not erase them. Field provenance and separate resume
evidence are retained. Authentication email, GitHub and LinkedIn are not changed.

The Resume sidebar screen shows extracted facts and prior uploads. Its upload
action asks for confirmation before applying changes. Onboarding explicitly labels
upload as updating the profile. Chat document changes retain their existing
proposal/confirmation workflow.

No MCP server is required: these tools need only the authorized upload. Keep
ANTHROPIC_API_KEY in backend configuration for fallback, never frontend code.
