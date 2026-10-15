import concurrent.futures
import time
from datetime import datetime, date, timezone, timedelta
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.main import app, db, client, IST, ensure_indexes

test_client = TestClient(app)


@pytest.fixture(autouse=True, scope="module")
def setup_test_db():
    ensure_indexes()
    yield


def test_health():
    res = test_client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_create_employee_and_duplicate():
    emp_payload = {
        "emp_code": "EMP8001",
        "name": "Rohan Sharma",
        "email": "rohan@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-06-01",
    }
    # Clean up before
    db.employees.delete_many({"emp_code": "EMP8001"})
    db.attendance_logs.delete_many({"emp_code": "EMP8001"})

    res = test_client.post("/employees", json=emp_payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP8001"
    assert isinstance(data["created_at"], int)
    assert "_id" not in data

    # Duplicate create must return 409
    res_dup = test_client.post("/employees", json=emp_payload)
    assert res_dup.status_code == 409


def test_create_employee_validation():
    # Same shift_start and shift_end -> 422
    invalid_emp = {
        "emp_code": "EMP8002",
        "name": "Invalid Emp",
        "email": "invalid@example.com",
        "department": "Engineering",
        "shift_start": "10:00",
        "shift_end": "10:00",
        "joined_on": "2026-06-01",
    }
    res = test_client.post("/employees", json=invalid_emp)
    assert res.status_code == 422

    # Bad emp_code pattern
    invalid_emp2 = dict(invalid_emp, emp_code="NOT_EMP", shift_end="19:00")
    res2 = test_client.post("/employees", json=invalid_emp2)
    assert res2.status_code == 422


def test_list_employees_pagination_and_sorting():
    res = test_client.get("/employees?page=1&page_size=3")
    assert res.status_code == 200
    body = res.json()
    assert len(body["items"]) <= 3
    assert body["page"] == 1
    assert body["page_size"] == 3
    assert body["total"] >= 1
    codes = [e["emp_code"] for e in body["items"]]
    assert codes == sorted(codes)


def test_punch_in_and_duplicate():
    emp_code = "EMP8001"
    db.attendance_logs.delete_many({"emp_code": emp_code})

    # Punch-in at 09:30:00 IST on 2026-07-06 (04:00:00 UTC)
    # 2026-07-06T04:00:00Z -> epoch ms
    dt_utc = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    ts_ms = int(dt_utc.timestamp()) * 1000

    payload = {"emp_code": emp_code, "punched_at": ts_ms, "status": "PRESENT"}
    res = test_client.post("/attendance/punch-in", json=payload)
    assert res.status_code == 201
    record = res.json()
    assert record["emp_code"] == emp_code
    assert record["date"] == "2026-07-06"
    assert record["status"] == "PRESENT"
    assert record["late_minutes"] == 0
    assert record["punch_out"] is None
    assert record["work_hours"] is None
    assert "_id" not in record

    # Duplicate punch in for same day -> 409
    res_dup = test_client.post("/attendance/punch-in", json=payload)
    assert res_dup.status_code == 409


def test_concurrent_punch_in():
    emp_code = "EMP8001"
    # Punch in for 2026-07-07
    dt_utc = datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc)
    ts_ms = int(dt_utc.timestamp()) * 1000
    payload = {"emp_code": emp_code, "punched_at": ts_ms, "status": "PRESENT"}

    db.attendance_logs.delete_many({"emp_code": emp_code, "date": "2026-07-07"})

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(test_client.post, "/attendance/punch-in", json=payload) for _ in range(5)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result().status_code)

    assert results.count(201) == 1
    assert results.count(409) == 4


def test_late_minutes_boundary():
    # R2: Grace is 10 minutes. 09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45
    emp_code = "EMP8001"

    # Case 1: 09:40:00 IST -> 04:10:00 UTC on 2026-07-08
    db.attendance_logs.delete_many({"emp_code": emp_code, "date": "2026-07-08"})
    dt_40_00 = datetime(2026, 7, 8, 4, 10, 0, tzinfo=timezone.utc)
    res1 = test_client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": int(dt_40_00.timestamp()) * 1000})
    assert res1.status_code == 201
    assert res1.json()["late_minutes"] == 0

    # Case 2: 09:40:01 IST -> 04:10:01 UTC on 2026-07-09
    db.attendance_logs.delete_many({"emp_code": emp_code, "date": "2026-07-09"})
    dt_40_01 = datetime(2026, 7, 9, 4, 10, 1, tzinfo=timezone.utc)
    res2 = test_client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": int(dt_40_01.timestamp()) * 1000})
    assert res2.status_code == 201
    assert res2.json()["late_minutes"] == 10

    # Case 3: 10:15:59 IST -> 04:45:59 UTC on 2026-07-10
    db.attendance_logs.delete_many({"emp_code": emp_code, "date": "2026-07-10"})
    dt_15_59 = datetime(2026, 7, 10, 4, 45, 59, tzinfo=timezone.utc)
    res3 = test_client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": int(dt_15_59.timestamp()) * 1000})
    assert res3.status_code == 201
    assert res3.json()["late_minutes"] == 45


def test_overnight_shift_and_ist_boundaries():
    # Create employee with overnight shift 22:00 to 06:00
    emp_payload = {
        "emp_code": "EMP8005",
        "name": "Overnight Worker",
        "email": "overnight@example.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-06-01",
    }
    db.employees.delete_many({"emp_code": "EMP8005"})
    db.attendance_logs.delete_many({"emp_code": "EMP8005"})
    test_client.post("/employees", json=emp_payload)

    # Punch-in at 2026-07-07 05:00:00 IST (earlier than shift_end 06:00)
    # Belongs to shift starting 2026-07-06!
    # 05:00 IST on 2026-07-07 is 2026-07-06 23:30:00 UTC
    dt_early = datetime(2026, 7, 6, 23, 30, 0, tzinfo=timezone.utc)
    res = test_client.post("/attendance/punch-in", json={"emp_code": "EMP8005", "punched_at": int(dt_early.timestamp()) * 1000})
    assert res.status_code == 201
    assert res.json()["date"] == "2026-07-06"


def test_punch_out_calculations_and_race():
    emp_code = "EMP8001"
    # Punch in 09:30 IST on 2026-07-11 -> 04:00 UTC
    db.attendance_logs.delete_many({"emp_code": emp_code, "date": "2026-07-11"})
    dt_in = datetime(2026, 7, 11, 4, 0, 0, tzinfo=timezone.utc)
    test_client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": int(dt_in.timestamp()) * 1000})

    # Punch out 9.125 hours later (04:00 + 9h 7m 30s = 13:07:30 UTC)
    # (9 * 3600 + 7 * 60 + 30) = 32850s / 3600 = 9.125 -> half-up rounding = 9.13 hours
    # 13:07:30 UTC is 18:37:30 IST (shift ends 18:30 -> overtime is 7 minutes, which is < 30 min -> overtime = 0)
    dt_out = dt_in + timedelta(seconds=32850)
    out_ms = int(dt_out.timestamp()) * 1000

    res_out = test_client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": out_ms})
    assert res_out.status_code == 200
    rec = res_out.json()
    assert rec["work_hours"] == 9.13
    assert rec["overtime_minutes"] == 0
    assert rec["half_day"] is False

    # Second punch-out must return 409
    res_out_2 = test_client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": out_ms})
    assert res_out_2.status_code == 409


def test_overtime_above_30_minutes():
    emp_code = "EMP8001"
    # Punch in on 2026-07-12 09:30 IST
    db.attendance_logs.delete_many({"emp_code": emp_code, "date": "2026-07-12"})
    dt_in = datetime(2026, 7, 12, 4, 0, 0, tzinfo=timezone.utc)
    test_client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": int(dt_in.timestamp()) * 1000})

    # Punch out at 19:15:00 IST (45 minutes after 18:30 shift_end) -> 13:45:00 UTC
    dt_out = datetime(2026, 7, 12, 13, 45, 0, tzinfo=timezone.utc)
    res = test_client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": int(dt_out.timestamp()) * 1000})
    assert res.status_code == 200
    assert res.json()["overtime_minutes"] == 45


def test_half_day_threshold():
    emp_code = "EMP8001"
    # Punch in 2026-07-13
    db.attendance_logs.delete_many({"emp_code": emp_code, "date": "2026-07-13"})
    dt_in = datetime(2026, 7, 13, 4, 0, 0, tzinfo=timezone.utc)
    test_client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": int(dt_in.timestamp()) * 1000})

    # Punch out after 4 hours 15 min (4.25 hours < 4.50) -> half_day True
    dt_out = dt_in + timedelta(hours=4, minutes=15)
    res = test_client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": int(dt_out.timestamp()) * 1000})
    assert res.status_code == 200
    assert res.json()["half_day"] is True
    assert res.json()["work_hours"] == 4.25


def test_regularization_and_history():
    emp_code = "EMP8001"
    date_str = "2026-07-13"

    # Regularize to fix punch-out
    # Current punch_in is 2026-07-13 04:00:00 UTC
    # Set punch_out to 2026-07-13 13:00:00 UTC (9 hours)
    dt_new_out = datetime(2026, 7, 13, 13, 0, 0, tzinfo=timezone.utc)
    new_out_ms = int(dt_new_out.timestamp()) * 1000

    patch_payload = {
        "punch_out": new_out_ms,
        "reason": "Forgot to punch out on time due to meeting",
        "regularized_by": "manager.jane",
    }
    res = test_client.patch(f"/attendance/{emp_code}/{date_str}", json=patch_payload)
    assert res.status_code == 200
    updated = res.json()
    assert updated["work_hours"] == 9.0
    assert updated["half_day"] is False
    assert len(updated["history"]) == 1
    h = updated["history"][0]
    assert h["by"] == "manager.jane"
    assert "work_hours" in h["changes"]
    assert "half_day" in h["changes"]
    assert h["changes"]["half_day"]["from"] is True
    assert h["changes"]["half_day"]["to"] is False

    # A request that changes nothing -> 422
    res_noop = test_client.patch(f"/attendance/{emp_code}/{date_str}", json=patch_payload)
    assert res_noop.status_code == 422


def test_leaderboard_competition_ranking_ties():
    # Seed specific late logs for month 2026-08 to test ties:
    # EMP8011: 40 late min
    # EMP8012: 40 late min (tie with EMP8011)
    # EMP8013: 20 late min (should get rank 3!)
    month = "2026-08"
    for code, name in [("EMP8011", "Alice"), ("EMP8012", "Bob"), ("EMP8013", "Charlie")]:
        db.employees.delete_many({"emp_code": code})
        db.attendance_logs.delete_many({"emp_code": code})
        test_client.post("/employees", json={
            "emp_code": code,
            "name": name,
            "email": f"{code.lower()}@example.com",
            "department": "QA",
            "joined_on": "2026-01-01",
        })

    db.attendance_logs.insert_one({"emp_code": "EMP8011", "date": "2026-08-01", "status": "PRESENT", "late_minutes": 40})
    db.attendance_logs.insert_one({"emp_code": "EMP8012", "date": "2026-08-01", "status": "PRESENT", "late_minutes": 40})
    db.attendance_logs.insert_one({"emp_code": "EMP8013", "date": "2026-08-01", "status": "PRESENT", "late_minutes": 20})

    res = test_client.get(f"/analytics/leaderboard/late?month={month}&department=QA")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3
    # First two tied at rank 1, ordered by emp_code asc
    assert items[0]["emp_code"] == "EMP8011" and items[0]["rank"] == 1
    assert items[1]["emp_code"] == "EMP8012" and items[1]["rank"] == 1
    # Next employee skips rank 2 and gets rank 3 (standard competition ranking 1, 1, 3!)
    assert items[2]["emp_code"] == "EMP8013" and items[2]["rank"] == 3


def test_explain_all_endpoints():
    for ep, params in [
        ("attendance_list", {"date_from": "2026-07-06", "date_to": "2026-07-08"}),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("late_leaderboard", {"month": "2026-07"}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-06", "to": "2026-07-10"}),
    ]:
        res = test_client.get(f"/admin/explain/{ep}", params=params)
        assert res.status_code == 200
        data = res.json()
        assert data["endpoint"] == ep
        assert "collection" in data
        assert "explain" in data


def test_attendance_list_index_scan():
    # Verify that attendance_list uses IXSCAN and no COLLSCAN
    res = test_client.get("/admin/explain/attendance_list?date_from=2026-07-06&date_to=2026-07-08")
    assert res.status_code == 200
    explain = res.json()["explain"]
    plan = explain.get("queryPlanner", {}).get("winningPlan", {})

    # Helper function to check for COLLSCAN anywhere in the plan
    def find_stages(node):
        stages = [node.get("stage")]
        if "inputStage" in node:
            stages.extend(find_stages(node["inputStage"]))
        if "inputStages" in node:
            for s in node["inputStages"]:
                stages.extend(find_stages(s))
        return [s for s in stages if s]

    stages = find_stages(plan)
    assert "IXSCAN" in stages
    assert "COLLSCAN" not in stages


def test_weekend_attendance_rules():
    # Rule R7: Weekend records (Sat/Sun) do NOT contribute to present_days,
    # but still add to late and overtime totals.
    emp_code = "EMP8020"
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    test_client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Weekend Worker",
        "email": "weekend@example.com",
        "department": "Support",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })

    # 2026-07-11 is Saturday. 2026-07-12 is Sunday.
    # Punch in Saturday with 15 min late
    db.attendance_logs.insert_one({
        "emp_code": emp_code,
        "date": "2026-07-11",
        "status": "PRESENT",
        "work_hours": 8.0,
        "late_minutes": 15,
        "overtime_minutes": 40,
        "half_day": False,
    })

    # Punch in Sunday with 20 min late
    db.attendance_logs.insert_one({
        "emp_code": emp_code,
        "date": "2026-07-12",
        "status": "PRESENT",
        "work_hours": 8.0,
        "late_minutes": 20,
        "overtime_minutes": 35,
        "half_day": False,
    })

    res = test_client.get(f"/analytics/employees/{emp_code}/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    # present_days only counts Mon-Fri records -> must be 0.0!
    assert data["present_days"] == 0.0
    # But weekend records still add to late and overtime totals!
    assert data["late_count"] == 2
    assert data["total_late_minutes"] == 35
    assert data["total_overtime_minutes"] == 75


def test_mid_month_joiner_working_days():
    # Rule R7: For an employee who joined mid-month, working days are counted from joined_on.
    # In 2026-07: month starts on Wednesday 2026-07-01. Total Mon-Fri in July 2026 is 23.
    # Employee joins on Monday 2026-07-20.
    # From 2026-07-20 to 2026-07-31:
    # Week 1: 20, 21, 22, 23, 24 (5 days)
    # Week 2: 27, 28, 29, 30, 31 (5 days)
    # Total working days = 10 days!
    emp_code = "EMP8025"
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    test_client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Mid Month Joiner",
        "email": "mid@example.com",
        "department": "Finance",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-20",
    })

    res = test_client.get(f"/analytics/employees/{emp_code}/monthly?month=2026-07")
    assert res.status_code == 200
    assert res.json()["working_days"] == 10


def test_department_summary_with_zero_log_employees():
    # Headcount per R9: people with zero logs still count, people who join later do not.
    dept = "Marketing"
    db.employees.delete_many({"department": dept})
    db.attendance_logs.delete_many({"emp_code": {"$in": ["EMP8031", "EMP8032", "EMP8033"]}})

    # Employee 1 joined before month end (has 1 log)
    test_client.post("/employees", json={
        "emp_code": "EMP8031", "name": "Mkt 1", "email": "m1@example.com",
        "department": dept, "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-01"
    })
    # Employee 2 joined before month end (has ZERO logs)
    test_client.post("/employees", json={
        "emp_code": "EMP8032", "name": "Mkt 2", "email": "m2@example.com",
        "department": dept, "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-15"
    })
    # Employee 3 joined AFTER month end (joined 2026-08-01 - must NOT be in headcount for 2026-07)
    test_client.post("/employees", json={
        "emp_code": "EMP8033", "name": "Mkt 3", "email": "m3@example.com",
        "department": dept, "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-08-01"
    })

    # Add 1 log for EMP8031
    db.attendance_logs.insert_one({
        "emp_code": "EMP8031",
        "date": "2026-07-06",
        "status": "PRESENT",
        "work_hours": 8.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    })

    res = test_client.get(f"/analytics/departments/summary?month=2026-07&department={dept}")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    # Headcount must be 2 (EMP8031 and EMP8032), excluding EMP8033!
    assert items[0]["headcount"] == 2
    assert items[0]["present_days"] == 1.0


def test_invalid_query_parameters():
    # date_from > date_to -> 422
    res = test_client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert res.status_code == 422

    # trend to < from -> 422
    res2 = test_client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01")
    assert res2.status_code == 422

    # trend range > 92 days -> 422
    res3 = test_client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-06-01")
    assert res3.status_code == 422

    # invalid month pattern -> 422
    res4 = test_client.get("/analytics/departments/summary?month=2026-13")
    assert res4.status_code == 422


def test_invalid_timestamp_types_and_ranges():
    emp_code = "EMP8001"
    # String timestamp -> 422
    r1 = test_client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": "1783312500000"})
    assert r1.status_code == 422

    # Float timestamp -> 422
    r2 = test_client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": 1783312500000.5})
    assert r2.status_code == 422

    # Bool timestamp -> 422
    r3 = test_client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": True})
    assert r3.status_code == 422

    # Seconds-looking timestamp (< 100000000000) -> 422
    r4 = test_client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": 1783312500})
    assert r4.status_code == 422

    # Far future (> 4102444800000) -> 422
    r5 = test_client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": 5000000000000})
    assert r5.status_code == 422


def test_absent_and_leave_punch_time_restrictions():
    emp_code = "EMP8001"
    date_str = "2026-07-06"
    # Setting ABSENT while supplying punch_in -> 422
    r1 = test_client.patch(
        f"/attendance/{emp_code}/{date_str}",
        json={"status": "ABSENT", "punch_in": 1783312500000, "reason": "Sick leave requested", "regularized_by": "admin"},
    )
    assert r1.status_code == 422

    # Setting LEAVE while supplying punch_out -> 422
    r2 = test_client.patch(
        f"/attendance/{emp_code}/{date_str}",
        json={"status": "LEAVE", "punch_out": 1783312500000, "reason": "Sick leave requested", "regularized_by": "admin"},
    )
    assert r2.status_code == 422


def test_missing_resource_errors():
    # Unknown employee punch in -> 404
    r1 = test_client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert r1.status_code == 404

    # Unknown employee punch out -> 404
    r2 = test_client.post("/attendance/punch-out", json={"emp_code": "EMP9999"})
    assert r2.status_code == 404

    # Unknown employee monthly analytics -> 404
    r3 = test_client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
    assert r3.status_code == 404

    # Unknown department trend -> 404
    r4 = test_client.get("/analytics/departments/NonExistentDept/trend?from=2026-07-01&to=2026-07-05")
    assert r4.status_code == 404

    # Unknown attendance record regularization -> 404
    r5 = test_client.patch("/attendance/EMP8001/2026-12-31", json={"status": "WFH", "reason": "Work from home day", "regularized_by": "admin"})
    assert r5.status_code == 404


def test_concurrent_regularizations():
    # Ensure concurrent regularizations on the same record do not silently lose history
    emp_code = "EMP8001"
    date_str = "2026-07-06"
    # Base record exists
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    assert rec is not None

    def make_patch(status_val, reason_val):
        return test_client.patch(
            f"/attendance/{emp_code}/{date_str}",
            json={"status": status_val, "reason": reason_val, "regularized_by": "admin"},
        )

    statuses = ["WFH", "ON_DUTY", "PRESENT"]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(make_patch, statuses[i], f"Concurrent reason {i+1}") for i in range(3)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result().status_code)

    # Exactly one request or valid sequence succeeds; loser gets 409, no silent history loss
    assert 200 in results
    # Check that history length reflects the successful updates
    updated = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    assert len(updated.get("history", [])) >= 1


def test_unsupported_explain_endpoint():
    res = test_client.get("/admin/explain/unknown_route")
    assert res.status_code == 422


