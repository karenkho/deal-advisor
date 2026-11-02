---
doc_id: guideline
doc_type: guideline
title: Ground-Up Build Estimation Guideline (synthetic, POC)
---

# Ground-Up Build Estimation Guideline

## Purpose

This guideline describes how a delivery team turns a client Terms of Reference (TOR) for a ground-up software build into a sized feature backlog. It is decision support: every backlog and size produced with it must be reviewed by a Business Analyst (BA) before it is used. Sizes are expressed as T-shirt tiers (S, M, L, XL); hours per tier and role live in a separate estimation configuration owned by delivery leads. This guideline never covers pricing, rates, or margins — costing is done by sales from the hours hand-off. All examples in this corpus are synthetic.

## Feature types

Every feature is classified into exactly one type. The type selects the sizing rubric.

- **screen_crud** — data entry and maintenance screens: create, read, update, delete, list, search and filter for one business entity.
- **workflow** — multi-step processes with states, approvals, routing, escalations, or SLAs.
- **integration** — exchange of data with an external system: payment gateway, government API, ERP, SMS/email provider, SSO identity provider, file-based interfaces.
- **report_dashboard** — read-only outputs: printable reports, exports, KPI dashboards, charts.
- **data_migration** — moving legacy data (spreadsheets, old databases, paper records already encoded) into the new system, including cleansing, mapping and reconciliation.
- **notification** — email, SMS, push or in-app messages triggered by events or schedules.
- **admin_config** — reference data maintenance, system parameters, templates, user and role administration screens.
- **nfr** — non-functional work that produces a deliverable of its own: performance tuning to a stated target, accessibility conformance, security hardening, high availability or DR setup.

When a requirement spans types, split it into one feature per type (for example, "approve permit and notify applicant by SMS" becomes a workflow feature plus a notification feature).

## Sizing rubric: screen_crud

- **S** — one entity, up to ~10 fields, simple validation, single list with basic filter, no file attachments.
- **M** — one entity with child records (master-detail), 10–30 fields, cross-field validation, search with several filters, CSV export, or file attachment.
- **L** — several related entities managed together, dynamic forms, complex validation rules from a rule table, bulk edit, or role-dependent field visibility.
- **XL** — configurable form builder, highly dynamic screens driven by metadata, or offline-capable data capture with sync.

Size up one tier when the screen must support more than two languages or strict accessibility conformance (WCAG 2.1 AA) beyond the project default.

## Sizing rubric: workflow

- **S** — single-step approval (approve/reject) with status field and history, one role.
- **M** — two-level approval or a linear 3–5 state process, return-for-correction, basic SLA timestamp.
- **L** — three or more approval levels, conditional routing by amount/type/location, delegation, escalation on SLA breach, parallel reviewers.
- **XL** — configurable workflow engine (admin-defined steps and rules), or a workflow spanning multiple organizations with external hand-offs.

A workflow feature includes its state history and audit entries. Notifications triggered by the workflow are separate notification features.

## Sizing rubric: integration

- **S** — outbound call to a well-documented REST API with a sandbox, one operation, no callback (e.g., address lookup).
- **M** — two to four operations on a documented API, or a file drop (CSV/SFTP) in a fixed format, with retry and error logging.
- **L** — payment gateway with callbacks/webhooks and reconciliation, SSO with an enterprise identity provider, or a government API with certificates, IP allow-listing, or onboarding steps.
- **XL** — undocumented or legacy system integration (no sandbox, no spec), bi-directional sync with conflict handling, or a high-volume real-time interface.

Size up one tier when the counterparty's API documentation or sandbox is not yet available at estimation time and record an assumption.

## Sizing rubric: report_dashboard

- **S** — one tabular report with up to 5 filters, export to CSV/Excel.
- **M** — formatted printable report (PDF with header/footer/signatories), or a report with grouping, subtotals and drill-down to records.
- **L** — dashboard with 4–8 KPI widgets and charts, role-based data scope, date-range comparison.
- **XL** — self-service ad-hoc reporting, or a dashboard on near-real-time data from multiple sources requiring a separate reporting store.

## Sizing rubric: data_migration

- **S** — one clean spreadsheet, under ~5,000 rows, one target entity, client does cleansing.
- **M** — two to four source files or tables, mapping rules, validation report of rejected rows, one rehearsal run.
- **L** — legacy database with many tables, code-value mapping, deduplication, two rehearsals plus cut-over plan and reconciliation report.
- **XL** — multiple legacy systems, poor data quality requiring vendor-side cleansing, document/attachment migration, or phased migration with coexistence.

Data migration is frequently implied but not stated (e.g., "the system shall contain all existing member records"). Raise it as a feature and a client question when existing records are mentioned.

## Sizing rubric: ai_ml_feature

AI or machine-learning capability the TOR asks the system itself to provide (chatbots, document extraction, recommendations, forecasting), owned by the AI Engineer. S: one call to a hosted model behind an existing screen, no custom data. M: a prompt or retrieval pipeline over the client's documents with evaluation checks. L: several models or a custom training set, with monitoring and human review of outputs. XL: a regulated or decision-making use with bias, audit and explainability requirements, or custom model development.

## Sizing rubric: notification

- **S** — one or two event-triggered emails from fixed templates.
- **M** — a set of event-triggered email and SMS notifications through one provider, editable templates, delivery log.
- **L** — scheduled reminders and escalations, user notification preferences, multiple channels (email, SMS, in-app), retry on provider failure.
- **XL** — campaign-style bulk messaging with segmentation, throttling and opt-out management.

## Sizing rubric: admin_config

- **S** — maintain one simple reference table (code, name, active flag).
- **M** — maintain several reference tables, or system parameters with validation and change history.
- **L** — user and role administration with permission matrix, or document/letter template management with placeholders.
- **XL** — tenant/organization hierarchy administration with delegated administration.

## Sizing rubric: nfr

- **S** — documented configuration only (e.g., session timeout, password policy values).
- **M** — measurable target requiring work: load test to a stated concurrency, WCAG 2.1 AA audit and fixes for a small app, backup/restore runbook.
- **L** — high-availability deployment across zones, DR with stated RPO/RTO and a DR drill, third-party penetration test remediation.
- **XL** — strict regulatory certification or performance targets requiring architecture change.

Generic NFR boilerplate does not become a feature (see "Treating NFR boilerplate").

## Definition of Ready

A feature is ready for sizing when:

1. It has a clear actor and a business outcome ("As a registrar, I can …").
2. The data it handles is identified at the entity level.
3. Business rules are stated or explicitly assumed.
4. External systems it touches are named, with documentation availability known.
5. It has at least two acceptance criteria in Given/When/Then form.
6. Open questions are logged as client questions, not hidden in the size.

A feature that fails points 1–3 is routed as **unclear** and becomes a client question instead of being sized.

## Writing acceptance criteria

Features follow INVEST: Independent, Negotiable, Valuable, Estimable, Small, Testable. Acceptance criteria use Given/When/Then:

- *Given* a precondition and context,
- *When* the actor performs an action,
- *Then* an observable, testable outcome occurs.

Write 2–4 criteria per feature covering the main path and the most important rule or error case. Avoid UI-implementation detail ("click the blue button"). Each criterion must be verifiable by QA without asking the BA.

Example: *Given* an applicant with an incomplete application, *When* they submit, *Then* the system lists the missing required fields and does not change the status.

## Foundation epic

Default architecture: cloud-deployed microservices; cloud and license costs are excluded and handled in the costing sheet.

Every ground-up build includes a Foundation epic, estimated separately from TOR features with fixed hours from the estimation configuration. It always contains:

- cloud environments and infrastructure-as-code (development, UAT, production);
- container platform setup (cluster or container apps, ingress, secrets);
- API gateway and a service template (shared libraries, health checks, configuration);
- identity and authentication service (login, password policy or SSO hook, RBAC);
- CI/CD pipeline per service (build, automated tests, deployment);
- centralized logging, monitoring and distributed tracing;
- core data model (base entities, reference data, audit columns);
- UI shell and design system (layout, navigation, shared components).

If the TOR implies user accounts (applicants, staff, approvers, "my records") but never mentions login or authentication, authentication is still required: it is covered by the Foundation epic, and the BA should confirm the login method (local accounts vs SSO) as a client question. Cross-cutting items that are NOT in Foundation and must be checked as separate features: audit trail viewer, data migration, user administration screens, notification framework, and reporting.

## Coverage checklist

Before finalizing a backlog, check for these frequently missed features:

1. Authentication and user administration (who creates accounts? self-registration?).
2. Role and permission management.
3. Audit trail of create/update/delete and approvals, and a screen to view it.
4. Data migration of existing records.
5. Notifications for workflow state changes.
6. Reference data / lookup maintenance.
7. Reports implied by "monitor", "track", "summary", "statistics".
8. Document upload and storage if forms mention attachments or requirements checklists.
9. Search and filter on every main list.
10. Integration points named anywhere in the TOR, including annexes.

## Standard exclusions

Unless the TOR explicitly makes them a vendor deliverable and they are agreed, the following are excluded and listed in the exclusion list:

- supply of hardware (laptops, desktops, servers, printers, scanners, network equipment, tablets);
- software licenses and subscriptions (database, OS, office, third-party SaaS);
- third-party fees (payment gateway transaction fees, SMS charges, certificate fees, government API onboarding fees, cloud hosting charges);
- content entry and data encoding of paper records;
- warranty, hypercare and end-user help-desk after go-live (development-phase estimate only);
- business process re-engineering and organizational change management;
- internet connectivity at client sites.

## Standard assumptions

- The client provides a single product owner with decision authority and timely feedback (within 3 working days).
- The client provides access to external system documentation and sandboxes before the related sprint.
- The estimate covers the development phase only: UAT support and warranty not included (quoted separately if required).
- English is the only UI language unless stated.
- The application is web-based and responsive; no native mobile apps unless stated.
- Default architecture: cloud-deployed microservices; cloud and license costs are excluded and handled in the costing sheet.

## Treating NFR boilerplate

AI-generated and template TORs often contain long NFR sections ("the system shall be secure, scalable, user-friendly, available 24/7, and compliant with all applicable laws"). Treat such statements as follows:

- Generic, unmeasurable statements become **assumptions** ("standard secure coding practices per OWASP Top 10 apply"), not features.
- A measurable target (e.g., "99.9% availability", "500 concurrent users with < 3 s response", "RPO 1 hour") becomes an **nfr** feature sized with the nfr rubric, and the target is copied into its acceptance criteria.
- Contradictory or impossible NFRs (e.g., "zero downtime" with a single-server budget) become **client questions**.
- Compliance references (e.g., Data Privacy Act) become an assumption plus, if specific controls are named (consent capture, data subject access request), a feature.

## Handling duplicates and conflicts

Long TORs repeat the same requirement in several sections. Merge duplicates into one feature and keep every page citation. When two statements of the same requirement conflict (different numbers of approval levels, different deadlines, different user groups), do not pick one silently: size with the more conservative interpretation, mark it, and raise a client question quoting both citations.

## Out-of-scope and unclear items

- **Out of scope**: items that are not software build work (hardware supply, licenses, physical installation, manpower augmentation) are routed out_of_scope and listed as exclusions with the TOR citation.
- **Unclear**: requirements with no actor, no outcome, or undefined terms ("the system shall handle all relevant transactions efficiently") are routed unclear and converted to a client question. They are not sized until answered.
- **Novel**: requirements with no close precedent in past projects are sized with the rubric only and flagged for BA attention.

## AI suitability guidance

Every delivery activity (per feature and role) is classified into one AI strategy. The classification drives the AI-Augmented (B) and AI-First (C) effort; the factors are configurable assumptions, not validated benchmarks. Consider requirements clarity, functional complexity, integration complexity, security sensitivity, standardization and reusability, testability, the need for human verification, and expected rework.

- **human_led**: people do the work; AI helps only with drafting and lookup. Use for client-facing elicitation and clarification, client acceptance and UAT support, negotiation of unclear scope, and decisions with accountability (security design sign-off). Examples: elicitation workshops with HR managers; acceptance walkthroughs with the client.
- **ai_assisted**: a person produces the output with an AI assistant (code completion, test suggestions, story drafting). Use for security LOGIC (authentication, authorization and RBAC enforcement, encryption, personal-data export and handling logic, payroll computation), complex or novel business rules, integrations with unspecified or external counterparts, and anything where requirements are unclear. A screen, report or form that merely displays or captures personal data is NOT security logic: classify it like any other screen. Examples: RBAC enforcement code; payroll interface logic; leave-accrual rules with many exceptions.
- **ai_first_with_review**: an AI agent produces the first complete version and a person reviews, integrates and verifies it. Use for standardized, well-specified, testable work with strong precedents: CRUD screens and APIs, standard reports and exports, unit and component tests for clear rules, test case design from clear acceptance criteria, wireframes for standard screens, notification templates. Examples: employee master-data screens; standard tabular reports; unit tests for a documented computation.
- **deterministic_automated**: generators, templates or pipelines produce the output with minimal human effort. Use for scaffolding from templates, pipeline-as-code from a standard template, regression suites re-run by CI, and code generated from schemas. Examples: CI/CD pipeline from the company template; API client generated from an OpenAPI schema.

Never classify elicitation or client acceptance as anything but human_led, and never classify security logic above ai_assisted. Wireframes and prototypes are at least ai_assisted (BAs already generate them with AI).
