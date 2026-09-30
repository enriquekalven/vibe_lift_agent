-- Model Armor sanitize operations, one row per prompt/response check.
-- Ported from gemini-enterprise-stage ds_ge_curated_staging.v_model_armor_curated with fixes:
--   * Verdicts arrive as MODEL_ARMOR_SANITIZATION_VERDICT_BLOCK / _ALLOW. Stage tested
--     IN ('BLOCK','REDACT'), so is_blocked was always FALSE. The prefix is stripped here.
--   * No 'ALLOW' default for a missing verdict and no 'INTERNAL_NETWORK' default IP.
--   * Restores the safety (RAI), CSAM and malicious-URI flags and the check direction.
--   * Drops the prompt text preview and client IP (PII).
--   * trace is always NULL in these logs; the assist token inside the client correlation id
--     ('AS|<assistant path>|<token>') is the reliable join key to activity.
WITH armor AS (
  SELECT
    timestamp AS event_timestamp,
    LOWER(REGEXP_EXTRACT(trace, r'([0-9a-fA-F]{32})$')) AS trace_id,
    insertId AS insert_id,
    jsonpayload_v1_sanitizeoperationlogentry AS ma,
    labels AS lb,
    resource AS res
  FROM ({{src_armor}})
),
fields AS (
  SELECT
    *,
    LAX_STRING(lb.modelarmor_googleapis_com_client_correlation_id) AS correlation_id,
    LAX_STRING(ma.sanitizationresult.sanitizationverdict) AS verdict_raw,
    LAX_STRING(ma.sanitizationresult.filterresults.pi_and_jailbreak.piandjailbreakfilterresult.matchstate) = 'MATCH_FOUND' AS is_prompt_injection,
    LAX_STRING(ma.sanitizationresult.filterresults.sdp.sdpfilterresult.inspectresult.matchstate) = 'MATCH_FOUND'
      OR LAX_STRING(ma.sanitizationresult.filterresults.sdp.sdpfilterresult.deidentifyresult.matchstate) = 'MATCH_FOUND' AS is_sensitive_data,
    LAX_STRING(ma.sanitizationresult.filterresults.rai.raifilterresult.matchstate) = 'MATCH_FOUND' AS is_safety_violation,
    LAX_STRING(ma.sanitizationresult.filterresults.csam.csamfilterfilterresult.matchstate) = 'MATCH_FOUND' AS is_csam,
    LAX_STRING(ma.sanitizationresult.filterresults.malicious_uris.maliciousurifilterresult.matchstate) = 'MATCH_FOUND' AS is_malicious_uri
  FROM armor
)
SELECT
  event_timestamp,
  DATE(event_timestamp) AS event_date,
  CONCAT('ARMOR:', COALESCE(insert_id, CAST(UNIX_MICROS(event_timestamp) AS STRING))) AS event_id,
  trace_id,
  insert_id,
  IF(STARTS_WITH(correlation_id, 'AS|'), NULLIF(SPLIT(correlation_id, '|')[SAFE_OFFSET(2)], ''), NULL) AS assist_token,
  COALESCE(
    LAX_STRING(res.labels.template_id),
    REGEXP_EXTRACT(correlation_id, r'templates/([^/|]+)')
  ) AS template_id,
  LAX_STRING(res.labels.location) AS location,
  COALESCE(
    LAX_STRING(ma.operationtype),
    LAX_STRING(lb.modelarmor_googleapis_com_operation_type)
  ) AS operation_type,
  LAX_STRING(lb.modelarmor_googleapis_com_client_name) AS client_name,
  REGEXP_REPLACE(verdict_raw, r'^MODEL_ARMOR_SANITIZATION_VERDICT_', '') AS sanitization_verdict,
  REGEXP_CONTAINS(verdict_raw, r'(BLOCK|REDACT)$') AS is_blocked,
  LAX_STRING(ma.sanitizationresult.filtermatchstate) = 'MATCH_FOUND' AS is_policy_match,
  COALESCE(is_prompt_injection, FALSE) AS is_prompt_injection,
  COALESCE(is_sensitive_data, FALSE) AS is_sensitive_data,
  COALESCE(is_safety_violation, FALSE) AS is_safety_violation,
  COALESCE(is_csam, FALSE) AS is_csam,
  COALESCE(is_malicious_uri, FALSE) AS is_malicious_uri,
  CASE
    WHEN is_prompt_injection THEN 'PROMPT_INJECTION'
    WHEN is_sensitive_data THEN 'SENSITIVE_DATA'
    WHEN is_safety_violation THEN 'RESPONSIBLE_AI'
    WHEN is_csam THEN 'CSAM'
    WHEN is_malicious_uri THEN 'MALICIOUS_URI'
    WHEN REGEXP_CONTAINS(verdict_raw, r'(BLOCK|REDACT)$') THEN 'OTHER_POLICY'
  END AS violation_category
FROM fields
