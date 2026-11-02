---
doc_id: project_03
doc_type: project
project_id: P03
domain: on_demand_courier
client_type: Metro Manila on-demand motorcycle courier start-up (fictional)
title: On-demand motorcycle courier app, Metro Manila
project_size: Mid
months: 6
---

# P03 — On-demand motorcycle courier app, Metro Manila

## TOR summary

A fictional courier start-up wanted a same-day motorcycle delivery service in Metro Manila with a 90-minute delivery promise. Customers book pickups in a mobile app, see a computed fare, add built-in package insurance, pay by e-wallet or card, and track their rider live on a map. Riders apply through a rider app, complete training and an exam, and receive jobs from an automatic dispatch and matching engine. Operations staff monitor every active delivery against the 90-minute SLA on a live map, reassign jobs, handle insurance claims and support tickets, and pay riders weekly. Delivered in 6 months by a Mid team (PM 1, TA 0.5, Dev 4, UI 1, BA 1, QA 2), development phase only.

## Foundation

Cloud-deployed microservices (customer, rider, dispatch, pricing, payments, notifications) behind an API gateway, on a managed container platform; infrastructure-as-code for dev/UAT/prod; CI/CD per service; customer and rider identity with OTP; central logging, metrics and tracing; core data model.

## Epics and features

### Epic: Customer app

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Customer sign-up and login with mobile OTP | integration | M | Given a new mobile number, When the customer requests an OTP, Then a 6-digit code is sent by SMS and expires in 5 minutes. | Given three wrong codes, When entered, Then login is blocked for 15 minutes. |
| Delivery booking with map pickup/drop-off, package details and scheduling | screen_crud | L | Given pickup and drop-off pins, When the customer books, Then addresses, package size and schedule are captured. | Given an address outside the service zone, When booking, Then it is rejected with a message. |
| Fare computation (distance, size, surcharges, promos) | workflow | L | Given a route distance and package size, When a quote is requested, Then the fare is computed from the active rate card with line items. | Given a valid promo code, When applied, Then the discount is shown separately. |
| Package insurance coverage at booking | workflow | M | Given a declared value, When insurance is selected, Then the premium is added and a coverage reference is issued. | Given a declared value above the cap, When selected, Then the booking is blocked. |
| E-wallet and card payments with webhooks and reconciliation | integration | L | Given a successful payment callback, When received, Then the booking is confirmed. | Given a missing callback, When daily reconciliation runs, Then mismatches are listed for finance. |
| Live rider tracking for customers | integration | XL | Given an active delivery, When the customer opens tracking, Then the rider position refreshes at least every 5 seconds. | Given the rider goes offline, When no update arrives for 60 seconds, Then the last known position is shown with a notice. |
| Order history and e-receipts | screen_crud | S | Given a completed delivery, When opened, Then the e-receipt shows fare, insurance and payment. | Given history, When filtered by month, Then matching orders are listed. |
| Ratings and feedback | screen_crud | S | Given a delivered order, When the customer rates the rider, Then the rating is stored. | Given a rating below 3, When submitted, Then a reason is required. |
| Promo codes and vouchers | admin_config | M | Given a promo with a usage limit, When the limit is reached, Then it is no longer accepted. | Given an expired promo, When applied, Then it is rejected. |

### Epic: Rider app

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Rider onboarding application with document upload and verification | workflow | L | Given a rider applicant, When licence, registration and clearances are uploaded, Then ops can verify each document. | Given an expired licence, When verified, Then the application is returned. |
| Rider training and exam records | screen_crud | M | Given a rider, When training modules and exam score are recorded, Then a pass unlocks activation. | Given a failed exam, When recorded, Then a retake date is required. |
| Job offer, accept/decline and delivery status updates with proof of delivery | workflow | L | Given a job offer, When the rider accepts within 30 seconds, Then the job is assigned. | Given delivery, When the rider captures a photo and recipient name, Then the job is completed. |
| Rider background location streaming | integration | XL | Given an on-duty rider, When the app is in the background, Then location is sent every 5 seconds. | Given poor signal, When reconnecting, Then buffered points are uploaded. |
| Rider availability and shift toggle | screen_crud | S | Given a rider goes off duty, When toggled, Then no new jobs are offered. | Given a shift end, When reached, Then the rider is prompted. |
| Rider earnings and payout statements | report_dashboard | M | Given a week, When opened, Then earnings per job, tips and deductions are shown. | Given a payout, When processed, Then the statement is final. |

### Epic: Dispatch and operations

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Automatic dispatch and matching engine | workflow | XL | Given a confirmed booking, When dispatch runs, Then the nearest available eligible rider is offered the job. | Given no acceptance in 30 seconds, When the offer expires, Then the next rider is offered. |
| 90-minute SLA monitoring with alerts and escalation | workflow | L | Given an active delivery, When 75 minutes elapse, Then ops receive an alert. | Given a breach, When 90 minutes pass, Then the delivery is flagged and escalated to the shift lead. |
| Live operations map | report_dashboard | XL | Given ops staff, When the map is open, Then all active riders and deliveries are shown with SLA colour. | Given a delivery, When clicked, Then its timeline opens. |
| Manual dispatch override and reassignment | workflow | M | Given a stuck delivery, When ops reassign it, Then the new rider is notified and history records the change. | Given a completed delivery, When reassignment is attempted, Then it is blocked. |
| Customer support tickets for delivery issues | workflow | M | Given a complaint, When logged, Then it is linked to the delivery and assigned. | Given resolution, When closed, Then the customer is notified. |
| Insurance claims filing and approval | workflow | M | Given a damaged-package claim with photos, When filed, Then it routes for approval. | Given approval, When granted, Then the payout amount is recorded. |
| Rider weekly payout disbursement | integration | L | Given approved earnings, When the payout batch runs, Then disbursements are sent to rider e-wallets. | Given a failed disbursement, When reported, Then it is retried next batch. |

### Epic: Administration and reporting

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Service zones and geofence management | admin_config | M | Given a polygon zone, When saved, Then bookings outside it are rejected. | Given a zone surcharge, When set, Then fares inside it include it. |
| Rate card configuration | admin_config | M | Given a new rate card with an effective date, When saved, Then quotes after that date use it. | Given overlapping cards, When saved, Then they are rejected. |
| Push and SMS notifications to customers and riders | notification | L | Given a status change, When it occurs, Then the customer receives a push notification, with SMS fallback. | Given notification preferences, When set, Then only chosen channels are used. |
| Rider performance and SLA reports | report_dashboard | M | Given a period, When run, Then on-time rate, acceptance rate and rating per rider are shown. | Given export, When clicked, Then a spreadsheet downloads. |
| Operations KPI dashboard | report_dashboard | L | Given management, When opened, Then deliveries, SLA compliance, revenue volume and active riders are shown. | Given a date range, When changed, Then widgets refresh. |
| Staff user and role administration | admin_config | M | Given an ops user, When assigned a role, Then menus follow the role. | Given deactivation, When saved, Then sessions end. |
| Audit trail | admin_config | M | Given a reassignment or payout change, When saved, Then user, time and change are logged. | Given filters, When applied, Then entries are listed. |
| Load and performance testing for live tracking | nfr | L | Given 3,000 concurrent tracked deliveries, When load-tested, Then position updates arrive within 2 seconds at the 95th percentile. | Given the test report, When reviewed, Then bottlenecks found are fixed and re-tested. |
| Observability: service-level objectives and alerting | nfr | M | Given dispatch and payment services, When error rate exceeds the SLO, Then on-call is alerted. | Given a trace ID, When searched, Then the full request path across services is shown. |

### Epic: Customer app extensions

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Saved addresses and favourites | screen_crud | S | Given a frequent address, When saved, Then it is selectable on the next booking. | Given a saved address outside the zone, When selected, Then it is flagged. |
| Multi-stop deliveries | workflow | L | Given a booking with up to 5 drop-offs, When quoted, Then the fare covers the optimized route. | Given one stop fails, When reported, Then the remaining stops continue and the failed one is returned. |
| Cancellation and refund handling | workflow | M | Given a cancellation before rider pickup, When requested, Then the payment is refunded in full. | Given a cancellation after pickup, When requested, Then the cancellation fee rule applies. |
| In-app chat between customer and rider | integration | L | Given an active delivery, When either party sends a message, Then it is delivered in real time with masked phone numbers. | Given delivery completion, When closed, Then the chat becomes read-only. |

### Epic: Business accounts

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Business accounts with monthly invoicing | workflow | L | Given an approved business account, When staff book deliveries, Then charges accrue to the account instead of card payment. | Given a credit limit, When exceeded, Then new bookings are blocked. |
| Corporate billing statements | report_dashboard | M | Given a month end, When statements are generated, Then each business account receives itemized deliveries and totals. | Given a disputed line, When flagged, Then it is excluded pending review. |
| Scheduled and recurring bookings | workflow | M | Given a weekly recurring pickup, When created, Then bookings are generated ahead of each date. | Given a holiday, When a recurrence falls on it, Then the customer is asked to confirm. |
| Partner merchant API for bulk bookings | integration | L | Given a merchant API key, When a batch of orders is posted, Then each order gets a tracking number. | Given an invalid address in the batch, When posted, Then that order is rejected with an error and the rest proceed. |
| Referral program | admin_config | M | Given a referral code, When a new customer completes a first delivery, Then both receive credits. | Given self-referral, When detected, Then credits are not issued. |

### Epic: Rider management and risk

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Rider incentive and bonus rules | workflow | L | Given a peak-hour bonus rule, When a rider completes qualifying jobs, Then the bonus is added to earnings. | Given overlapping rules, When evaluated, Then only the highest applies. |
| Rider document expiry reminders | notification | S | Given a licence expiring in 30 days, When the daily job runs, Then the rider is reminded. | Given an expired document, When reached, Then the rider is set off duty. |
| Rider suspension and deactivation workflow | workflow | M | Given repeated complaints, When ops suspend a rider, Then no jobs are offered and the reason is recorded. | Given an appeal, When approved, Then the rider is reinstated. |
| Fraud detection rules for bookings and cancellations | workflow | L | Given repeated cancel-after-pickup patterns, When detected, Then the account is flagged for review. | Given a flagged account, When it books, Then prepayment is required. |
| Insurance partner policy issuance integration | integration | L | Given a booking with insurance, When confirmed, Then a policy is issued through the insurer's API and its number stored. | Given the insurer API is down, When issuing fails, Then it is retried and ops are alerted. |
| Demand heat map and surge pricing | workflow | L | Given demand above supply in a zone, When the surge rule triggers, Then quotes in that zone include the surge factor. | Given a surge cap, When reached, Then the factor does not increase further. |

## Assumptions

- Default architecture: cloud-deployed microservices; cloud and license costs are excluded and handled in the costing sheet.
- Native mobile apps for customers and riders built with one cross-platform framework.
- Map, SMS, e-wallet and insurance-partner APIs have sandboxes.
- Estimate covers the development phase only; UAT support and warranty not included.

## Exclusions

- Rider phones, helmets and delivery boxes.
- Map API, SMS and payment transaction fees.
- Insurance underwriting (provided by an insurance partner).
