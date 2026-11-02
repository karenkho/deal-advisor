---
doc_id: project_01
doc_type: project
project_id: P01
domain: port_operations
client_type: Regional port terminal operator (fictional)
title: Port Management System with berth scheduling
project_size: Mid
months: 6
---

# P01 — Port Management System with berth scheduling

## TOR summary

A fictional regional port terminal operator needed one system for vessel calls, berth planning, container yard, gates, passenger terminals and billing. Shipping lines register online and are approved by the port. Each vessel visit is applied for, assessed, berthed, worked (discharge/load, tally, cargo movements), completed and billed. The container yard needed a configurable block/stack/tier layout with slot positioning. Gates issue and check gate passes, including weighing-scale lanes. Passenger terminals record onboarding with QR tickets. Billing produces statements of account per visit and monthly, from rate tables, and posts invoices to the client's ERP. Delivered in 6 months by a Mid team (PM 1, TA 0.5, Dev 4, UI 1, BA 1, QA 2), development phase only.

## Foundation

Cloud-deployed services on a managed Kubernetes cluster with GitOps deployment and CI pipelines; container registry; local accounts with JWT and fine-grained permissions; metrics endpoint and monitoring console; core data model with audit columns.

## Epics and features

### Epic: Customer onboarding

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Shipping-line self-registration with review, approve and return | workflow | M | Given a shipping line submits registration, When port staff approve it, Then the account is activated and an email is sent. | Given staff return it, When remarks are entered, Then the applicant can correct and resubmit. |
| Shipping-line master maintenance | screen_crud | M | Given an approved shipping line, When staff update details, Then the change is saved with remarks in history. | Given the list, When filtered by status, Then matching shipping lines are shown. |

### Epic: Vessel management

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Vessel registration with assessment and approval | workflow | M | Given a submitted vessel registration, When assessed and approved, Then the vessel becomes selectable for visits. | Given a rejection, When recorded, Then the shipping line is emailed the reason. |
| Vessel master maintenance | screen_crud | S | Given a vessel, When its particulars change, Then the update is saved and audit-logged. | Given a search by name, When run, Then matching vessels are listed. |

### Epic: Vessel visit

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Vessel visit application with multi-tab form, drafts and validation | screen_crud | L | Given a draft visit, When the shipping line saves, Then it can be resumed later. | Given missing cargo or container details, When submitted, Then each tab lists its validation errors. |
| Vessel visit assessment and approval | workflow | M | Given a submitted visit, When the port approves, Then it moves to berth planning. | Given a return, When remarks are added, Then the shipping line is notified. |
| Discharge and load list bulk upload with row validation | screen_crud | L | Given a container list file, When uploaded, Then invalid rows are shown with reasons before import. | Given a valid file, When confirmed, Then containers are attached to the visit. |
| Bulk update of cargo and container records with email summary | screen_crud | M | Given selected records, When a bulk update runs, Then all are updated in one step. | Given the update completes, When done, Then a summary email is sent. |
| Arrival, close boarding and departure status flow | workflow | M | Given an arrived vessel, When boarding is closed, Then the visit status changes and time is recorded. | Given departure, When confirmed, Then the visit is ready for completion. |
| Vessel visit completion with cargo and container summary | workflow | M | Given all movements are done, When the visit is completed, Then a cargo and container summary is generated. | Given open movements, When completion is attempted, Then it is blocked. |
| Completion report set (multi-section printable PDFs) | report_dashboard | L | Given a completed visit, When the report is generated, Then all sections print in the official layout. | Given container and break-bulk cargo, When generated, Then both sections appear. |
| Ship crane productivity and lift-time override | report_dashboard | M | Given recorded lifts, When productivity is viewed, Then moves per hour per crane are shown. | Given a supervisor override, When saved, Then the corrected lift time is used. |
| Additional service events per visit | screen_crud | M | Given a visit, When a service event is added, Then it is listed with quantity and time. | Given an invalid event type, When saved, Then it is rejected. |
| Tally sheet capture for discharge, load and general cargo | screen_crud | L | Given a working vessel, When tally clerks record moves, Then quantities update per hatch. | Given a tally mismatch, When saved, Then it is flagged for review. |
| Cargo and container movement tracking with quantity validation | workflow | L | Given a cargo movement, When posted, Then remaining quantity decreases. | Given a quantity above remaining, When posted, Then it is rejected. |

### Epic: Berth planning

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Berth schedule and assignment with audit | workflow | L | Given an approved visit, When a berth and window are assigned, Then overlapping assignments are rejected. | Given a change, When saved, Then the shipping line is notified and the change is audit-logged. |
| Berth forecast list and details | report_dashboard | M | Given a date range, When opened, Then planned berth occupancy is listed. | Given a visit, When clicked, Then its details open. |

### Epic: Container yard

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Yard layout configuration (blocks, stacks, tiers, stack templates) | admin_config | L | Given a new block, When configured from a template, Then its stacks and tiers are created. | Given a block with containers, When deletion is attempted, Then it is blocked. |
| Block allocation management with priority | admin_config | M | Given allocations, When priority changes, Then new containers go to the highest-priority block. | Given a full block, When allocating, Then the next block is used. |
| Container slot positioning (discharge to slot, load, swap, move) | workflow | XL | Given a discharged container, When assigned, Then a valid free slot is suggested respecting stack rules. | Given a swap, When confirmed, Then both positions update atomically. |
| Container information view, update and release with export and email | screen_crud | M | Given a container, When released, Then its status changes and the consignee is emailed. | Given filters, When exported, Then a spreadsheet downloads. |
| Yard block visual view | report_dashboard | M | Given a block, When opened, Then a top view shows occupied and free slots. | Given a slot, When selected, Then the container details show. |
| Yard service requests (stuffing, delivery and receiving bookings) | workflow | M | Given a booking request, When approved, Then the slot and time are reserved. | Given completion, When recorded, Then the requester is emailed. |
| Outbound container receiving with upload validation | screen_crud | M | Given an outbound list, When uploaded, Then invalid containers are rejected with reasons. | Given a received container, When saved, Then it is assigned a slot. |
| Yard truck handling | screen_crud | S | Given a truck, When assigned to a move, Then its plate number is recorded. | Given an unassigned truck, When listed, Then it shows as available. |

### Epic: Gate operations

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Gate pass generation and printing | workflow | L | Given a released container, When a gate pass is generated, Then a numbered printable pass is produced. | Given a paid statement is required, When unpaid, Then generation is blocked. |
| Yard entrance and exit gate processing | workflow | M | Given a valid gate pass, When scanned at exit, Then the exit time is recorded. | Given an expired pass, When scanned, Then exit is refused. |
| Pier entrance and exit gates including weighing-scale lanes | workflow | L | Given a scale lane, When a truck is weighed, Then gross weight is recorded on the pass. | Given a weight outside tolerance, When recorded, Then it is flagged. |
| Empty-truck ticket | screen_crud | S | Given an empty truck, When it enters, Then a ticket prints. | Given the ticket, When scanned at exit, Then it closes. |
| Vessel visit monitoring list | report_dashboard | S | Given active visits, When opened, Then status and progress are shown. | Given a filter, When applied, Then the list updates. |

### Epic: Passenger operations

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Passenger onboarding records with QR ticket scanning | workflow | L | Given a ticket QR, When scanned at boarding, Then the passenger is recorded for the voyage. | Given a duplicate scan, When scanned again, Then it is rejected. |
| Disembarkation recording | screen_crud | M | Given an arrived voyage, When passengers disembark, Then counts are recorded per category. | Given the record, When saved, Then statistics update. |
| Voyage management | screen_crud | M | Given a voyage, When created, Then it is available for onboarding. | Given a schedule change, When saved, Then subscribers are emailed. |
| Terminal fees list | screen_crud | S | Given a date, When opened, Then terminal fees collected are listed. | Given an export, When clicked, Then a file downloads. |

### Epic: Billing

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Vessel, cargo and container charge rate tables | admin_config | L | Given a rate with an effective date, When saved, Then statements after that date use it. | Given overlapping rates, When saved, Then the system rejects them. |
| Yard, storage, lift-on/lift-off and dangerous-goods rate tables | admin_config | L | Given a dangerous-goods class, When rated, Then the surcharge applies on statements. | Given storage free days, When configured, Then storage is charged only after them. |
| Shifting charges | admin_config | M | Given a shifting charge type, When maintained, Then it is available on statements. | Given an inactive charge, When billing, Then it is not applied. |
| Per-visit statement of account generation | workflow | XL | Given a completed visit, When the statement is generated, Then all charges are computed from rate tables. | Given a regenerated statement, When rates changed, Then the differences are shown before saving. |
| Monthly domestic and foreign statements of account | workflow | L | Given a month end, When generated, Then statements per shipping line summarize the month's charges. | Given foreign vessels, When billed, Then foreign rates apply. |
| Container-yard statement of account | workflow | L | Given yard services, When the statement is generated, Then storage and handling charges are listed. | Given a manual charge, When added, Then totals recompute. |
| Statement assessment (issue, return, cancel) and posting | workflow | M | Given a draft statement, When issued, Then it is locked and numbered. | Given a cancellation, When approved, Then the statement is voided with reason. |
| Statement of account printable PDFs | report_dashboard | L | Given an issued statement, When printed, Then it follows the official layout with details pages. | Given multiple service lines, When printed, Then each has its own section. |
| Statement email to customer | notification | S | Given an issued statement, When sent, Then the shipping line receives it by email. | Given an email failure, When it occurs, Then it is logged. |
| Grouped and additional charges on statements | screen_crud | M | Given a grouped charge, When added, Then its components are listed. | Given removal, When confirmed, Then totals recompute. |

### Epic: Reports

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Dwell-time calculation and storage-fee report | report_dashboard | M | Given a container, When dwell time is calculated, Then storage days beyond free time are shown. | Given an override, When saved, Then the report uses it. |
| Passenger daily statistics | report_dashboard | M | Given a date, When run, Then passenger counts by category are shown. | Given export, When clicked, Then a file downloads. |
| Roll-on/roll-off daily statistics per shipping line | report_dashboard | M | Given a date range, When run, Then vehicle and passenger counts per shipping line are shown. | Given a summary view, When opened, Then totals are shown. |
| Service events log with export | report_dashboard | S | Given filters, When run, Then service events are listed. | Given export, When clicked, Then a file downloads. |
| Tally sheet report export | report_dashboard | S | Given a visit, When exported, Then the tally sheet downloads. | Given no tally data, When exported, Then a message is shown. |

### Epic: Integration and notifications

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| ERP integration service (invoice push; master data sync) | integration | L | Given an issued statement, When posted, Then an invoice is created in the ERP and its number stored. | Given the ERP is unavailable, When posting fails, Then it is retried and flagged. |
| Team-chat webhook alerts | integration | S | Given a critical event, When it occurs, Then a message is posted to the operations chat. | Given webhook failure, When it occurs, Then it is logged. |
| Event-triggered email notifications | notification | M | Given a registration, assessment or berth change, When it occurs, Then the relevant party is emailed. | Given a template, When the event fires, Then placeholders are filled. |

### Epic: Administration

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| User management | admin_config | M | Given an administrator, When creating a user, Then roles are assigned and an activation email is sent. | Given a deactivated user, When logging in, Then access is denied. |
| Role and permission management | admin_config | L | Given a role, When permissions change, Then menus and actions follow. | Given a direct API call without permission, When made, Then it is rejected. |
| Reference data maintenance | admin_config | S | Given a reference value, When deactivated, Then it is no longer selectable. | Given a change, When saved, Then it is audit-logged. |
| Service-event reference maintenance | admin_config | S | Given a service event type, When added, Then it is selectable on visits. | Given duplicates, When saved, Then they are rejected. |
| System configuration and document sequences | admin_config | M | Given a document type, When its sequence format is set, Then new numbers follow it. | Given a parameter change, When saved, Then it applies immediately. |
| Activity log and audit trail viewer | admin_config | M | Given any change, When saved, Then user, time and change are logged. | Given filters, When applied, Then matching entries are listed. |

## Assumptions

- Default architecture: cloud-deployed microservices on a managed container platform; cloud and license costs are excluded and handled in the costing sheet.
- The client provides ERP API documentation and a test instance.
- Estimate covers the development phase only; UAT support and warranty not included.

## Exclusions

- Gate hardware, weighing scales, QR scanners and printers.
- ERP licenses and configuration.
- Data migration of historical visits (not required).
