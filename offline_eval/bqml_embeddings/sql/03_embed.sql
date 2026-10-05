-- §5.5 — 03: embed corpus rows not embedded yet (incremental, re-runnable).
-- text-embedding-005, task_type SEMANTIC_SIMILARITY (address vs address).
CREATE TABLE IF NOT EXISTS `{{eval}}.embeddings` (
  address_key  STRING NOT NULL,
  content      STRING,
  embedding    ARRAY<FLOAT64>,
  model        STRING,
  embedded_at  TIMESTAMP
)
CLUSTER BY address_key;

INSERT INTO `{{eval}}.embeddings` (address_key, content, embedding, model, embedded_at)
SELECT address_key, content, ml_generate_embedding_result, 'text-embedding-005', CURRENT_TIMESTAMP()
FROM ML.GENERATE_EMBEDDING(
  MODEL `{{eval}}.text_embedding_005`,
  (
    SELECT c.address_key, c.content            -- the input column must be named `content`
    FROM `{{eval}}.corpus` AS c
    LEFT JOIN `{{eval}}.embeddings` AS e
      ON e.address_key = c.address_key AND e.content = c.content
    WHERE e.address_key IS NULL
  ),
  STRUCT('SEMANTIC_SIMILARITY' AS task_type, TRUE AS flatten_json_output)
)
WHERE ml_generate_embedding_status = '';

SELECT COUNT(*) AS still_missing
FROM `{{eval}}.corpus` AS c
LEFT JOIN `{{eval}}.embeddings` AS e
  ON e.address_key = c.address_key AND e.content = c.content
WHERE e.address_key IS NULL;
