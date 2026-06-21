"""Olympus command-line interface."""

from __future__ import annotations

import argparse
import sys

from . import heartbeat, memory, orchestrator


def _chat() -> None:
    from . import config
    pool = config.ModelPool.from_env()
    print("OLYMPUS — Zeus speaking. Type 'exit' to leave, "
          "'/good' or '/bad' to rate the last answer.")
    if pool.is_multi():
        print(pool.assignment())
    print()
    bot = orchestrator.Olympus(report=lambda msg: print(f"  {msg}"),
                               user="cli", conversation_id="cli-default",
                               pool=pool)
    while True:
        try:
            user = input("you ▸ ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user.lower() in {"exit", "quit"}:
            break
        if user.lower().startswith(("/good", "/bad")):
            verdict = "up" if user.lower().startswith("/good") else "down"
            print(f"  {bot.feedback(verdict, user.partition(' ')[2])}")
            continue
        if user.lower().startswith("/lang"):
            print(f"  {bot.set_language(user.partition(' ')[2] or 'auto')}")
            continue
        if user.lower().startswith("/contribute"):
            arg = user.partition(' ')[2].strip().lower()
            print(f"  {bot.set_contribute(arg in ('on','yes','true','1','enable'))}")
            continue
        try:
            reply = bot.ask(user)
        except Exception as err:
            print(f"  [error] {err}")
            continue
        print(f"\nolympus ▸ {reply}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="olympus",
        description="OLYMPUS — self-improving multi-agent AI system",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("chat", help="interactive conversation (default)")
    sub.add_parser("setup", help="choose your AI provider & save your API key")
    p_prof = sub.add_parser(
        "profile", help="show or set what Olympus remembers about you")
    p_prof.add_argument("about", nargs="*",
                        help="a note about you (no args = show the card)")
    p_prof.add_argument("--set", nargs=2, metavar=("KEY", "VALUE"),
                        help="set a fact, e.g. --set company Acme")
    p_prof.add_argument("--clear", action="store_true", help="forget it all")
    p_mem = sub.add_parser(
        "memory", help="inspect/approve/forget durable memories; or carry your "
                       "file memory out (migrate/export/import/delete)")
    p_mem.add_argument(
        "action", nargs="?", default="list",
        choices=["list", "candidates", "approve", "reject", "forget", "search",
                 "migrate", "export", "import", "delete"])
    p_mem.add_argument("arg", nargs="*",
                       help="id (approve/reject/forget), query (search), or "
                            "archive path (export/import)")
    p_mem.add_argument("--user", help="memory namespace for export/delete "
                                      "(default: shared)")
    p_mem.add_argument("--all", action="store_true",
                       help="export every user's memory, not just one")
    p_mem.add_argument("--out", help="archive path for export")
    p_mem.add_argument("--category", help="limit delete to one category "
                                          "(e.g. lessons)")
    p_mem.add_argument("--id", dest="note_id",
                       help="limit delete to one note (filename or stem)")
    p_mem.add_argument("--encrypt", action="store_true",
                       help="encrypt the export with OLYMPUS_SECRET_KEY")
    p_mem.add_argument("--yes", action="store_true",
                       help="skip the confirmation prompt on delete")
    p_pb = sub.add_parser(
        "playbook", help="save/run/manage repeatable workflows")
    p_pb.add_argument(
        "action", nargs="?", default="list",
        choices=["list", "proposed", "show", "save", "run", "forget",
                 "approve", "reject"])
    p_pb.add_argument("name", nargs="?", help="playbook name or id")
    p_pb.add_argument("steps", nargs="*",
                      help="for save: steps separated by ';'")
    p_gr = sub.add_parser(
        "graph", help="inspect the people/companies relationship graph")
    p_gr.add_argument("entity", nargs="*",
                      help="an entity to describe (no args = list everything)")
    p_gr.add_argument("--forget", metavar="ENTITY", help="remove an entity")
    sub.add_parser("outcomes", help="Olympus's track record: what you approved, "
                                    "edited, or declined")
    sub.add_parser("status", help="instance health: provider, spend, usage")

    p_replay = sub.add_parser(
        "replay", help="re-execute a recorded run against its frozen LLM "
                       "responses and prove the decision path is unchanged")
    p_replay.add_argument("run_id", help="the run id from a trace")
    p_explain = sub.add_parser(
        "explain", help="show the decision path of a recorded run, or one "
                        "decision record by id")
    p_explain.add_argument("id", help="a run id, or a decision record id")

    p_ask = sub.add_parser("ask", help="one-shot question through the full pipeline")
    p_ask.add_argument("question", nargs="+")

    sub.add_parser("scan", help="Argus: scan the web for opportunities now")
    sub.add_parser("audit", help="Prometheus: self-audit and self-upgrade now")

    p_watch = sub.add_parser("watch", help="Mnemosyne: watch a YouTube video now")
    p_watch.add_argument("url")

    p_queue = sub.add_parser("queue", help="queue a YouTube video for the heartbeat")
    p_queue.add_argument("url")

    sub.add_parser("heartbeat", help="run the self-recurring autonomous loop")
    sub.add_parser("learn", help="Metis: run the daily learning cycle now")
    sub.add_parser("eval", help="run the quality benchmark and save the score")
    sub.add_parser("code-eval", help="run execution-scored coding benchmarks "
                                     "(runs Hephaestus's code against tests)")
    sub.add_parser("skills", help="list the self-built skill library")
    sub.add_parser("gate", help="benchmark-gate provisional skills now")
    p_train = sub.add_parser("train", help="score all specialists and have "
                                           "Prometheus strengthen the weakest")
    p_train.add_argument("--focus", type=int, default=2,
                         help="how many of the weakest specialists to improve")
    sub.add_parser("scores", help="show per-specialist benchmark scores")
    sub.add_parser("models", help="show the model pool and role assignments")
    sub.add_parser("contrib", help="show the cross-model contribution queue size")
    p_usage = sub.add_parser("usage", help="show estimated token/cost spend")
    p_usage.add_argument("--days", type=int, default=7)
    p_budget = sub.add_parser(
        "budget", help="show or set the daily spend cap on your API key (USD)")
    p_budget.add_argument("amount", nargs="?", type=float,
                          help="e.g. 5  (0 removes the cap)")

    # --- the Action spine (controlled-autonomy execution) ---
    sub.add_parser("actions", help="list pending actions awaiting approval")
    p_ap = sub.add_parser("approve", help="approve and execute a prepared action")
    p_ap.add_argument("action_id")
    p_rj = sub.add_parser("reject", help="reject a prepared action")
    p_rj.add_argument("action_id")
    p_rj.add_argument("reason", nargs="*")
    p_ed = sub.add_parser("edit", help="edit a prepared action before approving")
    p_ed.add_argument("action_id")
    p_ed.add_argument("changes", nargs="+", metavar="field=value",
                      help="e.g. subject='New subject' body='...'")
    p_un = sub.add_parser("undo", help="undo a reversible, executed action")
    p_un.add_argument("action_id")
    p_au = sub.add_parser("autonomy", help="show or set the autonomy level (0-4)")
    p_au.add_argument("level", nargs="?", type=int)
    p_gr = sub.add_parser("grant", help="grant a permission scope (e.g. email)")
    p_gr.add_argument("scope")
    p_rv = sub.add_parser("revoke", help="revoke a scope ('all' = kill switch)")
    p_rv.add_argument("scope")
    p_lim = sub.add_parser(
        "limit", help="show or set daily execution caps per action type")
    p_lim.add_argument("type", nargs="?", help="action type, e.g. gmail_send")
    p_lim.add_argument("n", nargs="?", type=int, help="max per day (0 = off)")

    sub.add_parser("connectors", help="list configured MCP servers and plugins")
    p_mcp = sub.add_parser("add-mcp", help="add an MCP server connector")
    p_mcp.add_argument("name")
    p_mcp.add_argument("url")
    p_mcp.add_argument("--type", choices=["data", "action"], default="data")
    p_mcp.add_argument("--auth-env", help="env var holding the bearer token")
    p_mcp.add_argument("--specialists", help="comma-separated specialist keys")

    p_web = sub.add_parser("web", help="serve the browser chat UI")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8484)

    sub.add_parser("telegram", help="run the Telegram gateway "
                                    "(needs TELEGRAM_BOT_TOKEN)")
    p_wa = sub.add_parser("whatsapp", help="run the WhatsApp Cloud API gateway "
                                           "(needs WHATSAPP_* env vars)")
    p_wa.add_argument("--host", default="0.0.0.0")
    p_wa.add_argument("--port", type=int, default=8485)

    args = parser.parse_args(argv)

    from . import firstrun
    firstrun.load_env_file()        # saved keys apply to every command

    if args.command == "setup":
        firstrun.wizard()
    elif args.command == "profile":
        from . import profile
        user = "cli"
        if args.clear:
            print(profile.clear(user))
        elif args.set:
            print(profile.set_fact(user, args.set[0], args.set[1]))
        elif args.about:
            print(profile.set_about(user, " ".join(args.about)))
        else:
            card = profile.card(user)
            print(card.strip() if card else
                  "Nothing saved yet. Try: olympus profile \"I'm the founder "
                  "of Acme; keep replies concise.\"")
    elif args.command == "memory":
        from . import usermem, recall
        user = "cli"
        arg = " ".join(args.arg).strip()
        if args.action == "migrate":
            r = memory.migrate_notes()
            print(f"Migrated {r['migrated']} note(s) to schema v"
                  f"{memory.NOTE_SCHEMA_VERSION} (scanned {r['scanned']}).")
        elif args.action == "export":
            out = args.out or (arg or None)
            if not out:
                print("Where to? Use: memory export --out <archive> "
                      "[--user U | --all] [--encrypt]")
                return 1
            m = memory.export_memory(out, user=args.user,
                                     all_users=args.all, encrypt=args.encrypt)
            scope = "all users" if args.all else f"user '{m['scope']['user']}'"
            print(f"Exported {len(m['files'])} file(s) for {scope} to {out} "
                  f"(schema v{m['schema_version']}"
                  + (", encrypted)." if args.encrypt else ")."))
        elif args.action == "import":
            src = args.out or (arg or None)
            if not src:
                print("Which archive? Use: memory import <archive>")
                return 1
            try:
                r = memory.import_memory(src)
            except ValueError as err:
                print(f"[refused] {err}")
                return 1
            print(f"Imported {r['count']} file(s) (schema v"
                  f"{r['schema_version']}, {r['verified']} checksum-verified).")
        elif args.action == "delete":
            target = args.user or "shared"
            # Show what will go before doing it, then require confirmation.
            from . import config as _cfg
            roots = (memory._memory_roots(target) if not args.category
                     else [memory._dir(args.category, memory.safe_id(target))])
            doomed = [p.relative_to(_cfg.MEMORY_DIR).as_posix()
                      for r in roots if r.exists()
                      for p in sorted(r.rglob("*")) if p.is_file()
                      and (args.note_id is None
                           or args.note_id in (p.name, p.stem))]
            if not doomed:
                print("Nothing matches that scope — nothing deleted.")
                return 0
            print(f"This will permanently delete {len(doomed)} file(s):")
            for rel in doomed:
                print(f"  {rel}")
            if not args.yes:
                ok = input("Type 'delete' to confirm: ").strip().lower()
                if ok != "delete":
                    print("Aborted — nothing deleted.")
                    return 0
            removed = memory.delete_memory(target, category=args.category,
                                           note_id=args.note_id)
            print(f"Deleted {len(removed)} file(s).")
        elif args.action == "list":
            mems = usermem.active_memories(user)
            if not mems:
                print("No durable memories yet — they form as you chat.")
            for m in sorted(mems, key=lambda x: x["type"]):
                eff = usermem.effective_confidence(m)
                print(f"  [{m['id']}] ({m['type']}, conf {eff:.2f}) {m['content']}")
        elif args.action == "candidates":
            cands = usermem.candidates(user)
            if not cands:
                print("No memories awaiting your approval.")
            for c in cands:
                print(f"  [{c['id']}] ({c['type']}, {c.get('reason','')}) "
                      f"{c['content']}\n     → memory approve {c['id']} | reject {c['id']}")
        elif args.action == "approve":
            c = usermem.pop_candidate(user, arg)
            if not c:
                print("No such candidate.")
            else:
                usermem.add_memory(user, type=c["type"], content=c["content"],
                                   confidence=c.get("confidence", 0.7),
                                   key=c.get("key"),
                                   importance=c.get("importance", 0.5),
                                   sensitivity=c.get("sensitivity", "normal"),
                                   provenance=c.get("provenance", []))
                print("Saved.")
        elif args.action == "reject":
            print("Dismissed." if usermem.pop_candidate(user, arg) else "No such candidate.")
        elif args.action == "forget":
            print("Forgotten." if usermem.tombstone(user, arg) else "No such memory.")
        elif args.action == "search":
            hits = recall.retrieve(user, arg)
            if not hits:
                print("Nothing relevant.")
            for m in hits:
                print(f"  [{m['id']}] ({m['type']}) {m['content']}")
    elif args.command == "playbook":
        from . import playbooks
        user = "cli"
        name = args.name or ""
        if args.action in ("list", "proposed"):
            status = playbooks.PROPOSED if args.action == "proposed" else playbooks.ACTIVE
            items = playbooks.list_all(user, status=status)
            if not items:
                print("No playbooks yet." if args.action == "list"
                      else "No proposed playbooks.")
            for p in items:
                print(f"  [{p['id']}] {p['name']}  (v{p['version']}, "
                      f"used {p['use_count']}x, {len(p['steps'])} steps)")
        elif args.action == "show":
            p = playbooks.get(user, name)
            if not p:
                print("No such playbook.")
            else:
                print(f"{p['name']}  (v{p['version']}, {p['status']})")
                for i, s in enumerate(p["steps"], 1):
                    print(f"  {i}. {s}")
        elif args.action == "save":
            steps = " ".join(args.steps).split(";")
            try:
                p = playbooks.save(user, name, steps)
                print(f"Saved '{p['name']}' (v{p['version']}, {len(p['steps'])} steps). "
                      f"Run it anytime with: olympus playbook run \"{p['name']}\"")
            except ValueError as err:
                print(f"Error: {err}")
        elif args.action == "run":
            block = playbooks.run_block(user, name)
            print(block.strip() if block else "No such active playbook.")
        elif args.action == "approve":
            print("Approved." if playbooks.approve(user, name) else "No such playbook.")
        elif args.action == "reject":
            print("Removed." if playbooks.delete(user, name) else "No such playbook.")
        elif args.action == "forget":
            print("Forgotten." if playbooks.delete(user, name) else "No such playbook.")
    elif args.command == "graph":
        from . import relgraph
        user = "cli"
        if args.forget:
            print("Removed." if relgraph.forget(user, args.forget)
                  else "No such entity.")
        elif args.entity:
            desc = relgraph.describe(user, " ".join(args.entity))
            print(desc or "No such entity in the graph.")
        else:
            ns = relgraph.nodes(user)
            if not ns:
                print("The relationship graph is empty — it fills in as you "
                      "mention people and companies.")
            for n in ns:
                conns = relgraph.neighbors(user, n["id"])
                print(f"  {n['label']} ({n['kind']}) — {len(conns)} connection(s)")
    elif args.command == "outcomes":
        from . import outcomes
        user = "cli"
        s = outcomes.stats(user)["overall"]
        if not s["total"]:
            print("No outcomes yet — they accrue as you approve, edit, or "
                  "reject prepared actions.")
        else:
            print(f"Track record ({s['total']} actions): "
                  f"{s['approved']} approved as-is, "
                  f"{s['approved_after_edit']} approved after edit, "
                  f"{s['rejected']} rejected, {s['undone']} undone.")
            for ins in outcomes.insights(user):
                print(f"\n  💡 {ins['message']}")
    elif args.command == "status":
        from . import config, usage, accounts
        firstrun.load_env_file()
        s = config.Settings.from_env()
        print("OLYMPUS status")
        print(f"  provider/model : {s.provider} / {s.model or '(default)'}")
        print(f"  key configured : {'yes' if firstrun.configured() else 'NO'}")
        b = usage.budget_status()
        print(f"  daily budget   : "
              + (f"${b['spent']:.4f} / ${b['limit']:.2f}"
                 + ("  ⚠ reached" if b["exceeded"] else "")
                 if b["enabled"] else "off"))
        print(f"  accounts       : "
              + ("required (login)" if accounts.require_login() else "open"))
        print()
        print(usage.report(7))
    elif args.command == "replay":
        try:
            original, fresh, diffs = orchestrator.replay_run(args.run_id)
        except ValueError as err:
            print(f"[error] {err}")
            return 1
        n = len(original.get("decisions", []))
        if not diffs:
            print(f"✓ Re-executable replay of run {args.run_id}: "
                  f"{n} decision(s) replayed byte-identically against the "
                  "frozen LLM responses. The reasoning path is reproducible.")
        else:
            print(f"✗ Replay of run {args.run_id} diverged in "
                  f"{len(diffs)} decision(s):")
            for d in diffs:
                orig = d["original"] or {}
                rep = d["replayed"] or {}
                print(f"  - decision #{d['index']}: "
                      f"{orig.get('decision_type', '∅')} → "
                      f"{rep.get('decision_type', '∅')}")
            return 1
    elif args.command == "explain":
        import json
        from . import trace
        run = trace.load_run(args.id)
        if run:
            decisions = run.get("decisions", [])
            inp = (run.get("meta") or {}).get("input", "")
            print(f"Run {args.id} ({run.get('kind', '?')}) — "
                  f"{len(decisions)} decision(s)")
            if inp:
                print(f"  input: {inp[:200]}")
            for d in decisions:
                agent = (d.get("agent") or {}).get("model", "?")
                ref = d.get("model_response_ref") or "—"
                out = (d.get("outcome") or {}).get("status", "?")
                print(f"\n  [{d.get('record_id')}] {d.get('decision_type')} "
                      f"({out}, model={agent}, response={ref[:12]})")
                rat = d.get("rationale")
                if rat is not None:
                    text = rat if isinstance(rat, str) else \
                        json.dumps(rat, ensure_ascii=False)
                    print(f"    rationale: {text[:300]}")
        else:
            found = trace.find_record(args.id)
            if not found:
                print(f"No run or decision record '{args.id}' found.")
                return 1
            print(f"Decision {args.id} (run {found['run_id']}):")
            print(json.dumps(found["decision"], indent=2, ensure_ascii=False))
    elif args.command in (None, "chat"):
        if not firstrun.ensure_ready():
            return 1
        _chat()
    elif args.command == "ask":
        if not firstrun.ensure_ready():
            return 1
        from . import config
        bot = orchestrator.Olympus(
            report=lambda msg: print(f"  {msg}", file=sys.stderr),
            pool=config.ModelPool.from_env())
        print(bot.ask(" ".join(args.question)))
    elif args.command == "scan":
        print(orchestrator.opportunity_scan())
    elif args.command == "audit":
        print(orchestrator.evolution_audit())
    elif args.command == "watch":
        print(orchestrator.watch_and_learn(args.url))
    elif args.command == "queue":
        memory.watchlist_add(args.url)
        print(f"Queued for the next heartbeat pass: {args.url}")
    elif args.command == "learn":
        print(orchestrator.daily_learning())
    elif args.command == "eval":
        from . import evals
        print(evals.run_and_save())
    elif args.command == "code-eval":
        from . import code_eval
        print(code_eval.run_and_save())
    elif args.command == "skills":
        from . import skills
        print(skills.index())
    elif args.command == "gate":
        print(orchestrator.gate_skills())
    elif args.command == "train":
        print(orchestrator.train_specialists(focus=args.focus))
    elif args.command == "models":
        from . import config
        print(config.ModelPool.from_env().assignment())
    elif args.command in ("actions", "approve", "reject", "edit", "undo",
                          "autonomy", "grant", "revoke", "limit"):
        from . import actions, builtin_actions  # noqa: F401 (registers built-ins)
        user = "cli"
        if args.command == "actions":
            items = actions.pending(user)
            if not items:
                print("No actions awaiting approval.")
            for a in items:
                print(f"\n[{a.id}] {a.title}  ({a.risk_class})")
                if a.why:
                    print(f"  why: {a.why}")
                print("  " + a.preview.replace("\n", "\n  "))
                print(f"  → approve {a.id}  |  edit {a.id} field=value  |  "
                      f"reject {a.id} <reason>")
        elif args.command == "approve":
            a = actions.approve(user, args.action_id)
            print(f"{a.status}: {a.error or a.result}")
        elif args.command == "reject":
            a = actions.reject(user, args.action_id, " ".join(args.reason))
            print(f"Rejected {a.id}.")
        elif args.command == "edit":
            changes = {}
            for pair in args.changes:
                if "=" not in pair:
                    print(f"Skipping '{pair}' — expected field=value.")
                    continue
                key, _, value = pair.partition("=")
                changes[key.strip()] = value
            a = actions.edit(user, args.action_id, changes)
            print(f"Edited {a.id} — still awaiting approval. New preview:\n")
            print("  " + a.preview.replace("\n", "\n  "))
        elif args.command == "undo":
            a = actions.undo(user, args.action_id)
            print(f"{a.status}: {a.error or 'reversed'}")
        elif args.command == "autonomy":
            if args.level is None:
                print(f"Autonomy level: L{actions.autonomy_level(user)}")
            else:
                print(actions.set_autonomy(user, args.level))
        elif args.command == "grant":
            print(actions.grant_scope(user, args.scope))
        elif args.command == "revoke":
            print(actions.revoke_all(user) if args.scope == "all"
                  else actions.revoke_scope(user, args.scope))
        elif args.command == "limit":
            if args.type is None:
                eff = actions.limits(user)
                if not eff:
                    print("No action types registered.")
                print("Daily execution limits (0 = unlimited):")
                for name, n in sorted(eff.items()):
                    print(f"  {name}: {n if n else 'unlimited'}")
            elif args.n is None:
                print(f"{args.type}: {actions.daily_limit(user, args.type) or 'unlimited'} per day")
            else:
                print(actions.set_limit(user, args.type, args.n))
    elif args.command == "budget":
        from . import usage
        if args.amount is None:
            b = usage.budget_status()
            if not b["enabled"]:
                print("No daily budget set. Olympus will not cap spend on your "
                      "API key.\nSet one with `olympus budget 5` (USD/day).")
            else:
                print(f"Daily budget: ${b['spent']:.4f} / ${b['limit']:.2f} "
                      f"spent today" + ("  ⚠ reached" if b["exceeded"] else ""))
        else:
            print(usage.set_budget(args.amount))
    elif args.command == "scores":
        from . import evals
        scores = evals.per_specialist_scores()
        if not scores:
            print("No scores (benchmark unavailable — set ANTHROPIC_API_KEY).")
        else:
            for s, sc in sorted(scores.items(), key=lambda kv: kv[1]):
                print(f"  {s}: {sc}/10")
    elif args.command == "contrib":
        from . import contrib
        print(f"Cross-model contribution queue: {contrib.count()} snapshots.")
    elif args.command == "usage":
        from . import usage
        print(usage.report(args.days))
    elif args.command == "connectors":
        from . import connectors
        print(connectors.summary())
    elif args.command == "add-mcp":
        from . import connectors
        specialists = ([s.strip() for s in args.specialists.split(",")]
                       if args.specialists else None)
        print(connectors.add_mcp_server(args.name, args.url, args.type,
                                        args.auth_env, specialists))
    elif args.command == "heartbeat":
        try:
            heartbeat.run_forever()
        except KeyboardInterrupt:
            print("\nHeartbeat stopped.")
    elif args.command == "web":
        from . import web
        try:
            web.serve(args.host, args.port)
        except KeyboardInterrupt:
            print("\nWeb UI stopped.")
    elif args.command == "telegram":
        from . import telegram
        try:
            telegram.run_bot()
        except KeyboardInterrupt:
            print("\nTelegram gateway stopped.")
    elif args.command == "whatsapp":
        from . import whatsapp
        try:
            whatsapp.run_server(args.host, args.port)
        except KeyboardInterrupt:
            print("\nWhatsApp gateway stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
