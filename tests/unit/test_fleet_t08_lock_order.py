"""The final worker transaction shares admission/declaration lock ordering."""
from sqlalchemy import event

from tests.unit import test_application_form_review_worker as worker_fixture
from workers.application_form_review import handle_form_review


def test_worker_takes_identity_locks_before_operation_lock():
    fixture = worker_fixture.ApplicationFormReviewWorkerTests()
    fixture.setUp()
    locked = []

    def record_locks(connection, clause, multiparams, params, options):
        if getattr(clause, "_for_update_arg", None) is not None:
            locked.extend(table.name for table in clause.get_final_froms())

    try:
        operation = fixture.make_operation(profile_version=str(fixture.store.profile("fixture")["version"]))
        event.listen(fixture.store.engine, "before_execute", record_locks)
        service, _ = fixture.with_analysis_service()
        with service:
            handle_form_review(fixture.store, operation, fetch_html=lambda _: "<form/>", check=lambda: None)
        assert locked[:4] == [
            "applications", "career_profiles", "application_routes", "worker_operations",
        ]
    finally:
        event.remove(fixture.store.engine, "before_execute", record_locks)
        fixture.tearDown()
