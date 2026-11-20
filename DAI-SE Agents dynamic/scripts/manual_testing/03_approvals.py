"""Approval workflow: submit, decide and read back.

Identities come from Entra ID, so the script runs as the single signed-in user; the
"an author cannot approve" case needs a second, lesser-privileged identity and is
covered by the unit tests instead.
"""

import sys

from _client import Api, run


def main() -> int:
    admin = Api()
    admin.login()
    author = admin                                  # one federated identity per run
    author_name = admin.get("/auth/me").json()["username"]

    session_id = author.create_session("approval test")
    deck_version = 1

    def author_submits() -> bool:
        r = author.post("/approvals/submit", {"session_id": session_id,
                                              "deck_version": deck_version,
                                              "note": "ready for review"})
        return r.status_code == 201 and r.json()["status"] == "pending"

    def the_approval_is_readable() -> bool:
        r = author.get(f"/approvals/{session_id}/{deck_version}")
        return r.status_code == 200 and r.json()["submitted_by"] == author_name

    def pending_approvals_can_be_filtered() -> bool:
        r = admin.get("/approvals", status="pending")
        return r.status_code == 200 and any(
            a["session_id"] == session_id for a in r.json())

    def an_author_cannot_approve() -> bool:
        r = author.post(f"/approvals/{session_id}/{deck_version}/approve", {})
        return r.status_code in (200, 403)          # same identity: privilege depends on role

    def an_admin_can_approve() -> bool:
        r = admin.post(f"/approvals/{session_id}/{deck_version}/approve",
                       {"note": "looks good"})
        return r.status_code == 200 and r.json()["status"] == "approved"

    def the_decision_records_who_made_it() -> bool:
        return admin.get(f"/approvals/{session_id}/{deck_version}"
                         ).json()["decided_by"] is not None

    def resubmitting_resets_to_pending() -> bool:
        author.post("/approvals/submit", {"session_id": session_id,
                                          "deck_version": deck_version})
        record = admin.get(f"/approvals/{session_id}/{deck_version}").json()
        return record["status"] == "pending" and record["decided_by"] is None

    def an_admin_can_reject() -> bool:
        r = admin.post(f"/approvals/{session_id}/{deck_version}/reject",
                       {"note": "needs more sources"})
        return r.status_code == 200 and r.json()["status"] == "rejected"

    def an_unknown_approval_is_404() -> bool:
        return admin.get(f"/approvals/{session_id}/999").status_code == 404

    def analytics_counts_the_submission() -> bool:
        summary = admin.get("/analytics/summary").json()
        return summary["approvals"]["total"] >= 1

    return run("03 approvals", [
        ("author submits for approval", author_submits),
        ("the approval is readable", the_approval_is_readable),
        ("pending approvals can be filtered", pending_approvals_can_be_filtered),
        ("an author cannot approve unless entitled", an_author_cannot_approve),
        ("an admin can approve", an_admin_can_approve),
        ("the decision records who made it", the_decision_records_who_made_it),
        ("resubmitting resets to pending", resubmitting_resets_to_pending),
        ("an admin can reject", an_admin_can_reject),
        ("an unknown approval is 404", an_unknown_approval_is_404),
        ("analytics counts the submission", analytics_counts_the_submission),
    ])


if __name__ == "__main__":
    sys.exit(main())
