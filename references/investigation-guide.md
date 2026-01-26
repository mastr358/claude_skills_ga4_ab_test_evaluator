# Investigation Guide

Detailed procedures for investigating AB test results, especially when LuigisBox underperforms.

## Table of Contents

1. [Expected Event Tracking Differences](#expected-event-tracking-differences)
2. [Device/Browser Breakdown](#devicebrowser-breakdown)
3. [Timeline Analysis](#timeline-analysis)
4. [Event Funnel Analysis](#event-funnel-analysis)
5. [Root Cause Investigation](#root-cause-investigation)

## Expected Event Tracking Differences

**Zero or near-zero events in LuigisBox group may be EXPECTED, not a bug.**

LuigisBox injects its own frontend components which may not trigger client's existing event tracking.

### Expected (don't flag as bugs):
- `search` events at 0 in LB group (injected search component)
- `view_search_results` missing (different rendering)
- UI interaction events missing (different component architecture)

### Flag as issues:
- Purchase/transaction events drastically different
- Add-to-cart events drastically different (unless cart UI replaced)
- Page view events drastically different

### Magnitude matters:
- **50x+ difference:** Likely expected (component injection)
- **3x or within same order of magnitude:** Investigate further

## Device/Browser Breakdown

Check if issue is device or browser-specific.

**Query:**
```
dimensions: [ab_test_dimension, "deviceCategory", "date"]
metrics: ["users", "purchaseRevenue"]
```

**Look for:**
- Mobile broken but desktop fine?
- Specific browser issues?

**Hypothesis examples:**
- "Mobile UI broken"
- "Safari-specific JavaScript error"

## Timeline Analysis

Check when performance diverged.

**Query:**
```
dimensions: [ab_test_dimension, "date"]
metrics: ["users", "purchaseRevenue", "sessions"]
```

**Look for:**
- Did LB start strong then degrade?
- Bad from start?
- Specific dates when things changed?

**Hypothesis examples:**
- "Implementation broke on Dec 15"
- "Performance degraded over time"

## Event Funnel Analysis

Check where users drop off.

**Compare between groups:**
- search → view_item → add_to_cart → purchase

**Look for:**
- Where does LB group lose users vs Original?

**Hypothesis examples:**
- "Users can't find products (low view_item)"
- "Cart doesn't work (low add_to_cart)"

## Root Cause Investigation

When LuigisBox underperforms, form hypotheses about WHY.

### Mandatory Steps:

1. **Form Initial Hypotheses:**
   - Technical issues (broken integration, JS errors, slow load)
   - UX problems (poor relevance, confusing interface)
   - Configuration issues (wrong settings, bad ranking)
   - Segment-specific issues (mobile broken, browser bugs)

2. **Device/Browser Breakdown** - see above

3. **Timeline Analysis** - see above

4. **Event Funnel Analysis** - see above

5. **User Behavior Patterns:**
   - Sessions per user
   - Pages per session
   - Session duration

6. **Segment-Specific:**
   - New vs returning users
   - Different traffic sources

### Output Format for Worse Performance:

```markdown
## Performance Analysis: LuigisBox Underperforming

### Summary
- Revenue: -X% (LB worse)
- CVR: -Y% (LB worse)

### Root Cause Investigation

#### Hypothesis 1: [e.g., Mobile UI Broken]
**Evidence:**
- Desktop: LB revenue +5% (good)
- Mobile: LB revenue -25% (BROKEN)
**Data source:** dimensions, metrics, dates
**Conclusion:** Issue is mobile-specific

#### Hypothesis 2: [e.g., Implementation Failed Mid-Test]
**Evidence:**
- Dec 1-10: LB +8% (working)
- Dec 11-31: LB -15% (broken)
**Data source:** dimensions, metrics, dates
**Conclusion:** Something broke around Dec 10-11
```

### IMPORTANT: NO RECOMMENDATIONS

- Do NOT include "## Recommendations", "## Next Steps", "## Action Items"
- Do NOT suggest fixes
- Your role is PASSIVE EVALUATION only
- Explain what happened and WHY, not what to do about it
