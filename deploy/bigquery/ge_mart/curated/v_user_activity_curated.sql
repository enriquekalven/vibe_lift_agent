-- Gemini Enterprise user activity: assistant (StreamAssist, ModelArmorAudit, ...) and search.
-- Ported from gemini-enterprise-stage ds_ge_curated_staging.v_user_activity_curated with fixes:
--   * JSON paths use the sink's real lowercase field names (logmetadata.methodname, not
--     logMetadata.methodName, which is always NULL and made every row look like StreamAssist).
--   * No invented defaults: missing values stay NULL (no 'ANONYMOUS_USER', 'Core Assistant',
--     'COMPLETED', 'STANDALONE_SESSION', '—', 0 documents).
--   * Searches are not sessions; session_id is NULL unless the log carries one.
--   * ModelArmorAudit rows are guardrail evidence (GUARDRAIL_AUDIT), never user turns.
--   * No prompt/response text and no file names; only sizes, counts and mime types.
--   * Targeted JSON paths instead of regex over the whole serialized payload.
-- Rendered by deploy/bigquery/provision_ge_mart.py, which fills the source and dataset placeholders.
WITH assistant AS (
  SELECT
    timestamp AS event_timestamp,
    LOWER(REGEXP_EXTRACT(trace, r'([0-9a-fA-F]{32})$')) AS trace_id,
    NULLIF(spanId, '') AS span_id,
    insertId AS insert_id,
    severity,
    jsonPayload AS jp,
    resource AS res
  FROM ({{src_assistant}})
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY COALESCE(insertId, CAST(UNIX_MICROS(timestamp) AS STRING))
    ORDER BY timestamp DESC
  ) = 1
),
assistant_fields AS (
  SELECT
    *,
    LAX_STRING(jp.logmetadata.methodname) AS api_method,
    COALESCE(LAX_STRING(jp.logmetadata.name), LAX_STRING(jp.request.name)) AS resource_path,
    COALESCE(LAX_STRING(jp.response.session), LAX_STRING(jp.request.session)) AS session_ref,
    LAX_STRING(jp.response.answer.name) AS answer_name,
    LAX_INT64(jp.status.code) AS status_code,
    TO_JSON_STRING(jp.request) AS request_str
  FROM assistant
),
search AS (
  SELECT
    timestamp AS event_timestamp,
    LOWER(REGEXP_EXTRACT(trace, r'([0-9a-fA-F]{32})$')) AS trace_id,
    NULLIF(spanId, '') AS span_id,
    insertId AS insert_id,
    severity,
    jsonPayload AS jp,
    resource AS res
  FROM ({{src_search}})
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY COALESCE(insertId, CAST(UNIX_MICROS(timestamp) AS STRING))
    ORDER BY timestamp DESC
  ) = 1
),
search_fields AS (
  SELECT
    *,
    LAX_STRING(jp.logmetadata.methodname) AS api_method,
    COALESCE(LAX_STRING(jp.request.servingconfig), LAX_STRING(jp.logmetadata.name)) AS resource_path,
    LAX_STRING(jp.request.session) AS session_ref,
    LAX_INT64(jp.status.code) AS status_code
  FROM search
),
unioned AS (
  SELECT
    event_timestamp,
    'ASSISTANT' AS source_log,
    trace_id,
    span_id,
    insert_id,
    severity,
    api_method,
    CASE
      WHEN api_method = 'ModelArmorAudit' THEN 'GUARDRAIL_AUDIT'
      WHEN REGEXP_CONTAINS(COALESCE(api_method, ''), r'(?i)widget') THEN 'WIDGET_ACTION'
      WHEN REGEXP_CONTAINS(COALESCE(api_method, ''), r'(?i)(upload|file)') THEN 'FILE_UPLOAD'
      WHEN api_method IN ('StreamAssist', 'Assist', 'AnswerQuery') THEN 'CHAT'
      ELSE 'OTHER'
    END AS activity_category,
    resource_path,
    res,
    NULLIF(COALESCE(
      REGEXP_EXTRACT(session_ref, r'sessions/([^/]+)'),
      IF(STRPOS(session_ref, '/') = 0, session_ref, NULL),
      REGEXP_EXTRACT(answer_name, r'sessions/([^/]+)'),
      REGEXP_EXTRACT(resource_path, r'sessions/([^/]+)')
    ), '-') AS session_id,
    COALESCE(LAX_STRING(jp.response.assisttoken), LAX_STRING(jp.assisttoken)) AS assist_token,
    LAX_STRING(jp.response.attributiontoken) AS attribution_token,
    IF(
      REGEXP_CONTAINS(COALESCE(LAX_STRING(jp.useriamprincipal), ''), r'@'),
      REGEXP_REPLACE(LAX_STRING(jp.useriamprincipal), r'^(user|serviceAccount):', ''),
      NULL
    ) AS user_email,
    LAX_STRING(jp.response.agentinfo.displayname) AS agent_display_name,
    COALESCE(
      REGEXP_EXTRACT(LAX_STRING(jp.response.agentinfo.agent), r'agents/([^/]+)$'),
      LAX_STRING(jp.response.agentinfo.agent),
      LAX_STRING(jp.request.agentsspec.agentspecs[0].agentid),
      NULLIF(LAX_STRING(res.labels.agent_id), 'core_assistant')
    ) AS agent_id,
    LAX_STRING(jp.response.agentinfo.agentkind) AS agent_kind,
    LAX_BOOL(jp.response.agentinfo.coreassistant) AS is_core_assistant,
    COALESCE(
      LAX_STRING(jp.response.modelinfo.model),
      LAX_STRING(jp.response.modelinfo.requestedmodel)
    ) AS model_name,
    LAX_STRING(jp.response.modelinfo.modelselectionmode) AS model_selection_mode,
    LAX_STRING(jp.response.datasourceinfo.selectionmode) AS datastore_selection_mode,
    ARRAY_LENGTH(JSON_QUERY_ARRAY(jp.response.datasourceinfo.datastores)) AS datastore_count,
    LAX_STRING(jp.response.answer.state) AS answer_state,
    ARRAY_TO_STRING(ARRAY(
      SELECT LAX_STRING(r) FROM UNNEST(JSON_QUERY_ARRAY(jp.response.answer.assistskippedreasons)) AS r
    ), ',') AS skipped_reasons,
    IF(jp.response.answer IS NULL, NULL, (
      SELECT COUNT(1)
      FROM UNNEST(JSON_QUERY_ARRAY(jp.response.answer.replies)) AS reply,
           UNNEST(JSON_QUERY_ARRAY(reply.groundedcontent.textgroundingmetadata.references)) AS ref
    )) AS reference_count,
    (
      SELECT SUM(LENGTH(LAX_STRING(p.text)))
      FROM UNNEST(JSON_QUERY_ARRAY(jp.request.query.parts)) AS p
    ) AS query_chars,
    REGEXP_CONTAINS(COALESCE(api_method, ''), r'(?i)(upload|file)')
      OR NULLIF(LAX_STRING(jp.response.fileid), '') IS NOT NULL
      OR NULLIF(LAX_STRING(jp.request.fileid), '') IS NOT NULL
      OR REGEXP_CONTAINS(COALESCE(request_str, ''), r'"file([nN]ame|[iI]ds?)"') AS has_uploaded_file,
    REGEXP_EXTRACT(request_str, r'"mime[tT]ype"\s*:\s*"([^"]+)"') AS uploaded_file_mimetype,
    SAFE_CAST(SAFE_CAST(REGEXP_EXTRACT(request_str, r'"byte[cC]ount"\s*:\s*"?([0-9.eE+]+)') AS FLOAT64) AS INT64) AS uploaded_file_byte_size,
    SUBSTR(COALESCE(
      NULLIF(CONCAT(
        IFNULL(ARRAY_TO_STRING(ARRAY(
          SELECT LAX_STRING(p.text) FROM UNNEST(JSON_QUERY_ARRAY(jp.request.query.parts)) AS p WITH OFFSET o
          WHERE LAX_STRING(p.text) IS NOT NULL ORDER BY o
        ), '\n'), ''),
        IF(
          NULLIF(LAX_STRING(jp.request.actionexecutionparams.actionname), '') IS NOT NULL,
          CONCAT(
            '\n[Confirmed Action: ',
            LAX_STRING(jp.request.actionexecutionparams.actionname),
            IF(JSON_TYPE(jp.request.actionexecutionparams.args) NOT IN ('null'), CONCAT(' ', TO_JSON_STRING(jp.request.actionexecutionparams.args)), ''),
            ']'
          ),
          ''
        )
      ), ''),
      IF(
        api_method = 'UploadSessionFile',
        IF(
          NULLIF(LAX_STRING(jp.response.fileid), '') IS NOT NULL,
          CONCAT('[File Upload Completed — fileId: ', LAX_STRING(jp.response.fileid), ']'),
          '[File Upload Initiated]'
        ),
        NULL
      )
    ), 1, 2000) AS prompt_preview,
    SUBSTR(COALESCE(
      NULLIF(TRIM(LAX_STRING(jp.servicetextreply)), ''),
      NULLIF(ARRAY_TO_STRING(ARRAY(
        SELECT LAX_STRING(r.groundedcontent.content.text) FROM UNNEST(JSON_QUERY_ARRAY(jp.response.answer.replies)) AS r WITH OFFSET o
        WHERE LAX_STRING(r.groundedcontent.content.text) IS NOT NULL ORDER BY o
      ), ''), ''),
      IF(
        api_method = 'UploadSessionFile',
        IF(
          NULLIF(LAX_STRING(jp.response.fileid), '') IS NOT NULL,
          CONCAT('Uploaded session file ID: ', LAX_STRING(jp.response.fileid)),
          'Session file upload request received'
        ),
        NULL
      )
    ), 1, 2000) AS response_preview,
    NULLIF(ARRAY_TO_STRING(ARRAY(
      SELECT DISTINCT f FROM UNNEST(ARRAY_CONCAT(
        IFNULL(REGEXP_EXTRACT_ALL(COALESCE(request_str, ''), r'"file[nN]ame"\s*:\s*"([^"]+)"'), CAST([] AS ARRAY<STRING>)),
        IF(NULLIF(LAX_STRING(jp.response.fileid), '') IS NOT NULL, [LAX_STRING(jp.response.fileid)], CAST([] AS ARRAY<STRING>)),
        IF(NULLIF(LAX_STRING(jp.request.fileid), '') IS NOT NULL, [LAX_STRING(jp.request.fileid)], CAST([] AS ARRAY<STRING>))
      )) AS f
      WHERE f IS NOT NULL AND f != ''
      ORDER BY f
    ), ', '), '') AS uploaded_file_names,
    NULLIF(ARRAY_TO_STRING(ARRAY(
      SELECT DISTINCT REGEXP_REPLACE(LAX_STRING(ds), r'^projects/[^/]+/locations/[^/]+/collections/[^/]+/dataStores/', '')
      FROM UNNEST(JSON_QUERY_ARRAY(jp.response.datasourceinfo.datastores)) AS ds
      WHERE NULLIF(LAX_STRING(ds), '') IS NOT NULL
      ORDER BY 1
    ), ', '), '') AS queried_data_stores,
    NULLIF(ARRAY_TO_STRING(ARRAY(
      SELECT DISTINCT COALESCE(
        NULLIF(LAX_STRING(ref.documentmetadata.title), ''),
        NULLIF(LAX_STRING(ref.documentmetadata.uri), ''),
        NULLIF(LAX_STRING(ref.documentmetadata.domain), '')
      )
      FROM UNNEST(JSON_QUERY_ARRAY(jp.response.answer.replies)) AS reply,
           UNNEST(JSON_QUERY_ARRAY(reply.groundedcontent.textgroundingmetadata.references)) AS ref
      WHERE COALESCE(
        NULLIF(LAX_STRING(ref.documentmetadata.title), ''),
        NULLIF(LAX_STRING(ref.documentmetadata.uri), ''),
        NULLIF(LAX_STRING(ref.documentmetadata.domain), '')
      ) IS NOT NULL
      LIMIT 15
    ), ' | '), '') AS citation_sources,
    LAX_STRING(jp.response.sanitizationresult.sanitizationverdict) AS sanitization_verdict_raw,
    LAX_STRING(jp.response.sanitizationresult.sanitizationverdictreason) AS sanitization_verdict_reason,
    LAX_STRING(jp.response.sanitizationresult.filtermatchstate) AS sanitization_filter_match_state,
    status_code,
    NULLIF(LAX_STRING(jp.status.message), '') AS status_message
  FROM assistant_fields

  UNION ALL

  SELECT
    event_timestamp,
    'SEARCH' AS source_log,
    trace_id,
    span_id,
    insert_id,
    severity,
    api_method,
    'SEARCH' AS activity_category,
    resource_path,
    res,
    NULLIF(COALESCE(
      REGEXP_EXTRACT(session_ref, r'sessions/([^/]+)'),
      IF(STRPOS(session_ref, '/') = 0, session_ref, NULL),
      REGEXP_EXTRACT(resource_path, r'sessions/([^/]+)')
    ), '-') AS session_id,
    CAST(NULL AS STRING) AS assist_token,
    LAX_STRING(jp.response.attributiontoken) AS attribution_token,
    IF(
      REGEXP_CONTAINS(COALESCE(LAX_STRING(jp.useriamprincipal), ''), r'@'),
      REGEXP_REPLACE(LAX_STRING(jp.useriamprincipal), r'^(user|serviceAccount):', ''),
      NULL
    ) AS user_email,
    CAST(NULL AS STRING) AS agent_display_name,
    CAST(NULL AS STRING) AS agent_id,
    CAST(NULL AS STRING) AS agent_kind,
    CAST(NULL AS BOOL) AS is_core_assistant,
    CAST(NULL AS STRING) AS model_name,
    CAST(NULL AS STRING) AS model_selection_mode,
    CAST(NULL AS STRING) AS datastore_selection_mode,
    CAST(NULL AS INT64) AS datastore_count,
    CAST(NULL AS STRING) AS answer_state,
    CAST(NULL AS STRING) AS skipped_reasons,
    IF(jp.response.results IS NULL, NULL, ARRAY_LENGTH(JSON_QUERY_ARRAY(jp.response.results))) AS reference_count,
    LENGTH(LAX_STRING(jp.request.query)) AS query_chars,
    FALSE AS has_uploaded_file,
    CAST(NULL AS STRING) AS uploaded_file_mimetype,
    CAST(NULL AS INT64) AS uploaded_file_byte_size,
    SUBSTR(NULLIF(LAX_STRING(jp.request.query), ''), 1, 2000) AS prompt_preview,
    CASE
      WHEN IFNULL(ARRAY_LENGTH(JSON_QUERY_ARRAY(jp.response.results)), 0) + IFNULL(ARRAY_LENGTH(JSON_QUERY_ARRAY(jp.response.oneboxresults)), 0) > 0
        THEN CONCAT(
          'Returned ',
          CAST(IFNULL(ARRAY_LENGTH(JSON_QUERY_ARRAY(jp.response.results)), 0) AS STRING),
          ' search result(s)',
          IF(
            IFNULL(ARRAY_LENGTH(JSON_QUERY_ARRAY(jp.response.oneboxresults)), 0) > 0,
            CONCAT(' and ', CAST(ARRAY_LENGTH(JSON_QUERY_ARRAY(jp.response.oneboxresults)) AS STRING), ' connector/onebox group(s)'),
            ''
          )
        )
      WHEN JSON_TYPE(jp.response) NOT IN ('null') THEN '0 search results returned'
      ELSE CAST(NULL AS STRING)
    END AS response_preview,
    CAST(NULL AS STRING) AS uploaded_file_names,
    NULLIF(ARRAY_TO_STRING(ARRAY(
      SELECT DISTINCT ds
      FROM UNNEST(IFNULL(REGEXP_EXTRACT_ALL(TO_JSON_STRING(jp.response), r'dataStores/([A-Za-z0-9_-]+)'), CAST([] AS ARRAY<STRING>))) AS ds
      WHERE ds IS NOT NULL AND ds != ''
      ORDER BY ds
    ), ', '), '') AS queried_data_stores,
    NULLIF(ARRAY_TO_STRING(ARRAY(
      SELECT doc_title
      FROM (
        SELECT CONCAT('Document ', LAX_STRING(r.id)) AS doc_title
        FROM UNNEST(JSON_QUERY_ARRAY(jp.response.results)) AS r
        WHERE LAX_STRING(r.id) IS NOT NULL
        UNION ALL
        SELECT REGEXP_REPLACE(LAX_STRING(sr.document.name), r'^projects/[^/]+/locations/[^/]+/collections/[^/]+/', '') AS doc_title
        FROM UNNEST(JSON_QUERY_ARRAY(jp.response.oneboxresults)) AS ob,
             UNNEST(JSON_QUERY_ARRAY(ob.searchresults)) AS sr
        WHERE LAX_STRING(sr.document.name) IS NOT NULL
      )
      LIMIT 15
    ), ' | '), '') AS citation_sources,
    CAST(NULL AS STRING) AS sanitization_verdict_raw,
    CAST(NULL AS STRING) AS sanitization_verdict_reason,
    CAST(NULL AS STRING) AS sanitization_filter_match_state,
    status_code,
    NULLIF(LAX_STRING(jp.status.message), '') AS status_message
  FROM search_fields
)
SELECT
  event_timestamp,
  DATE(event_timestamp) AS event_date,
  CONCAT(source_log, ':', COALESCE(insert_id, CAST(UNIX_MICROS(event_timestamp) AS STRING))) AS event_id,
  source_log,
  trace_id,
  span_id,
  insert_id,
  severity,
  api_method,
  activity_category,
  activity_category IN ('CHAT', 'SEARCH', 'FILE_UPLOAD', 'WIDGET_ACTION') AS is_user_turn,
  session_id,
  assist_token,
  attribution_token,
  user_email,
  COALESCE(
    REGEXP_EXTRACT(resource_path, r'^projects/([^/]+)'),
    LAX_STRING(res.labels.project_id),
    REGEXP_EXTRACT(LAX_STRING(res.labels.resource_container), r'([0-9]+)$')
  ) AS project_ref,
  COALESCE(
    REGEXP_EXTRACT(resource_path, r'/locations/([^/]+)'),
    LAX_STRING(res.labels.location)
  ) AS location,
  COALESCE(
    REGEXP_EXTRACT(resource_path, r'/engines/([^/]+)'),
    LAX_STRING(res.labels.engine_id)
  ) AS engine_id,
  agent_display_name,
  agent_id,
  agent_kind,
  is_core_assistant,
  model_name,
  model_selection_mode,
  datastore_selection_mode,
  datastore_count,
  answer_state,
  NULLIF(skipped_reasons, '') AS skipped_reasons,
  reference_count,
  query_chars,
  has_uploaded_file,
  uploaded_file_mimetype,
  uploaded_file_byte_size,
  prompt_preview,
  response_preview,
  uploaded_file_names,
  queried_data_stores,
  citation_sources,
  REGEXP_REPLACE(sanitization_verdict_raw, r'^MODEL_ARMOR_SANITIZATION_VERDICT_', '') AS sanitization_verdict,
  REGEXP_CONTAINS(sanitization_verdict_raw, r'(BLOCK|REDACT)$') AS is_guardrail_block,
  sanitization_filter_match_state = 'MATCH_FOUND' AS is_guardrail_policy_match,
  sanitization_verdict_reason,
  status_code,
  -- google.rpc.Code classes (activity status codes are rpc codes, not HTTP).
  CASE
    WHEN status_code IS NULL OR status_code = 0 THEN NULL
    WHEN status_code = 8 THEN 'RATE_LIMITED'
    WHEN status_code = 7 THEN 'PERMISSION_DENIED'
    WHEN status_code = 16 THEN 'UNAUTHENTICATED'
    WHEN status_code = 1 THEN 'CANCELLED'
    WHEN status_code IN (2, 4, 13, 14, 15) THEN 'SERVER_ERROR'
    WHEN status_code IN (3, 5, 6, 9, 10, 11, 12) THEN 'CLIENT_ERROR'
    ELSE 'OTHER_ERROR'
  END AS status_class,
  status_message
FROM unioned
