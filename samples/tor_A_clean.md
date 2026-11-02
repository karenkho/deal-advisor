<!-- page 1 -->
# Terms of Reference: Online School Enrollment System

**Synthetic document for a proof of concept. The school, people and figures are fictional.**

## 1. Background

A private basic-education school (Kindergarten to Grade 12) with about 2,400 students currently runs enrollment on paper forms and spreadsheets. Each school year, parents queue at the registrar's office for several days, documents are checked by hand, and section lists are prepared in spreadsheets. The school wants an online enrollment system that parents can use from home and that gives the registrar, the accounting office and school administrators one shared view of enrollment.

## 2. Objectives and scope

The school is procuring the design, development, testing and deployment of a web-based, mobile-responsive enrollment system. The system shall:

- let parents and guardians register, apply for new students and re-enroll returning students online;
- let the registrar review applications and documents, and assign students to sections;
- compute tuition and fees, and accept online payments;
- notify parents at each step and issue a certificate of enrollment;
- give administrators reports and a dashboard of enrollment status.

Users: parents/guardians (about 2,000 households), registrar staff (4), accounting staff (3), school administrators (5) and an IT administrator (1).

Out of scope: the student information system for grades and attendance, the learning management system, and the school's accounting system. The school will supply all computers, printers and scanners. Payment gateway transaction fees and SMS charges are paid by the school directly to the providers.

<!-- page 2 -->
## 3. Functional requirements

### 3.1 Parent/guardian accounts

Parents and guardians shall create an account with their name, email address and mobile number. The system shall send an email verification link, and the account shall stay inactive until it is verified. A parent may link several children to one account. Parents shall be able to reset their own password.

### 3.2 New student enrollment application

For a new student, the parent shall complete an online application with the student's personal details, previous school, grade level applied for, and emergency contacts. The application shall be saveable as a draft and submitted when complete. The system shall check that all required fields are completed and that the student's age matches the grade level rules maintained by the registrar.

### 3.3 Returning student re-enrollment

For a returning student, the system shall prefill the application from the previous school year so that the parent only confirms or updates the details and selects the next grade level.

### 3.4 Requirements documents

Each grade level has a list of required documents (for example, birth certificate, report card, certificate of good moral character). The parent shall upload each document as PDF or image (maximum 10 MB per file). The system shall show a checklist of what is still missing and shall allow submission only when all required documents are uploaded.

<!-- page 3 -->
### 3.5 Registrar review

The registrar shall review each submitted application and its documents, and either approve it or return it to the parent with remarks for correction. A returned application can be corrected and resubmitted. The history of review actions shall be kept.

### 3.6 Section assignment

After approval, the registrar shall assign each student to a section of the grade level. Each section has a maximum capacity. The system shall suggest a section with available seats, prevent assignment beyond capacity, and allow the registrar to move a student between sections with a recorded reason.

### 3.7 Tuition and fee assessment

The system shall compute the assessment for each enrolled student from the fee table of the grade level (tuition, miscellaneous fees, other fees). It shall apply discounts maintained by the accounting office: sibling discount, full-payment discount, and scholarship grants as a percentage. Parents choose a payment plan (full, semestral, quarterly or monthly) and the system shall compute the schedule of due amounts.

### 3.8 Online payment

Parents shall pay the down payment or full amount online through a payment gateway (card and e-wallet). The system shall mark the assessment paid when the gateway confirms payment and shall issue an acknowledgment receipt number. The accounting office shall run a daily reconciliation that lists payments recorded by the gateway but not matched in the system, and the reverse.

### 3.9 Notifications

The system shall notify parents by email and SMS when an application is submitted, returned, approved, assessed, and paid, and when the student is officially enrolled.

<!-- page 4 -->
### 3.10 Certificate of enrollment

Once the student is officially enrolled (approved, assigned to a section, and down payment paid), the parent shall be able to download a certificate of enrollment as PDF with the school's official layout and a QR code. Scanning the QR code shall open a verification page showing whether the certificate is valid.

### 3.11 Class list report

The registrar shall be able to print the class list per section, sorted by surname, and export it to Excel.

### 3.12 Enrollment dashboard

School administrators shall see a dashboard showing applications by status, enrolled students per grade level and section, seats remaining, payments collected against assessments, and daily enrollment trend compared with the previous school year.

### 3.13 School setup

Administrators shall maintain school years, grade levels, sections and capacities, required documents per grade level, age rules, fee tables and discounts. Changes shall apply only to the selected school year.

### 3.14 Staff users and roles

The IT administrator shall create staff accounts and assign roles (registrar, accounting, administrator, IT administrator). Each role sees only its own menus and actions.

### 3.15 Audit trail

The system shall record every create, update, approval and payment action with user, date and time, and changed values. Administrators shall be able to search the audit trail by user, student and date.

### 3.16 Existing student records

The current student master list (about 2,400 active students and 6 previous school years) is kept in spreadsheets. The vendor shall migrate active students and their latest enrollment data into the system before the start of online enrollment, with a validation report of rejected rows. The school will clean the spreadsheets based on that report.

<!-- page 5 -->
## 4. Non-functional requirements

- Availability of 99.5% during the enrollment period (May to August).
- Pages shall load within 3 seconds for 300 concurrent parents.
- Personal data shall be protected in line with the Data Privacy Act of 2012: consent is captured at account registration, and access is limited by role.
- The system shall work on current versions of common browsers on desktop and mobile.

## 5. Deliverables and timeline

1. Inception report and confirmed backlog (week 2).
2. Working increments every two weeks, demonstrated to the school.
3. Data migration rehearsal and final migration.
4. User acceptance testing by the school with vendor support.
5. Deployment to production before the enrollment period, user guides, and handover.

The school's IT administrator will be the product owner and will provide decisions within three working days.

## 6. Payment gateway and SMS

The school has not yet chosen its payment gateway or SMS provider. The vendor shall propose options; the school will sign the provider contracts.
