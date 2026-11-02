---
doc_id: project_06
doc_type: project
project_id: P06
domain: lgu_citizen_requests
client_type: Municipal government (LGU) public assistance office (fictional)
title: LGU Citizen Request and Complaint Tracker
project_size: Small
months: 4
---

# P06 — LGU Citizen Request and Complaint Tracker

## TOR summary

A fictional municipality wanted citizens to file requests and complaints (road repair, garbage collection, streetlights, document requests) online or through the public assistance desk, track them with a reference number, and receive updates. Requests are routed automatically to the responsible department by category and barangay, with SLA timers and escalation to the department head when overdue. The mayor's office wanted a public statistics page and an internal dashboard. Citizens could attach photos and a map location. The TOR required WCAG 2.1 AA for the public site. Delivered in 4 months by a Small team (PM 1, TA 0.5, Dev 2, UI 1, BA 1, QA 1), development phase only.

## Foundation

Cloud-deployed microservices (intake, case management, notifications, reporting) behind an API gateway; infrastructure-as-code for dev/UAT/prod; CI/CD per service; staff identity with RBAC; central logging and monitoring; core data model.

## Epics and features

### Epic: Citizen intake

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Online request filing with photo and map pin | screen_crud | M | Given a citizen, When they file a request, Then category, barangay, description and optional photos and map pin are captured. | Given a photo over 5 MB, When uploaded, Then it is compressed or rejected. |
| Public request tracking by reference number | screen_crud | S | Given a reference number and email, When entered, Then the request status and updates are shown without login. | Given a wrong combination, When entered, Then no details are revealed. |
| Walk-in request encoding at the public assistance desk | screen_crud | S | Given a walk-in citizen, When staff encode the request, Then a reference slip is printed. | Given no email, When encoded, Then the mobile number is required. |
| Public FAQ and service catalogue page | screen_crud | S | Given a request category, When a citizen opens the catalogue, Then requirements and expected resolution days are shown. | Given an inactive category, When listed, Then it is hidden. |

### Epic: Routing and resolution

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Auto-routing by category and barangay with SLA escalation | workflow | L | Given a new request, When submitted, Then it is assigned to the department mapped to its category and barangay. | Given an SLA breach, When the timer expires, Then it escalates to the department head and both are notified. |
| Department case handling with field inspection and photo evidence | workflow | L | Given an assigned case, When the field inspector records findings with photos and GPS, Then the citizen sees the update. | Given a resolved case, When closed, Then the citizen is asked to confirm or reopen within 7 days. |

### Epic: Notifications

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| SMS and email status updates to citizens | notification | M | Given a status change, When saved, Then the citizen receives an SMS or email with the reference number. | Given notification failure, When the provider errors, Then it is logged. |

### Epic: Reporting and transparency

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Internal dashboard of requests by department and SLA compliance | report_dashboard | L | Given the mayor's office, When opened, Then open, overdue and resolved counts and SLA compliance per department are shown. | Given a barangay filter, When applied, Then widgets refresh. |
| Public statistics page | report_dashboard | M | Given a public visitor, When opening the page, Then monthly counts by category and average resolution days are shown without personal data. | Given the page, When audited, Then it meets WCAG 2.1 AA. |
| Monthly department performance report | report_dashboard | S | Given a month, When run, Then cases received, resolved and overdue per department are listed. | Given export, When clicked, Then Excel downloads. |

### Epic: Administration and compliance

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Category, department and SLA maintenance | admin_config | M | Given a category, When mapped to a department and SLA days, Then new requests use the mapping. | Given an SLA change, When saved, Then only new requests use it. |
| Staff user administration with fixed roles | admin_config | S | Given a department handler, When created, Then they see only their department's cases. | Given an administrator, When roles change, Then it is audit-logged. |
| Audit trail | admin_config | M | Given any case update, When saved, Then user, time and change are logged. | Given an auditor, When searching by reference, Then the full history appears. |
| WCAG 2.1 AA accessibility conformance for public pages | nfr | M | Given the public pages, When audited with automated and manual checks, Then no AA failures remain. | Given keyboard-only navigation, When filing a request, Then all steps are reachable. |
| Migration of open complaints from logbook spreadsheet | data_migration | S | Given the open complaints spreadsheet, When loaded, Then each gets a reference number and department. | Given closed items, When encountered, Then they are not migrated. |

## Assumptions

- Default architecture: cloud-deployed microservices; cloud and license costs are excluded and handled in the costing sheet.
- Map pin uses a free map tile service; no paid GIS license.
- Barangay-to-department mapping is provided by the municipality.
- Estimate covers the development phase only; UAT support and warranty not included.

## Exclusions

- Desktop computers for the public assistance desk.
- Hotline/call-center telephony.
- Content writing for the public page.
