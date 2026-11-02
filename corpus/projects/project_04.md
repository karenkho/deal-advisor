---
doc_id: project_04
doc_type: project
project_id: P04
domain: loan_origination
client_type: Consumer lending company (fictional)
title: Loans Originating System with automated loanable-amount computation
project_size: Small
months: 4
---

# P04 — Loans Originating System

## TOR summary

A fictional consumer lending company wanted applicants to apply for salary and personal loans online and get an automated loanable amount. Applicants register, complete a multi-step application, and upload documents. A rules engine checks eligibility and computes the loanable amount from income, existing obligations and product rules. A credit-bureau check is run automatically. Applications are approved in two levels (credit officer, then credit manager), and applicants are notified at each step. Administrators maintain loan products and rules without code changes. Delivered in 4 months by a Small team (PM 1, TA 0.5, Dev 2, UI 1, BA 1, QA 1), development phase only.

## Foundation

Cloud-deployed microservices (applicant, origination, rules, notifications) behind an API gateway; infrastructure-as-code for dev/UAT/prod; CI/CD per service; applicant and staff identity with RBAC; central logging and monitoring; core data model.

## Epics and features

### Epic: Applicant portal

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Applicant registration and login with OTP and privacy consent | screen_crud | M | Given a new applicant, When registering, Then privacy consent and mobile OTP verification are required. | Given an existing email, When reused, Then registration is rejected without revealing details. |
| Multi-step online loan application | screen_crud | M | Given the application wizard, When the applicant completes each step, Then progress is saved as a draft. | Given missing required fields, When submitted, Then they are listed per step. |
| Document upload with requirements checklist | screen_crud | M | Given a loan product, When the applicant opens uploads, Then the product's required documents are listed. | Given a file over 10 MB, When uploaded, Then it is rejected. |
| Application status tracking | screen_crud | S | Given a submitted application, When viewed, Then the current stage and decision date are shown. | Given a return for more documents, When it occurs, Then the request is visible. |

### Epic: Origination

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Eligibility and loanable-amount rules engine | workflow | L | Given declared income and obligations, When the application is submitted, Then the loanable amount is computed from the product's debt-to-income rule. | Given an applicant below the minimum age or income, When evaluated, Then the application is declined with the rule cited. |
| Credit-bureau check integration | integration | L | Given a submitted application, When the check runs, Then the bureau score and report reference are stored. | Given the bureau is unavailable, When the check fails, Then the application waits in "pending credit check" and retries. |
| Two-level approval (credit officer, credit manager) | workflow | M | Given an application within limits, When the credit officer recommends, Then it routes to the credit manager. | Given a decline, When recorded, Then remarks are required and the applicant is notified. |
| Offer letter PDF generation | report_dashboard | S | Given an approved loan, When the offer is generated, Then the PDF shows amount, term and schedule summary. | Given acceptance, When the applicant confirms, Then the offer is marked accepted. |

### Epic: Notifications and reporting

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Email and SMS status notifications | notification | M | Given a status change, When saved, Then the applicant receives email and SMS. | Given a failed SMS, When the provider errors, Then it is logged and retried once. |
| Application pipeline report | report_dashboard | S | Given a date range, When run, Then counts by stage and average turnaround are shown. | Given export, When clicked, Then a spreadsheet downloads. |

### Epic: Administration

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Loan product and rule configuration | admin_config | M | Given a product, When rates, terms and debt-to-income caps are set with an effective date, Then new applications use them. | Given an in-flight application, When rules change, Then it keeps the original rules. |
| Staff user and role administration (fixed roles) | admin_config | S | Given a credit officer, When created, Then approval limits follow the role. | Given deactivation, When saved, Then access ends. |
| Audit trail | admin_config | M | Given any decision or rule change, When saved, Then user, time and before/after values are logged. | Given an auditor, When searching by application, Then the full history is shown. |
| Security hardening and penetration-test remediation | nfr | M | Given the third-party penetration test, When findings are reported, Then all high and critical findings are fixed and re-tested. | Given OWASP Top 10 checks, When the release candidate is scanned, Then no high findings remain. |

## Assumptions

- Default architecture: cloud-deployed microservices; cloud and license costs are excluded and handled in the costing sheet.
- One credit bureau with documented API and sandbox.
- Loan servicing (disbursement, collections) is out of scope.
- Estimate covers the development phase only; UAT support and warranty not included.

## Exclusions

- Credit bureau subscription and per-inquiry fees.
- Third-party penetration test fees.
- Loan management/servicing system.
