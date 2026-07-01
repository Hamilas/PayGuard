# Cost Report: AI-Powered Fraud Detection MLOps Pipeline
## 10-Day Build (March 2026)

---

## Design Decisions That Kept Costs Low

| Decision | Impact |
|---|---|
| Spot instance instead of on-demand | ~75% cheaper EC2 |
| Ephemeral/persistent Terraform split | $0/hr overnight |
| K3s instead of EKS | No $0.10/hr control plane fee |
| Single t3.2xlarge vs node group | No over-provisioning |
| Phi3/Ollama for triage | $0 per triage call (local LLM) |
| Redis fast-path for repeat alerts | $0 for cached resolutions |
| ChromaDB semantic cache | Avoids LLM calls on similar incidents |
| db.t3.micro RDS | $0.50/day vs $3–5/day for larger |

---

## AWS Infrastructure Costs

### Persistent Resources (Always Running)

| Resource | Spec | Daily Cost | 10-Day Total |
|---|---|---|---|
| RDS PostgreSQL | db.t3.micro, 20GB gp3 | ~$0.50 | ~$5.00 |
| S3 (5 buckets) | ~500MB total | ~$0.05 | ~$0.50 |
| ECR (3 repos) | ~200MB images | ~$0.02 | ~$0.20 |
| SSM Parameter Store | ~15 SecureString params | ~$0.03 | ~$0.30 |
| CloudWatch Logs | Agent + K3s logs | ~$0.05 | ~$0.50 |
| **Subtotal** | | **~$0.65/day** | **~$6.50** |

### Ephemeral Resources (~8–10 hrs/day)

| Resource | Spec | Hourly | Daily | 10-Day |
|---|---|---|---|---|
| EC2 t3.2xlarge Spot | 8 vCPU, 32GB | ~$0.10 | ~$1.00 | ~$10.00 |
| EBS gp3 50GB | Attached to Spot | — | ~$0.07 | ~$0.70 |
| Public IP | Dynamic | $0.005/hr | ~$0.05 | ~$0.50 |
| Data Transfer | S3 sync egress | — | ~$0.10 | ~$1.00 |
| **Subtotal** | | | **~$1.22/day** | **~$12.20** |

### AWS Total: ~$18.70

---

## LLM API Costs

### Per-Call Reference

| LLM | Model | Est. Cost/Call | Used For |
|---|---|---|---|
| Anthropic Claude | claude-3-5-sonnet | ~$0.02–$0.03 | investigator, synthesizer |
| Google Gemini | gemini-1.5-pro | ~$0.005–$0.01 | data_scientist node |
| Perplexity | sonar-pro | ~$0.005–$0.01 | researcher node |
| Phi3 / Ollama | phi3:mini (local) | **$0.00** | triage node |
| Redis fast-path | cache hit | **$0.00** | repeat alert dedup |

### LLM Spend by Day

| Day | Activity | LLM Cost |
|---|---|---|
| Days 1–4 | Platform build, no LLMs | $0.00 |
| Day 5 | First agent experiments, API tests | ~$0.50 |
| Day 6 | Agent scaffolding | ~$0.30 |
| Day 7 | LangGraph node testing, parallel trials | ~$1.50 |
| Day 8 | End-to-end demo runs (5–8 investigations) | ~$2.00 |
| Day 9 | Redis demo, attack mode, conflict detection | ~$1.50 |
| Day 10 | Minimal (documentation) | ~$0.10 |
| **Total** | | **~$5.90** |

---

## 10-Day Grand Total

| Category | Total |
|---|---|
| AWS Infrastructure | ~$18.70 |
| LLM APIs | ~$5.90 |
| **Grand Total** | **~$24.60** |

---

## Monthly Equivalent (For Reference Only)

| Scenario | Monthly Cost |
|---|---|
| Dev hours only (8 hrs/day, Mon–Fri) | ~$55/month |
| Always-on on-demand equivalent | ~$380/month |

The ephemeral architecture is the right call for a portfolio/demo project.

---

*All costs are estimates based on us-east-2 pricing, March 2026.
Actual charges may differ. Check AWS Cost Explorer for your real spend.*
