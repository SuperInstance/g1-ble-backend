-- Proposed decisions-ledger schema for the G1 relay agent.
--
-- Produced by the learning-substrate review panel. Every view and query in
-- documents/07-panel-learning-substrate.md was executed against SQLite 3.46.1;
-- these are real computed outputs, not illustrations.
--
-- Design properties:
--   * Additive. No change to the existing `cards` or `events` tables.
--   * Reversible. DROP TABLE returns the system to today's behaviour, losing
--     nothing.
--   * Backfillable. Historical cards can be imported as unlabelled decisions.
--
-- The point of the split is that `pos`/`neg` (explicit human labels) are the
-- ONLY writer of ground truth. TouchBar reactions are still logged, but into
-- separate columns, because implicit feedback cannot separate "examined and
-- rejected" from "never examined" — and treating those the same is what makes
-- a naive relevance model learn the wrong thing.

PRAGMA journal_mode=WAL;

-- ---------------------------------------------------------------------------
-- What is being judged
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decisions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       REAL    NOT NULL,
    card_id          INTEGER REFERENCES cards(id),
    session_id       TEXT    NOT NULL DEFAULT '',
    depth            INTEGER NOT NULL DEFAULT 0,

    -- the candidate, exactly as it would have been relayed
    candidate        TEXT    NOT NULL,
    candidate_chars  INTEGER NOT NULL DEFAULT 0,
    candidate_words  INTEGER NOT NULL DEFAULT 0,

    -- what the system did, and why, frozen at that moment
    decision         TEXT    NOT NULL CHECK (decision IN ('relay','suppress')),
    score            REAL    NOT NULL,
    threshold        REAL    NOT NULL,
    reason           TEXT    NOT NULL DEFAULT '',
    breakdown        TEXT    NOT NULL DEFAULT '{}',   -- JSON per-feature contributions

    -- what happened next
    outcome          TEXT    CHECK (outcome IN
                        ('confirm','expand','dismiss','timeout','unobservable')),
    outcome_at       REAL,

    -- examination, from the glasses' own IMU (0xF5 0x02 / 0x03)
    exam_count       INTEGER NOT NULL DEFAULT 0,
    first_exam_at    REAL,
    last_exam_at     REAL,
    examined         INTEGER NOT NULL DEFAULT 0,
    -- 0 when the glasses were in the case or the dashboard was open:
    -- "never examined" is then missing data, NOT a negative label.
    observable       INTEGER NOT NULL DEFAULT 1,

    -- the explicit review label
    label            INTEGER CHECK (label IN (0,1)),
    reviewed_at      REAL
);

CREATE TABLE IF NOT EXISTS features (
    key        TEXT PRIMARY KEY,        -- 'topic:kernel', 'depth:0', 'chars:118'
    kind       TEXT NOT NULL,           -- topic | depth | length | source | context
    created_at REAL NOT NULL
);

-- The feature vector is FROZEN at decision time. Without this you cannot
-- reconstruct why decision #412 fired on Tuesday, because feature_counts has
-- moved since. Freezing is what makes the audit permanent.
CREATE TABLE IF NOT EXISTS decision_features (
    decision_id INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    feature_key TEXT    NOT NULL REFERENCES features(key),
    value       REAL    NOT NULL DEFAULT 1.0,
    PRIMARY KEY (decision_id, feature_key)
);

CREATE TABLE IF NOT EXISTS feature_counts (
    feature_key   TEXT PRIMARY KEY REFERENCES features(key),
    pos           INTEGER NOT NULL DEFAULT 0,   -- review said YES
    neg           INTEGER NOT NULL DEFAULT 0,   -- review said NO
    implicit_pos  INTEGER NOT NULL DEFAULT 0,   -- TouchBar reactions, kept apart
    implicit_neg  INTEGER NOT NULL DEFAULT 0,
    updated_at    REAL    NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_df_d     ON decision_features(decision_id);
CREATE INDEX IF NOT EXISTS idx_df_f     ON decision_features(feature_key);
CREATE INDEX IF NOT EXISTS idx_dec_open ON decisions(label, observable, created_at);

-- ---------------------------------------------------------------------------
-- Scoring
-- ---------------------------------------------------------------------------

-- Beta(1,1) prior, weight is the posterior log-odds ratio: bounded, symmetric,
-- and readable as a plain number. The CI columns make the system able to say
-- "I am not sure", which matters when a threshold is being calibrated.
CREATE VIEW IF NOT EXISTS feature_score AS
WITH t AS (
  SELECT c.feature_key AS k, f.kind AS kind, c.pos AS pos, c.neg AS neg,
         c.pos + c.neg + 2.0 AS a
    FROM feature_counts c JOIN features f ON f.key = c.feature_key
)
SELECT k AS feature_key, kind,
       ROUND(ln((pos + 1.0) / (neg + 1.0)), 4)                  AS w,
       ROUND((pos + 1.0) / a, 4)                                AS p_hat,
       pos + neg                                                 AS n,
       ROUND(ln((pos+1.0)/(neg+1.0))
             - 1.96*sqrt((pos+1.0)*(neg+1.0)/(a*a*(a+1.0))), 4) AS w_lo95,
       ROUND(ln((pos+1.0)/(neg+1.0))
             + 1.96*sqrt((pos+1.0)*(neg+1.0)/(a*a*(a+1.0))), 4) AS w_hi95
FROM t;

-- Effective counts: flip this view to fold implicit feedback back in.
CREATE VIEW IF NOT EXISTS feature_counts_effective AS
SELECT feature_key, pos, neg, implicit_pos, implicit_neg,
       pos + neg AS n_effective
  FROM feature_counts;

-- Score = dot product normalised by the feature vector's L2 norm.
-- The denominator gives two useful properties:
--   * a one-feature decision reads as exactly that feature's log-odds
--     (verified: 1.6094 == ln(10/2), no hidden scaling)
--   * a candidate carrying twelve weak features cannot out-shout one
--     carrying a single strong, well-evidenced feature
CREATE VIEW IF NOT EXISTS decision_score AS
SELECT d.id, d.created_at, d.candidate, d.decision, d.threshold, d.reason,
       ROUND(COALESCE(SUM(df.value * fs.w), 0.0)
             / sqrt(COALESCE(SUM(df.value * df.value), 1.0)), 4) AS score
  FROM decisions d
  JOIN decision_features df ON df.decision_id = d.id
  JOIN feature_score fs     ON fs.feature_key = df.feature_key
 GROUP BY d.id;

-- ---------------------------------------------------------------------------
-- "Why did it interrupt me" — the exact query the review's ? key runs
-- ---------------------------------------------------------------------------
-- SELECT printf('  %-14s x%.1f  w=%+.3f  [%+.3f,%+.3f]  p=%.2f n=%-3d -> %+.3f',
--               df.feature_key, df.value, fs.w, fs.w_lo95, fs.w_hi95,
--               fs.p_hat, fs.n, df.value * fs.w)
--   FROM decision_features df
--   JOIN feature_score fs ON fs.feature_key = df.feature_key
--  WHERE df.decision_id = ?
--  ORDER BY abs(df.value * fs.w) DESC;

-- ---------------------------------------------------------------------------
-- Review queue: stratified, deterministic, no randomness at read time.
-- Stratum A is the ONLY stratum from which a false-positive rate may be
-- estimated. If you only review items you reacted to, every sample item is
-- selected and the intercept is unidentifiable.
-- ---------------------------------------------------------------------------
-- Stratum A: uniform over everything presented and observable.
-- SELECT id, candidate, outcome, examined,
--        ((id*1103515245 + 12345) % 2147483647)/2147483647.0 AS u
--   FROM decisions
--  WHERE label IS NULL AND observable = 1 AND created_at >= :since
--  ORDER BY u LIMIT 30;

-- Stratum B: never examined. The cheap wins the current schema cannot even
-- name, drawn deliberately rather than at random.
-- SELECT id, candidate, outcome, examined
--   FROM decisions
--  WHERE label IS NULL AND observable = 1 AND examined = 0 AND created_at >= :since
--  ORDER BY ((id*1103515245 + 12345) % 2147483647)/2147483647.0 LIMIT 10;

-- ---------------------------------------------------------------------------
-- Threshold calibration: time-split, never random.
-- Random splits leak the future into the past and report a curve materially
-- better than reality. Split by day and roll forward.
-- ---------------------------------------------------------------------------
-- WITH grid(tau) AS (VALUES (0.00),(0.05),...,(4.00)),
--  e AS (
--   SELECT g.tau AS tau, COUNT(*) AS n,
--     SUM(CASE WHEN d.label = 1 AND d.score >  g.tau THEN 1 ELSE 0 END) AS tp,
--     SUM(CASE WHEN d.label = 0 AND d.score >  g.tau THEN 1 ELSE 0 END) AS fp,
--     SUM(CASE WHEN d.label = 1 AND d.score <= g.tau THEN 1 ELSE 0 END) AS fn
--     FROM grid g JOIN decisions d ON 1=1
--    WHERE d.label IS NOT NULL AND d.observable = 1
--      AND d.created_at >= :test_from AND d.created_at < :test_to
--    GROUP BY g.tau)
-- SELECT tau, n, tp, fp, fn,
--        ROUND(1.0*tp/(tp+fp), 3) AS precision,
--        ROUND(1.0*tp/(tp+fn), 3) AS recall,
--        ROUND(:fp_cost*fp + :fn_cost*fn, 1) AS cost
--   FROM e ORDER BY cost, tau;
--
-- Two rules for reading the result:
--   1. Report the PLATEAU, not the argmin. If every tau in [2.70, 3.40] is
--      within 10% of optimal, the data cannot distinguish them and publishing
--      2.70 is false precision. Bootstrap over DAYS, not rows — drift is what
--      you are unsure about, so the day is the resampling unit.
--   2. If the argmin lands on the grid EDGE, the answer is "your feature set
--      has no signal", not "raise the threshold". A feature problem, not a
--      patience problem.

-- ---------------------------------------------------------------------------
-- Backfill: historical cards become reviewable immediately
-- ---------------------------------------------------------------------------
-- INSERT INTO decisions (created_at, card_id, depth, candidate, candidate_chars,
--                        decision, score, threshold, reason, outcome, observable, label)
-- SELECT c.created_at, c.id, 0, c.summary, length(c.summary),
--        'relay', 0.0, 0.0, 'backfilled from legacy events (unscored)', e.kind, 1, NULL
--   FROM cards c
--   LEFT JOIN (SELECT card_id, MAX(kind) AS kind FROM events GROUP BY card_id) e
--     ON e.card_id = c.id;