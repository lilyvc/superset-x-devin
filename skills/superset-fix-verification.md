### Skill: superset-fix-verification

1. **Reproduce first, again.** Before changing code, re-run the Investigator's
   reproduction on a clean checkout and confirm you see the failure. If you
   can't, stop and report BLOCKED rather than fixing blind.
2. **Write the regression test before the fix** where practical. Run it and
   confirm it FAILS for the right reason.
3. **Implement the smallest fix** at the root cause. Do not touch unrelated
   code, formatting, or dependencies.
4. **Run the regression test** — must pass now.
5. **Run targeted existing tests** for the touched module(s)
   (e.g. `pytest tests/unit_tests/<area>/`, `npm run test -- <path>` in
   `superset-frontend/`). Record each command and result verbatim.
6. **Re-run the ORIGINAL reproduction scenario** from step 1. For UI changes,
   drive the running app in the browser and capture an after-screenshot or a
   short recording. For API changes, capture the corrected response.
7. **Lint/type-check only the files you changed** (`pre-commit run --files
   ...`, `npm run type` for TS).
8. **Never claim success without evidence.** If any of steps 4–6 fail after
   reasonable iteration, report VERIFICATION_FAILED with what failed and why.
9. **PR body must let a reviewer verify without asking you**: Problem,
   Reproduction, Root cause, Fix, Regression coverage, Tests executed,
   Verification, Evidence, Related issue.
