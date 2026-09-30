-- VibeLift turn fact, materialized from v_fct_turns.
-- v_fct_turns joins four curated views over raw JSON log sinks. That takes minutes of slot time
-- per read, too slow for a dashboard that reads the turns several times per refresh. This table is
-- rebuilt in full by `provision_ge_mart.py --refresh` (or a scheduled query running the same DDL).
-- A full rebuild over the lookback window keeps audit matching correct when late logs arrive.
-- refreshed_at records when the rows were built, so readers can show staleness instead of
-- presenting an old snapshot as current (the stage tbl_* tables went stale silently).
SELECT
  *,
  CURRENT_TIMESTAMP() AS refreshed_at
FROM `{{mart}}.v_fct_turns`
