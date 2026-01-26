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
║  • GA4 API Limit: ALWAYS set limit: 50000                              ║
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
  limit: 50000  # ⛔ MANDATORY!
```

**IMMEDIATELY save** the full response to `{date_folder}/transactions_raw.json`

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

### B. Data Truncation Check

```python
# Run this check:
import json
with open('transactions_raw.json') as f:
    data = json.load(f)
    rows = data.get('result', data).get('rows', [])
    print(f"Row count: {len(rows)}")
    if len(rows) == 10000:
        print("⛔ TRUNCATED! Re-query with limit: 50000")
```

- Row count == 10,000 → **DATA TRUNCATED, INVALID RESULTS**
- Must re-query with `limit: 50000`

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

If CVR uplift ≥ 1.5%, include:
> "We can consider presenting these results to the client to close the test if they accept the current improvement."

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
2. **ALWAYS use limit: 50000** - GA4 default truncates at 10,000
3. **CVR = transactions/users** - never sessions
4. **Run the script** - don't manually calculate metrics
5. **Validate before reporting** - complete the checklist
6. **Control first, Treatment second** - in all tables
7. **No recommendations** - explain what happened, not what to do
