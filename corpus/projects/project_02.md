---
doc_id: project_02
doc_type: project
project_id: P02
domain: energy_trading_billing
client_type: Power generation and retail supply company (fictional)
title: Energy Trading & Billing System
project_size: Large
months: 9
---

# P02 — Energy Trading & Billing System

## TOR summary

A fictional power generation and retail supply company needed one platform for customer and contract management, wholesale-market trading operations, and customer billing and collections. Customers and their supply contracts are registered and reviewed. Trading staff extract market data on a schedule, declare bilateral contract quantities, nominate plant capacity, submit offers to the market, and monitor real-time dispatch. Billing computes monthly bills from contract formulas, market determinants and rate tables, applies interest and penalties, records payments, and produces statements, invoices and revenue reports. Customers use a self-service portal. Delivered in 9 months by a Large team (PM 1, TA 1, Dev 4, UI 1.5, BA 1, QA 3), development phase only.

## Foundation

Cloud-deployed microservices (about ten deployables) on a managed Kubernetes cluster: API gateway, service discovery and central configuration, message broker between services, shared service libraries, distributed tracing and metrics, central audit service, GitOps deployment with CI pipelines, local accounts with JWT and roles.

## Epics and features

### Epic: Customer management

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Customer registration with review and assessment | workflow | L | Given a new customer registration, When the reviewer approves, Then the customer becomes active for contracting. | Given a return, When remarks are added, Then the account officer can correct and resubmit. |
| Customer profile management (company, contacts, positions, assigned officers) | screen_crud | L | Given a customer, When contacts are added with positions, Then they appear in notifications lists. | Given an officer reassignment, When saved, Then history records the change. |
| Energy share allocation per customer | screen_crud | M | Given a customer with several facilities, When shares are entered, Then they must total 100%. | Given a share change, When saved, Then it is effective from the chosen month. |
| Contract registration with review, loads, sources and attachments | workflow | L | Given a contract registration, When submitted with loads and sources, Then it routes for review. | Given an approved contract, When viewed, Then its attachments and variables are read-only. |
| Power supply contract management with attachments | screen_crud | M | Given a contract, When an amendment is uploaded, Then it is versioned. | Given an expiring contract, When listed, Then it is highlighted. |
| Contract variables with bulk add | screen_crud | M | Given a variable template, When bulk-added to contracts, Then each contract receives the values. | Given a missing mandatory variable, When saving, Then it is rejected. |
| Customer targets | screen_crud | S | Given a customer, When annual targets are entered, Then monthly targets are derived. | Given a target, When edited, Then history is kept. |
| Customer performance rating | screen_crud | S | Given a review period, When a rating is entered, Then it is shown on the profile. | Given ratings, When listed, Then they are sortable. |
| Monthly customer data capture | screen_crud | S | Given a month, When customer data is entered, Then it is available for billing. | Given a closed month, When editing, Then it is blocked. |
| Customer requests with assessment | workflow | M | Given a customer request, When assessed, Then the customer sees the decision. | Given a pending request, When overdue, Then it is flagged. |
| Announcements | screen_crud | S | Given an announcement, When published, Then it appears in the customer portal. | Given an expiry date, When reached, Then it is hidden. |
| Interruptible load program tool | screen_crud | M | Given an enrolled customer, When curtailment events are recorded, Then credits are computed for billing. | Given an event, When edited after billing, Then it is blocked. |

### Epic: Customer portal

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Customer self-service portal (dashboard, bills, nominations, requests, profile) | screen_crud | L | Given a customer user, When they log in, Then they see only their own bills and nominations. | Given a new request, When submitted from the portal, Then it appears in the staff queue. |

### Epic: Trading operations

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Scheduled market-data extraction from the market operator (~45 datasets) | integration | XL | Given the schedule, When an extraction runs, Then each dataset is loaded and the run is logged with counts. | Given a failed dataset, When the run completes, Then it is retried and the failure is visible. |
| Settlement file ingestion and processing | integration | L | Given a settlement file, When ingested, Then amounts are parsed into trading records. | Given a malformed file, When ingested, Then it is rejected with line errors. |
| Bilateral contract quantity declaration, update and variance checks | workflow | L | Given a trading day, When quantities are declared, Then they are validated against contract limits. | Given settlement data, When compared, Then variances above tolerance are listed. |
| Customer nominations (create, review) | workflow | M | Given a customer nomination, When reviewed, Then it is accepted or returned. | Given a late nomination, When submitted, Then it is flagged. |
| Plant capacity nomination with ramp rates, draft and final, assessment and graph | workflow | XL | Given a draft nomination, When finalized, Then capacities and ramp rates are locked for submission. | Given a graph view, When opened, Then intervals are charted against limits. |
| Offer preparation and XML submission to the market | integration | L | Given offer blocks and intervals, When submitted, Then a valid XML file is generated and sent. | Given a schema validation error, When generating, Then the offer is not sent. |
| Market management system offer copy and extract-error handling | integration | M | Given a prior offer, When copied, Then a new draft is created. | Given extract errors, When listed, Then they can be reprocessed. |
| Daily metered quantities with nominated views and adjustments | screen_crud | M | Given a day, When metered quantities are viewed, Then nominated and actual are compared. | Given an adjustment, When saved, Then it is audit-logged. |
| Billing determinants generation, checkers, summary, import and review | workflow | XL | Given a billing period, When determinants are generated, Then checker rules flag inconsistencies. | Given review approval, When granted, Then determinants are released to billing. |
| Real-time dispatch monitoring (5-minute and hourly charts, tolerance, messaging) | report_dashboard | XL | Given live dispatch data, When the monitor is open, Then the 5-minute chart refreshes each interval. | Given a deviation above tolerance, When detected, Then an alert message is raised. |
| Market real-time outlook (advisories, regional map and charts) | report_dashboard | L | Given market advisories, When published, Then they appear on the outlook page. | Given a region, When selected, Then its price and demand charts show. |
| Market trading node maintenance | admin_config | S | Given a trading node, When added, Then it is selectable for plants. | Given an inactive node, When listed, Then it is marked. |

### Epic: Billing and collections

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Billing variables (index and exchange-rate values, attachments, assessment) | screen_crud | M | Given a month, When index values are entered with sources, Then they await assessment. | Given approval, When granted, Then billing uses them. |
| Distribution and time-of-use rate tables | admin_config | M | Given a time-of-use rate, When effective-dated, Then bills after that date use it. | Given overlapping periods, When saved, Then they are rejected. |
| Fuel cost entries with assessment | screen_crud | M | Given monthly fuel cost, When entered, Then it awaits assessment. | Given approval, When granted, Then pass-through charges use it. |
| Configurable billing formula template builder with preview | workflow | XL | Given a formula template, When built from variables and operators, Then a preview computes a sample bill. | Given an invalid formula, When saved, Then the error position is shown. |
| Admin query builder for billing particulars with parameters and preview | report_dashboard | XL | Given a parameterized query, When previewed, Then results display with the parameters applied. | Given a query writing data, When saved, Then it is rejected. |
| Monthly bill computation per contract with assessment and ledger | workflow | XL | Given released determinants, When bills are computed, Then each contract's lines follow its formula template. | Given assessment approval, When granted, Then ledger entries are posted. |
| Customer bills: view, send, cancellation request, ERP invoice upload | workflow | L | Given an approved bill, When sent, Then the customer receives it by email and in the portal. | Given a cancellation request, When approved, Then the bill is voided with reason. |
| Bill and invoice PDF templates | report_dashboard | L | Given an approved bill, When printed, Then the template for the customer class is used. | Given an adjustment bill, When printed, Then it references the original. |
| Interest and penalty computation and management | workflow | L | Given an overdue bill, When the computation runs, Then interest is computed per contract terms. | Given a waiver, When approved, Then the penalty is reversed. |
| Wholesale settlement charges pass-through (charges, adjustments, contestable shares) | workflow | L | Given wholesale charges for a month, When allocated, Then each customer receives its share. | Given an adjustment, When posted, Then the next bill reflects it. |
| Collection notices with assessment | workflow | M | Given overdue accounts, When notices are generated, Then they await assessment before sending. | Given approval, When granted, Then notices are emailed. |
| Payment recording and allocation to bills; payment history | workflow | L | Given a payment, When allocated, Then oldest bills are settled first unless specified. | Given an overpayment, When posted, Then a credit is recorded. |
| Statement of account generation, view, remarks and sending | workflow | M | Given a customer, When a statement is generated, Then open bills and payments are listed. | Given sending, When confirmed, Then the customer is emailed. |
| Revenue report setup, parameters, generation and review | report_dashboard | L | Given a revenue setup, When generated for a month, Then revenue by customer and component is shown. | Given review, When approved, Then the report is locked. |
| Holiday calendar | admin_config | S | Given a holiday, When added, Then due-date computation skips it. | Given a past holiday, When edited, Then it is blocked. |

### Epic: Notifications, reports and dashboard

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Notification service: event messages, user and contact notifications, market alerts | notification | L | Given a market event, When published, Then subscribed users receive in-app and email notifications. | Given a contact preference, When set, Then only chosen channels are used. |
| Report registry and viewer with per-user access | report_dashboard | M | Given a registered report, When a user has access, Then it appears in their report list. | Given no access, When opening, Then it is denied. |
| Home dashboard | report_dashboard | M | Given a user, When they log in, Then key counts for their role are shown. | Given a widget, When clicked, Then the related list opens. |

### Epic: Administration

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| User management | admin_config | M | Given a new user, When created, Then roles are assigned and an activation email is sent. | Given deactivation, When saved, Then sessions end. |
| Role management | admin_config | L | Given a role, When permissions are edited, Then gateway authorization follows. | Given a change, When saved, Then it is audit-logged. |
| Company management | admin_config | S | Given a company, When added, Then it is selectable for customers. | Given duplicates, When saved, Then they are rejected. |
| Plant management | admin_config | S | Given a plant, When added with its node, Then it is available for nominations. | Given an inactive plant, When nominating, Then it is not selectable. |
| Reference data maintenance | admin_config | S | Given a reference value, When deactivated, Then it is hidden from forms. | Given a change, When saved, Then it is audit-logged. |
| Contract variable definitions | admin_config | S | Given a variable definition, When created, Then it can be used in formulas. | Given a variable in use, When deletion is attempted, Then it is blocked. |
| System configuration | admin_config | M | Given a parameter, When changed, Then services pick it up from central configuration. | Given an invalid value, When saved, Then it is rejected. |
| Activity log viewer backed by the central audit service | admin_config | M | Given any service action, When performed, Then an audit event is stored centrally. | Given filters, When applied, Then matching events are listed. |
| User profile and change password | screen_crud | S | Given a logged-in user, When the password is changed, Then the policy is enforced. | Given the profile, When viewed, Then roles are shown. |

## Assumptions

- Default architecture: cloud-deployed microservices on a managed container platform; cloud and license costs are excluded and handled in the costing sheet.
- Market operator interfaces and file formats are documented and stable.
- Estimate covers the development phase only; UAT support and warranty not included.

## Exclusions

- Market operator registration and fees.
- ERP licenses and configuration.
- Metering hardware.
