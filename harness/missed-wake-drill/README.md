# Missed-wake stall drill

```
python harness/missed-wake-drill/missed_wake_drill.py [--out <dir>]
```

Exits 0 iff every step held. Writes `<out>/missed-wake-drill.md`; `--out` defaults to a temp
directory, so a bare run never touches the live board.

**Run it by path, as above, not as a module.** `python -m agentflow.wake_watch` — the command the
drill itself invokes in a subprocess and the one this README and `docs/REGISTRY.md` tell an
operator to schedule — resolves against the *installed* package. On a branch where the watcher has
not merged yet, that is the main checkout and you get `No module named agentflow.wake_watch`; the
drill sidesteps it by putting `skills/_shared` on the subprocess `PYTHONPATH`, and you can do the
same (`PYTHONPATH=skills/_shared`) if you want to type the command by hand pre-merge.

## Why it exists

`missed-wake-alarm` defined done as *"a deliberate stall of a test lane past its declared `eta`
produces an alert, with nobody at a terminal."* Nothing exercised it. A repo-wide grep for
`stall drill` / `deliberate stall` / `unattended alert` returned zero matches, and the shipping
PR's verification listed mutation kills and passing tests but no stall. The fleet's only
self-monitoring carried more assurance than it had earned.

## What it does

1. Builds a board where one lane's declared `eta` has lapsed four grace-windows and its
   heartbeat predates that eta — the shape of 2026-08-29, when every process stayed alive and
   no timer fired. A second lane declares a live `eta` and acts as the control.
2. Runs the detector the steward runs, and requires it to name the stalled lane, **not** the
   control, and to cite the exact eta that was missed.
3. Drives the alert leg the steward's own prose specifies — a routed Note at the stalled lane —
   through the mediated CLI, and confirms it landed on the board.
4. Runs `python -m agentflow.wake_watch` in a **separate interpreter** against the same board
   and requires it to name the stalled lane, not the control, and to exit 0.
5. Stalls the **steward's own row** and requires the outside watcher to name it. This is the
   case no in-sweep detector can reach: the steward runs the sweep, so when the steward is what
   stopped, the thing that would have reported it is the thing that stopped.
6. Stands a lane down through the real verb, gives it the stalled lane's **exact** lapsed cells,
   and requires the watcher to stay silent about it while still naming the two genuinely stalled
   lanes.
7. Builds the alert body from the findings with the sender injected, requires it to carry exactly
   the lanes the report named, and compares the **bytes that reached the sender** against that
   body. The comparison is unconditional and runs in CI. It did not always: it was guarded on the
   sender having been called at all, and under the CI suite — which stubs `ntfy.notify` so no test
   can POST — it never was, so the guard passed by not executing and a corrupted payload survived
   every build. The drill now supplies its own send path (a fresh, unpatched import of
   `agentflow.ntfy` with its destination pinned to two env names the drill owns) so the leg runs
   the same way in CI and on a configured box. The sender is still injected, so no packet can
   leave the machine either way.
8. Writes a record naming the lanes, the missed eta and the lag, so a later audit can cite it
   instead of re-deriving it.

The stall is written into the row **directly**, not through `status`: every `status` write
stamps `heartbeat=now`, so a row staged through the CLI always proves its own wake fired. An
earlier version of the alarm's tests made that mistake and said so.

Steps 4–6 use a **subprocess** rather than a function call on purpose. Calling `watch()`
in-process would re-prove what step 2 already proves; only a fresh interpreter establishes the
claim actually at issue — that the alarm fires with no loop, no sweep and no session anywhere in
the process running it.

## What it deliberately does not prove

- **The drill schedules nothing, and cannot see whether anything else did.** The watcher is a
  command; invoking it on a cadence is the suite's one permitted daemon (the single-daemon rule,
  2026-09-22) and lives in an OS scheduler entry, not in here. This drill registers none and
  reads none. A watcher that is never invoked is silent, and no limb here can see that.
- **No packet leaves the machine, so delivery is unproven.** `notify()` returns None whether it
  POSTed, was switched off by `notify.kind: none`, or found no destination in the environment,
  so no caller anywhere can prove delivery today.
- **"Outside a loop" is proven to a process boundary and no further.** Whether the machine had
  an agentflow session open while the drill ran is a property of the desktop, not of this code.
- **The watcher never relaunches a lane, and this drill does not test that it could.** That is
  the LOCKED decision on the epic, not an omission, and the 2026-09-22 revocation of the
  daemon-free invariant did not touch it: the single-daemon budget buys a scheduled REPORTER and
  is already spent. Notify costs one push; relaunch costs the single-writer rule, because
  relaunching on a false positive starts a second copy of a live lane.

All four are stated in the drill's own record so an audit cites the limit rather than
rediscovering it.

## In CI

`skills/_shared/tests/test_missed_wake_drill.py` runs this module every build and, more to the
point, breaks each limb of it in turn — neutering the stall, making the detector name every row,
swallowing the alert, blinding the outside watcher, and deleting the halted-by-design exclusion —
and requires the drill to FAIL each time. A drill whose steps cannot fail is a fixture that
describes itself.
