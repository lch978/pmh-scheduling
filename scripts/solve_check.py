import json
import time
import datetime
import urllib.request


def main(year: int | None = None, month: int | None = None) -> None:
    today = datetime.date.today()
    if year is None:
        year = today.year
    if month is None:
        month = today.month

    payload = json.dumps({"year": year, "month": month}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:5000/start_solve",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        job_id = json.loads(resp.read().decode("utf-8")).get("job_id")
    print(f"job_id={job_id}")

    deadline = time.time() + 45
    last_status = None
    while time.time() < deadline:
        time.sleep(2)
        with urllib.request.urlopen(
            f"http://127.0.0.1:5000/solve_status/{job_id}", timeout=10
        ) as resp:
            status_obj = json.loads(resp.read().decode("utf-8"))
        last_status = status_obj.get("status")
        print(f"status={last_status} best={status_obj.get('best')}")
        if last_status != "running":
            if last_status == "done":
                print("FINAL:")
                print(json.dumps(status_obj.get("solution"), separators=(",", ":")))
            else:
                print(f"FINAL status={last_status}")
            return

    print(f"FINAL status={last_status}")


if __name__ == "__main__":
    main()


