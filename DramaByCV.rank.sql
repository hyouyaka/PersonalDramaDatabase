--1. Ranking: both platforms, all works
WITH filtered AS (
  SELECT
    cv_name,
    title,
    COALESCE(total_play_count, 0) AS total_play_count
  FROM cv_works
),
ranked AS (
  SELECT
    cv_name,
    title,
    total_play_count,
    ROW_NUMBER() OVER (
      PARTITION BY cv_name
      ORDER BY total_play_count DESC, title
    ) AS rn
  FROM filtered
),
top3 AS (
  SELECT
    cv_name,
    GROUP_CONCAT(title, ' | ') AS top3_titles
  FROM (
    SELECT cv_name, title, rn
    FROM ranked
    WHERE rn <= 3
    ORDER BY cv_name, rn
  )
  GROUP BY cv_name
),
summary AS (
  SELECT
    cv_name,
    SUM(total_play_count) AS total_play_count,
    COUNT(*) AS lead_count
  FROM filtered
  GROUP BY cv_name
),
final_ranked AS (
  SELECT
    cv_name,
    total_play_count,
    lead_count,
    DENSE_RANK() OVER (
      ORDER BY total_play_count DESC, cv_name
    ) AS rank_no
  FROM summary
)
SELECT
  s.rank_no AS '排名',
  s.cv_name AS 'CV名称',
  CASE
    WHEN s.total_play_count >= 100000000 THEN printf('%.2f亿', s.total_play_count / 100000000.0)
    WHEN s.total_play_count >= 10000 THEN printf('%.1f万', s.total_play_count / 10000.0)
    ELSE CAST(s.total_play_count AS TEXT)
  END AS '总播放量',
  s.lead_count AS '主役总数',
  COALESCE(t.top3_titles, '') AS 'Top3标题'
FROM final_ranked s
LEFT JOIN top3 t
  ON t.cv_name = s.cv_name
ORDER BY s.total_play_count DESC, s.cv_name;


--2. Ranking: both platforms, pure-love only
WITH filtered AS (
  SELECT
    cv_name,
    title,
    COALESCE(total_play_count, 0) AS total_play_count
  FROM cv_works
  WHERE genre = '纯爱'
),
ranked AS (
  SELECT
    cv_name,
    title,
    total_play_count,
    ROW_NUMBER() OVER (
      PARTITION BY cv_name
      ORDER BY total_play_count DESC, title
    ) AS rn
  FROM filtered
),
top3 AS (
  SELECT
    cv_name,
    GROUP_CONCAT(title, ' | ') AS top3_titles
  FROM (
    SELECT cv_name, title, rn
    FROM ranked
    WHERE rn <= 3
    ORDER BY cv_name, rn
  )
  GROUP BY cv_name
),
summary AS (
  SELECT
    cv_name,
    SUM(total_play_count) AS total_play_count,
    COUNT(*) AS lead_count
  FROM filtered
  GROUP BY cv_name
),
final_ranked AS (
  SELECT
    cv_name,
    total_play_count,
    lead_count,
    DENSE_RANK() OVER (
      ORDER BY total_play_count DESC, cv_name
    ) AS rank_no
  FROM summary
)
SELECT
  s.rank_no AS '排名',
  s.cv_name AS 'CV名称',
  CASE
    WHEN s.total_play_count >= 100000000 THEN printf('%.2f亿', s.total_play_count / 100000000.0)
    WHEN s.total_play_count >= 10000 THEN printf('%.1f万', s.total_play_count / 10000.0)
    ELSE CAST(s.total_play_count AS TEXT)
  END AS '总播放量',
  s.lead_count AS '主役总数',
  COALESCE(t.top3_titles, '') AS 'Top3标题'
FROM final_ranked s
LEFT JOIN top3 t
  ON t.cv_name = s.cv_name
ORDER BY s.total_play_count DESC, s.cv_name;


--3. Ranking: 猫耳, pure-love only
WITH filtered AS (
  SELECT
    cv_name,
    title,
    COALESCE(total_play_count, 0) AS total_play_count
  FROM cv_works
  WHERE genre = '纯爱'
    AND platform = '猫耳'
),
ranked AS (
  SELECT
    cv_name,
    title,
    total_play_count,
    ROW_NUMBER() OVER (
      PARTITION BY cv_name
      ORDER BY total_play_count DESC, title
    ) AS rn
  FROM filtered
),
top3 AS (
  SELECT
    cv_name,
    GROUP_CONCAT(title, ' | ') AS top3_titles
  FROM (
    SELECT cv_name, title, rn
    FROM ranked
    WHERE rn <= 3
    ORDER BY cv_name, rn
  )
  GROUP BY cv_name
),
summary AS (
  SELECT
    cv_name,
    SUM(total_play_count) AS total_play_count,
    COUNT(*) AS lead_count
  FROM filtered
  GROUP BY cv_name
),
final_ranked AS (
  SELECT
    cv_name,
    total_play_count,
    lead_count,
    DENSE_RANK() OVER (
      ORDER BY total_play_count DESC, cv_name
    ) AS rank_no
  FROM summary
)
SELECT
  s.rank_no AS '排名',
  s.cv_name AS 'CV名称',
  CASE
    WHEN s.total_play_count >= 100000000 THEN printf('%.2f亿', s.total_play_count / 100000000.0)
    WHEN s.total_play_count >= 10000 THEN printf('%.1f万', s.total_play_count / 10000.0)
    ELSE CAST(s.total_play_count AS TEXT)
  END AS '总播放量',
  s.lead_count AS '主役总数',
  COALESCE(t.top3_titles, '') AS 'Top3标题'
FROM final_ranked s
LEFT JOIN top3 t
  ON t.cv_name = s.cv_name
ORDER BY s.total_play_count DESC, s.cv_name;


--4. Ranking: 漫播, pure-love only
WITH filtered AS (
  SELECT
    cv_name,
    title,
    COALESCE(total_play_count, 0) AS total_play_count
  FROM cv_works
  WHERE genre = '纯爱'
    AND platform = '漫播'
),
ranked AS (
  SELECT
    cv_name,
    title,
    total_play_count,
    ROW_NUMBER() OVER (
      PARTITION BY cv_name
      ORDER BY total_play_count DESC, title
    ) AS rn
  FROM filtered
),
top3 AS (
  SELECT
    cv_name,
    GROUP_CONCAT(title, ' | ') AS top3_titles
  FROM (
    SELECT cv_name, title, rn
    FROM ranked
    WHERE rn <= 3
    ORDER BY cv_name, rn
  )
  GROUP BY cv_name
),
summary AS (
  SELECT
    cv_name,
    SUM(total_play_count) AS total_play_count,
    COUNT(*) AS lead_count
  FROM filtered
  GROUP BY cv_name
),
final_ranked AS (
  SELECT
    cv_name,
    total_play_count,
    lead_count,
    DENSE_RANK() OVER (
      ORDER BY total_play_count DESC, cv_name
    ) AS rank_no
  FROM summary
)
SELECT
  s.rank_no AS '排名',
  s.cv_name AS 'CV名称',
  CASE
    WHEN s.total_play_count >= 100000000 THEN printf('%.2f亿', s.total_play_count / 100000000.0)
    WHEN s.total_play_count >= 10000 THEN printf('%.1f万', s.total_play_count / 10000.0)
    ELSE CAST(s.total_play_count AS TEXT)
  END AS '总播放量',
  s.lead_count AS '主役总数',
  COALESCE(t.top3_titles, '') AS 'Top3标题'
FROM final_ranked s
LEFT JOIN top3 t
  ON t.cv_name = s.cv_name
ORDER BY s.total_play_count DESC, s.cv_name;


--5. Ranking: both platforms, exclude audiobook-like works
WITH filtered AS (
  SELECT
    cv_name,
    title,
    COALESCE(total_play_count, 0) AS total_play_count
  FROM cv_works
  WHERE COALESCE(catalog_name, '') <> '有声剧'
),
ranked AS (
  SELECT
    cv_name,
    title,
    total_play_count,
    ROW_NUMBER() OVER (
      PARTITION BY cv_name
      ORDER BY total_play_count DESC, title
    ) AS rn
  FROM filtered
),
top3 AS (
  SELECT
    cv_name,
    GROUP_CONCAT(title, ' | ') AS top3_titles
  FROM (
    SELECT cv_name, title, rn
    FROM ranked
    WHERE rn <= 3
    ORDER BY cv_name, rn
  )
  GROUP BY cv_name
),
summary AS (
  SELECT
    cv_name,
    SUM(total_play_count) AS total_play_count,
    COUNT(*) AS lead_count
  FROM filtered
  GROUP BY cv_name
),
final_ranked AS (
  SELECT
    cv_name,
    total_play_count,
    lead_count,
    DENSE_RANK() OVER (
      ORDER BY total_play_count DESC, cv_name
    ) AS rank_no
  FROM summary
)
SELECT
  s.rank_no AS '排名',
  s.cv_name AS 'CV名称',
  CASE
    WHEN s.total_play_count >= 100000000 THEN printf('%.2f亿', s.total_play_count / 100000000.0)
    WHEN s.total_play_count >= 10000 THEN printf('%.1f万', s.total_play_count / 10000.0)
    ELSE CAST(s.total_play_count AS TEXT)
  END AS '总播放量',
  s.lead_count AS '主役总数',
  COALESCE(t.top3_titles, '') AS 'Top3标题'
FROM final_ranked s
LEFT JOIN top3 t
  ON t.cv_name = s.cv_name
ORDER BY s.total_play_count DESC, s.cv_name;


--6. Ranking: both platforms, pure-love only, exclude audiobook-like works
WITH filtered AS (
  SELECT
    cv_name,
    title,
    COALESCE(total_play_count, 0) AS total_play_count
  FROM cv_works
  WHERE genre = '纯爱'
    AND COALESCE(catalog_name, '') <> '有声剧'
),
ranked AS (
  SELECT
    cv_name,
    title,
    total_play_count,
    ROW_NUMBER() OVER (
      PARTITION BY cv_name
      ORDER BY total_play_count DESC, title
    ) AS rn
  FROM filtered
),
top3 AS (
  SELECT
    cv_name,
    GROUP_CONCAT(title, ' | ') AS top3_titles
  FROM (
    SELECT cv_name, title, rn
    FROM ranked
    WHERE rn <= 3
    ORDER BY cv_name, rn
  )
  GROUP BY cv_name
),
summary AS (
  SELECT
    cv_name,
    SUM(total_play_count) AS total_play_count,
    COUNT(*) AS lead_count
  FROM filtered
  GROUP BY cv_name
),
final_ranked AS (
  SELECT
    cv_name,
    total_play_count,
    lead_count,
    DENSE_RANK() OVER (
      ORDER BY total_play_count DESC, cv_name
    ) AS rank_no
  FROM summary
)
SELECT
  s.rank_no AS '排名',
  s.cv_name AS 'CV名称',
  CASE
    WHEN s.total_play_count >= 100000000 THEN printf('%.2f亿', s.total_play_count / 100000000.0)
    WHEN s.total_play_count >= 10000 THEN printf('%.1f万', s.total_play_count / 10000.0)
    ELSE CAST(s.total_play_count AS TEXT)
  END AS '总播放量',
  s.lead_count AS '主役总数',
  COALESCE(t.top3_titles, '') AS 'Top3标题'
FROM final_ranked s
LEFT JOIN top3 t
  ON t.cv_name = s.cv_name
ORDER BY s.total_play_count DESC, s.cv_name;


--7. Ranking: 猫耳, pure-love only, exclude audio dramas
WITH filtered AS (
  SELECT
    cv_name,
    title,
    COALESCE(total_play_count, 0) AS total_play_count
  FROM cv_works
  WHERE genre = '纯爱'
    AND platform = '猫耳'
    AND COALESCE(catalog_name, '') <> '有声剧'
),
ranked AS (
  SELECT
    cv_name,
    title,
    total_play_count,
    ROW_NUMBER() OVER (
      PARTITION BY cv_name
      ORDER BY total_play_count DESC, title
    ) AS rn
  FROM filtered
),
top3 AS (
  SELECT
    cv_name,
    GROUP_CONCAT(title, ' | ') AS top3_titles
  FROM (
    SELECT cv_name, title, rn
    FROM ranked
    WHERE rn <= 3
    ORDER BY cv_name, rn
  )
  GROUP BY cv_name
),
summary AS (
  SELECT
    cv_name,
    SUM(total_play_count) AS total_play_count,
    COUNT(*) AS lead_count
  FROM filtered
  GROUP BY cv_name
),
final_ranked AS (
  SELECT
    cv_name,
    total_play_count,
    lead_count,
    DENSE_RANK() OVER (
      ORDER BY total_play_count DESC, cv_name
    ) AS rank_no
  FROM summary
)
SELECT
  s.rank_no AS '排名',
  s.cv_name AS 'CV名称',
  CASE
    WHEN s.total_play_count >= 100000000 THEN printf('%.2f亿', s.total_play_count / 100000000.0)
    WHEN s.total_play_count >= 10000 THEN printf('%.1f万', s.total_play_count / 10000.0)
    ELSE CAST(s.total_play_count AS TEXT)
  END AS '总播放量',
  s.lead_count AS '主役总数',
  COALESCE(t.top3_titles, '') AS 'Top3标题'
FROM final_ranked s
LEFT JOIN top3 t
  ON t.cv_name = s.cv_name
ORDER BY s.total_play_count DESC, s.cv_name;


--8. Ranking: 漫播, pure-love only, exclude audiobooks
WITH filtered AS (
  SELECT
    cv_name,
    title,
    COALESCE(total_play_count, 0) AS total_play_count
  FROM cv_works
  WHERE genre = '纯爱'
    AND platform = '漫播'
    AND COALESCE(catalog_name, '') <> '有声剧'
),
ranked AS (
  SELECT
    cv_name,
    title,
    total_play_count,
    ROW_NUMBER() OVER (
      PARTITION BY cv_name
      ORDER BY total_play_count DESC, title
    ) AS rn
  FROM filtered
),
top3 AS (
  SELECT
    cv_name,
    GROUP_CONCAT(title, ' | ') AS top3_titles
  FROM (
    SELECT cv_name, title, rn
    FROM ranked
    WHERE rn <= 3
    ORDER BY cv_name, rn
  )
  GROUP BY cv_name
),
summary AS (
  SELECT
    cv_name,
    SUM(total_play_count) AS total_play_count,
    COUNT(*) AS lead_count
  FROM filtered
  GROUP BY cv_name
),
final_ranked AS (
  SELECT
    cv_name,
    total_play_count,
    lead_count,
    DENSE_RANK() OVER (
      ORDER BY total_play_count DESC, cv_name
    ) AS rank_no
  FROM summary
)
SELECT
  s.rank_no AS '排名',
  s.cv_name AS 'CV名称',
  CASE
    WHEN s.total_play_count >= 100000000 THEN printf('%.2f亿', s.total_play_count / 100000000.0)
    WHEN s.total_play_count >= 10000 THEN printf('%.1f万', s.total_play_count / 10000.0)
    ELSE CAST(s.total_play_count AS TEXT)
  END AS '总播放量',
  s.lead_count AS '主役总数',
  COALESCE(t.top3_titles, '') AS 'Top3标题'
FROM final_ranked s
LEFT JOIN top3 t
  ON t.cv_name = s.cv_name
ORDER BY s.total_play_count DESC, s.cv_name;
