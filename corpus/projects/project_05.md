---
doc_id: project_05
doc_type: project
project_id: P05
domain: inventory_warehouse
client_type: Regional consumer-goods distributor (fictional)
title: Inventory and Warehouse Management System
project_size: Mid
months: 6
---

# P05 — Inventory and Warehouse Management System

## TOR summary

A fictional consumer-goods distributor with three warehouses and its own delivery trucks wanted a web and mobile warehouse system. Scope covered item and supplier masters, purchase requisitions and orders with approval, goods receipt with lot and expiry, putaway, stock transfers, cycle counts, order intake from an e-commerce channel, wave picking, packing, dispatch, proof of delivery, customer returns, replenishment, kitting, and reports. Warehouse staff use a mobile scanning app that works offline. Stock and receipts are exchanged with the existing ERP. Delivered in 6 months by a Mid team (PM 1, TA 0.5, Dev 4, UI 1, BA 1, QA 2), development phase only.

## Foundation

Cloud-deployed microservices (catalog, inbound, inventory, outbound, integration, reporting) behind an API gateway; infrastructure-as-code for dev/UAT/prod; CI/CD per service; staff identity with RBAC scoped by warehouse; central logging and monitoring; core data model.

## Epics and features

### Epic: Master data

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Item master with units of measure and barcodes | screen_crud | M | Given a new item, When saved, Then SKU, base unit, conversions and barcode are required. | Given an item with stock, When deactivation is attempted, Then it is blocked. |
| Supplier master maintenance | screen_crud | M | Given a supplier, When saved with terms and contacts, Then it is selectable on purchase orders. | Given a blacklisted supplier, When ordering, Then it is not selectable. |
| Warehouse and bin location maintenance | admin_config | S | Given a warehouse, When bins are added, Then each bin code is unique within the warehouse. | Given an inactive bin, When putaway runs, Then it is not suggested. |
| Reason codes and reference data | admin_config | S | Given an adjustment reason, When deactivated, Then it is hidden from new adjustments. | Given a change, When saved, Then it is audit-logged. |

### Epic: Inbound

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Purchase requisition and purchase order with two-level approval | workflow | M | Given a requisition above the threshold, When the supervisor approves, Then it routes to the purchasing head. | Given an approved requisition, When converted, Then a purchase order is created. |
| Purchase order import from ERP via nightly file | integration | M | Given the nightly file, When imported, Then new POs are created and duplicates skipped by PO number. | Given a malformed row, When imported, Then it is logged and the rest continues. |
| Goods receipt against purchase order with lot and expiry | workflow | L | Given a PO, When goods are received, Then quantities, lot and expiry are recorded and stock increases. | Given over-receipt above 5%, When posted, Then supervisor approval is required. |
| Putaway with bin suggestion | workflow | L | Given received pallets, When putaway starts, Then bins are suggested by item class and free capacity. | Given a scanned bin different from the suggestion, When confirmed, Then the actual bin is recorded. |
| Quarantine and damaged-stock handling | workflow | M | Given damaged goods, When quarantined, Then they are excluded from available stock. | Given release approval, When granted, Then stock returns to available. |
| Barcode and pallet label printing | report_dashboard | M | Given a receipt, When labels are printed, Then each pallet label carries item, lot, expiry and barcode. | Given a reprint, When requested, Then it is logged. |

### Epic: Inventory control

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Inter-warehouse stock transfer | workflow | L | Given a transfer, When dispatched, Then stock moves to in-transit; When received, Then destination stock increases. | Given a short receipt, When posted, Then the variance is recorded. |
| ABC classification and cycle count scheduling | workflow | M | Given item movement history, When classification runs, Then items are ranked A, B, C and count frequency is assigned. | Given a due count, When the day arrives, Then a count task is generated. |
| Cycle count with adjustment approval | workflow | M | Given counted quantities that differ, When submitted, Then an adjustment awaits warehouse manager approval. | Given approval, When granted, Then balances update with the reason. |
| Stock reservation for orders | workflow | M | Given a confirmed order, When reserved, Then available-to-promise decreases. | Given a cancelled order, When cancelled, Then the reservation is released. |
| Replenishment suggestions (min/max) | workflow | L | Given a pick-face below minimum, When the job runs, Then a replenishment task is created from reserve stock. | Given no reserve stock, When evaluated, Then a purchase suggestion is raised. |
| Kitting and assembly orders | workflow | L | Given a kit bill of materials, When a kitting order completes, Then components decrease and kits increase. | Given insufficient components, When started, Then the order is blocked. |
| Lot recall and traceability | report_dashboard | M | Given a lot number, When traced, Then all receipts, movements and customer deliveries are listed. | Given a recall, When initiated, Then remaining stock of the lot is quarantined. |

### Epic: Outbound

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Order intake from e-commerce channel API | integration | L | Given a new web order, When received via the channel API, Then a warehouse order is created with the channel reference. | Given an unknown SKU, When received, Then the order is held with an error. |
| Wave picking with first-expiry-first-out | workflow | L | Given orders in a wave, When pick lists are generated, Then lots are suggested first-expiry-first-out and picks are batched by zone. | Given short stock, When generating, Then short lines are flagged. |
| Pack station with weight check | workflow | M | Given a packed carton, When weighed, Then weight outside tolerance blocks closing. | Given closing, When confirmed, Then a packing list prints. |
| Cross-docking | workflow | L | Given an inbound line allocated to an outbound order, When received, Then it goes directly to the dispatch staging area. | Given a partial receipt, When posted, Then the remainder follows the normal flow. |
| Dispatch and delivery receipt printing | report_dashboard | M | Given a packed order, When dispatched, Then a delivery receipt PDF prints with lot numbers. | Given dispatch, When posted, Then stock decreases. |
| Delivery route planning for own trucks | workflow | L | Given the day's deliveries, When planned, Then stops are grouped per truck within capacity. | Given a route, When published, Then drivers see stops in sequence. |
| Proof of delivery capture | screen_crud | M | Given a delivery, When the driver captures signature and photo, Then the order is marked delivered. | Given a refused delivery, When recorded, Then a return is created. |
| Customer returns processing | workflow | L | Given a return authorization, When goods are received, Then they are inspected and restocked or quarantined. | Given a restock, When posted, Then the credit request goes to finance. |

### Epic: Mobile and integrations

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Offline-capable mobile scanning app for receiving, putaway, picking and counts | screen_crud | XL | Given no network, When a picker scans, Then transactions are queued and synced when online. | Given a sync conflict, When detected, Then the supervisor resolves it. |
| Stock and receipt posting back to ERP via API | integration | L | Given a posted receipt, When the integration runs, Then the ERP receives quantities and lots. | Given ERP rejection, When returned, Then the posting is flagged for retry. |
| Shipping label integration with third-party courier | integration | M | Given a parcel order, When booked with the courier API, Then a shipping label and tracking number are returned. | Given courier API failure, When booking fails, Then it is retried. |

### Epic: Alerts, reporting and administration

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Low-stock and near-expiry email alerts | notification | S | Given an item below reorder level, When the daily job runs, Then purchasing receives an email. | Given lots expiring in 30 days, When the job runs, Then they are listed. |
| Order status emails to customers | notification | S | Given dispatch, When posted, Then the customer receives the delivery reference by email. | Given delivery, When confirmed, Then a delivered email is sent. |
| Stock on hand report by warehouse | report_dashboard | S | Given a warehouse filter, When run, Then items with quantities by lot are shown and exportable. | Given an as-of date, When chosen, Then balances reflect that date. |
| Stock movement ledger | report_dashboard | M | Given an item and date range, When run, Then all movements with running balance are shown. | Given a movement, When clicked, Then the source document opens. |
| Inventory valuation report (weighted average) | report_dashboard | M | Given a month end, When run, Then stock value per warehouse is computed at weighted average cost. | Given ERP cost updates, When imported, Then valuation uses them. |
| Inventory dashboard | report_dashboard | L | Given management, When opened, Then stock value, turnover, fill rate and near-expiry counts are shown. | Given a warehouse selection, When changed, Then widgets refresh. |
| User and role administration by warehouse | admin_config | M | Given a user, When assigned to a warehouse, Then they only see that warehouse's transactions. | Given role changes, When saved, Then they are audit-logged. |
| Audit trail | admin_config | M | Given any stock adjustment, When posted, Then user, time and before/after quantities are logged. | Given filters, When applied, Then entries are listed. |
| Opening balance load from physical count | data_migration | M | Given the count spreadsheet, When loaded, Then balances per item, lot and bin are created. | Given unknown SKUs, When loaded, Then they are rejected with a report. |
| Backup, restore and disaster recovery setup | nfr | M | Given daily backups, When a restore test runs, Then data loss is within the 4-hour RPO. | Given the DR runbook, When the drill runs, Then service is restored within the 8-hour RTO. |

### Epic: Advanced warehousing

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Consignment stock by owner | workflow | L | Given stock owned by a supplier, When received, Then it is tracked separately and excluded from company valuation. | Given consumption, When posted, Then a settlement record is created for the owner. |
| Serial number tracking | workflow | L | Given a serialized item, When received or picked, Then each serial number is scanned and recorded. | Given a duplicate serial, When scanned, Then it is rejected. |
| Supplier portal for vendor-managed inventory | screen_crud | L | Given a supplier user, When logged in, Then they see stock levels and open orders for their items only. | Given a replenishment proposal, When the supplier submits it, Then it awaits purchasing approval. |
| Advance shipping notices from suppliers | integration | M | Given an ASN file from a supplier, When received, Then an expected receipt is created with lots and quantities. | Given a receipt that differs from the ASN, When posted, Then the variance is reported. |
| Dock appointment scheduling | workflow | M | Given dock capacity per slot, When a supplier books a delivery, Then the slot is reserved. | Given a full slot, When booking, Then it is unavailable. |
| Yard and trailer management | screen_crud | M | Given a trailer at the gate, When checked in, Then its location in the yard and contents are recorded. | Given a trailer waiting over 4 hours, When detected, Then the supervisor is alerted. |
| Cold-zone temperature sensor monitoring and alerts | integration | L | Given sensor readings every 5 minutes, When a reading exceeds the limit, Then the warehouse manager is alerted. | Given a sensor offline for 15 minutes, When detected, Then an alert is raised. |
| Pick-path optimization | workflow | L | Given a pick list, When generated, Then lines are sequenced by bin walk order. | Given a blocked aisle, When configured, Then routes avoid it. |
| Slotting optimization by velocity | workflow | XL | Given 90 days of movement history, When slotting runs, Then fast movers are proposed for pick faces nearest dispatch. | Given an approved proposal, When executed, Then move tasks are generated. |
| Full physical inventory with stock freeze | workflow | M | Given a scheduled physical count, When started, Then movements in the counted zone are frozen. | Given count completion, When approved, Then variances post and the freeze lifts. |
| Return to vendor | workflow | M | Given defective stock, When a return to vendor is approved, Then stock is issued with the supplier's authorization number. | Given the supplier credit, When received, Then the return closes. |
| Carrier damage claims | workflow | M | Given goods damaged in transit, When a claim is filed with photos, Then it is tracked to settlement. | Given a claim past its deadline, When detected, Then it is flagged. |

### Epic: Customer and supplier insights

| Feature | Type | Size | Acceptance criterion 1 | Acceptance criterion 2 |
|---|---|---|---|---|
| Customer order-tracking portal | screen_crud | M | Given a customer user, When logged in, Then they see their orders' status and delivery proof. | Given another customer's order number, When searched, Then nothing is shown. |
| Warehouse labour productivity report | report_dashboard | M | Given a shift, When run, Then lines picked and putaways per staff member are shown. | Given a date range, When compared, Then trends are charted. |
| Supplier performance scorecard | report_dashboard | M | Given a quarter, When run, Then on-time delivery, ASN accuracy and damage rate per supplier are shown. | Given export, When clicked, Then a spreadsheet downloads. |
| Stock aging report | report_dashboard | S | Given an as-of date, When run, Then stock is bucketed by age since receipt. | Given a warehouse filter, When applied, Then only its stock is shown. |

## Assumptions

- Default architecture: cloud-deployed microservices; cloud and license costs are excluded and handled in the costing sheet.
- ERP and e-commerce channel APIs are documented with test environments.
- Mobile scanning app runs on Android handhelds.
- Estimate covers the development phase only; UAT support and warranty not included.

## Exclusions

- Handheld scanners, label printers and racking.
- ERP changes and licenses.
- Courier service fees.
