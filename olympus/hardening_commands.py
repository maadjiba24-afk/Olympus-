"""Operator entry points for M03 evidence inspection and explicit recovery."""
import json

from . import config, deltas, note_evidence as notes, owner_evidence as oe


def _print(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def sleeptime_command(args):
    from . import sleeptime as st, sleeptime_cycles as cycles, sleeptime_evidence as se
    user = args.user or "cli"
    try:
        action = args.action
        if action in ("status", "state-status"):
            result = {"owner": se.status(user), "cycles": cycles.status(),
                      "enabled": config.sleeptime_enabled(), "autoapply": config.sleeptime_autoapply()}
            result["graduated"] = st.graduated() if result["cycles"]["state"] != "unavailable" else None
            _print(result)
            return int(any(result[key]["state"] == "unavailable" for key in ("owner", "cycles")))
        if action == "initialize-empty":
            _print(se.initialize(user, acknowledge_legacy=args.acknowledge_legacy))
        elif action == "cycles-initialize":
            _print(cycles.initialize(acknowledge_legacy=args.acknowledge_legacy))
        elif action == "retry-quarantine":
            _print(se.flush_quarantine(user))
        elif action == "retry-cycle":
            if not args.ident:
                raise ValueError("retry-cycle requires its recorded cycle ID")
            record = cycles.replay(args.ident)
            if record is None:
                raise ValueError("no persisted evidence for this cycle; no model work was repeated")
            _print(record)
        elif action == "proposals":
            rows = st.proposals(args.user or args.ident or "cli")
            if not rows:
                print("No proposals.")
            for row in rows:
                print(st.render_diff(row))
        elif action in ("apply", "revert"):
            if not args.ident:
                raise ValueError(action + " requires the exact proposal/snapshot ID and optional --user")
            result = st.approve(user, args.ident) if action == "apply" else st.revert(user, args.ident)
            _print({"owner": user, "action": action, "id": args.ident, "result": result})
            return 0 if result else 1
        elif action == "run":
            result = st.run_result()
            lines = result["lines"]
            print("\n".join(lines) if lines else
                  ("Sleep-time is disabled." if not config.sleeptime_enabled() else "Nothing to refine."))
            return int(result["status"] in ("unavailable", "dirty"))
        return 0
    except (oe.OwnerEvidenceStateError, deltas.DeltaError, ValueError) as err:
        print(str(err))
        return 1


def supervised_command(args):
    from . import supervise
    try:
        report = supervise.run_supervised_cycle(cycle_id=args.cycle_id)
        rendered = supervise.render_report(report)
        print(rendered)
        notes.create("shared", "reports", "sleeptime supervision cycle", rendered,
                     action_id="supervision:" + report["cycle_id"])
        return 0 if report["grade"] == "CLEAN" else 1
    except (oe.OwnerEvidenceStateError, deltas.DeltaError, ValueError) as err:
        print(str(err))
        return 1


def wiki_command(args):
    from . import wiki, wiki_evidence as we
    try:
        action = args.action
        if action == "state-status":
            result = we.status(args.user)
            _print(result)
            return int(result["state"] == "unavailable")
        if action == "initialize-empty":
            _print(we.initialize(args.user, acknowledge_legacy=args.acknowledge_legacy))
        elif action == "list":
            print(wiki.summary(args.user))
        elif action == "show":
            if not args.page:
                raise ValueError("wiki show requires a page name")
            print(wiki.read(args.user, args.page))
        elif action == "lint":
            issues = wiki.lint(args.user)
            print("\n".join(issues) if issues else "Wiki is fresh — no issues.")
        elif action == "dream":
            result = wiki.dream(args.user)
            print(result)
            return int(result.startswith("dream failed:"))
        elif action == "rm":
            if not args.page:
                raise ValueError("wiki rm requires a page name")
            removed = wiki.remove(args.user, args.page)
            print("Removed." if removed else "No such page.")
            return 0 if removed else 1
        return 0
    except (oe.OwnerEvidenceStateError, ValueError) as err:
        print(str(err))
        return 1


def prompt_command(args):
    from . import prompt_evidence
    try:
        result = (prompt_evidence.status(args.agent) if args.command == "prompt-status" else
                  prompt_evidence.recover(args.agent, args.operation, args.decision))
        _print(result)
        return int(result.get("state") in ("unavailable", "recovery required"))
    except (oe.OwnerEvidenceStateError, deltas.DeltaError, ValueError, OSError) as err:
        print(str(err))
        return 1
