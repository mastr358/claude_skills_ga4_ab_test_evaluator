#!/usr/bin/env python3
"""
GA4 AB Test Full Evaluation Pipeline

This script performs the COMPLETE evaluation from raw GA4 API response to final report.
It hardcodes all methodology decisions to ensure reproducibility.
Outputs: api_calls.log, transactions_clean.csv (group, tx_id, revenue), config.yaml, markdown report,
run_complete.json.

USAGE:
    The LLM must:
    1. Query GA4 API with EXACT parameters shown below
    2. Save response to transactions_raw.json
    3. Run this script

    python run_full_evaluation.py \
        --domain grizly.cz \
        --property-id 281685462 \
        --start-date 2025-12-15 \
        --end-date 2026-01-04 \
        --treatment Luigis \
        --control Original \
        --dimension customUser:ab_test \
        --users-treatment 69248 \
        --users-control 70343 \
        --output-dir /path/to/ga4-investigations/grizly-cz/

REQUIRED GA4 QUERY (LLM must run this EXACTLY):
    mcp__analytics-mcp__run_report:
      property_id: {property_id}
      date_ranges: [{"start_date": "{start_date}", "end_date": "{end_date}"}]
      dimensions: ["{ab_test_dimension}", "transactionId", "date"]
      metrics: ["purchaseRevenue"]
      dimension_filter:
        filter:
          field_name: "{ab_test_dimension}"
          in_list_filter:
            values: ["{treatment}", "{control}"]
            case_sensitive: true
      limit: 100000

HARDCODED METHODOLOGY:
    - Outlier removal: 99th percentile (NOT IQR, NOT mean+3std)
    - User CVR: transactions / users (PRIMARY metric)
    - Session CVR: transactions / sessions (SECONDARY metric)
    - Statistical test: Two-proportion Z-test (two-sided) for both CVRs
    - Significance level: alpha = 0.05
"""

import argparse
import json
import csv
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

# Chart generation imports (optional - gracefully degrade if not available)
try:
    import altair as alt
    import numpy as np
    from scipy.stats import beta
    CHARTS_AVAILABLE = True
except ImportError:
    CHARTS_AVAILABLE = False


# =============================================================================
# HARDCODED METHODOLOGY CONSTANTS - DO NOT CHANGE
# =============================================================================
OUTLIER_PERCENTILE = 99.0  # 99th percentile
SIGNIFICANCE_LEVEL = 0.05  # alpha = 0.05
MIN_EXPECTED_ROWS = 1000   # Warn if fewer rows than this
STATISTICAL_POWER = 0.80   # 80% power for sample size calculations
Z_ALPHA_TWO_SIDED = 1.96   # z-score for alpha=0.05 (two-sided)
Z_BETA = 0.84              # z-score for 80% power


def calculate_percentile(values: List[float], percentile: float) -> float:
    """
    Calculate the nth percentile using linear interpolation.

    Method: INCLUSIVE (matches Excel's PERCENTILE.INC)
    Formula: index = (percentile / 100) * (n - 1)

    This is equivalent to:
    - Excel: PERCENTILE() or PERCENTILE.INC()
    - NumPy: np.percentile(..., method='linear') [default]
    - NOT equivalent to Excel's PERCENTILE.EXC()

    For comparing with Excel, use =PERCENTILE.INC(range, 0.99)
    """
    if not values:
        return 0.0
    sorted_values = sorted(values)
    n = len(sorted_values)
    index = (percentile / 100) * (n - 1)
    lower = int(index)
    upper = lower + 1
    if upper >= n:
        return sorted_values[-1]
    fraction = index - lower
    return sorted_values[lower] + fraction * (sorted_values[upper] - sorted_values[lower])


def norm_cdf(x: float) -> float:
    """Standard normal CDF using error function approximation."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def two_proportion_ztest(n1: int, x1: int, n2: int, x2: int) -> Tuple[float, float]:
    """
    Two-proportion Z-test (two-sided).

    Args:
        n1: Sample size group 1 (control)
        x1: Successes group 1 (control conversions)
        n2: Sample size group 2 (treatment)
        x2: Successes group 2 (treatment conversions)

    Returns:
        Tuple of (z_statistic, p_value)
    """
    p1 = x1 / n1 if n1 > 0 else 0
    p2 = x2 / n2 if n2 > 0 else 0

    # Pooled proportion
    p_pool = (x1 + x2) / (n1 + n2) if (n1 + n2) > 0 else 0

    # Standard error
    se = math.sqrt(p_pool * (1 - p_pool) * (1/n1 + 1/n2)) if p_pool > 0 and p_pool < 1 else 0

    if se == 0:
        return 0.0, 1.0

    # Z statistic
    z = (p1 - p2) / se

    # Two-sided p-value using normal CDF approximation
    p_value = 2 * (1 - norm_cdf(abs(z)))

    return z, p_value


def two_sample_mean_ztest(values1: List[float], values2: List[float]) -> Tuple[float, float, float, float]:
    """
    Two-sample Z-test for comparing means (two-sided).

    Used for AOV comparison where we have individual transaction values.

    Args:
        values1: List of values from group 1 (e.g., transaction revenues)
        values2: List of values from group 2

    Returns:
        Tuple of (z_statistic, p_value, std1, std2)
    """
    n1 = len(values1)
    n2 = len(values2)

    if n1 < 2 or n2 < 2:
        return 0.0, 1.0, 0.0, 0.0

    # Calculate means
    mean1 = sum(values1) / n1
    mean2 = sum(values2) / n2

    # Calculate sample variances (using n-1 for unbiased estimate)
    var1 = sum((x - mean1) ** 2 for x in values1) / (n1 - 1)
    var2 = sum((x - mean2) ** 2 for x in values2) / (n2 - 1)

    std1 = math.sqrt(var1)
    std2 = math.sqrt(var2)

    # Standard error of the difference of means
    se = math.sqrt(var1 / n1 + var2 / n2)

    if se == 0:
        return 0.0, 1.0, std1, std2

    # Z statistic
    z = (mean1 - mean2) / se

    # Two-sided p-value
    p_value = 2 * (1 - norm_cdf(abs(z)))

    return z, p_value, std1, std2


def required_sample_size_proportions(p1: float, p2: float) -> Optional[int]:
    """
    Calculate required sample size per group for two-proportion z-test.

    Uses standard power analysis formula with:
    - alpha = 0.05 (two-sided)
    - power = 80%

    Args:
        p1: Proportion in group 1 (e.g., 0.0285 for 2.85% CVR)
        p2: Proportion in group 2

    Returns:
        Required sample size per group, or None if effect is too small
    """
    effect = abs(p1 - p2)
    if effect < 0.0001:  # Effect too small to detect
        return None

    p_pooled = (p1 + p2) / 2

    # Avoid edge cases where pooled proportion is 0 or 1
    if p_pooled <= 0 or p_pooled >= 1:
        return None

    # Sample size formula for two proportions
    # n = 2 * (z_alpha + z_beta)^2 * p_pooled * (1 - p_pooled) / effect^2
    numerator = 2 * ((Z_ALPHA_TWO_SIDED + Z_BETA) ** 2) * p_pooled * (1 - p_pooled)
    n = numerator / (effect ** 2)

    return int(math.ceil(n))


def required_sample_size_means(mean1: float, mean2: float, std1: float, std2: float) -> Optional[int]:
    """
    Calculate required sample size per group for two-sample z-test of means.

    Uses standard power analysis formula with:
    - alpha = 0.05 (two-sided)
    - power = 80%

    Args:
        mean1: Mean of group 1
        mean2: Mean of group 2
        std1: Standard deviation of group 1
        std2: Standard deviation of group 2

    Returns:
        Required sample size per group, or None if effect is too small
    """
    effect = abs(mean1 - mean2)
    if effect < 0.01:  # Effect too small to detect
        return None

    # Pooled standard deviation
    pooled_var = (std1 ** 2 + std2 ** 2) / 2
    if pooled_var <= 0:
        return None

    # Sample size formula for two means
    # n = 2 * (z_alpha + z_beta)^2 * pooled_var / effect^2
    numerator = 2 * ((Z_ALPHA_TWO_SIDED + Z_BETA) ** 2) * pooled_var
    n = numerator / (effect ** 2)

    return int(math.ceil(n))


def estimate_days_to_significance(
    required_n: Optional[int],
    current_n: int,
    days_elapsed: int,
    already_significant: bool
) -> Optional[int]:
    """
    Estimate remaining days to reach statistical significance.

    Args:
        required_n: Required sample size per group (None if undeterminable)
        current_n: Current sample size per group
        days_elapsed: Number of days of data collection so far
        already_significant: Whether result is already significant

    Returns:
        Estimated days remaining, or None if cannot be determined
    """
    if already_significant:
        return 0

    if required_n is None:
        return None  # Cannot determine (effect too small)

    if days_elapsed <= 0:
        return None

    # Daily rate of sample collection
    daily_rate = current_n / days_elapsed

    if daily_rate <= 0:
        return None

    # Remaining samples needed
    remaining_n = required_n - current_n

    if remaining_n <= 0:
        # We have enough samples but not significant - effect may be smaller than observed
        # Recalculate assuming we need 2x current sample
        remaining_n = current_n

    days_remaining = int(math.ceil(remaining_n / daily_rate))

    return days_remaining


def append_api_call_entry(log_path: Path, entry: Dict[str, Any]) -> None:
    """Append a single API call entry to api_calls.log."""
    lines = ["---"]
    for key, value in entry.items():
        if isinstance(value, list):
            rendered = "[" + ", ".join(str(item) for item in value) + "]"
        else:
            rendered = str(value)
        lines.append(f"{key}: {rendered}")
    with open(log_path, "a") as f:
        f.write("\n".join(lines) + "\n")


def write_api_calls_log(
    output_dir: Path,
    property_id: str,
    start_date: str,
    end_date: str,
    ab_test_dimension: str,
    treatment: str,
    control: str,
    users: Dict[str, int],
    sessions: Dict[str, int],
    total_rows: int
) -> None:
    """Write standardized API call entries for reproducibility."""
    log_path = output_dir / "api_calls.log"
    timestamp = datetime.now().isoformat()
    base = {
        "timestamp": timestamp,
        "property_id": property_id,
        "date_range": f"{start_date} to {end_date}",
        "dimension_filter": f"{ab_test_dimension} IN (\"{treatment}\", \"{control}\")",
        "case_sensitive": "true",
    }

    append_api_call_entry(log_path, {
        **base,
        "call": "run_report (users + sessions)",
        "dimensions": [ab_test_dimension],
        "metrics": ["totalUsers", "sessions"],
        "row_count": 2,
        "users_treatment": users.get(treatment, 0),
        "users_control": users.get(control, 0),
        "sessions_treatment": sessions.get(treatment, 0),
        "sessions_control": sessions.get(control, 0),
        "note": "Derived from user/session counts input to run_full_evaluation.py",
    })

    # Determine truncation status
    if total_rows == 10000:
        truncation_status = "TRUNCATED_DEFAULT_LIMIT"
    elif total_rows == 50000:
        truncation_status = "POSSIBLY_TRUNCATED_50K"
    elif total_rows >= 100000:
        truncation_status = "TRUNCATED_NEEDS_PAGINATION"
    else:
        truncation_status = "OK"

    append_api_call_entry(log_path, {
        **base,
        "call": "run_report (transactions)",
        "dimensions": [ab_test_dimension, "transactionId"],
        "metrics": ["purchaseRevenue"],
        "limit_requested": 100000,
        "row_count": total_rows,
        "truncation_check": truncation_status,
    })


def load_and_validate_raw_data(
    raw_file: Path,
    treatment: str,
    control: str
) -> Tuple[Dict[str, List[Dict[str, Any]]], int]:
    """
    Load raw GA4 API response and validate it.

    Returns:
        Tuple of (transactions_by_group, total_row_count)
    """
    with open(raw_file, 'r') as f:
        data = json.load(f)

    # Navigate to rows
    rows = data.get('result', data).get('rows', [])
    total_rows = len(rows)

    # CRITICAL: Check for truncation at various limit thresholds
    if total_rows == 10000:
        print("=" * 70)
        print("⛔ CRITICAL ERROR: DATA TRUNCATED AT DEFAULT LIMIT!")
        print("=" * 70)
        print(f"Row count is exactly 10,000 - GA4 default limit was hit.")
        print(f"The LLM forgot to set 'limit: 100000' in the API call.")
        print(f"Results will be INVALID - missing transactions.")
        print()
        print("REQUIRED ACTION: Re-run the GA4 query with 'limit: 100000'")
        print("=" * 70)
        sys.exit(1)

    if total_rows == 50000:
        print("=" * 70)
        print("⚠️ WARNING: DATA MAY BE TRUNCATED AT 50,000 ROWS!")
        print("=" * 70)
        print(f"Row count is exactly 50,000 - old limit may have been hit.")
        print(f"Consider re-running with 'limit: 100000' or using pagination.")
        print("=" * 70)
        # Continue but warn - may be coincidence

    if total_rows >= 100000:
        print("=" * 70)
        print("⛔ CRITICAL: ROW COUNT >= 100,000 - PAGINATION REQUIRED!")
        print("=" * 70)
        print(f"Row count is {total_rows:,} - limit was likely hit.")
        print(f"Use pagination: query with offset: 100000, then offset: 200000, etc.")
        print(f"Merge all responses before saving to transactions_raw.json")
        print("=" * 70)
        sys.exit(1)

    # Check for sampling metadata
    result = data.get('result', data)
    metadata = result.get('metadata', {})
    sampling = metadata.get('sampling_metadatas', [])
    if sampling:
        samples_read = int(sampling[0].get('samples_read_count', 0))
        sampling_space = int(sampling[0].get('sampling_space_size', 0))
        pct = (samples_read / sampling_space * 100) if sampling_space else 100
        print("=" * 70)
        print(f"⚠️ WARNING: DATA IS SAMPLED - {pct:.1f}% of events analyzed!")
        print("=" * 70)
        print(f"Only {samples_read:,} of {sampling_space:,} events were analyzed.")
        print(f"Consider using shorter date ranges to avoid sampling.")
        print("=" * 70)

    if total_rows < MIN_EXPECTED_ROWS:
        print(f"⚠️ WARNING: Only {total_rows} rows found. Verify this is expected for the domain.")

    # Parse transactions by group (keep tx_id and date for reproducibility)
    transactions: Dict[str, List[Dict[str, Any]]] = {treatment: [], control: []}

    for row in rows:
        dim_values = row.get('dimension_values', [])
        metric_values = row.get('metric_values', [])

        if len(dim_values) < 2 or not metric_values:
            continue

        group = dim_values[0].get('value', '')
        tx_id = dim_values[1].get('value', '')
        # Date is optional for backward compatibility with old data
        date = dim_values[2].get('value', '') if len(dim_values) > 2 else ''

        if group not in [treatment, control]:
            continue

        try:
            revenue = float(metric_values[0].get('value', 0))
        except (ValueError, TypeError):
            continue

        transactions[group].append({
            "group": group,
            "tx_id": tx_id,
            "date": date,
            "revenue": revenue,
        })

    return transactions, total_rows


def apply_outlier_removal(
    transactions: Dict[str, List[Dict[str, Any]]]
) -> Tuple[Dict[str, List[Dict[str, Any]]], float, Dict[str, int]]:
    """
    Apply 99th percentile outlier removal (HARDCODED methodology).

    Returns:
        Tuple of (clean_transactions, threshold, outliers_removed_per_group)
    """
    # Combine all revenues to calculate threshold
    all_revenues = []
    for records in transactions.values():
        all_revenues.extend([r["revenue"] for r in records])

    if not all_revenues:
        return transactions, 0.0, {}

    # HARDCODED: 99th percentile
    threshold = calculate_percentile(all_revenues, OUTLIER_PERCENTILE)

    # Apply threshold
    clean_transactions: Dict[str, List[Dict[str, Any]]] = {}
    outliers_removed: Dict[str, int] = {}

    for group, records in transactions.items():
        clean = [r for r in records if r["revenue"] <= threshold]
        clean_transactions[group] = clean
        outliers_removed[group] = len(records) - len(clean)

    return clean_transactions, threshold, outliers_removed


def calculate_metrics(
    clean_transactions: Dict[str, List[Dict[str, Any]]],
    users: Dict[str, int],
    treatment: str,
    control: str,
    days_elapsed: int = 1,
    sessions: Optional[Dict[str, int]] = None
) -> Dict:
    """Calculate all metrics from clean data."""

    metrics = {}
    sessions = sessions or {}

    for group in [treatment, control]:
        records = clean_transactions.get(group, [])
        revenues = [r["revenue"] for r in records]
        user_count = users.get(group, 0)
        session_count = sessions.get(group, 0)

        tx_count = len(revenues)
        total_revenue = sum(revenues)
        aov = total_revenue / tx_count if tx_count > 0 else 0
        user_cvr = (tx_count / user_count * 100) if user_count > 0 else 0
        session_cvr = (tx_count / session_count * 100) if session_count > 0 else 0
        rpu = total_revenue / user_count if user_count > 0 else 0

        # Calculate AOV standard deviation for the group
        if tx_count > 1:
            aov_variance = sum((r - aov) ** 2 for r in revenues) / (tx_count - 1)
            aov_std = math.sqrt(aov_variance)
        else:
            aov_std = 0.0

        metrics[group] = {
            'users': user_count,
            'sessions': session_count,
            'transactions': tx_count,
            'revenue': total_revenue,
            'aov': aov,
            'aov_std': aov_std,
            'user_cvr': user_cvr,
            'session_cvr': session_cvr,
            'cvr': user_cvr,  # Keep 'cvr' as alias for user_cvr for backward compatibility
            'rpu': rpu,
            'revenues': revenues  # Keep for AOV z-test
        }

    # Calculate differences (treatment vs control)
    ctrl = metrics[control]
    treat = metrics[treatment]

    metrics['differences'] = {
        'transactions_pct': ((treat['transactions'] / ctrl['transactions']) - 1) * 100 if ctrl['transactions'] > 0 else 0,
        'revenue_pct': ((treat['revenue'] / ctrl['revenue']) - 1) * 100 if ctrl['revenue'] > 0 else 0,
        'aov_pct': ((treat['aov'] / ctrl['aov']) - 1) * 100 if ctrl['aov'] > 0 else 0,
        'user_cvr_pct': ((treat['user_cvr'] / ctrl['user_cvr']) - 1) * 100 if ctrl['user_cvr'] > 0 else 0,
        'session_cvr_pct': ((treat['session_cvr'] / ctrl['session_cvr']) - 1) * 100 if ctrl['session_cvr'] > 0 else 0,
        'cvr_pct': ((treat['user_cvr'] / ctrl['user_cvr']) - 1) * 100 if ctrl['user_cvr'] > 0 else 0,  # Alias for backward compat
        'rpu_pct': ((treat['rpu'] / ctrl['rpu']) - 1) * 100 if ctrl['rpu'] > 0 else 0,
    }

    # Statistical significance (User CVR) - Two-proportion z-test
    user_cvr_z_stat, user_cvr_p_value = two_proportion_ztest(
        ctrl['users'], ctrl['transactions'],
        treat['users'], treat['transactions']
    )
    user_cvr_significant = user_cvr_p_value < SIGNIFICANCE_LEVEL

    # Statistical significance (Session CVR) - Two-proportion z-test
    if ctrl['sessions'] > 0 and treat['sessions'] > 0:
        session_cvr_z_stat, session_cvr_p_value = two_proportion_ztest(
            ctrl['sessions'], ctrl['transactions'],
            treat['sessions'], treat['transactions']
        )
        session_cvr_significant = session_cvr_p_value < SIGNIFICANCE_LEVEL
    else:
        session_cvr_z_stat, session_cvr_p_value = 0.0, 1.0
        session_cvr_significant = False

    metrics['statistics'] = {
        # User CVR statistics (PRIMARY)
        'user_cvr_z_statistic': user_cvr_z_stat,
        'user_cvr_p_value': user_cvr_p_value,
        'user_cvr_significant': user_cvr_significant,
        # Session CVR statistics (SECONDARY)
        'session_cvr_z_statistic': session_cvr_z_stat,
        'session_cvr_p_value': session_cvr_p_value,
        'session_cvr_significant': session_cvr_significant,
        # Keep legacy keys for backward compatibility (use user CVR)
        'cvr_z_statistic': user_cvr_z_stat,
        'cvr_p_value': user_cvr_p_value,
        'cvr_significant': user_cvr_significant,
        'z_statistic': user_cvr_z_stat,
        'p_value': user_cvr_p_value,
        'significant': user_cvr_significant,
        'alpha': SIGNIFICANCE_LEVEL
    }

    # Statistical significance (AOV) - Two-sample z-test for means
    aov_z_stat, aov_p_value, _, _ = two_sample_mean_ztest(
        treat['revenues'],
        ctrl['revenues']
    )

    aov_significant = aov_p_value < SIGNIFICANCE_LEVEL

    metrics['statistics']['aov_z_statistic'] = aov_z_stat
    metrics['statistics']['aov_p_value'] = aov_p_value
    metrics['statistics']['aov_significant'] = aov_significant

    # Calculate days to significance estimates
    # User CVR: based on user counts
    user_cvr_treat_prop = treat['transactions'] / treat['users'] if treat['users'] > 0 else 0
    user_cvr_ctrl_prop = ctrl['transactions'] / ctrl['users'] if ctrl['users'] > 0 else 0

    user_cvr_required_n = required_sample_size_proportions(user_cvr_treat_prop, user_cvr_ctrl_prop)
    avg_users_per_group = (treat['users'] + ctrl['users']) / 2

    user_cvr_days_to_sig = estimate_days_to_significance(
        user_cvr_required_n,
        int(avg_users_per_group),
        days_elapsed,
        user_cvr_significant
    )

    metrics['statistics']['user_cvr_required_n'] = user_cvr_required_n
    metrics['statistics']['user_cvr_days_to_significance'] = user_cvr_days_to_sig
    # Legacy keys
    metrics['statistics']['cvr_required_n'] = user_cvr_required_n
    metrics['statistics']['cvr_days_to_significance'] = user_cvr_days_to_sig

    # Session CVR: based on session counts
    if ctrl['sessions'] > 0 and treat['sessions'] > 0:
        session_cvr_treat_prop = treat['transactions'] / treat['sessions']
        session_cvr_ctrl_prop = ctrl['transactions'] / ctrl['sessions']

        session_cvr_required_n = required_sample_size_proportions(session_cvr_treat_prop, session_cvr_ctrl_prop)
        avg_sessions_per_group = (treat['sessions'] + ctrl['sessions']) / 2

        session_cvr_days_to_sig = estimate_days_to_significance(
            session_cvr_required_n,
            int(avg_sessions_per_group),
            days_elapsed,
            session_cvr_significant
        )
    else:
        session_cvr_required_n = None
        session_cvr_days_to_sig = None

    metrics['statistics']['session_cvr_required_n'] = session_cvr_required_n
    metrics['statistics']['session_cvr_days_to_significance'] = session_cvr_days_to_sig

    # AOV: based on transaction counts
    aov_required_n = required_sample_size_means(
        treat['aov'], ctrl['aov'],
        treat['aov_std'], ctrl['aov_std']
    )
    avg_tx_per_group = (treat['transactions'] + ctrl['transactions']) / 2

    aov_days_to_sig = estimate_days_to_significance(
        aov_required_n,
        int(avg_tx_per_group),
        days_elapsed,
        aov_significant
    )

    metrics['statistics']['aov_required_n'] = aov_required_n
    metrics['statistics']['aov_days_to_significance'] = aov_days_to_sig

    # Clean up revenues from metrics (not needed in output)
    for group in [treatment, control]:
        del metrics[group]['revenues']

    return metrics


def save_clean_csv(
    clean_transactions: Dict[str, List[Dict[str, Any]]],
    output_file: Path,
    group_order: Optional[List[str]] = None
) -> None:
    """Save clean transactions to CSV."""
    with open(output_file, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['group', 'tx_id', 'date', 'revenue'])
        groups = group_order if group_order else sorted(clean_transactions.keys())
        for group in groups:
            for record in clean_transactions.get(group, []):
                writer.writerow([record["group"], record["tx_id"], record.get("date", ""), record["revenue"]])


def summarize_transactions(records: List[Dict[str, Any]]) -> Tuple[int, float]:
    """Return (count, revenue_sum) for a list of transaction records."""
    count = len(records)
    revenue_sum = sum(r["revenue"] for r in records)
    return count, revenue_sum


def safe_percent(numerator: int, denominator: int) -> float:
    """Return percentage or 0.0 when denominator is zero."""
    return (numerator / denominator * 100) if denominator else 0.0


def format_days_to_sig(days: Optional[int]) -> str:
    """Format days to significance for display in report."""
    if days is None:
        return "N/A"
    elif days == 0:
        return "✓"
    elif days > 365:
        return ">365"
    else:
        return str(days)


def build_aligned_table(headers: List[str], rows: List[List[str]], alignments: Optional[List[str]] = None) -> str:
    """
    Build a markdown table with properly aligned columns.

    Args:
        headers: List of column header strings
        rows: List of rows, each row is a list of cell values
        alignments: Optional list of alignments ('left', 'right', 'center') per column.
                   Defaults to 'left' for all columns.

    Returns:
        Formatted markdown table string with aligned columns
    """
    if not headers or not rows:
        return ""

    num_cols = len(headers)
    if alignments is None:
        alignments = ['left'] * num_cols

    # Calculate max width for each column (accounting for emoji width)
    def display_width(s: str) -> int:
        """Calculate display width, accounting for emojis taking 2 chars."""
        width = 0
        for char in s:
            # Common emojis used in reports
            if char in '✅⚠️❌✓⛔':
                width += 2
            else:
                width += 1
        return width

    col_widths = []
    for i in range(num_cols):
        max_width = display_width(headers[i])
        for row in rows:
            if i < len(row):
                max_width = max(max_width, display_width(row[i]))
        col_widths.append(max_width)

    def pad_cell(value: str, width: int, align: str) -> str:
        """Pad a cell value to the specified width with given alignment."""
        current_width = display_width(value)
        padding_needed = width - current_width
        if padding_needed <= 0:
            return value

        if align == 'right':
            return ' ' * padding_needed + value
        elif align == 'center':
            left_pad = padding_needed // 2
            right_pad = padding_needed - left_pad
            return ' ' * left_pad + value + ' ' * right_pad
        else:  # left
            return value + ' ' * padding_needed

    # Build header row
    header_cells = [pad_cell(headers[i], col_widths[i], alignments[i]) for i in range(num_cols)]
    header_line = '| ' + ' | '.join(header_cells) + ' |'

    # Build separator row with alignment markers
    sep_cells = []
    for i in range(num_cols):
        if alignments[i] == 'right':
            sep_cells.append('-' * (col_widths[i] - 1) + ':')
        elif alignments[i] == 'center':
            sep_cells.append(':' + '-' * (col_widths[i] - 2) + ':')
        else:  # left
            sep_cells.append('-' * col_widths[i])
    sep_line = '| ' + ' | '.join(sep_cells) + ' |'

    # Build data rows
    data_lines = []
    for row in rows:
        # Pad row if needed
        padded_row = list(row) + [''] * (num_cols - len(row))
        cells = [pad_cell(padded_row[i], col_widths[i], alignments[i]) for i in range(num_cols)]
        data_lines.append('| ' + ' | '.join(cells) + ' |')

    return '\n'.join([header_line, sep_line] + data_lines)


def generate_markdown_report(
    domain: str,
    property_id: str,
    start_date: str,
    end_date: str,
    ab_test_dimension: str,
    treatment: str,
    control: str,
    raw_transactions: Dict[str, List[Dict[str, Any]]],
    clean_transactions: Dict[str, List[Dict[str, Any]]],
    threshold: float,
    outliers_removed: Dict[str, int],
    metrics: Dict,
    total_rows: int,
    generated_charts: Optional[Dict[str, Path]] = None
) -> str:
    """Generate the final markdown report."""

    treat = metrics[treatment]
    ctrl = metrics[control]
    diff = metrics['differences']
    stats = metrics['statistics']

    # Determine status
    if stats['significant']:
        if diff['cvr_pct'] > 0:
            status = "OUTPERFORMING"
        else:
            status = "UNDERPERFORMING"
    else:
        status = "INCONCLUSIVE"

    days = (datetime.strptime(end_date, '%Y-%m-%d') - datetime.strptime(start_date, '%Y-%m-%d')).days + 1

    raw_treatment_count, raw_treatment_revenue = summarize_transactions(raw_transactions[treatment])
    raw_control_count, raw_control_revenue = summarize_transactions(raw_transactions[control])

    # Build charts section dynamically based on what was generated
    charts_section_parts = []
    if generated_charts:
        if "cvr_diff_daily" in generated_charts:
            charts_section_parts.append(f"""### Daily CVR Difference
![Daily CVR Difference](charts/cvr_diff_daily.png)

> Green bars indicate days where {treatment} outperforms {control}. Red bars indicate underperformance.""")
        elif "tx_diff_daily" in generated_charts:
            charts_section_parts.append(f"""### Daily Transaction Difference
![Daily Transaction Difference](charts/tx_diff_daily.png)

> Green bars indicate days where {treatment} has more transactions than {control}. Red bars indicate fewer transactions.
> Note: Daily CVR chart requires --daily-users-file parameter with daily user counts per group.""")

        if "aov_diff_daily" in generated_charts:
            charts_section_parts.append(f"""### Daily AOV Difference
![Daily AOV Difference](charts/aov_diff_daily.png)

> Green bars indicate days where {treatment} has higher AOV than {control}. Red bars indicate lower AOV.""")

        if "cvr_posterior" in generated_charts:
            charts_section_parts.append(f"""### CVR Posterior Distributions (Bayesian)
![CVR Posterior Distributions](charts/cvr_posterior.png)

> Shows the probability distributions of true conversion rates for each group based on observed data.
> The title shows the probability that {treatment} has a higher true CVR than {control}.""")

    if charts_section_parts:
        charts_section = "\n\n".join(charts_section_parts)
    else:
        charts_section = "_No charts generated. Ensure the GA4 query includes the `date` dimension and chart libraries (altair, scipy) are installed._"

    # Build Executive Summary table
    exec_summary_headers = ['Metric', treatment, control, 'Difference', 'p-value', 'Days to Sig', 'Status']
    exec_summary_rows = [
        ['**User CVR**', f"{treat['user_cvr']:.2f}%", f"{ctrl['user_cvr']:.2f}%", f"{diff['user_cvr_pct']:+.2f}%", f"{stats['user_cvr_p_value']:.4f}",
         format_days_to_sig(stats.get('user_cvr_days_to_significance')),
         "✅" if stats['user_cvr_significant'] and diff['user_cvr_pct'] > 0 else "⚠️" if not stats['user_cvr_significant'] else "❌"],
        ['Session CVR', f"{treat['session_cvr']:.2f}%" if treat['sessions'] > 0 else "N/A", f"{ctrl['session_cvr']:.2f}%" if ctrl['sessions'] > 0 else "N/A",
         f"{diff['session_cvr_pct']:+.2f}%" if ctrl['sessions'] > 0 else "N/A", f"{stats['session_cvr_p_value']:.4f}" if ctrl['sessions'] > 0 else "N/A",
         format_days_to_sig(stats.get('session_cvr_days_to_significance')),
         "✅" if stats['session_cvr_significant'] and diff['session_cvr_pct'] > 0 else "⚠️" if not stats['session_cvr_significant'] else "❌"] if ctrl['sessions'] > 0 else ['Session CVR', 'N/A', 'N/A', 'N/A', 'N/A', 'N/A', '-'],
        ['AOV', f"{treat['aov']:,.2f}", f"{ctrl['aov']:,.2f}", f"{diff['aov_pct']:+.2f}%", f"{stats['aov_p_value']:.4f}",
         format_days_to_sig(stats.get('aov_days_to_significance')),
         "✅" if stats['aov_significant'] and diff['aov_pct'] > 0 else "⚠️" if not stats['aov_significant'] else "❌"],
        ['Revenue', f"{treat['revenue']:,.2f}", f"{ctrl['revenue']:,.2f}", f"{diff['revenue_pct']:+.2f}%", '-', '-', '-'],
        ['RPU', f"{treat['rpu']:,.2f}", f"{ctrl['rpu']:,.2f}", f"{diff['rpu_pct']:+.2f}%", '-', '-', '-'],
    ]
    exec_summary_table = build_aligned_table(exec_summary_headers, exec_summary_rows,
                                              ['left', 'right', 'right', 'right', 'right', 'right', 'center'])

    # Build Data Verification table
    verification_headers = ['Metric', treatment, control, 'Total']
    verification_rows = [
        ['Users', f"{treat['users']:,}", f"{ctrl['users']:,}", f"{treat['users'] + ctrl['users']:,}"],
        ['Sessions', f"{treat['sessions']:,}" if treat['sessions'] > 0 else "N/A", f"{ctrl['sessions']:,}" if ctrl['sessions'] > 0 else "N/A", f"{treat['sessions'] + ctrl['sessions']:,}" if treat['sessions'] > 0 else "N/A"],
        ['Transactions (raw)', f"{raw_treatment_count:,}", f"{raw_control_count:,}", f"{raw_treatment_count + raw_control_count:,}"],
        ['Revenue (raw)', f"{raw_treatment_revenue:,.2f}", f"{raw_control_revenue:,.2f}", f"{raw_treatment_revenue + raw_control_revenue:,.2f}"],
        ['Transactions (clean)', f"{treat['transactions']:,}", f"{ctrl['transactions']:,}", f"{treat['transactions'] + ctrl['transactions']:,}"],
        ['Revenue (clean)', f"{treat['revenue']:,.2f}", f"{ctrl['revenue']:,.2f}", f"{treat['revenue'] + ctrl['revenue']:,.2f}"],
    ]
    verification_table = build_aligned_table(verification_headers, verification_rows,
                                              ['left', 'right', 'right', 'right'])

    # Build User Split table
    user_split_headers = ['Group', 'Users', 'Percentage']
    user_split_rows = [
        [treatment, f"{treat['users']:,}", f"{treat['users']/(treat['users']+ctrl['users'])*100:.2f}%"],
        [control, f"{ctrl['users']:,}", f"{ctrl['users']/(treat['users']+ctrl['users'])*100:.2f}%"],
    ]
    user_split_table = build_aligned_table(user_split_headers, user_split_rows,
                                            ['left', 'right', 'right'])

    # Build Raw Transactions table
    raw_tx_headers = ['Group', 'Transactions', 'Revenue']
    raw_tx_rows = [
        [treatment, f"{raw_treatment_count:,}", f"{raw_treatment_revenue:,.2f}"],
        [control, f"{raw_control_count:,}", f"{raw_control_revenue:,.2f}"],
    ]
    raw_tx_table = build_aligned_table(raw_tx_headers, raw_tx_rows,
                                        ['left', 'right', 'right'])

    # Build Clean Transactions table
    clean_tx_headers = ['Group', 'Transactions', 'Revenue', 'AOV', 'AOV Std Dev']
    clean_tx_rows = [
        [treatment, f"{treat['transactions']:,}", f"{treat['revenue']:,.2f}", f"{treat['aov']:,.2f}", f"{treat['aov_std']:,.2f}"],
        [control, f"{ctrl['transactions']:,}", f"{ctrl['revenue']:,.2f}", f"{ctrl['aov']:,.2f}", f"{ctrl['aov_std']:,.2f}"],
    ]
    clean_tx_table = build_aligned_table(clean_tx_headers, clean_tx_rows,
                                          ['left', 'right', 'right', 'right', 'right'])

    # Build User CVR Comparison table
    user_cvr_headers = ['Group', 'Transactions', 'Users', 'User CVR']
    user_cvr_rows = [
        [treatment, f"{treat['transactions']:,}", f"{treat['users']:,}", f"{treat['user_cvr']:.4f}%"],
        [control, f"{ctrl['transactions']:,}", f"{ctrl['users']:,}", f"{ctrl['user_cvr']:.4f}%"],
    ]
    user_cvr_table = build_aligned_table(user_cvr_headers, user_cvr_rows,
                                          ['left', 'right', 'right', 'right'])

    # Build Session CVR Comparison table (if sessions available)
    if treat['sessions'] > 0 and ctrl['sessions'] > 0:
        session_cvr_headers = ['Group', 'Transactions', 'Sessions', 'Session CVR']
        session_cvr_rows = [
            [treatment, f"{treat['transactions']:,}", f"{treat['sessions']:,}", f"{treat['session_cvr']:.4f}%"],
            [control, f"{ctrl['transactions']:,}", f"{ctrl['sessions']:,}", f"{ctrl['session_cvr']:.4f}%"],
        ]
        session_cvr_table = build_aligned_table(session_cvr_headers, session_cvr_rows,
                                                 ['left', 'right', 'right', 'right'])
    else:
        session_cvr_table = None

    report = f"""# AB Test Evaluation: {domain}

**Domain:** {domain}
**Property ID:** {property_id}
**Test Period:** {start_date} to {end_date} ({days} days)
**Evaluation Date:** {datetime.now().strftime('%Y-%m-%d')}
**AB Test Dimension:** {ab_test_dimension}
**Treatment Group:** {treatment}
**Control Group:** {control}

---

## Executive Summary

{exec_summary_table}

> **User CVR** = transactions / unique users (PRIMARY metric - measures user-level conversion)
> **Session CVR** = transactions / sessions (SECONDARY metric - measures session-level conversion)

**User CVR Statistical Significance:** p = {stats['user_cvr_p_value']:.4f} ({"SIGNIFICANT" if stats['user_cvr_significant'] else "NOT SIGNIFICANT"} at α = {stats['alpha']})
**Session CVR Statistical Significance:** p = {stats['session_cvr_p_value']:.4f} ({"SIGNIFICANT" if stats['session_cvr_significant'] else "NOT SIGNIFICANT"} at α = {stats['alpha']}) {'' if ctrl['sessions'] > 0 else '(N/A - sessions not provided)'}
**AOV Statistical Significance:** p = {stats['aov_p_value']:.4f} ({"SIGNIFICANT" if stats['aov_significant'] else "NOT SIGNIFICANT"} at α = {stats['alpha']})

**Status:** {status}

---

## Data Verification (Compare with GA4)

Use this table to verify the data matches what you see in Google Analytics:

{verification_table}

> **Note:** "Raw" = before outlier removal, "Clean" = after 99th percentile outlier removal.
> Compare "raw" values with GA4 to verify data extraction is correct.

---

## Data Validation

### Row Count Check
- **Total rows from GA4:** {total_rows:,}
- **Truncation check:** {"⛔ FAILED - DEFAULT LIMIT HIT" if total_rows == 10000 else "⚠️ POSSIBLY TRUNCATED AT 50K" if total_rows == 50000 else "⛔ NEEDS PAGINATION" if total_rows >= 100000 else "✅ PASSED"}

### User Split
{user_split_table}

---

## Transaction Data

### Raw Transactions
{raw_tx_table}

### Outlier Removal (99th Percentile)
- **Threshold:** {threshold:,.2f}
- **{treatment} outliers removed:** {outliers_removed[treatment]} ({safe_percent(outliers_removed[treatment], raw_treatment_count):.2f}%)
- **{control} outliers removed:** {outliers_removed[control]} ({safe_percent(outliers_removed[control], raw_control_count):.2f}%)

### Clean Transactions
{clean_tx_table}

---

## Statistical Analysis

### User CVR Comparison (PRIMARY)
{user_cvr_table}

**Relative Difference:** {diff['user_cvr_pct']:+.2f}%

### Two-Proportion Z-Test (User CVR)
```
Test: Two-Proportion Z-Test (Two-Sided)
----------------------------------------------------------------------
  Group 1 ({control}): {ctrl['users']:,} users, {ctrl['transactions']:,} conversions ({ctrl['user_cvr']:.2f}%)
  Group 2 ({treatment}): {treat['users']:,} users, {treat['transactions']:,} conversions ({treat['user_cvr']:.2f}%)

  Z-statistic: {stats['user_cvr_z_statistic']:.4f}
  P-value:     {stats['user_cvr_p_value']:.4f}
  Alpha:       {stats['alpha']}
  Result:      {"SIGNIFICANT" if stats['user_cvr_significant'] else "NOT SIGNIFICANT"}
```

### Session CVR Comparison (SECONDARY)
{session_cvr_table if session_cvr_table else "_Session data not provided - skipping Session CVR analysis._"}

{f'''**Relative Difference:** {diff['session_cvr_pct']:+.2f}%

### Two-Proportion Z-Test (Session CVR)
```
Test: Two-Proportion Z-Test (Two-Sided)
----------------------------------------------------------------------
  Group 1 ({control}): {ctrl['sessions']:,} sessions, {ctrl['transactions']:,} conversions ({ctrl['session_cvr']:.2f}%)
  Group 2 ({treatment}): {treat['sessions']:,} sessions, {treat['transactions']:,} conversions ({treat['session_cvr']:.2f}%)

  Z-statistic: {stats['session_cvr_z_statistic']:.4f}
  P-value:     {stats['session_cvr_p_value']:.4f}
  Alpha:       {stats['alpha']}
  Result:      {"SIGNIFICANT" if stats['session_cvr_significant'] else "NOT SIGNIFICANT"}
```''' if session_cvr_table else ''}

### Two-Sample Z-Test (AOV)
```
Test: Two-Sample Z-Test for Means (Two-Sided)
----------------------------------------------------------------------
  Group 1 ({control}): {ctrl['transactions']:,} transactions, AOV = {ctrl['aov']:,.2f} (σ = {ctrl['aov_std']:,.2f})
  Group 2 ({treatment}): {treat['transactions']:,} transactions, AOV = {treat['aov']:,.2f} (σ = {treat['aov_std']:,.2f})

  Z-statistic: {stats['aov_z_statistic']:.4f}
  P-value:     {stats['aov_p_value']:.4f}
  Alpha:       {stats['alpha']}
  Result:      {"SIGNIFICANT" if stats['aov_significant'] else "NOT SIGNIFICANT"}
```

---

## Charts

{charts_section}

---

## Methodology (HARDCODED)

This report was generated using deterministic methodology:

1. **Outlier Removal:** 99th percentile threshold ({threshold:,.2f})
   - Percentile method: **INCLUSIVE** (equivalent to Excel's `PERCENTILE.INC`)
   - Formula: `index = percentile × (n - 1)` with linear interpolation
   - Threshold application: transactions with revenue **≤ threshold are KEPT**
   - To verify in Excel: `=PERCENTILE.INC(revenue_range, 0.99)`
2. **User CVR Formula:** transactions / users (PRIMARY metric)
3. **Session CVR Formula:** transactions / sessions (SECONDARY metric)
4. **CVR Statistical Tests:** Two-proportion Z-test (two-sided) for both User and Session CVR
5. **AOV Statistical Test:** Two-sample Z-test for means (two-sided)
6. **Significance Level:** α = {stats['alpha']}
7. **Days to Significance:** Power analysis with 80% power, α = 0.05 (two-sided)

---

*Report generated: {datetime.now().isoformat()}*
*Script: run_full_evaluation.py*
"""

    return report


def save_config_yaml(
    output_dir: Path,
    domain: str,
    property_id: str,
    ab_test_dimension: str,
    treatment: str,
    control: str,
    start_date: str,
    end_date: str,
    raw_transactions: Dict[str, List[Dict[str, Any]]],
    clean_transactions: Dict[str, List[Dict[str, Any]]],
    threshold: float,
    outliers_removed: Dict[str, int],
    metrics: Dict
) -> None:
    """Save config.yaml with results."""
    diff = metrics['differences']
    stats = metrics['statistics']
    treat = metrics[treatment]
    ctrl = metrics[control]

    raw_treatment_count, raw_treatment_revenue = summarize_transactions(raw_transactions[treatment])
    raw_control_count, raw_control_revenue = summarize_transactions(raw_transactions[control])
    total_removed = outliers_removed.get(treatment, 0) + outliers_removed.get(control, 0)

    days = (datetime.strptime(end_date, '%Y-%m-%d') - datetime.strptime(start_date, '%Y-%m-%d')).days + 1

    if stats['significant']:
        status = "outperforming" if diff['cvr_pct'] > 0 else "underperforming"
    else:
        status = "inconclusive"

    config = f"""domain: {domain}
property_id: {property_id}
ab_test_dimension: "{ab_test_dimension}"
treatment_group: "{treatment}"
control_group: "{control}"
test_start_date: "{start_date}"
last_evaluation_date: "{datetime.now().strftime('%Y-%m-%d')}"
latest_results:
  status: "{status}"
  evaluation_period:
    start: "{start_date}"
    end: "{end_date}"
    days: {days}
  statistical_tests:
    user_cvr:
      p_value: {stats['user_cvr_p_value']:.4f}
      z_statistic: {stats['user_cvr_z_statistic']:.4f}
      significant: {str(stats['user_cvr_significant']).lower()}
      required_n: {stats.get('user_cvr_required_n') or 'null'}
      days_to_significance: {stats.get('user_cvr_days_to_significance') or 'null'}
    session_cvr:
      p_value: {stats['session_cvr_p_value']:.4f}
      z_statistic: {stats['session_cvr_z_statistic']:.4f}
      significant: {str(stats['session_cvr_significant']).lower()}
      required_n: {stats.get('session_cvr_required_n') or 'null'}
      days_to_significance: {stats.get('session_cvr_days_to_significance') or 'null'}
    # Legacy key for backward compatibility
    cvr:
      p_value: {stats['cvr_p_value']:.4f}
      z_statistic: {stats['cvr_z_statistic']:.4f}
      significant: {str(stats['cvr_significant']).lower()}
      required_n: {stats.get('cvr_required_n') or 'null'}
      days_to_significance: {stats.get('cvr_days_to_significance') or 'null'}
    aov:
      p_value: {stats['aov_p_value']:.4f}
      z_statistic: {stats['aov_z_statistic']:.4f}
      significant: {str(stats['aov_significant']).lower()}
      required_n: {stats.get('aov_required_n') or 'null'}
      days_to_significance: {stats.get('aov_days_to_significance') or 'null'}
    alpha: {stats['alpha']}
  # Legacy key for backward compatibility
  p_value: {stats['cvr_p_value']:.4f}
  diffs_percent:
    user_cvr: {diff['user_cvr_pct']:.2f}
    session_cvr: {diff['session_cvr_pct']:.2f}
    cvr: {diff['cvr_pct']:.2f}  # Legacy key (same as user_cvr)
    revenue: {diff['revenue_pct']:.2f}
    aov: {diff['aov_pct']:.2f}
    revenue_per_user: {diff['rpu_pct']:.2f}
  users:
    "{treatment}": {treat['users']}
    "{control}": {ctrl['users']}
  sessions:
    "{treatment}": {treat['sessions']}
    "{control}": {ctrl['sessions']}
  raw_transactions:
    "{treatment}":
      count: {raw_treatment_count}
      revenue: {raw_treatment_revenue:.2f}
    "{control}":
      count: {raw_control_count}
      revenue: {raw_control_revenue:.2f}
  clean_transactions:
    "{treatment}":
      count: {treat['transactions']}
      revenue: {treat['revenue']:.2f}
      aov: {treat['aov']:.2f}
      aov_std: {treat['aov_std']:.2f}
    "{control}":
      count: {ctrl['transactions']}
      revenue: {ctrl['revenue']:.2f}
      aov: {ctrl['aov']:.2f}
      aov_std: {ctrl['aov_std']:.2f}
  outlier_removal:
    method: "99th_percentile"
    threshold: {threshold:.2f}
    total_removed: {total_removed}
    removed_by_group:
      "{treatment}": {outliers_removed.get(treatment, 0)}
      "{control}": {outliers_removed.get(control, 0)}
"""

    with open(output_dir / 'config.yaml', 'w') as f:
        f.write(config)


def write_completion_marker(output_dir: Path, metadata: Dict[str, Any]) -> Path:
    """Write a completion marker for deterministic runs."""
    marker_path = output_dir / "run_complete.json"
    payload = {"created_at": datetime.now().isoformat(), **metadata}
    with open(marker_path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    return marker_path


# =============================================================================
# CHART GENERATION FUNCTIONS
# =============================================================================

def calculate_daily_metrics(
    clean_transactions: Dict[str, List[Dict[str, Any]]],
    users: Dict[str, int],
    treatment: str,
    control: str,
    start_date: str,
    end_date: str,
    daily_users: Optional[Dict[str, Dict[str, int]]] = None
) -> List[Dict[str, Any]]:
    """
    Calculate daily metrics (CVR, AOV, revenue) for each group.

    Args:
        daily_users: Optional dict of {date: {group: user_count}}
                    If not provided, daily CVR cannot be calculated accurately.
    """
    from collections import defaultdict

    # Group transactions by date and group
    daily_data: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))

    for group in [treatment, control]:
        for record in clean_transactions.get(group, []):
            date = record.get("date", "")
            if date:
                daily_data[date][group].append(record["revenue"])

    if not daily_data:
        return []

    # Build daily metrics
    daily_metrics = []
    for date in sorted(daily_data.keys()):
        for group in [treatment, control]:
            revenues = daily_data[date].get(group, [])
            tx_count = len(revenues)
            revenue_sum = sum(revenues) if revenues else 0
            aov = revenue_sum / tx_count if tx_count > 0 else 0

            # Use actual daily users if provided, otherwise mark as unavailable
            if daily_users and date in daily_users and group in daily_users[date]:
                actual_users = daily_users[date][group]
                cvr = (tx_count / actual_users * 100) if actual_users > 0 else 0
            else:
                actual_users = 0
                cvr = 0  # Cannot calculate without daily user data

            daily_metrics.append({
                "date": date,
                "group": group,
                "transactions": tx_count,
                "revenue": revenue_sum,
                "aov": aov,
                "users": actual_users,
                "cvr": cvr
            })

    return daily_metrics


def calculate_daily_diffs(
    daily_metrics: List[Dict[str, Any]],
    treatment: str,
    control: str
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Calculate daily relative differences (treatment vs control).
    Positive values mean treatment outperforms control.

    Returns:
        Tuple of (diffs list, has_cvr_data bool)
    """
    from collections import defaultdict

    # Group by date
    by_date: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for m in daily_metrics:
        by_date[m["date"]][m["group"]] = m

    diffs = []
    has_cvr_data = False

    for date in sorted(by_date.keys()):
        treat = by_date[date].get(treatment, {})
        ctrl = by_date[date].get(control, {})

        # Check if we have real CVR data (users > 0)
        treat_users = treat.get("users", 0)
        ctrl_users = ctrl.get("users", 0)
        if treat_users > 0 and ctrl_users > 0:
            has_cvr_data = True

        # Calculate relative differences (treatment vs control)
        cvr_diff = ((treat.get("cvr", 0) / ctrl.get("cvr", 1)) - 1) * 100 if ctrl.get("cvr", 0) > 0 else 0
        aov_diff = ((treat.get("aov", 0) / ctrl.get("aov", 1)) - 1) * 100 if ctrl.get("aov", 0) > 0 else 0

        # Transaction count diff (always available)
        treat_tx = treat.get("transactions", 0)
        ctrl_tx = ctrl.get("transactions", 0)
        tx_diff = ((treat_tx / ctrl_tx) - 1) * 100 if ctrl_tx > 0 else 0

        diffs.append({
            "date": date,
            "cvr_diff_pct": cvr_diff,
            "aov_diff_pct": aov_diff,
            "tx_diff_pct": tx_diff,
            "treatment_cvr": treat.get("cvr", 0),
            "control_cvr": ctrl.get("cvr", 0),
            "treatment_aov": treat.get("aov", 0),
            "control_aov": ctrl.get("aov", 0),
            "treatment_tx": treat_tx,
            "control_tx": ctrl_tx,
            "treatment_users": treat_users,
            "control_users": ctrl_users,
        })

    return diffs, has_cvr_data


def generate_charts(
    daily_diffs: List[Dict[str, Any]],
    treatment: str,
    control: str,
    users: Dict[str, int],
    transactions: Dict[str, int],
    domain: str,
    output_dir: Path,
    has_cvr_data: bool = True
) -> Dict[str, Path]:
    """
    Generate PNG charts for the AB test evaluation.

    Returns dict of chart_name -> file_path for successfully generated charts.
    """
    if not CHARTS_AVAILABLE:
        print("  ⚠️ Chart generation skipped (altair/scipy not available)")
        return {}

    charts_dir = output_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)

    generated = {}

    # Convert to format suitable for Altair
    import pandas as pd
    df = pd.DataFrame(daily_diffs)

    if df.empty:
        print("  ⚠️ No daily data available for charts")
        return {}

    # Chart styling
    chart_width = 700
    chart_height = 300

    # 1. CVR Diff by Day chart (or Transaction Diff if no daily user data)
    try:
        if has_cvr_data:
            # Real CVR diff chart
            cvr_chart = (
                alt.Chart(df)
                .mark_bar(color="#4C78A8")
                .encode(
                    x=alt.X("date:O", title="Date", axis=alt.Axis(labelAngle=-45)),
                    y=alt.Y("cvr_diff_pct:Q", title="CVR Difference (%)",
                           axis=alt.Axis(format="+.1f")),
                    color=alt.condition(
                        alt.datum.cvr_diff_pct > 0,
                        alt.value("#2E7D32"),  # Green for positive
                        alt.value("#C62828")   # Red for negative
                    ),
                    tooltip=[
                        alt.Tooltip("date:O", title="Date"),
                        alt.Tooltip("cvr_diff_pct:Q", title="CVR Diff %", format="+.2f"),
                        alt.Tooltip("treatment_cvr:Q", title=f"{treatment} CVR %", format=".3f"),
                        alt.Tooltip("control_cvr:Q", title=f"{control} CVR %", format=".3f"),
                        alt.Tooltip("treatment_users:Q", title=f"{treatment} Users", format=",d"),
                        alt.Tooltip("control_users:Q", title=f"{control} Users", format=",d"),
                    ]
                )
                .properties(
                    width=chart_width,
                    height=chart_height,
                    title=f"Daily CVR Difference: {treatment} vs {control} ({domain})"
                )
            )
            chart_name = "cvr_diff_daily"
        else:
            # Fallback: Transaction count diff chart
            cvr_chart = (
                alt.Chart(df)
                .mark_bar(color="#4C78A8")
                .encode(
                    x=alt.X("date:O", title="Date", axis=alt.Axis(labelAngle=-45)),
                    y=alt.Y("tx_diff_pct:Q", title="Transaction Count Difference (%)",
                           axis=alt.Axis(format="+.1f")),
                    color=alt.condition(
                        alt.datum.tx_diff_pct > 0,
                        alt.value("#2E7D32"),  # Green for positive
                        alt.value("#C62828")   # Red for negative
                    ),
                    tooltip=[
                        alt.Tooltip("date:O", title="Date"),
                        alt.Tooltip("tx_diff_pct:Q", title="Tx Diff %", format="+.2f"),
                        alt.Tooltip("treatment_tx:Q", title=f"{treatment} Tx", format=",d"),
                        alt.Tooltip("control_tx:Q", title=f"{control} Tx", format=",d"),
                    ]
                )
                .properties(
                    width=chart_width,
                    height=chart_height,
                    title=f"Daily Transaction Difference: {treatment} vs {control} ({domain}) [CVR requires --daily-users-file]"
                )
            )
            chart_name = "tx_diff_daily"
            print("  ⚠️ No daily user data - showing transaction counts instead of CVR")

        # Add zero line
        zero_line = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="black", strokeDash=[3, 3]).encode(y="y:Q")
        cvr_chart = cvr_chart + zero_line

        cvr_path = charts_dir / f"{chart_name}.png"
        cvr_chart.save(str(cvr_path), scale_factor=2.0)
        generated[chart_name] = cvr_path
        print(f"  ✅ Generated: {cvr_path}")
    except Exception as e:
        print(f"  ⚠️ Failed to generate CVR/Tx chart: {e}")

    # 2. AOV Diff by Day chart
    try:
        aov_chart = (
            alt.Chart(df)
            .mark_bar()
            .encode(
                x=alt.X("date:O", title="Date", axis=alt.Axis(labelAngle=-45)),
                y=alt.Y("aov_diff_pct:Q", title="AOV Difference (%)",
                       axis=alt.Axis(format="+.1f")),
                color=alt.condition(
                    alt.datum.aov_diff_pct > 0,
                    alt.value("#2E7D32"),  # Green for positive
                    alt.value("#C62828")   # Red for negative
                ),
                tooltip=[
                    alt.Tooltip("date:O", title="Date"),
                    alt.Tooltip("aov_diff_pct:Q", title="AOV Diff %", format="+.2f"),
                    alt.Tooltip("treatment_aov:Q", title=f"{treatment} AOV", format=",.2f"),
                    alt.Tooltip("control_aov:Q", title=f"{control} AOV", format=",.2f"),
                ]
            )
            .properties(
                width=chart_width,
                height=chart_height,
                title=f"Daily AOV Difference: {treatment} vs {control} ({domain})"
            )
        )

        # Add zero line
        zero_line = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="black", strokeDash=[3, 3]).encode(y="y:Q")
        aov_chart = aov_chart + zero_line

        aov_path = charts_dir / "aov_diff_daily.png"
        aov_chart.save(str(aov_path), scale_factor=2.0)
        generated["aov_diff_daily"] = aov_path
        print(f"  ✅ Generated: {aov_path}")
    except Exception as e:
        print(f"  ⚠️ Failed to generate AOV chart: {e}")

    # 3. CVR Posterior Distribution (Hill Chart)
    try:
        posterior_chart = generate_posterior_chart(
            users, transactions, treatment, control, domain
        )
        if posterior_chart:
            posterior_path = charts_dir / "cvr_posterior.png"
            posterior_chart.save(str(posterior_path), scale_factor=2.0)
            generated["cvr_posterior"] = posterior_path
            print(f"  ✅ Generated: {posterior_path}")
    except Exception as e:
        print(f"  ⚠️ Failed to generate posterior chart: {e}")

    return generated


def generate_posterior_chart(
    users: Dict[str, int],
    transactions: Dict[str, int],
    treatment: str,
    control: str,
    domain: str,
    n_samples: int = 10000
) -> Optional[Any]:
    """
    Generate Bayesian posterior distribution chart for CVR comparison.

    Uses Beta-Binomial model:
    - Prior: Beta(1, 1) = Uniform
    - Posterior: Beta(1 + conversions, 1 + non-conversions)
    """
    if not CHARTS_AVAILABLE:
        return None

    import pandas as pd

    # Calculate posterior parameters
    groups_data = {}
    for group in [treatment, control]:
        n_users = users.get(group, 0)
        n_tx = transactions.get(group, 0)
        if n_users > 0:
            alpha = 1 + n_tx  # successes + prior
            beta_param = 1 + (n_users - n_tx)  # failures + prior
            groups_data[group] = {
                "alpha": alpha,
                "beta": beta_param,
                "mean": alpha / (alpha + beta_param),
                "samples": beta.rvs(alpha, beta_param, size=n_samples)
            }

    if len(groups_data) < 2:
        return None

    # Build dataframe for plotting
    plot_data = []
    for group, data in groups_data.items():
        for sample in data["samples"]:
            plot_data.append({
                "Group": group,
                "CVR": sample * 100,  # Convert to percentage
                "Mean": data["mean"] * 100
            })

    df = pd.DataFrame(plot_data)

    # Calculate credible intervals for annotation
    ci_data = []
    for group, data in groups_data.items():
        samples = data["samples"] * 100
        ci_low = np.percentile(samples, 2.5)
        ci_high = np.percentile(samples, 97.5)
        mean = data["mean"] * 100
        ci_data.append({
            "group": group,
            "mean": mean,
            "ci_low": ci_low,
            "ci_high": ci_high,
            "label": f"{group}: {mean:.2f}% [{ci_low:.2f}%, {ci_high:.2f}%]"
        })

    # Calculate probability that treatment > control
    treat_samples = groups_data[treatment]["samples"]
    ctrl_samples = groups_data[control]["samples"]
    prob_treat_better = np.mean(treat_samples > ctrl_samples) * 100

    # Create density chart
    base = alt.Chart(df).transform_density(
        "CVR",
        groupby=["Group"],
        as_=["CVR", "density"],
        extent=[
            min(df["CVR"]) - 0.5,
            max(df["CVR"]) + 0.5
        ]
    )

    density = base.mark_area(opacity=0.5).encode(
        x=alt.X("CVR:Q", title="Conversion Rate (%)", axis=alt.Axis(format=".1f")),
        y=alt.Y("density:Q", title="Density"),
        color=alt.Color("Group:N", scale=alt.Scale(
            domain=[treatment, control],
            range=["#2E7D32", "#1565C0"]
        ))
    )

    # Add mean lines
    mean_df = pd.DataFrame([
        {"Group": group, "Mean": data["mean"] * 100}
        for group, data in groups_data.items()
    ])

    mean_lines = (
        alt.Chart(mean_df)
        .mark_rule(strokeDash=[5, 5], strokeWidth=2)
        .encode(
            x="Mean:Q",
            color=alt.Color("Group:N", scale=alt.Scale(
                domain=[treatment, control],
                range=["#2E7D32", "#1565C0"]
            ))
        )
    )

    chart = (density + mean_lines).properties(
        width=700,
        height=350,
        title=f"CVR Posterior Distributions ({domain}) - P({treatment} > {control}) = {prob_treat_better:.1f}%"
    )

    return chart


def save_daily_metrics_csv(
    daily_metrics: List[Dict[str, Any]],
    output_file: Path
) -> None:
    """Save daily metrics to CSV."""
    if not daily_metrics:
        return

    with open(output_file, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=daily_metrics[0].keys())
        writer.writeheader()
        writer.writerows(daily_metrics)


def load_daily_users(
    daily_users_file: Path,
    treatment: str,
    control: str
) -> Dict[str, Dict[str, int]]:
    """
    Load daily user counts from GA4 API response JSON.

    Expected format: GA4 run_report response with dimensions [ab_test_variant, date]
    and metric [totalUsers].

    Returns: {date: {group: user_count}}
    """
    with open(daily_users_file) as f:
        data = json.load(f)

    # Handle both direct response and wrapped response
    result = data.get('result', data)
    rows = result.get('rows', [])

    daily_users: Dict[str, Dict[str, int]] = {}

    for row in rows:
        dim_values = row.get('dimension_values', [])
        metric_values = row.get('metric_values', [])

        if len(dim_values) < 2 or not metric_values:
            continue

        group = dim_values[0].get('value', '')
        date = dim_values[1].get('value', '')

        if group not in [treatment, control]:
            continue

        try:
            users = int(metric_values[0].get('value', 0))
        except (ValueError, TypeError):
            continue

        if date not in daily_users:
            daily_users[date] = {}
        daily_users[date][group] = users

    return daily_users


# Currency mapping by domain TLD
DOMAIN_CURRENCIES = {
    '.pl': 'zł',
    '.cz': 'Kč',
    '.sk': '€',
    '.hu': 'Ft',
    '.ro': 'lei',
    '.de': '€',
    '.at': '€',
    '.fr': '€',
    '.it': '€',
    '.es': '€',
    '.uk': '£',
    '.co.uk': '£',
}


def get_currency_for_domain(domain: str) -> str:
    """Get currency symbol based on domain TLD."""
    domain_lower = domain.lower()
    for tld, currency in DOMAIN_CURRENCIES.items():
        if domain_lower.endswith(tld):
            return currency
    return '€'  # Default to EUR


def format_date_display(date_str: str) -> str:
    """Format YYYY-MM-DD to 'Mon DD, YYYY'."""
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    return dt.strftime('%b %d, %Y')


def generate_html_overview(
    domain: str,
    start_date: str,
    end_date: str,
    treatment: str,
    control: str,
    metrics: Dict,
    output_file: Path,
    currency: Optional[str] = None,
    property_id: Optional[str] = None,
) -> None:
    """Generate colorful HTML overview dashboard for AB test results."""

    # Auto-detect currency if not provided
    if currency is None:
        currency = get_currency_for_domain(domain)

    treat = metrics[treatment]
    ctrl = metrics[control]
    diff = metrics['differences']
    stats = metrics['statistics']

    days = (datetime.strptime(end_date, '%Y-%m-%d') - datetime.strptime(start_date, '%Y-%m-%d')).days + 1

    # Calculate AOV×CVR
    aov_cvr_ctrl = ctrl['aov'] * (ctrl['user_cvr'] / 100)
    aov_cvr_treat = treat['aov'] * (treat['user_cvr'] / 100)
    aov_cvr_diff = ((aov_cvr_treat / aov_cvr_ctrl) - 1) * 100 if aov_cvr_ctrl > 0 else 0

    # Helper for gradient color class (10 levels)
    def color_class(value: float) -> str:
        if value >= 8.0:
            return 'pos-5'    # Darkest green
        elif value >= 5.0:
            return 'pos-4'
        elif value >= 3.0:
            return 'pos-3'
        elif value >= 1.5:
            return 'pos-2'
        elif value >= 0.5:
            return 'pos-1'    # Lightest green
        elif value >= -0.5:
            return 'neutral'  # Yellow
        elif value >= -1.5:
            return 'neg-1'    # Light orange
        elif value >= -3.0:
            return 'neg-2'
        elif value >= -5.0:
            return 'neg-3'
        elif value >= -8.0:
            return 'neg-4'
        else:
            return 'neg-5'    # Darkest red

    # Helper for p-value color.
    # Significance is directionally neutral — a non-significant p-value is not "bad",
    # it just means no detectable effect. Only significant p-values get a color
    # (green), everything else is neutral/transparent. Whether the metric change
    # itself is good or bad is conveyed by the metric cell's own color, not here.
    def pvalue_class(pvalue: float) -> str:
        if pvalue <= 0.05:
            return 'pval-green'      # Significant
        else:
            return 'pval-neutral'    # Not significant — neutral, no judgement

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AB Test Overview - {domain}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background-color: #1a2744;
            color: white;
            padding: 40px;
            min-height: 100vh;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}
        .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 40px; }}
        .logo img {{ height: 40px; }}
        .page-title {{ font-size: 24px; font-weight: 600; }}
        .domain-name {{ font-size: 48px; font-weight: 300; margin-bottom: 30px; color: #4fc3f7; }}
        .metrics-table {{
            width: 100%;
            border-collapse: separate;
            border-spacing: 4px 0;
            margin-bottom: 50px;
        }}
        .metrics-table th {{
            padding: 12px 16px;
            text-align: center;
            font-weight: 400;
            font-size: 13px;
            color: rgba(255,255,255,0.7);
            border-bottom: 1px solid rgba(255,255,255,0.1);
        }}
        .metrics-table th:first-child {{ text-align: left; width: 100px; }}
        .metrics-table td {{
            padding: 16px;
            text-align: center;
            font-size: 18px;
            font-weight: 500;
            border-bottom: 1px solid rgba(255,255,255,0.1);
        }}
        .metrics-table td:first-child {{
            text-align: left;
            font-weight: 400;
            font-size: 14px;
            color: rgba(255,255,255,0.8);
        }}
        .metrics-table tbody tr:last-child td {{ border-bottom: none; }}
        /* Difference row styling */
        .diff-row td {{
            padding: 12px 8px;
            border-bottom: none;
        }}
        .diff-row td:not(:first-child) {{
            border-radius: 6px;
        }}
        .diff-row td:first-child {{
            background: transparent !important;
        }}
        /* Add visual gaps using box-shadow inset trick */
        .diff-row td:not(:first-child):not(.no-color) {{
            box-shadow: inset 0 0 0 2px #1a2744;
        }}
        /* Positive gradients (green) */
        .pos-5 {{ background-color: #1b5e20; color: white; }}
        .pos-4 {{ background-color: #2e7d32; color: white; }}
        .pos-3 {{ background-color: #43a047; color: white; }}
        .pos-2 {{ background-color: #66bb6a; color: white; }}
        .pos-1 {{ background-color: #a5d6a7; color: #1a2744; }}
        /* Neutral */
        .neutral {{ background-color: #fdd835; color: #1a2744; }}
        /* Negative gradients (red) */
        .neg-1 {{ background-color: #ffab91; color: #1a2744; }}
        .neg-2 {{ background-color: #ff8a65; color: white; }}
        .neg-3 {{ background-color: #e64a19; color: white; }}
        .neg-4 {{ background-color: #d32f2f; color: white; }}
        .neg-5 {{ background-color: #b71c1c; color: white; }}
        .no-color {{ background-color: transparent; color: rgba(255,255,255,0.7); }}
        /* P-value colors — only significant results are colored; non-significant is neutral */
        .pval-green {{ background-color: #2e7d32; }}
        .pval-neutral {{ background-color: transparent; border: 1px solid rgba(255,255,255,0.25); }}
        .significance-section {{ text-align: center; }}
        .significance-title {{ font-size: 36px; font-weight: 300; color: #4fc3f7; margin-bottom: 30px; }}
        .significance-boxes {{ display: flex; justify-content: center; gap: 40px; align-items: flex-start; }}
        .date-boxes {{ display: flex; gap: 0; }}
        .date-box {{
            background: transparent;
            border: 1px solid rgba(255,255,255,0.3);
            padding: 15px 30px;
            text-align: center;
        }}
        .date-box-label {{ font-size: 11px; color: rgba(255,255,255,0.6); margin-bottom: 8px; }}
        .date-box-value {{ font-size: 18px; font-weight: 500; }}
        .pvalue-boxes {{ display: flex; gap: 4px; }}
        .pvalue-box {{
            padding: 15px 30px;
            text-align: center;
            min-width: 140px;
            border-radius: 4px;
        }}
        .pvalue-label {{ font-size: 11px; color: rgba(255,255,255,0.9); margin-bottom: 8px; }}
        .pvalue-value {{ font-size: 24px; font-weight: 600; }}
        .helper-text {{ font-size: 11px; color: rgba(255,255,255,0.5); margin-top: 10px; }}
        .note {{ font-size: 12px; color: rgba(255,255,255,0.4); margin-top: 40px; text-align: center; }}
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <div class="logo">
            <img src="https://www.luigisbox.com/app/uploads/2025/05/luigis-box-logo.svg" alt="Luigi's Box" height="40">
        </div>
        <div class="page-title">AB TEST: Overview</div>
    </div>

    <h1 class="domain-name">{domain}</h1>

    <table class="metrics-table">
        <thead>
            <tr>
                <th></th>
                <th>Users</th>
                <th>Sessions</th>
                <th>Transactions</th>
                <th>Revenue</th>
                <th>AOV</th>
                <th>CVR</th>
                <th>AOV×CVR</th>
            </tr>
        </thead>
        <tbody>
            <tr>
                <td>Original</td>
                <td>{ctrl['users']:,}</td>
                <td>{ctrl['sessions']:,}</td>
                <td>{ctrl['transactions']:,}</td>
                <td>{ctrl['revenue']:,.0f} {currency}</td>
                <td>{ctrl['aov']:.2f} {currency}</td>
                <td>{ctrl['user_cvr']:.2f}%</td>
                <td>{aov_cvr_ctrl:.2f}</td>
            </tr>
            <tr>
                <td>Luigi's Box</td>
                <td>{treat['users']:,}</td>
                <td>{treat['sessions']:,}</td>
                <td>{treat['transactions']:,}</td>
                <td>{treat['revenue']:,.0f} {currency}</td>
                <td>{treat['aov']:.2f} {currency}</td>
                <td>{treat['user_cvr']:.2f}%</td>
                <td>{aov_cvr_treat:.2f}</td>
            </tr>
            <tr class="diff-row">
                <td><strong>Difference</strong></td>
                <td class="no-color">{(treat['users']/ctrl['users']-1)*100:+.2f}%</td>
                <td class="no-color">{(treat['sessions']/ctrl['sessions']-1)*100:+.2f}%</td>
                <td class="{color_class(diff['transactions_pct'])}">{diff['transactions_pct']:+.2f}%</td>
                <td class="{color_class(diff['revenue_pct'])}">{diff['revenue_pct']:+.2f}%</td>
                <td class="{color_class(diff['aov_pct'])}">{diff['aov_pct']:+.2f}%</td>
                <td class="{color_class(diff['user_cvr_pct'])}">{diff['user_cvr_pct']:+.2f}%</td>
                <td class="{color_class(aov_cvr_diff)}">{aov_cvr_diff:+.2f}%</td>
            </tr>
        </tbody>
    </table>

    <div class="significance-section">
        <h2 class="significance-title">Test significance</h2>

        <div class="significance-boxes">
            <div>
                <div class="date-boxes">
                    <div class="date-box">
                        <div class="date-box-label">Date start</div>
                        <div class="date-box-value">{format_date_display(start_date)}</div>
                    </div>
                    <div class="date-box">
                        <div class="date-box-label">Date end</div>
                        <div class="date-box-value">{format_date_display(end_date)}</div>
                    </div>
                </div>
                <div class="helper-text">The full period of the test significance</div>
            </div>

            <div>
                <div class="pvalue-boxes">
                    <div class="pvalue-box {pvalue_class(stats['aov_p_value'])}">
                        <div class="pvalue-label">p-value AOV</div>
                        <div class="pvalue-value">{stats['aov_p_value']:.3f}</div>
                    </div>
                    <div class="pvalue-box {pvalue_class(stats['user_cvr_p_value'])}">
                        <div class="pvalue-label">p-value CVR</div>
                        <div class="pvalue-value">{stats['user_cvr_p_value']:.3f}</div>
                    </div>
                </div>
                <div class="helper-text">Significant if green (p &lt; 0.05)</div>
            </div>
        </div>
    </div>

    <p class="note">Data: {start_date} to {end_date} | {days} days</p>
    <p class="note">CVR is user-based (transactions / unique users). Data pulled from the client's GA4{f" property {property_id}" if property_id else ""}.</p>
</div>
</body>
</html>'''

    with open(output_file, 'w') as f:
        f.write(html)


def generate_executive_summary(
    domain: str,
    start_date: str,
    end_date: str,
    treatment: str,
    control: str,
    metrics: Dict,
    output_file: Path
) -> str:
    """
    Generate a brief executive summary interpreting the AB test results.

    Rules:
    - User split: <0.5% safe, 0.5-1% concerning, >1% invalidates test (if >10k users/group)
    - P-value: <0.05 significant, <0.15 getting close, >=0.15 not significant
    - Effect size: 1-2% modest, 3-5% nice, 6%+ good result
    """
    treat = metrics[treatment]
    ctrl = metrics[control]
    diff = metrics['differences']
    stats = metrics['statistics']

    days = (datetime.strptime(end_date, '%Y-%m-%d') - datetime.strptime(start_date, '%Y-%m-%d')).days + 1

    # Calculate user split difference
    user_diff_pct = abs((treat['users'] / ctrl['users'] - 1) * 100)
    min_users = min(treat['users'], ctrl['users'])

    # Build summary paragraphs
    paragraphs = []

    # 1. User split assessment
    if user_diff_pct < 0.5:
        split_text = f"The user split is well-balanced ({user_diff_pct:.2f}% difference), which validates the test setup."
    elif user_diff_pct < 1.0:
        split_text = f"The user split shows a {user_diff_pct:.2f}% difference, which is slightly concerning but acceptable."
    else:
        if min_users > 10000:
            split_text = f"**Warning:** The user split difference of {user_diff_pct:.2f}% is too high and potentially invalidates the AB test, as this imbalance with {min_users:,}+ users per group is unlikely to be accidental."
        else:
            split_text = f"The user split shows a {user_diff_pct:.2f}% difference. With smaller sample sizes ({min_users:,} users), this may be acceptable but should be monitored."
    paragraphs.append(split_text)

    # 2. CVR assessment
    cvr_diff = diff['user_cvr_pct']
    cvr_pval = stats['user_cvr_p_value']

    # Effect size description
    abs_cvr = abs(cvr_diff)
    if abs_cvr < 1:
        effect_desc = "negligible"
    elif abs_cvr < 2:
        effect_desc = "modest"
    elif abs_cvr < 3:
        effect_desc = "moderate"
    elif abs_cvr < 6:
        effect_desc = "nice"
    else:
        effect_desc = "strong"

    direction = "positive" if cvr_diff > 0 else "negative"

    if cvr_pval < 0.05:
        cvr_text = f"The conversion rate shows a **statistically significant** {direction} difference of {cvr_diff:+.2f}% (p={cvr_pval:.3f}). This is a {effect_desc} effect that we can be confident is real."
    elif cvr_pval < 0.15:
        days_hint = stats.get('user_cvr_days_to_significance')
        if days_hint and days_hint > 0:
            cvr_text = f"The conversion rate difference of {cvr_diff:+.2f}% is approaching significance (p={cvr_pval:.3f}). This {effect_desc} {direction} trend needs approximately {days_hint} more days to confirm."
        else:
            cvr_text = f"The conversion rate difference of {cvr_diff:+.2f}% is approaching significance (p={cvr_pval:.3f}). This {effect_desc} {direction} trend needs more data to confirm."
    else:
        cvr_text = f"The conversion rate difference of {cvr_diff:+.2f}% is not statistically significant (p={cvr_pval:.3f}). We cannot yet conclude there is a real effect."
    paragraphs.append(cvr_text)

    # 3. AOV assessment
    aov_diff = diff['aov_pct']
    aov_pval = stats['aov_p_value']

    abs_aov = abs(aov_diff)
    if abs_aov < 1:
        aov_effect = "negligible"
    elif abs_aov < 2:
        aov_effect = "modest"
    elif abs_aov < 3:
        aov_effect = "moderate"
    elif abs_aov < 6:
        aov_effect = "notable"
    else:
        aov_effect = "substantial"

    aov_direction = "increase" if aov_diff > 0 else "decrease"

    if aov_pval < 0.05:
        aov_text = f"The average order value shows a **statistically significant** {aov_direction} of {aov_diff:+.2f}% (p={aov_pval:.3f})."
    elif aov_pval < 0.15:
        aov_text = f"The AOV {aov_direction} of {aov_diff:+.2f}% is trending but not yet significant (p={aov_pval:.3f})."
    else:
        if abs_aov < 1:
            aov_text = f"The AOV difference of {aov_diff:+.2f}% is negligible and not significant (p={aov_pval:.3f}), effectively zero."
        else:
            aov_text = f"The AOV {aov_direction} of {aov_diff:+.2f}% is not statistically significant (p={aov_pval:.3f}) and should be considered inconclusive."
    paragraphs.append(aov_text)

    # 4. Overall conclusion (focus on CVR and AOV significance, not revenue)
    if stats['user_cvr_significant'] and cvr_diff > 0:
        if aov_pval >= 0.15:
            # CVR significant positive, AOV not significant = report CVR as the win
            conclusion = f"**Conclusion:** Luigi's Box delivers a confirmed **{cvr_diff:+.2f}% improvement in conversion rate** (statistically significant). AOV shows no significant change and should be considered neutral."
        elif aov_diff >= 0:
            conclusion = f"**Conclusion:** Luigi's Box delivers significant improvements in both CVR ({cvr_diff:+.2f}%) and AOV ({aov_diff:+.2f}%)."
        else:
            conclusion = f"**Conclusion:** Luigi's Box significantly improves CVR ({cvr_diff:+.2f}%) but AOV is also significantly lower ({aov_diff:+.2f}%). Evaluate trade-off."
    elif cvr_pval < 0.15 and cvr_diff > 0:
        if aov_pval >= 0.15:
            conclusion = f"**Conclusion:** CVR shows a promising **{cvr_diff:+.2f}% improvement** approaching significance (p={cvr_pval:.3f}). AOV difference is not significant and should be considered neutral. Continue testing to confirm CVR effect."
        else:
            conclusion = f"**Conclusion:** CVR trending positive ({cvr_diff:+.2f}%, p={cvr_pval:.3f}) but needs more data. AOV also showing a trend ({aov_diff:+.2f}%, p={aov_pval:.3f})."
    elif cvr_diff > 0:
        conclusion = f"**Conclusion:** Early positive CVR trend ({cvr_diff:+.2f}%) but insufficient data after {days} days (p={cvr_pval:.3f}). Continue monitoring."
    else:
        conclusion = f"**Conclusion:** No clear CVR benefit detected after {days} days ({cvr_diff:+.2f}%, p={cvr_pval:.3f}). Consider extending the test or reviewing the implementation."
    paragraphs.append(conclusion)

    # 5. Optional: Client push note if uplift >= 1.5%
    if cvr_diff >= 1.5:
        client_note = f"*Note: With a {cvr_diff:+.2f}% CVR uplift, we can consider presenting these results to the client to close the test if they accept the current improvement.*"
        paragraphs.append(client_note)

    # Compose the summary
    summary = f"""# Executive Summary: {domain}

**Test Period:** {start_date} to {end_date} ({days} days)

{chr(10).join(paragraphs)}
"""

    with open(output_file, 'w') as f:
        f.write(summary)

    return summary


def main():
    parser = argparse.ArgumentParser(
        description='Run full GA4 AB test evaluation with hardcoded methodology',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument('--domain', required=True, help='Domain name (e.g., grizly.cz)')
    parser.add_argument('--property-id', required=True, help='GA4 property ID')
    parser.add_argument('--start-date', required=True, help='Test start date (YYYY-MM-DD)')
    parser.add_argument('--end-date', required=True, help='Test end date (YYYY-MM-DD)')
    parser.add_argument('--treatment', required=True, help='Treatment group name')
    parser.add_argument('--control', required=True, help='Control group name')
    parser.add_argument('--dimension', default='customUser:ab_test', help='AB test dimension')
    parser.add_argument('--users-treatment', type=int, required=True, help='Unique users in treatment group')
    parser.add_argument('--users-control', type=int, required=True, help='Unique users in control group')
    parser.add_argument('--sessions-treatment', type=int, default=0, help='Sessions in treatment group (optional, for Session CVR)')
    parser.add_argument('--sessions-control', type=int, default=0, help='Sessions in control group (optional, for Session CVR)')
    parser.add_argument('--output-dir', required=True, help='Output directory for results (date-specific folder)')
    parser.add_argument('--config-dir', help='Directory for config.yaml (default: parent of output-dir, i.e., domain folder)')
    parser.add_argument('--raw-file', help='Path to transactions_raw.json (default: {output-dir}/transactions_raw.json)')
    parser.add_argument('--daily-users-file', help='Path to daily_users.json with daily user counts per group (required for accurate daily CVR charts)')
    parser.add_argument('--currency', help='Currency symbol (e.g., zł, Kč, €). Auto-detected from domain if not provided.')

    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Config goes to parent directory (domain folder) by default
    config_dir = Path(args.config_dir) if args.config_dir else output_dir.parent
    config_dir.mkdir(parents=True, exist_ok=True)

    raw_file = Path(args.raw_file) if args.raw_file else output_dir / 'transactions_raw.json'

    # Check raw file exists
    if not raw_file.exists():
        print("=" * 70)
        print("⛔ ERROR: transactions_raw.json not found!")
        print("=" * 70)
        print(f"Expected at: {raw_file}")
        print()
        print("The LLM must first run this GA4 query and save the response:")
        print()
        print(f"  mcp__analytics-mcp__run_report:")
        print(f"    property_id: {args.property_id}")
        print(f"    date_ranges: [{{\"start_date\": \"{args.start_date}\", \"end_date\": \"{args.end_date}\"}}]")
        print(f"    dimensions: [\"{args.dimension}\", \"transactionId\", \"date\"]")
        print(f"    metrics: [\"purchaseRevenue\"]")
        print(f"    dimension_filter:")
        print(f"      filter:")
        print(f"        field_name: \"{args.dimension}\"")
        print(f"        in_list_filter:")
        print(f"          values: [\"{args.treatment}\", \"{args.control}\"]")
        print(f"          case_sensitive: true")
        print(f"    limit: 100000")
        print()
        print(f"Save the FULL response to: {raw_file}")
        print("=" * 70)
        sys.exit(1)

    print("=" * 70)
    print("GA4 AB TEST EVALUATION")
    print("=" * 70)
    print(f"Domain: {args.domain}")
    print(f"Period: {args.start_date} to {args.end_date}")
    print(f"Treatment: {args.treatment} ({args.users_treatment:,} users, {args.sessions_treatment:,} sessions)")
    print(f"Control: {args.control} ({args.users_control:,} users, {args.sessions_control:,} sessions)")
    print()

    # Step 1: Load and validate raw data
    print("Step 1: Loading and validating raw data...")
    raw_transactions, total_rows = load_and_validate_raw_data(
        raw_file, args.treatment, args.control
    )
    print(f"  ✅ Loaded {total_rows:,} rows")
    print(f"  ✅ {args.treatment}: {len(raw_transactions[args.treatment]):,} transactions")
    print(f"  ✅ {args.control}: {len(raw_transactions[args.control]):,} transactions")
    print()

    # Step 2: Apply outlier removal
    print("Step 2: Applying outlier removal (99th percentile)...")
    clean_transactions, threshold, outliers_removed = apply_outlier_removal(raw_transactions)

    # Calculate raw totals for display
    raw_treat_count = len(raw_transactions[args.treatment])
    raw_ctrl_count = len(raw_transactions[args.control])
    raw_treat_revenue = sum(r["revenue"] for r in raw_transactions[args.treatment])
    raw_ctrl_revenue = sum(r["revenue"] for r in raw_transactions[args.control])

    # Calculate clean totals for display
    clean_treat_count = len(clean_transactions[args.treatment])
    clean_ctrl_count = len(clean_transactions[args.control])
    clean_treat_revenue = sum(r["revenue"] for r in clean_transactions[args.treatment])
    clean_ctrl_revenue = sum(r["revenue"] for r in clean_transactions[args.control])

    print(f"  ✅ Threshold: {threshold:,.2f}")
    print(f"  ✅ {args.treatment}: {outliers_removed[args.treatment]} outliers removed ({raw_treat_count} → {clean_treat_count} transactions)")
    print(f"  ✅ {args.control}: {outliers_removed[args.control]} outliers removed ({raw_ctrl_count} → {clean_ctrl_count} transactions)")
    print()
    print("  Clean data summary:")
    print(f"    {args.treatment}: {clean_treat_count:,} transactions, {clean_treat_revenue:,.2f} revenue")
    print(f"    {args.control}: {clean_ctrl_count:,} transactions, {clean_ctrl_revenue:,.2f} revenue")
    print()

    # Step 3: Calculate metrics
    print("Step 3: Calculating metrics...")
    users = {args.treatment: args.users_treatment, args.control: args.users_control}
    sessions = {args.treatment: args.sessions_treatment, args.control: args.sessions_control}
    days_elapsed = (datetime.strptime(args.end_date, '%Y-%m-%d') - datetime.strptime(args.start_date, '%Y-%m-%d')).days + 1
    metrics = calculate_metrics(clean_transactions, users, args.treatment, args.control, days_elapsed, sessions)

    # Format days to significance for display
    def format_days(days: Optional[int]) -> str:
        if days is None:
            return "N/A"
        elif days == 0:
            return "0 (significant)"
        elif days > 365:
            return ">365"
        else:
            return str(days)

    user_cvr_days = format_days(metrics['statistics']['user_cvr_days_to_significance'])
    session_cvr_days = format_days(metrics['statistics']['session_cvr_days_to_significance'])
    aov_days = format_days(metrics['statistics']['aov_days_to_significance'])

    print(f"  ✅ User CVR difference: {metrics['differences']['user_cvr_pct']:+.2f}%")
    print(f"  ✅ User CVR P-value: {metrics['statistics']['user_cvr_p_value']:.4f}")
    print(f"  ✅ User CVR Significant: {metrics['statistics']['user_cvr_significant']}")
    print(f"  ✅ User CVR Days to significance: {user_cvr_days}")
    if args.sessions_treatment > 0 and args.sessions_control > 0:
        print(f"  ✅ Session CVR difference: {metrics['differences']['session_cvr_pct']:+.2f}%")
        print(f"  ✅ Session CVR P-value: {metrics['statistics']['session_cvr_p_value']:.4f}")
        print(f"  ✅ Session CVR Significant: {metrics['statistics']['session_cvr_significant']}")
        print(f"  ✅ Session CVR Days to significance: {session_cvr_days}")
    print(f"  ✅ AOV difference: {metrics['differences']['aov_pct']:+.2f}%")
    print(f"  ✅ AOV P-value: {metrics['statistics']['aov_p_value']:.4f}")
    print(f"  ✅ AOV Significant: {metrics['statistics']['aov_significant']}")
    print(f"  ✅ AOV Days to significance: {aov_days}")
    print()

    # Step 4: Save outputs
    print("Step 4: Saving outputs...")

    # Write api_calls.log for reproducibility
    write_api_calls_log(
        output_dir,
        args.property_id,
        args.start_date,
        args.end_date,
        args.dimension,
        args.treatment,
        args.control,
        users,
        sessions,
        total_rows
    )
    print(f"  ✅ Saved: {output_dir / 'api_calls.log'}")

    # Save clean CSV
    csv_file = output_dir / 'transactions_clean.csv'
    save_clean_csv(clean_transactions, csv_file, [args.treatment, args.control])
    print(f"  ✅ Saved: {csv_file}")

    # Step 5: Generate charts
    print()
    print("Step 5: Generating charts...")

    # Load daily users if provided
    daily_users = None
    if args.daily_users_file:
        daily_users_file = Path(args.daily_users_file)
        if daily_users_file.exists():
            daily_users = load_daily_users(daily_users_file, args.treatment, args.control)
            print(f"  ✅ Loaded daily user data from: {daily_users_file}")
            print(f"     Days with data: {len(daily_users)}")
        else:
            print(f"  ⚠️ Daily users file not found: {daily_users_file}")
    else:
        print("  ⚠️ No --daily-users-file provided. Daily CVR charts will show transaction counts instead.")

    daily_metrics = calculate_daily_metrics(
        clean_transactions, users,
        args.treatment, args.control,
        args.start_date, args.end_date,
        daily_users
    )

    # Save daily metrics CSV
    if daily_metrics:
        daily_csv = output_dir / 'daily_metrics.csv'
        save_daily_metrics_csv(daily_metrics, daily_csv)
        print(f"  ✅ Saved: {daily_csv}")

        # Calculate daily diffs and generate charts
        daily_diffs, has_cvr_data = calculate_daily_diffs(daily_metrics, args.treatment, args.control)
        tx_counts = {
            args.treatment: metrics[args.treatment]["transactions"],
            args.control: metrics[args.control]["transactions"]
        }
        generated_charts = generate_charts(
            daily_diffs, args.treatment, args.control,
            users, tx_counts, args.domain, output_dir, has_cvr_data
        )
    else:
        print("  ⚠️ No daily data available (date dimension may be missing from raw data)")
        generated_charts = {}

    print()
    # Save config to parent directory (domain folder, shared across evaluations)
    save_config_yaml(
        config_dir,
        args.domain,
        args.property_id,
        args.dimension,
        args.treatment,
        args.control,
        args.start_date,
        args.end_date,
        raw_transactions,
        clean_transactions,
        threshold,
        outliers_removed,
        metrics
    )
    print(f"  ✅ Saved: {config_dir / 'config.yaml'}")

    # Generate and save report
    report = generate_markdown_report(
        args.domain, args.property_id, args.start_date, args.end_date,
        args.dimension, args.treatment, args.control,
        raw_transactions, clean_transactions, threshold, outliers_removed,
        metrics, total_rows, generated_charts
    )

    # Report goes to date-specific folder as report.md
    report_file = output_dir / "report.md"
    with open(report_file, 'w') as f:
        f.write(report)
    print(f"  ✅ Saved: {report_file}")

    # Write deterministic completion marker (used by QA checks)
    files_dict = {
        "transactions_raw.json": str(raw_file),
        "transactions_clean.csv": str(csv_file),
        "api_calls.log": str(output_dir / "api_calls.log"),
        "config.yaml": str(config_dir / "config.yaml"),
        "report": str(report_file),
    }
    # Add chart files if generated
    for chart_name, chart_path in generated_charts.items():
        files_dict[f"charts/{chart_name}.png"] = str(chart_path)

    marker_path = write_completion_marker(output_dir, {
        "domain": args.domain,
        "property_id": args.property_id,
        "ab_test_dimension": args.dimension,
        "date_range": f"{args.start_date} to {args.end_date}",
        "treatment": args.treatment,
        "control": args.control,
        "users": users,
        "sessions": sessions,
        "raw_row_count": total_rows,
        "clean_transactions": {
            args.treatment: metrics[args.treatment]["transactions"],
            args.control: metrics[args.control]["transactions"],
        },
        "charts_generated": list(generated_charts.keys()),
        "files": files_dict,
    })
    print(f"  ✅ Saved: {marker_path}")

    # Step 6: Generate HTML overview dashboard and executive summary
    print()
    print("Step 6: Generating HTML overview and executive summary...")
    html_file = output_dir / "ab_test_overview.html"
    generate_html_overview(
        args.domain,
        args.start_date,
        args.end_date,
        args.treatment,
        args.control,
        metrics,
        html_file,
        args.currency,
        property_id=args.property_id,
    )
    print(f"  ✅ Saved: {html_file}")

    summary_file = output_dir / "executive-summary.md"
    generate_executive_summary(
        args.domain,
        args.start_date,
        args.end_date,
        args.treatment,
        args.control,
        metrics,
        summary_file
    )
    print(f"  ✅ Saved: {summary_file}")

    print()
    print("=" * 70)
    print("EVALUATION COMPLETE")
    print("=" * 70)
    print()
    print("User CVR Analysis (PRIMARY):")
    print(f"  {args.treatment}: {metrics[args.treatment]['user_cvr']:.2f}% vs {args.control}: {metrics[args.control]['user_cvr']:.2f}%")
    print(f"  Difference: {metrics['differences']['user_cvr_pct']:+.2f}%")
    print(f"  P-value: {metrics['statistics']['user_cvr_p_value']:.4f}")
    print(f"  Result: {'SIGNIFICANT' if metrics['statistics']['user_cvr_significant'] else 'NOT SIGNIFICANT'}")
    print()
    if args.sessions_treatment > 0 and args.sessions_control > 0:
        print("Session CVR Analysis (SECONDARY):")
        print(f"  {args.treatment}: {metrics[args.treatment]['session_cvr']:.2f}% vs {args.control}: {metrics[args.control]['session_cvr']:.2f}%")
        print(f"  Difference: {metrics['differences']['session_cvr_pct']:+.2f}%")
        print(f"  P-value: {metrics['statistics']['session_cvr_p_value']:.4f}")
        print(f"  Result: {'SIGNIFICANT' if metrics['statistics']['session_cvr_significant'] else 'NOT SIGNIFICANT'}")
        print()
    print("AOV Analysis:")
    print(f"  {args.treatment}: {metrics[args.treatment]['aov']:,.2f} vs {args.control}: {metrics[args.control]['aov']:,.2f}")
    print(f"  Difference: {metrics['differences']['aov_pct']:+.2f}%")
    print(f"  P-value: {metrics['statistics']['aov_p_value']:.4f}")
    print(f"  Result: {'SIGNIFICANT' if metrics['statistics']['aov_significant'] else 'NOT SIGNIFICANT'}")


if __name__ == "__main__":
    main()
