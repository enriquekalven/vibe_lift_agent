-- Cloud Audit Logs (admin activity + data access) for Gemini Enterprise.
-- Ported from gemini-enterprise-stage ds_ge_curated_staging.v_consolidated_audit_log with fixes:
--   * status.code is a google.rpc.Code, not an HTTP status. Stage compared it to 429/403/500,
--     which never matches; status_class maps the rpc codes (8 = RESOURCE_EXHAUSTED, ...).
--   * A missing status means OK: status_code stays NULL and status_message is NULL, not 'NONE'.
--   * Adds the rpc ErrorInfo reason and engine/location parsed from the resource name.
--   * Drops the caller IP (PII not needed for reporting).
--   * Streaming RPCs (StreamAssist) write two DATA_ACCESS entries per call with the same
--     operation.id (operation.first=true and operation.last=true at the same timestamp).
--     Deduplicate by operation.id so each RPC is counted once.
WITH audit AS (
  SELECT 'ADMIN_ACTIVITY' AS audit_log_type, timestamp, trace, spanId, insertId, severity,
         protopayload_auditlog AS pp, operation AS op
  FROM ({{src_audit_activity}})
  UNION ALL
  SELECT 'DATA_ACCESS' AS audit_log_type, timestamp, trace, spanId, insertId, severity,
         protopayload_auditlog AS pp, operation AS op
  FROM ({{src_audit_data_access}})
),
fields AS (
  SELECT
    *,
    LAX_STRING(pp.methodName) AS method_name,
    REGEXP_EXTRACT(LAX_STRING(pp.methodName), r'([^.]+)$') AS method_short_name,
    LAX_STRING(pp.resourceName) AS resource_name,
    COALESCE(LAX_STRING(pp.requestJson), TO_JSON_STRING(pp.request), '') AS request_json_str,
    COALESCE(LAX_STRING(pp.responseJson), TO_JSON_STRING(pp.response), '') AS response_json_str,
    LAX_INT64(pp.status.code) AS status_code,
    LAX_STRING(op.id) AS operation_id,
    COALESCE(LAX_INT64(pp.status.code), 0) = 7 OR EXISTS(
      SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(pp.authorizationInfo)) AS auth
      WHERE NOT COALESCE(LAX_BOOL(auth.granted), FALSE)
    ) AS has_permission_denial
  FROM audit
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY COALESCE(
      LAX_STRING(op.id),
      CONCAT(audit_log_type, ':', COALESCE(insertId, CAST(UNIX_MICROS(timestamp) AS STRING)))
    )
    ORDER BY IF(LAX_BOOL(op.last), 0, 1), COALESCE(LAX_INT64(pp.status.code), 0) DESC, timestamp DESC, insertId DESC
  ) = 1
)
SELECT
  timestamp AS event_timestamp,
  DATE(timestamp) AS event_date,
  CONCAT('AUDIT:', COALESCE(insertId, CAST(UNIX_MICROS(timestamp) AS STRING))) AS event_id,
  audit_log_type,
  severity,
  LOWER(REGEXP_EXTRACT(trace, r'([0-9a-fA-F]{32})$')) AS trace_id,
  NULLIF(spanId, '') AS span_id,
  insertId AS insert_id,
  LAX_STRING(pp.serviceName) AS service_name,
  method_name,
  method_short_name,
  method_short_name IN (
    'StreamAssist', 'Assist', 'AsyncAssist', 'ReadAsyncAssist', 'AnswerQuery', 'Search',
    'ExecuteUiWidgetAction', 'UploadSessionFile', 'DownloadSessionFile',
    'ListSessionFileMetadata', 'AddContextFile'
  ) AS is_interactive,
  CASE
    WHEN method_short_name IN ('StreamAssist', 'Assist', 'AsyncAssist', 'ReadAsyncAssist', 'AnswerQuery') THEN 'CHAT'
    WHEN method_short_name = 'Search' THEN 'SEARCH'
    WHEN method_short_name = 'ExecuteUiWidgetAction' THEN 'WIDGET_ACTION'
    WHEN method_short_name IN ('UploadSessionFile', 'DownloadSessionFile', 'ListSessionFileMetadata', 'AddContextFile') THEN 'FILE_UPLOAD'
  END AS interaction_kind,
  resource_name,
  COALESCE(
    REGEXP_EXTRACT(resource_name, r'^projects/([^/]+)'),
    REGEXP_EXTRACT(request_json_str, r'projects/([^/"]+)')
  ) AS project_ref,
  COALESCE(
    REGEXP_EXTRACT(resource_name, r'/locations/([^/]+)'),
    REGEXP_EXTRACT(request_json_str, r'/locations/([^/"]+)')
  ) AS location,
  COALESCE(
    REGEXP_EXTRACT(resource_name, r'/engines/([^/]+)'),
    REGEXP_EXTRACT(request_json_str, r'/engines/([^/"]+)')
  ) AS engine_id,
  NULLIF(COALESCE(
    REGEXP_EXTRACT(resource_name, r'sessions/([^/]+)'),
    REGEXP_EXTRACT(request_json_str, r'sessions/([^/"]+)'),
    REGEXP_EXTRACT(response_json_str, r'sessions/([^/"]+)')
  ), '-') AS session_id,
  COALESCE(
    REGEXP_EXTRACT(resource_name, r'agents/([^/]+)'),
    REGEXP_EXTRACT(request_json_str, r'agents/([^/"]+)')
  ) AS agent_id,
  status_code,
  COALESCE(status_code, 0) != 0 OR has_permission_denial AS is_error,
  CASE
    WHEN status_code = 7 OR (COALESCE(status_code, 0) = 0 AND has_permission_denial) THEN 'PERMISSION_DENIED'
    WHEN status_code IS NULL OR status_code = 0 THEN 'OK'
    WHEN status_code = 8 THEN 'RATE_LIMITED'
    WHEN status_code = 16 THEN 'UNAUTHENTICATED'
    WHEN status_code = 1 THEN 'CANCELLED'
    WHEN status_code IN (2, 4, 13, 14, 15) THEN 'SERVER_ERROR'
    WHEN status_code IN (3, 5, 6, 9, 10, 11, 12) THEN 'CLIENT_ERROR'
    ELSE 'OTHER_ERROR'
  END AS status_class,
  NULLIF(LAX_STRING(pp.status.message), '') AS status_message,
  COALESCE(
    LAX_STRING(pp.status.details_google_rpc_errorinfo[0].reason),
    LAX_STRING(pp.status.details_google_rpc_errorinfo.reason)
  ) AS error_reason,
  REGEXP_REPLACE(LAX_STRING(pp.authenticationInfo.principalEmail), r'^(user|serviceAccount):', '') AS principal_email,
  COALESCE(
    (
      SELECT LAX_STRING(auth.permission)
      FROM UNNEST(JSON_QUERY_ARRAY(pp.authorizationInfo)) AS auth
      WHERE NOT COALESCE(LAX_BOOL(auth.granted), FALSE)
      LIMIT 1
    ),
    LAX_STRING(pp.authorizationInfo[0].permission)
  ) AS permission_name,
  has_permission_denial,
  LAX_STRING(pp.requestMetadata.callerSuppliedUserAgent) AS caller_user_agent
FROM fields
