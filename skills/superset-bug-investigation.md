### Skill: superset-bug-investigation

1. **Read everything first.** Issue body, every comment, linked issues/PRs. Note
   the reporter's Superset version, database engine, chart type, and exact
   steps. Write down expected vs observed behavior in one sentence each.
2. **Decide if the report is answerable.** If you cannot state expected
   behavior without guessing at product intent, stop and ask ONE precise
   question. Never fill gaps with assumptions.
3. **Locate the code path before running anything.** Identify the API
   endpoint, command/DAO, SQLAlchemy model, or React component involved.
   Superset layout: `superset/` (Flask backend), `superset-frontend/src/`
   (React/TS), `tests/unit_tests/` and `tests/integration_tests/`.
4. **Reproduce before theorizing.** Prefer the cheapest faithful reproduction:
   - backend: a focused pytest under `tests/unit_tests/` or a short Python
     snippet against the real function;
   - API: run Superset (`superset run` / docker compose) and call the endpoint;
   - UI: run the frontend and use the browser; capture a screenshot of the
     wrong behavior.
   Record the exact command/steps and the output as evidence.
5. **Confirm the symptom matches the report.** If current master behaves
   correctly, it is NOT_REPRODUCIBLE — report precisely what you tried.
6. **Check current behavior intent.** Search tests and docs, then use
   `git log -L` or blame on the relevant lines to see whether it is deliberate.
7. **Find the root cause, not the first suspicious line.** Trace data from
   input to the wrong output. Identify the specific line(s) and explain the
   mechanism. Rate your confidence honestly.
8. **Write a verification plan a stranger could execute**: exact test paths,
   API calls with payloads, or UI steps and the expected correct result.
9. **Return structured evidence.** Every claim in the output must be backed by
   something you actually observed.
