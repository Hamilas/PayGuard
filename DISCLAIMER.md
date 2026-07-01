# DISCLAIMER: PLEASE READ BEFORE USE

## This is a Learning / Portfolio Project

This repository contains the source code for an **AI-Powered Fraud Detection
MLOps Pipeline** built over 10 days as a personal learning and portfolio
project. It is **NOT production-ready** and is shared solely for educational
and demonstrative purposes.

---

## Security Warnings

- **Do NOT use this codebase in a production environment without a thorough
  security review.** It has not been hardened, pen-tested, or audited.
- All secrets, API keys, and credentials referenced in this project are
  managed via **AWS SSM Parameter Store** and are **never** stored in code.
  The `REPLACE_ME` placeholders in Terraform are seed values that must be
  replaced with real values via the `update-secrets.sh` script.
- This project is designed for a **single-developer, single-account** AWS
  environment. Multi-tenant or shared-account deployments require significant
  additional hardening.
- The SSH key pair is auto-generated ephemerally by Terraform and
  auto-deleted on `terraform destroy`. Do not reuse it across projects.
- The default passwords for Grafana and Airflow set in deploy-platform.sh must be changed
  before any external-facing deployment.

---

## Cost Warning

Running this project on AWS **will incur real costs**. Estimated costs
based on the author's 10-day build:

| Resource | Approx. Daily Cost |
|---|---|
| EC2 t3.2xlarge Spot (~8–10 hrs/day) | ~$0.90–$1.10/day |
| RDS db.t3.micro (always-on) | ~$0.50/day |
| S3 (5 buckets, minimal data) | ~$0.05/day |
| ECR, SSM, CloudWatch | ~$0.10/day |
| LLM APIs (Claude / Gemini / Perplexity) | ~$0.50–$2.00/day |
| **Total (10-day project)** | **~$25–$40 total** |

You are solely responsible for any AWS charges incurred. The author
assumes no liability for unexpected costs or billing surprises.

**Always run `terraform destroy` at the end of each session.**

---

## Data and Model Disclaimer

- The fraud detection model is trained on the **PaySim** synthetic dataset,
  which simulates mobile money transactions. It does NOT represent real
  financial transaction data.
- Model performance metrics (ROC-AUC 0.9995) are achieved on synthetic
  data and **cannot be assumed to hold on real-world fraud data**.
- This model **must not be used to make actual fraud decisions** against
  real users or transactions.

---

## Legal

- The author is not responsible for any financial losses, data breaches,
  or regulatory violations resulting from use of this code.
- This project uses third-party open-source libraries. Each library is
  subject to its own license. See `requirements/requirements-mlops.txt`
  and `requirements/day5-agents.txt`.
- This project integrates third-party LLM APIs (Anthropic Claude, OpenAI GPT, Google
  Gemini, Perplexity). Use of these APIs is subject to their respective
  Terms of Service.
- AWS services used in this project are subject to AWS's Terms of Service.
- The author makes no warranty, express or implied, about the fitness of
  this software for any particular purpose.

---

## Intended Use

| | |
|---|---|
| Yes | Learning about MLOps architecture and tooling |
| Yes | Understanding LangGraph multi-agent patterns |
| Yes | Portfolio demonstration of end-to-end ML system design |
| Yes | Reference implementation for K3s-based ML infrastructure |
| No | Production fraud detection |
| No | Real financial decisions of any kind |
| No | Multi-tenant or enterprise deployment without security review |

---

*Rebuilt and adapted for local Docker Compose by Rayen Lassoued, June 2026.
Original platform architecture (10-day AWS/K3s build) by Rayen Lassoued, March 2026.*
