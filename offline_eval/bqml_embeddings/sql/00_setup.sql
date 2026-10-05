-- §5.5 BQML embeddings evaluation — 00: dataset + remote model (one-time).
-- Offline only: nothing under app/ calls any of this (.specify/spec.md §5.5).
--
-- Prerequisite (GCP infra, provisioned outside application code, see README):
-- a Cloud resource connection {{connection}} whose service account has
-- roles/aiplatform.user.

CREATE SCHEMA IF NOT EXISTS `{{eval}}`
  OPTIONS (location = '{{location}}',
           description = 'Route-Optimization spec §5.5: address-embedding evaluation (offline)');

CREATE OR REPLACE MODEL `{{eval}}.text_embedding_005`
  REMOTE WITH CONNECTION `{{connection}}`
  OPTIONS (ENDPOINT = 'text-embedding-005');
