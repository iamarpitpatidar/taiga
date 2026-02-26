---
name: qa-summary
description: Aggregates QA statistics across processed instances
---

Read entire instances_output.csv (score column: `score_mean`)

Compute:
- Total processed
- Total accepted
- Total rejected
- Acceptance rate (%)
- Average score of accepted
- Average score of rejected

Return structured dashboard:

{
  total_processed: number,
  accepted: number,
  rejected: number,
  acceptance_rate: percentage,
  avg_score_accepted: number,
  avg_score_rejected: number
}
