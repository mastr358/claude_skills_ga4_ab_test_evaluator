#!/usr/bin/env python3
"""
4home.cz AB Test Evaluation with PostMAMA Support

This script extends the base evaluation with PostMAMA (margin) metric tracking
which is specifically requested by the 4home.cz client.

USAGE:
    The LLM must:
    1. Query GA4 API with BOTH purchaseRevenue AND customEvent:post_mama metrics
    2. Save response to transactions_raw.json
    3. Run this script

    python run_4home_evaluation.py \
        --start-date 2026-01-23 \
        --end-date 2026-01-25 \
        --users-treatment 23927 \
        --users-control 23872 \
        --sessions-treatment 32312 \
        --sessions-control 30575 \
        --output-dir /path/to/ga4-investigations/4home.cz/YYYY-MM-DD_to_YYYY-MM-DD/

REQUIRED GA4 QUERY (LLM must run this EXACTLY):
    mcp__analytics-mcp__run_report:
      property_id: 282336364
      date_ranges: [{"start_date": "{start_date}", "end_date": "{end_date}"}]
      dimensions: ["customUser:user_experiment_group", "transactionId", "date"]
      metrics: ["purchaseRevenue", "customEvent:post_mama"]  # BOTH METRICS!
      dimension_filter:
        filter:
          field_name: "customUser:user_experiment_group"
          in_list_filter:
            values: ["1", "0"]
            case_sensitive: true
      limit: 100000
"""

import argparse
import json
import math
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 4home.cz fixed configuration
DOMAIN = "4home.cz"
PROPERTY_ID = "282336364"
AB_TEST_DIMENSION = "customUser:user_experiment_group"
TREATMENT = "1"  # LuigisBox
CONTROL = "0"    # Original
CURRENCY = "Kc"

SIGNIFICANCE_LEVEL = 0.05


def norm_cdf(x: float) -> float:
    """Standard normal CDF using error function approximation."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def two_sample_mean_ztest(values1: List[float], values2: List[float]) -> Tuple[float, float]:
    """Two-sample Z-test for comparing means."""
    n1, n2 = len(values1), len(values2)
    if n1 < 2 or n2 < 2:
        return 0.0, 1.0

    mean1 = sum(values1) / n1
    mean2 = sum(values2) / n2
    var1 = sum((x - mean1) ** 2 for x in values1) / (n1 - 1)
    var2 = sum((x - mean2) ** 2 for x in values2) / (n2 - 1)
    se = math.sqrt(var1 / n1 + var2 / n2)

    if se == 0:
        return 0.0, 1.0

    z = (mean1 - mean2) / se
    p_value = 2 * (1 - norm_cdf(abs(z)))
    return z, p_value


def load_postmama_data(raw_file: Path) -> Dict[str, List[float]]:
    """
    Load PostMAMA values from raw GA4 response.

    Expects metrics: [purchaseRevenue, customEvent:post_mama]
    Returns: {group: [postmama_values]}
    """
    with open(raw_file, 'r') as f:
        data = json.load(f)

    rows = data.get('result', data).get('rows', [])
    postmama_by_group: Dict[str, List[float]] = {TREATMENT: [], CONTROL: []}

    for row in rows:
        dim_values = row.get('dimension_values', [])
        metric_values = row.get('metric_values', [])

        if len(dim_values) < 2 or len(metric_values) < 2:
            continue

        group = dim_values[0].get('value', '')
        if group not in [TREATMENT, CONTROL]:
            continue

        try:
            postmama = float(metric_values[1].get('value', 0))
        except (ValueError, TypeError):
            postmama = 0.0

        postmama_by_group[group].append(postmama)

    return postmama_by_group


def calculate_postmama_metrics(
    postmama_data: Dict[str, List[float]],
    users: Dict[str, int]
) -> Dict[str, Any]:
    """Calculate PostMAMA-specific metrics."""
    treat_values = postmama_data[TREATMENT]
    ctrl_values = postmama_data[CONTROL]

    treat_total = sum(treat_values)
    ctrl_total = sum(ctrl_values)

    treat_per_user = treat_total / users[TREATMENT] if users[TREATMENT] > 0 else 0
    ctrl_per_user = ctrl_total / users[CONTROL] if users[CONTROL] > 0 else 0

    # Percentage differences
    total_pct = ((treat_total / ctrl_total) - 1) * 100 if ctrl_total > 0 else 0
    per_user_pct = ((treat_per_user / ctrl_per_user) - 1) * 100 if ctrl_per_user > 0 else 0

    # Statistical test
    z_stat, p_value = two_sample_mean_ztest(treat_values, ctrl_values)
    significant = p_value < SIGNIFICANCE_LEVEL

    return {
        TREATMENT: {
            'total': treat_total,
            'per_user': treat_per_user,
            'count': len(treat_values)
        },
        CONTROL: {
            'total': ctrl_total,
            'per_user': ctrl_per_user,
            'count': len(ctrl_values)
        },
        'differences': {
            'total_pct': total_pct,
            'per_user_pct': per_user_pct
        },
        'statistics': {
            'z_statistic': z_stat,
            'p_value': p_value,
            'significant': significant
        }
    }


def append_postmama_to_report(report_file: Path, postmama_metrics: Dict[str, Any]) -> None:
    """Append PostMAMA section to the existing report."""
    treat = postmama_metrics[TREATMENT]
    ctrl = postmama_metrics[CONTROL]
    diff = postmama_metrics['differences']
    stats = postmama_metrics['statistics']

    section = f"""

---

## PostMAMA Analysis (4home.cz Specific)

PostMAMA represents the margin/profit metric that the client tracks alongside revenue.

### PostMAMA Summary

| Metric | LuigisBox (1) | Control (0) | Difference | p-value | Status |
|--------|---------------|-------------|------------|---------|--------|
| **Total PostMAMA** | {treat['total']:,.2f} | {ctrl['total']:,.2f} | {diff['total_pct']:+.2f}% | {stats['p_value']:.4f} | {"✅" if stats['significant'] and diff['total_pct'] > 0 else "⚠️" if not stats['significant'] else "❌"} |
| PostMAMA/User | {treat['per_user']:,.2f} | {ctrl['per_user']:,.2f} | {diff['per_user_pct']:+.2f}% | - | - |

### Statistical Test

- **Test:** Two-sample Z-test for means
- **Z-statistic:** {stats['z_statistic']:.4f}
- **P-value:** {stats['p_value']:.4f}
- **Result:** {"SIGNIFICANT" if stats['significant'] else "NOT SIGNIFICANT"} at α=0.05

### Interpretation

{"PostMAMA shows a statistically significant improvement." if stats['significant'] and diff['total_pct'] > 0 else "PostMAMA shows a statistically significant decline." if stats['significant'] else "PostMAMA difference is not statistically significant. More data needed."}
"""

    with open(report_file, 'a') as f:
        f.write(section)


def append_postmama_to_executive_summary(summary_file: Path, postmama_metrics: Dict[str, Any]) -> None:
    """Append PostMAMA line to executive summary."""
    diff = postmama_metrics['differences']
    stats = postmama_metrics['statistics']

    line = f"\nPostMAMA (margin) difference of {diff['total_pct']:+.2f}% is {'statistically significant' if stats['significant'] else 'not statistically significant'} (p={stats['p_value']:.3f})."

    with open(summary_file, 'a') as f:
        f.write(line)


def update_run_complete(run_file: Path, postmama_metrics: Dict[str, Any]) -> None:
    """Add PostMAMA data to run_complete.json."""
    with open(run_file, 'r') as f:
        data = json.load(f)

    data['postmama'] = {
        TREATMENT: postmama_metrics[TREATMENT],
        CONTROL: postmama_metrics[CONTROL],
        'differences': postmama_metrics['differences'],
        'statistics': postmama_metrics['statistics']
    }

    with open(run_file, 'w') as f:
        json.dump(data, f, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description='Run 4home.cz AB test evaluation with PostMAMA support',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument('--start-date', required=True, help='Test start date (YYYY-MM-DD)')
    parser.add_argument('--end-date', required=True, help='Test end date (YYYY-MM-DD)')
    parser.add_argument('--users-treatment', type=int, required=True, help='Unique users in treatment group')
    parser.add_argument('--users-control', type=int, required=True, help='Unique users in control group')
    parser.add_argument('--sessions-treatment', type=int, default=0, help='Sessions in treatment group')
    parser.add_argument('--sessions-control', type=int, default=0, help='Sessions in control group')
    parser.add_argument('--output-dir', required=True, help='Output directory for results')
    parser.add_argument('--skip-base', action='store_true', help='Skip running base evaluation (for re-running PostMAMA only)')

    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    raw_file = output_dir / 'transactions_raw.json'

    # Check raw file exists
    if not raw_file.exists():
        print("=" * 70)
        print("⛔ ERROR: transactions_raw.json not found!")
        print("=" * 70)
        print(f"Expected at: {raw_file}")
        print()
        print("Run this GA4 query with BOTH metrics:")
        print()
        print("  mcp__analytics-mcp__run_report:")
        print(f"    property_id: {PROPERTY_ID}")
        print(f"    date_ranges: [{{\"start_date\": \"{args.start_date}\", \"end_date\": \"{args.end_date}\"}}]")
        print(f"    dimensions: [\"{AB_TEST_DIMENSION}\", \"transactionId\", \"date\"]")
        print("    metrics: [\"purchaseRevenue\", \"customEvent:post_mama\"]")
        print("    dimension_filter:")
        print(f"      filter:")
        print(f"        field_name: \"{AB_TEST_DIMENSION}\"")
        print("        in_list_filter:")
        print(f"          values: [\"{TREATMENT}\", \"{CONTROL}\"]")
        print("          case_sensitive: true")
        print("    limit: 100000")
        print("=" * 70)
        sys.exit(1)

    print("=" * 70)
    print("4HOME.CZ AB TEST EVALUATION (with PostMAMA)")
    print("=" * 70)
    print(f"Period: {args.start_date} to {args.end_date}")
    print(f"Treatment (LuigisBox): {args.users_treatment:,} users")
    print(f"Control (Original): {args.users_control:,} users")
    print()

    # Step 1: Run base evaluation
    if not args.skip_base:
        print("Step 1: Running base evaluation...")
        base_script = Path(__file__).parent / 'run_full_evaluation.py'

        cmd = [
            sys.executable, str(base_script),
            '--domain', DOMAIN,
            '--property-id', PROPERTY_ID,
            '--start-date', args.start_date,
            '--end-date', args.end_date,
            '--treatment', TREATMENT,
            '--control', CONTROL,
            '--dimension', AB_TEST_DIMENSION,
            '--users-treatment', str(args.users_treatment),
            '--users-control', str(args.users_control),
            '--sessions-treatment', str(args.sessions_treatment),
            '--sessions-control', str(args.sessions_control),
            '--output-dir', str(output_dir),
            '--currency', CURRENCY
        ]

        result = subprocess.run(cmd, capture_output=False)
        if result.returncode != 0:
            print("⛔ Base evaluation failed!")
            sys.exit(1)
        print()
    else:
        print("Step 1: Skipping base evaluation (--skip-base)")
        print()

    # Step 2: Calculate PostMAMA metrics
    print("Step 2: Calculating PostMAMA metrics...")
    postmama_data = load_postmama_data(raw_file)

    users = {TREATMENT: args.users_treatment, CONTROL: args.users_control}
    postmama_metrics = calculate_postmama_metrics(postmama_data, users)

    treat = postmama_metrics[TREATMENT]
    ctrl = postmama_metrics[CONTROL]
    diff = postmama_metrics['differences']
    stats = postmama_metrics['statistics']

    print(f"  ✅ Treatment PostMAMA: {treat['total']:,.2f} ({treat['count']} transactions)")
    print(f"  ✅ Control PostMAMA: {ctrl['total']:,.2f} ({ctrl['count']} transactions)")
    print(f"  ✅ Total difference: {diff['total_pct']:+.2f}%")
    print(f"  ✅ Per-user difference: {diff['per_user_pct']:+.2f}%")
    print(f"  ✅ P-value: {stats['p_value']:.4f}")
    print(f"  ✅ Significant: {stats['significant']}")
    print()

    # Step 3: Append PostMAMA to reports
    print("Step 3: Appending PostMAMA to reports...")

    report_file = output_dir / 'report.md'
    if report_file.exists():
        append_postmama_to_report(report_file, postmama_metrics)
        print(f"  ✅ Updated: {report_file}")

    summary_file = output_dir / 'executive-summary.md'
    if summary_file.exists():
        append_postmama_to_executive_summary(summary_file, postmama_metrics)
        print(f"  ✅ Updated: {summary_file}")

    run_file = output_dir / 'run_complete.json'
    if run_file.exists():
        update_run_complete(run_file, postmama_metrics)
        print(f"  ✅ Updated: {run_file}")

    print()
    print("=" * 70)
    print("EVALUATION COMPLETE (with PostMAMA)")
    print("=" * 70)
    print()
    print("PostMAMA Analysis:")
    print(f"  LuigisBox: {treat['total']:,.2f} vs Control: {ctrl['total']:,.2f}")
    print(f"  Total Difference: {diff['total_pct']:+.2f}%")
    print(f"  Per-User: {treat['per_user']:,.2f} vs {ctrl['per_user']:,.2f} ({diff['per_user_pct']:+.2f}%)")
    print(f"  P-value: {stats['p_value']:.4f}")
    print(f"  Result: {'SIGNIFICANT' if stats['significant'] else 'NOT SIGNIFICANT'}")


if __name__ == "__main__":
    main()
