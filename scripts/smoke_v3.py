"""The plan's smoke test (POC v3 section 13), in a real headless browser: one full HRIS run through Contract-Ready.

Usage: python scripts/smoke_v3.py <url> <password> [output folder]
Needs Playwright and Chrome, which are deliberately not in requirements.txt: the image does not need them. Creates a new sample opportunity each run;
it never opens an existing one. Exits 1 if a step fails. Prints no document text.
"""
from __future__ import annotations

import io
import re
import sys
import time
from pathlib import Path

from docx import Document
from playwright.sync_api import sync_playwright

BDM, SPECIALIST, LEGAL = "BDM", "Solutions Specialist", "Legal/Management Reviewer"
CONTRACT = {"Client address": "1 Fictional Road, Manila", "Client's representative": "Fictional Representative", "Representative's designation": "HR Director", "Effective date": "2027-03-01"}
results: list[tuple[str, bool]] = []


def check(name: str, ok) -> None:
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name, flush=True)


class Smoke:
    def __init__(self, page, out: Path):
        self.page, self.out = page, out

    def settle(self, ms: int = 1500) -> None:
        """Waits until the app has stopped working and stays stopped: on a small container a redraw can take many seconds."""
        self.page.wait_for_timeout(ms)
        quiet = 0
        for _ in range(240):
            busy = self.page.locator('[data-testid="stStatusWidget"], [data-testid="stToolbarActions"] button:has-text("Stop"), button:has-text("Stop")').count() > 0
            quiet = 0 if busy else quiet + 1
            if quiet >= 3:
                return
            self.page.wait_for_timeout(1000)

    def inner_tab(self, name: str) -> None:
        self.settle(500)
        self.page.get_by_role("tab", name=name, exact=True).first.click()
        self.page.wait_for_timeout(700)

    def button(self, name):
        return self.page.get_by_role("button", name=name)

    def click(self, name, ms: int = 2500) -> None:
        self.button(name).first.click()
        self.settle(ms)

    def pick(self, label: str, name: str) -> None:
        self.page.get_by_role("combobox", name=re.compile(label)).first.click()
        self.page.wait_for_timeout(500)
        self.page.get_by_role("option", name=name, exact=True).click()

    def role(self, name: str) -> None:
        self.pick("Switch role", name)
        self.settle()

    def step(self, name: str) -> None:
        self.click(re.compile("^" + re.escape(name)), 1500)

    def status(self, text: str) -> bool:
        return self.page.get_by_text(re.compile("Where you left off: " + re.escape(text))).count() > 0

    def clean(self, where: str) -> None:
        check(f"no exception: {where}", self.page.get_by_test_id("stException").count() == 0)

    def shot(self, name: str) -> None:
        self.page.screenshot(path=str(self.out / f"{name}.png"), full_page=True)

    def wait_status(self, text: str, seconds: int = 180) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            if self.status(text):
                return True
            self.page.wait_for_timeout(2000)
        return False

    def download(self, name) -> bytes:
        with self.page.expect_download() as dl:
            self.button(name).first.click()
        return Path(dl.value.path()).read_bytes()

    def accept_all_and_approve_brd(self) -> None:
        if self.button("Accept all remaining").count():
            self.click("Accept all remaining")
        if self.button("Approve BRD").first.is_disabled():
            self.page.get_by_role("tab", name="Open questions").click()     # a button on a hidden tab cannot be seen
            self.page.wait_for_timeout(500)
            self.click("Acknowledge")
        self.click("Approve BRD", 4000)

    def run(self, password: str) -> None:
        p = self.page
        self.settle(5000)
        check("nothing shown before the password", self.button("Create sample HRIS opportunity").count() == 0)
        p.get_by_label("Password", exact=True).fill("definitely wrong")
        self.click("Enter")
        check("a wrong password is refused", p.get_by_text("Wrong password").count() > 0)
        p.get_by_label("Password", exact=True).fill(password)
        self.click("Enter")
        p.get_by_label("Your name").fill("Smoke Test")
        self.click("Continue")
        check("signed in with a self-declared role", p.get_by_text("self-declared (POC)").count() > 0)

        # 1. BDM creates the sample opportunity
        self.click("Create sample HRIS opportunity", 4000)
        check("1. sample created, draft BRD generated", self.wait_status("BRD Generated"))
        self.settle(1000)
        self.clean("BRD")

        # 2. Solutions Specialist validates, edits a statement in the file, re-uploads, approves
        self.role(SPECIALIST)
        self.click("Start validation")
        p.get_by_text("Re-upload an edited BRD").click()
        self.settle(800)
        data = self.download(re.compile(r"^Download BRD-"))
        doc = Document(io.BytesIO(data))
        table = next(t for t in doc.tables if t.rows[0].cells[0].text == "BR ID (do not edit)")
        cell = next(r for r in table.rows if r.cells[0].text == "BR-003").cells[3]
        cell.text = cell.text + " Managers can export the list."
        edited = self.out / "edited_brd.docx"
        doc.save(edited)
        p.get_by_test_id("stFileUploader").filter(has_text="Edited BRD").locator("input[type=file]").set_input_files(str(edited))
        self.settle(3000)
        check("2. the re-upload shows the diff", p.get_by_text("Changes, side by side").count() > 0)
        self.click(re.compile("^Apply$"), 3000)
        check("2. applied as v0.2", p.get_by_text("BRD v0.2").count() > 0)
        self.accept_all_and_approve_brd()
        check("2. BRD approved v1.0", self.status("BRD Approved"))
        self.clean("BRD approved")

        # an estimate now, so the bulletin can make it Outdated
        self.click("Generate estimates and timeline", 5000)
        check("estimate generated", self.status("Estimate and Timeline Generated"))

        # 3. a bid bulletin -> change request -> v1.1 -> re-approve v2.0 -> estimate Outdated
        self.role(BDM)
        self.step("1 · Intake")
        self.click("Add sample bid bulletin", 3000)
        check("3. the bulletin opens a change request", p.get_by_text(re.compile("Change request from Bid Bulletin")).count() > 0)
        self.role(SPECIALIST)
        self.click(re.compile("^Write merged statements"), 6000)
        check("3. one clean merged statement", p.get_by_text("Needs rewording").count() == 0)
        accept = self.page.get_by_role("button", name="Accept", exact=True)
        while accept.count():
            accept.first.click()
            self.settle(2000)
        self.click("Apply the accepted changes", 4000)
        check("3. applied as v1.1", p.get_by_text("BRD v1.1").count() > 0)
        self.shot("smoke_brd_v1_1")
        self.accept_all_and_approve_brd()
        check("3. re-approved v2.0", self.status("BRD Approved"))
        check("3. the estimate is Outdated", p.get_by_text("Outdated").count() > 0)
        self.clean("BRD v2.0")

        # 4. estimate updated -> edit one size -> approve
        self.click("Generate estimates and timeline", 5000)
        check("4. estimate delta shown", p.get_by_text(re.compile("What changed from BRD v1.0 to v2.0")).count() > 0)
        panel = p.get_by_role("tabpanel", name="Work breakdown structure")
        grid = panel.get_by_test_id("stDataFrame").first
        grid.scroll_into_view_if_needed()
        box = grid.bounding_box()
        size_x = box["x"] + 510
        p.mouse.move(size_x, box["y"] + 53)
        p.wait_for_timeout(300)
        p.mouse.click(size_x, box["y"] + 53)
        p.wait_for_timeout(500)
        p.keyboard.press("Enter")
        p.wait_for_timeout(700)
        p.keyboard.type("XL")
        p.wait_for_timeout(400)
        p.keyboard.press("Enter")
        self.settle(3000)
        self.shot("smoke_size_edit")
        check("4. one size edited", self.button(re.compile(r"Save size changes \(1 unsaved\)")).count() > 0)
        self.click(re.compile(r"Save size changes \(1 unsaved\)"), 4000)
        p.get_by_text(re.compile(r"Estimate v0\.3")).first.wait_for(timeout=180000)
        self.clean("estimate edit")
        self.inner_tab("Validate")
        if self.button("Start validation").count():
            self.click("Start validation", 3000)
            self.inner_tab("Validate")
        if self.button("Validate all remaining rows").count():
            self.click("Validate all remaining rows", 3000)
            self.inner_tab("Validate")
        for label in ("Effort and complexity", "Resource allocation", "Dependencies and sequencing", "AI productivity assumptions", "Integration, testing and contingency effort", "Delivery feasibility", "Cost, pricing and target margin"):
            p.get_by_text(label, exact=True).click()
            self.settle(2000)
            self.inner_tab("Validate")
        self.click("Approve estimate", 4000)
        check("4. estimate approved", self.status("Estimate Approved"))

        # 5. BDM triggers the proposal -> approve -> submitted
        self.role(BDM)
        for _ in range(8):
            box_ = p.get_by_role("combobox", name=re.compile("Acknowledge every")).first
            box_.click()
            p.wait_for_timeout(500)
            if p.get_by_role("option").filter(has_not_text="No results").count() == 0:
                p.keyboard.press("Escape")
                break
            p.keyboard.press("ArrowDown")
            p.wait_for_timeout(150)
            p.keyboard.press("Enter")
            p.wait_for_timeout(300)
            p.keyboard.press("Escape")
            self.settle(1800)
        self.click("Generate proposal", 4000)
        check("5. proposal generated", self.wait_status("Proposal Generated", 240))
        self.settle(1000)
        check("5. leak and consistency checks passed", p.get_by_text("leak check and consistency check passed").count() > 0)
        client = Document(io.BytesIO(self.download("Download client copy")))
        text = "\n".join(x.text for x in client.paragraphs)
        check("5. the client copy has no internal questions or management section", "Terms Requiring Management Review" not in text and "Internal Open Questions" not in text and "no KPIs" not in text)
        check("5. client questions are clarification requests", "Please confirm" in text)
        self.click("Start the proposal review", 3000)
        self.click("Approve proposal", 3000)
        self.role(LEGAL)
        self.click("Approve proposal", 4000)
        self.role(BDM)
        self.date("Submission date", "20270115")
        self.click("Mark as submitted", 3000)
        check("5. submitted", self.status("Proposal Submitted"))

        # 6. award
        self.date("Decision date", "20270201")
        self.pick("Outcome", "Awarded")
        p.wait_for_timeout(500)
        self.click("Record the decision", 3000)
        check("6. awarded", self.status("Awarded"))

        # 7. SOW + MSA
        for label, value in CONTRACT.items():
            p.get_by_label(label, exact=True).fill(value)
        self.click("Save contract details", 3000)
        self.click("Generate SOW and MSA", 5000)
        check("7. SOW generated", self.status("SOW and MSA Generated") and p.get_by_text(re.compile("SOW v0.1")).count() > 0)
        msa_ready = p.get_by_text(re.compile("MSA v0.1")).count() > 0
        check("7. MSA generated from the private template", msa_ready)
        self.clean("contracts")
        self.shot("smoke_contracts")
        if not msa_ready:
            print("MSA not available: stopping before the final approvals.", flush=True)
            return

        # 8. three approvals -> Contract-Ready
        self.role(LEGAL)
        self.click("Start the legal and management review", 3000)
        check("8. review started", self.status("Legal and Management Review"))
        clauses = p.get_by_test_id("stCheckbox").filter(has=p.locator("input:not(:checked)"))
        while clauses.count():
            clauses.first.locator("label").first.click()
            self.settle(1800)
        self.shot("smoke_final")
        enabled_approve = p.get_by_role("button", name="Approve", exact=True, disabled=False)      # each role has its own
        enabled_approve.first.click()
        self.settle(3000)
        check("8. Legal approved", p.get_by_text(re.compile(r"· approved by Smoke Test")).count() == 1)
        self.role(SPECIALIST)
        enabled_approve.first.click()
        self.settle(3000)
        self.role(BDM)
        enabled_approve.first.click()
        self.settle(5000)
        check("8. Contract-Ready", self.status("Contract-Ready"))
        self.clean("Contract-Ready")
        self.shot("smoke_contract_ready")

        # the audit trail shows every step
        p.get_by_role("tab", name="Audit trail", exact=True).click()
        self.settle(800)
        self.shot("smoke_audit")
        p.get_by_role("tab", name="Versions", exact=True).click()
        self.settle(800)
        self.shot("smoke_versions")
        check("versions listed", p.get_by_text(re.compile("BRD v2.0")).count() > 0 and p.get_by_text(re.compile("MSA v1.0")).count() > 0)
        check("nothing waiting to be published", p.get_by_text(re.compile("waiting to be published")).count() == 0)

    def date(self, label: str, digits: str) -> None:
        box = self.page.get_by_test_id("stDateInput").filter(has_text=label)
        box.get_by_role("spinbutton").first.click()
        self.page.wait_for_timeout(300)
        self.page.keyboard.type(digits, delay=60)
        self.page.wait_for_timeout(300)
        self.page.keyboard.press("Escape")
        self.settle(2000)


def main() -> int:
    url, password = sys.argv[1], sys.argv[2]
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(".")
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        page = browser.new_context(viewport={"width": 1500, "height": 1500}, accept_downloads=True).new_page()
        page.set_default_timeout(180000)
        page.goto(url, timeout=180000)
        smoke = Smoke(page, out)
        try:
            smoke.run(password)
        except Exception as e:  # noqa: BLE001 — report where it stopped
            waiting = next((line.strip() for line in str(e).splitlines() if "waiting for" in line), "")
            check(f"stopped: {type(e).__name__}: {waiting[:200]}", False)
            smoke.shot("smoke_failure")
        browser.close()
    failed = [n for n, ok in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} passed; failed: {failed}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
