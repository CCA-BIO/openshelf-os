#!/usr/bin/env python3
"""Find the lowest Y peak current that still lowers the carriage and its payload under control.

Runs Y descent trials (POST /api/robot/debug/motors/y_axis/descent-trial) on a machine in
development mode, from the configured current down, and stops at the first trial that fails,
whose peak following error passes --margin of the drive trip, or whose carriage sags more than
--max-sag-mm after it parks. Stdlib only; run it from a
laptop against the machine's API. Every trial response is saved in full under --output.

Before running, on site:
  1. Development mode, Y homed, area clear. Tool fully retracted, gripper holding the
     heaviest supported load (5 kg) or the agreed test mass. Nobody inside the machine.
  2. Pick a descent span well above the structure, e.g. --start-mm 700 --end-mm 300. A too-low
     current can let the carriage run ahead of the command until the trip fires, so keep at
     least 150 mm of clear travel under --end-mm and put blocks under the carriage there.
  3. Keep a hand on the emergency stop. Closing this script does not stop a trial in progress.
  4. Run --check-only first: one trial at the configured current. It confirms the drive answers
     the trace registers (trace_failed false), that the profile and feedback positions scale as
     expected (about 250 and 100 per mm at 10000 ppr, 4000 cpr, 40 mm/rev), and the baseline
     error.

The sweep:
  5. Run without --check-only. The configured current runs first as the baseline under a 40 mm
     trip; the sweep trip is then 3x the baseline peak following error, clamped to 5..40 mm.
     Each current gets --repeats trials. --trip-mm skips the baseline and fixes the trip.
  6. On a failed trial the script stops and leaves Y unhomed. Inspect the machine, remove the
     blocks (and any gauge) from under the carriage, clear the alarm from the dev UI, then home
     Y yourself. The script never homes: homing descends to the bottom at full current.
  7. Sag is how far Y drops between the end of the move and 0.5 s later, while the holding
     current (a share of the trial current) waits for the brake. On lb4a-9 with 5 kg it was
     <=0.04 mm down to 2.0 A, 0.9-2.3 mm at 1.6 A, and 1.3 A ran away during the stop, so a
     sagging current is the last warning before a runaway and stops the sweep like a failure.
     The floor is the lowest current where every trial completed inside the margin and the sag
     limit. The summary
     prints 1.5x and 2x the floor as the candidate descent current.
  8. Confirm the candidate: rerun with --currents <candidate> --trip-mm <sweep trip> --repeats 20,
     at the cold start of a day and again after an hour of cycling, on each Y motor type (LB4
     and LB4A differ).

Contact force (not automated): put a load cell rated for at least 3 kN on the structure under
the carriage, home Y, and run one trial with --end-mm about 5 mm below the point of contact,
--currents <candidate> --trip-mm <sweep trip> --repeats 1. The descent ends on the gauge and
stops on the trip; read the peak force from the gauge. Repeat at the configured current to get
today's number. The trial fails by design, so step 6 applies after each run.

--pause-at-mm stops each descent part-way through the machine's own safety-guard pause (what a
light curtain break does), holds the load for --pause-s, then resumes. The stop brakes harder
than the end of a move, and the hold runs on the descent current; both show in the following
error and the row reports where it paused.

--leg lift sweeps the climb current instead (descent at the configured current), to measure
how much of the configured current lifting actually needs.

Example:
  scripts/y_descent_sweep.py --base-url http://192.168.1.17:8000 --start-mm 700 --end-mm 300 --check-only
  scripts/y_descent_sweep.py --base-url http://192.168.1.17:8000 --start-mm 700 --end-mm 300
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TRIAL_PATH = "/api/robot/debug/motors/y_axis/descent-trial"
BASELINE_TRIP_MM = 40.0
AUTO_TRIP_FACTOR = 3.0
AUTO_TRIP_MIN_MM = 5.0
AUTO_TRIP_MAX_MM = 40.0
# A trial moves at most a meter at the configured speed; this only bounds a dead connection.
REQUEST_TIMEOUT_S = 300.0
LADDER = "40,30,25,20,16,13,11,9,8,7,6"


def main() -> int:
    args = parse_args()
    api = Api(args.base_url, args.token)

    mode = api.get("/api/system/mode")["mode"]
    if mode != "development":
        print(f"Refusing: {args.base_url} runs in {mode} mode; trials need development mode.")
        return 2
    probe = api.post("/api/robot/debug/motors/probe", {})
    y_position = next(
        (r.get("position_mm") for r in probe.get("results", []) if r.get("motor_name") == "y_axis"), None
    )
    print(f"Machine at {args.base_url}: mode {mode}, Y at {y_position} mm.")

    ip = args.configured_ip
    ladder = [int(value) for value in args.currents.split(",") if value.strip()]
    run_baseline = args.check_only or args.trip_mm is None
    if args.check_only:
        ladder = []
    elif run_baseline:
        ladder = [current for current in ladder if current < ip]
    levels = ([ip] if run_baseline else []) + ladder
    repeats = 1 if args.check_only else args.repeats
    print(
        f"Plan: {args.leg} leg {args.start_mm} -> {args.end_mm} mm, currents {levels} (0.1 A) x {repeats}; "
        f"{'baseline first, then a derived trip' if run_baseline else f'trip {args.trip_mm} mm'}. "
        "The emergency stop is the only abort once a trial runs."
    )
    if not args.yes and input("Is this the machine in front of you, cleared for the test? Type yes: ").strip() != "yes":
        print("Not confirmed; nothing moved.")
        return 1

    output = Path(args.output) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True, exist_ok=True)
    run = Run(args, output)
    trip_mm = BASELINE_TRIP_MM if run_baseline else args.trip_mm

    for level_index, current in enumerate(levels):
        is_baseline = run_baseline and level_index == 0
        for repeat in range(repeats):
            result = run.trial(current, repeat, trip_mm)
            if is_baseline and result["configured_peak_current"] != ip:
                print(f"Configured Y current is {result['configured_peak_current']}; rerun with --configured-ip "
                      f"{result['configured_peak_current']}.")
                return run.finish()
            if run.stop_reason is not None:
                print(run.stop_reason)
                return run.finish()
        if is_baseline and args.check_only:
            run.report_check(result)
        if is_baseline and not args.check_only:
            baseline_mm = max(row["max_following_error_mm"] for row in run.rows)
            trip_mm = round(min(AUTO_TRIP_MAX_MM, max(AUTO_TRIP_MIN_MM, AUTO_TRIP_FACTOR * baseline_mm)), 1)
            print(f"Baseline peak following error {baseline_mm:.2f} mm; sweep trip set to {trip_mm} mm.")

    return run.finish()


class Run:
    def __init__(self, args: argparse.Namespace, output: Path) -> None:
        self.args = args
        self.output = output
        self.api = Api(args.base_url, args.token)
        self.rows: list[dict] = []
        self.profile_per_mm: float | None = None
        self.feedback_per_mm: float | None = None
        self.stop_reason: str | None = None

    def trial(self, current: int, repeat: int, trip_mm: float) -> dict:
        args = self.args
        body: dict = {"start_mm": args.start_mm, "end_mm": args.end_mm, "following_error_trip_mm": trip_mm}
        if args.rpm is not None:
            body["rpm"] = args.rpm
        if args.pause_at_mm is not None:
            body["pause_at_mm"] = args.pause_at_mm
            body["pause_s"] = args.pause_s
        if args.leg == "descent":
            body["descent_peak_current"] = current
        else:
            body["approach_peak_current"] = current
            body["descent_peak_current"] = args.configured_ip

        result = self.api.post(TRIAL_PATH, body)
        (self.output / f"trial-{current:03d}-{repeat + 1:02d}.json").write_text(json.dumps(result, indent=1))

        leg = result["descent"] if args.leg == "descent" else result["approach"]
        fe_mm, fe_at_mm = self.derived_following_error(leg)
        shares = [] if fe_mm is None else [fe_mm / trip_mm]
        sag_mm = None
        if leg and leg.get("end_mm") is not None and leg.get("settled_mm") is not None:
            sag_mm = leg["end_mm"] - leg["settled_mm"]
        row = {
            "current_a": current / 10,
            "repeat": repeat + 1,
            "outcome": result["outcome"],
            "alarm_code": None if leg is None else leg.get("alarm_code"),
            "trip_mm": trip_mm,
            "max_following_error_mm": fe_mm,
            "share_of_trip": max(shares) if shares else None,
            "at_mm": fe_at_mm,
            "sag_mm": sag_mm,
            "paused_at_mm": None if leg is None else leg.get("paused_at_mm"),
            "hold_start_mm": None if leg is None else leg.get("hold_start_mm"),
            "hold_end_mm": None if leg is None else leg.get("hold_end_mm"),
            "trace_failed": None if leg is None else leg.get("trace_failed"),
        }
        self.rows.append(row)
        print(_format_row(row))

        if result["outcome"] != "completed":
            self.stop_reason = (
                "Trial failed: Y is stopped and needs homing. Inspect, remove blocks and gauge from under "
                "the carriage, clear the alarm, then home Y yourself."
            )
        elif fe_mm is None:
            self.stop_reason = (
                f"The following-error trace is missing or too short to scale (trace_failed="
                f"{row['trace_failed']}). Fix that before sweeping."
            )
        elif row["share_of_trip"] > args.margin:
            self.stop_reason = f"Peak following error reached {row['share_of_trip']:.0%} of the trip; stopping."
        elif sag_mm is None or abs(sag_mm) > args.max_sag_mm:
            self.stop_reason = (
                f"Y sagged {'an unknown amount' if sag_mm is None else f'{sag_mm:.2f} mm'} after parking "
                f"(limit {args.max_sag_mm} mm); this current cannot hold the load until the brake sets. Stopping."
            )
        return result

    def derived_following_error(self, leg: dict | None) -> tuple[float | None, float | None]:
        """Peak |profile - feedback| in mm and where it happened.

        0x1010 on the CL57RS is not the following error in any unit the trip uses (on lb4a-3 it
        tracked the real error at ~155k per mm), so the error is profile minus feedback. Both
        restart at 0 each move; their units come from a leg's travel (250/mm command pulses and
        100/mm encoder counts at 40 mm/rev), kept from the first leg long enough to measure.
        """
        samples = [] if leg is None else [
            s for s in leg.get("samples") or []
            if s.get("profile_position") is not None and s.get("feedback_position") is not None
        ]
        if len(samples) < 2:
            return None, None
        travel_mm = samples[-1]["position_mm"] - samples[0]["position_mm"]
        if abs(travel_mm) >= 50 and self.profile_per_mm is None:
            self.profile_per_mm = (samples[-1]["profile_position"] - samples[0]["profile_position"]) / travel_mm
            self.feedback_per_mm = (samples[-1]["feedback_position"] - samples[0]["feedback_position"]) / travel_mm
        if not self.profile_per_mm or not self.feedback_per_mm:
            return None, None
        peak = max(
            samples,
            key=lambda s: abs(s["profile_position"] / self.profile_per_mm - s["feedback_position"] / self.feedback_per_mm),
        )
        fe = abs(peak["profile_position"] / self.profile_per_mm - peak["feedback_position"] / self.feedback_per_mm)
        return fe, peak["position_mm"]

    def report_check(self, result: dict) -> None:
        trip_units_per_mm = result["following_error_trip_counts"] / result["following_error_trip_mm"]
        print(
            f"Profile {self.profile_per_mm} and feedback {self.feedback_per_mm} per mm; trip written at "
            f"{trip_units_per_mm:.0f} counts per mm; holding {result['holding_current_percent']}%, "
            f"standby {result['standby_current_percent']}%."
        )

    def finish(self) -> int:
        args = self.args
        rows = self.rows
        with (self.output / "summary.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["current_a"])
            writer.writeheader()
            writer.writerows(rows)

        levels: dict[float, list[dict]] = {}
        for row in rows:
            levels.setdefault(row["current_a"], []).append(row)
        repeats = 1 if args.check_only else args.repeats
        passing: list[float] = []
        print("\ncurrent  trials  completed  worst fe mm  worst share  worst sag mm")
        for current, level in levels.items():
            completed = sum(1 for r in level if r["outcome"] == "completed")
            worst_fe = max((r["max_following_error_mm"] or 0.0 for r in level), default=0.0)
            worst_share = max((r["share_of_trip"] or 0.0 for r in level), default=0.0)
            worst_sag = max((abs(r["sag_mm"] or 0.0) for r in level), default=0.0)
            sag_known = all(r["sag_mm"] is not None for r in level)
            if (
                len(level) == repeats and completed == len(level) and worst_share <= args.margin
                and sag_known and worst_sag <= args.max_sag_mm
            ):
                passing.append(current)
            print(f"{current:>5.1f} A  {len(level):>6}  {completed:>9}  {worst_fe:>11.2f}  {worst_share:>11.0%}  {worst_sag:>12.2f}")

        if args.check_only:
            pass
        elif args.trip_mm is not None and len(levels) == 1:
            current = next(iter(levels))
            verdict = "passed" if passing else "did not pass"
            print(f"\nConfirmation: {current:.1f} A {verdict} ({len(levels[current])} of {repeats} trials ran).")
        elif passing:
            floor = min(passing)
            cap = args.configured_ip / 10
            print(f"\nFloor: {floor:.1f} A. Candidates: {min(cap, 1.5 * floor):.1f} A (1.5x), {min(cap, 2 * floor):.1f} A (2x).")
            print("Confirm the candidate with --currents <0.1 A> --trip-mm <sweep trip> --repeats 20 before using it.")
        print(f"Saved under {self.output}")
        return 0


def _format_row(row: dict) -> str:
    fe = "-" if row["max_following_error_mm"] is None else f"{row['max_following_error_mm']:.2f}"
    share = "-" if row["share_of_trip"] is None else f"{row['share_of_trip']:.0%}"
    sag = "-" if row["sag_mm"] is None else f"{row['sag_mm']:.2f}"
    paused = "" if row["paused_at_mm"] is None else f"  paused at {row['paused_at_mm']:.1f} mm"
    if row.get("hold_start_mm") is not None and row.get("hold_end_mm") is not None:
        paused += (
            f", stopped {row['paused_at_mm'] - row['hold_start_mm']:.2f} mm later,"
            f" slid {row['hold_start_mm'] - row['hold_end_mm']:.3f} mm in the hold"
        )
    return (
        f"{row['current_a']:>4.1f} A  #{row['repeat']:<2} {row['outcome']:<11} fe {fe:>6} mm "
        f"({share} of {row['trip_mm']} mm trip) at {row['at_mm']} mm  sag {sag} mm{paused}"
    )


class Api:
    def __init__(self, base_url: str, token: str | None) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def get(self, path: str) -> dict:
        return self._request("GET", path, None)

    def post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, body)

    def _request(self, method: str, path: str, body: dict | None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base_url + path, data=data, method=method, headers=self.headers)
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            sys.exit(f"{method} {path} returned {exc.code}: {detail}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="API of the machine, e.g. http://192.168.1.17:8000")
    parser.add_argument("--token", default=os.environ.get("OPSHELF_TOKEN"), help="Bearer token (env OPSHELF_TOKEN)")
    parser.add_argument("--start-mm", type=float, required=True, help="Height each trial climbs to first")
    parser.add_argument("--end-mm", type=float, required=True, help="Height each trial descends to")
    parser.add_argument("--configured-ip", type=int, default=60, help="Configured Y Ip in 0.1 A; the baseline runs at it")
    parser.add_argument("--currents", default=LADDER, help="Trial currents in 0.1 A, high to low (below the configured Ip)")
    parser.add_argument("--repeats", type=int, default=5, help="Trials per current")
    parser.add_argument("--leg", choices=("descent", "lift"), default="descent")
    parser.add_argument("--trip-mm", type=float, default=None, help="Fixed trip, no baseline; default derives it from the baseline")
    parser.add_argument("--margin", type=float, default=0.5, help="Stop once peak following error passes this share of the trip")
    parser.add_argument("--max-sag-mm", type=float, default=0.1, help="Fail a trial whose carriage drops more than this after parking")
    parser.add_argument("--rpm", type=int, default=None, help="Trial speed; default is the configured speed")
    parser.add_argument("--pause-at-mm", type=float, default=None, help="Pause each descent here through the safety-guard stop")
    parser.add_argument("--pause-s", type=float, default=5.0, help="How long each paused descent holds")
    parser.add_argument("--output", default="y-descent-sweep", help="Directory for trial JSON and summary.csv")
    parser.add_argument("--check-only", action="store_true", help="One baseline trial at the configured current, then stop")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(main())
