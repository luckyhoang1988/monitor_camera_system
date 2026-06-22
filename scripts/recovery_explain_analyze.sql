\timing on

-- Usage example:
-- psql -v start_ts="'2026-06-01 00:00:00+00'" -v end_ts="'2026-06-22 23:59:59+00'" -v area="''" -f scripts/recovery_explain_analyze.sql
--
-- area:
--   ''      -> all areas
--   'Khu A' -> filter one area

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
WITH params AS (
    SELECT
        :'start_ts'::timestamptz AS start_ts,
        :'end_ts'::timestamptz AS end_ts,
        NULLIF(:'area', '')::text AS area
),
nvr_scope AS (
    SELECT d.id AS nvr_id, d.name, d.area
    FROM nvr_devices d
    JOIN params p ON true
    WHERE p.area IS NULL OR d.area = p.area
),
in_range AS (
    SELECT l.nvr_id, l.status, l.checked_at
    FROM nvr_status_logs l
    JOIN nvr_scope s ON s.nvr_id = l.nvr_id
    JOIN params p ON true
    WHERE l.checked_at >= p.start_ts
      AND (p.end_ts IS NULL OR l.checked_at <= p.end_ts)
),
prev_seed_ranked AS (
    SELECT
        l.nvr_id,
        l.status,
        l.checked_at,
        row_number() OVER (
            PARTITION BY l.nvr_id
            ORDER BY l.checked_at DESC
        ) AS rn
    FROM nvr_status_logs l
    JOIN nvr_scope s ON s.nvr_id = l.nvr_id
    JOIN params p ON true
    WHERE l.checked_at < p.start_ts
),
base_logs AS (
    SELECT nvr_id, status, checked_at FROM in_range
    UNION ALL
    SELECT nvr_id, status, checked_at
    FROM prev_seed_ranked
    WHERE rn = 1
),
lagged AS (
    SELECT
        b.nvr_id,
        b.status,
        b.checked_at,
        lag(b.status) OVER (
            PARTITION BY b.nvr_id
            ORDER BY b.checked_at
        ) AS prev_status
    FROM base_logs b
)
SELECT
    l.nvr_id,
    s.name,
    s.area,
    l.checked_at AS recovered_at,
    l.prev_status AS from_status
FROM lagged l
JOIN nvr_scope s ON s.nvr_id = l.nvr_id
JOIN params p ON true
WHERE l.checked_at >= p.start_ts
  AND (p.end_ts IS NULL OR l.checked_at <= p.end_ts)
  AND l.status = 'Online'
  AND l.prev_status IS NOT NULL
  AND l.prev_status <> 'Online'
ORDER BY l.checked_at DESC;

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
WITH params AS (
    SELECT
        :'start_ts'::timestamptz AS start_ts,
        :'end_ts'::timestamptz AS end_ts,
        NULLIF(:'area', '')::text AS area
),
camera_scope AS (
    SELECT
        c.id AS camera_id,
        c.nvr_id,
        d.name AS nvr_name,
        d.area,
        c.channel_no,
        c.name AS camera_name
    FROM camera_channels c
    JOIN nvr_devices d ON d.id = c.nvr_id
    JOIN params p ON true
    WHERE p.area IS NULL OR d.area = p.area
),
in_range AS (
    SELECT l.camera_id, l.status, l.checked_at
    FROM camera_status_logs l
    JOIN camera_scope s ON s.camera_id = l.camera_id
    JOIN params p ON true
    WHERE l.checked_at >= p.start_ts
      AND (p.end_ts IS NULL OR l.checked_at <= p.end_ts)
),
prev_seed_ranked AS (
    SELECT
        l.camera_id,
        l.status,
        l.checked_at,
        row_number() OVER (
            PARTITION BY l.camera_id
            ORDER BY l.checked_at DESC
        ) AS rn
    FROM camera_status_logs l
    JOIN camera_scope s ON s.camera_id = l.camera_id
    JOIN params p ON true
    WHERE l.checked_at < p.start_ts
),
base_logs AS (
    SELECT camera_id, status, checked_at FROM in_range
    UNION ALL
    SELECT camera_id, status, checked_at
    FROM prev_seed_ranked
    WHERE rn = 1
),
lagged AS (
    SELECT
        b.camera_id,
        b.status,
        b.checked_at,
        lag(b.status) OVER (
            PARTITION BY b.camera_id
            ORDER BY b.checked_at
        ) AS prev_status
    FROM base_logs b
)
SELECT
    l.camera_id,
    s.nvr_id,
    s.nvr_name,
    s.area,
    s.channel_no,
    s.camera_name,
    l.checked_at AS recovered_at,
    l.prev_status AS from_status
FROM lagged l
JOIN camera_scope s ON s.camera_id = l.camera_id
JOIN params p ON true
WHERE l.checked_at >= p.start_ts
  AND (p.end_ts IS NULL OR l.checked_at <= p.end_ts)
  AND l.status = 'Online'
  AND l.prev_status IS NOT NULL
  AND l.prev_status <> 'Online'
ORDER BY l.checked_at DESC;
