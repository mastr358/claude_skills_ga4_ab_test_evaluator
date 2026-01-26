# Query Patterns

GA4 Data API query structures for AB test analysis.

## Table of Contents

1. [CRITICAL: API Limit](#critical-api-limit)
2. [Avoiding Sampling](#avoiding-sampling)
3. [Core Metric Queries](#core-metric-queries)
4. [Breakdown Queries](#breakdown-queries)
5. [Outlier Removal](#outlier-removal)
6. [Methodology Documentation](#methodology-documentation)

## CRITICAL: API Limit & Pagination

```
╔════════════════════════════════════════════════════════════════════════╗
║  ⛔ MANDATORY FOR ALL QUERIES: limit: 100000                            ║
║                                                                        ║
║  GA4 DEFAULT LIMIT IS 10,000 ROWS - This WILL truncate your data!     ║
║  GA4 MAXIMUM LIMIT IS 250,000 ROWS per request.                       ║
║                                                                        ║
║  EVERY mcp__analytics-mcp__run_report call MUST include:               ║
║    limit: 100000                                                       ║
║                                                                        ║
║  AFTER EVERY API call, immediately check:                              ║
║  - If row_count == 10000 → FORGOT LIMIT! Re-query with limit: 100000   ║
║  - If row_count == limit → USE PAGINATION! Query with offset parameter ║
║  - Check for sampling_metadatas in response                            ║
║                                                                        ║
║  This is the #1 cause of irreproducible results.                       ║
╚════════════════════════════════════════════════════════════════════════╝
```

### Pagination Pattern

If row_count equals limit, use offset-based pagination:

```yaml
# First request (offset defaults to 0)
mcp__analytics-mcp__run_report:
  property_id: 281685462
  ...
  limit: 100000

# If 100,000 rows returned, fetch next page:
mcp__analytics-mcp__run_report:
  property_id: 281685462
  ...
  limit: 100000
  offset: 100000  # Start from row 100,001

# Continue until returned rows < limit
# Merge all responses before processing
```

**Example correct API call:**
```yaml
mcp__analytics-mcp__run_report:
  property_id: 281685462
  date_ranges: [{"start_date": "2025-12-15", "end_date": "2026-01-04"}]
  dimensions: ["customUser:ab_test", "transactionId"]
  metrics: ["purchaseRevenue"]
  limit: 100000  # ← MANDATORY - DO NOT OMIT
```

## Avoiding Sampling

**CRITICAL: Always include "date" dimension to avoid GA4 sampling.**

- Query each metric with "date" as a dimension
- Sum the daily values afterward
- Duplicate counts (users active multiple days) are acceptable
- Exception: Individual transaction queries (already granular)

### Detecting Sampling in API Response

GA4 adds `sampling_metadatas` to the response when sampling occurs:

```python
# Check for sampling in API response
metadata = response.get('result', response).get('metadata', {})
sampling = metadata.get('sampling_metadatas', [])

if sampling:
    samples_read = int(sampling[0].get('samples_read_count', 0))
    sampling_space = int(sampling[0].get('sampling_space_size', 0))
    percentage = (samples_read / sampling_space * 100) if sampling_space else 100
    print(f"⛔ DATA SAMPLED: {percentage:.1f}% of {sampling_space:,} events analyzed")
    # Solution: Use shorter date ranges or query daily and aggregate
else:
    print("✅ No sampling - full data coverage")
```

**If sampling detected:**
1. Split the date range into smaller chunks (weekly or daily)
2. Query each chunk separately
3. Aggregate results after collection

## Core Metric Queries

### Users and Sessions (day by day)
```
dimensions: [ab_test_dimension, "date"]
metrics: ["sessions", "totalUsers"]
date_ranges: [user-specified range]
→ Sum daily values afterward
```

### Add-to-Cart Events (day by day)
```
dimensions: [ab_test_dimension, "date", "eventName"]
metrics: ["eventCount"]
dimension_filter: {eventName = "add_to_cart"}
→ Sum daily values afterward
```

### Individual Transactions
```
dimensions: [ab_test_dimension, "transactionId"]
metrics: ["itemRevenue"] or ["purchaseRevenue"]
dimension_filter: {eventName = "purchase"}
→ List individual transactions, then remove outliers
```

### 12-Month Revenue (for projected impact)
```
dimensions: ["date"]
metrics: ["purchaseRevenue"] or ["itemRevenue"]
date_ranges: [{"start_date": "12monthsAgo", "end_date": "today"}]
→ Sum all daily revenue, divide by 12 for monthly average
```

## Breakdown Queries

### Device Breakdown (day by day)
```
dimensions: [ab_test_dimension, "date", "deviceCategory"]
metrics: ["sessions", "totalUsers"]
→ Sum daily values per device category
```

### Browser Breakdown (day by day)
```
dimensions: [ab_test_dimension, "date", "browser"]
metrics: ["sessions", "totalUsers"]
→ Sum daily values per browser
```

### Timeline Analysis
```
dimensions: [ab_test_dimension, "date"]
metrics: ["sessions", "totalUsers", "purchaseRevenue"]
order_by: date ascending
→ Display daily values (no summing needed)
```

### Traffic Source Breakdown
```
dimensions: [ab_test_dimension, "date", "sessionDefaultChannelGroup"]
metrics: ["sessions", "transactions"]
→ Sum daily values per channel
```

## Outlier Removal

### Algorithm:
1. Collect all transaction revenue values for each group
2. Calculate 99th percentile threshold
3. Remove transactions > 99th percentile
4. Recalculate metrics using cleaned data

### Documentation format:
```
**Outlier Removal:**
- Raw transactions: X
- Outliers removed: Y (Z%)
- Clean transactions: X-Y
- Method: Removed transactions > 99th percentile
```

## Methodology Documentation

**Every finding MUST include methodology.**

### Bad (not sufficient):
```
CVR declined by 9.46%
```

### Good (includes methodology):
```
**CVR Decline: -9.46%**
- Luigis: 4,402 / 97,702 = 4.50%
- Original: 4,892 / 98,366 = 4.97%
- Formula: CVR = transactions / users

**Data source:**
- Users: dimensions=["customUser:ab_test_variant", "date"],
         metrics=["totalUsers"], summed daily
- Transactions: dimensions=["customUser:ab_test_variant", "transactionId"],
               metrics=["itemRevenue"], outliers removed (>99th percentile)
- Date range: 2025-12-19 to 2026-01-05
```

### Required for each finding:
- Event name(s) or metric name(s) queried
- Dimensions used
- Segments/filters applied (exact dimension names and values)
- Date range (YYYY-MM-DD to YYYY-MM-DD)
- Aggregation/calculation steps
- Raw numbers before and after calculations
