-- The AI report moved from Claude to OpenAI. Keep 'claude' so rows written before the switch stay valid.
ALTER TABLE ai_reports DROP CONSTRAINT ai_reports_source_check;
ALTER TABLE ai_reports ADD CONSTRAINT ai_reports_source_check CHECK (source IN ('openai', 'claude', 'rules'));
