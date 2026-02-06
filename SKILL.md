---
name: ga4-ab-test-evaluator
description: "Evaluate LuigisBox AB tests using GA4 data. Calculates CVR, AOV, revenue metrics with statistical significance. Use when: 'evaluate AB test', 'run GA4 evaluation', 'check LuigisBox performance', 'AB test analysis'."
---

# GA4 AB Test Evaluator

Evaluate AB tests comparing LuigisBox against control groups using GA4 data. Calculate ROI metrics with statistical significance testing.

## Quick Start

If user provides domain and date range, proceed directly. Otherwise ask:
- Domain (e.g., homla.com.pl, grizly.cz)
- Date range (e.g., "Jan 21-25" or "last 7 days")

## Directory Structure

```
ga4-investigations/{domain}/
  ├── config.yaml                    # Shared domain config
  └── YYYY-MM-DD_to_YYYY-MM-DD/      # Date-specific folder
      ├── report.md                  # Full evaluation report
      ├── ab_test_overview.html      # Visual dashboard
      ├── ab_test_overview.png       # Screenshot of HTML dashboard
      ├── executive-summary.md       # Brief interpretation
      ├── transactions_raw.json      # Raw GA4 API response
      ├── transactions_clean.csv     # After outlier removal
      ├── run_complete.json          # Execution metadata
      ├── api_calls.log              # API call log
      └── charts/                    # Visualizations
```

## Fixed Methodology (NO DEVIATION)

```
╔════════════════════════════════════════════════════════════════════════╗
║  HARDCODED METHODOLOGY - DO NOT CHANGE                                 ║
╠════════════════════════════════════════════════════════════════════════╣
║  • Transaction Dimension: "transactionId" (NOT customEvent:...)        ║
║  • Outlier Method: 99th percentile (NOT IQR, NOT mean+3std)            ║
║  • CVR Formula: transactions / users (NOT sessions)                    ║
║  • Statistical Tests: Two-proportion Z-test (CVR), Z-test means (AOV)  ║
║  • Significance Level: α = 0.05                                        ║
║  • GA4 API Limit: ALWAYS set limit: 100000                             ║
║  • Pagination: Use offset if row_count == limit                        ║
╚════════════════════════════════════════════════════════════════════════╝
```

## Workflow

### Step 0: Check for Existing Data

Before querying GA4, check if data already exists:

```
1. Determine date folder: YYYY-MM-DD_to_YYYY-MM-DD
2. Check: ga4-investigations/{domain}/{date_folder}/run_complete.json
3. If exists and date_range matches → reuse existing data, skip to reporting
4. If user wants fresh data → proceed with queries
```

### Step 1: Get Config or Discover Dimension

If `config.yaml` exists, read it for:
- `property_id`
- `ab_test_dimension`
- `treatment_group`, `control_group`

If not, use `mcp__analytics-mcp__get_account_summaries` to find property ID, then probe dimensions:
```
Candidates: customUser:ab_test, customUser:ab_test_variant, customUser:lb_ab_test
```

### Step 2: Query User and Session Counts

```yaml
mcp__analytics-mcp__run_report:
  property_id: {property_id}
  date_ranges: [{"start_date": "{start}", "end_date": "{end}"}]
  dimensions: ["{ab_test_dimension}"]
  metrics: ["totalUsers", "sessions"]
  dimension_filter:
    filter:
      field_name: "{ab_test_dimension}"
      in_list_filter:
        values: ["{treatment}", "{control}"]
        case_sensitive: true
```

Save: `users_treatment`, `users_control`, `sessions_treatment`, `sessions_control`

### Step 3: Query Transactions (SAVE FULL RESPONSE)

```yaml
mcp__analytics-mcp__run_report:
  property_id: {property_id}
  date_ranges: [{"start_date": "{start}", "end_date": "{end}"}]
  dimensions: ["{ab_test_dimension}", "transactionId", "date"]  # ALL 3 REQUIRED!
  metrics: ["purchaseRevenue"]
  dimension_filter:
    filter:
      field_name: "{ab_test_dimension}"
      in_list_filter:
        values: ["{treatment}", "{control}"]
        case_sensitive: true
  limit: 100000  # ⛔ MANDATORY!
```

**IMMEDIATELY save** the full response to `{date_folder}/transactions_raw.json`

#### Step 3a: Check for Pagination & Sampling

After receiving the response, check:

```python
# Check row count vs limit
returned_rows = len(response.get('result', response).get('rows', []))
row_count = response.get('result', response).get('row_count', returned_rows)

# If row_count equals limit, there may be more data - use pagination
if returned_rows >= 100000:
    print("⚠️ Row count equals limit - pagination required!")
    # Query again with offset: 100000, then offset: 200000, etc.
    # Merge all results before saving to transactions_raw.json

# Check for sampling
metadata = response.get('result', response).get('metadata', {})
sampling = metadata.get('sampling_metadatas', [])
if sampling:
    samples_read = sampling[0].get('samples_read_count', 0)
    sampling_space = sampling[0].get('sampling_space_size', 0)
    pct = (int(samples_read) / int(sampling_space) * 100) if sampling_space else 100
    print(f"⛔ DATA IS SAMPLED: {pct:.1f}% of events analyzed")
    # Consider using shorter date ranges to avoid sampling
```

**If pagination needed:** Query with `offset: 100000`, then `offset: 200000`, etc., until fewer than `limit` rows return. Merge all responses before saving.

### Step 4: Run Deterministic Script

```bash
python ~/.claude/skills/ga4-ab-test-evaluator/scripts/run_full_evaluation.py \
  --domain {domain} \
  --property-id {property_id} \
  --start-date {start_date} \
  --end-date {end_date} \
  --treatment "{treatment}" \
  --control "{control}" \
  --users-treatment {users_treatment} \
  --users-control {users_control} \
  --sessions-treatment {sessions_treatment} \
  --sessions-control {sessions_control} \
  --output-dir {cwd}/ga4-investigations/{domain}/{date_folder}/
```

The script generates ALL outputs:
- report.md, ab_test_overview.html, executive-summary.md
- transactions_clean.csv, config.yaml, run_complete.json
- charts/

### Step 4b: Screenshot HTML Dashboard

After the evaluation script completes, screenshot the HTML dashboard to PNG:

```bash
python ~/.claude/skills/ga4-ab-test-evaluator/scripts/screenshot_html_report.py \
  {cwd}/ga4-investigations/{domain}/{date_folder}/ab_test_overview.html
```

This saves `ab_test_overview.png` alongside the HTML file. Requires `playwright` Python package with Chromium browser installed.

---

## ⛔ MANDATORY SELF-VALIDATION (Before Reporting)

```
╔════════════════════════════════════════════════════════════════════════╗
║  YOU MUST COMPLETE THIS CHECKLIST BEFORE REPORTING RESULTS             ║
║                                                                        ║
║  Do NOT skip. Do NOT trust your memory. Actually run these checks.     ║
╚════════════════════════════════════════════════════════════════════════╝
```

### A. File Existence (Use Glob tool)

```bash
Glob: ga4-investigations/{domain}/config.yaml                        → MUST exist
Glob: ga4-investigations/{domain}/{date_folder}/report.md            → MUST exist
Glob: ga4-investigations/{domain}/{date_folder}/transactions_raw.json   → MUST exist
Glob: ga4-investigations/{domain}/{date_folder}/transactions_clean.csv  → MUST exist
Glob: ga4-investigations/{domain}/{date_folder}/run_complete.json       → MUST exist
Glob: ga4-investigations/{domain}/{date_folder}/ab_test_overview.html   → MUST exist
Glob: ga4-investigations/{domain}/{date_folder}/executive-summary.md    → MUST exist
Glob: ga4-investigations/{domain}/{date_folder}/charts/*.png            → Should exist
```

**If ANY file is missing → GO BACK and fix it. Do not proceed.**

### B. Data Truncation & Sampling Check

```python
# Run this check:
import json
with open('transactions_raw.json') as f:
    data = json.load(f)
    result = data.get('result', data)
    rows = result.get('rows', [])
    row_count = len(rows)

    # Check for truncation
    print(f"Row count: {row_count}")
    if row_count == 10000:
        print("⛔ TRUNCATED at default limit! Re-query with limit: 100000")
    elif row_count == 50000:
        print("⛔ TRUNCATED at 50k! Re-query with limit: 100000")
    elif row_count >= 100000:
        print("⛔ MAY BE TRUNCATED! Use pagination with offset parameter")

    # Check for sampling
    metadata = result.get('metadata', {})
    sampling = metadata.get('sampling_metadatas', [])
    if sampling:
        reads = int(sampling[0].get('samples_read_count', 0))
        space = int(sampling[0].get('sampling_space_size', 0))
        pct = (reads / space * 100) if space else 100
        print(f"⛔ DATA SAMPLED: Only {pct:.1f}% of events analyzed!")
        print("   → Use shorter date range or split into daily queries")
    else:
        print("✅ No sampling detected")
```

- Row count == 10,000 → **DEFAULT LIMIT HIT, INVALID RESULTS**
- Row count == 50,000 or 100,000 → **MAY BE TRUNCATED, use pagination**
- `sampling_metadatas` present → **DATA IS SAMPLED, consider daily queries**

### C. Methodology Compliance

Read report.md and verify:
- [ ] Uses "transactionId" dimension (NOT "customEvent:transaction_id")
- [ ] Outlier method = 99th percentile
- [ ] CVR = transactions / users (NOT sessions)
- [ ] Both User CVR and Session CVR present
- [ ] P-values calculated for CVR and AOV

### D. Numbers Match

Compare report.md numbers against transactions_clean.csv:
- [ ] Transaction counts match
- [ ] Revenue totals match

---

## Interpretation Rules

### User Split Validation
| Difference | Interpretation |
|------------|----------------|
| < 0.5% | Safe to ignore |
| 0.5% - 1% | Concerning but acceptable |
| > 1% (if >10k users) | Potentially invalidates test |

### P-value Interpretation
| P-value | Interpretation |
|---------|----------------|
| < 0.05 | **Significant** - confident the effect is real |
| < 0.15 | Approaching significance - needs more data |
| ≥ 0.15 | Not significant - no conclusion |

### Effect Size
| Difference | Quality |
|------------|---------|
| 1-2% | Modest |
| 3-5% | Nice |
| 6%+ | Strong |

### Reporting Philosophy

**Focus on CVR and AOV with p-values, NOT revenue.**

- Lead with CVR (primary metric)
- Report AOV as secondary
- Treat non-significant results as "no detectable effect"
- Don't lead with revenue (it combines significant and non-significant effects)

**Why:** If CVR is +7% (significant) and AOV is -2% (not significant), revenue would dilute the validated win.

### Client Closing Note

**Always push for closure.** Even if p-value is not < 0.05, we want to close the test and move forward. Waiting for perfect statistical significance is rarely worth the opportunity cost — the client loses time without Luigi's Box active on 100% of traffic.

If CVR uplift ≥ 1.5% (regardless of p-value), include:
> "We recommend closing the test and going live with Luigi's Box. The observed +X.X% CVR improvement, while not yet at traditional statistical significance, represents a meaningful uplift. Continuing the test delays the full benefit to all users."

If CVR is neutral or slightly negative but other metrics are positive (e.g., ATC rate, engagement), still frame positively and push for closure:
> "Results show positive engagement signals. We recommend going live to capture the full benefit."

Only if CVR is significantly negative (p < 0.05 AND negative direction) should we acknowledge underperformance — but even then, frame it as "we identified issues, applied fixes, and the trend is now positive."

---

## Report to User

After validation passes:

```markdown
## GA4 AB Test Results: {domain}

**Test Period:** {dates}
**Status:** {outperforming | underperforming | inconclusive}

### Key Metrics
| Metric | Original | LuigisBox | Diff | p-value | Status |
|--------|----------|-----------|------|---------|--------|
| **User CVR** | X% | Y% | +Z% | 0.XXX | ⚠️/✅/❌ |
| Session CVR | X% | Y% | +Z% | 0.XXX | ⚠️/✅/❌ |
| AOV | X | Y | +Z% | 0.XXX | ⚠️/✅/❌ |

### Executive Summary
{Copy from executive-summary.md}

### Files
- {date_folder}/ab_test_overview.html (visual dashboard)
- {date_folder}/executive-summary.md
- {date_folder}/report.md (full details)
```

---

## Known Issues

### GA4 Data Delay
GA4 takes 24-48 hours to fully process. If querying recent dates, data may be incomplete. Wait or re-run later.

### Dimension Discovery
Always verify dimension name. Common mistake: `customUser:ab_test` vs `customUser:ab_test_variant`.

---

## Key Rules

1. **Save data files** - transactions_raw.json MUST exist before reporting
2. **ALWAYS use limit: 100000** - GA4 default truncates at 10,000
3. **Check for pagination** - if row_count == limit, use `offset` to get more rows
4. **Check for sampling** - look for `sampling_metadatas` in response
5. **CVR = transactions/users** - never sessions
6. **Run the script** - don't manually calculate metrics
7. **Validate before reporting** - complete the checklist
8. **Control first, Treatment second** - in all tables
9. **No recommendations** - explain what happened, not what to do

---

## Domain-Specific: 4home.cz

4home.cz requires **PostMAMA** (margin) metric tracking in addition to standard metrics.

### 4home.cz Configuration

```yaml
domain: 4home.cz
property_id: 282336364
ab_test_dimension: "customUser:user_experiment_group"
treatment_group: "1"
control_group: "0"
```

### 4home.cz Workflow

**Step 3 Query (DIFFERENT - includes PostMAMA):**

```yaml
mcp__analytics-mcp__run_report:
  property_id: 282336364
  date_ranges: [{"start_date": "{start}", "end_date": "{end}"}]
  dimensions: ["customUser:user_experiment_group", "transactionId", "date"]
  metrics: ["purchaseRevenue", "customEvent:post_mama"]  # BOTH METRICS!
  dimension_filter:
    filter:
      field_name: "customUser:user_experiment_group"
      in_list_filter:
        values: ["1", "0"]
        case_sensitive: true
  limit: 100000
```

**Step 4 Script (DIFFERENT - use 4home-specific script):**

```bash
python ~/.claude/skills/ga4-ab-test-evaluator/scripts/run_4home_evaluation.py \
  --start-date {start_date} \
  --end-date {end_date} \
  --users-treatment {users_treatment} \
  --users-control {users_control} \
  --sessions-treatment {sessions_treatment} \
  --sessions-control {sessions_control} \
  --output-dir {cwd}/ga4-investigations/4home.cz/{date_folder}/
```

### 4home.cz Report Template

Include PostMAMA in the results table:

```markdown
| Metric | Control (0) | LuigisBox (1) | Diff | p-value | Status |
|--------|-------------|---------------|------|---------|--------|
| **User CVR** | X% | Y% | +Z% | 0.XXX | ... |
| Session CVR | X% | Y% | +Z% | 0.XXX | ... |
| AOV | X | Y | +Z% | 0.XXX | ... |
| **PostMAMA** | X | Y | +Z% | 0.XXX | ... |
| PostMAMA/User | X | Y | +Z% | - | - |
```
